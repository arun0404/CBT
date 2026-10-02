/**
 * AudioPlayer — thin, stateful wrapper around a single HTMLAudioElement.
 *
 * Concurrency contract
 * --------------------
 * There is exactly ONE <audio> element for the whole app. That is
 * deliberate (a pool of elements is how you get two tracks audible at
 * once), but it makes the element a piece of shared mutable state: a
 * superseded request must never be allowed to point it at an old source.
 * Two mechanisms keep that safe:
 *
 *   1. unload() — a HARD teardown. Pausing alone is not enough: the
 *      element keeps its src, its buffered data and its readyState, so a
 *      late `play()` from a dead request would happily resume the old
 *      track mid-stream. unload() pauses, drops the source attribute and
 *      re-runs the resource selection algorithm, leaving the element
 *      genuinely empty.
 *
 *   2. Load epochs — every load bumps `_epoch`. Media events are
 *      asynchronous, so a source we abandoned can still emit `error` or a
 *      late `loadedmetadata` after we've moved on. Handlers stamped with a
 *      stale epoch are dropped instead of firing callbacks that would
 *      corrupt the current session's duration / progress bar / UI state.
 *
 * app.js owns the "latest request wins" decision (see
 * js/requestcoordinator.js); this class only guarantees the element can't
 * be left in a half-torn-down state.
 *
 * Playback rate
 * -------------
 * `playbackRate` is now SESSION state, not element state. Speed is applied
 * client-side rather than baked in at synthesis time (see the note in
 * speechsession.js), so it must survive every source swap: a mid-stream
 * voice switch loads a new WAV, and the user's 1.5x must still be in force
 * when it starts. `_rate` is the source of truth and `_applyRate()`
 * re-asserts it after every load — some engines reset the property when
 * the resource selection algorithm re-runs.
 *
 * Crucially, `currentTime` is measured on the MEDIA timeline and is
 * unaffected by `playbackRate`. That is why word highlighting needs no
 * rate awareness at all: the timings stay valid at every speed, and the
 * highlight naturally accelerates in lockstep with the audio.
 */
class AudioPlayer {

    constructor() {

        this.audio = new Audio();

        this.audio.preload = "auto";

        // ---------------------------------------
        // Event Callbacks
        // ---------------------------------------

        this.onPlay = null;
        this.onPause = null;
        this.onEnded = null;
        this.onTimeUpdate = null;
        this.onLoadedMetadata = null;
        this.onError = null;

        // ---------------------------------------
        // High-frequency progress polling
        //
        // The native "timeupdate" event only fires every ~200-250ms.
        // Short function words ("to", "is", "at") are frequently spoken
        // in less time than that, so driving highlighting from it means
        // those words can be skipped entirely — no sample ever lands
        // inside their window.
        //
        // requestAnimationFrame runs ~60fps (~16ms apart), giving
        // highlighter.update() enough resolution to catch every word.
        // The same loop drives the seek bar's fill.
        // ---------------------------------------

        this._rafId = null;

        // ---------------------------------------
        // Load epoch — see the class docstring.
        // ---------------------------------------

        this._epoch = 0;
        this._liveEpoch = 0;

        // True while we are deliberately tearing a source down. The
        // browser fires a spurious "error" (MEDIA_ELEMENT_ERROR: Empty
        // src) when load() runs with no source attached — that is OUR
        // doing, not a playback failure, and must not reach onError.
        this._tearingDown = false;

        // Session-level playback rate; survives source swaps.
        this._rate = 1.0;

        this._applyRate();

        this._bindEvents();
    }

    // ---------------------------------------
    // Media events
    // ---------------------------------------

