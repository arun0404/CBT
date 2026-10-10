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
// --------------------------------------------------------------------
// Words that are never spoken
//
// A DOM word made ONLY of dashes, quotation marks, invisible/zero-width
// characters, NBSP or emoji ("–", '"', "“”", "📄", a lone space ...) is
// structural decoration: the backend strips it before Piper (see
// text_sanitizer.py, whose lists this mirrors EXACTLY) and flags its timing
// entry silent. It must never take the highlight.
//
// Deliberately NOT matched, because the backend SPEAKS them: "°", arrows
// ("→", "↔"), "©", "®", "™", "&", "%" ... so no broad emoji class such as
// \p{Extended_Pictographic} is used here.
// --------------------------------------------------------------------
const NON_SPOKEN_ONLY_RE = new RegExp(
    "^[" +
    "\\s" +                                                  // incl. NBSP
    "\\u00AD\\u180E\\u200B-\\u200F\\u2060\\uFEFF" +          // invisible / zero-width
    "\\-\\u2010-\\u2015\\u2212" +                            // hyphen-minus, hyphens, dashes, minus
    "\"'\\u2018-\\u201F\\u00AB\\u00BB\\u2039\\u203A" +       // straight, curly and angle quotes
    "\\u2600-\\u27BF\\u2B00-\\u2BFF\\uFE0F" +                // misc symbols, dingbats
    "\\u{1F1E6}-\\u{1F1FF}\\u{1F300}-\\u{1F64F}" +           // flags, pictographs, emoticons
    "\\u{1F680}-\\u{1F8FF}\\u{1F900}-\\u{1FAFF}" +           // transport .. extended pictographs
    "]*$",
    "u"
);

/**
 * Is this word element something that is actually spoken?
 *
 * False for an empty element and for one containing only the non-spoken
 * characters above. Used to keep such an element from ever being the
 * active word, including when it is one piece of a word split across
 * several elements ("📄" + "Replace").
 *
 * @param {Element} element
 * @returns {boolean}
 */
function isSpokenWord(element) {

    const text = (element && element.textContent) || "";

    return text.length > 0 && !NON_SPOKEN_ONLY_RE.test(text);
}

class WordHighlighter {

    constructor(containerId) {

        this.container = document.getElementById(containerId);

        if (!this.container) {
            throw new Error(`WordHighlighter: no element with id "${containerId}".`);
        }

        // One entry per word the TIMINGS know about, in order. The element
        // that takes the highlight (and the scroll) for that word.
        this.wordElements = [];

        // wordGroups[i] = every DOM piece of word i. Almost always one
        // span; several when the word is split across elements with no
        // whitespace between them ("📄" glued to "Replace").
        this.wordGroups = [];

        // Every span prepare() created, aligned or not, in document order.
        this._allSpans = [];

        // True once wordElements/wordGroups were derived from the text the
        // backend receives (see _alignToPostedText); _alignFailed when that
        // was tried on a rendered container and could not be done.
        this._aligned = false;
        this._alignFailed = false;

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
        this.wordGroups = [];
        this._allSpans = [];
        this._aligned = false;
        this._alignFailed = false;

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

        // Every span just created. wordElements starts out as that same
        // list (one span per word, the long-standing behaviour) and is
        // replaced by the aligned list below when that is possible.
        this._allSpans = this.wordElements.slice();
        this.wordGroups = this._allSpans.map((span) => [span]);

        this._alignToPostedText();

        this.prepared = true;

        console.log(
            "Prepared Words:", this.wordElements.length,
            this._aligned ? "(aligned to the posted text)" : "(not aligned)"
        );
    }

    // --------------------------------------------------
    // Keep the DOM's words in step with the words the backend is given
    //
    // The timings hold exactly one entry per WORD OF THE TEXT POSTED TO
    // /speak, and that text is the container's innerText. The highlighter
    // pairs the two by position, so its word list must be that same list.
    // Wrapping every \S+ run of every text node does not guarantee it:
    //
    //   * pieces glued together in innerText but separate in the DOM:
    //     <span>📄</span><a>Replace</a> is ONE word "📄Replace" to the
    //     backend and TWO spans here; so is "H<sub>2</sub>O", or a comma
    //     in its own element after "LiDAR";
    //   * text in the DOM that innerText leaves out: the labels inside an
    //     SVG figure, the fallback text of a <video>, anything hidden.
    //
    // Either way the DOM ends up with extra words and every later highlight
    // is shifted by that many. Dashes, quotes, NBSP and emoji are not the
    // cause (as their own whitespace-separated words they pair up exactly,
    // and are flagged silent); it is when they sit in their own element
    // with no space beside them.
    //
    // So: take the posted text's words as the truth and give each one its
    // DOM span(s), skipping spans that are not in the posted text at all.
    // If that cannot be done cleanly the old one-span-per-word list is kept
    // (and the reason logged), so this can never be worse than before.
    // --------------------------------------------------

