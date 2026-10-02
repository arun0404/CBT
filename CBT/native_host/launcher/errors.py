"""
Typed exception hierarchy for the launcher.

Every failure mode the launcher can hit maps to exactly one exception
class, and every exception class maps to exactly one process exit code.
That matters more than usual here: in silent (VBS / windowed .exe) mode
there is no console, so the exit code plus the rotating log file are the
only diagnostics a support engineer gets. Keeping the mapping in one
place means `run.bat` and any future MSI/installer wrapper can branch on
a stable, documented contract instead of parsing log text.
"""

from __future__ import annotations


class LauncherError(Exception):
    """Base class for every launcher failure. Never raised directly."""

    exit_code = 1

    def __init__(self, message: str, *, hint: str | None = None):
        super().__init__(message)
        self.hint = hint

    def describe(self) -> str:
        """Human-readable message including the remediation hint, if any."""
        return f"{self}\n  Hint: {self.hint}" if self.hint else str(self)


class ConfigurationError(LauncherError):
    """The application layout on disk is not what the launcher expects."""

    exit_code = 2


class EnvironmentBuildError(LauncherError):
    """Virtual environment creation or offline dependency install failed."""

    exit_code = 3


class AssetProvisioningError(LauncherError):
    """A required model/data asset could not be placed where torch expects it."""

    exit_code = 4


class ServerStartError(LauncherError):
    """The host process could not be spawned at all."""

    exit_code = 5


class ServerExitedError(LauncherError):
    """The host process died before it began accepting HTTP requests."""

    exit_code = 6


class ReadinessTimeoutError(LauncherError):
    """The host process is alive but never became ready within the deadline."""

    exit_code = 7
