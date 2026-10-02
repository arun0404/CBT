"""
Logging configuration for the launcher.

The important constraint here is that in silent mode the launcher runs
under ``pythonw.exe``, where ``sys.stdout`` and ``sys.stderr`` are
``None`` — not merely redirected. Attaching a StreamHandler
unconditionally produces a ``ValueError`` (or a swallowed logging error)
the first time anything is logged, which is exactly when you most need
the log. The console handler is therefore attached only when a real
stream exists, and the rotating file handler is always attached.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)-8s [%(name)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

MAX_LOG_BYTES = 2 * 1024 * 1024
BACKUP_COUNT = 3


def configure_logging(log_file: Path, verbose: bool = False) -> logging.Logger:
    """
    Install handlers on the ``launcher`` logger and return it.

    Idempotent: calling twice does not duplicate handlers, which keeps
    the function safe to call from both ``bootstrap.py`` and
    ``launcher.__main__``.
    """

    logger = logging.getLogger("launcher")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False

    if logger.handlers:
        return logger

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=MAX_LOG_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError:
        # A read-only or missing state directory must not be fatal; the
        # console handler below (when present) still gives feedback.
        pass

    stream = sys.stderr if sys.stderr is not None else sys.stdout
    if stream is not None:
        console = logging.StreamHandler(stream)
        console.setLevel(logging.DEBUG if verbose else logging.INFO)
        console.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(console)

    if not logger.handlers:
        logger.addHandler(logging.NullHandler())

    return logger


def get_logger(name: str) -> logging.Logger:
    """Child logger that inherits the handlers installed above."""
    return logging.getLogger(f"launcher.{name}")
