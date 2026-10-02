/**
 * Timeline — an immutable, queryable view over one voice's timing bundle.
 *
 * Why this exists
 * ---------------
 * Two separate concerns were previously tangled inside WordHighlighter:
 *
 *   1. "Which word is spoken at time T?"          (lookup)
 *   2. "Which DOM span should carry .active-word?" (rendering)
 *
 * Mid-stream voice switching needs (1) WITHOUT (2), and needs it against
 * two different bundles at once — the timeline we are leaving and the one
 * we are joining. So the lookup half is extracted here, and the
 * highlighter becomes a pure renderer driven by it.
 *
 * The cross-voice invariant
 * -------------------------
 * The backend computes `preprocessor.process_with_alignment(text)` ONCE
 * per request and reuses that alignment for both the foreground voice and
 * the background prefetch of the other voice (see speech_pipeline.py:
 * handle_request). TimingGenerator.generate_from_word_bounds() then emits
 * exactly one entry per ORIGINAL word.
 *
 * Therefore, for the same chapter text, the male and female timing arrays
 * are INDEX-PARALLEL: same length, same words, same emoji flags — only
 * the start/end numbers differ.
 *
 * That is the entire basis for seamless voice switching. Wall-clock time
 * is NOT portable between voices (male 45.0s and female 45.0s are
 * different sentences), but a word ordinal IS. So a switch is:
 *
 *      oldTime --resolve--> (ordinal, fraction) --timeAt--> newTime
 *
 * Position space
 * --------------
 * A PlaybackPosition is deliberately voice-agnostic and serialisable:
 *
 *      {
 *        ordinal:  index into the SPOKEN words (-1 = before the first),
 *        wordIndex: index into the full timings/DOM array (-1 = none),
 *        fraction: 0..1 progress through that word's highlight window,
 *        rawTime:  the original seconds, kept only for the -1 case
 *      }
 *
 * Because it carries no seconds that mean anything to a specific voice,
 * a position captured mid-switch stays valid across further switches —
 * which is what makes rapid voice toggling safe (see speechsession.js).
 */
class Timeline {

    /**
     * @param {Array<{word:string,start:number,end:number,emoji?:boolean}>} timings
     *        The parsed timing JSON, exactly as served by /audio/<key>.json.
     */
    constructor(timings = []) {

        this.timings = Array.isArray(timings) ? timings : [];

        // Spoken-only view: emoji/invisible-only tokens carry timing
        // entries (so index parity with the DOM is preserved) but are
        // never valid highlight or seek targets — same rule the previous
        // highlighter applied via `_spokenTimings`.
        this._spoken = [];

        // domIndex -> ordinal, so a position captured from the DOM side
        // can be converted back into spoken space in O(1).
        this._ordinalByDomIndex = new Map();

        this._duration = 0;

        this._build();
    }

    // --------------------------------------------------
    // Construction
    // --------------------------------------------------

    _build() {

        this.timings.forEach((entry, domIndex) => {

            if (!entry || entry.emoji) {
                return;
            }

            const start = Number(entry.start);

            if (!Number.isFinite(start) || start < 0) {

                console.warn(
                    `[timeline] dropping entry ${domIndex} — unusable start:`,
                    entry && entry.start
                );

                return;
            }

            let end = Number(entry.end);

            // A zero/negative-width or missing window is survivable: the
            // effective window is recomputed below as "up until the next
            // spoken word starts" anyway.
            if (!Number.isFinite(end) || end < start) {
                end = start;
            }

            this._spoken.push({ start, end, domIndex });

            if (end > this._duration) {
                this._duration = end;
            }
        });

        // Defensive: every lookup below assumes non-decreasing starts.
        // Forced alignment emits them in order, but a corrupted or
        // hand-edited bundle must degrade to "slightly wrong word",
        // never to "binary search returns garbage".
        this._spoken.sort((a, b) => a.start - b.start);

        this._spoken.forEach((word, ordinal) => {
            this._ordinalByDomIndex.set(word.domIndex, ordinal);
        });
    }

    // --------------------------------------------------
    // Introspection
    // --------------------------------------------------

