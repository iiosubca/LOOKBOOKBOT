from __future__ import annotations

import json
import threading
from collections import Counter
from datetime import date

import pytest

from lookbookbot.domain import BuildMode, ProviderKind
from lookbookbot.pipeline import PipelineEngine, _read_tsv, _write_tsv
from lookbookbot.providers import CodexProvider, ProviderLimitError
from lookbookbot.state import StateStore
from lookbookbot.vision_checkpoints import VisionCheckpoint, set_review_generation


class FakeVision(CodexProvider):
    def __init__(self, model="gpt-6-luna", fail=None):
        super().__init__(model, reasoning_effort="low")
        self.calls = Counter()
        self.fail = fail
        self.first_done = threading.Event()

    def run_readonly_decision_vision(self, prompt, root, *, images, look_ids, **kwargs):
        look = look_ids[0]
        self.calls[look] += 1
        if look == self.fail:
            assert self.first_done.wait(5)
            raise ProviderLimitError("Quota ended")
        self.first_done.set()
        return json.dumps({"decisions": [{"look_id": look, "accepted": True,
                           "note": "garment=blue coat; bag=black leather tote"}]})

    def run_readonly_closed_board_vision(self, prompt, root, *, look_id, **kwargs):
        self.calls[look_id] += 1
        if look_id == self.fail:
            assert self.first_done.wait(5)
            raise ProviderLimitError("Quota ended")
        self.first_done.set()
        return json.dumps({"look_id": look_id, "choice": "W:1", "note": "garment=blue coat; bag=black tote"})


def _engine(tmp_path):
    store = StateStore(tmp_path / "state.db")
    project = store.save_project(name="test", source_dir=tmp_path, output_root=tmp_path,
                                 project_dir=tmp_path / "book", show_date=date(2026, 10, 4),
                                 provider=ProviderKind.CODEX, model="gpt-6-luna", build_mode=BuildMode.QUICK)
    engine = PipelineEngine(store)
    root = project.project_dir
    (root / "control/work").mkdir(parents=True)
    rows = []
    for number in (1, 2):
        look = f"LOOK_{number:03}"
        proof = root / f"cards/{look}.jpg"
        proof.parent.mkdir(parents=True, exist_ok=True)
        proof.write_bytes(f"pixels-{look}".encode())
        rows.append({"look_id": look, "visual_status": "PENDING", "evidence_file": f"cards/{look}.jpg"})
    _write_tsv(root / "control/work/caption-map.tsv", rows)
    return engine, project, root, rows


def test_successful_alternative_check_is_reused_after_a_quota_stop(tmp_path):
    engine, _, root, rows = _engine(tmp_path)
    failing = FakeVision(fail="LOOK_002")
    with pytest.raises(ProviderLimitError):
        engine._inspect_codex_credit_evidence_parallel(failing, root, rows)
    assert failing.calls == {"LOOK_001": 1, "LOOK_002": 1}
    resumed = FakeVision()
    fresh_engine = PipelineEngine(engine.store)
    decisions = fresh_engine._inspect_codex_credit_evidence_parallel(resumed, root, rows)
    assert all(decision.accepted for decision in decisions.values())
    assert resumed.calls == {"LOOK_002": 1}


def test_completed_primary_check_is_committed_before_reporting_quota(tmp_path, monkeypatch):
    engine, project, root, rows = _engine(tmp_path)

    def commit(_root, accepted):
        actual = _read_tsv(root / "control/work/caption-map.tsv")
        for row in actual:
            if row["look_id"] in {x[0] for x in accepted}:
                row["visual_status"] = "CONFIRMED"
        _write_tsv(root / "control/work/caption-map.tsv", actual)

    monkeypatch.setattr(engine, "_confirm_credit_batch", commit)
    with pytest.raises(ProviderLimitError):
        engine._confirm_codex_credit_proofs_parallel(project, FakeVision(fail="LOOK_002"))
    saved = _read_tsv(root / "control/work/caption-map.tsv")
    assert [x["visual_status"] for x in saved] == ["CONFIRMED", "PENDING"]
    resumed = FakeVision()
    assert engine._confirm_codex_credit_proofs_parallel(project, resumed) == []
    assert resumed.calls == {"LOOK_002": 1}


