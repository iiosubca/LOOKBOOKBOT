from datetime import date
from pathlib import Path
import sqlite3

from lookbookbot.domain import ProviderKind, StageStatus
from lookbookbot.state import StateStore


def test_state_resumes_first_incomplete_stage(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    project = store.save_project(
        name="TSUM_FS-0260726",
        source_dir=tmp_path / "sources",
        output_root=tmp_path,
        project_dir=tmp_path / "TSUM_FS-0260726",
        show_date=date(2026, 7, 26),
        provider=ProviderKind.CODEX,
        model="",
    )
    store.set_stage(project.id, "prepare", StageStatus.PASSED)
    store.set_stage(project.id, "looks", StageStatus.PASSED)

    assert store.first_incomplete_stage(project.id) == "credits_map"

    store.reset_from(project.id, "looks")
    assert store.first_incomplete_stage(project.id) == "looks"


def test_pre_catalog_astra_project_migrates_to_established_terra_default(tmp_path: Path) -> None:
    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as connection:
        connection.execute("""
            CREATE TABLE projects (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, source_dir TEXT NOT NULL,
                output_root TEXT NOT NULL, project_dir TEXT NOT NULL UNIQUE,
                show_date TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL DEFAULT '',
                build_mode TEXT NOT NULL DEFAULT 'full', active INTEGER NOT NULL DEFAULT 0,
                approved INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )
        """)
        connection.execute(
            "INSERT INTO projects VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("old", "old", str(tmp_path), str(tmp_path), str(tmp_path / "old"),
             "2026-09-01", "codex", "gpt-6-astra", "quick", 1, 0, "earlier", "earlier"),
        )

    store = StateStore(database)
    project = store.get_project("old")
    assert project.model == "gpt-5.6-terra"
    assert project.reasoning_effort == ""


def test_controller_reconciliation_clears_stale_prior_failure(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    project = store.save_project(
        name="TSUM_FS-0260830",
        source_dir=tmp_path / "sources",
        output_root=tmp_path,
        project_dir=tmp_path / "TSUM_FS-0260830",
        show_date=date(2026, 8, 30),
        provider=ProviderKind.CODEX,
        model="",
    )
    store.set_stage(project.id, "structure", StageStatus.FAILED, error="old template failure")

    store.confirm_prior_stages(project.id, "captions", reason="controller proof")

    rows = store.stage_rows(project.id)
    for key in ("prepare", "looks", "credits_map", "map", "structure", "dates", "frames", "images"):
        assert rows[key]["status"] == StageStatus.PASSED.value
        assert rows[key]["error"] == ""
    assert store.first_incomplete_stage(project.id) == "captions"


def test_manual_rows_are_persistent(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    project = store.save_project(
        name="demo", source_dir=tmp_path, output_root=tmp_path,
        project_dir=tmp_path / "demo", show_date=date(2026, 8, 7),
        provider=ProviderKind.OLLAMA, model="qwen3-vl",
    )
    store.replace_looks(project.id, [{
        "look_id": "LOOK_001", "spread_order": "1", "pdf_spread": "1",
        "left_filename": "full.jpg", "right_filename": "close.jpg",
        "indd_left_page": "2", "indd_right_page": "3",
    }])
    store.update_look(project.id, "LOOK_001", left_filename="new-full.jpg", status="manual")

    assert store.looks(project.id)[0]["left_filename"] == "new-full.jpg"
    assert store.looks(project.id)[0]["status"] == "manual"
