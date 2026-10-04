from __future__ import annotations

import argparse
import hashlib
import json
import runpy
from pathlib import Path


GATE_PATH = Path(__file__).parents[1] / "automation-engine" / "lookbook-layout" / "scripts" / "lookbook_gate.py"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_changed_caption_data_retires_only_credit_dependent_proof(tmp_path: Path, capsys) -> None:
    controller = runpy.run_path(str(GATE_PATH), run_name="lookbook_gate_caption_map_test")
    project = tmp_path / "project"
    work = project / "control" / "work"
    work.mkdir(parents=True)
    caption_map = work / "caption-map.tsv"
    captions = work / "caption-data.tsv"
    provenance = work / "caption-provenance.json"
    caption_map.write_text("new map\n", encoding="utf-8")
    captions.write_text("new credit text\n", encoding="utf-8")
    provenance.write_text('{"new": true}\n', encoding="utf-8")
    state = {
        "schema": 1,
        "gates": ["map", "structure", "dates", "frames", "images", "captions", "visual", "release", "pdf"],
        "session_id": "session-1",
        "build_mode": "quick",
        "caption_map": "control/work/caption-map.tsv",
        "captions": "control/work/caption-data.tsv",
        "caption_provenance": "control/work/caption-provenance.json",
    }
    (project / "control" / "lookbook-state.json").write_text(json.dumps(state), encoding="utf-8")
    evidence = project / "control" / "evidence"
    evidence.mkdir()
    (evidence / "map.json").write_text(json.dumps({
        "schema": 1, "session_id": "session-1", "gate": "map", "passed": True,
        "caption_map_sha256": "old-map", "caption_data_sha256": "old-data", "caption_provenance_sha256": "old-proof",
    }), encoding="utf-8")
    for gate in ("structure", "dates", "frames", "images", "captions", "visual", "release", "pdf"):
        (evidence / f"{gate}.json").write_text(json.dumps({"gate": gate}), encoding="utf-8")
    (project / "control" / "arms").mkdir()
    (project / "control" / "arms" / "captions.json").write_text("{}", encoding="utf-8")
    (project / "control" / "progress").mkdir()
    (project / "control" / "progress" / "captions.json").write_text("{}", encoding="utf-8")
    (project / "control" / "visual").mkdir()
    (project / "control" / "visual" / "manifest.json").write_text("{}", encoding="utf-8")
    (project / "control" / "final-deliverables.json").write_text("{}", encoding="utf-8")

    controller["command_invalidate_caption_map"](argparse.Namespace(project=str(project)))

    assert "CAPTION MAP INVALIDATED" in capsys.readouterr().out
    assert not (evidence / "map.json").exists()
    assert not (evidence / "captions.json").exists()
    assert not (evidence / "visual.json").exists()
    assert not (project / "control" / "final-deliverables.json").exists()
    assert (evidence / "structure.json").is_file()
    assert (evidence / "dates.json").is_file()
    assert (evidence / "frames.json").is_file()
    assert (evidence / "images.json").is_file()
    assert list((project / "control" / "history").glob("caption-map-change-*/evidence/captions.json"))
    assert captions.read_text(encoding="utf-8") == "new credit text\n"
    assert _sha256(caption_map) != "old-map"
