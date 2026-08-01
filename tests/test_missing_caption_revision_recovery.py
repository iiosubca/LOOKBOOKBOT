from __future__ import annotations

import importlib.util
import json
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

from lookbookbot.pipeline import PipelineEngine, PipelineResult


GATE_PATH = Path(__file__).parents[1] / "automation-engine" / "lookbook-layout" / "scripts" / "lookbook_gate.py"
SPEC = importlib.util.spec_from_file_location("lookbook_gate_missing_caption_revision", GATE_PATH)
assert SPEC and SPEC.loader
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def test_missing_verified_caption_revision_is_rebuilt_from_its_signed_source(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "project"
    control = project / "control"
    revisions = control / "revisions"
    revisions.mkdir(parents=True)
    source = project / "TSUM_FS-0260830_LB_WA_01.indd"
    target = project / "TSUM_FS-0260830_LB_WA_02.indd"
    source.write_bytes(b"verified source master")
    audit_ref = "control/history/revision-02/manual-caption-revision.json"
    state = {
        "schema": gate.SCHEMA,
        "gates": list(gate.GATES),
        "master": target.name,
        "current_revision": 2,
        "manual_caption_revision": audit_ref,
        "structure_revision": 2,
    }
    gate.write_json(control / "lookbook-state.json", state)
    gate.write_json(
        revisions / "revision-02.json",
        {
            "schema": gate.SCHEMA,
            "kind": "captions",
            "caption_application": "ready",
            "master": {"name": target.name},
            "source_master": gate.identity(source),
            "manual_caption_revision": audit_ref,
            "changed_looks": ["LOOK_001"],
        },
    )
    (control / "visual" / "proof").mkdir(parents=True)
    (control / "visual" / "proof" / "stale.pdf").write_bytes(b"stale proof")

    monkeypatch.setattr(gate, "manual_caption_revision_audit", lambda *_args: (project / audit_ref, {}))
    monkeypatch.setattr(gate, "current_gate", lambda *_args: "captions")

    gate.command_recover_missing_caption_revision_master(Namespace(project=str(project)))

    assert target.read_bytes() == source.read_bytes()
    recovered = gate.read_json(control / "lookbook-state.json")
    assert recovered["structure_revision"] == 2
    assert (control / "visual").is_dir()
    assert not (control / "visual" / "proof" / "stale.pdf").exists()
    assert list((control / "history").glob("missing-master-recovery-02-*/visual/proof/stale.pdf"))


def test_interrupted_recovery_restores_pre_caption_evidence_without_rebuilding_structure(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "project"
    control = project / "control"
    revisions = control / "revisions"
    revisions.mkdir(parents=True)
    source = project / "TSUM_FS-0260830_LB_WA_01.indd"
    target = project / "TSUM_FS-0260830_LB_WA_02.indd"
    source.write_bytes(b"verified source master")
    target.write_bytes(b"recovered source master")
    audit_ref = "control/history/revision-02/manual-caption-revision.json"
    state = {
        "schema": gate.SCHEMA,
        "gates": list(gate.GATES),
        "master": target.name,
        "current_revision": 2,
        "manual_caption_revision": audit_ref,
        "structure_revision": 1,
    }
    gate.write_json(control / "lookbook-state.json", state)
    gate.write_json(
        revisions / "revision-02.json",
        {
            "schema": gate.SCHEMA,
            "kind": "captions",
            "caption_application": "ready",
            "master": {"name": target.name},
            "source_master": gate.identity(source),
            "manual_caption_revision": audit_ref,
            "changed_looks": ["LOOK_001"],
        },
    )
    archive = control / "history" / "missing-master-recovery-02-legacy"
    for relative, content in {
        "evidence/structure.json": b"structure",
        "evidence/dates.json": b"dates",
        "evidence/frames.json": b"frames",
        "evidence/images.json": b"images",
        "arms/structure.json": b"structure arm",
        "arms/dates.json": b"dates arm",
        "arms/frames.json": b"frames arm",
        "arms/images.json": b"images arm",
    }.items():
        file = archive / relative
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(content)
    (control / "visual" / "proof").mkdir(parents=True)
    (control / "visual" / "proof" / "stale.pdf").write_bytes(b"stale proof")
    (control / "arms").mkdir(parents=True)
    (control / "arms" / "structure.json").write_bytes(b"wrong template arm")

    monkeypatch.setattr(gate, "manual_caption_revision_audit", lambda *_args: (project / audit_ref, {}))
    monkeypatch.setattr(gate, "current_gate", lambda *_args: "captions")

    gate.command_recover_missing_caption_revision_master(Namespace(project=str(project)))

    recovered = gate.read_json(control / "lookbook-state.json")
    assert recovered["structure_revision"] == 2
    assert (control / "evidence" / "structure.json").read_bytes() == b"structure"
    assert (control / "arms" / "images.json").read_bytes() == b"images arm"
    assert (control / "arms" / "structure.json").read_bytes() == b"structure arm"
    assert list(archive.glob("retry-*/arms/structure.json"))
    assert not (control / "visual" / "proof" / "stale.pdf").exists()
    assert (revisions / "missing-master-recovery-02.json").is_file()


def test_pending_recovery_marker_replays_captions_before_another_revision(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    control = project_dir / "control"
    revisions = control / "revisions"
    revisions.mkdir(parents=True)
    master = project_dir / "TSUM_FS-0260830_LB_WA_02.indd"
    master.write_bytes(b"recovered master")
    (control / "lookbook-state.json").write_text(
        json.dumps({"master": master.name, "current_revision": 2, "structure_revision": 2}),
        encoding="utf-8",
    )
    marker = revisions / "missing-master-recovery-02.json"
    marker.write_text(json.dumps({"status": "awaiting-captions"}), encoding="utf-8")

    class Store:
        def __init__(self) -> None:
            self.calls: list[tuple[object, ...]] = []

        def set_approved(self, *args) -> None:
            self.calls.append(("approved", *args))

        def reset_from(self, *args) -> None:
            self.calls.append(("reset", *args))

    class Controller:
        def __init__(self) -> None:
            self.calls: list[tuple[object, ...]] = []

        def gate(self, *args, **kwargs) -> None:
            self.calls.append(args)

    store = Store()
    engine = PipelineEngine(store)  # type: ignore[arg-type]
    controller = Controller()
    engine.controller = controller  # type: ignore[assignment]
    engine.run = lambda *_args, **_kwargs: PipelineResult(("captions",), None, "ok")  # type: ignore[method-assign]

    engine.recover_missing_caption_revision_master(SimpleNamespace(id="project", project_dir=project_dir))

    assert controller.calls[0][0] == "recover-missing-caption-revision-master"
    assert ("reset", "project", "captions") in store.calls
    assert json.loads(marker.read_text(encoding="utf-8"))["status"] == "captions-verified"
