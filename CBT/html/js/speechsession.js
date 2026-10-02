/**
 * SpeechSession — owns "what is currently being read, in which voice, at
 * what speed, and where are we in it".
 *
 * Before this module, that state was spread across app.js's local
 * variables, the <audio> element, and the highlighter, and the only
 * transition it knew how to make was "throw everything away and start
 * from zero". Mid-stream switching needs a real state machine, because it
 * introduces transitions that PRESERVE position across a source swap.
 *
 * The three transitions
 * ---------------------
 *   play(text, voice)   fresh start, position 0
 *   switchVoice(voice)  new audio + new timings, SAME reading position
 *   setRate(rate)       no audio change at all
 *
 * Why speed is free and voice is not
 * ----------------------------------
 * `audio.currentTime` is measured on the MEDIA timeline, which
 * `playbackRate` does not affect. A 1x timing bundle is therefore exactly
 * as correct at 1.5x — the audio and the highlight both consume the same
 * clock, so they accelerate together with no code in between. setRate()
 * is a one-line property assignment for that reason.
 *
 * Voice is the opposite. Male and female come from different acoustic
 * models (piper.py), so the same sentence occupies different seconds in
 * each. Seeking the new voice to the old voice's `currentTime` would
 * preserve the wall clock and lose the reading position — the listener
 * hears repeated or skipped sentences and the highlight lands on the
 * wrong word. Position is therefore carried through WORD space, not time
 * space (see js/timeline.js):
 *
 *      oldTime --resolve--> (ordinal, fraction) --project--> newTime
 *
 * Race safety
 * -----------
 * Three overlapping hazards, each handled explicitly:
 *
 *   1. Two switches in flight. The second must not read the position off
 *      the audio element — the first already paused it and is about to
 *      re-point it. So the position captured at INTENT time is retained
 *      in `_pendingSwitch` and reused. Because a PlaybackPosition is
 *      voice-independent, it stays valid no matter how many switches
 *      pile up.
 *
 *   2. A switch landing after a Stop or a chapter change. Every stage
 *      re-checks the RequestCoordinator token after its await, and a
 *      stale continuation returns without touching shared state.
 *
 *   3. A switch arriving before the first Play has finished generating.
 *      There is no position to preserve yet, so it degrades to a plain
 *      restart in the new voice.
 *
 * Prefetching
 * -----------
 * Two separate prefetches, both riding the background `prefetcher`
 * coordinator so neither can ever supersede or abort a real play():
 *
 *   - preloadText(text) — called as soon as a new section renders (see
 *     index.html), before the user has clicked Play at all. Warms the
 *     CURRENT voice's bundle ahead of time, so the first Play for a
 *     freshly-opened section is a client-cache hit instead of a cold
 *     network fetch.
 *   - _prefetchOtherVoice() — called after a successful play()/
 *     switchVoice(). speech_pipeline.handle_request() already generates
 *     the OTHER voice in the background on every request, so the server
 *     side of a switch is usually a cache hit; this closes the
 *     remaining gap by pulling that bundle across the wire too, so a
 *     switch typically costs zero network round trips.
 */

const VOICES = ["male", "female"];

const VOICE_LABELS = {
    male: "Male",
    female: "Female"
};

const SESSION_STATUS = {
    IDLE: "idle",
    GENERATING: "generating",
    PLAYING: "playing",
    PAUSED: "paused"
};


class SpeechSession {

