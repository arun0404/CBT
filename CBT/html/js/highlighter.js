/**
 * WordHighlighter — renders the active word, driven by a Timeline.
 *
 * Split of responsibilities
 * -------------------------
 * All "which word is spoken at time T" logic now lives in Timeline (see
 * js/timeline.js). This class is a pure renderer: it owns the DOM spans
 * and the .active-word class, and nothing else.
 *
 * That split is what makes mid-stream voice switching cheap. Switching
 * voices changes the TIMELINE but not the DOM — the same chapter, the
 * same words, the same spans. So a switch is `setTimeline(next)` followed
 * by `syncTo(newTime)`, with no re-tokenisation, no re-wrapping, and no
 * flicker: if the projected time resolves to the same word the user was
 * already on (which is the whole point of projecting through word space),
 * highlight() early-returns and the DOM is never touched at all.
 *
 * Emoji handling is inherited from Timeline: entries flagged `emoji` by
 * the backend (timing.py / text_sanitizer.is_pure_emoji_word) are absent
 * from the spoken lookup space entirely, so they render normally but can
 * never become a highlight target or shift the index of a neighbour.
 *
 * Audio-output latency compensation
 * --------------------------------
 * `<audio>.currentTime` tracks the DECODE/render clock — the position the
 * media pipeline has processed to — NOT the sample physically leaving the
 * speaker. The OS output buffer (and, at playbackRate != 1, the
 * pitch-preserving time-stretcher) hold tens to a few hundred milliseconds
 * of already-rendered audio. Because the highlight is driven purely by
 * currentTime, an uncompensated highlight leads the sound by exactly that
 * buffer depth, and the reading feels like the highlight is "running
 * ahead". This class looks each word up at a slightly EARLIER media
 * position (see `_lookupTime`) so the highlight tracks the acoustic onset.
 *
 * Forced-alignment timestamps themselves are correct at every speed and
 * are NOT rescaled per playbackRate: currentTime and the timing bundle
 * are both on the media timeline, so they already move in lockstep. What
 * DOES scale with the rate is the buffer's depth measured in media time —
 * 90 ms of real sound is 180 ms of media at 2x — so `_lookupTime` scales
 * the (wall-clock) latency offset by the current rate before subtracting.
 */
class WordHighlighter {

    constructor(containerId) {

        this.container = document.getElementById(containerId);

        if (!this.container) {
            throw new Error(`WordHighlighter: no element with id "${containerId}".`);
        }

        this.wordElements = [];

        this.timeline = Timeline.empty();

        this.currentIndex = -1;

        this.prepared = false;

        // Suppresses scrollIntoView for one highlight. A mid-stream
        // switch re-asserts the highlight programmatically; yanking the
        // viewport for a word the user is already looking at is jarring.
        this._suppressScrollOnce = false;

        // Audio→highlight sync calibration. `_audioLatency` is wall-clock
        // seconds of output-buffer delay to compensate for; `_playbackRate`
        // converts it into media-time inside `_lookupTime`. app.js seeds
        // both (from localStorage / the speed control) and exposes a
        // runtime tuning hook.
        this._audioLatency = WordHighlighter.DEFAULT_AUDIO_LATENCY;
        this._playbackRate = 1;
    }

    // --------------------------------------------------
    // Audio / visual sync calibration
    // --------------------------------------------------

    /**
     * Set the output-latency compensation, in wall-clock seconds. The
     * highlight lookup is shifted this far earlier (scaled by playbackRate)
     * so words light up on the acoustic onset rather than on the render
     * clock. Adjustable at runtime so a high-latency output path
     * (Bluetooth, an external DAC) can be calibrated per deployment.
     *
     * @param {number} seconds
     * @returns {number} the clamped value actually applied
     */
    setAudioLatency(seconds) {

        const value = Number(seconds);

        if (Number.isFinite(value)) {
            // Bounded so a mistyped value can't strand the highlight far
            // from the audio. Negative is permitted (a pipeline that runs
            // the highlight LATE) but equally bounded.
            this._audioLatency = Math.max(-0.5, Math.min(value, 1.5));
        }

        return this._audioLatency;
    }

