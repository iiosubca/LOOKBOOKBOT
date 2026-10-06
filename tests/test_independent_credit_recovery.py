from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from lookbookbot.pipeline import PipelineEngine, ReviewRequired, _read_tsv, _write_tsv
from lookbookbot.providers import CodexProvider, ProviderError, ProviderLimitError, VisionDecision
from lookbookbot.state import StateStore
from lookbookbot.domain import ProviderKind


def setup_engine(tmp_path, monkeypatch):
    engine = PipelineEngine(StateStore(tmp_path / "state.db"))
    mapping = tmp_path / "control/work/caption-map.tsv"
    mapping.parent.mkdir(parents=True)
    rows = [{"look_id": f"LOOK_{i:03}", "excel_sheet": "W", "excel_look_number": str(i),
             "evidence_file": f"{i}.jpg", "visual_status": "PENDING"} for i in range(1, 5)]
    _write_tsv(mapping, rows)
    manifest = tmp_path / "control/work/ui-overrides/targeted-credit-rematch.json"
    manifest.parent.mkdir(parents=True)
    engine._initialize_rematch_manifest(manifest, rows, {r["look_id"]: r for r in rows},
                                       [r["look_id"] for r in rows], {})
    commits = []

    def script(name, _root, *args, **kwargs):
        if name == "auto_caption_map.py" and args[args.index("--mode") + 1] == "select-alternatives":
            actual = _read_tsv(mapping)
            assignments = args[args.index("--assignments") + 1]
            selected = {}
            for part in assignments.split(","):
                look, pair = part.split("=")
                selected[look] = pair.split(":")
            for row in actual:
                if row["look_id"] in selected:
                    row["excel_sheet"], row["excel_look_number"] = selected[row["look_id"]]
            pairs = [(r["excel_sheet"], r["excel_look_number"]) for r in actual]
            assert len(pairs) == len(set(pairs))
            commits.append(set(selected))
            _write_tsv(mapping, actual)

    def confirm(_root, accepted):
        actual = _read_tsv(mapping)
        for row in actual:
            if row["look_id"] in {look for look, _ in accepted}:
                row["visual_status"] = "CONFIRMED"
        _write_tsv(mapping, actual)

    monkeypatch.setattr(engine.controller, "script", script)
    monkeypatch.setattr(engine, "_confirm_credit_batch", confirm)
    monkeypatch.setattr(engine, "_alternative_evidence_rows", lambda _root, selected:
                        [{"look_id": look, "evidence_file": f"{look}.jpg"} for look in selected])
    return engine, mapping, manifest, commits


def positive(rows):
    return {row["look_id"]: VisionDecision(True, "garment=black leather coat; trousers=black tailored")
            for row in rows}


def test_one_failed_search_keeps_successful_choices_available(tmp_path, monkeypatch):
    engine = PipelineEngine(StateStore(tmp_path / "state.db"))

    def choose(_p, _r, look):
        if look == "LOOK_002":
            raise ProviderError("No visually matching card")
        return ("W", "9")

    monkeypatch.setattr(engine, "_inspect_codex_rematch_candidate", choose)
    with pytest.raises(ReviewRequired) as caught:
        engine._choose_codex_rematch_candidates(object(), tmp_path, ["LOOK_001", "LOOK_002"])
    assert caught.value.partial_choices == {"LOOK_001": ("W", "9")}


def test_failed_search_does_not_hold_independent_verified_replacement(tmp_path, monkeypatch):
    engine, mapping, manifest, commits = setup_engine(tmp_path, monkeypatch)
    def choose(*_):
        error = ReviewRequired("LOOK_002 was not resolved")
        error.partial_choices = {"LOOK_001": ("M", "9")}
        raise error
    monkeypatch.setattr(engine, "_choose_codex_rematch_candidates", choose)
    monkeypatch.setattr(engine, "_inspect_codex_credit_evidence_parallel", lambda _p, _r, rows: positive(rows))
    with pytest.raises(ReviewRequired, match="LOOK_002"):
        engine._choose_and_verify_codex_rematch_candidates(object(), tmp_path,
                                                         ["LOOK_001", "LOOK_002"], manifest)
    actual = _read_tsv(mapping)
    assert actual[0]["excel_sheet"] == "M"
    assert actual[0]["excel_look_number"] == "9"
    assert actual[0]["visual_status"] == "CONFIRMED"
    assert actual[1]["visual_status"] == "PENDING"
    assert commits == [{"LOOK_001"}]


