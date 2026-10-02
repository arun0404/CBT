/**
 * Interactive audio seek / scrub bar.
 *
 * Owns nothing about the audio element itself — it renders playback
 * position and emits *seek intents* through the `onSeek` callback,
 * staying fully decoupled from AudioPlayer (the same way WordHighlighter
 * is decoupled from the audio element). app.js wires the two together.
 *
 * Bidirectional sync
 * ------------------
 *   Outbound (playback -> bar):
 *       update(currentTime) is called on every animation frame by the
 *       player's requestAnimationFrame loop, so the fill advances at
 *       ~60fps rather than the ~4fps of the native "timeupdate" event.
 *
 *   Inbound  (bar -> playback):
 *       Dragging or clicking the slider emits onSeek(targetSeconds).
 *       app.js turns that into player.seek() + highlighter.update() so
 *       audio and text jump to the same instant, together.
 *
 * The `isScrubbing` flag is the crux of keeping those two directions
 * from fighting: while the user owns the thumb, the RAF-driven update()
 * must NOT drag it back to the audio's (soon-to-be-stale) position.
 */
class ProgressBar {

    constructor({ sliderId, currentTimeId, durationId, onSeek } = {}) {

        this.slider = document.getElementById(sliderId);
        this.currentTimeLabel = document.getElementById(currentTimeId);
        this.durationLabel = document.getElementById(durationId);

        this.onSeek = typeof onSeek === "function" ? onSeek : () => {};

        this.duration = 0;
        this.isScrubbing = false;

        this._bindEvents();
        this.reset();
    }

    // --------------------------------------------------
    // Wiring
    // --------------------------------------------------

    _bindEvents() {

        // "input" fires continuously for a click, a drag, AND keyboard
        // arrow-key nudges — the single source of "the user is moving the
        // thumb". We seek live here so the response is instant.
        this.slider.addEventListener("input", () => {

            this.isScrubbing = true;

            const time = Number(this.slider.value);

            this._render(time);

            this.onSeek(time);
        });

        // "change" fires once the interaction settles (mouse release /
        // committed keyboard value). Final commit, then hand the thumb
        // back to the playback loop.
        this.slider.addEventListener("change", () => {

            const time = Number(this.slider.value);

            this.onSeek(time);

            this.isScrubbing = false;
        });

        // Bracket the drag with pointer events too, so the RAF loop can't
        // repaint the thumb in the sliver of time between the user
        // grabbing it and the first "input" event firing.
        this.slider.addEventListener("pointerdown", () => {
            this.isScrubbing = true;
        });

        const endScrub = () => {
            this.isScrubbing = false;
        };

        this.slider.addEventListener("pointerup", endScrub);
        this.slider.addEventListener("pointercancel", endScrub);
    }

    // --------------------------------------------------
    // Called once the audio's duration is known
    // --------------------------------------------------

    setDuration(duration) {

        if (!Number.isFinite(duration) || duration <= 0) {
            return;
        }

        this.duration = duration;

        this.slider.max = duration;
        this.slider.disabled = false;

        this.durationLabel.textContent = this._format(duration);

        // Re-render so the fill reflects the (now meaningful) max.
        this._render(Number(this.slider.value) || 0);
    }

    // --------------------------------------------------
    // Outbound sync: called every animation frame while playing
    // --------------------------------------------------

    update(currentTime) {

        // The user owns the thumb right now — leave it alone.
        if (this.isScrubbing) {
            return;
        }

        this._render(currentTime);
    }

    // Snap the bar to fully-played (used when playback ends).
    complete() {
        if (this.duration > 0) {
            this._render(this.duration);
        }
    }

    // --------------------------------------------------
    // Rendering
    // --------------------------------------------------

    _render(time) {

        const clamped = this.duration > 0
            ? Math.max(0, Math.min(time, this.duration))
            : Math.max(0, time);

        // Don't overwrite the native thumb position mid-scrub; the browser
        // already placed it from the pointer / keyboard.
        if (!this.isScrubbing) {
            this.slider.value = clamped;
        }

        const pct = this.duration > 0
            ? (clamped / this.duration) * 100
            : 0;

        // Drives the WebKit track fill (see CSS: var(--progress)). Firefox
        // fills natively via ::-moz-range-progress from value/max, so both
        // engines stay in sync off the same underlying value.
        this.slider.style.setProperty("--progress", pct + "%");

        this.currentTimeLabel.textContent = this._format(clamped);
    }

    // --------------------------------------------------
    // Reset (new chapter / stop / before a fresh generation)
    // --------------------------------------------------

    reset() {

        this.isScrubbing = false;
        this.duration = 0;

        this.slider.value = 0;
        this.slider.max = 0;
        this.slider.disabled = true;
        this.slider.style.setProperty("--progress", "0%");

        this.currentTimeLabel.textContent = "00:00";
        this.durationLabel.textContent = "00:00";
    }

    // --------------------------------------------------
    // Helpers
    // --------------------------------------------------

    _format(seconds) {

        if (!Number.isFinite(seconds) || seconds < 0) {
            seconds = 0;
        }

        const total = Math.floor(seconds);
        const mins = Math.floor(total / 60);
        const secs = total % 60;

        const pad = (n) => String(n).padStart(2, "0");

        return `${pad(mins)}:${pad(secs)}`;
    }
}
