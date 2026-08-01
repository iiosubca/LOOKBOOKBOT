from __future__ import annotations

import importlib.util
from pathlib import Path


GATE_PATH = Path(__file__).parents[1] / "automation-engine" / "lookbook-layout" / "scripts" / "lookbook_gate.py"
SPEC = importlib.util.spec_from_file_location("lookbook_gate_caption_revision_preconditions", GATE_PATH)
assert SPEC and SPEC.loader
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def test_caption_revision_can_be_copied_before_prior_review_pdf(tmp_path: Path, monkeypatch) -> None:
    """Only current map and captions proof are required for a chained revision."""
    audit = tmp_path / "draft.json"
    audit.write_text("{}", encoding="utf-8")
    state = {"session_id": "session"}
    source_hashes = {
        "registry": "registry-sha",
        "reference_pdf": "reference-sha",
        "caption_map": "caption-map-sha",
        "caption_workbook": "workbook-sha",
        "caption_provenance": "provenance-sha",
    }
    prior_map = {
        "caption_data_sha256": "captions-sha",
        **{f"{key}_sha256": value for key, value in source_hashes.items()},
    }
    evidence_calls: list[str] = []

    def load_evidence(_project: Path, _state: dict[str, object], gate_name: str):
        evidence_calls.append(gate_name)
        return prior_map if gate_name == "map" else {"passed": True} if gate_name == "captions" else None

    monkeypatch.setattr(gate, "load_evidence", load_evidence)
    monkeypatch.setattr(gate, "state_artifact", lambda _project, _state, key: Path(key))
    monkeypatch.setattr(gate, "digest", lambda path: source_hashes.get(Path(path).name, "captions-sha"))
    monkeypatch.setattr(gate, "read_json", lambda _path: {"before_caption_data_sha256": "captions-sha"})
    monkeypatch.setattr(gate, "validate_caption_revision_draft_baseline", lambda *_args: ({}, {}))
    monkeypatch.setattr(gate, "current_gate", lambda *_args: "visual")

    result = gate._validate_caption_revision_preconditions(tmp_path, state, audit)

    assert result is prior_map
    assert evidence_calls == ["map", "captions"]
