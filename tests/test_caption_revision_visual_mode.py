from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from lookbookbot.domain import ProviderKind
from lookbookbot.pipeline import PipelineEngine
from lookbookbot.state import StateStore
from lookbookbot.ui import MainWindow


def _project(store: StateStore, tmp_path: Path):
    return store.save_project(
        name="revision-mode",
        source_dir=tmp_path / "SOURCES",
        output_root=tmp_path,
        project_dir=tmp_path / "revision-mode",
        show_date=date(2026, 8, 7),
        provider=ProviderKind.CODEX,
        model="",
    )


def test_scope_only_revision_mode_is_persisted_in_the_revision_record(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    project = _project(store, tmp_path)
    root = project.project_dir
    revision_dir = root / "control" / "revisions"
    revision_dir.mkdir(parents=True)
    (root / "control" / "lookbook-state.json").write_text(
        json.dumps({"manual_caption_revision": "control/revisions/manual.json", "current_revision": 2}), encoding="utf-8"
    )
    record = revision_dir / "revision-02.json"
    record.write_text(json.dumps({"visual_check_mode": "scope-only"}), encoding="utf-8")

    assert PipelineEngine._caption_revision_targeted_visual_enabled(root) is False

    record.write_text(json.dumps({"visual_check_mode": "targeted"}), encoding="utf-8")
    assert PipelineEngine._caption_revision_targeted_visual_enabled(root) is True


def test_scope_only_visual_route_runs_scope_audit_without_rendering_pairs(tmp_path: Path, monkeypatch) -> None:
    store = StateStore(tmp_path / "state.db")
    project = _project(store, tmp_path)
    engine = PipelineEngine(store)
    actions: list[str] = []

    def gate(action: str, _root: Path, *_args: str, **_kwargs):
        actions.append(action)
        return SimpleNamespace(returncode=0, text="PASS")

    monkeypatch.setattr(engine.controller, "gate", gate)

    engine._record_scope_only_caption_revision_visual(project)

    assert actions == ["arm", "audit-caption-revision-scope", "record-caption-revision-scope-visual"]
    assert "render-revision-visual-proof" not in actions


def test_next_revision_is_allowed_after_the_current_review_pdf_is_controller_confirmed(tmp_path: Path) -> None:
    root = tmp_path / "project"
    (root / "control" / "evidence").mkdir(parents=True)
    master = "TSUM_FS-0261112_LB_WA_03.indd"
    pdf = "TSUM_FS-0261112_LB_WA_03_review.pdf"
    (root / pdf).write_bytes(b"review PDF")
    (root / "control" / "lookbook-state.json").write_text(json.dumps({"master": master}), encoding="utf-8")
    (root / "control" / "evidence" / "pdf.json").write_text(
        json.dumps({"passed": True, "master": {"name": master}, "pdf": {"name": pdf}}), encoding="utf-8"
    )

    window_stub = SimpleNamespace(project=SimpleNamespace(project_dir=root))

    assert MainWindow._current_caption_revision_review_pdf_passed(window_stub) is True
