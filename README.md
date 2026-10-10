# CBT

A fully offline (air-gapped) desktop text-to-speech reader. [Piper](https://github.com/rhasspy/piper)
synthesizes audio for a chapter of text, torchaudio's MMS_FA model force-aligns the audio to get real
per-word timings, and a Flask-served single-page frontend plays the audio while highlighting the word
currently being spoken.

## Features

- Offline neural TTS with two voices (male / female) and instant voice switching that keeps your reading position
- Word-level highlighting driven by forced alignment, with an equal-duration fallback if alignment fails. The page's
  words are matched to the narrated text word for word, so figures, icons, dashes, quotes and glued punctuation cannot shift the highlight
- Playback speed applied client-side, so changing speed never re-synthesizes
- Text preprocessing for technical content: abbreviations, units, acronyms (spelled letter by letter in brackets), reference codes read digit by digit
- Content-addressed alignment cache and background prefetch of the other voice
- Request cancellation so navigating away frees the CPU instead of finishing abandoned jobs
- Global search, Previous/Next navigation, content zoom, calculators drawer, and a custom video player with captions
- Compact, touch-friendly footer on phones and small tablets (transport bar plus a "More" menu)
- Built-in quiz: questions for every section, instant feedback with explanations, scores, history, and a review list of missed questions (see [Quiz](#quiz))
- Windows-first desktop launcher that builds its own virtual environment from a local wheelhouse

## Repository layout

| Path | Purpose |
|------|---------|
| `CBT/native_host/` | Flask app (`host.py`), TTS pipeline, text processing, forced aligner, launcher |
| `CBT/html/` | Frontend (plain HTML/CSS/JS, no bundler), the manual content in `data2.json` and the quiz questions in `quiz.json` |
| `CBT/native_host/launcher/` | Launcher package: venv bootstrap, process supervision, readiness polling |
| `tools/` | PyInstaller build, shortcut-creation scripts, and `validate_quiz.py` |
| `run.bat`, `launch.vbs`, `CBT.exe` | Entry points that forward into `native_host/bootstrap.py` |

See `README-LAUNCHER.md` for launcher behavior, exit codes and runtime state layout.

## Not included in this repository

The following are too large for GitHub and are excluded via `.gitignore`. They must be supplied locally
before the app can synthesize audio:

- `CBT/piper/` — Piper executable, MMS_FA alignment model (`model.pt`) and torch hub cache
- `CBT/voices/` — `male.onnx` and `female.onnx` voice models
- `CBT/native_host/Python310/` — bundled Python 3.10 used to create the runtime venv
- `CBT/native_host/packages/` — offline wheelhouse (`pip install --no-index`)
- `CBT.exe`, `CBT/output/` (generated cache), and `*.mp4` video assets

## Running

```bat
run.bat                     REM build venv on first run, start server, open browser
run.bat --app-window        REM chromeless Edge/Chrome window
run.bat --no-browser        REM start the server only
run.bat --status            REM machine-readable JSON status
run.bat --stop              REM stop a running instance
run.bat --rebuild-env       REM delete and rebuild the virtual environment
run.bat --port 5050         REM use a different port
```

For backend development, run `python host.py` inside `CBT/native_host` with an interpreter that has
`requirements.txt` installed. It serves on `127.0.0.1:5000` with debug on.

Environment overrides: `CBT_HOME`, `CBT_VENV`, `CBT_HOST`, `CBT_PORT`, `CBT_DEBUG`.

Runtime state (venv, pid file, logs) lives under `%LOCALAPPDATA%\CBT_venv\`, never in the application directory.

## Quiz

The footer's **Quiz** button opens a quiz on what you have just read. There are 326 questions, covering all
46 sections of the 12 chapters. Choose what to practise — this topic, this chapter, the whole manual, or your
missed questions — and how many questions (5, 10, 20 or all).

- **Instant feedback.** Every answer shows right or wrong plus a short explanation, with a link to open that
  topic in the manual. Keyboard: press `1`–`4` or `A`–`D` to answer, `Enter` for the next question, `Esc` to
  close (a quiz in progress is kept and resumes when you reopen it). Narration is paused while the quiz is open.
- **Scores and history.** The results screen shows your score and how it compares with your last attempt at
  the same thing. *Scores & history* lists recent quizzes (tap one to review its answers), your average and
  best scores, and the topics you do worst on.
- **Missed questions.** Anything answered wrongly goes on a list; answer it correctly in a later quiz and it
  drops off. Review the list, or practise it as a quiz.

Results are kept in the browser (`localStorage`, key `cbt_quiz_v1`), so they belong to one browser profile and
one address: running on another `--port`, or in another browser, starts with an empty history. If the
browser refuses to store data, the quiz still works for the session and says so. *Clear history…* resets
scores and the missed list.

### Editing the questions

`CBT/html/quiz.json` holds the questions, grouped by the section ids used in `data2.json` (`t0004`, …). Each
question has an `id`, the question text `q`, its `options`, the index of the correct one in `answer`, an
`explain` text shown after answering, and `src` — the wording in the manual the answer was written from.
Two-option questions are true/false. Option order is shuffled each time a quiz is taken.

- Never renumber or reuse an `id`: saved history refers to it. Reword freely.
- After editing `quiz.json` **or** `data2.json`, run `python tools/validate_quiz.py`. It checks the structure,
  that every section still has questions, and that each `src` phrase is still in its section — so a question
  made stale by a change to the manual is flagged. `--strict` makes warnings fail too.

## Pronunciation dictionaries

Text is expanded before it reaches Piper using dictionaries in `CBT/native_host/text_processing/`:

- `client_dictionary.json` holds the project's acronyms. It is matched **case-sensitively** and as whole words,
  so `CBS` (Chip Burning System) and `CBs` (Circuit Breakers), or `MFDS` and `MFDs`, are separate entries; write
  each key exactly as it appears in the manual. The in-app glossary search is case-sensitive on acronyms too.
  An entry's expansion is **spoken exactly as written**: no later step rewrites it, so spell out anything you want
  said (write "Latitude and Longitude", not "Latitude/Longitude", and "Revolutions Per Minute", not "RPM").
  Only `&` and `+` are voiced for you ("and", "plus"). Avoid keys that are ordinary words (`IF`, `SET`, `TOP`):
  they fire in any ALL-CAPS warning or heading. A key containing `/` or `.` (`kg/hr`, `g/kW.h`, `L/L`, `N.m`) is
  matched as a whole token before the unit and single-letter rules run, so "45 kg/hr" is "45 kilograms per hour".
- `engineering.py` and `abbreviations.py` hold general terms and are matched case-insensitively.
- The letter `L` is ambiguous (litres or length), so it is resolved by context in `preprocess.py` rather than by a
  dictionary: `2.0L`, `1 L` (singular), `(50L)` and `L/100km` are litres; `L x W x H`, `L = 250` and
  `wheelbase (L)` are length. A bare `L` anywhere else is left as the letter.
- An ALL-CAPS acronym **inside brackets** is read letter by letter: `Integrated Air Defence System (IADS)` is
  "... I A D S" and `(NATO)` is "N A T O". Outside brackets nothing changes. Left as words: abbreviations the app
  already expands (`FIG`, `KG`), ordinary words (`NOTE`, `ON`, `OFF`; the list is `SPEAK_AS_WORD` in
  `parentheticals.py`), long words such as `(BOOSTER)`, and acronyms that sit inside a longer bracketed sentence
  when the dictionary can already expand them.
- Roman-numeral list markers are read as numbers: `i.` `i)` `(i)` at the start of a line are "One." (and so on up to
  `xx`), and so are the upper-case forms `II.` `IV)` `(III)`. A single capital `I.` / `V.` / `X.` is left alone (a
  pronoun or an initial). Inside a sentence a bracketed numeral is spoken as a number when it opens a clause or sits in
  a run: `do: (i) clean it; (ii) replace it` is "One, clean it; Two, replace it". A bare `i`, `v` or `x`, and a
  variable such as `velocity (v)` or `Voltage (V)`, stays a letter.

Editing a dictionary re-generates the cached audio for affected text automatically. Changing how matching
*works* (code, not entries) needs a bump of `ALIGNMENT_CACHE_VERSION` in `CBT/native_host/config.py`.

## Captions (optional, dev-only)

`CBT/native_host/services/caption_generator.py` transcribes a local video into a `.vtt` file using
`faster-whisper`. It has its own `requirements-captions.txt` and is deliberately not part of the runtime venv.

## Requirements

- Windows 10/11 (the launcher is Windows-first; Linux/macOS paths exist but are less exercised)
- Python 3.10 with the packages in `CBT/native_host/requirements.txt` (torch and torchaudio are pinned;
  forced alignment relies on APIs deprecated in torchaudio 2.8)
