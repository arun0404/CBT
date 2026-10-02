# CBT

A fully offline (air-gapped) desktop text-to-speech reader. [Piper](https://github.com/rhasspy/piper)
synthesizes audio for a chapter of text, torchaudio's MMS_FA model force-aligns the audio to get real
per-word timings, and a Flask-served single-page frontend plays the audio while highlighting the word
currently being spoken.

## Features

- Offline neural TTS with two voices (male / female) and instant voice switching that keeps your reading position
- Word-level highlighting driven by forced alignment, with an equal-duration fallback if alignment fails
- Playback speed applied client-side, so changing speed never re-synthesizes
- Text preprocessing for technical content: abbreviations, units, acronyms, reference codes read digit by digit
- Content-addressed alignment cache and background prefetch of the other voice
- Request cancellation so navigating away frees the CPU instead of finishing abandoned jobs
- Global search, Previous/Next navigation, content zoom, calculators drawer, and a custom video player with captions
- Windows-first desktop launcher that builds its own virtual environment from a local wheelhouse

## Repository layout

| Path | Purpose |
|------|---------|
| `CBT/native_host/` | Flask app (`host.py`), TTS pipeline, text processing, forced aligner, launcher |
| `CBT/html/` | Frontend (plain HTML/CSS/JS, no bundler) and the manual content in `data2.json` |
| `CBT/native_host/launcher/` | Launcher package: venv bootstrap, process supervision, readiness polling |
| `tools/` | PyInstaller build and shortcut-creation scripts |
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

## Captions (optional, dev-only)

`CBT/native_host/services/caption_generator.py` transcribes a local video into a `.vtt` file using
`faster-whisper`. It has its own `requirements-captions.txt` and is deliberately not part of the runtime venv.

## Requirements

- Windows 10/11 (the launcher is Windows-first; Linux/macOS paths exist but are less exercised)
- Python 3.10 with the packages in `CBT/native_host/requirements.txt` (torch and torchaudio are pinned;
  forced alignment relies on APIs deprecated in torchaudio 2.8)