    get spokenCount() {
        return this._spoken.length;
    }

    get wordCount() {
        return this.timings.length;
    }

    /** Last known end time, in seconds. Not the audio's duration. */
    get duration() {
        return this._duration;
    }

    get firstStart() {
        return this._spoken.length ? this._spoken[0].start : 0;
    }

    isEmpty() {
        return this._spoken.length === 0;
    }

    // --------------------------------------------------
    // Lookup
    // --------------------------------------------------

    /**
     * Ordinal of the LAST spoken word whose start time has passed, or -1.
     *
     * Matching on "start has passed" rather than "inside [start, end]" is
     * what keeps very short function words ("to", "is", "at") from being
     * skipped: a word owns the timeline until the next word claims it,
     * so any sample landing after its start highlights it.
     *
     * O(log n) — this runs on every animation frame during playback.
     *
     * @param {number} time seconds
     * @returns {number} ordinal into the spoken words, or -1
     */
    ordinalAt(time) {

        const spoken = this._spoken;

        if (!spoken.length || !Number.isFinite(time)) {
            return -1;
        }

        let lo = 0;
        let hi = spoken.length - 1;
        let match = -1;

        while (lo <= hi) {

            const mid = (lo + hi) >> 1;

            if (spoken[mid].start <= time) {
                match = mid;
                lo = mid + 1;
            }
            else {
                hi = mid - 1;
            }
        }

        return match;
    }

    /**
     * Index into the full timings array (and therefore into the
     * highlighter's wordElements), or -1 before the first spoken word.
     *
     * @param {number} time seconds
     * @returns {number}
     */
    domIndexAt(time) {

        const ordinal = this.ordinalAt(time);

        return ordinal === -1
            ? -1
            : this._spoken[ordinal].domIndex;
    }

    /**
     * The half-open window [start, nextStart) that a spoken word owns.
     *
     * Using the NEXT word's start (rather than this word's `end`) makes
     * the timeline gapless: inter-word silence belongs to the word that
     * preceded it, so `fraction` below is continuous and a position
     * captured during a pause still maps somewhere sane.
     *
     * @param {number} ordinal
     * @returns {{start:number,end:number}|null}
     */
    windowAt(ordinal) {

        const word = this._spoken[ordinal];

        if (!word) {
            return null;
        }

        const next = this._spoken[ordinal + 1];

        let end = next ? next.start : word.end;

        if (!(end > word.start)) {
            end = word.start + Timeline.MIN_WINDOW_SECONDS;
        }

        return { start: word.start, end };
    }

    // --------------------------------------------------
    // Position capture / projection
    // --------------------------------------------------

    /**
     * Convert a moment on THIS timeline into a voice-independent
     * PlaybackPosition.
     *
     * @param {number} time seconds
     * @returns {{ordinal:number,wordIndex:number,fraction:number,rawTime:number}}
     */
    resolve(time) {

        const safeTime = Number.isFinite(time) && time > 0 ? time : 0;

        const ordinal = this.ordinalAt(safeTime);

        if (ordinal === -1) {

            // Lead-in silence (or an entirely empty timeline). There is
            // no word to anchor to, so the raw offset is all we have.
            return {
                ordinal: -1,
                wordIndex: -1,
                fraction: 0,
                rawTime: safeTime
            };
        }

        const window = this.windowAt(ordinal);

        const span = window.end - window.start;

        const fraction = span > 0
            ? clamp01((safeTime - window.start) / span)
            : 0;

        return {
            ordinal,
            wordIndex: this._spoken[ordinal].domIndex,
            fraction,
            rawTime: safeTime
        };
    }