    _bindEvents() {

        this.audio.addEventListener("play", () => {

            console.log("Audio Started");

            // Re-assert on the transition out of paused: this is the last
            // point before sound is actually produced, so a rate that any
            // earlier step dropped is corrected before it can be heard.
            this._applyRate();

            if (this.onPlay) {
                this.onPlay();
            }

            this._startProgressLoop();
        });

        this.audio.addEventListener("pause", () => {

            console.log("Audio Paused");

            this._stopProgressLoop();

            // A pause caused by unload()/stop() during teardown is
            // bookkeeping, not a user-visible "Paused" state.
            if (this._tearingDown) {
                return;
            }

            if (this.onPause) {
                this.onPause();
            }
        });

        this.audio.addEventListener("ended", () => {

            console.log("Audio Finished");

            this._stopProgressLoop();

            if (this.onEnded) {
                this.onEnded();
            }
        });

        // Duration becomes known once metadata is parsed. Both events are
        // wired because some browsers report the final duration via
        // "durationchange" slightly after "loadedmetadata"; the callback
        // is idempotent so firing twice is harmless. The epoch check stops
        // a discarded source's late metadata from resizing the seek bar
        // for the track that replaced it.
        const emitDuration = () => {

            if (this._tearingDown || this._epoch !== this._liveEpoch) {
                return;
            }

            this._applyRate();

            if (this.onLoadedMetadata && Number.isFinite(this.audio.duration)) {
                this.onLoadedMetadata(this.audio.duration);
            }
        };

        this.audio.addEventListener("loadedmetadata", emitDuration);
        this.audio.addEventListener("durationchange", emitDuration);

        this.audio.addEventListener("error", (event) => {

            this._stopProgressLoop();

            if (this._tearingDown) {

                // Expected: we emptied the element on purpose.
                return;
            }

            if (this._epoch !== this._liveEpoch) {

                console.warn("Ignoring error from a superseded audio source.");

                return;
            }

            console.error("Audio Error", this.audio.error);

            if (this.onError) {
                this.onError(event);
            }
        });
    }

    // ---------------------------------------
    // Progress loop (internal)
    // ---------------------------------------

    _startProgressLoop() {

        // Defensive: never let two loops run concurrently (e.g. if "play"
        // somehow fires twice without an intervening "pause").
        this._stopProgressLoop();

        const step = () => {

            if (this.audio.paused || this.audio.ended) {
                this._rafId = null;
                return;
            }

            this._emitTime();

            this._rafId = requestAnimationFrame(step);
        };

        this._rafId = requestAnimationFrame(step);
    }

    _stopProgressLoop() {

        if (this._rafId !== null) {

            cancelAnimationFrame(this._rafId);

            this._rafId = null;
        }
    }

    /**
     * Push the current position to listeners once, outside the RAF loop.
     *
     * Needed after a seek that happens while paused (a mid-stream switch
     * of a paused track, or a scrub): the loop is not running, so nothing
     * would otherwise refresh the highlight and the seek bar.
     */
    _emitTime() {

        if (this.onTimeUpdate) {
            this.onTimeUpdate(this.audio.currentTime);
        }
    }

    // ---------------------------------------
    // Rate
    // ---------------------------------------

    _applyRate() {

        // preservesPitch keeps 1.5x from sounding chipmunked. It is the
        // default in current engines but is explicitly asserted here
        // because the vendor-prefixed forms are not, and a silently
        // pitch-shifted voice is a bug users report as "broken audio".
        if ("preservesPitch" in this.audio) {
            this.audio.preservesPitch = true;
        }

        if ("mozPreservesPitch" in this.audio) {
            this.audio.mozPreservesPitch = true;
        }

        if ("webkitPreservesPitch" in this.audio) {
            this.audio.webkitPreservesPitch = true;
        }

        if (this.audio.playbackRate !== this._rate) {
            this.audio.playbackRate = this._rate;
        }
    }

    /**
     * Change playback speed. Takes effect immediately, mid-stream, with
     * no reload and no re-synthesis — and because word timings live on
     * the media timeline, the highlight re-paces itself for free.
     *
     * @param {number} rate
     * @returns {number} the clamped rate actually applied
     */
    setRate(rate) {

        const requested = Number(rate);

        if (!Number.isFinite(requested) || requested <= 0) {

            console.warn("[player] ignoring invalid playback rate:", rate);

            return this._rate;
        }

        // Beyond this band the browser's time-stretcher produces audible
        // artefacts, and some engines refuse the assignment outright and
        // silently snap back to 1.0.
        this._rate = Math.max(
            AudioPlayer.MIN_RATE,
            Math.min(requested, AudioPlayer.MAX_RATE)
        );

        if (this._rate !== requested) {

            console.warn(
                `[player] rate ${requested} clamped to ${this._rate}.`
            );
        }

        this._applyRate();

        return this._rate;
    }

    getRate() {
        return this._rate;
    }

    // Retained for source compatibility with existing call sites.
    setSpeed(speed) {
        return this.setRate(speed);
    }

    getSpeed() {
        return this._rate;
    }

