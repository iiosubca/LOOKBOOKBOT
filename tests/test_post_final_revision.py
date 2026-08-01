from __future__ import annotations

import importlib.util
import json
from argparse import Namespace
from pathlib import Path


GATE_PATH = Path(__file__).parents[1] / "automation-engine" / "lookbook-layout" / "scripts" / "lookbook_gate.py"
SPEC = importlib.util.spec_from_file_location("lookbook_gate_post_final_revision", GATE_PATH)
assert SPEC and SPEC.loader
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def test_post_final_revision_archives_manifest_without_removing_prior_pdfs(tmp_path: Path, monkeypatch) -> None:
    """A correction after approval must create _02 rather than lock _01."""
    project = tmp_path / "TSUM_FS-0261115"
    control = project / "control"
    control.mkdir(parents=True)
    master = project / "TSUM_FS-0261115_LB_WA_01.indd"
    prior_final = project / "TSUM_FS-0261115_LB_WA_01_40mb.pdf"
    master.write_bytes(b"reviewed-master")
    prior_final.write_bytes(b"approved-final-pdf")
    gate.write_json(
        control / "lookbook-state.json",
        {
            "schema": gate.SCHEMA,
            "gates": list(gate.GATES),
            "session_id": "approved-session",
            "master": master.name,
            "current_revision": 1,
        },
    )
    gate.write_json(
        control / "final-deliverables.json",
        {
            "schema": gate.SCHEMA,
            "status": "complete",
            "master": gate.identity(master),
            "outputs": [{"path": prior_final.name}],
        },
    )
    calls = 0

    def current_gate(_project: Path, _state: dict) -> str | None:
        nonlocal calls
        calls += 1
        return None if calls == 1 else "visual"

    monkeypatch.setattr(gate, "current_gate", current_gate)

    gate.command_begin_revision(
        Namespace(
            project=str(project),
            reset_from="visual",
            caption_audit="",
            notes="Правка после уже согласованного финального выпуска.",
        )
    )

    new_master = project / "TSUM_FS-0261115_LB_WA_02.indd"
    assert new_master.read_bytes() == b"reviewed-master"
    assert prior_final.read_bytes() == b"approved-final-pdf"
    assert not (control / "final-deliverables.json").exists()
    archived = list((control / "history").glob("revision-02-*/final-deliverables.json"))
    assert len(archived) == 1
    assert json.loads(archived[0].read_text(encoding="utf-8"))["status"] == "complete"
    revision = json.loads((control / "revisions" / "revision-02.json").read_text(encoding="utf-8"))
    assert revision["superseded_final_publication"].startswith("control/history/")
