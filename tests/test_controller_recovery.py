from __future__ import annotations

from pathlib import Path

from lookbookbot.config import ToolPaths
from lookbookbot.controller import CommandResult, CommandRunner, LookbookController


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
    evidence_checks = iter([False, False, False, True])
    monkeypatch.setattr("lookbookbot.controller.evidence_passed", lambda *_args: next(evidence_checks))

    controller.apply_native_gate(tmp_path, "release")

    assert calls == ["arm", "apply", "apply"]
