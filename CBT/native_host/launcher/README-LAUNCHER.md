# CBT Launcher

Turns the application package into a double-clickable desktop app: no fixed
sleeps, no repeated `pip install`, no console window.

---

## 1. File placement

Copy the files into your existing tree exactly like this:

```
<PACKAGE ROOT>\                     (the folder that currently holds run.bat)
├── run.bat                         REPLACE the existing file
├── launch.vbs                      NEW  - silent launcher (Option A)
├── stop.bat                        NEW
├── CBT.exe                  NEW  - produced by tools\build_exe.bat (Option B)
├── tools\
│   ├── create_shortcut.ps1         NEW
│   ├── CBT.spec             NEW
│   ├── build_exe.bat               NEW
│   └── CBT.ico              SUPPLY YOUR OWN (256x256 recommended)
└── CBT\
    ├── piper\
    │   ├── model\model.pt          existing
    │   └── torch_home\             created automatically
    └── native_host\
        ├── bootstrap.py            NEW
        ├── launcher\               NEW  (10 modules)
        ├── host.py                 apply patches\host_py_changes.md
        ├── requirements.txt        existing
        ├── packages\               existing offline wheelhouse
        └── Python310\              existing bundled interpreter
```

Then apply the three edits in `patches/host_py_changes.md`. Edit #1 is
mandatory; the app will still run without it, but every start loads the
forced-alignment model twice.

---

## 2. Runtime state

Nothing is written inside the application directory except
`CBT\piper\torch_home`. Everything else lives per-user:

```
%LOCALAPPDATA%\CBT_venv\
├── venv\                       the virtual environment
│   └── .cbt-env.json    fingerprint stamp (drives fast restarts)
└── state\
    ├── CBT.pid
    ├── browser-profile\        only used with --app-window
    └── logs\
        ├── launcher.log        rotating, 2 MB x 4
        ├── server.log          host.py stdout/stderr
        └── pip-install.log     full output of the last dependency install
```

Uninstall = delete that folder plus the application directory.

---

## 3. Commands

All wrappers forward their arguments, so these work through `run.bat`,
`bootstrap.py`, or `CBT.exe`.

| Command | Purpose |
|---|---|
| `run.bat` | Normal launch |
| `run.bat --app-window` | Chromeless Edge/Chrome window (desktop-app feel) |
| `run.bat --no-browser` | Start the server only |
| `run.bat --detach` | Launch, open the browser, exit; server keeps running |
| `run.bat --show-console` | Show the host's console window (debugging) |
| `run.bat --status` | Machine-readable status as JSON |
| `run.bat --stop` | Stop a running instance |
| `run.bat --rebuild-env` | Delete and rebuild the virtual environment |
| `run.bat --setup-only` | Build the environment without starting the app |
| `run.bat --port 5050` | Use a different port |
| `run.bat --verbose` | Debug-level logging |

---

## 4. Exit codes

| Code | Meaning | First thing to check |
|---|---|---|
| 0 | Success | — |
| 1 | Unexpected error | `launcher.log` traceback |
| 2 | Configuration / missing files | Was the folder moved or partially copied? |
| 3 | Environment build failed | `pip-install.log`; is `packages\` complete? |
| 4 | Voice model provisioning failed | Disk space, permissions, `model.pt` present |
| 5 | Host process could not start | Is the venv intact? `--rebuild-env` |
| 6 | Host crashed at startup | `server.log` — usually a missing dependency |
| 7 | Readiness timeout | `server.log`; raise `--timeout` on very slow machines |

---

## 5. Option A — silent launcher + desktop shortcut

```powershell
powershell -ExecutionPolicy Bypass -File tools\create_shortcut.ps1 -Target vbs -StartMenu
```

`launch.vbs` runs under `wscript.exe`, which owns no console, so nothing
flashes on screen. On the very first run it shows a short "one-time setup"
prompt and runs the install visibly, because a silent multi-minute torch
install is indistinguishable from a failed launch.

For a shortcut with a custom icon, drop a `.ico` at `tools\CBT.ico`
before running the script.

## 6. Option B — single executable

```bat
tools\build_exe.bat
powershell -ExecutionPolicy Bypass -File tools\create_shortcut.ps1 -Target exe
```

Produces `CBT.exe` (~8-12 MB) at the package root. The exe freezes only
the launcher, not torch — see the header comment in `tools\CBT.spec` for
why freezing the whole application is the wrong trade here.

The exe must sit in the folder that *contains* the `CBT` directory, or
be pointed at it with `CBT_HOME`.

---

## 7. Environment variable overrides

| Variable | Effect |
|---|---|
| `CBT_HOME` | Path to `CBT\native_host` |
| `CBT_VENV` | Venv root (`venv\` and `state\` are created inside) |
| `CBT_HOST` | Bind interface (default `127.0.0.1`) |
| `CBT_PORT` | Bind port (default `5000`) |

Useful for side-by-side test installs and for CI smoke tests.

---

## 8. Linux / macOS

The launcher package is pure standard library and platform-aware
(`Scripts` vs `bin`, `.exe` suffixes, `SIGTERM` vs `taskkill`). Only the
`.bat`/`.vbs`/`.ps1` wrappers are Windows-specific:

```sh
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/CBT/native_host"
exec python3 bootstrap.py "$@"
```

Save as `run.sh`, `chmod +x run.sh`. State lands in
`$XDG_DATA_HOME/CBT` (or `~/.local/share/CBT`).

---

## 9. Verified behaviour

Exercised against a stub application with the same directory layout:

- cold start builds the venv, provisions the checkpoint, and opens on readiness
- warm start reuses the venv; launcher overhead measured at ~0.13 s
- readiness detected within 0.31 s of the server binding (250 ms poll interval)
- second launch while running takes the single-instance path in ~0.12 s
- host crash during startup fails in 0.39 s instead of waiting out a 60 s timeout
- editing `requirements.txt` invalidates the fingerprint and triggers a rebuild
- exit codes 2 and 6 confirmed against the documented contract
- unusable preferred `TORCH_HOME` falls back to the per-user state directory
