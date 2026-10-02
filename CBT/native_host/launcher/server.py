"""
Lifecycle management for the Flask host process.

Responsibilities:
  * spawn ``host.py`` inside the venv with no console window
  * hand it a clean, explicitly air-gapped environment
  * capture its stdout/stderr into a rotating log (mandatory: with no
    console attached, unredirected output is lost and a crash becomes
    undiagnosable)
  * record a pid file so ``--stop`` and ``--status`` work from any shell
  * supervise, and shut the child down cleanly when the launcher exits

The child is deliberately started with ``CREATE_NEW_PROCESS_GROUP`` on
Windows so that a Ctrl+C in a visible console reaches the launcher only.
Shutdown is then explicit and ordered (terminate, wait, kill), rather
than two processes racing to tear down the same port.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .config import IS_WINDOWS, LauncherConfig
from .errors import ServerStartError
from .logging_setup import get_logger

logger = get_logger("server")

MAX_SERVER_LOG_BYTES = 5 * 1024 * 1024
GRACEFUL_SHUTDOWN_SECONDS = 10.0
LOG_TAIL_LINES = 40


def _rotate_if_large(path: Path, max_bytes: int = MAX_SERVER_LOG_BYTES) -> None:
    """Single-generation rotation. Enough for a desktop app; no extra deps."""

    try:
        if path.is_file() and path.stat().st_size > max_bytes:
            backup = path.with_suffix(path.suffix + ".1")
            backup.unlink(missing_ok=True)
            path.replace(backup)
    except OSError:
        logger.debug("Could not rotate %s", path, exc_info=True)


def read_log_tail(path: Path, lines: int = LOG_TAIL_LINES) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(content.strip().splitlines()[-lines:])


def build_child_environment(config: LauncherConfig, torch_home: Path) -> dict[str, str]:
    """
    Environment for the host process.

    Inherits the parent environment (the venv layout and any corporate
    proxy settings must survive) and then pins the values the app relies
    on. The offline flags are defence in depth: if a future dependency
    ever tries to reach a model hub, it fails fast and locally instead of
    hanging on a network timeout inside an air-gapped deployment.
    """

    env = os.environ.copy()

    env["TORCH_HOME"] = str(torch_home)

    # Production run: no reloader, no debugger. See the host.py patch.
    env["CBT_DEBUG"] = "0"
    env["FLASK_DEBUG"] = "0"
    env["FLASK_ENV"] = "production"
    env["CBT_HOST"] = config.host
    env["CBT_PORT"] = str(config.port)

    # Unbuffered + UTF-8 so the captured log is complete and correct even
    # if the process is killed (see host.py's own stream reconfiguration).
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "0"

    # Air-gap guards.
    env["HF_HUB_OFFLINE"] = "1"
    env["TRANSFORMERS_OFFLINE"] = "1"

    return env


class HostProcess:
    """Owns exactly one ``host.py`` child process."""

    def __init__(self, config: LauncherConfig, torch_home: Path, windowless: bool = True):
        self._config = config
        self._torch_home = torch_home
        self._windowless = windowless
        self._process: subprocess.Popen | None = None
        self._log_handle = None

    # ---------------- lifecycle ----------------

    def _interpreter(self) -> Path:
        """
        ``pythonw.exe`` when hidden, else ``python.exe``.

        pythonw is preferred over CREATE_NO_WINDOW because it also
        prevents a console being allocated if any dependency calls into
        code that would attach one.
        """

        if self._windowless and self._config.venv_pythonw.is_file():
            return self._config.venv_pythonw
        return self._config.venv_python

    def start(self) -> None:
        config = self._config
        interpreter = self._interpreter()

        if not interpreter.is_file():
            raise ServerStartError(
                f"Interpreter not found at {interpreter}.",
                hint="Rebuild the environment with:  run.bat --rebuild-env",
            )

        config.ensure_state_dirs()
        _rotate_if_large(config.server_log)

        creationflags = 0
        start_new_session = False
        if IS_WINDOWS:
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            start_new_session = True

        logger.info("Starting host: %s %s", interpreter.name, config.host_script.name)

        try:
            self._log_handle = config.server_log.open("a", encoding="utf-8", errors="replace")
            self._log_handle.write(
                f"\n{'=' * 70}\n"
                f"Session started {time.strftime('%Y-%m-%d %H:%M:%S')} "
                f"on {config.base_url}\n{'=' * 70}\n"
            )
            self._log_handle.flush()

            self._process = subprocess.Popen(
                [str(interpreter), str(config.host_script)],
                cwd=str(config.native_host_dir),
                env=build_child_environment(config, self._torch_home),
                stdin=subprocess.DEVNULL,
                stdout=self._log_handle,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
                start_new_session=start_new_session,
                close_fds=True,
            )
        except OSError as exc:
            self._close_log()
            raise ServerStartError(
                f"Could not start the host process: {exc}",
                hint=f"Verify that {config.host_script} exists.",
            ) from exc

        write_pid_file(config, self._process.pid)
        logger.info("Host process started (pid %s); output -> %s", self._process.pid, config.server_log)

    def is_alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def returncode(self) -> int | None:
        return self._process.poll() if self._process else None

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process else None

    def log_tail(self) -> str:
        return read_log_tail(self._config.server_log)

    def wait(self) -> int:
        """Block until the child exits. Returns its exit code."""
        if self._process is None:
            return 0
        return self._process.wait()

    def stop(self, timeout: float = GRACEFUL_SHUTDOWN_SECONDS) -> None:
        """Terminate, wait, then kill. Always clears the pid file."""

        process = self._process
        if process is not None and process.poll() is None:
            logger.info("Stopping host process (pid %s) ...", process.pid)
            try:
                process.terminate()
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                logger.warning("Host did not exit in %.0fs; killing.", timeout)
                process.kill()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    logger.error("Host process could not be killed.")
            except OSError as exc:
                logger.warning("Error while stopping host process: %s", exc)

        self._close_log()
        clear_pid_file(self._config)

    def _close_log(self) -> None:
        if self._log_handle is not None:
            try:
                self._log_handle.close()
            except OSError:
                pass
            self._log_handle = None

    # ---------------- context manager ----------------

    def __enter__(self) -> "HostProcess":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()


# ------------------------------------------------------------------
# Pid file helpers — used by --stop / --status, which must work from a
# different process than the one that started the server.
# ------------------------------------------------------------------


def write_pid_file(config: LauncherConfig, pid: int) -> None:
    try:
        config.ensure_state_dirs()
        config.pid_file.write_text(str(pid), encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not write pid file %s: %s", config.pid_file, exc)


def read_pid_file(config: LauncherConfig) -> int | None:
    try:
        return int(config.pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def clear_pid_file(config: LauncherConfig) -> None:
    try:
        config.pid_file.unlink(missing_ok=True)
    except OSError:
        pass


def process_exists(pid: int) -> bool:
    """Best-effort liveness check for a pid we do not own."""

    if pid <= 0:
        return False

    if IS_WINDOWS:
        try:
            completed = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
            )
            return str(pid) in (completed.stdout or "")
        except OSError:
            return False

    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def stop_running_instance(config: LauncherConfig, timeout: float = GRACEFUL_SHUTDOWN_SECONDS) -> bool:
    """
    Stop a server started by a previous launcher process.

    Returns True if a process was found and terminated.
    """

    pid = read_pid_file(config)
    if pid is None or not process_exists(pid):
        clear_pid_file(config)
        logger.info("No running CBT instance found.")
        return False

    logger.info("Stopping CBT (pid %s) ...", pid)

    try:
        if IS_WINDOWS:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
            )
        else:
            os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        logger.error("Failed to stop pid %s: %s", pid, exc)
        return False

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_exists(pid):
            break
        time.sleep(0.2)
    else:
        if not IS_WINDOWS:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    clear_pid_file(config)
    logger.info("Stopped.")
    return True


def install_signal_handlers(host: HostProcess) -> None:
    """Ensure Ctrl+C / SIGTERM tears the child down instead of orphaning it."""

    def _handler(signum, _frame):  # pragma: no cover - signal path
        logger.info("Received signal %s; shutting down.", signum)
        host.stop()
        sys.exit(0)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            # Not on the main thread, or unsupported on this platform.
            pass


__all__ = [
    "HostProcess",
    "build_child_environment",
    "clear_pid_file",
    "install_signal_handlers",
    "process_exists",
    "read_log_tail",
    "read_pid_file",
    "stop_running_instance",
    "write_pid_file",
]
