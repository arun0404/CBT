"""
Browser launch.

Two modes:

  * **default** — hands the URL to the OS default browser. Predictable,
    respects user choice, works everywhere.

  * **app window** — launches Edge or Chrome with ``--app=<url>``, which
    opens a chromeless window with no address bar or tab strip. This is
    what makes a local Flask app read as a desktop application rather
    than "a website that happens to be on localhost", and it is the main
    reason ``--app-window`` exists as an option. Falls back to the
    default browser if no Chromium-based browser is found, so it can
    never be a hard failure.

Browser launch is never fatal: if it fails, the server is still running
and the URL is logged, so the user can paste it manually.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import webbrowser
from pathlib import Path

from .config import IS_WINDOWS
from .logging_setup import get_logger

logger = get_logger("browser")


def _chromium_candidates() -> list[Path]:
    """Known Edge/Chrome locations, most-likely-installed first."""

    candidates: list[Path] = []

    if IS_WINDOWS:
        program_dirs = [
            os.environ.get("PROGRAMFILES", r"C:\Program Files"),
            os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
            os.environ.get("LOCALAPPDATA", ""),
        ]
        relative = [
            r"Microsoft\Edge\Application\msedge.exe",
            r"Google\Chrome\Application\chrome.exe",
        ]
        for base in filter(None, program_dirs):
            for rel in relative:
                candidates.append(Path(base) / rel)
    else:
        for name in ("microsoft-edge", "google-chrome", "chromium", "chromium-browser"):
            found = shutil.which(name)
            if found:
                candidates.append(Path(found))
        candidates.append(
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        )

    return candidates


def _open_app_window(url: str, profile_dir: Path | None = None) -> bool:
    """Try to open a chromeless window. Returns False if no browser was found."""

    for browser in _chromium_candidates():
        if not browser.is_file():
            continue

        command = [str(browser), f"--app={url}"]
        if profile_dir is not None:
            # A dedicated profile keeps the app window out of the user's
            # normal session (no shared cookies, no "restore tabs" prompt).
            command.append(f"--user-data-dir={profile_dir}")

        try:
            subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0,
                close_fds=True,
            )
            logger.info("Opened application window using %s", browser.name)
            return True
        except OSError as exc:
            logger.debug("Could not launch %s: %s", browser, exc)

    return False


def _open_default(url: str) -> bool:
    try:
        if webbrowser.open_new_tab(url):
            logger.info("Opened %s in the default browser.", url)
            return True
    except Exception as exc:  # webbrowser raises a variety of platform errors
        logger.debug("webbrowser.open_new_tab failed: %s", exc)

    if IS_WINDOWS:
        try:
            os.startfile(url)  # type: ignore[attr-defined]
            logger.info("Opened %s via the shell.", url)
            return True
        except OSError as exc:
            logger.debug("os.startfile failed: %s", exc)

    return False


def open_browser(url: str, *, app_window: bool = False, profile_dir: Path | None = None) -> bool:
    """
    Open ``url``. Returns True on success; logs and returns False otherwise.

    Never raises — a browser that will not start must not take down a
    server that is already running correctly.
    """

    if app_window and _open_app_window(url, profile_dir):
        return True

    if _open_default(url):
        return True

    logger.warning("Could not open a browser automatically. Open %s manually.", url)
    return False


__all__ = ["open_browser"]
