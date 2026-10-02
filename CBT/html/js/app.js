console.log("========== CBT Started ==========");

/**
 * Application orchestration.
 *
 * Wires five decoupled modules and owns nothing but that wiring:
 *
 *      RequestCoordinator  — which request is allowed to win
 *      AudioPlayer         — the single <audio> element
 *      Timeline            — "which word is spoken at time T"
 *      WordHighlighter     — renders .active-word
 *      SpeechSession       — voice / speed / position state machine
 *
 * Concurrency model — "latest request wins"
 * -----------------------------------------
 * Every user action that needs speech starts a multi-stage async pipeline
 * (POST /speak, GET the timing JSON, then load + seek + play the WAV).
 * Every stage is an await, so a second action lands *inside* the first
 * pipeline rather than after it. Without a policy, both run to completion
 * and race to mutate the same audio element and highlighter — and because
 * a cache HIT can return in milliseconds while a cache MISS synthesizes
 * for seconds, they can finish in reverse order.
 *
 * The policy lives in SpeechSession and RequestCoordinator; this file
 * only guarantees that the click handlers stay SYNCHRONOUS up to the
 * point the coordinator is engaged, so no two handlers can interleave
 * into a half-torn-down state.
 *
 * Voice and speed
 * ---------------
 * The two controls now behave completely differently, and that asymmetry
 * is intentional (see js/speechsession.js for the reasoning):
 *
 *   Speed  -> audio.playbackRate. Instant, no network, no reload. Word
 *             highlighting re-paces itself because it is bound to
 *             audio.currentTime, which playbackRate does not distort.
 *
 *   Voice  -> a real source swap that preserves the reading position by
 *             projecting it through word space rather than wall-clock
 *             time. Male 45.0s and female 45.0s are different sentences.
 */

// ----------------------------------------------------
// UI Controls
// ----------------------------------------------------

const playBtn = document.getElementById("playBtn");
const pauseBtn = document.getElementById("pauseBtn");
const stopBtn = document.getElementById("stopBtn");

const voiceSelect = document.getElementById("voiceSelect");
const speedSelect = document.getElementById("speedSelect");

const statusNote = document.getElementById("statusNote");

const PLAY_LABEL = "▶ Play";
const GENERATING_LABEL = "⏳ Generating…";
const PAUSE_LABEL = "⏸ Pause";
const RESUME_LABEL = "▶ Resume";

// ----------------------------------------------------
// Modules
// ----------------------------------------------------

const highlighter = new WordHighlighter("content");
window.highlighter = highlighter;

// ----------------------------------------------------
// Audio → highlight sync calibration
//
// The word highlight is driven by <audio>.currentTime, which runs ahead
// of the sound at the speaker by the output buffer's depth (see
// WordHighlighter's docstring). This offset — wall-clock milliseconds —
// pulls the highlight lookup back so words light up on the acoustic
// onset. Persisted per machine, and adjustable live via
//     window.CBT.setSyncOffset(120)   // e.g. for a Bluetooth headset
// ----------------------------------------------------

const SYNC_OFFSET_STORAGE_KEY = "cbt.audioSyncOffsetMs";

(function initSyncCalibration() {

    try {
        const stored = window.localStorage.getItem(SYNC_OFFSET_STORAGE_KEY);

        if (stored !== null && Number.isFinite(Number(stored))) {
            highlighter.setAudioLatency(Number(stored) / 1000);
        }
    }
    catch (err) {
        // localStorage unavailable (private mode, disabled) — the
        // compiled-in default still applies.
    }

    window.CBT = window.CBT || {};

    window.CBT.getSyncOffset = () => Math.round(highlighter.getAudioLatency() * 1000);

    window.CBT.setSyncOffset = (milliseconds) => {

        const appliedMs = Math.round(
            highlighter.setAudioLatency(Number(milliseconds) / 1000) * 1000
        );

        try {
            window.localStorage.setItem(SYNC_OFFSET_STORAGE_KEY, String(appliedMs));
        }
        catch (err) {
            /* not persisted, but live for this session */
        }

        console.log(`[sync] audio→highlight offset = ${appliedMs} ms`);

        return appliedMs;
    };

    console.log(
        `[sync] audio→highlight offset = ${window.CBT.getSyncOffset()} ms ` +
        `(adjust with window.CBT.setSyncOffset(<ms>))`
    );
})();

const progressBar = new ProgressBar({

    sliderId: "seekBar",
    currentTimeId: "currentTime",
    durationId: "duration",

    // Inbound sync: the user dragged / clicked / arrow-keyed the bar.
    onSeek: (time) => {

        if (!player.hasSource()) {
            return;
        }

        const target = player.seek(time);

        // Jump the highlight to the same instant, so text and audio never
        // disagree after a scrub — even while paused.
        highlighter.syncTo(target);
    }
});

const playback = new RequestCoordinator({ name: "playback" });
const prefetch = new RequestCoordinator({ name: "prefetch" });

