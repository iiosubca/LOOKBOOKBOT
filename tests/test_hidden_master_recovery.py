from __future__ import annotations

import json
from pathlib import Path

import pytest

from lookbookbot.config import ToolPaths
from lookbookbot.controller import CommandError, CommandRunner, CommandResult, LookbookController


def fixture(root):
    (root / "control").mkdir()
    (root / "master.indd").write_bytes(b"saved master")
    (root / "control/lookbook-state.json").write_text(json.dumps({"master": "master.indd"}))
    lock = root / "master.idlk"
    lock.touch()
    tools = ToolPaths(root / "python.exe", root, root / "gate.py", root / "template.indd")
    return LookbookController(tools, CommandRunner()), lock


def test_hidden_saved_master_can_close_after_finished_worker_without_changing_file(tmp_path, monkeypatch):
    controller, lock = fixture(tmp_path)
    before = (tmp_path / "master.indd").read_bytes()
    def probe(command, **kwargs):
        assert "recover_saved_master.ps1" in str(command)
        assert kwargs["timeout"] == 30
        lock.unlink()  # InDesign closes its own document and removes its lock.
        return CommandResult((), 0, '{"status":"closed"}', "", 0)
    monkeypatch.setattr(controller.runner, "run", probe)
    monkeypatch.setattr("lookbookbot.controller.time.sleep", lambda _: None)
    controller._wait_for_master(tmp_path)
    assert (tmp_path / "master.indd").read_bytes() == before


def test_unsaved_changes_are_never_discarded(tmp_path, monkeypatch):
    controller, lock = fixture(tmp_path)
    monkeypatch.setattr(controller.runner, "run", lambda *args, **kwargs: CommandResult((), 0, '{"status":"unsaved"}', "", 0))
    with pytest.raises(CommandError, match="несохранённые изменения"):
        controller._wait_for_master(tmp_path)
    assert lock.exists()


def test_busy_worker_does_not_trigger_another_apply_or_delete_a_lock(tmp_path, monkeypatch):
    controller, lock = fixture(tmp_path)
    calls = []
    def busy(*args, **kwargs):
        calls.append(True)
        return CommandResult((), 0, '{"status":"busy","reason":"worker alive"}', "", 0)
    monkeypatch.setattr(controller.runner, "run", busy)
    with pytest.raises(CommandError, match="второй процесс не запущен"):
        controller._wait_for_master(tmp_path, timeout=0)
    assert lock.exists() and len(calls) == 1


def test_native_close_helper_guards_modified_not_merely_saved():
    scripts = Path(__file__).parents[1] / "automation-engine/lookbook-layout/scripts"
    text = (scripts / "recover_saved_master.ps1").read_text(encoding="utf-8")
    assert "$doc.Modified" in text and "run_lookbook_gate_com" in text
    assert "Remove-Item" not in text and "Stop-Process" not in text
    assert "MainWindowTitle" in text and "$signature" in text


def test_busy_inspection_retries_only_after_worker_can_finish(tmp_path, monkeypatch):
    controller, lock = fixture(tmp_path)
    clock, calls = [0], []
    monkeypatch.setattr("lookbookbot.controller.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("lookbookbot.controller.time.sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    def probe(*args, **kwargs):
        calls.append(clock[0])
        if len(calls) == 1:
            return CommandResult((), 0, '{"status":"busy"}', "", 0)
        lock.unlink()
        return CommandResult((), 0, '{"status":"closed"}', "", 0)
    monkeypatch.setattr(controller.runner, "run", probe)
    controller._wait_for_master(tmp_path, timeout=20)
    assert calls == [0, 10]
