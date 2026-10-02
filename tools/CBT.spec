# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for the CBT launcher stub  (Option B).

IMPORTANT - what this does and does not freeze
----------------------------------------------
This freezes ONLY the launcher (bootstrap.py + the launcher package),
producing an ~8-12 MB executable. It does NOT freeze the application
itself. That is a deliberate architectural decision:

  * torch + torchaudio in a onefile bundle is 2+ GB, takes minutes to
    build, and unpacks to a temp directory on every launch - which
    would destroy exactly the 2-3 second start-up this work is for.
  * torchaudio loads native extensions and resolves backends
    dynamically; hidden-import and binary-collection issues there are a
    recurring maintenance cost.
  * piper.exe, espeak-ng-data and the .onnx voices are external data
    files that must stay on disk anyway.
  * Large self-extracting executables are a well-known trigger for
    antivirus heuristics; a small, signable stub is far less likely to
    be quarantined on a locked-down corporate desktop.

The stub is pure standard library, so the bundle is small, builds in
seconds, and has no hidden-import surface beyond the launcher package.

Build from the package root:
    pyinstaller tools/CBT.spec --noconfirm --clean

Output:
    dist/CBT.exe   ->  copy to the package root, beside CBT/
"""

import os
from pathlib import Path

# SPECPATH is injected by PyInstaller; fall back for editors/linters.
_spec_dir = Path(globals().get("SPECPATH", os.getcwd())).resolve()
PACKAGE_ROOT = _spec_dir.parent
NATIVE_HOST = PACKAGE_ROOT / "CBT" / "native_host"
ICON = _spec_dir / "CBT.ico"

block_cipher = None


a = Analysis(
    [str(NATIVE_HOST / "bootstrap.py")],
    pathex=[str(NATIVE_HOST)],
    binaries=[],
    datas=[],
    # bootstrap.py imports the package only after adjusting sys.path, so
    # the dependency graph cannot be discovered statically. Every module
    # is listed explicitly rather than relying on the hook system.
    hiddenimports=[
        "launcher",
        "launcher.__main__",
        "launcher.browser",
        "launcher.config",
        "launcher.environment",
        "launcher.errors",
        "launcher.logging_setup",
        "launcher.readiness",
        "launcher.server",
        "launcher.torch_assets",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Trim modules the launcher provably never uses. Keeps the stub small
    # and shortens the onefile unpack on every launch.
    excludes=[
        "tkinter",
        "unittest",
        "pydoc",
        "doctest",
        "test",
        "sqlite3",
        "xml",
        "html",
        "pdb",
        "difflib",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="CBT",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX compression is another common antivirus trigger.
    upx_exclude=[],
    runtime_tmpdir=None,
    # No console: this is the double-click experience. Diagnostics go to
    # the rotating log files, and `CBT.exe --status` still works
    # from a terminal.
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON) if ICON.is_file() else None,
    version=None,
)
