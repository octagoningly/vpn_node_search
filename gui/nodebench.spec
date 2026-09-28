# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for NodeBench Desktop launcher.

Build:  pyinstaller gui/nodebench.spec
Output: dist/NodeBench/NodeBench.exe
"""

from pathlib import Path

root = Path(SPECPATH).resolve().parent  # project root

a = Analysis(
    [str(root / "gui" / "app.py")],
    pathex=[str(root)],
    binaries=[],
    datas=[],
    hiddenimports=["yaml", "nodebench"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="NodeBench",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # windowed app, no console
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="NodeBench",
)
