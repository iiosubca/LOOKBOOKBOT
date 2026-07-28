from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from lookbookbot.pipeline import PipelineEngine, PipelineError
from lookbookbot.state import StateStore


def test_visual_resume_reuses_existing_plan_and_reconciles_prior_geometry(tmp_path: Path, monkeypatch) -> None:
    engine = PipelineEngine(StateStore(tmp_path / "state.db"))
    root = tmp_path / "project"
    visual = root / "control" / "visual"
    visual.mkdir(parents=True)
    (visual / "composition-plan.tsv").write_text("look_id\n", encoding="utf-8")
    (visual / "clearance-correction-plan.json").write_text("{}", encoding="utf-8")
    scripts: list[str] = []
    gates: list[str] = []

    monkeypatch.setattr(engine.controller, "script", lambda name, *_args, **_kwargs: scripts.append(name))
    monkeypatch.setattr(
        engine.controller,
        "gate",
        lambda action, *_args, **_kwargs: (gates.append(action), SimpleNamespace(returncode=0, text="PASS visual resume"))[1],
    )

    engine._prepare_or_resume_visual_composition(root)

    assert scripts == []
    assert gates == ["reconcile-clearance-plan-priors"]


def test_visual_start_creates_plan_only_when_none_exists(tmp_path: Path, monkeypatch) -> None:
    engine = PipelineEngine(StateStore(tmp_path / "state.db"))
    root = tmp_path / "project"
    root.mkdir()
    scripts: list[str] = []

    monkeypatch.setattr(engine.controller, "script", lambda name, *_args, **_kwargs: scripts.append(name))

    engine._prepare_or_resume_visual_composition(root)

    assert scripts == ["prepare_composition_audit.py"]


def test_clearance_recovery_has_no_arbitrary_attempt_limit(tmp_path: Path, monkeypatch) -> None:
    engine = PipelineEngine(StateStore(tmp_path / "state.db"))
    root = tmp_path / "project"
    attempts = {"render": 0, "plan": 0, "apply": 0}

    monkeypatch.setattr(engine.controller, "script", lambda *_args, **_kwargs: None)

    def gate(action: str, *_args, **_kwargs):
        if action == "render-visual-proof":
            attempts["render"] += 1
            if attempts["render"] <= 17:
                return SimpleNamespace(returncode=1, text="CAPTION CLEARANCE BLOCKED")
            pair = root / "control" / "visual" / "proof" / "pairs" / "final" / "LOOK_001.jpg"
            pair.parent.mkdir(parents=True, exist_ok=True)
            pair.write_bytes(b"proof")
            return SimpleNamespace(returncode=0, text="VISUAL PROOF RENDERED")
        if action == "plan-clearance-corrections":
            attempts["plan"] += 1
            plan = root / "control" / "visual" / "clearance-correction-plan.json"
            plan.parent.mkdir(parents=True, exist_ok=True)
            plan.write_text(json.dumps({
                "corrections": [{"look_id": "LOOK_001", "to_points": attempts["plan"]}],
                "caption_corrections": [], "unresolved": [],
            }), encoding="utf-8")
            return SimpleNamespace(returncode=0, text="CLEARANCE CORRECTION PLAN")
        if action == "apply-composition":
            attempts["apply"] += 1
            return SimpleNamespace(returncode=0, text="PASS composition")
        return SimpleNamespace(returncode=0, text="PASS")

    monkeypatch.setattr(engine.controller, "gate", gate)

    pairs = engine._prepare_visual_proof(SimpleNamespace(project_dir=root))

    assert [pair.stem for pair in pairs] == ["LOOK_001"]
    assert attempts == {"render": 18, "plan": 17, "apply": 18}


def test_clearance_recovery_stops_before_reapplying_identical_plan(tmp_path: Path, monkeypatch) -> None:
    engine = PipelineEngine(StateStore(tmp_path / "state.db"))
    root = tmp_path / "project"
    calls = {"apply": 0, "plan": 0}

    monkeypatch.setattr(engine.controller, "script", lambda *_args, **_kwargs: None)

    def gate(action: str, *_args, **_kwargs):
        if action == "render-visual-proof":
            return SimpleNamespace(returncode=1, text="CAPTION CLEARANCE BLOCKED")
        if action == "plan-clearance-corrections":
            calls["plan"] += 1
            plan = root / "control" / "visual" / "clearance-correction-plan.json"
            plan.parent.mkdir(parents=True, exist_ok=True)
            plan.write_text(json.dumps({
                "corrections": [{"look_id": "LOOK_001", "to_points": 12}],
                "caption_corrections": [], "unresolved": [],
            }), encoding="utf-8")
            return SimpleNamespace(returncode=0, text="CLEARANCE CORRECTION PLAN")
        if action == "apply-composition":
            calls["apply"] += 1
            return SimpleNamespace(returncode=0, text="PASS composition")
        return SimpleNamespace(returncode=0, text="PASS")

    monkeypatch.setattr(engine.controller, "gate", gate)

    with pytest.raises(PipelineError, match="повторил уже применённый план"):
        engine._prepare_visual_proof(SimpleNamespace(project_dir=root))

    assert calls == {"apply": 2, "plan": 2}
