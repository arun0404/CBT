"""
Cooperative cancellation for in-flight speech generation.

Why this exists
---------------
Aborting a `fetch()` in the browser (see js/requestcoordinator.js) tears
down the CLIENT side of a request immediately, but it does not stop the
server: Flask's /speak handler keeps running to completion, shelling out
to piper.exe and then doing a full forced-alignment forward pass, for a
page the user has already navigated away from. Worse, every /speak also
spawns a background thread generating the OTHER voice
(speech_pipeline._prefetch_secondary), so navigating quickly across three
uncached pages leaves SIX synthesis+alignment jobs competing for the same
CPU — which is exactly what makes the page the user actually landed on
take so long.

Detecting the client disconnect itself is not viable here: under WSGI a
dropped connection is generally not observable until the handler tries to
write a response, which for this workload is long after all the expensive
work has finished. So cancellation is driven EXPLICITLY by the client
instead: every /speak carries a monotonically increasing `nav_epoch`, and
the arrival of a higher epoch is what cancels the work belonging to lower
ones. The client already has exactly this notion of "newer supersedes
older" in RequestCoordinator's monotonic token; `nav_epoch` is that same
idea carried across the wire.

Cancellation is COOPERATIVE, with one forceful exception:

  - Cooperative: generation checks `token.raise_if_cancelled()` at stage
    boundaries (before synthesis, before alignment, before writing to
    cache), so an abandoned job stops at the next checkpoint instead of
    running to completion.

  - Forceful: piper.exe is a subprocess that can block for seconds with
    no checkpoints of its own, so a running Piper process is registered
    on the token and genuinely killed on cancel. That is the difference
    between freeing the CPU now and freeing it whenever Piper happens to
    finish.

Thread-safety: tokens are created on request threads, cancelled from
OTHER request threads (a newer request cancelling an older one), and
observed from background prefetch threads, so all mutable state is
guarded by a lock.
"""

from __future__ import annotations

import logging
import subprocess
import threading
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class GenerationCancelled(Exception):
    """
    Raised inside a generation pipeline when its token has been
    cancelled. Callers treat this as an expected, non-error outcome —
    the work was deliberately abandoned, not broken.
    """


class CancellationToken:
    """
    One cancellable unit of generation work.

    A single token is shared by a /speak request AND the background
    prefetch of its other voice, so navigating away cancels both halves
    of that page's work together rather than leaving the background
    thread running.
    """

    def __init__(self, epoch: int, label: str = ""):

        self.epoch = epoch
        self.label = label

        self._lock = threading.Lock()
        self._cancelled = False

        # The Piper subprocess currently running under this token, if
        # any. Registered by piper.py for the duration of one synthesis
        # call so cancel() can kill it mid-run.
        self._process: Optional[subprocess.Popen] = None

    # ------------------------------------------------------------

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def raise_if_cancelled(self) -> None:
        """Checkpoint. Call at stage boundaries in a generation pipeline."""
        if self.cancelled:
            raise GenerationCancelled(
                f"Generation cancelled (epoch={self.epoch}, {self.label})."
            )

    # ------------------------------------------------------------

    def cancel(self) -> bool:
        """
        Mark cancelled and kill any registered subprocess.

        Idempotent and safe to call from any thread. Returns True only
        for the call that actually performed the cancellation, so a
        caller can log it exactly once.
        """

        with self._lock:

            if self._cancelled:
                return False

            self._cancelled = True
            process = self._process

        if process is not None and process.poll() is None:
            # Kill rather than terminate: piper.exe has no cleanup we
            # care about (its only output is a scratch WAV the caller
            # deletes anyway), and a graceful signal on Windows is not
            # meaningfully gentler for a console process like this.
            try:
                process.kill()
                logger.info(
                    "Killed in-flight Piper process for cancelled generation "
                    "(epoch=%s, %s).", self.epoch, self.label
                )
            except OSError as exc:
                # Already exited between poll() and kill() — harmless.
                logger.debug("Could not kill Piper process: %s", exc)

        return True

    # ------------------------------------------------------------

    def register_process(self, process: subprocess.Popen) -> None:
        """
        Attach a running subprocess so cancel() can kill it.

        If the token was ALREADY cancelled before the process started,
        the process is killed immediately — this closes the race where
        cancellation lands between the caller's last checkpoint and the
        subprocess actually launching.
        """

        with self._lock:
            self._process = process
            already_cancelled = self._cancelled

        if already_cancelled and process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass

    def clear_process(self) -> None:
        with self._lock:
            self._process = None


class _NullToken(CancellationToken):
    """
    A token that is never cancelled, used when a caller supplies none.

    Lets every generation path take a token unconditionally instead of
    threading `if token is not None` checks through the pipeline.
    """

    def __init__(self):
        super().__init__(epoch=-1, label="null")

    def cancel(self) -> bool:  # pragma: no cover - never meaningfully called
        return False

    @property
    def cancelled(self) -> bool:
        return False


NULL_TOKEN = _NullToken()


class CancellationRegistry:
    """
    Tracks live tokens so a newer navigation can cancel older ones.

    Keyed by epoch. Entries are removed when their generation finishes,
    so this stays small (one entry per genuinely in-flight page).
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._tokens: Dict[int, CancellationToken] = {}

    def begin(self, epoch: int, label: str = "") -> CancellationToken:
        """
        Register a token for `epoch` and cancel every live token from an
        EARLIER epoch — the "a newer navigation supersedes older work"
        rule this whole module exists for.
        """

        token = CancellationToken(epoch=epoch, label=label)

        with self._lock:
            superseded = [
                existing for existing_epoch, existing in self._tokens.items()
                if existing_epoch < epoch
            ]
            self._tokens[epoch] = token

        for stale in superseded:
            if stale.cancel():
                logger.info(
                    "Cancelling superseded generation (epoch=%s, %s) — "
                    "newer request at epoch %s.",
                    stale.epoch, stale.label, epoch
                )

        return token

    def end(self, token: CancellationToken) -> None:
        """Drop a finished token, unless a newer one already replaced it."""

        with self._lock:
            if self._tokens.get(token.epoch) is token:
                self._tokens.pop(token.epoch, None)

    def cancel_all(self) -> None:
        with self._lock:
            tokens = list(self._tokens.values())
            self._tokens.clear()

        for token in tokens:
            token.cancel()


__all__ = [
    "CancellationRegistry",
    "CancellationToken",
    "GenerationCancelled",
    "NULL_TOKEN",
]
