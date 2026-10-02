# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

CBT is a fully offline (air-gapped) desktop text-to-speech reader: Piper synthesizes audio for a
chapter of text, torchaudio's MMS_FA model force-aligns the audio to get real per-word timings, and a
Flask-served single-page frontend plays the audio while highlighting the currently spoken word. There is
no build step for a normal change — this is a plain Python/Flask backend plus vanilla-JS frontend served
as static files, wrapped in a Windows-first desktop launcher.

Not a git repository in this checkout — there is no history to consult with `git log`/`git blame`.

**This checkout is incomplete** — several things `CLAUDE.md` and the code reference are genuinely absent, so
verify before assuming a command will run:

- `native_host/piper/` (so **no `piper.exe`** and no voice models) — nothing can actually synthesize audio
  here, and any change to how text is spoken can only be reasoned about, not heard. Say so rather than
  claiming an audio fix is verified.
- `html/media/` — every `data2.json` image `src` points into it. Harmless: `renderer.js` is constructed with
  `renderImages:false`, so image/hotspot data is inert, unrendered metadata.
- The `native_host/test_*.py` scripts described under Tests below.

A working runtime venv usually *does* exist at `%LOCALAPPDATA%\CBT_venv\venv\` and is the interpreter to use
for anything that imports the app (the bundled `Python310\python.exe` has no third-party packages, so it can
only `py_compile`). Importing `speech_pipeline`/`preprocess` from it works fine, which makes the text
pipeline and cancellation logic genuinely testable in isolation even without Piper.

The manual content in `data2.json` is an **automobile** technical manual (Introduction / Anti-Lock Braking
System / Steering & Suspension / Engine System). Some strings in it are deliberate *test fixtures* for the
text pipeline rather than real prose — the `MKMTM-79-24-00-920A` reference code, `RPM`, `7A`/`14 A`, the
`📄` emoji, `i.`/`ii)` list markers — so don't "clean them up" without checking which pass they exercise.

## Repository layout

- `CBT/native_host/` — the Flask app and all Python logic (see Architecture below).
- `CBT/html/` — the frontend served by Flask (`static_folder`), plain HTML/CSS/JS, no bundler.
- `CBT/html/assets/` — locally-authored video files (and, once generated, their `.vtt` sidecars) embedded
  into `data2.json` sections via the custom video player, plus the two background images
  (`login-bg.png`, `home-bg.png`). Served by `host.py`'s catch-all `/<path:filename>` static route, so
  dropping a file here needs no backend change. Both backgrounds sit UNDER a `linear-gradient` scrim in the
  same `background-image` declaration (CSS paints the first listed layer on top) — the login one dark, the
  landing one pale, because the login card is self-contained white-on-dark while the landing view's own
  content is dark-on-light and a dark scrim would fight it.
- `CBT/native_host/launcher/` — the desktop launcher package (venv bootstrap, process supervision,
  readiness polling, browser launch). Also has its own `README-LAUNCHER.md` (same content as the root one).
- `CBT/native_host/packages/` — offline wheelhouse (pip installs with `--no-index` from here).
- `CBT/native_host/Python310/` — bundled Python 3.10 interpreter used to *create* the runtime venv.
- `CBT/native_host/services/caption_generator.py` — an **offline, dev-only** CLI tool (`faster-whisper`)
  that transcribes a local video/audio file into a `.vtt` file for the custom video player's CC button.
  Deliberately **not** part of `requirements.txt`/the shipped runtime venv — its own
  `services/requirements-captions.txt` is a separate, manually-installed environment. Run it, don't wire it
  into `host.py`, unless that trade-off (bundling `ctranslate2` + a Whisper model into every install) is an
  explicit decision, not an accidental one.
- `run.bat` / `launch.vbs` / `CBT.exe` — thin entry points that all forward args into
  `native_host/bootstrap.py` → `launcher.__main__`.
- `create-shortcut.bat` — double-click wrapper around `tools/create_shortcut.ps1`
  (`create-shortcut.bat [vbs|exe|bat] [startmenu]`, default target `launch.vbs`).
- `tools/` — `build_exe.bat` (PyInstaller build of the launcher only, not the app), `CBT.spec`,
  `create_shortcut.ps1` (desktop shortcut creation).
- `README-LAUNCHER.md` — the canonical doc for the launcher/packaging behavior, exit codes, and runtime
  state layout. Read it before touching anything under `launcher/`, `bootstrap.py`, `run.bat`, or the
  `.spec`/`.vbs`/`.ps1` tooling.

## Running the app

```bat
run.bat                     REM normal launch (builds venv on first run, then starts + opens browser)
run.bat --app-window        REM chromeless Edge/Chrome window
run.bat --no-browser        REM start the server only
run.bat --detach            REM launch, open browser, exit; server keeps running
run.bat --show-console      REM visible console on the host process (debugging)
run.bat --status            REM machine-readable JSON status
run.bat --stop              REM stop a running instance
run.bat --rebuild-env       REM delete and rebuild the virtual environment
run.bat --setup-only        REM build the environment without starting the app
run.bat --port 5050         REM use a different port
run.bat --verbose           REM debug-level logging
```

All of these also work via `bootstrap.py` directly (`Python310\python.exe bootstrap.py --status`) or via
`CBT.exe`, since `run.bat` is only a shim.

For active backend development, `python host.py` inside `CBT/native_host` runs the Flask app
directly against `127.0.0.1:5000` with `debug=True` (reads `CBT_DEBUG`/`CBT_HOST`/
`CBT_PORT` env vars, defaulting to dev-friendly values) — this skips the launcher entirely and
needs an interpreter that already has `requirements.txt` installed (i.e. the runtime venv, not the bare
bundled `Python310`).

Env var overrides useful for side-by-side installs / test runs: `CBT_HOME` (native_host dir),
`CBT_VENV` (venv root), `CBT_HOST`, `CBT_PORT`.

Runtime state (venv, pid file, logs) lives per-user under `%LOCALAPPDATA%\CBT_venv\` — never inside
the application directory (the app tree must stay writable-free/portable). `launcher.log`, `server.log`,
and `pip-install.log` under `...\state\logs\` are the first place to check when a launch fails; exit codes
2–7 map to specific failure stages (see `README-LAUNCHER.md` §4 for the full table).

## Tests

**Not present in this checkout** (see above) — the commands below describe how they work if restored.

Tests under `CBT/native_host/test_*.py` (`test_cache.py`, `test_alignment.py`,
`test_audio_processor.py`, `test_forced_aligner_mock.py`, `test_gap_filling.py`,
`test_minimum_duration.py`) are **plain standalone scripts**, not pytest suites — they run top-level
asserts and `print("OK")`/raise on failure. Run one directly with an interpreter that has the full
`requirements.txt` installed (torch/torchaudio included), from inside `native_host/` so the bare-module
imports (`from cache import ...`, `from aligner...`) resolve:

```bat
cd CBT\native_host
%LOCALAPPDATA%\CBT_venv\venv\Scripts\python.exe test_cache.py
```

`test_forced_aligner_mock.py` fakes out the MMS_FA bundle so it doesn't need the real checkpoint; the
other alignment tests may need `piper\model\model.pt` and a working torch install. There is no test
runner/CI config in the repo — run each file individually.

For ad-hoc verification of a text-pipeline or cancellation change (which is most changes, and works in this
checkout), write a throwaway script to the scratch directory and run it with the venv interpreter above —
`preprocessor.process_with_alignment(text)` returns the `processed_text` actually sent to Piper plus the
alignment groups. The invariant worth asserting is **not** `len(groups) == len(original_words)` (a group
legitimately spans several original words when a substitution collapses them) but that every original index
is covered exactly once: `sorted(i for g in r.groups for i in g.original_indices) == list(range(n))`.
`SpeechPipeline._response_for` and everything in `cancellation.py` are testable without Piper too — a
`CancellationToken` will genuinely kill any `Popen` you register on it.

Before bumping `torch`/`torchaudio` in `requirements.txt`, re-run `test_forced_aligner_mock.py` and the
alignment tests: `aligner/forced_aligner.py` depends on `torchaudio.pipelines.MMS_FA` /
`torchaudio.functional.forced_align`, which are deprecated as of torchaudio 2.8 and confirmed working only
through 2.11 (pinned in `requirements.txt`).

## Architecture

### Backend request flow (`native_host/`)

`host.py` is a thin Flask adapter. It builds one `PiperTTS` (`piper.py`) and one `SpeechPipeline`
(`speech_pipeline.py`) at import time, with the forced aligner initially `None` — the real MMS_FA aligner
(`aligner/forced_aligner.py`, backed by `aligner/model.py` and `aligner/audio.py`) loads on a background
daemon thread and is swapped into the pipeline via `pipeline.set_forced_aligner()` once ready, so the
Flask socket binds (and `/healthz` answers) immediately instead of blocking on model load. `/healthz`
reports both `forced_alignment` (available at all) and `forced_alignment_ready` (background load done) —
this is what the launcher's readiness poll and any manual startup check should key off.

`POST /speak` → `SpeechPipeline.handle_request()`:
1. `text_processing/preprocess.py` expands abbreviations/units/symbols/parentheticals once (voice- and
   speed-independent), producing an `alignment` object reused for both voices below.
2. The *requested* voice is generated (or served from cache) **in the foreground**, blocking, and returned
   to the caller.
3. The **other** voice is kicked off on a background thread (`_prefetch_secondary`) so a later voice
   switch for the same text/speed is an instant cache hit. This dual-voice prefetch is the reason
   `PiperTTS.synthesize()` resolves the model path as a local variable rather than mutable instance state
   — two voices really do synthesize concurrently.
4. Generation (`_generate`): Piper synthesizes to a scratch WAV, then if the forced aligner is available,
   `aligner.forced_aligner.align()` produces real per-word timings (`timing.py`
   `generate_from_forced_alignment`); on any alignment failure it falls back to naive equal-duration
   timing (`generate_from_alignment`).
5. **Cache policy** (`cache.py`): only forced-aligned (trustworthy) results are written to the real,
   content-addressed cache (`output/alignment_cache/`, two-tier: in-memory LRU + disk, atomic
   temp-file-then-rename writes). Equal-duration fallback results are deliberately **never** cached there
   — they're served from a small ephemeral, LRU-trimmed location (`output/ephemeral/`,
   `EPHEMERAL_MAX_ENTRIES`) instead, so a later identical request gets a fresh shot at real alignment
   rather than being stuck with the degraded timing forever. Don't "fix" this by caching fallback results.
6. Cache key = sha256 of `{text, voice, speed, dictionary_fingerprint, voice_fingerprint,
   algorithm_version}` (`config.py`). `dictionary_fingerprint` busts on any text-processing dictionary
   edit; `VOICE_FINGERPRINT` busts on `piper.exe`/model file changes (name+size+mtime, not content hash);
   `ALIGNMENT_CACHE_VERSION` must be bumped by hand whenever the alignment/timing algorithm **or the spoken
   text any pass produces** changes in a way that should invalidate old entries — see the running version
   history comment in `config.py`, which records the reason for every bump. Currently `v21`. No fingerprint
   covers a code change inside a text-processing *function* (`dictionary_fingerprint` only hashes the
   dictionaries), so editing e.g. how a reference code is spoken **requires** a manual bump or every already
   cached chapter keeps serving the old audio forever.
7. In-flight de-duplication (`SpeechPipeline._claim`/`_release`/`_wait`) prevents two threads (e.g. a
   foreground request and a background prefetch for the same voice) from redundantly regenerating the
   identical `(text, voice, speed)` at the same time.

`POST /speak` returns the timing array **embedded** in its JSON (`timings`) as well as the `timing` URL.
The client uses the embedded copy and skips the follow-up GET (`api.js`'s `fetchSpeechBundle` falls back to
`getTimings()` only if the field is absent), so a normal play costs one round trip, not two. Every caller
of `_response_for()` already holds the list in memory, so this is free — don't "simplify" it back to a
URL-only response.

### Request cancellation (`cancellation.py`)

Aborting a `fetch` client-side stops the client reading a response but does **not** stop the server: the
Flask handler runs synthesis and forced alignment to completion, and each `/speak` also spawns a background
thread generating the other voice. Navigating quickly across three uncached pages otherwise leaves six
jobs competing for CPU, which is what makes the page the user actually landed on slow.

A client disconnect is not reliably observable under WSGI (generally not until the handler writes its
response — long after the expensive work), so cancellation is driven by an **explicit client-supplied
epoch**: every `/speak` carries `nav_epoch` (a monotonic counter bumped by `bumpNavEpoch()` in `api.js`
from `renderContent()`/`showLanding()`), and `CancellationRegistry.begin(N)` cancels every live token from
an earlier epoch. This is the across-the-wire counterpart of `RequestCoordinator`'s monotonic token.

Cancellation is cooperative (`token.raise_if_cancelled()` at stage boundaries in `speech_pipeline`) with one
forceful exception: `piper.py` uses `Popen` + `communicate()` rather than `subprocess.run()` **specifically**
so a running Piper process can be registered on the token and genuinely killed — that's the difference
between freeing the CPU now and whenever Piper happens to finish. Two ordering constraints are load-bearing:
the cancellation check must come **before** the returncode test (a killed process exits non-zero, and
reporting that as "Piper failed" turns a deliberate cancel into a spurious 500), and the forced-alignment
`try` must re-raise `GenerationCancelled` **ahead of** its broad `except Exception`, or an abandoned
generation is swallowed as "alignment failed" and goes on to write an ephemeral fallback entry to disk.
A foreground request and its background prefetch share one token, so navigating away cancels both halves.
`/speak` answers a cancellation with **409 + `cancelled:true`**, which `api.js` re-shapes into an
`AbortError` so every existing `RequestCoordinator.isAbortError()` guard swallows it — otherwise it would
reach `SpeechSession._handleFailure` and raise an `alert()`.

Route registration order in `host.py` matters: the catch-all `/<path:filename>` static-file handler must
stay below `/`, `/healthz`, `/css/*`, `/js/*`, `/speak`, and `/audio/*`, or those routes get swallowed.

`SENTENCE_SILENCE_SECONDS` (`config.py`, passed as Piper's `--sentence_silence`) is what makes a period
render as an audible gap. Not passing the flag leaves Piper's own short default, which is why an inserted
period produced no perceptible pause. Because it applies to *every* sentence-ending mark, punctuation added
purely for pacing is expensive: a period per digit inside a reference code would insert ~0.5s of silence
between every digit. That constraint is why the reference-code and heading-pause passes below use word
choice and spacing rather than punctuation.

Encoding: both `host.py` (stdout/stderr) and `piper.py` (subprocess stdin/stdout/stderr) explicitly force
UTF-8 with `errors="replace"`, because Windows' default legacy code page crashes on emoji/accented/
invisible characters otherwise. Preserve this pattern in any new subprocess or stream-writing code.

### Text preprocessing (`text_processing/`)

`preprocess.py`'s `TextPreprocessor.process_with_alignment()` runs substitution passes in a fixed order,
each through an `AlignmentTracker` (`alignment.py`) that tracks the per-character original-word origin
through every rewrite so that timing can be anchored to original words no matter how many processed
words an original word expands into.

**Pass order is not arbitrary** — several passes only work in their current slot:

- `insert_heading_pauses` — turns the `` markers the FRONTEND inserts (see below) into real
  sentence-ending punctuation. Runs first so its "does this heading already end in punctuation?" check sees
  the text as authored.
- `expand_roman_numeral_list_markers` — `i.`/`ii)` → `One.`/`Two.` (the manual indents lists this way;
  Piper otherwise reads "i dot"). Deliberately **lowercase-only and line-start-anchored**: a capitalised
  `I.` is overwhelmingly the pronoun, and the trailing `(?=\s|$)` is what excludes `i.e.`.
- `normalize_standalone_dashes` — a dash with whitespace on **both** sides becomes `", "`, because Piper
  reads a bare `-` aloud as "dash". `symbols.py` deliberately leaves `-` unmapped (its `" minus "` entry is
  commented out) since a blanket rule would wreck `Anti-Lock`, `seventy-nine`, `5mm-10mm`; the required
  bounding whitespace is what makes this safe.
- `expand_abs_acronym` — `ABS` → `A B S`. Runs **last**, after both `restore_protected_*` calls: the
  manual writes "Anti-Lock Braking System (ABS)", which classifies as an INITIALISM parenthetical and is
  therefore a `\x02PH..\x02` placeholder for most of the pipeline — there is no literal `ABS` to match until
  it's restored. Running it earlier silently catches bare `ABS Module` but misses every bracketed one.
  Case-sensitive (no `IGNORECASE`), which is also why it can't be a dictionary entry — `replace_dictionary`
  is unconditionally case-insensitive.

**Reference/task codes** (`REFERENCE_CODE_RE`, e.g. `79-24-00-870-7A`) are read strictly digit-by-digit via
an explicit `_DIGIT_WORDS` table (pinned rather than `num2words`, which is locale-aware), joined by single
spaces with **no internal punctuation** — see `SENTENCE_SILENCE_SECONDS` above for why commas between
segments were removed. A run of the same digit collapses to a counted phrase (`00` → "double zero", `000` →
"triple zero", `_REPEAT_PREFIXES`/`_chunk_repeat_run`): attention-based neural TTS under-articulates a token
repeated immediately after itself, and the counted form removes the repetition without needing punctuation.
`_chunk_repeat_run` never leaves a bare trailing single (`0000` → "double zero double zero", not "triple
zero zero") precisely to avoid recreating that adjacency. Trailing letters are UPPERCASED (`7A` → "seven A")
because a standalone lowercase `a` is the article and espeak voices it as a schwa, not the letter name —
matching what `expand_initialisms` relies on.

Two mechanisms protect certain spans from being re-expanded by later passes:

- **Parenthetical placeholders** (`_PLACEHOLDER_RE`, sentinel `\x02PHn\x02`): acronyms and codes inside
  brackets (e.g. `(RPM)`) are decided once and replaced with an inert placeholder. Without this, the
  ENGINEERING_TERMS pass would expand the placeholder's text a second time ("Revolutions Per Minute
  Revolutions Per Minute").
- **Reference code placeholders** (`_REFERENCE_CODE_PLACEHOLDER_RE`, sentinel `\x02RCn\x02`): hyphenated
  task codes like `79-24-00-870-7A` are rewritten to spoken form and shielded from the unit-expansion
  pass, which would otherwise misread a trailing digit+letter as a unit ("7A" → "7 amperes").

Both placeholders use `\x02` ("start of text"), not `\x1E` ("record separator"): `\x1E` is treated as
whitespace by Python's `str.isspace()` and therefore by `\s` in regex, so it can be matched through and
deleted by a unit-suffix pattern like `\d+\s*unit`. `\x02` is not whitespace and cannot be matched that
way. The two placeholder namespaces are kept separate and restored in order (parentheticals first, then
reference codes) — they must never share a counter.

**`apply()` vs `apply_segments()`** in `AlignmentTracker`:
- `apply()` tags the entire replacement with the *union* of the origins of everything the match consumed
  — correct when a substitution genuinely collapses several original words ("Auxiliary Power Unit" →
  "APU").
- `apply_segments()` accepts a list of typed segments and preserves per-segment origins: a `(start, end)`
  slice carries its own character-level origin through unchanged, a `str` gets the union origin, and a
  `Verbatim` carries an explicit origin array. Use `apply_segments()` whenever a transformation only
  touches a match's *boundary* while leaving its interior words intact — e.g. stripping brackets around
  "(Refer to Task 79-24-00-870-7A)". Using plain `apply()` there would smear four distinct original words
  into one undifferentiated group and break forced-alignment timing for each of them.

**Tokenization constraint** — `ORIGINAL_WORD_RE = re.compile(r"\S+")` in `alignment.py` defines what
counts as one "original word" on the Python side. It must stay in sync with
`textContent.match(/\S+|\s+/g)` in `highlighter.js`'s `wrapTextNode()`, which defines what becomes one
`<span class="tts-word">` in the DOM. The backend and frontend must agree on word boundaries; if they
drift, timing entries index the wrong spans and highlighting breaks. Similarly, `PROCESSED_WORD_RE =
re.compile(r"\b[\w'-]+\b")` in `alignment.py` must match `TimingGenerator.split_words()` in `timing.py`.

### Frontend (`html/js/`)

Modular vanilla JS wired together in `app.js`, no framework/bundler:
- `api.js` — backend API client. `fetchSpeechBundle()` is the only entry point used by the session layer
  (POST `/speak` then GET timing JSON). Always sends `speed: SYNTHESIS_SPEED` (= `1.0`) to the backend,
  regardless of the user's speed control. The user's speed is applied entirely as `audio.playbackRate`
  client-side, never baked into synthesis. Changing `SYNTHESIS_SPEED` re-partitions the server-side
  cache (all existing entries become unreachable), so treat it as a deployment constant.
- `requestcoordinator.js` — "latest request wins" concurrency policy (a cache-hit response can return
  faster than an in-flight cache-miss from an earlier action, so ordering by completion time is wrong).
  Enforced via two mechanisms: `AbortController` (tears down in-flight fetches immediately) and a
  monotonic token (checked after every `await` to catch responses already in the microtask queue when
  the abort landed). Two separate coordinator instances are used in `app.js` — `playback` guards
  user-facing requests, `prefetch` guards background voice warming — so a prefetch can never abort the
  user's pipeline and the user's next action can abort the prefetch cleanly without ambiguity.
- `player.js` — wraps the single `<audio>` element. Uses a load-epoch counter (`_epoch`/`_liveEpoch`)
  to discard stale media events (metadata, error) from a superseded source. `unload()` removes the `src`
  attribute entirely and calls `load()` to flush the decoded buffer; pausing alone is not enough.
  `playbackRate` is session state held in `_rate` and re-asserted after every load, because some engines
  reset it when the resource selection algorithm re-runs.
- `timeline.js` / `highlighter.js` — map playback time to the currently-spoken word and render it.
  `Timeline` is an immutable lookup structure built from one voice's timing array; `ordinalAt(t)` is
  O(log n) and runs on every animation frame. Cross-voice position mapping goes through word space:
  `Timeline.projectPosition(position, from, to)` — never through wall-clock time, since male and female
  narrations of the same text have different durations.
- `speechsession.js` — voice/speed/position state machine. Speed changes are pure `audio.playbackRate`
  (instant, no re-synthesis, highlighting stays in sync because it reads `audio.currentTime`); voice
  changes are a real source swap that preserves reading position by projecting through *word index*, not
  wall-clock time. `_switchDepth` is a counter, not a boolean — two overlapping switches must not have
  the inner one clear the flag while the outer is still running, which would cause the footer to blink
  from "Paused" to something else on every switch. Two background prefetches, both on the `prefetch`
  coordinator so neither can abort a real `play()`: `preloadText(text)` (called from `renderContent()` as
  soon as a section renders, warming the CURRENT voice so the first Play is a client-cache hit rather than a
  cold fetch) and `_prefetchOtherVoice()` (after a successful play/switch, warming the other voice).
- `progressbar.js` — seek bar, driven by the same `onTimeUpdate` callback as the highlighter.

`app.js`'s click handlers are kept synchronous up to the point they hand off to `SpeechSession`/
`RequestCoordinator`, specifically so two rapid user actions can't interleave into a half-torn-down state
before the first `await`.

`renderer.js`'s `ContentRenderer` is a one-way JSON → HTML string builder ONLY — given a chapter/section
from `data2.json`, it returns markup for `#content`. It has no involvement in TTS, navigation, or word
highlighting, and never touches the DOM directly. Don't assume TTS-extraction or nav-related fixes belong
there; see below for where that logic actually lives.

#### `index.html`'s own inline systems

Beyond wiring `#content` in place, `index.html`'s trailing `<script>` blocks own several self-contained
subsystems that don't live in `html/js/*.js` at all — a deliberate choice to keep them out of the
external, more `app.js`-owned files:

- **Login gate** — a full-screen `#login-overlay` (hardcoded `CBT`/`CBT`, `sessionStorage`-backed) that
  gatekeeps `#appRoot`. This is a client-side UI convenience, not real access control — the credentials
  are visible in page source and trivially bypassed via devtools. `window.cbtLogout()` clears the session
  and re-locks the app; wired to the header's Logout icon button.
- **Calculators drawer** (`#toolbox-drawer`) — torque / pressure / temperature converters plus Ohm's law
  and voltage drop, opened from a header icon. A direct child of `#appRoot`, deliberately outside
  `#content`, so none of its labels or inputs can reach `currentChapterText()`. Converters recompute on
  `input` and avoid feedback loops by one rule: a handler only ever writes to fields OTHER than the one
  being typed in. Ohm's law picks which field to derive by EDIT RECENCY, not by which is blank — once all
  three are filled a blank-field test has nothing to go on and would overwrite one arbitrarily. Voltage
  drop uses `R = 2ρL/A` (the doubled length is the return path; omitting it halves the answer).
- **Custom video player** — sections in `data2.json` can embed a YouTube-style player
  (`.yt-video-wrapper` + `.yt-controls`, with a settings popup for playback speed/quality) directly inside
  a text block's `data.html`. `initCustomVideoPlayers()` (called from `renderContent()` on every render)
  wires up every such wrapper found under `#content`, scoped per-instance via `querySelector` on classes —
  never global `getElementById` — so multiple players on the page never collide.  A single global
  `fullscreenchange`/outside-click listener (`ensureVideoPlayerGlobalListeners()`, installed once) closes
  open settings popups and keeps the fullscreen icon in sync, including Esc-triggered exits.
- **TTS-extraction exclusion** — `app.js`'s `currentChapterText()` builds the string sent to Piper from
  `#content.innerText`, which walks every *visible* real DOM text node under `#content` — there is no
  per-element opt-out mechanism. The video player's button icons/time display are therefore rendered as
  CSS `::before { content: ... }` (driven by `data-state`/`data-label` attributes JS sets, never
  `.textContent`), not real text — `::before`/`::after` generated content is never part of the DOM tree
  `.innerText` walks, so it's excluded by spec, consistently across browsers. Don't revert this to
  `.textContent`/`.innerText` assignments on player controls without reintroducing that leak. (Anything
  positioned *outside* `#content` — the footer, the floating seek bar overlay — was never at risk in the
  first place, regardless of any `class="tts-ignore"`/`data-tts-skip` markers on it; those are
  intent-documentation, not the actual mechanism.)
- **Heading pause markers** — `app.js`'s `currentChapterText()` still builds the TTS string from
  `#content.innerText` (preserving every exclusion that depends on it), then inserts a `` (unit
  separator) after each `<h1>`–`<h4>`'s own `.innerText`, searching forward so two identically-titled
  headings each get their own marker. `preprocess.py` converts it to real punctuation. `` is chosen
  because it IS whitespace per both Python's `\s` and `ORIGINAL_WORD_RE` (`\S+`), so inserting it can never
  create a word that doesn't exist in the DOM — `original_words`/`processed_words` counts are unchanged.
  Note `host.py`'s `.strip()` also treats it as whitespace, so a marker that lands as the very last
  character is silently dropped (harmless: a heading with nothing after it has nothing to pause before).
  Because the marker mechanism keys off *any* `h1`–`h4` under `#content`, authoring real heading tags inside
  a `data.html` block (as Section 1's System Introduction does) gets pauses for free.
- **Global search** (`#headerSearch`) — index built from `flatNavList` via `buildSearchIndex()`, navigation
  reuses `focusNavNode()`. In-page match highlighting glows the **containing block element** via a class
  toggle, never wrapping the matched substring: `highlighter.js` tracks `.tts-word` spans by direct DOM
  reference, so splitting a text node to insert a `<mark>` would corrupt whichever span the match falls
  inside and break that word's TTS highlighting for the rest of the session.
- **Previous/Next navigation** — walks `flatNavList`, a single depth-first flattening of every real content
  node across **all** chapters (so Next at the end of one chapter continues into the next). `focusNavNode()`
  switches chapter with `{autoSelectFirst:false}` so the target node isn't rendered twice, expands ancestor
  submenus via `nodeRowMap`, and renders through the same `showSubtopic()` a sidebar click uses — which is
  also where `currentNavIndex` is updated, so the disabled states can't drift from how the page was reached.
- **Home module search + grid/list view** (`#home-module-search`, `#view-grid-btn`/`#view-list-btn`) — sit
  inline to the RIGHT of the landing title inside `.landing-head`, a `flex-wrap:wrap` row whose
  `max-width:900px` matches `.topic-cards` so their edges align; the wrap itself is what drops the
  controls below the title on a narrow viewport (the 760px media query only tidies the wrapped state,
  it does not cause the wrap). Both
  live inside `#landingView`, which is a SIBLING of `#topicView` (and therefore of `#content`) and is
  `[hidden]` whenever a chapter is open, so none of it is reachable by `currentChapterText()`. Filtering
  matches a per-card `data-search` attribute that `buildTopicCards()` precomputes from the section number,
  title, the chapter's lead paragraph and every flattened `toc` title — an attribute, so the topic titles
  are searchable without being rendered. `data2.json` has **no** `description` field: `chapterLeadText()`
  derives one from the first `type:"text"` block by stripping tags **with a regex, never by assigning
  `.innerHTML` to a detached element** (that would start a fetch for every `<img src>`, all of which point
  into the absent `html/media/`). `chapterSummary()` prettifies that for display (drops the block's own
  leading `1.1 GENERAL`-style numbering/heading) while the haystack keeps the FULL text, so a word trimmed
  for display is still findable. One card markup serves both views — the description and "Open System"
  button are always in the DOM and merely `display:none` in grid view — so search behaviour never depends
  on the active view. View preference persists in `localStorage` under `cbt_home_view`; anything that isn't
  exactly `"list"` resolves to grid, so a stale value degrades to the default. Card visibility uses the
  `hidden` attribute, relying on the global `[hidden]{display:none !important}` to beat `.topic-cards`'
  own `display:grid`.

- **Tooltips** — one shared implementation keyed on the `data-tooltip` ATTRIBUTE (`[data-tooltip]::after`),
  not on any component class, so the header icons, sub-header Back, sidebar toggle/re-open tab, footer
  transport + Prev/Next + zoom + Quiz, and the drawer close buttons all share one look and one fade. This
  is the ONLY thing those buttons share — each keeps its own background/border/colour. Placement is the
  only variable, opt-in via `data-tooltip-pos` (`above-right`, `below`, `below-right`, `right`; default is
  above-centred); the right-anchored variants exist for controls in a right-hand group or against a
  clipping ancestor (the sidebar head sits inside `.sidebar`'s `overflow:auto`). A control must carry
  `data-tooltip` **or** a native `title`, never both, or the browser draws its own box on top. The text is
  `::after` generated content, so it is never part of the DOM tree `.innerText` walks. The Speed/Voice
  dropdown triggers deliberately have NO tooltip — their visible label already is the current value.

- **Content-only zoom** — CSS `zoom` on `#content` alone (not `font-size`: much of the content CSS uses
  fixed px, so only `zoom` scales tables/images/spacing proportionally). Survives navigation for free
  because `renderContent()` replaces `#content`'s children, never the element itself.
- **Floating seek bar overlay** (`#audio-seekbar-overlay`, reusing the same `#seekBar`/`#currentTime`/
  `#duration` elements `ProgressBar` in `progressbar.js` already binds to — only the wrapping container's
  position/visibility changed) — shown only while TTS audio is actually playing (`player.audio`'s native
  `play`/`ended` events), unlike `#tts-footer`'s transport buttons, which stay visible for as long as a
  topic is open regardless of playback state. Its `bottom` offset, `#tts-footer`'s reserved layout space
  (`.layout`'s height) and `main`'s bottom padding all read from one `--footer-height` custom property
  rather than independently hardcoded numbers — always use the variable, or they drift out of sync (they
  did once already). `--footer-height` is **measured at runtime** by `trackFooterHeight()` (a
  `ResizeObserver` on `#tts-footer`, plus a `MutationObserver` on its `hidden` attribute so the first real
  measurement lands when it's revealed) and written back onto `:root`. That's necessary because the footer
  *wraps* on narrow viewports — its height is a function of window width, so any per-breakpoint constant
  would be a guess that silently hides content or leaves a dead gap. A 0-height (hidden) footer is never
  published, so the last real measurement survives the landing view.
- **Unified footer Play/Pause button** (`#btn-play-pause`) — `app.js` still owns a real 4-state machine
  (`UI_STATE.IDLE/GENERATING/PLAYING/PAUSED`) across its original, now visually-`hidden` `#playBtn`/
  `#pauseBtn`. Rather than reimplementing that state machine, `#btn-play-pause` delegates its click to
  whichever hidden button is currently the live action (`pauseBtn` when enabled, `playBtn` otherwise) and
  a `MutationObserver` mirrors that button's label — so it can never drift from `app.js`'s real state.
- All of the above attach *additional* `addEventListener` calls directly on `player.audio` (the real
  `<audio>` element) rather than reassigning `player.onPlay`/`.onPause`/`.onEnded`/`.onTimeUpdate` —
  `app.js` already owns those single-callback properties for the highlighter and the seek bar, so
  overwriting any of them would silently break both. `player` (from `player.js`) is reachable as a bare
  identifier here — not `window.player` — because classic (non-module) `<script>` tags share one global
  scope; `app.js` itself relies on the same thing.

#### Overlay stacking order

Several independent overlays can be open at once, so their `z-index` values form a deliberate ladder —
check it before adding another: footer Speed/Voice `.dropdown-menu` **1100** → `.search-results` **1200**
→ `#toolbox-backdrop` **1300** → `#toolbox-drawer` **1400** → `#login-overlay` **9999** (must always win;
it gatekeeps everything). `#audio-seekbar-overlay` is **1000** — the dropdowns sit above it specifically
because they used to render *behind* it: both are children of the position:fixed `#tts-footer`, and the
intermediate wrappers (`.footer-dropdown`, `.controls-row`) are `position:relative` with no `z-index`, so
they create no stacking context and the two are compared directly against each other regardless of DOM
order.

#### Responsive layout

A RESPONSIVE section at the end of the stylesheet owns three breakpoints (1100 / 768 / 480px). The 1100px
one is the important one and the reason it isn't 768: `.footer-nav-controls` (Prev/Next) and `#quizBtn` are
`position:absolute` at full width to pin them to the footer's corners, which puts them **outside the flex
flow** where `.controls-row`'s `flex-wrap` cannot avoid them — summing real control widths, the centre group
starts colliding with the pinned sides at roughly 1100px, a laptop width. Below it both groups return to
`position:static` so wrapping actually works (and `#quizBtn:hover` must then drop the `translateY(-50%)`
it uses for pinned vertical centring, or it jumps). Anything else pinned into a footer corner needs the
same treatment. `.header-title` carries `min-width:0` because a flex item defaults to `min-width:auto` and
would otherwise refuse to shrink, pushing the header buttons off-screen instead of ellipsising.

### Launcher (`native_host/launcher/`)

Pure standard library, platform-aware (Windows vs Linux/macOS paths, `taskkill` vs `SIGTERM`). Startup
sequence (`__main__.py`): resolve config → configure logging → single-instance check via port polling →
`environment.ensure_environment` (builds/reuses the venv from `packages/` wheels, fingerprinted by
`requirements.txt` so an edit there triggers a rebuild) → `torch_assets.prepare_torch_home` → spawn
`host.py` windowless via `pythonw.exe` (`server.py`) → `readiness.wait_until_ready` polls `/healthz`
(falls back to `/` for older `host.py` builds) → open browser (`browser.py`) → supervise until exit.
`config.py` (`resolve_config`/`LauncherConfig`) is the single source of truth for on-disk paths across all
four deployment shapes (dev `python -m launcher`, `bootstrap.py` via bundled interpreter, frozen
`CBT.exe`, Linux/macOS) — every other launcher module receives a fully-resolved `LauncherConfig`
rather than computing paths itself; keep new path logic there too.

`tools/build_exe.bat` builds `CBT.exe` from a **separate** temp build venv (not the runtime venv),
specifically so PyInstaller never pollutes the shipped requirements fingerprint. The frozen exe wraps only
the launcher, not torch/the app — see the header comment in `tools/CBT.spec` for why. If you touch
`tools/CBT.spec`'s `excludes` list, don't exclude `email` (breaks something downstream — see
`CBT/launcher.md`).
