from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path

import pytest


GATE_PATH = Path(__file__).parents[1] / "automation-engine" / "lookbook-layout" / "scripts" / "lookbook_gate.py"
SPEC = importlib.util.spec_from_file_location("lookbook_gate_caption_revision_history", GATE_PATH)
assert SPEC and SPEC.loader
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def _change(look_id: str, before_brand: str, after_brand: str) -> dict:
    return {
        "look_id": look_id,
        "before": [{"type": "Товар", "brand": before_brand, "price": "1 000 ₽", "article": "0001"}],
        "after": [{"type": "Товар", "brand": after_brand, "price": "1 000 ₽", "article": "0001"}],
    }


def _audit(*changes: dict) -> dict:
    return {
        "schema": "lookbookbot-caption-revision-v1",
        "look_ids": [change["look_id"] for change in changes],
        "before_caption_data_sha256": "before",
        "after_caption_data_sha256": "pending",
        "changes": list(changes),
    }


def test_chained_caption_audit_keeps_earlier_manual_corrections() -> None:
    merged = gate.merge_caption_revision_history(
        _audit(_change("LOOK_001", "EXCEL BRAND", "FIRST FIX")),
        _audit(_change("LOOK_002", "EXCEL BRAND", "SECOND FIX")),
        previous_reference="control/history/revision-02/manual-caption-revision.json",
    )

    assert merged["look_ids"] == ["LOOK_001", "LOOK_002"]
    assert [item["look_id"] for item in merged["changes"]] == ["LOOK_001", "LOOK_002"]
    assert merged["changes"][0]["after"][0]["brand"] == "FIRST FIX"
    assert merged["changes"][1]["after"][0]["brand"] == "SECOND FIX"
    assert merged["previous_manual_caption_revision"].endswith("revision-02/manual-caption-revision.json")


def test_chained_caption_audit_replaces_only_the_latest_value_for_same_look() -> None:
    merged = gate.merge_caption_revision_history(
        _audit(_change("LOOK_035", "EXCEL BRAND", "FIRST FIX")),
        _audit(_change("LOOK_035", "FIRST FIX", "SECOND FIX")),
    )

    assert merged["look_ids"] == ["LOOK_035"]
    assert merged["changes"] == [_change("LOOK_035", "EXCEL BRAND", "SECOND FIX")]


def test_chained_caption_audit_rejects_a_stale_second_draft() -> None:
    with pytest.raises(gate.GateError, match="does not start from the latest signed"):
        gate.merge_caption_revision_history(
            _audit(_change("LOOK_035", "EXCEL BRAND", "FIRST FIX")),
            _audit(_change("LOOK_035", "EXCEL BRAND", "SECOND FIX")),
        )


def test_legacy_delta_only_revision_is_reconciled_before_captions(monkeypatch, tmp_path: Path) -> None:
    """A project created by an older EXE recovers without re-entering edits."""
    project = tmp_path / "TSUM_FS-0260809"
    control = project / "control"
    captions = control / "work" / "caption-data.tsv"
    captions.parent.mkdir(parents=True)
    captions.write_text(
        "look_id\ttype\tbrand\tprice\tarticle\n"
        "LOOK_001\tТовар\tFIRST FIX\t1 000 ₽\t0001\n"
        "LOOK_002\tТовар\tSECOND FIX\t1 000 ₽\t0001\n",
        encoding="utf-8",
    )
    previous_ref = "control/history/revision-02/manual-caption-revision.json"
    current_ref = "control/history/revision-03/manual-caption-revision.json"
    previous = project / previous_ref
    current = project / current_ref
    previous.parent.mkdir(parents=True)
    gate.write_json(previous, _audit(_change("LOOK_001", "EXCEL BRAND", "FIRST FIX")))
    current_payload = _audit(_change("LOOK_002", "EXCEL BRAND", "SECOND FIX"))
    current_payload["after_caption_data_sha256"] = hashlib.sha256(captions.read_bytes()).hexdigest()
    gate.write_json(current, current_payload)
    (control / "revisions").mkdir(exist_ok=True)
    gate.write_json(control / "revisions" / "revision-02.json", {"manual_caption_revision": previous_ref})
    gate.write_json(control / "evidence" / "map.json", {"schema": gate.SCHEMA})
    state = {
        "current_revision": 3,
        "manual_caption_revision": current_ref,
        "captions": str(captions.relative_to(project)),
        "master": "TSUM_FS-0260809_LB_WA_03.indd",
    }

    monkeypatch.setattr(gate, "load_state", lambda _project: state)
    monkeypatch.setattr(gate, "caption_revision_record", lambda *_args: {"kind": "captions"})
    monkeypatch.setattr(gate, "current_gate", lambda *_args: "map")
    monkeypatch.setattr(gate, "validate_manual_caption_revision_inputs", lambda *_args: 2)
    monkeypatch.setattr(gate, "load_evidence", lambda *_args: {"passed": True})

    gate.command_reconcile_caption_revision_history(type("Args", (), {"project": str(project)})())

    repaired = json.loads(current.read_text(encoding="utf-8"))
    assert [item["look_id"] for item in repaired["changes"]] == ["LOOK_001", "LOOK_002"]
    assert repaired["previous_manual_caption_revision"] == previous_ref
    repaired_map = json.loads((control / "evidence" / "map.json").read_text(encoding="utf-8"))
    assert repaired_map["caption_data_sha256"] == hashlib.sha256(captions.read_bytes()).hexdigest()
    assert repaired_map["manual_caption_revision_sha256"] == hashlib.sha256(current.read_bytes()).hexdigest()
    assert (control / "history" / "revision-03-history-repair.json").is_file()
