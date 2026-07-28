from __future__ import annotations

from pathlib import Path

from lookbookbot.domain import ProviderKind, StageStatus
from lookbookbot.project_import import open_existing_project
from lookbookbot.state import StateStore


def _tsv(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_existing_project_restores_only_durable_evidence(tmp_path: Path) -> None:
    project_dir = tmp_path / "TSUM_FS-0260807"
    control = project_dir / "control"
    (control / "work").mkdir(parents=True)
    (project_dir / "TSUM_FS-0260807_LB_WA_02.indd").write_bytes(b"indd")
    _tsv(
        control / "work" / "look-register.tsv",
        "look_id\tspread_order\tpdf_spread\tleft_filename\tright_filename\tindd_left_page\tindd_right_page\n"
        "LOOK_001\t1\t1\tfull.jpg\tclose.jpg\t2\t3\n",
    )
    _tsv(
        control / "work" / "caption-map.tsv",
        "look_id\texcel_sheet\texcel_look_number\texcel_image\tevidence_file\tvisual_status\n"
        "LOOK_001\tW\t1\tcard.jpg\tcontrol/work/mapping-evidence/LOOK_001.jpg\tCONFIRMED\n",
    )
    evidence = control / "evidence"
    evidence.mkdir()
    for gate in ("map", "structure", "dates", "frames", "images", "captions", "visual", "pdf"):
        (evidence / f"{gate}.json").write_text('{"passed": true}', encoding="utf-8")
    (control / "final-deliverables.json").write_text(
        '{"passed": true, "outputs": ['
        '{"export_format": "adobe-pdf-print-jpeg-medium-v1"},'
        '{"export_format": "adobe-pdf-print-jpeg-medium-v1"},'
        '{"export_format": "adobe-pdf-print-jpeg-medium-v1"},'
        '{"export_format": "adobe-pdf-print-jpeg-medium-v1"},'
        '{"export_format": "adobe-pdf-print-jpeg-medium-v1"}'
        ']}',
        encoding="utf-8",
    )

    store = StateStore(tmp_path / "state.db")
    project = open_existing_project(store, project_dir, provider=ProviderKind.CODEX, model="")
    stages = store.stage_rows(project.id)

    assert project.show_date.isoformat() == "2026-08-07"
    assert store.looks(project.id)[0]["look_id"] == "LOOK_001"
    assert store.credits(project.id)[0]["visual_status"] == "CONFIRMED"
    assert all(row["status"] == StageStatus.PASSED.value for row in stages.values())
