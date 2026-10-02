"""
Entry point for the CBT launcher.

Kept deliberately tiny and standard-library-only: this file is executed
by the *bundled* Python 3.10 interpreter before any virtual environment
exists, and it is also the PyInstaller entry script. All real logic lives
in the ``launcher`` package.

Usage:
    Python310\\python.exe  bootstrap.py [options]     (visible console)
    Python310\\pythonw.exe bootstrap.py [options]     (silent)
    CBT.exe [options]                          (frozen build)

Run ``bootstrap.py --help`` for the full option list.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _ensure_launcher_importable() -> None:
    """
    Make ``launcher`` importable regardless of the current directory.

    In a frozen build the package is already inside the bundle, so the
    import succeeds without touching sys.path. In the source layout the
    package sits next to this file.
    """

    here = Path(__file__).resolve().parent
    if str(here) not in sys.path:
        # Appended rather than inserted at position 0 so this directory
        # cannot shadow standard library modules; the launcher package
        # uses relative imports internally, so it never picks up the
        # application's own config.py by accident.
        sys.path.append(str(here))


def main() -> int:
    _ensure_launcher_importable()

    try:
        from launcher.__main__ import main as launcher_main
    except ImportError as exc:  # pragma: no cover - packaging failure
        message = (
            f"CBT launcher package could not be imported: {exc}\n"
            "The installation appears to be incomplete."
        )
        if sys.stderr is not None:
            print(message, file=sys.stderr)
        return 2

    return launcher_main(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