    /**
     * Project a PlaybackPosition onto THIS timeline, returning seconds.
     *
     * @param {object} position as produced by resolve()
     * @returns {number} seconds, always >= 0
     */
    timeAt(position) {

        if (this.isEmpty()) {
            return 0;
        }

        if (!position || position.ordinal === -1) {

            // Preserve lead-in silence, but never overshoot past the
            // first word of the voice we are joining.
            const raw = position && Number.isFinite(position.rawTime)
                ? position.rawTime
                : 0;

            return Math.max(0, Math.min(raw, this.firstStart));
        }

        const ordinal = clampInt(position.ordinal, 0, this._spoken.length - 1);

        const window = this.windowAt(ordinal);

        const span = window.end - window.start;

        const target = window.start + clamp01(position.fraction) * span;

        // Land strictly INSIDE the word's window. Seeking to exactly
        // `window.end` would resolve to the NEXT word, so a switch at the
        // tail of a word would visibly jump the highlight forward one.
        const ceiling = Math.max(window.start, window.end - Timeline.EDGE_EPSILON);

        return Math.max(0, Math.min(target, ceiling));
    }

    // --------------------------------------------------
    // Cross-voice remapping
    // --------------------------------------------------

    /**
     * Translate a moment on `from` into the equivalent moment on `to`.
     *
     * This is the whole point of the module. Both bundles describe the
     * same original words, so we travel through word space rather than
     * time space.
     *
     * @param {number}   time seconds on the `from` timeline
     * @param {Timeline} from the timeline being left
     * @param {Timeline} to   the timeline being joined
     * @returns {number} seconds on the `to` timeline
     */
    static remapTime(time, from, to) {

        if (!(from instanceof Timeline) || !(to instanceof Timeline)) {
            throw new TypeError("Timeline.remapTime requires two Timeline instances.");
        }

        return Timeline.projectPosition(from.resolve(time), from, to);
    }

    /**
     * Project an already-captured position onto `to`.
     *
     * Split out from remapTime() because a switch that is superseded
     * mid-flight must reuse the position captured at INTENT time — by
     * then the audio element has moved on and re-reading it would give a
     * meaningless answer. See SpeechSession._capturePosition().
     *
     * @param {object}   position
     * @param {Timeline} from
     * @param {Timeline} to
     * @returns {number} seconds on the `to` timeline
     */
    static projectPosition(position, from, to) {

        if (!position || to.isEmpty()) {
            return 0;
        }

        if (position.ordinal === -1) {
            return to.timeAt(position);
        }

        // Expected path: index-parallel bundles, ordinal maps 1:1.
        if (from.spokenCount === to.spokenCount) {
            return to.timeAt(position);
        }

        // Degraded path. Reached only if the two bundles tokenized
        // differently — e.g. one voice fell back to the ephemeral
        // equal-split timing after a forced-alignment failure, against a
        // text that sanitized differently. Scale the ordinal rather than
        // the clock: proportional word position is still far closer to
        // correct than proportional wall-clock across two voices with
        // different speaking rates.
        console.warn(
            `[timeline] bundle length mismatch (${from.spokenCount} -> ` +
            `${to.spokenCount}); remapping proportionally.`
        );

        if (from.spokenCount === 0) {
            return 0;
        }

        const scaled = Math.round(
            (position.ordinal / from.spokenCount) * to.spokenCount
        );

        return to.timeAt({
            ordinal: clampInt(scaled, 0, to.spokenCount - 1),
            fraction: position.fraction,
            rawTime: position.rawTime
        });
    }

    /** An always-safe placeholder, so callers never branch on null. */
    static empty() {
        return EMPTY_TIMELINE;
    }
}

// Floor applied when a word's own window is degenerate. Mirrors
// config.MIN_WORD_HIGHLIGHT_DURATION on the backend.
Timeline.MIN_WINDOW_SECONDS = 0.08;

// Keeps a projected seek strictly inside its word's window.
Timeline.EDGE_EPSILON = 0.004;

const EMPTY_TIMELINE = new Timeline([]);

// --------------------------------------------------
// Local helpers
// --------------------------------------------------

function clamp01(value) {

    if (!Number.isFinite(value)) {
        return 0;
    }

    return value < 0 ? 0 : (value > 1 ? 1 : value);
}

function clampInt(value, min, max) {

    const n = Math.round(Number(value));

    if (!Number.isFinite(n)) {
        return min;
    }

    return n < min ? min : (n > max ? max : n);
}

if (typeof module !== "undefined" && module.exports) {
    module.exports = { Timeline };
}
