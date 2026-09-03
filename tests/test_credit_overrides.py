from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from lookbookbot.domain import ProviderKind, StageStatus
from lookbookbot.pipeline import (
    PipelineEngine,
    ReviewRequired,
    _caption_map_duplicate_pairs,
    _credit_override_batches,
    _parse_codex_rematch_candidate,
    _read_tsv,
    _write_tsv,
)
from lookbookbot.providers import CodexProvider
from lookbookbot.providers import VisionDecision
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


def test_rematch_candidate_must_be_a_label_from_the_controlled_board() -> None:
    candidates = {("W", "7"), ("M", "11")}
    assert _parse_codex_rematch_candidate(
        '{"look_id":"LOOK_007","excel_sheet":"W","excel_look_number":"7",'
        '"note":"garment=blue denim jumpsuit; bag=metallic silver bag"}',
        "LOOK_007",
        candidates,
    ) == ("W", "7")
    with pytest.raises(Exception, match="outside the supplied candidate board"):
        _parse_codex_rematch_candidate(
            '{"look_id":"LOOK_007","excel_sheet":"W","excel_look_number":"8",'
            '"note":"garment=blue denim jumpsuit; bag=metallic silver bag"}',
            "LOOK_007",
            candidates,
        )


def test_rematch_candidate_accepts_a_zero_padded_rendering_of_a_board_label() -> None:
    assert _parse_codex_rematch_candidate(
        '{"look_id":"LOOK_007","excel_sheet":"w","excel_look_number":"7",'
        '"note":"garment=blue pleated blouse; bag=straw tote"}',
        "LOOK_007",
        {("W", "007")},
    ) == ("W", "007")


def test_rematch_candidate_accepts_compact_choice_schema_and_stale_auxiliary_fields() -> None:
    assert _parse_codex_rematch_candidate(
        '{"look_id":"LOOK_007","choice":"EXCEL W:007","excel_sheet":"M",'
        '"excel_look_number":"999","note":"garment=blue pleated blouse; bag=straw tote"}',
        "LOOK_007",
        {("W", "7"), ("M", "11")},
    ) == ("W", "7")


def test_rematch_selection_repairs_an_out_of_board_provider_answer(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    engine = PipelineEngine(store)
    root = tmp_path / "project"
    folder = root / "control" / "work" / "rematch-evidence" / "LOOK_007"
    folder.mkdir(parents=True)
    (folder / "pdf-pair.jpg").write_bytes(b"proof")
    (folder / "candidates-01.jpg").write_bytes(b"proof")
    (folder / "manifest.json").write_text(
        '{"candidate_pool":["W:7"],"pdf_pair":"control/work/rematch-evidence/LOOK_007/pdf-pair.jpg",'
        '"candidate_pages":[{"path":"control/work/rematch-evidence/LOOK_007/candidates-01.jpg"}]}',
        encoding="utf-8",
    )

    class Provider:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        def run_readonly_agent(self, prompt: str, *_args, **_kwargs) -> str:
            self.prompts.append(prompt)
            if len(self.prompts) == 1:
                return '{"look_id":"LOOK_007","excel_sheet":"W","excel_look_number":"8","note":"blue blouse and straw tote"}'
            return '{"look_id":"LOOK_007","excel_sheet":"W","excel_look_number":"7","note":"blue blouse and straw tote"}'

    provider = Provider()
    assert engine._inspect_codex_rematch_candidate(provider, root, "LOOK_007") == ("W", "7")
    assert len(provider.prompts) == 2
    assert "The only valid labels are: W:7." in provider.prompts[0]
    assert "Return one of these exact labels only: W:7." in provider.prompts[1]


def test_rematch_can_transfer_a_provisional_card_and_resolve_only_the_displaced_look(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StateStore(tmp_path / "state.db")
    engine = PipelineEngine(store)
    root = tmp_path / "project"
    manifest = root / "control" / "work" / "ui-overrides" / "targeted-credit-rematch.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"attempted_candidates":{},"reserved_candidates":[]}', encoding="utf-8")

    choices = iter([
        {"LOOK_001": ("W", "1"), "LOOK_002": ("M", "1")},
        {"LOOK_002": ("W", "1")},
        {"LOOK_001": ("M", "1")},
    ])
    decisions = iter([
        {
            "LOOK_001": VisionDecision(True, "blue dress", ""),
            "LOOK_002": VisionDecision(False, "wrong dark jacket", ""),
        },
        {"LOOK_002": VisionDecision(True, "blue jumpsuit", "")},
        {"LOOK_001": VisionDecision(True, "dark jacket", "")},
    ])
    script_calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(engine.controller, "script", lambda *args, **_kwargs: script_calls.append(args))
    monkeypatch.setattr(engine, "_choose_codex_rematch_candidates", lambda *_args: next(choices))
    monkeypatch.setattr(engine, "_alternative_evidence_rows", lambda _root, selected: [
        {"look_id": look_id, "evidence_file": f"{look_id}.jpg"} for look_id in selected
    ])
    monkeypatch.setattr(engine, "_inspect_codex_credit_evidence_parallel", lambda *_args: next(decisions))

    resolved = engine._choose_and_verify_codex_rematch_candidates(
        CodexProvider(), root, ["LOOK_001", "LOOK_002"], manifest,
    )

    assert resolved == {"LOOK_002": ("W", "1"), "LOOK_001": ("M", "1")}
    assert len([call for call in script_calls if call[0] == "build_targeted_rematch_proofs.py"]) == 3
    saved = json.loads(manifest.read_text(encoding="utf-8"))
    assert saved["reserved_candidates"] == []
    assert "W:1" in saved["attempted_candidates"]["LOOK_001"]


def test_coverless_completed_gender_exports_are_returned_to_the_final_stage(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    project = _project(store, tmp_path)
    control = project.project_dir / "control"
    control.mkdir(parents=True)
    (control / "lookbook-state.json").write_text('{"expected_pages":102}', encoding="utf-8")
    (control / "final-deliverables.json").write_text(
        json.dumps({
            "status": "complete",
            "outputs": [
                {"key": "full_10mb", "page_range": "ALL"},
                {"key": "full_20mb", "page_range": "ALL"},
                {"key": "full_40mb", "page_range": "ALL"},
                {"key": "male_300ppi", "page_range": "4-49"},
                {"key": "female_300ppi", "page_range": "2-101"},
            ],
        }),
        encoding="utf-8",
    )
    store.set_stage(project.id, "final", StageStatus.PASSED)
    engine = PipelineEngine(store)

    assert engine.invalidate_coverless_gender_final_exports(project)
    assert store.stage_rows(project.id)["final"]["status"] == StageStatus.PENDING.value


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

    def simulated_rematch(_project, _provider, _targets, _manifest) -> None:
        changed = _read_tsv(map_path)
        changed[0].update({"excel_sheet": "M", "excel_look_number": "9", "excel_image": "nine.jpg", "visual_status": "CONFIRMED"})
        changed[1].update({"excel_sheet": "M", "excel_look_number": "8", "excel_image": "eight.jpg", "visual_status": "CONFIRMED"})
        _write_tsv(map_path, changed)

    provider = CodexProvider()
    monkeypatch.setattr(engine, "_resolve_codex_targeted_credit_rematch", simulated_rematch)
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
