/**
 * Backend API client.
 *
 * Every call takes an optional AbortSignal so a superseded request can
 * tear its network work down immediately instead of racing to completion
 * behind a newer one (see js/requestcoordinator.js).
 *
 * Endpoints (host.py):
 *   POST /speak  -> { success, audio, timing, voice, cached }
 *   GET  <timing url> -> [ { word, start, end, emoji? }, ... ]
 *
 * Synthesis speed vs playback speed
 * ---------------------------------
 * The backend can bake a speed into the audio itself: piper.py maps
 * `speed` to Piper's `--length_scale`, and speech_pipeline._make_key()
 * folds it into the cache key. That is genuinely higher fidelity than
 * time-stretching, but it is the wrong tool for a speed CONTROL:
 *
 *   - changing speed becomes a full resynthesis + forced-alignment pass
 *     (seconds), which cannot be done mid-stream;
 *   - it emits a new timing array, so the highlighter must be rebuilt;
 *   - it multiplies the cache by the number of speeds (2 voices x 4
 *     speeds = 8 entries per chapter instead of 2), which in turn
 *     wrecks the hit rate of the background other-voice prefetch that
 *     makes voice switching instant.
 *
 * So the client now always synthesizes at SYNTHESIS_SPEED (1.0) and
 * applies the user's chosen speed as `audio.playbackRate` instead. That
 * is instant, needs no network, and — because `audio.currentTime` is on
 * the media timeline and unaffected by playbackRate — keeps every cached
 * timing bundle valid at every speed.
 *
 * This requires NO backend change: host.py already accepts and honours
 * whatever `speed` it is given.
 */

const API = {
    SPEAK: "/speak"
};

/**
 * The only speed ever sent to Piper. Changing this re-partitions the
 * server-side alignment cache, so treat it as a deployment constant.
 */
const SYNTHESIS_SPEED = 1.0;


// --------------------------------------------------
// Navigation epoch
//
// Aborting a fetch() stops the CLIENT reading a response, but the server
// keeps synthesizing and force-aligning for a page the user has already
// left — and every /speak also spawns a background thread doing the same
// work for the other voice. Navigating quickly across three uncached
// pages therefore leaves six jobs competing for the CPU, which is what
// makes the page actually being viewed slow to arrive.
//
// So each /speak carries this counter. The server cancels any still-
// running generation from a LOWER epoch (killing its Piper process) when
// a higher one arrives — see native_host/cancellation.py. It is the
// across-the-wire counterpart of RequestCoordinator's monotonic token.
//
// Deliberately NOT bumped per request: two requests from the same page
// (the foreground play and its other-voice prefetch) must share an epoch
// so neither cancels the other. Only real navigation advances it — see
// bumpNavEpoch(), called from renderContent()/showLanding().
// --------------------------------------------------

let navEpoch = 0;

function bumpNavEpoch() {
    navEpoch += 1;
    return navEpoch;
}

function currentNavEpoch() {
    return navEpoch;
}


// --------------------------------------------------
// Internal: JSON fetch with uniform error semantics
// --------------------------------------------------

async function fetchJson(url, options = {}) {

    const response = await fetch(url, options);

    let payload = null;

    try {
        payload = await response.json();
    }
    catch (err) {

        // A non-JSON body (an HTML 404 page, a proxy error) must not
        // surface as an opaque "Unexpected token <" SyntaxError.
        if (!response.ok) {
            throw new Error(`${url} returned HTTP ${response.status}.`);
        }

        throw new Error(`${url} returned a malformed JSON body.`);
    }

    return { response, payload };
}


// --------------------------------------------------
// POST /speak
//
// Resolves with the server's payload as-is. A backend-reported failure
// (success:false) is returned, NOT thrown — it's a normal, expected
// outcome the caller shows to the user. Network/abort problems throw.
// --------------------------------------------------

async function generateSpeech(text, voice, { signal } = {}) {

    const { response, payload } = await fetchJson(API.SPEAK, {

        method: "POST",

        headers: {
            "Content-Type": "application/json"
        },

        body: JSON.stringify({
            text: text,
            voice: voice,
            speed: SYNTHESIS_SPEED,
            nav_epoch: navEpoch
        }),

        signal: signal
    });

    // A server-side cancellation (HTTP 409 + cancelled:true — the newer
    // navigation superseded this generation, see native_host/
    // cancellation.py) is NOT a failure and must never reach the user.
    // Surfacing it as an ordinary !success payload would make
    // fetchSpeechBundle throw a plain Error, which SpeechSession's
    // _handleFailure treats as real and escalates to an alert().
    // Re-shaping it as an AbortError instead means every existing
    // RequestCoordinator.isAbortError() guard already swallows it
    // correctly — no new handling needed anywhere downstream.
    if (response.status === 409 && payload && payload.cancelled) {

        throw new DOMException(
            payload.message || "Superseded by a newer request.",
            "AbortError"
        );
    }

    if (!response.ok) {

        return {
            success: false,
            message: payload && payload.message
                ? payload.message
                : `Speech generation failed (HTTP ${response.status}).`
        };
    }

    return payload;
}


