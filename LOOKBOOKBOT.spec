# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


a = Analysis(
    ['launcher.py'],
    pathex=['src'],
    binaries=[],
    datas=[('src\\assets\\lbb-logo.png', 'assets'), ('src\\assets\\lookbookbot.ico', 'assets'), ('automation-engine\\lookbook-layout\\scripts', 'automation-engine\\core\\scripts')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

# Do not ship copies of native runtimes extracted from unrelated dependencies.
# On Windows the Qt 6 binaries must use the system UCRT and ICU.  The Poppler
# ICU build exports version-suffixed symbols and is incompatible with the ICU
# ABI expected by Qt6Core, causing WinError 127 during application startup.
_CONFLICTING_NATIVE_DLLS = {
    "ucrtbase.dll",
    "icuuc.dll",
    "icudt78.dll",
}
a.binaries = [
    entry
    for entry in a.binaries
    if Path(str(entry[0])).name.casefold() not in _CONFLICTING_NATIVE_DLLS
]

# Engine scripts run in the external Python runtime. Ship their source, not
# cached bytecode from whichever interpreter last ran a development test.
a.datas = [
    entry for entry in a.datas
    if "__pycache__" not in Path(str(entry[0])).parts
    and not str(entry[0]).lower().endswith((".pyc", ".pyo"))
]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='LOOKBOOKBOT',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['src\\assets\\lookbookbot.ico'],
)
