from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from lookbookbot.pipeline import PipelineEngine, _parse_codex_batch_decisions, _parse_codex_rematch_candidate, _read_tsv, _write_tsv
from lookbookbot.providers import CodexProvider, ProviderError, VisionDecision
from lookbookbot.state import StateStore


def _engine(tmp_path):
    return PipelineEngine(StateStore(tmp_path / "state.db"))


def _map(root):
    (root / "control/work").mkdir(parents=True, exist_ok=True)
    rows = [{"look_id": f"LOOK_{n:03}", "excel_sheet": "W", "excel_look_number": str(n),
             "excel_image": f"{n}.jpg", "left_filename": f"{n}l.jpg", "right_filename": f"{n}r.jpg",
             "evidence_file": f"control/work/mapping-evidence/LOOK_{n:03}.jpg",
             "visual_status": "PENDING" if n == 1 else "CONFIRMED"} for n in (1, 2, 3)]
    _write_tsv(root / "control/work/caption-map.tsv", rows)
    return rows


def test_none_is_a_valid_visual_result_not_a_fake_card():
    raw = '{"look_id":"LOOK_001","choice":"NONE","note":"Reference has a brown coat and black clutch."}'
    assert _parse_codex_rematch_candidate(raw, "LOOK_001", {("M", "1")}, allow_no_match=True) is None
    with pytest.raises(ProviderError):
        _parse_codex_rematch_candidate(raw, "LOOK_001", {("M", "1")})


@pytest.mark.parametrize("extra", ['"matched":false,', ''])
def test_a_choice_with_an_explicit_nonmatch_is_not_a_candidate(extra):
    raw = '{"look_id":"LOOK_001",' + extra + '"choice":"W:8","note":"No shown card matches the reference outfit; garment=tan coat; bag=small clutch."}'
    assert _parse_codex_rematch_candidate(raw, "LOOK_001", {("W", "8")}, allow_no_match=True) is None


