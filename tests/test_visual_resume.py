from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from lookbookbot.pipeline import PipelineEngine
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
