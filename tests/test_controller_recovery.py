from __future__ import annotations

from pathlib import Path

from lookbookbot.config import ToolPaths
from lookbookbot.controller import CommandError, CommandResult, CommandRunner, LookbookController


def _tools(tmp_path: Path) -> ToolPaths:
    return ToolPaths(
        python=tmp_path / "python.exe",
        skill_root=tmp_path,
        scripts=tmp_path,
        gate=tmp_path / "lookbook_gate.py",
        template=tmp_path / "template.indd",
    )


def test_native_gate_retries_one_safe_com_disconnect(tmp_path: Path, monkeypatch) -> None:
    controller = LookbookController(_tools(tmp_path), CommandRunner())
    calls: list[str] = []
    apply_calls = 0

    def fake_gate(action: str, _project: Path, *_args, **_kwargs) -> CommandResult:
        nonlocal apply_calls
        calls.append(action)
        if action == "arm":
            return CommandResult((), 0, "ARMED", "", 0)
        apply_calls += 1
        if apply_calls == 1:
            return CommandResult((), 1, "", "RPC_E_DISCONNECTED 0x80010108", 0)
        return CommandResult((), 0, "COM_GATE_PASS release", "", 0)

    monkeypatch.setattr(controller, "gate", fake_gate)
    monkeypatch.setattr(controller, "_wait_for_master", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(controller, "_restart_controlled_indesign", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(controller, "_wait_for_indesign_ready", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("lookbookbot.controller.time.sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("lookbookbot.controller.evidence_passed", lambda *_args: apply_calls >= 2)

    controller.apply_native_gate(tmp_path, "release")

    assert calls == ["arm", "apply", "apply"]


def test_release_retries_the_same_arm_after_two_com_disconnects(tmp_path: Path, monkeypatch) -> None:
    controller = LookbookController(_tools(tmp_path), CommandRunner())
    calls: list[str] = []
    apply_calls = 0
    restarts = 0

    def fake_gate(action: str, _project: Path, *_args, **_kwargs) -> CommandResult:
        nonlocal apply_calls
        calls.append(action)
        if action == "arm":
            return CommandResult((), 0, "ARMED", "", 0)
        apply_calls += 1
        if apply_calls < 3:
            return CommandResult((), 1, "", "RPC_E_DISCONNECTED 0x80010108", 0)
        return CommandResult((), 0, "COM_GATE_PASS release", "", 0)

    def fake_restart(*_args, **_kwargs) -> bool:
        nonlocal restarts
        restarts += 1
        return True

    monkeypatch.setattr(controller, "gate", fake_gate)
    monkeypatch.setattr(controller, "_wait_for_master", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(controller, "_restart_controlled_indesign", fake_restart)
    monkeypatch.setattr(controller, "_wait_for_indesign_ready", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("lookbookbot.controller.evidence_passed", lambda *_args: apply_calls >= 3)

    controller.apply_native_gate(tmp_path, "release")

    assert calls == ["arm", "apply", "apply", "apply"]
    assert restarts == 2


def test_com_disconnect_stops_when_safe_restart_cannot_be_proven(tmp_path: Path, monkeypatch) -> None:
    controller = LookbookController(_tools(tmp_path), CommandRunner())

    def fake_gate(action: str, _project: Path, *_args, **_kwargs) -> CommandResult:
        if action == "arm":
            return CommandResult((), 0, "ARMED", "", 0)
        return CommandResult((), 1, "", "RPC_E_DISCONNECTED 0x80010108", 0)

    monkeypatch.setattr(controller, "gate", fake_gate)
    monkeypatch.setattr(controller, "_wait_for_master", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(controller, "_restart_controlled_indesign", lambda *_args, **_kwargs: False)

    try:
        controller.apply_native_gate(tmp_path, "release")
    except CommandError as error:
        assert "не смогло доказать" in str(error)
    else:
        raise AssertionError("a non-provable restart must not touch InDesign")


def test_review_export_reissues_permit_after_safe_com_recovery(tmp_path: Path, monkeypatch) -> None:
    controller = LookbookController(_tools(tmp_path), CommandRunner())
    calls: list[str] = []
    export_attempts = 0

    def fake_gate(action: str, _project: Path, *_args, **_kwargs) -> CommandResult:
        nonlocal export_attempts
        calls.append(action)
        if action == "pre-export":
            return CommandResult((), 0, "PERMIT", "", 0)
        export_attempts += 1
        if export_attempts == 1:
            return CommandResult((), 1, "", "RPC_E_DISCONNECTED 0x80010108", 0)
        return CommandResult((), 0, "PDF EXPORTED", "", 0)

    monkeypatch.setattr(controller, "gate", fake_gate)
    monkeypatch.setattr(controller, "_wait_for_master", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(controller, "_restart_controlled_indesign", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(controller, "_wait_for_indesign_ready", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("lookbookbot.controller.time.sleep", lambda *_args, **_kwargs: None)

    controller.export_review_pdf(tmp_path, "review.pdf")

    assert calls == ["pre-export", "export-pdf", "pre-export", "export-pdf"]