    getAudioLatency() {
        return this._audioLatency;
    }

    /**
     * Keep the highlighter aware of the active playback rate so the
     * media-time latency offset tracks it. Called by app.js whenever the
     * speed control changes.
     *
     * @param {number} rate
     */
    setPlaybackRate(rate) {

        const value = Number(rate);

        this._playbackRate = Number.isFinite(value) && value > 0 ? value : 1;
    }

    /**
     * Map a raw media-clock reading to the media position whose audio is
     * physically reaching the listener right now. Never returns a negative
     * time (the compensated lookup simply pins to 0 during the opening
     * `_audioLatency` seconds, exactly as an uncompensated lookup does at
     * t = 0).
     *
     * @param {number} currentTime seconds, straight off the media element
     * @returns {number}
     */
    _lookupTime(currentTime) {

        const shifted = currentTime - this._audioLatency * this._playbackRate;

        return shifted > 0 ? shifted : 0;
    }

    // --------------------------------------------------
    // Prepare document (run once per chapter)
    // --------------------------------------------------

    prepare() {

        if (this.prepared) {
            return;
        }

        this.wordElements = [];

        this.currentIndex = -1;

        const walker = document.createTreeWalker(

            this.container,

            NodeFilter.SHOW_TEXT,

            {
                acceptNode: (node) => {

                    if (!node.textContent.trim()) {
                        return NodeFilter.FILTER_REJECT;
                    }

                    const parent = node.parentElement;

                    if (!parent) {
                        return NodeFilter.FILTER_REJECT;
                    }

                    if (parent.tagName === "SCRIPT" || parent.tagName === "STYLE") {
                        return NodeFilter.FILTER_REJECT;
                    }

                    if (parent.classList.contains("tts-word")) {
                        return NodeFilter.FILTER_REJECT;
                    }

                    return NodeFilter.FILTER_ACCEPT;
                }
            }
        );

        const textNodes = [];

        while (walker.nextNode()) {
            textNodes.push(walker.currentNode);
        }

        textNodes.forEach((node) => {
            this.wrapTextNode(node);
        });

        this.prepared = true;

        console.log("Prepared Words:", this.wordElements.length);
    }

    // --------------------------------------------------
    // Wrap one text node
    //
    // Emoji are ordinary characters to this tokenizer — they stay wrapped
    // like any other word and remain fully visible. Whether a word is
    // ever eligible to be a highlight TARGET is decided by Timeline, from
    // the "emoji" flag on its matching timing entry.
    // --------------------------------------------------

    wrapTextNode(textNode) {

        const fragment = document.createDocumentFragment();

        const parts = textNode.textContent.match(/\S+|\s+/g);

        if (!parts) {
            return;
        }

        parts.forEach((part) => {

            if (/^\s+$/.test(part)) {

                fragment.appendChild(document.createTextNode(part));

                return;
            }

            const span = document.createElement("span");

            span.className = "tts-word";
            span.textContent = part;

            fragment.appendChild(span);

            this.wordElements.push(span);
        });

        textNode.parentNode.replaceChild(fragment, textNode);
    }

    // --------------------------------------------------
    // New chapter loaded
    // --------------------------------------------------

    refresh() {

        this.reset();

        this.container.querySelectorAll(".tts-word").forEach((span) => {
            span.replaceWith(document.createTextNode(span.textContent));
        });

        // Merge adjacent text nodes so re-tokenisation sees whole runs.
        this.container.normalize();

        this.prepared = false;

        this.wordElements = [];

        this.timeline = Timeline.empty();

        this.currentIndex = -1;

        this.prepare();
    }

    // --------------------------------------------------
    // Timing bundles
    // --------------------------------------------------

    /**
     * Swap in a new lookup timeline WITHOUT clearing the current
     * highlight. That is deliberate: during a voice switch the correct
     * word does not change, only the seconds at which it is spoken, so
     * clearing here would produce a visible blink and a redundant scroll.
     *
     * Call syncTo() straight afterwards to re-anchor to the new clock.
     *
     * @param {Timeline} timeline
     */
    setTimeline(timeline) {

        if (!(timeline instanceof Timeline)) {
            throw new TypeError("setTimeline() expects a Timeline instance.");
        }

        this.timeline = timeline;

        if (timeline.wordCount !== this.wordElements.length) {

            console.warn(
                `Word/timing count mismatch: ${this.wordElements.length} spans ` +
                `vs ${timeline.wordCount} timings. Highlighting may drift.`
            );
        }
    }

