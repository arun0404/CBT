"""
Virtual environment provisioning.

This module owns requirement #3 — subsequent launches must not pay for
``venv`` creation or a pip resolve. The naive guard (``if not exist venv``)
is not sufficient for a shipped product: it silently keeps a stale
environment after an application update changes ``requirements.txt``, and
the resulting ``ImportError`` surfaces to the user as "the app stopped
working".

Instead the build is gated on a *fingerprint* covering everything that
could invalidate the environment:

    - the exact bytes of requirements.txt
    - the inventory (name + size) of the offline wheelhouse
    - the interpreter used to create the venv
    - the venv location and the launcher's own env-schema version

The fingerprint is written to ``venv/.cbt-env.json`` only after a
fully successful install, so an interrupted or failed build never leaves
a stamp behind that would cause the next launch to skip repair.

Steady-state cost of ``ensure_environment`` is one file read, one stat
per wheel, and one small JSON parse — well under 50 ms.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .config import LauncherConfig
from .errors import EnvironmentBuildError
from .logging_setup import get_logger

logger = get_logger("environment")

# Bump when the *shape* of the environment changes (e.g. a new pip flag
# that alters what gets installed), forcing a rebuild on next launch
# even though requirements.txt itself is unchanged.
ENV_SCHEMA_VERSION = 1

# Failed installs of torch wheels produce very long output; only the tail
# is echoed into the launcher log (the full text always goes to pip-install.log).
LOG_TAIL_LINES = 40


@dataclass(frozen=True)
class EnvironmentStatus:
    """Result of :func:`ensure_environment`, useful for --status and metrics."""

    python_exe: Path
    rebuilt: bool
    duration_seconds: float


def _iter_wheel_inventory(wheelhouse: Path) -> list[tuple[str, int]]:
    """Sorted (name, size) pairs for every distribution in the wheelhouse."""

    if not wheelhouse.is_dir():
        return []

    inventory: list[tuple[str, int]] = []
    for entry in sorted(wheelhouse.iterdir(), key=lambda p: p.name):
        if entry.suffix.lower() in (".whl", ".gz", ".zip") and entry.is_file():
            try:
                inventory.append((entry.name, entry.stat().st_size))
            except OSError:
                inventory.append((entry.name, -1))
    return inventory


def compute_fingerprint(config: LauncherConfig) -> str:
    """Stable hash of every input that determines the venv's contents."""

    digest = hashlib.sha256()
    digest.update(f"schema:{ENV_SCHEMA_VERSION}\n".encode("utf-8"))
    digest.update(f"python:{config.bootstrap_python}\n".encode("utf-8"))
    digest.update(f"venv:{config.venv_dir}\n".encode("utf-8"))

    try:
        digest.update(config.requirements_file.read_bytes())
    except OSError as exc:
        raise EnvironmentBuildError(
            f"Cannot read {config.requirements_file}: {exc}",
            hint="The application directory looks incomplete.",
        ) from exc

    for name, size in _iter_wheel_inventory(config.wheelhouse_dir):
        digest.update(f"{name}:{size}\n".encode("utf-8"))

    return digest.hexdigest()


def _read_stamp(stamp_file: Path) -> dict | None:
    try:
        with stamp_file.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _write_stamp(stamp_file: Path, fingerprint: str, config: LauncherConfig) -> None:
    payload = {
        "fingerprint": fingerprint,
        "schema": ENV_SCHEMA_VERSION,
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "bootstrap_python": str(config.bootstrap_python),
        "requirements": str(config.requirements_file),
    }
    tmp = stamp_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, stamp_file)


def environment_is_current(config: LauncherConfig) -> bool:
    """True when the venv exists and matches the current fingerprint."""

    if not config.venv_python.is_file():
        return False

    stamp = _read_stamp(config.env_stamp_file)
    if stamp is None:
        return False

    return stamp.get("fingerprint") == compute_fingerprint(config)


