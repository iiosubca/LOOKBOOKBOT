from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).parents[1] / "automation-engine" / "lookbook-layout" / "scripts" / "lookbook_gate.py"
SPEC = importlib.util.spec_from_file_location("lookbook_gate_delta_test", SCRIPT)
assert SPEC and SPEC.loader
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def _delta_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / "control" / "visual").mkdir(parents=True)
    archive = project / "control" / "history" / "visual-clearance" / "visual"
    archive.mkdir(parents=True)
    (archive / "composition-applied.json").write_text("{}", encoding="utf-8")
    gate.write_json(
        project / "control" / "visual" / "clearance-correction-plan.json",
        {"prior_proof_archive": "control/history/visual-clearance"},
    )
    return project


def _patch_delta_dependencies(monkeypatch: pytest.MonkeyPatch, project: Path, *, checkpoint: bool) -> None:
    state = {"look_count": 50}
    monkeypatch.setattr(gate, "load_state", lambda _project: state)
    monkeypatch.setattr(gate, "current_gate", lambda _project, _state: "visual")
    monkeypatch.setattr(gate, "validate_composition_plan", lambda _project, _state: None)
    monkeypatch.setattr(gate, "validate_composition_applied", lambda _project, _state: (_ for _ in ()).throw(gate.GateError("not complete")))
    monkeypatch.setattr(gate, "visual_confirmation_dir", lambda _project: project / "control" / "visual" / "confirmations")

    def fake_driver(_arguments: list[str], *, timeout_seconds: int) -> None:
        assert timeout_seconds == 240
        if checkpoint:
            gate.write_json(project / "control" / "progress" / "composition-delta.json", {"completed_looks": ["LOOK_001"]})

    monkeypatch.setattr(gate, "run_com_driver", fake_driver)


def test_first_delta_call_treats_missing_progress_as_zero_checkpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    project = _delta_project(tmp_path)
    _patch_delta_dependencies(monkeypatch, project, checkpoint=True)

    gate.command_apply_composition(SimpleNamespace(project=str(project)))

    assert "CHECKPOINT composition: 1 targeted looks saved" in capsys.readouterr().out


def test_delta_without_checkpoint_reports_native_recovery_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _delta_project(tmp_path)
    _patch_delta_dependencies(monkeypatch, project, checkpoint=False)

    with pytest.raises(gate.GateError, match="did not write a durable checkpoint"):
        gate.command_apply_composition(SimpleNamespace(project=str(project)))


def test_delta_worker_defers_unapplied_caption_corrections_until_final_batch() -> None:
    worker = Path(__file__).parents[1] / "automation-engine" / "lookbook-layout" / "scripts" / "run_lookbook_gate_com.ps1"
    source = worker.read_text(encoding="utf-8")

    assert "Assert-VisualCaptionCorrectionsApplied $doc $appliedCaptionCorrections" in source
    assert "if ($deltaByLook.Count -eq $targets.Count) { Assert-VisualCaptionCorrectionsApplied $doc $visualCaptionCorrections }" in source


def test_retry_plan_recovers_prior_caption_geometry_from_delta_evidence(tmp_path: Path) -> None:
    project = tmp_path / "project"
    master = project / "master.indd"
    master.parent.mkdir(parents=True)
    master.write_bytes(b"master")
    archive = project / "control" / "history" / "visual-clearance-test" / "visual"
    archive.mkdir(parents=True)
    state = {"session_id": "session", "master": "master.indd"}
    gate.write_json(archive / "composition-applied.json", {
        "schema": gate.SCHEMA,
        "generator": "run_lookbook_gate_com.ps1:ApplyCompositionDelta",
        "session_id": "session",
        "master": gate.identity(master),
        "items": [{
            "look_id": "LOOK_019",
            "caption_correction": {"after_frame_bounds": [89.0, 126.0, 356.0, 246.0]},
        }],
    })
    correction = {
        "look_id": "LOOK_019",
        "from_frame_bounds": [89.0, 66.0, 356.0, 186.0],
        "to_frame_bounds": [439.0, 416.0, 706.0, 536.0],
    }

    gate._reconcile_caption_priors_with_current_master(project, state, [correction])

    assert correction["prior_frame_bounds"] == [89.0, 126.0, 356.0, 246.0]
