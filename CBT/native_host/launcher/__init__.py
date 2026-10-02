"""
CBT launcher.

A dependency-free (standard library only) orchestrator that turns the
application package into a double-clickable desktop app:

    ensure venv -> provision model -> start host -> poll readiness ->
    open browser -> supervise -> clean shutdown

Being stdlib-only is a design constraint, not an accident: the launcher
runs on the *bundled* interpreter before any virtual environment exists,
and its diagnostic commands (``--status``, ``--stop``) must keep working
even when that environment is broken.

Entry points:
    python -m launcher            (from native_host/)
    python bootstrap.py           (from anywhere; used by run.bat)
"""

from __future__ import annotations

__version__ = "1.0.0"
__all__ = ["__version__"]