    /**
     * @returns {boolean} true if wordElements/wordGroups now follow the
     *          posted text word for word.
     */
    _alignToPostedText() {

        const spans = this._allSpans;

        this._aligned = false;

        if (!spans.length) {
            return false;
        }

        // innerText of a container that is not being rendered is just its
        // textContent (blocks run together), NOT what is posted when the
        // user presses play. Align later, once it is on screen.
        if (this.container.getClientRects().length === 0) {
            return false;
        }

        const tokens = (this.container.innerText.match(/\S+/g) || [])
            .map((token) => token.toLowerCase());     // innerText applies text-transform

        // Laid out but with invisible text (visibility:hidden, e.g. the
        // locked app) has an EMPTY innerText while the spans exist. There is
        // nothing to align to yet; aligning to zero words would throw every
        // span away. Try again when the timeline arrives.
        if (!tokens.length) {
            return false;
        }

        const texts = spans.map((span) => span.textContent.toLowerCase());

        // How many unrelated spans may sit between two posted words.
        // Generous (a diagram can carry dozens of labels) but bounded, so
        // a hopeless mismatch fails fast instead of scanning the chapter
        // once per word.
        const MAX_SKIP = 600;

        const groups = [];

        let next = 0;               // first span not yet used

        for (let i = 0; i < tokens.length; i++) {

            const token = tokens[i];

            let matched = false;

            for (let start = next; start < spans.length && start - next <= MAX_SKIP; start++) {

                let built = "";
                let end = start;

                while (end < spans.length && built.length < token.length) {

                    built += texts[end];
                    end++;

                    if (!token.startsWith(built)) {
                        break;
                    }
                }

                if (built === token) {

                    groups.push(spans.slice(start, end));

                    next = end;

                    matched = true;

                    break;
                }
            }

            if (!matched) {

                console.warn(
                    `[highlighter] word ${i} "${tokens[i]}" of the posted text was not found in the DOM; ` +
                    `keeping one span per word (highlighting may drift).`
                );

                this._alignFailed = true;

                return false;
            }
        }

        // Each word's highlight target is its first SPOKEN piece, so the
        // emoji of "📄Replace" never carries the highlight (and the scroll).
        this.wordGroups = groups;
        this.wordElements = groups.map((pieces) => pieces.find(isSpokenWord) || pieces[0]);

        this._aligned = true;

        return true;
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
        this.wordGroups = [];
        this._allSpans = [];
        this._aligned = false;
        this._alignFailed = false;

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

        // The chapter may have been prepared while it was not on screen
        // (alignment needs the container rendered); by the time audio
        // arrives it is. Nothing is highlighted yet, so swapping the word
        // list now is safe.
        if (!this._aligned && !this._alignFailed && this.currentIndex === -1) {
            this._alignToPostedText();
        }

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

            // Every piece, not just the target: a word split across
            // elements is highlighted as a whole.
            for (const piece of this.wordGroups[this.currentIndex] || [this.wordElements[this.currentIndex]]) {
                piece.classList.remove("active-word");
            }
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

        // Highlight the SPOKEN pieces of the word. A piece that is only
        // dashes, quotes, NBSP or an emoji ("📄" glued to "Replace") is
        // skipped; and a word with no spoken piece at all highlights
        // nothing. (The Timeline already keeps such words, flagged silent
        // by the backend, from being chosen; this is the second guard.)
        for (const piece of this.wordGroups[index] || [element]) {

            if (isSpokenWord(piece)) {
                piece.classList.add("active-word");
            }
        }

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
        this.wordGroups = [];
        this._allSpans = [];
        this._aligned = false;
        this._alignFailed = false;

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
    module.exports = { WordHighlighter, isSpokenWord };
}