    /**
     * @param {object}             deps
     * @param {AudioPlayer}        deps.player
     * @param {WordHighlighter}    deps.highlighter
     * @param {RequestCoordinator} deps.coordinator   guards user-facing work
     * @param {RequestCoordinator} deps.prefetcher    guards background warming
     * @param {object}             [deps.callbacks]
     * @param {function}           [deps.callbacks.onBusy]    (isBusy, message)
     * @param {function}           [deps.callbacks.onVoice]   (voice) authoritative voice
     * @param {function}           [deps.callbacks.onError]   (Error)
     */
    constructor({ player, highlighter, coordinator, prefetcher, callbacks = {} }) {

        if (!player || !highlighter || !coordinator) {
            throw new Error("SpeechSession requires player, highlighter and coordinator.");
        }

        this._player = player;
        this._highlighter = highlighter;
        this._coordinator = coordinator;
        this._prefetcher = prefetcher || null;

        this._onBusy = asFunction(callbacks.onBusy);
        this._onVoice = asFunction(callbacks.onVoice);
        this._onError = asFunction(callbacks.onError);

        // --- Session state ------------------------------------------

        this._text = "";
        this._voice = VOICES[0];
        this._rate = 1.0;

        this._bundle = null;                 // the bundle currently loaded
        this._timeline = Timeline.empty();   // its lookup view

        // voice -> bundle, for the CURRENT text only. Cleared whenever
        // the text changes, since a bundle is only valid for the text it
        // was aligned against.
        this._bundles = new Map();

        // Position + resume intent for a switch that is still in flight.
        // See hazard (1) in the class docstring.
        this._pendingSwitch = null;

        // Debounce handle for the inline loading indicator.
        this._busyTimer = null;
        this._busyShown = false;

        // Non-zero for the duration of a voice switch. A switch pauses
        // the element on its way through, which fires a real "pause"
        // event — without this the footer would blink Pause -> Resume ->
        // Pause on every switch. A COUNTER rather than a boolean, so two
        // overlapping switches cannot have the inner one clear the flag
        // while the outer one is still running.
        this._switchDepth = 0;
    }

    // ================================================================
    // Introspection
    // ================================================================

    get voice() {
        return this._voice;
    }

    get rate() {
        return this._rate;
    }

    get timeline() {
        return this._timeline;
    }

    /** True when a bundle is loaded into the audio element right now. */
    isLoaded() {
        return Boolean(this._bundle) && this._player.hasSource();
    }

    isPlaying() {
        return this._player.isPlaying();
    }

    /**
     * True while a voice swap is in progress. The UI uses this to ignore
     * the transient pause that a swap necessarily produces.
     */
    get isSwitching() {
        return this._switchDepth > 0;
    }

    // ================================================================
    // Text lifecycle
    // ================================================================

    /**
     * Point the session at a body of text. A change invalidates every
     * cached bundle: timings are aligned against specific words, so a
     * bundle for the previous chapter would index spans that no longer
     * exist in the DOM.
     *
     * @param {string} text
     * @returns {boolean} true if the text actually changed
     */
    attachText(text) {

        const next = typeof text === "string" ? text.trim() : "";

        if (next === this._text) {
            return false;
        }

        this._text = next;

        this._discardBundles("text changed");

        return true;
    }

    _discardBundles(reason) {

        if (this._prefetcher) {
            this._prefetcher.cancel(reason);
        }

        this._bundles.clear();

        this._bundle = null;

        this._timeline = Timeline.empty();

        this._pendingSwitch = null;
    }

    // ================================================================
    // Voice selection (no active playback)
    // ================================================================

    /**
     * Set the voice used by the NEXT play, without touching playback.
     * Used when nothing is loaded, where "switching" is just a preference.
     */
    selectVoice(voice) {

        if (!SpeechSession.isKnownVoice(voice) || voice === this._voice) {
            return this._voice;
        }

        this._voice = voice;

        this._onVoice(this._voice);

        return this._voice;
    }

    // ================================================================
    // Speed — instant, mid-stream, no network
    // ================================================================

    /**
     * Apply a playback rate. Safe at any time, in any state.
     *
     * Nothing else needs to happen: the highlighter reads the same
     * `audio.currentTime` the browser is already re-pacing, so words
     * speed up and slow down in lockstep automatically.
     *
     * @param {number} rate
     * @returns {number} the rate actually applied (clamped)
     */
    setRate(rate) {

        this._rate = this._player.setRate(rate);

        console.log(`[session] playback rate -> ${this._rate}x`);

        return this._rate;
    }

    // ================================================================
    // Play — fresh start from position zero
    // ================================================================

