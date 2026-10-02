"""
Provisioning of the bundled torch checkpoint.

The original batch script copied ``piper/model/model.pt`` into
``%USERPROFILE%\\.cache\\torch\\hub\\checkpoints`` on first run. That works,
but it has three properties you do not want in a shipped product:

    1. It writes into a shared, global cache that other torch
       applications also use, so an unrelated ``torch.hub`` call can
       evict or overwrite the file.
    2. ``if not exist`` only checks presence, so a truncated or partially
       copied file is treated as valid forever.
    3. Uninstalling the app leaves the file behind.

Torch resolves its download cache from the ``TORCH_HOME`` environment
variable before falling back to the user cache, so the launcher instead
points ``TORCH_HOME`` at an application-owned directory and provisions
the checkpoint there. Nothing outside the app is touched, and the whole
thing stays air-gapped.

Two candidate locations are tried in order, because the application
directory is not writable when installed under ``C:\\Program Files``:

    <app_root>/piper/torch_home      preferred (self-contained, portable)
    <state_dir>/torch_home           fallback  (per-user, always writable)
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from .config import LauncherConfig
from .errors import AssetProvisioningError
from .logging_setup import get_logger

logger = get_logger("torch_assets")

CHECKPOINT_SUBPATH = Path("hub") / "checkpoints"


def _checkpoint_is_valid(destination: Path, source: Path) -> bool:
    """
    Presence *and* size match.

    Size is a cheap proxy for integrity that catches the realistic
    failure mode — a copy interrupted by a crash, a full disk, or the
    user closing the console during first run. A full hash would add
    seconds to every launch for a multi-hundred-megabyte checkpoint,
    which is exactly the cost this design is trying to avoid.
    """

    try:
        return destination.is_file() and destination.stat().st_size == source.stat().st_size
    except OSError:
        return False


def _provision_into(torch_home: Path, source: Path) -> Path:
    """Copy the checkpoint into ``torch_home`` atomically. Raises OSError on failure."""

    checkpoint_dir = torch_home / CHECKPOINT_SUBPATH
    checkpoint_dir.mkdir(parents=True, exist_ok=True)

    destination = checkpoint_dir / source.name

    if _checkpoint_is_valid(destination, source):
        logger.debug("Checkpoint already provisioned at %s", destination)
        return torch_home

    logger.info("Provisioning voice model into %s", checkpoint_dir)

    # Copy to a temporary name first, then rename. os.replace is atomic
    # on the same filesystem, so a crash mid-copy can never leave a
    # half-written file that passes the presence check next launch.
    temp_destination = destination.with_suffix(destination.suffix + ".partial")
    try:
        shutil.copyfile(source, temp_destination)
        os.replace(temp_destination, destination)
    finally:
        if temp_destination.exists():
            temp_destination.unlink(missing_ok=True)

    if not _checkpoint_is_valid(destination, source):
        raise OSError(f"Checkpoint at {destination} failed validation after copy.")

    logger.info("Voice model initialised.")
    return torch_home


def prepare_torch_home(config: LauncherConfig) -> Path:
    """
    Ensure a TORCH_HOME directory containing the bundled checkpoint.

    Returns the directory to export as ``TORCH_HOME`` to the host
    process. Raises :class:`AssetProvisioningError` only if *every*
    candidate location fails.
    """

    source = config.bundled_model_file

    if not source.is_file():
        raise AssetProvisioningError(
            f"Bundled voice model not found at {source}.",
            hint="The application package is incomplete; reinstall it.",
        )

    candidates = [
        config.app_root / "piper" / "torch_home",
        config.state_dir / "torch_home",
    ]

    failures: list[str] = []
    for candidate in candidates:
        try:
            return _provision_into(candidate, source)
        except OSError as exc:
            logger.debug("Cannot use %s as TORCH_HOME: %s", candidate, exc)
            failures.append(f"{candidate}: {exc}")

    raise AssetProvisioningError(
        "Could not provision the voice model into any writable location.\n  "
        + "\n  ".join(failures),
        hint="Check free disk space and folder permissions.",
    )


__all__ = ["prepare_torch_home"]