def test_changed_pixels_model_effort_and_manual_review_do_not_reuse_old_answer(tmp_path):
    engine, _, root, rows = _engine(tmp_path)
    row = [rows[0]]
    provider = FakeVision()
    engine._inspect_codex_credit_batch(provider, root, row)
    engine._inspect_codex_credit_batch(provider, root, row)
    assert provider.calls["LOOK_001"] == 1
    (root / row[0]["evidence_file"]).write_bytes(b"changed pixels")
    engine._inspect_codex_credit_batch(provider, root, row)
    assert provider.calls["LOOK_001"] == 2
    other = FakeVision("gpt-6.1-sol")
    engine._inspect_codex_credit_batch(other, root, row)
    assert other.calls["LOOK_001"] == 1
    provider.reasoning_effort = "high"
    engine._inspect_codex_credit_batch(provider, root, row)
    assert provider.calls["LOOK_001"] == 3
    set_review_generation(root, "LOOK_001", "operator-request-2")
    engine._inspect_codex_credit_batch(provider, root, row)
    assert provider.calls["LOOK_001"] == 4


def test_successful_candidate_choice_is_reused_after_restart(tmp_path):
    engine, _, root, _ = _engine(tmp_path)
    for look in ("LOOK_001", "LOOK_002"):
        folder = root / f"control/work/rematch-evidence/{look}"
        folder.mkdir(parents=True)
        (folder / "pair.jpg").write_bytes(b"pair")
        (folder / "board.jpg").write_bytes(b"board")
        (folder / "manifest.json").write_text(json.dumps({
            "candidate_pool": ["W:1"], "pdf_pair": f"control/work/rematch-evidence/{look}/pair.jpg",
            "candidate_pages": [{"path": f"control/work/rematch-evidence/{look}/board.jpg"}],
        }))
    with pytest.raises(ProviderLimitError):
        engine._choose_codex_rematch_candidates(FakeVision(fail="LOOK_002"), root, ["LOOK_001", "LOOK_002"])
    resumed = FakeVision()
    engine._choose_codex_rematch_candidates(resumed, root, ["LOOK_001", "LOOK_002"])
    assert resumed.calls == {"LOOK_002": 1}


def test_completed_legacy_audit_can_recover_but_failed_or_other_prompt_cannot(tmp_path):
    engine, _, root, rows = _engine(tmp_path)
    provider = FakeVision("gpt-6.1-sol")
    checkpoint = VisionCheckpoint(root, provider, "credit comparison", [root / rows[0]["evidence_file"]], ["LOOK_001"])
    audit = root / "control/work/ai-exchanges/legacy.json"
    audit.parent.mkdir(parents=True)
    record = {"status": "failed", "transport": "codex-app-server", "requested_model": provider.model,
              "requested_effort": "low", "prompt": checkpoint.prompt,
              "images": [{"sha256": checkpoint.hashes[0]}], "text": '{"accepted":true}'}
    audit.write_text(json.dumps(record))
    assert checkpoint.read() is None
    record["status"] = "completed"
    audit.write_text(json.dumps(record))
    assert checkpoint.read() == record["text"]
    assert VisionCheckpoint(root, provider, "another task", [root / rows[0]["evidence_file"]], ["LOOK_001"]).read() is None
    set_review_generation(root, "LOOK_001", "fresh-human-request")
    assert VisionCheckpoint(root, provider, "credit comparison", [root / rows[0]["evidence_file"]], ["LOOK_001"]).read() is None


def test_stage_quota_stop_keeps_progress_visible_and_continue_skips_old_stages(tmp_path, monkeypatch):
    from lookbookbot.domain import StageStatus
    engine, project, root, rows = _engine(tmp_path)
    engine.store.set_stage(project.id, "prepare", StageStatus.PASSED)
    engine.store.set_stage(project.id, "looks", StageStatus.PASSED)
    monkeypatch.setattr(engine, "_provider", lambda _: FakeVision())
    visited = []

    def stopped_stage(_project, key, _provider):
        visited.append(key)
        changed = _read_tsv(root / "control/work/caption-map.tsv")
        changed[0]["visual_status"] = "CONFIRMED"
        _write_tsv(root / "control/work/caption-map.tsv", changed)
        raise ProviderLimitError("Quota ended")

    monkeypatch.setattr(engine, "_run_stage", stopped_stage)
    result = engine.run(project, stop_after="credits_map")
    assert result.stopped_at == "credits_map"
    assert visited == ["credits_map"]
    assert engine.store.stage_rows(project.id)["credits_map"]["status"] == "review"
    assert [row["visual_status"] for row in engine.store.credits(project.id)] == ["CONFIRMED", "PENDING"]


