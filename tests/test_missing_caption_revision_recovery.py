from __future__ import annotations

import importlib.util
import json
from argparse import Namespace
from pathlib import Path


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
    monkeypatch.setattr(gate, "current_gate", lambda *_args: "structure")

    gate.command_recover_missing_caption_revision_master(Namespace(project=str(project)))

    assert target.read_bytes() == source.read_bytes()
    recovered = gate.read_json(control / "lookbook-state.json")
    assert recovered["structure_revision"] == 1
    assert (control / "visual").is_dir()
    assert not (control / "visual" / "proof" / "stale.pdf").exists()
    assert list((control / "history").glob("missing-master-recovery-02-*/visual/proof/stale.pdf"))
