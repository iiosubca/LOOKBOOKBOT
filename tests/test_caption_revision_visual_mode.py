from __future__ import annotations

import json
import hashlib
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from lookbookbot.domain import ProviderKind
from lookbookbot.pipeline import PipelineEngine
from lookbookbot.state import StateStore


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
    record.write_text(
        json.dumps(
            {
                "visual_check_mode": "scope-only",
                "source_visual_evidence": "control/history/source/evidence/visual.json",
                "source_visual_manifest": "control/history/source/visual/proof/manifest.json",
                "source_visual_proof": "control/history/source/visual/proof/proof.pdf",
            }
        ),
        encoding="utf-8",
    )

    assert PipelineEngine._caption_revision_targeted_visual_enabled(root) is False

    record.write_text(
        json.dumps(
            {
                "visual_check_mode": "targeted",
                "source_visual_evidence": "control/history/source/evidence/visual.json",
                "source_visual_manifest": "control/history/source/visual/proof/manifest.json",
                "source_visual_proof": "control/history/source/visual/proof/proof.pdf",
            }
        ),
        encoding="utf-8",
    )
    assert PipelineEngine._caption_revision_targeted_visual_enabled(root) is True


def test_unreviewed_caption_revision_uses_a_full_visual_proof(tmp_path: Path, monkeypatch) -> None:
    store = StateStore(tmp_path / "state.db")
    project = _project(store, tmp_path)
    root = project.project_dir
    revisions = root / "control" / "revisions"
    revisions.mkdir(parents=True)
    (root / "control" / "lookbook-state.json").write_text(
            json.dumps({
                "manual_caption_revision": "control/revisions/manual.json",
                "current_revision": 2,
                "structure_revision": 2,
            }),
        encoding="utf-8",
    )
    (revisions / "revision-02.json").write_text(
        json.dumps({"visual_check_mode": "full", "visual_baseline": "not-yet-confirmed"}),
        encoding="utf-8",
    )
    engine = PipelineEngine(store)
    calls: list[tuple[str, tuple[str, ...]]] = []

    def gate(action: str, _root: Path, *args: str, **_kwargs):
        calls.append((action, args))
        return SimpleNamespace(returncode=0, text="PASS")

    monkeypatch.setattr(engine.controller, "gate", gate)

    engine.set_caption_revision_visual_mode(project, targeted=False)

    assert calls == [("set-caption-revision-visual-mode", ("--mode", "full"))]
    assert PipelineEngine._caption_revision_has_inherited_visual_baseline(root) is False
    assert PipelineEngine._caption_revision_targeted_visual_enabled(root) is False


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


def test_caption_revision_is_only_reported_ready_after_native_captions_prove_the_saved_draft(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    project = _project(store, tmp_path)
    root = project.project_dir
    evidence_dir = root / "control" / "evidence"
    revisions_dir = root / "control" / "revisions"
    work_dir = root / "control" / "work"
    evidence_dir.mkdir(parents=True)
    revisions_dir.mkdir(parents=True)
    work_dir.mkdir(parents=True)
    master = "TSUM_FS-0261112_LB_WA_04.indd"
    captions = work_dir / "caption-data.tsv"
    captions.write_text("look_id\ttype\tbrand\tprice\tarticle\nLOOK_001\tОчки\tTEST\t1 ₽\t1\n", encoding="utf-8")
    digest = hashlib.sha256(captions.read_bytes()).hexdigest()
    draft_ref = "control/revisions/manual-caption-revision.json"
    (root / draft_ref).write_text(
        json.dumps({"look_ids": ["LOOK_001"], "after_caption_data_sha256": digest}), encoding="utf-8"
    )
    (root / "control" / "lookbook-state.json").write_text(
        json.dumps({"master": master, "current_revision": 4, "manual_caption_revision": draft_ref, "captions": "control/work/caption-data.tsv"}),
        encoding="utf-8",
    )
    master_path = root / master
    master_path.write_bytes(b"corrected InDesign master")
    (evidence_dir / "captions.json").write_text(
        json.dumps(
            {
                "passed": True,
                "created_at": "2026-07-29T20:00:00Z",
                "master": {
                    "name": master,
                    "length": master_path.stat().st_size,
                    "modified_ms": int(master_path.stat().st_mtime * 1000),
                },
            }
        ),
        encoding="utf-8",
    )
    record = revisions_dir / "revision-04.json"
    record.write_text(json.dumps({"manual_caption_revision": draft_ref, "caption_application": "pending"}), encoding="utf-8")

    message = PipelineEngine(store).caption_revision_ready_message(project)

    assert master in message
    assert "LOOK_001" in message
    assert json.loads(record.read_text(encoding="utf-8"))["caption_application"] == "ready"