def test_targeted_rematch_resumes_history_and_does_not_reset_a_completed_target(tmp_path, monkeypatch):
    engine, project, root, rows = _engine(tmp_path)
    mapping = root / "control/work/caption-map.tsv"
    for number, row in enumerate(rows, 1):
        row.update(excel_sheet="W", excel_look_number=str(number), visual_status="CONFIRMED")
    _write_tsv(mapping, rows)
    engine.store.replace_credits(project.id, rows)
    for look in ("LOOK_001", "LOOK_002"):
        engine.store.set_credit_rematch_requested(project.id, look, True)
    observation = root / "control/work/caption-map-observations/LOOK_002.json"
    visited = []

    def resolve(_project, _provider, targets, manifest):
        visited.append(targets.copy())
        if len(visited) == 1:
            engine._append_rematch_attempts(manifest, {"LOOK_001": ("W", "7"), "LOOK_002": ("W", "9")})
            engine._write_rematch_reserved(manifest, {("W", "7")})
            actual = _read_tsv(mapping)
            actual[0].update(excel_look_number="7", visual_status="CONFIRMED")
            _write_tsv(mapping, actual)
            observation.parent.mkdir(parents=True)
            observation.write_text('{"already_inspected":true}')
            raise ProviderLimitError("Quota ended")
        history = engine._load_rematch_manifest(manifest)
        # An operator marking a row is not visual proof that its prior card
        # can never match. Only actual negative comparisons are excluded.
        assert history["attempted_candidates"]["LOOK_002"] == ["W:9"]
        assert history["prior_assignments"]["LOOK_002"]["excel_look_number"] == "2"
        assert history["reserved_candidates"] == ["W:7"]
        assert observation.is_file()
        actual = _read_tsv(mapping)
        assert actual[0]["visual_status"] == "CONFIRMED"
        actual[1].update(excel_look_number="9", visual_status="CONFIRMED")
        _write_tsv(mapping, actual)

    monkeypatch.setattr(engine, "_resolve_codex_targeted_credit_rematch", resolve)
    monkeypatch.setattr(engine, "_repair_duplicate_caption_map", lambda *_: _read_tsv(mapping))
    monkeypatch.setattr(engine.controller, "script", lambda *args, **kwargs: "")
    with pytest.raises(ProviderLimitError):
        engine._targeted_credit_rematch(project, FakeVision(), ["LOOK_001", "LOOK_002"])
    engine._targeted_credit_rematch(project, FakeVision(), ["LOOK_001", "LOOK_002"])
    assert visited == [["LOOK_001", "LOOK_002"], ["LOOK_002"]]
    assert [row["excel_look_number"] for row in _read_tsv(mapping)] == ["7", "9"]
    assert engine.store.requested_credit_rematches(project.id) == []


@pytest.mark.parametrize("damaged", ["", "unexpected\nvalue\n", "look_id\nLOOK_001\textra\n"])
def test_bad_progress_file_does_not_hide_quota_or_erase_the_previous_ui(tmp_path, monkeypatch, damaged):
    from lookbookbot.domain import StageStatus
    engine, project, root, rows = _engine(tmp_path)
    engine.store.replace_credits(project.id, rows)
    before = engine.store.credits(project.id)
    (root / "control/work/caption-map.tsv").write_text(damaged)
    engine.store.set_stage(project.id, "prepare", StageStatus.PASSED)
    engine.store.set_stage(project.id, "looks", StageStatus.PASSED)
    monkeypatch.setattr(engine, "_provider", lambda _: FakeVision())

    def stop(*args):
        raise ProviderLimitError("Original quota message")

    monkeypatch.setattr(engine, "_run_stage", stop)
    result = engine.run(project, stop_after="credits_map")
    assert result.message == "Original quota message"
    assert engine.store.stage_rows(project.id)["credits_map"]["status"] == "review"
    assert engine.store.credits(project.id) == before


def test_incomplete_progress_file_cannot_remove_a_look_from_the_ui(tmp_path, monkeypatch):
    from lookbookbot.domain import StageStatus
    engine, project, root, rows = _engine(tmp_path)
    engine.store.replace_credits(project.id, rows)
    before = engine.store.credits(project.id)
    _write_tsv(root / "control/work/look-register.tsv", [{"look_id": row["look_id"]} for row in rows])
    _write_tsv(root / "control/work/caption-map.tsv", rows[:1])
    engine.store.set_stage(project.id, "prepare", StageStatus.PASSED)
    engine.store.set_stage(project.id, "looks", StageStatus.PASSED)
    monkeypatch.setattr(engine, "_provider", lambda _: FakeVision())

    def stop(*args):
        raise ProviderLimitError("Quota ended")

    monkeypatch.setattr(engine, "_run_stage", stop)
    engine.run(project, stop_after="credits_map")
    assert engine.store.credits(project.id) == before


