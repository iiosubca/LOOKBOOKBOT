from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from .ui import MainWindow


def _asset_path(filename: str) -> Path:
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        return Path(bundle_root) / "assets" / filename
    return Path(__file__).resolve().parents[1] / "assets" / filename


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("LOOKBOOKBOT")
    app.setOrganizationName("LOOKBOOKBOT")
    app.setWindowIcon(QIcon(str(_asset_path("lookbookbot.ico"))))
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
