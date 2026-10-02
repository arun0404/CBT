"""
Readiness detection — requirement #1.

Replaces the fixed sleep with a poll of the host's HTTP endpoint so the
browser opens the moment Flask is accepting requests: fast machines stop
waiting after ~2 s, slow first runs still succeed instead of opening a
browser onto a connection-refused page.

Three details make this production-grade rather than a naive retry loop:

  * **Any HTTP status means ready.** A 404 or a 500 still proves the
    socket accepted the connection and the WSGI application dispatched.
    Only transport-level errors count as "not up yet".

  * **Liveness is checked every iteration.** If the host process dies
    during startup (a bad venv, a port clash, an import error), the loop
    aborts immediately with the log tail instead of blocking the user
    for the full timeout.

  * **Endpoint fallback.** ``/healthz`` is preferred, but the loop falls
    back to ``/`` so the launcher also works against an unpatched
    host.py.

Uses only ``urllib``/``socket`` from the standard library, so it runs on
the bundled interpreter with nothing installed.
"""

from __future__ import annotations

import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

from .errors import ReadinessTimeoutError, ServerExitedError
from .logging_setup import get_logger

logger = get_logger("readiness")

PROBE_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True)
class ReadinessResult:
    url: str
    status: int
    elapsed_seconds: float


def is_port_listening(host: str, port: int, timeout: float = 0.5) -> bool:
    """
    True when *something* holds the TCP port.

    Used as the single-instance check: if the port is already served, a
    second double-click should surface the existing app rather than
    fight over the socket.
    """

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def probe_http(url: str, timeout: float = PROBE_TIMEOUT_SECONDS) -> int | None:
    """
    Single readiness probe.

    Returns the HTTP status code if the server responded at all, or
    ``None`` if the connection could not be established.
    """

    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "CBT-Launcher"})

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        # An HTTP error IS a response: the server is up and dispatching.
        # HTTPError subclasses URLError, so this must be caught first.
        return exc.code
    except (urllib.error.URLError, socket.timeout, ConnectionError, OSError):
        return None


def wait_until_ready(
    primary_url: str,
    *,
    fallback_url: str | None = None,
    timeout: float,
    interval: float,
    is_alive: Callable[[], bool] | None = None,
    on_process_death: Callable[[], str] | None = None,
) -> ReadinessResult:
    """
    Block until the server answers, then return immediately.

    :param is_alive: optional liveness callback for the host process.
    :param on_process_death: optional callback returning diagnostic text
        (typically the tail of the server log) used to build the error.
    :raises ServerExitedError: the host process died during startup.
    :raises ReadinessTimeoutError: the deadline elapsed while it was alive.
    """

    started = time.monotonic()
    deadline = started + timeout
    urls = [primary_url] + ([fallback_url] if fallback_url else [])
    attempts = 0
    next_progress_log = started + 10.0

    logger.info("Waiting for %s to accept requests ...", primary_url)

    while True:
        if is_alive is not None and not is_alive():
            details = on_process_death() if on_process_death else ""
            raise ServerExitedError(
                "The CBT host process exited during startup."
                + (f"\n--- server log tail ---\n{details}" if details else ""),
                hint=(
                    "If this mentions ModuleNotFoundError, rebuild the "
                    "environment with:  run.bat --rebuild-env"
                ),
            )

        for url in urls:
            status = probe_http(url)
            attempts += 1
            if status is not None:
                elapsed = time.monotonic() - started
                logger.info("Server ready after %.2fs (HTTP %s from %s)", elapsed, status, url)
                return ReadinessResult(url=url, status=status, elapsed_seconds=elapsed)

        now = time.monotonic()
        if now >= deadline:
            raise ReadinessTimeoutError(
                f"Server did not become ready within {timeout:.0f}s "
                f"({attempts} probes to {primary_url}).",
                hint="Check the server log for a stack trace, then retry.",
            )

        if now >= next_progress_log:
            logger.info("Still starting up (%.0fs elapsed) ...", now - started)
            next_progress_log = now + 15.0

        time.sleep(min(interval, max(0.0, deadline - now)))


__all__ = ["ReadinessResult", "is_port_listening", "probe_http", "wait_until_ready"]
