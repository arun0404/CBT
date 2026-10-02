"""
Launcher entry point and CLI.

Startup sequence (each step is a single call into a focused module):

    1. resolve configuration          config.resolve_config
    2. configure logging              logging_setup.configure_logging
    3. single-instance check          readiness.is_port_listening
    4. ensure virtual environment     environment.ensure_environment
    5. provision the torch checkpoint torch_assets.prepare_torch_home
    6. start the host process         server.HostProcess
    7. poll until it accepts requests readiness.wait_until_ready
    8. open the browser               browser.open_browser
    9. supervise until exit           server.HostProcess.wait

Exit codes come from the exception hierarchy in errors.py, so wrappers
(run.bat, the .exe, a future MSI) can branch on a stable contract:

    0  normal exit          4  asset provisioning failed
    1  unexpected error     5  host process could not start
    2  bad configuration    6  host process died during startup
    3  environment build    7  readiness timeout
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from .browser import open_browser
from .config import (
    APP_NAME,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_READY_TIMEOUT,
    LauncherConfig,
    resolve_config,
)
from .environment import ensure_environment, environment_is_current
from .errors import LauncherError
from .logging_setup import configure_logging, get_logger
from .readiness import is_port_listening, wait_until_ready
from .server import (
    HostProcess,
    install_signal_handlers,
    process_exists,
    read_pid_file,
    stop_running_instance,
)
from .torch_assets import prepare_torch_home

logger = get_logger("main")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description=f"Launch the {APP_NAME} offline text-to-speech application.",
    )

    parser.add_argument("--port", type=int, default=None, help="Port the host binds (default 5000).")
    parser.add_argument("--host", default=None, help="Interface the host binds (default 127.0.0.1).")
    parser.add_argument("--no-browser", action="store_true", help="Start the server without opening a browser.")
    parser.add_argument(
        "--app-window",
        action="store_true",
        help="Open a chromeless Edge/Chrome window instead of a normal browser tab.",
    )
    parser.add_argument(
        "--detach",
        action="store_true",
        help="Exit once the browser is open, leaving the server running. Stop it with --stop.",
    )
    parser.add_argument(
        "--show-console",
        action="store_true",
        help="Run the host with a visible console window (debugging).",
    )
    parser.add_argument("--rebuild-env", action="store_true", help="Delete and rebuild the virtual environment.")
    parser.add_argument("--setup-only", action="store_true", help="Build the environment and exit; do not start the server.")
    parser.add_argument("--stop", action="store_true", help="Stop a running instance and exit.")
    parser.add_argument("--status", action="store_true", help="Print the current status as JSON and exit.")
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_READY_TIMEOUT,
        help=f"Seconds to wait for readiness (default {DEFAULT_READY_TIMEOUT:.0f}).",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging.")

    return parser


def _print_status(config: LauncherConfig) -> int:
    pid = read_pid_file(config)
    status = {
        "app": APP_NAME,
        "url": config.base_url,
        "port_listening": is_port_listening(config.host, config.port),
        "pid": pid,
        "pid_alive": process_exists(pid) if pid else False,
        "environment_ready": environment_is_current(config),
        "venv": str(config.venv_dir),
        "native_host": str(config.native_host_dir),
        "launcher_log": str(config.launcher_log),
        "server_log": str(config.server_log),
    }
    print(json.dumps(status, indent=2))
    return 0


def _handle_existing_instance(config: LauncherConfig, args: argparse.Namespace) -> int:
    """A second launch should surface the running app, not fight for the port."""

    logger.info("%s is already running on %s.", APP_NAME, config.base_url)
    if not args.no_browser:
        open_browser(config.base_url, app_window=args.app_window, profile_dir=config.state_dir / "browser-profile")
    return 0


def run(args: argparse.Namespace) -> int:
    started = time.monotonic()

    config = resolve_config(port=args.port, host=args.host)
    config.ensure_state_dirs()
    configure_logging(config.launcher_log, verbose=args.verbose)

    logger.info("--- %s launcher starting (%s) ---", APP_NAME, config.native_host_dir)

    if args.status:
        return _print_status(config)

    if args.stop:
        stop_running_instance(config)
        return 0

    # ---- single instance --------------------------------------------
    if is_port_listening(config.host, config.port):
        return _handle_existing_instance(config, args)

    # ---- environment -------------------------------------------------
    env_status = ensure_environment(config, force_rebuild=args.rebuild_env)

    if args.setup_only:
        logger.info("Setup complete (rebuilt=%s).", env_status.rebuilt)
        return 0

    # ---- assets ------------------------------------------------------
    torch_home = prepare_torch_home(config)

    # ---- server ------------------------------------------------------
    host_process = HostProcess(config, torch_home, windowless=not args.show_console)
    host_process.start()
    install_signal_handlers(host_process)

    try:
        wait_until_ready(
            config.health_url,
            fallback_url=config.fallback_health_url,
            timeout=args.timeout,
            interval=DEFAULT_POLL_INTERVAL,
            is_alive=host_process.is_alive,
            on_process_death=host_process.log_tail,
        )
    except LauncherError:
        host_process.stop()
        raise

    logger.info("Total startup time: %.2fs", time.monotonic() - started)

    if not args.no_browser:
        open_browser(
            config.base_url,
            app_window=args.app_window,
            profile_dir=config.state_dir / "browser-profile",
        )

    if args.detach:
        logger.info("Detaching; server continues in the background (pid %s).", host_process.pid)
        logger.info("Stop it later with:  run.bat --stop")
        return 0

    logger.info("Supervising host process. Close this window or press Ctrl+C to stop.")
    exit_code = host_process.wait()
    host_process.stop()
    logger.info("Host process exited with code %s.", exit_code)
    return 0 if exit_code in (0, None) else 1


def _report(message: str, *, exc_info: bool = False) -> None:
    """
    Last-resort error reporting.

    ``run()`` may fail before logging is configured (for example when
    path resolution itself fails), and under pythonw there is no stderr
    to fall back to. Emitting through the logger *and* stderr when it
    exists covers both cases without ever raising from the error path.
    """

    log = get_logger("main")
    if exc_info:
        log.exception(message)
    else:
        log.error(message)

    if sys.stderr is not None and not log.handlers and not log.parent.handlers:
        print(message, file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        return run(args)
    except LauncherError as exc:
        _report(exc.describe())
        return exc.exit_code
    except KeyboardInterrupt:
        get_logger("main").info("Interrupted by user.")
        return 0
    except Exception:  # noqa: BLE001 - top-level guard for a GUI-less process
        _report("Unexpected launcher failure.", exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