    // ---------------------------------------
    // Hard teardown of the current source
    //
    // Leaves the element with NO source at all — not merely paused. This
    // is what guarantees an abandoned track can never resume or overlap
    // once a newer request takes over.
    //
    // Note: `_rate` is deliberately NOT reset. It is session state chosen
    // by the user, and it must survive every source swap.
    // ---------------------------------------

    unload() {

        this._tearingDown = true;

        try {

            this._stopProgressLoop();

            this.audio.pause();

            // Order matters: remove the attribute, THEN load(). Setting
            // src="" would resolve against the page URL and try to fetch
            // the document itself as media.
            this.audio.removeAttribute("src");

            // Re-runs resource selection against an empty source, which
            // is what actually flushes the decoded buffer and resets
            // readyState/currentTime.
            this.audio.load();
        }
        finally {

            // Media events are queued, not synchronous, so the spurious
            // "empty src" error arrives on a later task. Keep suppression
            // on until the current task queue drains.
            setTimeout(() => {
                this._tearingDown = false;
            }, 0);
        }
    }

    // ---------------------------------------
    // Loading
    // ---------------------------------------

    /**
     * Point the element at a source and resolve once its metadata (and
     * therefore its duration and seekable range) is known.
     *
     * Seeking before HAVE_METADATA is unreliable — the assignment is
     * either ignored or clamped against a duration of NaN — so every
     * "load then jump to an offset" flow has to wait here first.
     *
     * @param {string}      audioUrl
     * @param {object}      [options]
     * @param {AbortSignal} [options.signal] rejects the wait early
     * @returns {Promise<number>} the loaded epoch
     */
    load(audioUrl, { signal } = {}) {

        if (!audioUrl) {
            return Promise.reject(new Error("load() requires an audio URL."));
        }

        this.unload();

        this._epoch += 1;
        this._liveEpoch = this._epoch;

        const epoch = this._epoch;

        this.audio.src = AudioPlayer.resolveSourceUrl(audioUrl);

        this.audio.load();

        this._applyRate();

        return this._whenMetadata(epoch, signal);
    }

    /**
     * Cache-busting policy.
     *
     * Cached bundles are content-addressed (output/alignment_cache/<key>)
     * and immutable, so they MUST stay cacheable — the browser reusing a
     * prefetched WAV is exactly what makes a voice switch feel instant.
     *
     * The ephemeral fallback slot (output/ephemeral/<id>) reuses
     * filenames across requests, so a stale 200-from-disk there would
     * serve the previous track. Only those get a buster.
     */
    static resolveSourceUrl(audioUrl) {

        if (!AudioPlayer.VOLATILE_URL_PATTERN.test(audioUrl)) {
            return audioUrl;
        }

        const separator = audioUrl.includes("?") ? "&" : "?";

        return `${audioUrl}${separator}t=${Date.now()}`;
    }

    _whenMetadata(epoch, signal) {

        return new Promise((resolve, reject) => {

            // Already parsed (memory/HTTP cache hit) — nothing to wait for.
            if (this.audio.readyState >= HTMLMediaElement.HAVE_METADATA) {
                resolve(epoch);
                return;
            }

            let settled = false;

            const cleanup = () => {

                this.audio.removeEventListener("loadedmetadata", onReady);
                this.audio.removeEventListener("error", onFail);

                if (signal) {
                    signal.removeEventListener("abort", onAbort);
                }

                clearTimeout(timer);
            };

            const finish = (fn, value) => {

                if (settled) {
                    return;
                }

                settled = true;

                cleanup();

                fn(value);
            };

            const onReady = () => {

                // A newer load already superseded this one; its own
                // waiter owns the element now.
                if (epoch !== this._liveEpoch) {
                    finish(reject, new DOMException("superseded", "AbortError"));
                    return;
                }

                finish(resolve, epoch);
            };

            const onFail = () => {

                const code = this.audio.error && this.audio.error.code;

                finish(
                    reject,
                    new Error(`Audio source failed to load (code ${code}).`)
                );
            };

            const onAbort = () => {
                finish(reject, new DOMException("aborted", "AbortError"));
            };

            // A source that never fires either event (offline server,
            // dead socket) would otherwise hang the switch and leave the
            // loading indicator up forever.
            const timer = setTimeout(() => {

                finish(
                    reject,
                    new Error("Timed out waiting for audio metadata.")
                );

            }, AudioPlayer.METADATA_TIMEOUT_MS);

            this.audio.addEventListener("loadedmetadata", onReady);
            this.audio.addEventListener("error", onFail);

            if (signal) {

                if (signal.aborted) {
                    onAbort();
                    return;
                }

                signal.addEventListener("abort", onAbort);
            }
        });
    }