    /**
     * Convenience for callers holding a raw timing array.
     * @param {Array} timings
     */
    setTimings(timings) {

        this.setTimeline(new Timeline(timings));

        return this.timeline;
    }

    // --------------------------------------------------
    // Rendering
    // --------------------------------------------------

    /** Clear the active highlight, keeping the timeline and the spans. */
    reset() {

        if (this.currentIndex >= 0 && this.currentIndex < this.wordElements.length) {

            this.wordElements[this.currentIndex].classList.remove("active-word");
        }

        this.currentIndex = -1;
    }

    /**
     * Highlight one word by its index into wordElements.
     *
     * Only ever called with an index resolved through Timeline, so an
     * emoji's index can never reach it.
     */
    highlight(index) {

        if (index < 0 || index >= this.wordElements.length) {
            return;
        }

        const suppressScroll = this._suppressScrollOnce;

        this._suppressScrollOnce = false;

        if (index === this.currentIndex) {

            // Already correct — the common case on a voice switch.
            return;
        }

        this.reset();

        this.currentIndex = index;

        const element = this.wordElements[index];

        element.classList.add("active-word");

        if (suppressScroll) {
            return;
        }

        element.scrollIntoView({
            behavior: "smooth",
            block: "center",
            inline: "nearest"
        });
    }

    /** Full teardown — used on chapter change / hard reset. */
    clear() {

        this.reset();

        this.timeline = Timeline.empty();

        this.wordElements = [];

        this.prepared = false;
    }

    // --------------------------------------------------
    // Playback-driven updates
    // --------------------------------------------------

    /**
     * Called from AudioPlayer's animation-frame loop.
     *
     * `currentTime` is on the media timeline, so this stays correct at
     * every playbackRate with no rate awareness here whatsoever — the
     * highlight speeds up and slows down in lockstep for free.
     *
     * @param {number} currentTime seconds
     */
    update(currentTime) {

        // Look the word up at the position whose sound is actually
        // audible now, not at the render-clock position (which is ahead
        // by the output-buffer depth). See the class docstring.
        const index = this.timeline.domIndexAt(this._lookupTime(currentTime));

        if (index === -1) {

            // Before the first spoken word (lead-in silence, or a leading
            // emoji with nothing spoken yet) — nothing to highlight.
            return;
        }

        this.highlight(index);
    }

    /**
     * Re-anchor to an absolute time immediately, without scrolling.
     *
     * Used after a mid-stream voice switch and after a scrub: the audio
     * has jumped, and the highlight must agree at once rather than on the
     * next animation frame (which never arrives while paused).
     *
     * @param {number} currentTime seconds on the CURRENT timeline
     */
    syncTo(currentTime) {

        this._suppressScrollOnce = true;

        this.update(currentTime);

        // update() may have returned early (index -1), leaving the flag
        // armed for an unrelated later highlight. Always disarm.
        this._suppressScrollOnce = false;
    }

    /**
     * Capture the current playback position in voice-independent word
     * space, so it can be projected onto another voice's timeline.
     *
     * @param {number} currentTime seconds
     * @returns {object} PlaybackPosition — see js/timeline.js
     */
    positionAt(currentTime) {
        return this.timeline.resolve(currentTime);
    }
}

// Default audio-output latency to compensate for, in wall-clock seconds.
// An <audio> element's currentTime typically leads the speaker by ~80-150 ms
// on built-in output (more over Bluetooth). This is a deliberately
// conservative middle value; app.js persists any per-machine override and
// exposes window.CBT.setSyncOffset(ms) for calibration.
WordHighlighter.DEFAULT_AUDIO_LATENCY = 0.11;

if (typeof module !== "undefined" && module.exports) {
    module.exports = { WordHighlighter };
}
