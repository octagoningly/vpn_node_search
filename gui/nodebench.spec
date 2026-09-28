# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — NodeBench Desktop (native window + web UI).

Build:  uv run pyinstaller --noconfirm gui/nodebench.spec
Output: dist/NodeBench/NodeBench.exe
"""

from pathlib import Path

root = Path(SPECPATH).resolve().parent  # project root

a = Analysis(
    [str(root / "gui" / "desktop.py")],
    pathex=[str(root), str(root / "gui")],
    binaries=[],
    datas=[
        (str(root / "gui" / "web"), "gui/web"),
    ],
    hiddenimports=[
        "yaml",
        "nodebench",
        "server",
        "webview",
        "pystray",
        "PIL",
        "PIL.Image",
        "PIL.ImageDraw",
    ],
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
    console=False,
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