// --------------------------------------------------
// GET <timing url>
//
// The URL comes from the /speak response, so it always points at the
// bundle that matches the audio we're about to play — content-addressed
// for cache hits, ephemeral for forced-alignment fallbacks. Never
// hard-code a shared "timing.json" path here: that is itself a race,
// since two voices generated back-to-back would overwrite each other.
// --------------------------------------------------

async function getTimings(timingUrl, { signal } = {}) {

    if (!timingUrl) {
        throw new Error("No timing URL was returned by /speak.");
    }

    const response = await fetch(timingUrl, {

        // Content-addressed bundles are immutable and safe to cache; the
        // ephemeral fallback slot reuses filenames, so it must always be
        // read through to the server.
        cache: /\/ephemeral\//.test(timingUrl) ? "no-store" : "default",

        signal: signal
    });

    if (!response.ok) {
        throw new Error(`${timingUrl} returned HTTP ${response.status}.`);
    }

    const timings = await response.json();

    if (!Array.isArray(timings)) {
        throw new Error(`${timingUrl} did not contain a timing array.`);
    }

    return timings;
}


// --------------------------------------------------
// Composite: fetch a complete, playable speech bundle
//
// Both entry points that need speech — first Play, and a mid-stream
// voice switch — need exactly the same two stages in the same order.
// Keeping that sequence in one place is what stops the switch path from
// drifting out of sync with the play path as either evolves.
// --------------------------------------------------

/**
 * @param {string}      text
 * @param {string}      voice  "male" | "female"
 * @param {object}      [options]
 * @param {AbortSignal} [options.signal]
 * @returns {Promise<{voice:string,audio:string,timing:string,timings:Array,cached:boolean}>}
 * @throws  {Error} on network failure, abort, or a backend-reported error
 */
async function fetchSpeechBundle(text, voice, { signal } = {}) {

    const result = await generateSpeech(text, voice, { signal });

    if (!result || !result.success) {

        throw new Error(
            (result && result.message) || "Speech generation failed."
        );
    }

    // /speak now embeds the timing array directly (see speech_pipeline.py's
    // _response_for) so a normal play — cached or not — costs exactly ONE
    // round trip instead of two: this used to unconditionally follow up
    // with a separate GET for result.timing on every single call, even
    // when the server already had the array sitting in memory and had
    // just serialized result.timing purely so the client could re-fetch
    // the same data a second time. Falling back to that GET only if an
    // older/unexpected response omits the array keeps this
    // forward-compatible rather than a hard dependency on the new field.
    const timings = Array.isArray(result.timings)
        ? result.timings
        : await getTimings(result.timing, { signal });

    return {
        voice: result.voice || voice,
        audio: result.audio,
        timing: result.timing,
        timings: timings,
        cached: Boolean(result.cached)
    };
}


/**
 * Warm the browser's HTTP cache for an already-known audio URL.
 *
 * The server prefetches the OTHER voice's bundle as soon as one voice is
 * requested (speech_pipeline._prefetch_secondary), so by the time a user
 * switches, the WAV usually exists on disk. This pulls it across the wire
 * ahead of time as well, so the switch costs no network round trip at
 * all. Failure is entirely uninteresting — it just means the switch pays
 * the normal fetch cost.
 *
 * @param {string}      audioUrl
 * @param {object}      [options]
 * @param {AbortSignal} [options.signal]
 * @returns {Promise<boolean>} true if the resource was warmed
 */
async function warmAudio(audioUrl, { signal } = {}) {

    if (!audioUrl) {
        return false;
    }

    try {

        const response = await fetch(audioUrl, {
            cache: "force-cache",
            signal: signal
        });

        // The body must actually be drained for the entry to land in the
        // HTTP cache; a bare fetch() leaves the stream unconsumed.
        await response.arrayBuffer();

        return response.ok;
    }
    catch (err) {

        if (err && err.name === "AbortError") {
            return false;
        }

        console.warn("[api] audio warm-up skipped:", err && err.message);

        return false;
    }
}


if (typeof module !== "undefined" && module.exports) {
    module.exports = {
        API,
        SYNTHESIS_SPEED,
        bumpNavEpoch,
        currentNavEpoch,
        generateSpeech,
        getTimings,
        fetchSpeechBundle,
        warmAudio
    };
}