def test_search_moves_past_none_and_supports_more_than_four_boards(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    folder = tmp_path / "control/work/rematch-evidence/LOOK_001"
    folder.mkdir(parents=True)
    (folder / "pair.jpg").write_bytes(b"pair")
    pages = []
    for number in range(1, 6):
        image = folder / f"page-{number}.jpg"
        image.write_bytes(str(number).encode())
        pages.append({"path": image.relative_to(tmp_path).as_posix(), "cards": [f"W:{number}"]})
    (folder / "manifest.json").write_text(json.dumps({"pdf_pair": (folder / "pair.jpg").relative_to(tmp_path).as_posix(),
                                                    "candidate_pool": [f"W:{n}" for n in range(1, 6)], "candidate_pages": pages}))
    calls = []
    def inspect(_provider, _root, _look, candidates, images, **kwargs):
        calls.append(candidates)
        assert len(images) <= 3
        return ("W", "5") if ("W", "5") in candidates else None
    monkeypatch.setattr(engine, "_inspect_codex_rematch_board", inspect)
    assert engine._inspect_codex_rematch_candidate(object(), tmp_path, "LOOK_001") == ("W", "5")
    assert len(calls) == 3


def test_confirmed_owner_is_reopened_only_after_an_exact_positive_proof(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    rows = _map(tmp_path)
    manifest = tmp_path / "control/work/ui-overrides/targeted-credit-rematch.json"
    engine._initialize_rematch_manifest(manifest, rows, {r["look_id"]: r for r in rows}, ["LOOK_001"], {})
    choices = iter([{"LOOK_001": ("W", "2")}, {"LOOK_002": ("W", "1")}])
    seen = []
    monkeypatch.setattr(engine.controller, "script", lambda *args, **kwargs: seen.append(args))
    monkeypatch.setattr(engine, "_choose_codex_rematch_candidates", lambda *_: next(choices))
    monkeypatch.setattr(engine, "_alternative_evidence_rows", lambda _root, selected: [{"look_id": look} for look in selected])
    monkeypatch.setattr(engine, "_inspect_codex_credit_evidence_parallel", lambda _p, _r, batch: {
        row["look_id"]: VisionDecision(True, "garment=white jacket; bag=black tote", "") for row in batch})
    result = engine._choose_and_verify_codex_rematch_candidates(object(), tmp_path, ["LOOK_001"], manifest)
    assert result == {"LOOK_001": ("W", "2"), "LOOK_002": ("W", "1")}
    actual = _read_tsv(tmp_path / "control/work/caption-map.tsv")
    assert actual[1]["visual_status"] == "PENDING"
    assert actual[2] == rows[2]
    saved = engine._load_rematch_manifest(manifest)
    assert saved["expanded_looks"] == ["LOOK_002"]
    assert saved["expanded_prior_rows"]["LOOK_002"] == rows[1]
    assert saved["requested_looks"] == ["LOOK_001"]
    assert "--full-catalogue" in seen[0]


def test_manual_assignment_cannot_be_reopened(tmp_path):
    engine = _engine(tmp_path)
    rows = _map(tmp_path)
    manifest = tmp_path / "control/work/ui-overrides/targeted-credit-rematch.json"
    engine._initialize_rematch_manifest(manifest, rows, {r["look_id"]: r for r in rows}, ["LOOK_001"], {})
    saved = engine._load_rematch_manifest(manifest)
    saved["locked_candidates"] = ["W:2"]
    manifest.write_text(json.dumps(saved))
    engine._write_rematch_reserved(manifest, [])
    assert engine._load_rematch_manifest(manifest)["reserved_candidates"] == ["W:2"]
    from lookbookbot.pipeline import PipelineError
    with pytest.raises(PipelineError, match="ручное назначение"):
        engine._expand_credit_conflict(tmp_path, manifest, "LOOK_002")
    assert _read_tsv(tmp_path / "control/work/caption-map.tsv") == rows


def test_renderer_exposes_confirmed_card_and_large_catalogue_without_changing_map(tmp_path, monkeypatch):
    pytest.importorskip("PIL")
    sys.path.insert(0, str(Path(__file__).parents[1] / "automation-engine/lookbook-layout/scripts"))
    import build_targeted_rematch_proofs as builder
    rows = _map(tmp_path)
    (tmp_path / "_MAT/hires").mkdir(parents=True)
    index = tmp_path / "control/work/_mat/excel-images/index.tsv"
    index.parent.mkdir(parents=True, exist_ok=True)
    _write_tsv(index, [{"excel_sheet": "W", "excel_look_number": str(n), "excel_image": f"{n}.jpg"} for n in range(1, 75)])
    monkeypatch.setattr(builder, "render_pair", lambda *_: None)
    monkeypatch.setattr(builder, "render_candidate_sheet", lambda _root, _cards, target, _page: target.parent.mkdir(parents=True, exist_ok=True))
    monkeypatch.setattr(builder, "digest", lambda *_: "test-hash")
    monkeypatch.setattr(sys, "argv", ["builder", str(tmp_path), "--looks", "LOOK_001", "--full-catalogue"])
    builder.main()
    saved = json.loads((tmp_path / "control/work/rematch-evidence/LOOK_001/manifest.json").read_text())
    assert "W:2" in saved["candidate_pool"]  # confirmed but visible for discovery
    assert len(saved["candidate_pages"]) == 10
    assert all(len(page["cards"]) <= 8 for page in saved["candidate_pages"])
    assert _read_tsv(tmp_path / "control/work/caption-map.tsv") == rows


def test_legacy_forced_rejections_do_not_ban_the_correct_card(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    rows = _map(tmp_path)
    manifest = tmp_path / "control/work/ui-overrides/targeted-credit-rematch.json"
    engine._initialize_rematch_manifest(manifest, rows, {r["look_id"]: r for r in rows}, ["LOOK_001"], {})
    saved = engine._load_rematch_manifest(manifest)
    saved.pop("search_algorithm")
    saved["attempted_candidates"] = {"LOOK_001": ["W:1", "W:2", "W:3"]}
    manifest.write_text(json.dumps(saved))
    class Finished(Exception):
        pass
    def rendered(*_args, **_kwargs):
        current = engine._load_rematch_manifest(manifest)
        assert current["attempted_candidates"] == {}
        assert current["legacy_attempted_candidates"] == saved["attempted_candidates"]
        raise Finished
    monkeypatch.setattr(engine.controller, "script", rendered)
    with pytest.raises(Finished):
        engine._choose_and_verify_codex_rematch_candidates(object(), tmp_path, ["LOOK_001"], manifest)


def test_previous_accessory_rejections_are_revisited_once(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    rows = _map(tmp_path)
    manifest = tmp_path / "control/work/ui-overrides/targeted-credit-rematch.json"
    engine._initialize_rematch_manifest(manifest, rows, {r["look_id"]: r for r in rows}, ["LOOK_001"], {})
    saved = engine._load_rematch_manifest(manifest)
    saved.update(search_algorithm="full-catalogue-v1", attempted_candidates={"LOOK_001": ["W:2"]})
    manifest.write_text(json.dumps(saved))
    class Finished(Exception):
        pass
    def rendered(*_args, **_kwargs):
        migrated = engine._load_rematch_manifest(manifest)
        assert migrated["search_algorithm"] == "full-catalogue-v2"
        assert migrated["attempted_candidates"] == {}
        assert migrated["legacy_attempted_candidates"] == {"LOOK_001": ["W:2"]}
        raise Finished
    monkeypatch.setattr(engine.controller, "script", rendered)
    with pytest.raises(Finished):
        engine._choose_and_verify_codex_rematch_candidates(object(), tmp_path, ["LOOK_001"], manifest)
    saved = engine._load_rematch_manifest(manifest)
    saved["attempted_candidates"] = {"LOOK_001": ["W:3"]}
    manifest.write_text(json.dumps(saved))
    def resume(*_args, **_kwargs):
        assert engine._load_rematch_manifest(manifest)["attempted_candidates"] == {"LOOK_001": ["W:3"]}
        raise Finished
    monkeypatch.setattr(engine.controller, "script", resume)
    with pytest.raises(Finished):
        engine._choose_and_verify_codex_rematch_candidates(object(), tmp_path, ["LOOK_001"], manifest)
    assert _read_tsv(tmp_path / "control/work/caption-map.tsv") == rows


def test_optional_accessory_wording_does_not_override_structured_positive_choice():
    raw = json.dumps({"look_id": "LOOK_001", "matched": True, "choice": "M:1",
                      "note": "garment=black coat with brown fur collar; trousers=black tailored; glasses do not match; unlike the reference the Excel take has sunglasses."})
    assert _parse_codex_rematch_candidate(raw, "LOOK_001", {("M", "1")}, allow_no_match=True) == ("M", "1")


@pytest.mark.parametrize("contradiction", ["different blonde model", "white jacket versus black coat", "different leather bag construction"])
def test_credit_identity_changes_still_cannot_be_confirmed(contradiction):
    raw = json.dumps({"decisions": [{"look_id": "LOOK_001", "accepted": True,
                     "excel_observation": "Black coat and black trousers", "reference_observation": "Other outfit",
                     "contradictions": [contradiction], "note": "garment=black wool coat; shoes=black leather boots"}]})
    assert not _parse_codex_batch_decisions(raw, ["LOOK_001"], credit_notes=True)["LOOK_001"].accepted


def test_pose_aware_policy_is_scoped_to_catalogue_identity():
    from lookbookbot.ai_policy import CREDIT_IDENTITY_POLICY, READ_ONLY_VISION_POLICY
    assert "folded" in CREDIT_IDENTITY_POLICY
    assert "their absence alone" in CREDIT_IDENTITY_POLICY
    assert "clearly different" in CREDIT_IDENTITY_POLICY
    assert CREDIT_IDENTITY_POLICY not in READ_ONLY_VISION_POLICY