    // ---------------------------------------
    // Play
    // ---------------------------------------

    /**
     * Load a source, position it, and optionally start it.
     *
     * The offset is applied BEFORE play() rather than after, so a
     * mid-stream switch never leaks a fraction of a second of the new
     * voice's opening words before jumping to the real position.
     *
     * @param {string}      audioUrl
     * @param {object}      [options]
     * @param {number}      [options.offset=0]      seconds to start at
     * @param {boolean}     [options.autoplay=true] start, or just arm it
     * @param {AbortSignal} [options.signal]
     * @returns {Promise<number>} the clamped start position, in seconds
     */
    async playAt(audioUrl, { offset = 0, autoplay = true, signal } = {}) {

        await this.load(audioUrl, { signal });

        const position = this.seek(offset);

        // Repaint the highlight and seek bar at the new position even
        // when we are not about to start — a switch made while paused
        // must still land the user on the right word.
        this._emitTime();

        if (!autoplay) {
            return position;
        }

        try {

            await this.audio.play();
        }
        catch (err) {

            // AbortError here means a newer request already called
            // unload()/play() and interrupted this one — exactly the
            // outcome we want. Surface it so the caller can distinguish
            // it from a real decode/network failure; the session layer
            // swallows it once it sees its request token is stale.
            if (err && err.name === "AbortError") {
                console.warn("Playback superseded before it started.");
            }
            else {
                console.error(err);
            }

            throw err;
        }

        return position;
    }

    /**
     * Play a source from the beginning. Thin alias over playAt().
     */
    play(audioUrl) {
        return this.playAt(audioUrl, { offset: 0, autoplay: true });
    }

    // ---------------------------------------
    // Seek to an absolute position (seconds)
    //
    // Clamped into the valid range and applied immediately. Works whether
    // the audio is playing or paused. Returns the clamped target so
    // callers can keep their own UI in exact sync.
    // ---------------------------------------

    seek(time) {

        // Seeking an element with no source throws in some browsers.
        if (!this.hasSource()) {
            return 0;
        }

        const duration = this.audio.duration;

        let target = Number(time);

        if (!Number.isFinite(target) || target < 0) {
            target = 0;
        }

        if (Number.isFinite(duration) && target > duration) {
            target = duration;
        }

        this.audio.currentTime = target;

        return target;
    }

    // ---------------------------------------
    // Transport
    // ---------------------------------------

    pause() {

        if (!this.audio.paused) {
            this.audio.pause();
        }
    }

    resume() {

        if (!this.hasSource()) {
            return;
        }

        if (this.audio.paused) {

            // Ignore the rejection: a resume that loses a race with a new
            // request is a no-op, not an error worth alerting about.
            this.audio.play().catch((err) => {
                console.warn("Resume ignored:", err && err.name);
            });
        }
    }

    /**
     * Stop playback and rewind, keeping the source loaded so Play can
     * restart it without a re-fetch.
     */
    stop() {

        this._stopProgressLoop();

        this.audio.pause();

        if (this.hasSource()) {
            this.audio.currentTime = 0;
        }
    }

    /**
     * Full reset — stop AND drop the source entirely (chapter change,
     * hard reset of the reader).
     */
    reset() {
        this.unload();
    }

    // ---------------------------------------
    // Introspection
    // ---------------------------------------

    hasSource() {
        return Boolean(this.audio.getAttribute("src"));
    }

    currentTime() {
        return this.audio.currentTime;
    }

    duration() {
        return this.audio.duration;
    }

    isPlaying() {

        return !this.audio.paused &&
               !this.audio.ended &&
               this.hasSource();
    }

    /** True once a source is loaded far enough to be seeked. */
    isSeekable() {

        return this.hasSource() &&
               this.audio.readyState >= HTMLMediaElement.HAVE_METADATA;
    }
}

AudioPlayer.MIN_RATE = 0.25;
AudioPlayer.MAX_RATE = 4.0;

AudioPlayer.METADATA_TIMEOUT_MS = 20000;

// Only these paths are rewritten with a cache buster — see
// resolveSourceUrl(). Mirrors config.EPHEMERAL_URL_PREFIX.
AudioPlayer.VOLATILE_URL_PATTERN = /\/ephemeral\//;

const player = new AudioPlayer();

if (typeof module !== "undefined" && module.exports) {
    module.exports = { AudioPlayer };
}
