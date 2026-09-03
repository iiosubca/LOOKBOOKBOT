"""Windows entry point with an isolated Qt DLL search path for PyInstaller."""

from __future__ import annotations

import os
import sys
from pathlib import Path


# Keep the handles alive for the whole process.  PyInstaller's PySide6 runtime
# hook prepends the temporary extraction directory to PATH.  That directory
# also contains a bundled UCRT copy, which can be incompatible with the Qt
# binaries and causes WinError 127 while importing QtCore/QtGui.  The Qt
# libraries themselves live in the PySide6 subdirectory, so only those Qt
# directories need to be added to the DLL search path.
_BUNDLED_DLL_DIRECTORIES = []
if sys.platform == "win32" and getattr(sys, "frozen", False):
    _bundle_root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    for _dll_dir in (_bundle_root / "PySide6", _bundle_root / "shiboken6"):
        if _dll_dir.is_dir() and hasattr(os, "add_dll_directory"):
            _BUNDLED_DLL_DIRECTORIES.append(os.add_dll_directory(str(_dll_dir)))

    # Remove only the exact extraction directory inserted by PyInstaller's
    # runtime hook.  This keeps unrelated PATH entries intact while ensuring
    # dependencies such as ucrtbase.dll and ICU resolve from Windows rather
    # than from a foreign native package bundled by analysis.
    _bundle_root_key = os.path.normcase(os.path.abspath(str(_bundle_root)))
    _path_parts = []
    for _path_part in os.environ.get("PATH", "").split(os.pathsep):
        if not _path_part:
            continue
        try:
            _path_key = os.path.normcase(os.path.abspath(_path_part))
        except (OSError, ValueError):
            _path_key = os.path.normcase(_path_part)
        if _path_key != _bundle_root_key:
            _path_parts.append(_path_part)
    os.environ["PATH"] = os.pathsep.join(_path_parts)

from lookbookbot.__main__ import main


if __name__ == "__main__":
    raise SystemExit(main())