    /**
     * @param {object} [options]
     * @param {string} [options.text]  defaults to the attached text
     * @param {string} [options.voice] defaults to the current voice
     * @returns {Promise<boolean>} true if this call ended up owning playback
     */
    async play({ text, voice } = {}) {

        if (typeof text === "string") {
            this.attachText(text);
        }

        if (SpeechSession.isKnownVoice(voice)) {
            this._voice = voice;
        }

        if (!this._text) {
            throw new Error("No content to read.");
        }

        // --- Synchronous prelude ------------------------------------
        // Runs to completion before the first await, so by the time we
        // suspend, the previous request is dead and the element is
        // silent and empty. Nothing can interleave into a half state.

        const { token, signal } = this._coordinator.begin();

        this._pendingSwitch = null;

        this._player.unload();
        this._highlighter.reset();

        const requestedVoice = this._voice;

        this._setBusy(true, "Generating speech…");

        try {

            if (!this._highlighter.prepared) {
                this._highlighter.prepare();
            }

            const bundle = await this._loadBundle(requestedVoice, signal);

            if (this._coordinator.isStale(token)) {

                console.log(`[session] #${token} discarded after bundle fetch`);

                return false;
            }

            this._adoptBundle(bundle);

            this._highlighter.syncTo(0);

            await this._player.playAt(bundle.audio, {
                offset: 0,
                autoplay: true,
                signal
            });

            if (this._coordinator.isStale(token)) {

                // Superseded between load and play resolving. The newer
                // request already owns the element — do NOT clean up
                // here, that would kill ITS audio.
                console.log(`[session] #${token} discarded after play`);

                return false;
            }

            this._coordinator.complete(token);

            this._setBusy(false);

            this._prefetchOtherVoice();

            return true;
        }
        catch (err) {

            return this._handleFailure(err, token, "play");
        }
    }

    // ================================================================
    // Voice switch — new audio, same reading position
    // ================================================================

    /**
     * @param {string} nextVoice
     * @returns {Promise<boolean>} true if this call ended up owning playback
     */
    async switchVoice(nextVoice) {

        if (!SpeechSession.isKnownVoice(nextVoice)) {

            console.warn("[session] unknown voice:", nextVoice);

            return false;
        }

        if (nextVoice === this._voice) {
            return false;
        }

        // Nothing loaded and nothing in flight — this is a preference
        // change, not a switch.
        if (!this.isLoaded() && !this._coordinator.isBusy()) {

            this.selectVoice(nextVoice);

            return false;
        }

        // The first Play is still generating, so there is no position to
        // preserve. Restart cleanly in the new voice; the coordinator
        // aborts the in-flight generation for us.
        if (!this.isLoaded()) {

            console.log("[session] switch during generation — restarting");

            return this.play({ voice: nextVoice });
        }

        // --- Capture, once, at intent time ---------------------------
        // Reuse the pending capture if a switch is already in flight:
        // that one has already paused the element, so reading it now
        // would give a position that means nothing.

        const inFlight = this._pendingSwitch;

        const capture = inFlight
            ? inFlight
            : {
                position: this._timeline.resolve(this._player.currentTime()),
                from: this._timeline,
                resume: this._player.isPlaying()
            };

        const previousVoice = this._voice;

        const { token, signal } = this._coordinator.begin();

        this._pendingSwitch = capture;

        this._switchDepth += 1;

        // Stop the outgoing voice immediately — it must not keep reading
        // past the moment the user asked for a different one.
        this._player.pause();

        // Optimistic: the dropdown already shows the new voice, and the
        // failure path below puts it back.
        this._voice = nextVoice;

        this._setBusy(true, `Switching to ${SpeechSession.labelFor(nextVoice)}…`);

        console.log(
            `[session] #${token} switching ${previousVoice} -> ${nextVoice} ` +
            `at word ${capture.position.wordIndex} ` +
            `(resume: ${capture.resume})`
        );

        try {

            const bundle = await this._loadBundle(nextVoice, signal);

            if (this._coordinator.isStale(token)) {

                console.log(`[session] #${token} switch discarded after fetch`);

                return false;
            }

            const nextTimeline = this._adoptBundle(bundle);

            // Project the captured word position onto the new voice's
            // clock. This is the line the whole feature exists for.
            const targetTime = Timeline.projectPosition(
                capture.position,
                capture.from,
                nextTimeline
            );

            // Re-anchor the highlight BEFORE the audio moves, so the UI
            // is already correct at the instant sound resumes. Usually a
            // no-op in the DOM: the correct word has not changed, only
            // the second at which it is spoken.
            this._highlighter.syncTo(targetTime);

            await this._player.playAt(bundle.audio, {
                offset: targetTime,
                autoplay: capture.resume,
                signal
            });

            if (this._coordinator.isStale(token)) {

                console.log(`[session] #${token} switch discarded after seek`);

                return false;
            }

            this._pendingSwitch = null;

            this._coordinator.complete(token);

            this._setBusy(false);

            console.log(
                `[session] #${token} switched to ${nextVoice} ` +
                `at ${targetTime.toFixed(3)}s`
            );

            this._prefetchOtherVoice();

            return true;
        }
        catch (err) {

            // Put the control back where the audio actually is, so the
            // dropdown can never claim a voice that is not playing.
            if (this._coordinator.isCurrent(token)) {

                this._voice = previousVoice;

                this._onVoice(previousVoice);
            }

            return this._handleFailure(err, token, "voice switch");
        }
        finally {

            this._switchDepth -= 1;
        }
    }

