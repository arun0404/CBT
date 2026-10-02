"""
Path resolution and static configuration for the launcher.

This is the single place that knows the on-disk layout. Every other
module receives a fully-resolved :class:`LauncherConfig` and never
touches ``Path(__file__)`` or environment variables itself, which is what
makes the same code work in all four deployment shapes:

    1. ``python -m launcher``            (developer, cwd == native_host)
    2. ``Python310\\python.exe bootstrap.py``  (run.bat / launch.vbs)
    3. ``CBT.exe``                (PyInstaller, sys.frozen is set)
    4. ``python3 bootstrap.py``          (Linux / macOS)

Every default can be overridden with an environment variable so the same
build can be pointed at a test tree without editing code:

    CBT_HOME   -> the native_host directory
    CBT_VENV   -> the venv *root* (venv/ and state/ live inside)
    CBT_PORT   -> port the Flask host binds
    CBT_HOST   -> interface the Flask host binds
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigurationError

APP_NAME = "CBT"
IS_WINDOWS = os.name == "nt"

# --------------------------------------------------------------------
# Network / readiness defaults
# --------------------------------------------------------------------

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5000

# Cold start on a machine that has never loaded the MMS_FA weights is
# dominated by the torch import plus the forced-aligner model load, not
# by anything the launcher does. The deadline therefore has to be
# generous; the poll loop exits the *instant* the server answers, so a
# large timeout costs nothing on a fast machine.
DEFAULT_READY_TIMEOUT = 300.0
DEFAULT_POLL_INTERVAL = 0.25

# Preferred readiness probe. Falls back to "/" (which serves index.html)
# if the host has not yet been patched with the /healthz route, so this
# launcher works against both the old and new host.py.
HEALTH_PATH = "/healthz"
FALLBACK_HEALTH_PATH = "/"

# --------------------------------------------------------------------
# Environment variable names
# --------------------------------------------------------------------

ENV_HOME = "CBT_HOME"
ENV_VENV_ROOT = "CBT_VENV"
ENV_PORT = "CBT_PORT"
ENV_HOST = "CBT_HOST"


def _default_venv_root() -> Path:
    """
    Per-user, writable, and outside the application directory.

    Deliberately not next to the app: the package may be installed under
    ``C:\\Program Files`` (non-writable for standard users) or run from
    read-only media, and a per-user venv also means two accounts on the
    same machine do not fight over one interpreter.
    """

    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
        # Per-user venv + state, under the app name. Installs created
        # before the app was renamed will build a fresh venv here on
        # first launch; the old %LOCALAPPDATA% venv directory can be
        # deleted by hand.
        return root / "CBT_venv"

    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / APP_NAME


def _detect_native_host_dir() -> Path:
    """Locate ``CBT/native_host`` for the current deployment shape."""

    override = os.environ.get(ENV_HOME)
    if override:
        candidate = Path(override).expanduser().resolve()
        if not (candidate / "host.py").is_file():
            raise ConfigurationError(
                f"{ENV_HOME} points at {candidate}, which contains no host.py.",
                hint=f"Unset {ENV_HOME} or point it at CBT/native_host.",
            )
        return candidate

    if getattr(sys, "frozen", False):
        # PyInstaller build: the exe normally sits beside the CBT
        # folder, but tolerate it being dropped inside native_host too.
        exe_dir = Path(sys.executable).resolve().parent
        for candidate in (
            exe_dir / APP_NAME / "native_host",
            exe_dir / "native_host",
            exe_dir,
        ):
            if (candidate / "host.py").is_file():
                return candidate
        raise ConfigurationError(
            f"Could not locate {APP_NAME}/native_host relative to {exe_dir}.",
            hint=(
                f"Place {APP_NAME}.exe in the folder that CONTAINS the "
                f"{APP_NAME} directory, or set {ENV_HOME}."
            ),
        )

    # Source layout: this file is native_host/launcher/config.py
    return Path(__file__).resolve().parent.parent


def _detect_bootstrap_python(native_host_dir: Path) -> Path:
    """
    The interpreter used to *create* the venv.

    Prefers the interpreter shipped with the application so the venv is
    always built from a known-good Python 3.10 rather than whatever
    happens to be on the user's PATH.
    """

    bundled = native_host_dir / "Python310" / (
        "python.exe" if IS_WINDOWS else "bin/python3"
    )
    if bundled.is_file():
        return bundled

    if not getattr(sys, "frozen", False):
        return Path(sys.executable)

    for name in ("python3", "python"):
        found = shutil.which(name)
        if found:
            return Path(found)

    raise ConfigurationError(
        "No usable Python interpreter found to build the virtual environment.",
        hint=(
            f"Expected {native_host_dir / 'Python310'} to exist, or a python3 "
            "on PATH."
        ),
    )


def _venv_bin_dir(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts" if IS_WINDOWS else "bin")


@dataclass(frozen=True)
class LauncherConfig:
    """Fully-resolved, immutable view of where everything lives."""

    native_host_dir: Path
    app_root: Path
    project_root: Path
    bootstrap_python: Path
    venv_root: Path
    venv_dir: Path
    state_dir: Path
    log_dir: Path
    host: str
    port: int

    # ---------------- derived application paths ----------------

    @property
    def host_script(self) -> Path:
        return self.native_host_dir / "host.py"

    @property
    def requirements_file(self) -> Path:
        return self.native_host_dir / "requirements.txt"

    @property
    def wheelhouse_dir(self) -> Path:
        """Offline wheel directory used with ``pip --no-index``."""
        return self.native_host_dir / "packages"

    @property
    def bundled_model_file(self) -> Path:
        """The MMS_FA checkpoint shipped inside the package."""
        return self.app_root / "piper" / "model" / "model.pt"

    # ---------------- derived venv paths ----------------

    @property
    def venv_python(self) -> Path:
        return _venv_bin_dir(self.venv_dir) / ("python.exe" if IS_WINDOWS else "python")

    @property
    def venv_pythonw(self) -> Path:
        """Console-less interpreter. Windows only; falls back to python."""
        if not IS_WINDOWS:
            return self.venv_python
        return _venv_bin_dir(self.venv_dir) / "pythonw.exe"

    @property
    def env_stamp_file(self) -> Path:
        return self.venv_dir / ".cbt-env.json"

    # ---------------- derived state paths ----------------

    @property
    def pid_file(self) -> Path:
        return self.state_dir / f"{APP_NAME}.pid"

    @property
    def launcher_log(self) -> Path:
        return self.log_dir / "launcher.log"

    @property
    def server_log(self) -> Path:
        return self.log_dir / "server.log"

    @property
    def pip_log(self) -> Path:
        return self.log_dir / "pip-install.log"

    # ---------------- URLs ----------------

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def health_url(self) -> str:
        return self.base_url + HEALTH_PATH

    @property
    def fallback_health_url(self) -> str:
        return self.base_url + FALLBACK_HEALTH_PATH

    def ensure_state_dirs(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)


def _resolve_port(explicit: int | None) -> int:
    if explicit is not None:
        return explicit
    raw = os.environ.get(ENV_PORT)
    if not raw:
        return DEFAULT_PORT
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigurationError(
            f"{ENV_PORT} must be an integer, got {raw!r}."
        ) from exc


def resolve_config(port: int | None = None, host: str | None = None) -> LauncherConfig:
    """Build the :class:`LauncherConfig` for this process. Cheap; no I/O beyond stat()."""

    native_host_dir = _detect_native_host_dir()

    if not (native_host_dir / "host.py").is_file():
        raise ConfigurationError(
            f"host.py not found in {native_host_dir}.",
            hint="The application directory looks incomplete or was moved.",
        )

    app_root = native_host_dir.parent
    venv_root_env = os.environ.get(ENV_VENV_ROOT)
    venv_root = (
        Path(venv_root_env).expanduser().resolve()
        if venv_root_env
        else _default_venv_root()
    )
    state_dir = venv_root / "state"

    return LauncherConfig(
        native_host_dir=native_host_dir,
        app_root=app_root,
        project_root=app_root.parent,
        bootstrap_python=_detect_bootstrap_python(native_host_dir),
        venv_root=venv_root,
        venv_dir=venv_root / "venv",
        state_dir=state_dir,
        log_dir=state_dir / "logs",
        host=host or os.environ.get(ENV_HOST) or DEFAULT_HOST,
        port=_resolve_port(port),
    )
