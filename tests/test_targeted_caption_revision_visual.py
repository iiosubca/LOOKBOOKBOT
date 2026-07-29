from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from lookbookbot.domain import ProviderKind
from lookbookbot.pipeline import PipelineEngine
from lookbookbot.providers import CodexProvider, VisionDecision
from lookbookbot.state import StateStore


def _project(store: StateStore, tmp_path: Path):
    return store.save_project(
        name="targeted",
        source_dir=tmp_path / "SOURCES",
        output_root=tmp_path,
        project_dir=tmp_path / "targeted",
        show_date=date(2026, 8, 7),
        provider=ProviderKind.CODEX,
        model="",
    )


def test_caption_revision_visual_renders_and_inspects_only_changed_pairs(tmp_path: Path, monkeypatch) -> None:
    store = StateStore(tmp_path / "state.db")
    project = _project(store, tmp_path)
    engine = PipelineEngine(store)
    root = project.project_dir
    proof = root / "control" / "visual" / "proof" / "revision-targeted" / "pairs" / "LOOK_041.jpg"
    proof.parent.mkdir(parents=True, exist_ok=True)
    proof.write_bytes(b"targeted proof")
    actions: list[str] = []

    def gate(action: str, _root: Path, *_args: str, **_kwargs):
        actions.append(action)
        if action == "render-revision-visual-proof":
            manifest = proof.parents[2] / "manifest.json"
            manifest.write_text(json.dumps({"look_pairs": {"LOOK_041": {"image": str(proof.relative_to(root)).replace("\\", "/")}}}), encoding="utf-8")
        return SimpleNamespace(returncode=0, text="PASS")

    monkeypatch.setattr(engine.controller, "gate", gate)
    monkeypatch.setattr(
        engine,
        "_confirm_codex_visual_batch",
        lambda _provider, _root, batch: {
            item.stem: VisionDecision(accepted=True, note="Credits are readable, within the safe area and clear of the model.")
            for item in batch
        },
    )

    engine._run_targeted_caption_revision_visual(project, CodexProvider())

    assert actions == [
        "arm", "audit-caption-revision-scope", "render-revision-visual-proof",
        "confirm-visual-look", "record-visual",
    ]
    assert "render-visual-proof" not in actions
