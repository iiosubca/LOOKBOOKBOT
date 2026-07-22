from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from lookbookbot.domain import ProviderKind
from lookbookbot.pipeline import ReviewRequired, _credit_override_batches
from lookbookbot.state import StateStore


def _project(store: StateStore, tmp_path: Path):
    return store.save_project(
        name="demo",
        source_dir=tmp_path / "SOURCES",
        output_root=tmp_path,
        project_dir=tmp_path / "demo",
        show_date=date(2026, 8, 7),
        provider=ProviderKind.CODEX,
        model="",
    )


def _credits() -> list[dict[str, str]]:
    return [
        {"look_id": "LOOK_001", "excel_sheet": "W", "excel_look_number": "1", "visual_status": "CONFIRMED"},
        {"look_id": "LOOK_002", "excel_sheet": "W", "excel_look_number": "2", "visual_status": "CONFIRMED"},
    ]


def test_credit_override_is_confirmed_and_survives_unrelated_refresh(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    project = _project(store, tmp_path)
    store.replace_credits(project.id, _credits())

    assert store.save_credit_overrides(project.id, [{
        "look_id": "LOOK_001", "excel_sheet": "M", "excel_look_number": "9", "note": "ручная сверка",
    }]) == 1
    saved = {row["look_id"]: row for row in store.credits(project.id)}["LOOK_001"]
    assert saved["visual_status"] == "CONFIRMED"
    assert saved["manual_override"] == 1
    assert saved["excel_sheet"] == "M"

    store.replace_credits(project.id, _credits())
    preserved = {row["look_id"]: row for row in store.credits(project.id)}["LOOK_001"]
    assert preserved["excel_sheet"] == "M"
    assert preserved["excel_look_number"] == "9"
    assert preserved["manual_override"] == 1


def test_controller_refresh_consumes_matching_manual_override(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    project = _project(store, tmp_path)
    store.replace_credits(project.id, _credits())
    store.save_credit_overrides(project.id, [{
        "look_id": "LOOK_001", "excel_sheet": "M", "excel_look_number": "9", "note": "ручная сверка",
    }])

    refreshed = _credits()
    refreshed[0] = {**refreshed[0], "excel_sheet": "M", "excel_look_number": "9", "visual_status": "CONFIRMED"}
    store.replace_credits(project.id, refreshed)
    row = {row["look_id"]: row for row in store.credits(project.id)}["LOOK_001"]
    assert row["manual_override"] == 0
    assert row["excel_sheet"] == "M"


def test_duplicate_card_pairs_and_bad_numbers_are_rejected(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    project = _project(store, tmp_path)
    store.replace_credits(project.id, _credits())
    store.save_credit_overrides(project.id, [{
        "look_id": "LOOK_001", "excel_sheet": "W", "excel_look_number": "2", "note": "",
    }])
    assert store.duplicate_credit_pairs(project.id) == {("w", "2"): ["LOOK_001", "LOOK_002"]}
    with pytest.raises(ValueError, match="только из цифр"):
        store.save_credit_overrides(project.id, [{
            "look_id": "LOOK_001", "excel_sheet": "W", "excel_look_number": "2a", "note": "",
        }])


def test_swap_batches_are_atomic_and_limited_to_five() -> None:
    current = {
        "LOOK_001": {"excel_sheet": "W", "excel_look_number": "1"},
        "LOOK_002": {"excel_sheet": "W", "excel_look_number": "2"},
        "LOOK_003": {"excel_sheet": "M", "excel_look_number": "3"},
    }
    assignments = [("LOOK_001", "W", "2"), ("LOOK_002", "W", "1"), ("LOOK_003", "M", "3")]
    assert _credit_override_batches(assignments, current) == [
        [("LOOK_001", "W", "2"), ("LOOK_002", "W", "1")],
        [("LOOK_003", "M", "3")],
    ]
    with pytest.raises(ReviewRequired, match="LOOK_002"):
        _credit_override_batches([("LOOK_001", "W", "2")], current)