const session = new SpeechSession({

    player,
    highlighter,
    coordinator: playback,
    prefetcher: prefetch,

    callbacks: {

        // Debounced by the session, so this only fires for switches slow
        // enough that silence would otherwise be unexplained.
        onBusy: (isBusy, message) => {
            setStatusNote(isBusy ? message : "");
        },

        // The session is authoritative for which voice is actually
        // playing. On a failed switch it reverts, and the dropdown must
        // follow — a control that lies about the current voice is worse
        // than one that snaps back.
        onVoice: (voice) => {

            if (voiceSelect.value !== voice) {
                voiceSelect.value = voice;
            }
        },

        onError: (err) => {

            setUiState(UI_STATE.IDLE);

            alert(err.message);
        }
    }
});

window.session = session;

// ----------------------------------------------------
// UI state machine
//
// One function owns every control's enabled/label state, so no code path
// can leave the footer in an impossible combination (e.g. "Generating…"
// forever after an aborted request).
//
//   idle       — nothing loaded
//   generating — request in flight; Play stays ENABLED on purpose, so a
//                click supersedes rather than being swallowed
//   playing    — audio running
//   paused     — audio loaded, stopped mid-way
// ----------------------------------------------------

const UI_STATE = {
    IDLE: "idle",
    GENERATING: "generating",
    PLAYING: "playing",
    PAUSED: "paused"
};

let uiState = UI_STATE.IDLE;

function setUiState(state) {

    uiState = state;

    switch (state) {

        case UI_STATE.GENERATING:
            playBtn.disabled = false;
            playBtn.textContent = GENERATING_LABEL;
            pauseBtn.disabled = true;
            pauseBtn.textContent = PAUSE_LABEL;
            stopBtn.disabled = false;
            break;

        case UI_STATE.PLAYING:
            playBtn.disabled = false;
            playBtn.textContent = PLAY_LABEL;
            pauseBtn.disabled = false;
            pauseBtn.textContent = PAUSE_LABEL;
            stopBtn.disabled = false;
            break;

        case UI_STATE.PAUSED:
            playBtn.disabled = false;
            playBtn.textContent = PLAY_LABEL;
            pauseBtn.disabled = false;
            pauseBtn.textContent = RESUME_LABEL;
            stopBtn.disabled = false;
            break;

        case UI_STATE.IDLE:
        default:
            playBtn.disabled = false;
            playBtn.textContent = PLAY_LABEL;
            pauseBtn.disabled = true;
            pauseBtn.textContent = PAUSE_LABEL;
            stopBtn.disabled = true;
            break;
    }
}

/**
 * Inline, non-blocking status line. Used for the "still loading the other
 * voice" case in the spec's race-condition requirement — a switch that
 * cannot complete instantly explains itself rather than just going quiet.
 */
function setStatusNote(message) {

    if (!statusNote) {
        return;
    }

    statusNote.textContent = message || "";

    statusNote.hidden = !message;
}

// ----------------------------------------------------
// Reading the chapter out of the DOM
// ----------------------------------------------------

// Marks a natural pause point for Piper -- see
// TextPreprocessor.insert_heading_pauses() in preprocess.py, which turns
// this into real sentence-ending punctuation server-side (there is no
// SSML/break-tag support anywhere in this pipeline). "\u001F" (unit separator) is a C0
// "separator" control character that is whitespace per both Python's
// str.isspace()/regex \s (see preprocess.py's comment) and this app's
// own ORIGINAL_WORD_RE (\S+) -- inserting it can never create a new
// "word" that doesn't exist in the DOM, so original_words/
// processed_words counts on the Python side stay exactly what they'd be
// without it. It is never written into the DOM itself -- only into the
// string built here, right before it's sent to /speak.
const HEADING_PAUSE_MARKER = "\u001F";

function currentChapterText() {

    const content = document.getElementById("content");

    if (!content) {
        return "";
    }

    // .innerText (not a hand-rolled DOM walk) so every exclusion this
    // app already depends on -- CSS ::before-generated icon/label
    // content, hidden elements -- keeps working exactly as before;
    // headings are marked by finding each one's OWN .innerText inside
    // the already-flattened string, not by rebuilding the string a
    // different way.
    let text = content.innerText;

    // Search forward from the previous match so two headings with the
    // same title ("Introduction" appearing twice) each get their own
    // marker in the right place, instead of both collapsing onto the
    // first occurrence.
    let searchFrom = 0;

    content.querySelectorAll("h1, h2, h3, h4").forEach(heading => {

        const headingText = heading.innerText;

        if (!headingText) {
            return;
        }

        const index = text.indexOf(headingText, searchFrom);

        if (index === -1) {
            return;
        }

        const insertAt = index + headingText.length;

        text = text.slice(0, insertAt) + HEADING_PAUSE_MARKER + text.slice(insertAt);

        searchFrom = insertAt + HEADING_PAUSE_MARKER.length;

    });

    return text.trim();
}

// Exposed so index.html's renderContent() can kick off
// session.preloadText() with the just-rendered section's text as soon as
// it's in the DOM — well before the user clicks Play. See
// SpeechSession.preloadText() in speechsession.js for why that's what
// actually gets a cached chapter playing in well under a second.
window.currentChapterText = currentChapterText;