def test_quota_during_note_repair_commits_all_other_successful_checks(tmp_path, monkeypatch):
    from lookbookbot.controller import CommandError
    from lookbookbot.providers import VisionDecision
    engine, project, root, rows = _engine(tmp_path)
    for number in range(3, 8):
        rows.append({"look_id": f"LOOK_{number:03}", "visual_status": "PENDING", "evidence_file": f"LOOK_{number:03}.jpg"})
    mapping = root / "control/work/caption-map.tsv"
    _write_tsv(mapping, rows)
    repairs = []
    vague = {"LOOK_001", "LOOK_003"}

    def inspect(_provider, _root, batch, strict_notes=False):
        if strict_notes:
            repairs.append([row["look_id"] for row in batch])
            raise ProviderLimitError("Quota ended during note repair")
        return {row["look_id"]: VisionDecision(True, "vague observation without garment details" if row["look_id"] in vague else "garment=blue coat; bag=black tote")
                for row in batch}

    def commit(_root, accepted):
        for look, note in accepted:
            if note.startswith("vague"):
                raise CommandError(f"{look}: visual observation is not specific enough")
        actual = _read_tsv(mapping)
        for row in actual:
            if row["look_id"] in {pair[0] for pair in accepted}:
                row["visual_status"] = "CONFIRMED"
        _write_tsv(mapping, actual)

    monkeypatch.setattr(engine, "_inspect_codex_credit_batch", inspect)
    monkeypatch.setattr(engine, "_confirm_credit_batch", commit)
    with pytest.raises(ProviderLimitError, match="during note repair"):
        engine._confirm_codex_credit_proofs_parallel(project, FakeVision())
    assert repairs == [["LOOK_001"]]
    assert {row["look_id"] for row in _read_tsv(mapping) if row["visual_status"] != "CONFIRMED"} == vague


def test_multiple_weak_notes_in_one_batch_are_repaired_without_losing_valid_rows(tmp_path, monkeypatch):
    from lookbookbot.controller import CommandError
    from lookbookbot.providers import VisionDecision
    engine, project, root, rows = _engine(tmp_path)
    for number in range(3, 6):
        rows.append({"look_id": f"LOOK_{number:03}", "visual_status": "PENDING", "evidence_file": f"LOOK_{number:03}.jpg"})
    mapping = root / "control/work/caption-map.tsv"
    _write_tsv(mapping, rows)
    repaired = []
    weak = {"LOOK_001", "LOOK_003", "LOOK_005"}

    def inspect(_provider, _root, batch, strict_notes=False):
        if strict_notes:
            repaired.extend(row["look_id"] for row in batch)
        return {row["look_id"]: VisionDecision(True, "vague observation without garment details" if not strict_notes and row["look_id"] in weak else "garment=mustard plaid cape; bag=plaid tote") for row in batch}

    def commit(_root, accepted):
        for look, note in accepted:
            if note.startswith("vague"):
                raise CommandError(f"{look}: visual observation is not specific enough")
        actual = _read_tsv(mapping)
        for row in actual:
            if row["look_id"] in {pair[0] for pair in accepted}:
                row["visual_status"] = "CONFIRMED"
        _write_tsv(mapping, actual)

    monkeypatch.setattr(engine, "_inspect_codex_credit_batch", inspect)
    monkeypatch.setattr(engine, "_confirm_credit_batch", commit)
    assert engine._confirm_codex_credit_proofs_parallel(project, FakeVision()) == []
    assert set(repaired) == weak
    assert all(row["visual_status"] == "CONFIRMED" for row in _read_tsv(mapping))


def test_invalid_cached_credit_note_is_rechecked_and_not_confirmed(tmp_path):
    engine, _, root, rows = _engine(tmp_path)

    class Provider(FakeVision):
        def run_readonly_decision_vision(self, prompt, root, **kwargs):
            self.calls[kwargs["look_ids"][0]] += 1
            return json.dumps({"decisions": [{"look_id": kwargs["look_ids"][0], "accepted": True,
                              "note": "the proposed images match and show the same outfit" if sum(self.calls.values()) == 1 else "garment=mustard plaid cape; bag=plaid tote"}]})

    provider = Provider()
    result = engine._inspect_codex_credit_batch(provider, root, rows[:1])
    assert result["LOOK_001"].accepted
    assert provider.calls == {"LOOK_001": 2}
    resumed = FakeVision()
    assert engine._inspect_codex_credit_batch(resumed, root, rows[:1])["LOOK_001"].accepted
    assert not resumed.calls
