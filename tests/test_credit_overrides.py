from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from lookbookbot.domain import ProviderKind
from lookbookbot.pipeline import (
    PipelineEngine,
    ReviewRequired,
    _caption_map_duplicate_pairs,
    _credit_override_batches,
    _read_tsv,
    _write_tsv,
)
from lookbookbot.providers import CodexProvider
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


def test_rematch_request_is_durable_and_clears_manual_override(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    project = _project(store, tmp_path)
    store.replace_credits(project.id, _credits())
    store.save_credit_overrides(project.id, [{
        "look_id": "LOOK_001", "excel_sheet": "M", "excel_look_number": "9", "note": "manual",
    }])

    store.set_credit_rematch_requested(project.id, "LOOK_001", True)
    row = {row["look_id"]: row for row in store.credits(project.id)}["LOOK_001"]
    assert row["needs_rematch"] == 1
    assert row["manual_override"] == 0
    assert store.requested_credit_rematches(project.id) == ["LOOK_001"]

    store.clear_credit_rematches(project.id, ["LOOK_001"])
    assert store.requested_credit_rematches(project.id) == []


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


def test_marked_rows_take_the_targeted_credit_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = StateStore(tmp_path / "state.db")
    project = _project(store, tmp_path)
    registry = project.project_dir / "control" / "work" / "look-register.tsv"
    registry.parent.mkdir(parents=True)
    registry.write_text("look_id\nLOOK_001\n", encoding="utf-8")
    store.replace_credits(project.id, _credits())
    store.set_credit_rematch_requested(project.id, "LOOK_001", True)
    engine = PipelineEngine(store)
    called: list[list[str]] = []

    def fake_targeted(_project, _provider, requested: list[str]) -> str:
        called.append(requested)
        return "targeted"

    monkeypatch.setattr(engine, "_targeted_credit_rematch", fake_targeted)
    assert engine._credits_map(project, object()) == "targeted"
    assert called == [["LOOK_001"]]


def test_targeted_rematch_restores_every_unmarked_map_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = StateStore(tmp_path / "state.db")
    project = _project(store, tmp_path)
    map_path = project.project_dir / "control" / "work" / "caption-map.tsv"
    map_path.parent.mkdir(parents=True)
    rows = [
        {
            "look_id": "LOOK_001", "excel_sheet": "W", "excel_look_number": "1", "excel_image": "one.jpg",
            "left_filename": "one-full.jpg", "right_filename": "one-close.jpg", "evidence_file": "control/work/mapping-evidence/LOOK_001.jpg", "visual_status": "CONFIRMED",
        },
        {
            "look_id": "LOOK_002", "excel_sheet": "W", "excel_look_number": "2", "excel_image": "two.jpg",
            "left_filename": "two-full.jpg", "right_filename": "two-close.jpg", "evidence_file": "control/work/mapping-evidence/LOOK_002.jpg", "visual_status": "CONFIRMED",
        },
    ]
    _write_tsv(map_path, rows)
    store.replace_credits(project.id, rows)
    store.set_credit_rematch_requested(project.id, "LOOK_001", True)
    engine = PipelineEngine(store)
    monkeypatch.setattr(engine.controller, "script", lambda *_args, **_kwargs: None)

    def simulated_agent(*_args, **_kwargs) -> str:
        changed = _read_tsv(map_path)
        changed[0].update({"excel_sheet": "M", "excel_look_number": "9", "excel_image": "nine.jpg", "visual_status": "CONFIRMED"})
        changed[1].update({"excel_sheet": "M", "excel_look_number": "8", "excel_image": "eight.jpg", "visual_status": "CONFIRMED"})
        _write_tsv(map_path, changed)
        return "confirmed"

    provider = CodexProvider()
    monkeypatch.setattr(provider, "run_agent", simulated_agent)
    result = engine._targeted_credit_rematch(project, provider, ["LOOK_001"])
    after = {row["look_id"]: row for row in _read_tsv(map_path)}

    assert "LOOK_001" in result
    assert after["LOOK_001"]["excel_sheet"] == "M"
    assert after["LOOK_002"]["excel_sheet"] == "W"
    assert after["LOOK_002"]["excel_look_number"] == "2"
    assert store.requested_credit_rematches(project.id) == []


def test_duplicate_caption_map_is_repaired_before_caption_data_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = StateStore(tmp_path / "state.db")
    project = _project(store, tmp_path)
    map_path = project.project_dir / "control" / "work" / "caption-map.tsv"
    map_path.parent.mkdir(parents=True)
    rows = [
        {
            "look_id": "LOOK_001", "excel_sheet": "W", "excel_look_number": "6", "excel_image": "six-a.jpg",
            "left_filename": "one-full.jpg", "right_filename": "one-close.jpg", "evidence_file": "control/work/mapping-evidence/LOOK_001.jpg", "visual_status": "CONFIRMED",
        },
        {
            "look_id": "LOOK_002", "excel_sheet": "W", "excel_look_number": "6", "excel_image": "six-b.jpg",
            "left_filename": "two-full.jpg", "right_filename": "two-close.jpg", "evidence_file": "control/work/mapping-evidence/LOOK_002.jpg", "visual_status": "CONFIRMED",
        },
    ]
    _write_tsv(map_path, rows)
    engine = PipelineEngine(store)

    def repair(_name: str, *_args, **_kwargs) -> None:
        if _name != "auto_caption_map.py":
            return
        repaired = _read_tsv(map_path)
        repaired[1].update({"excel_look_number": "5", "excel_image": "five.jpg", "visual_status": "PENDING"})
        _write_tsv(map_path, repaired)

    monkeypatch.setattr(engine.controller, "script", repair)

    def confirm(_project, _provider):
        repaired = _read_tsv(map_path)
        repaired[1]["visual_status"] = "CONFIRMED"
        _write_tsv(map_path, repaired)
        return []

    monkeypatch.setattr(engine, "_confirm_codex_credit_proofs_parallel", confirm)
    repaired = engine._repair_duplicate_caption_map(project, CodexProvider())

    assert _caption_map_duplicate_pairs(repaired) == {}
    assert repaired[1]["excel_look_number"] == "5"
    assert repaired[1]["visual_status"] == "CONFIRMED"


def test_map_gate_skips_reaccepting_an_unchanged_confirmed_map(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = StateStore(tmp_path / "state.db")
    project = _project(store, tmp_path)
    engine = PipelineEngine(store)
    calls: list[str] = []

    monkeypatch.setattr("lookbookbot.pipeline.evidence_passed", lambda _root, gate: gate == "map")
    monkeypatch.setattr(engine.controller, "gate", lambda *args, **kwargs: calls.append(str(args[0])))

    result = engine._map_gate(project, CodexProvider())

    assert "уже подтверждена" in result
    assert calls == []


def test_map_gate_does_not_revalidate_after_mapper_already_passed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = StateStore(tmp_path / "state.db")
    project = _project(store, tmp_path)
    project.project_dir.mkdir()
    control = project.project_dir / "control"
    control.mkdir()
    (control / "lookbook-state.json").write_text("{}", encoding="utf-8")
    store.replace_looks(project.id, [{"look_id": "LOOK_001", "spread_order": "1"}])
    engine = PipelineEngine(store)
    calls: list[str] = []
    mapper_completed = False

    def passed(_root: Path, gate: str) -> bool:
        return gate == "map" and mapper_completed

    def completed_mapper(*_args, **_kwargs) -> None:
        nonlocal mapper_completed
        mapper_completed = True

    monkeypatch.setattr("lookbookbot.pipeline.evidence_passed", passed)
    monkeypatch.setattr(engine, "_confirm_codex_reference_proofs_parallel", completed_mapper)
    monkeypatch.setattr(engine.controller, "gate", lambda *args, **kwargs: calls.append(str(args[0])))

    result = engine._map_gate(project, CodexProvider())

    assert result
    assert calls == ["prepare-reference-order"]
