/**
 * RequestCoordinator — "latest request wins" for user-triggered async work.
 *
 * Problem it solves
 * -----------------
 * Clicking Play kicks off a multi-stage async pipeline:
 *
 *      POST /speak  ->  GET <timing>.json  ->  player.play(<audio>.wav)
 *
 * Each stage is an `await`. If the user clicks Play again mid-pipeline
 * (different voice, different speed), a SECOND pipeline starts while the
 * first is still suspended at one of those awaits. Neither pipeline knows
 * the other exists, so whichever the network happens to resolve LAST wins
 * — it overwrites the highlighter's timings and re-points the <audio>
 * element, cutting into whatever is already playing. Requests can even
 * complete out of order (a cache HIT for request #3 returns in ~5ms while
 * request #1 is still synthesizing), which is exactly the "plays the 3rd,
 * then jumps back to the 1st" symptom.
 *
 * The rule
 * --------
 * Only the MOST RECENT request is allowed to touch shared state (the audio
 * element, the highlighter, the progress bar). Every older one is dead the
 * instant a newer one begins.
 *
 * That is enforced on two levels, and both are needed:
 *
 *   1. AbortController — a *push* cancel. Aborting the signal tears down
 *      in-flight fetches immediately, so a superseded request stops
 *      consuming a connection and its `await` rejects with an AbortError
 *      rather than lingering.
 *
 *   2. Monotonic token — a *pull* check. AbortController cannot rewind
 *      work that has ALREADY resolved: a fetch may be sitting in the
 *      microtask queue, resolved, milliseconds before the abort lands. The
 *      token lets the continuation ask "am I still the current request?"
 *      after every await, before it mutates anything shared.
 *
 * Ownership
 * ---------
 * Deliberately knows nothing about audio, TTS, or the DOM — it's a generic
 * concurrency primitive, reusable for any "supersede the previous one"
 * interaction (chapter prefetch, search-as-you-type, etc.).
 */
class RequestCoordinator {

    /**
     * @param {object}   [options]
     * @param {string}   [options.name]     Label used in log output.
     * @param {function} [options.onAbort]  Called with (abortedToken) whenever a
     *                                      live request is superseded or cancelled.
     */
    constructor({ name = "request", onAbort = null } = {}) {

        this.name = name;

        this.onAbort = typeof onAbort === "function" ? onAbort : null;

        // Monotonically increasing. Never reused, never reset — a token
        // is a permanent identity, so a stale continuation can never be
        // mistaken for a fresh one.
        this._token = 0;

        this._activeToken = null;

        this._controller = null;
    }

    // --------------------------------------------------
    // Start a new request, superseding any live one
    // --------------------------------------------------

    /**
     * @returns {{token: number, signal: AbortSignal}} Handle for the new request.
     */
    begin() {

        // Kill the previous request BEFORE minting the new token, so the
        // old pipeline's post-await guards already see themselves as stale.
        this.cancel("superseded");

        this._token += 1;

        this._activeToken = this._token;

        this._controller = new AbortController();

        console.log(`[${this.name}] begin #${this._activeToken}`);

        return {
            token: this._activeToken,
            signal: this._controller.signal
        };
    }

    // --------------------------------------------------
    // Cancel whatever is in flight (Stop, chapter change, teardown)
    // --------------------------------------------------

    /**
     * @param {string} [reason] Logged; also passed to AbortController.abort().
     * @returns {boolean} true if a live request was actually cancelled.
     */
    cancel(reason = "cancelled") {

        if (this._controller === null) {
            return false;
        }

        const aborted = this._activeToken;

        const controller = this._controller;

        // Clear state FIRST. controller.abort() runs the signal's listeners
        // synchronously, and those listeners may re-enter this coordinator
        // (e.g. an error handler calling cancel() again) — leaving stale
        // fields in place while that happens would recurse.
        this._controller = null;
        this._activeToken = null;

        controller.abort(new DOMException(reason, "AbortError"));

        console.log(`[${this.name}] cancel #${aborted} (${reason})`);

        if (this.onAbort) {
            this.onAbort(aborted);
        }

        return true;
    }

    // --------------------------------------------------
    // Guards — call after EVERY await, before touching shared state
    // --------------------------------------------------

    /** Is this token still the one and only live request? */
    isCurrent(token) {
        return token === this._activeToken;
    }

    /** Inverse of isCurrent(), for readable early-returns. */
    isStale(token) {
        return token !== this._activeToken;
    }

    /**
     * Mark a request as finished successfully. No-op if the request was
     * already superseded, so a slow winner can't clear a newer one's state.
     *
     * @returns {boolean} true if this token was the live request.
     */
    complete(token) {

        if (this.isStale(token)) {
            return false;
        }

        this._controller = null;
        this._activeToken = null;

        console.log(`[${this.name}] complete #${token}`);

        return true;
    }

    /** Is any request currently in flight? */
    isBusy() {
        return this._activeToken !== null;
    }

    /**
     * True when an error is just this coordinator (or the browser) tearing
     * down a superseded request — i.e. expected, and must never surface to
     * the user as a failure alert.
     */
    static isAbortError(error) {

        return Boolean(error) && (
            error.name === "AbortError" ||
            error.code === 20                 // legacy DOMException.ABORT_ERR
        );
    }
}

// Node (tests) exports the class; the browser picks it up off the global
// scope like every other module in js/.
if (typeof module !== "undefined" && module.exports) {
    module.exports = { RequestCoordinator };
}
