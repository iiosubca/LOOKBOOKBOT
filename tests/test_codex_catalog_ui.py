from __future__ import annotations

import time

from PySide6.QtWidgets import QApplication

from lookbookbot.codex_catalog import CodexModel
from lookbookbot.ui import MainWindow


def test_startup_catalog_populates_model_and_model_specific_reasoning(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr("lookbookbot.ui.list_codex_models", lambda: [
        CodexModel("gpt-6-sol", "GPT-6 Sol", ("low", "medium", "high"), "medium"),
        CodexModel("gpt-6-luna", "GPT-6 Luna", ("low", "medium"), "medium"),
    ])
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.show()
    deadline = time.monotonic() + 5
    while (window._catalog_pending or not window.codex_catalog_verified) and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()

    assert window.codex_catalog_verified
    assert [window.model_combo.itemText(i) for i in range(window.model_combo.count())][:2] == [
        "gpt-6-sol", "gpt-6-luna",
    ]
    window.model_combo.setCurrentText("gpt-6-luna")
    assert [window.reasoning_combo.itemData(i) for i in range(window.reasoning_combo.count())] == [
        "", "low", "medium",
    ]
    window.reasoning_combo.setCurrentIndex(window.reasoning_combo.findData("low"))
    assert window.reasoning_combo.currentData() == "low"
    window.close()