@pytest.mark.parametrize("proven,expected", [
    ({"LOOK_001": ("W", "2")}, set()),  # incomplete swap
    ({"LOOK_001": ("W", "2"), "LOOK_002": ("W", "1")}, {"LOOK_001", "LOOK_002"}),
    ({"LOOK_001": ("W", "2"), "LOOK_002": ("W", "3"), "LOOK_004": ("M", "7")}, {"LOOK_004"}),
    ({"LOOK_001": ("M", "7"), "LOOK_002": ("M", "7")}, set()),  # unresolved double claim
])
def test_only_closed_unique_components_can_be_saved(tmp_path, monkeypatch, proven, expected):
    engine, mapping, manifest, commits = setup_engine(tmp_path, monkeypatch)
    monkeypatch.setattr(engine, "_inspect_codex_credit_evidence_parallel", lambda _p, _r, rows: positive(rows))
    before = _read_tsv(mapping)
    engine._save_independent_credit_replacements(object(), tmp_path, proven, manifest, verify_ordinary=True)
    assert (set().union(*commits) if commits else set()) == expected
    actual = _read_tsv(mapping)
    for old, new in zip(before, actual):
        if old["look_id"] not in expected:
            assert old == new
        else:
            assert new["visual_status"] == "CONFIRMED"


def test_quota_during_exact_proofs_preserves_proven_selection_without_new_ai(tmp_path, monkeypatch):
    engine, mapping, manifest, commits = setup_engine(tmp_path, monkeypatch)
    monkeypatch.setattr(engine, "_choose_codex_rematch_candidates", lambda *_:
                        {"LOOK_001": ("M", "9"), "LOOK_002": ("M", "8")})
    calls = []
    def inspect(_p, _r, rows):
        calls.append(rows)
        error = ProviderLimitError("Quota ended")
        error.partial_decisions = positive(rows[:1])
        raise error
    monkeypatch.setattr(engine, "_inspect_codex_credit_evidence_parallel", inspect)
    with pytest.raises(ProviderLimitError):
        engine._choose_and_verify_codex_rematch_candidates(object(), tmp_path,
                                                         ["LOOK_001", "LOOK_002"], manifest)
    assert len(calls) == 1
    assert commits == [{"LOOK_001"}]
    actual = _read_tsv(mapping)
    assert actual[0]["excel_sheet"] == "M"
    assert actual[0]["visual_status"] == "PENDING"  # ordinary proof still needs review
    assert actual[1]["excel_sheet"] == "W"
    assert json.loads(manifest.read_text())["saved_replacements"] == {"LOOK_001": ["M", "9"]}


def test_quota_collector_exposes_completed_exact_proofs(tmp_path, monkeypatch):
    engine = PipelineEngine(StateStore(tmp_path / "state.db"))
    def inspect(_p, _r, rows):
        if rows[0]["look_id"] == "LOOK_002":
            raise ProviderLimitError("Quota ended")
        return positive(rows)
    monkeypatch.setattr(engine, "_inspect_codex_credit_batch", inspect)
    with pytest.raises(ProviderLimitError) as caught:
        engine._inspect_codex_credit_evidence_parallel(CodexProvider(), tmp_path,
                                                     [{"look_id": "LOOK_001"}, {"look_id": "LOOK_002"}])
    assert caught.value.partial_decisions["LOOK_001"].accepted


def test_unverified_choice_is_not_saved(tmp_path, monkeypatch):
    engine, mapping, manifest, commits = setup_engine(tmp_path, monkeypatch)
    before = mapping.read_bytes()
    def choose(*_):
        error = ReviewRequired("LOOK_002 unresolved")
        error.partial_choices = {"LOOK_001": ("M", "9")}
        raise error
    monkeypatch.setattr(engine, "_choose_codex_rematch_candidates", choose)
    monkeypatch.setattr(engine, "_inspect_codex_credit_evidence_parallel", lambda _p, _r, rows:
                        {"LOOK_001": VisionDecision(False, "different garment")})
    with pytest.raises(ReviewRequired):
        engine._choose_and_verify_codex_rematch_candidates(object(), tmp_path,
                                                         ["LOOK_001", "LOOK_002"], manifest)
    assert not commits
    assert mapping.read_bytes() == before


def test_completed_partial_replacement_is_visible_and_clears_only_its_checkbox(tmp_path, monkeypatch):
    engine, mapping, manifest, _ = setup_engine(tmp_path, monkeypatch)
    project = engine.store.save_project(name="fixture", source_dir=tmp_path, output_root=tmp_path,
                                        project_dir=tmp_path, show_date=date(2026, 10, 19),
                                        provider=ProviderKind.CODEX, model="gpt-6-luna")
    engine.store.replace_credits(project.id, _read_tsv(mapping))
    for look in ("LOOK_001", "LOOK_002"):
        engine.store.set_credit_rematch_requested(project.id, look, True)
    monkeypatch.setattr(engine, "_inspect_codex_credit_evidence_parallel", lambda _p, _r, rows: positive(rows))
    engine._save_independent_credit_replacements(object(), tmp_path, {"LOOK_001": ("M", "9")},
                                                manifest, verify_ordinary=True)
    assert engine.store.requested_credit_rematches(project.id) == ["LOOK_002"]
    assert engine.store.credits(project.id)[0]["excel_look_number"] == "9"