def _tail(text: str, lines: int = LOG_TAIL_LINES) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def _run_step(
    command: list[str],
    *,
    description: str,
    cwd: Path | None = None,
    log_file: Path | None = None,
) -> None:
    """Run a subprocess, capture all output, raise a typed error on failure."""

    logger.info("%s ...", description)
    logger.debug("Command: %s", " ".join(command))

    creationflags = 0
    if os.name == "nt":
        # Prevents a console window flashing up when the launcher itself
        # is running windowless.
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
            check=False,
        )
    except OSError as exc:
        raise EnvironmentBuildError(
            f"{description} could not be started: {exc}",
            hint=f"Verify that {command[0]} exists and is executable.",
        ) from exc

    output = (completed.stdout or "") + (completed.stderr or "")

    if log_file is not None:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_file.write_text(output, encoding="utf-8", errors="replace")
        except OSError:
            logger.warning("Could not write %s", log_file)

    if completed.returncode != 0:
        logger.error("%s failed (exit %s):\n%s", description, completed.returncode, _tail(output))
        raise EnvironmentBuildError(
            f"{description} failed with exit code {completed.returncode}.",
            hint=(
                f"See {log_file} for the full output."
                if log_file
                else "Re-run with --verbose for details."
            ),
        )

    logger.debug("%s output:\n%s", description, _tail(output, 15))


def _remove_venv(venv_dir: Path) -> None:
    if not venv_dir.exists():
        return
    logger.info("Removing previous environment at %s", venv_dir)
    shutil.rmtree(venv_dir, ignore_errors=True)
    if venv_dir.exists():
        raise EnvironmentBuildError(
            f"Could not remove the existing environment at {venv_dir}.",
            hint="Close any running CBT process and try again.",
        )


def _create_venv(config: LauncherConfig) -> None:
    _run_step(
        [str(config.bootstrap_python), "-m", "venv", str(config.venv_dir)],
        description="Creating virtual environment",
    )

    if not config.venv_python.is_file():
        raise EnvironmentBuildError(
            f"Virtual environment created but {config.venv_python} is missing.",
            hint="The bundled interpreter may be an embeddable build without venv support.",
        )


def _install_requirements(config: LauncherConfig) -> None:
    if not config.wheelhouse_dir.is_dir():
        raise EnvironmentBuildError(
            f"Offline wheelhouse not found at {config.wheelhouse_dir}.",
            hint="This build is air-gapped and cannot download packages.",
        )

    _run_step(
        [
            str(config.venv_python),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-input",
            "--disable-pip-version-check",
            f"--find-links={config.wheelhouse_dir}",
            "-r",
            str(config.requirements_file),
        ],
        description="Installing dependencies from the local wheelhouse",
        cwd=config.native_host_dir,
        log_file=config.pip_log,
    )


def ensure_environment(
    config: LauncherConfig,
    *,
    force_rebuild: bool = False,
) -> EnvironmentStatus:
    """
    Guarantee a venv matching the current requirements, building only if needed.

    Returns immediately (no subprocesses) on the common path where the
    environment is already current — this is what makes repeat launches
    fast.
    """

    started = time.monotonic()

    if not force_rebuild and environment_is_current(config):
        logger.info("Environment is up to date: %s", config.venv_dir)
        return EnvironmentStatus(
            python_exe=config.venv_python,
            rebuilt=False,
            duration_seconds=time.monotonic() - started,
        )

    reason = "rebuild requested" if force_rebuild else "missing or out of date"
    logger.info("Building environment (%s). First run can take several minutes.", reason)

    config.venv_root.mkdir(parents=True, exist_ok=True)

    # A stale stamp must never survive a rebuild attempt.
    config.env_stamp_file.unlink(missing_ok=True)

    if force_rebuild or not config.venv_python.is_file():
        _remove_venv(config.venv_dir)
        _create_venv(config)

    _install_requirements(config)
    _write_stamp(config.env_stamp_file, compute_fingerprint(config), config)

    duration = time.monotonic() - started
    logger.info("Environment ready in %.1fs", duration)

    return EnvironmentStatus(
        python_exe=config.venv_python,
        rebuilt=True,
        duration_seconds=duration,
    )


__all__ = [
    "EnvironmentStatus",
    "compute_fingerprint",
    "ensure_environment",
    "environment_is_current",
]