    // ================================================================
    // Transport
    // ================================================================

    /**
     * @returns {boolean} true if audio is playing after the toggle
     */
    togglePause() {

        if (!this._player.hasSource()) {
            return false;
        }

        if (this._player.isPlaying()) {

            this._player.pause();

            return false;
        }

        this._player.resume();

        return true;
    }

    /** Stop and rewind, cancelling anything in flight. */
    stop() {

        this._coordinator.cancel("stopped by user");

        this._setBusy(false);

        this._pendingSwitch = null;

        this._player.reset();

        this._highlighter.reset();
    }

    /** Full teardown — chapter change or hard reset. */
    reset() {

        this._coordinator.cancel("session reset");

        this._setBusy(false);

        this._player.reset();

        this._highlighter.clear();

        this._text = "";

        this._discardBundles("session reset");
    }

    // ================================================================
    // Bundles
    // ================================================================

    /**
     * Fetch (or reuse) the complete bundle for a voice.
     *
     * Client-side memoisation matters here beyond saving a round trip:
     * it means a user toggling back and forth between two voices pays
     * the network cost at most once per voice per chapter.
     */
    async _loadBundle(voice, signal) {

        const cached = this._bundles.get(voice);

        if (cached) {

            console.log(`[session] bundle for '${voice}' served from client cache`);

            return cached;
        }

        const bundle = await fetchSpeechBundle(this._text, voice, { signal });

        this._bundles.set(voice, bundle);

        return bundle;
    }

    /**
     * Make a bundle the live one: build its timeline and hand it to the
     * highlighter WITHOUT clearing the current highlight (see
     * WordHighlighter.setTimeline).
     *
     * @returns {Timeline}
     */
    _adoptBundle(bundle) {

        const timeline = new Timeline(bundle.timings);

        this._bundle = bundle;
        this._timeline = timeline;

        this._highlighter.setTimeline(timeline);

        this._onVoice(this._voice);

        return timeline;
    }

    /**
     * Point the session at new text and immediately start warming the
     * CURRENT voice's bundle in the background — called as soon as a new
     * section finishes rendering (see index.html's renderContent()),
     * well before the user has clicked Play. By the time they do,
     * _loadBundle() inside play() finds the bundle already sitting in
     * the client cache instead of starting a cold fetch — which is what
     * actually gets a cached chapter playing in well under a second: the
     * network round trip already happened while the user was still
     * reading the page, not after they clicked Play.
     *
     * Uses attachText() (not a bespoke "pending text" field) specifically
     * because it already IS "point the session at this text" — play()
     * calls the exact same method and no-ops harmlessly if it's already
     * attached, so there's no second code path to keep in sync with it.
     *
     * Same separate-coordinator reasoning as _prefetchOtherVoice() below:
     * this must never touch this._coordinator (the user-facing one) or a
     * slow preload could supersede/abort a real play() the instant the
     * user actually clicks it.
     *
     * @param {string} text
     */
    preloadText(text) {

        const changed = this.attachText(text);

        if (!changed && this._bundles.has(this._voice)) {
            // Already attached to this exact text, and the current
            // voice is already warmed or warming — nothing new to do.
            return;
        }

        if (!this._prefetcher || !this._text) {
            return;
        }

        const target = this._voice;

        if (this._bundles.has(target)) {
            return;
        }

        const { token, signal } = this._prefetcher.begin();

        (async () => {

            try {

                const bundle = await fetchSpeechBundle(this._text, target, { signal });

                if (this._prefetcher.isStale(token)) {
                    return;
                }

                this._bundles.set(target, bundle);

                await warmAudio(bundle.audio, { signal });

                if (this._prefetcher.isStale(token)) {
                    return;
                }

                this._prefetcher.complete(token);

                console.log(`[session] '${target}' preloaded for the new section — Play should be instant`);
            }
            catch (err) {

                // Same as _prefetchOtherVoice(): a failed/aborted preload
                // costs nothing. Play() will simply fetch normally.
                if (!RequestCoordinator.isAbortError(err)) {

                    console.warn(
                        `[session] preload of '${target}' failed:`,
                        err && err.message
                    );
                }
            }
        })();

    }