// ----------------------------------------------------
// Reset Reader (chapter change / hard stop)
// ----------------------------------------------------

function resetReader() {

    console.log("Resetting Reader...");

    // Drops every cached bundle too: timings are aligned against specific
    // words, so the old chapter's would index spans that no longer exist.
    session.reset();

    progressBar.reset();

    setStatusNote("");

    setUiState(UI_STATE.IDLE);
}

// Make available to index.html
window.resetReader = resetReader;

// ----------------------------------------------------
// Audio Events
// ----------------------------------------------------

player.onPlay = () => {

    console.log("Playback Started");

    setUiState(UI_STATE.PLAYING);
};

player.onPause = () => {

    console.log("Playback Paused");

    // A voice switch pauses the element on its way through. That is
    // bookkeeping, not a user-visible pause, and must not flip the
    // button to "Resume" for a few frames.
    if (session.isSwitching) {
        return;
    }

    // Ignore pauses that happen while a new request is spinning up — the
    // GENERATING label must not be clobbered back to PAUSED.
    if (uiState === UI_STATE.PLAYING) {
        setUiState(UI_STATE.PAUSED);
    }
};

player.onLoadedMetadata = (duration) => {

    progressBar.setDuration(duration);
};

player.onEnded = () => {

    console.log("Playback Finished");

    highlighter.reset();

    // Leave the bar visibly complete (100%) rather than snapping to 0 —
    // it resets on the next Play / Stop / chapter change.
    progressBar.complete();

    setUiState(UI_STATE.PAUSED);
};

player.onError = (err) => {

    console.error("Playback error", err);

    setStatusNote("");

    setUiState(UI_STATE.IDLE);
};

// Fires ~60fps from the player's requestAnimationFrame loop — drives BOTH
// the word highlight and the smooth progress-bar fill. Also fired once
// after any programmatic seek, so a switch made while paused still
// repaints immediately.
player.onTimeUpdate = (currentTime) => {

    highlighter.update(currentTime);

    progressBar.update(currentTime);
};

// ----------------------------------------------------
// PLAY
//
// The server generates (or serves from cache) whichever voice is
// currently selected, and — behind the scenes — also generates the OTHER
// voice, so switching afterwards is a cache hit. The session then pulls
// that other bundle to the browser too, making the switch cost no round
// trip at all. See speech_pipeline.py for the backend half.
// ----------------------------------------------------

playBtn.addEventListener("click", () => {

    const text = currentChapterText();

    if (!text) {

        alert("No content found.");

        return;
    }

    // Synchronous: engages the coordinator and unloads the element before
    // the first await, so nothing can interleave into a half state.
    progressBar.reset();

    setUiState(UI_STATE.GENERATING);

    // Fire-and-forget: the session owns all of its own error handling.
    session
        .play({ text, voice: voiceSelect.value })
        .catch((err) => {

            console.error(err);

            setUiState(UI_STATE.IDLE);

            alert(err.message);
        });
});

// ----------------------------------------------------
// VOICE — mid-stream switch, position preserved
// ----------------------------------------------------

voiceSelect.addEventListener("change", () => {

    session.switchVoice(voiceSelect.value);
});

// ----------------------------------------------------
// SPEED — instant, no reload, no re-synthesis
//
// The highlight needs no involvement here at all: it reads
// audio.currentTime, which is on the media timeline and therefore
// unaffected by playbackRate, so words automatically re-pace with the
// audio.
// ----------------------------------------------------

speedSelect.addEventListener("change", () => {

    const applied = session.setRate(parseFloat(speedSelect.value));

    // The output-buffer lead, measured in MEDIA time, scales with the
    // rate — so the highlighter's latency compensation has to track it.
    highlighter.setPlaybackRate(applied);

    // If the rate was clamped, show what actually took effect rather
    // than letting the control claim something untrue.
    if (parseFloat(speedSelect.value) !== applied) {
        speedSelect.value = String(applied);
    }
});

// ----------------------------------------------------
// PAUSE / RESUME
// ----------------------------------------------------

pauseBtn.addEventListener("click", () => {

    session.togglePause();

    // The button label follows the player's own "play"/"pause" events
    // (via setUiState) rather than being toggled optimistically here, so
    // it can never disagree with what the audio is actually doing.
});

// ----------------------------------------------------
// STOP
// ----------------------------------------------------

stopBtn.addEventListener("click", () => {

    // Stop must also kill a generation that hasn't landed yet — otherwise
    // it would autoplay seconds later, after the user asked for silence.
    session.stop();

    progressBar.reset();

    setStatusNote("");

    setUiState(UI_STATE.IDLE);
});

// ----------------------------------------------------
// Initial state
// ----------------------------------------------------

session.selectVoice(voiceSelect.value);
highlighter.setPlaybackRate(session.setRate(parseFloat(speedSelect.value)));

setUiState(UI_STATE.IDLE);

console.log("========== App Ready ==========");
