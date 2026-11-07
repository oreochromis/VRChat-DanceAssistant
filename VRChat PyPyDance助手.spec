# -*- mode: python ; coding: utf-8 -*-
# Compatibility build entry. Prefer build_windows.ps1 to also collect notices.
# User configuration and cached videos are not bundled; approved built-in theme backgrounds are.

from pathlib import Path

project_root = Path(SPECPATH)

a = Analysis(
    [str(project_root / 'dance_cache_gui.py')],
    pathex=[str(project_root)],
    binaries=[],
    datas=[
        (str(project_root / 'README.md'), '.'),
        (str(project_root / 'LICENSE'), '.'),
        (str(project_root / 'assets' / 'default_cooldown.mp4'), 'assets'),
        (str(project_root / 'assets' / 'app-icon.png'), 'assets'),
        (str(project_root / 'assets' / 'app-icon.ico'), 'assets'),
        (str(project_root / 'assets' / 'backgrounds'), 'assets/backgrounds'),
    ],
    hiddenimports=[],
    hookspath=[str(project_root / 'tools' / 'pyinstaller_hooks')],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='PyPyDanceCache',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    uac_admin=True,
    icon=str(project_root / 'assets' / 'app-icon.ico'),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='PyPyDanceCache',
)