    /**
     * Warm the other voice in the background so the next switch is free.
     *
     * Deliberately uses a SEPARATE coordinator: a prefetch must never
     * supersede or abort the user's own playback pipeline, and the user's
     * next action must be able to abort the prefetch without ambiguity.
     */
    _prefetchOtherVoice() {

        if (!this._prefetcher || !this._text) {
            return;
        }

        const target = SpeechSession.otherVoice(this._voice);

        if (this._bundles.has(target)) {
            return;
        }

        const { token, signal } = this._prefetcher.begin();

        (async () => {

            try {

                const bundle = await fetchSpeechBundle(this._text, target, { signal });

                if (this._prefetcher.isStale(token)) {
                    return;
                }

                this._bundles.set(target, bundle);

                await warmAudio(bundle.audio, { signal });

                if (this._prefetcher.isStale(token)) {
                    return;
                }

                this._prefetcher.complete(token);

                console.log(`[session] '${target}' prefetched — switch is now instant`);
            }
            catch (err) {

                // A failed prefetch costs nothing: the switch simply
                // fetches normally. Never surfaced to the user.
                if (!RequestCoordinator.isAbortError(err)) {

                    console.warn(
                        `[session] prefetch of '${target}' failed:`,
                        err && err.message
                    );
                }
            }
        })();
    }

    // ================================================================
    // Failure handling
    // ================================================================

    /**
     * @returns {boolean} always false — the caller did not win
     */
    _handleFailure(err, token, what) {

        // A superseded request tears its own fetches down; that surfaces
        // as an AbortError, which is a success signal, not a failure.
        if (RequestCoordinator.isAbortError(err) || this._coordinator.isStale(token)) {

            console.log(`[session] #${token} ${what} aborted`);

            return false;
        }

        console.error(`[session] ${what} failed`, err);

        this._coordinator.cancel(`${what} failed`);

        this._setBusy(false);

        this._onError(err instanceof Error ? err : new Error(String(err)));

        return false;
    }

    // ================================================================
    // Inline loading indicator
    // ================================================================

    /**
     * Debounced so the overwhelmingly common case — a cache hit that
     * resolves in single-digit milliseconds — never flashes a spinner.
     * An indicator that appears and vanishes within one frame reads as a
     * glitch, not as feedback.
     */
    _setBusy(isBusy, message = "") {

        if (this._busyTimer !== null) {

            clearTimeout(this._busyTimer);

            this._busyTimer = null;
        }

        if (!isBusy) {

            if (this._busyShown) {

                this._busyShown = false;

                this._onBusy(false, "");
            }

            return;
        }

        this._busyTimer = setTimeout(() => {

            this._busyTimer = null;
            this._busyShown = true;

            this._onBusy(true, message);

        }, SpeechSession.BUSY_INDICATOR_DELAY_MS);
    }

    // ================================================================
    // Static helpers
    // ================================================================

    static isKnownVoice(voice) {
        return VOICES.includes(voice);
    }

    static otherVoice(voice) {
        return voice === "male" ? "female" : "male";
    }

    static labelFor(voice) {
        return VOICE_LABELS[voice] || voice;
    }
}

SpeechSession.BUSY_INDICATOR_DELAY_MS = 140;

SpeechSession.VOICES = VOICES;
SpeechSession.STATUS = SESSION_STATUS;


function asFunction(candidate) {

    return typeof candidate === "function"
        ? candidate
        : () => {};
}


if (typeof module !== "undefined" && module.exports) {
    module.exports = { SpeechSession, VOICES, VOICE_LABELS };
}
