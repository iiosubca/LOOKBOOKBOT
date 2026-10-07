from __future__ import annotations

import time

from PySide6.QtWidgets import QApplication

from lookbookbot.codex_catalog import CodexModel
from lookbookbot.ui import MainWindow
from test_excel_preparation import fixture_book


def test_button_prepares_selected_folder_in_background(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "settings"))
    monkeypatch.setattr("lookbookbot.ui.list_codex_models", lambda: [
        CodexModel("gpt-6-sol", "GPT-6 Sol", ("low", "medium"), "medium"),
    ])
    messages = []
    monkeypatch.setattr("lookbookbot.ui.QMessageBox.information", lambda *args: messages.append(args[2]))
    monkeypatch.setattr("lookbookbot.ui.QMessageBox.warning", lambda *args: messages.append(args[2]))
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    source = fixture_book(tmp_path / "credits.xlsx")
    window.source_edit.setText(str(tmp_path))
    window.prepare_excel_button.click()
    assert window.excel_thread is not None
    assert not window.prepare_excel_button.isEnabled()
    assert not window.create_project_button.isEnabled()
    assert not window.source_edit.isEnabled()
    deadline = time.monotonic() + 20
    while window.excel_thread is not None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert window.excel_thread is None
    assert window.prepare_excel_button.isEnabled()
    assert window.create_project_button.isEnabled()
    assert window.source_edit.isEnabled()
    assert source.is_file() and (tmp_path / "credits prepared.xlsx").is_file()
    assert messages and "Готово:" in messages[0]
    window.close()


def test_button_reports_error_without_new_file(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "settings"))
    monkeypatch.setattr("lookbookbot.ui.list_codex_models", lambda: [])
    messages = []
    monkeypatch.setattr("lookbookbot.ui.QMessageBox.warning", lambda *args: messages.append(args[2]))
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    source = fixture_book(tmp_path / "credits.xlsx", invalid=True)
    window.source_edit.setText(str(tmp_path))
    window.prepare_excel_button.click()
    deadline = time.monotonic() + 20
    while window.excel_thread is not None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert window.excel_thread is None
    assert window.prepare_excel_button.isEnabled()
    assert list(tmp_path.glob("*.xlsx")) == [source]
    assert messages and "I3" in messages[0]
    window.close()
