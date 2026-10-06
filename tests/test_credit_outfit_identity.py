from __future__ import annotations

import hashlib
import json
from datetime import date

import pytest

from lookbookbot.ai_policy import CREDIT_IDENTITY_POLICY, CREDIT_POLICY_MARKER, read_only_vision_prompt
from lookbookbot.pipeline import PipelineEngine, _parse_codex_batch_decisions, _read_tsv, _write_tsv
from lookbookbot.providers import CodexProvider, ProviderError, ProviderLimitError
from lookbookbot.state import StateStore, credit_product_warning


class Decisions(CodexProvider):
    def __init__(self, replies):
        super().__init__("test-model", reasoning_effort="low")
        self.replies = iter(replies)
        self.prompts = []

    def run_readonly_decision_vision(self, prompt, root, **kwargs):
        self.prompts.append(prompt)
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return json.dumps({"decisions": [reply]})


def decision(accepted, *, contradictions=None, styling=None):
    return {"look_id": "LOOK_001", "accepted": accepted,
            "excel_observation": "brown tweed jacket with belt and matching slit skirt",
            "reference_observation": "brown tweed jacket with belt and matching slit skirt",
            "contradictions": contradictions or [], "styling_differences": styling or [],
            "note": "garment=brown tweed belted jacket; skirt=matching slit skirt"}


def fixture(tmp_path):
    engine = PipelineEngine(StateStore(tmp_path / "test.db"))
    proof = tmp_path / "cards/proof.jpg"
    proof.parent.mkdir()
    proof.write_bytes(b"isolated proof pixels")
    return engine, [{"look_id": "LOOK_001", "evidence_file": "cards/proof.jpg"}]


def test_credit_contract_distinguishes_catalogue_outfit_from_exact_photo():
    prompt = read_only_vision_prompt(CREDIT_IDENTITY_POLICY)
    assert "DIFFERENT PERSON" in prompt
    assert "For PDF-to-hires photograph verification, still require the exact" in prompt
    assert "different model," not in CREDIT_IDENTITY_POLICY
    assert "styling_differences" in prompt


def test_rejection_gets_one_independent_pixel_audit_and_both_are_cached(tmp_path):
    engine, rows = fixture(tmp_path)
    provider = Decisions([decision(False, contradictions=["different person"]), decision(True)])
    assert engine._inspect_codex_credit_batch(provider, tmp_path, rows)["LOOK_001"].accepted
    assert len(provider.prompts) == 2
    assert "REJECTION_AUDIT" not in provider.prompts[0]
    assert "REJECTION_AUDIT" in provider.prompts[1]
    resumed = Decisions([])
    assert engine._inspect_codex_credit_batch(resumed, tmp_path, rows)["LOOK_001"].accepted
    assert resumed.prompts == []


def test_real_garment_mismatch_is_never_force_confirmed(tmp_path):
    engine, rows = fixture(tmp_path)
    provider = Decisions([decision(False, contradictions=["white blazer versus black leather coat"])] * 2)
    assert not engine._inspect_codex_credit_batch(provider, tmp_path, rows)["LOOK_001"].accepted
    assert len(provider.prompts) == 2  # No infinite reasoning/retry loop.
    assert not _parse_codex_batch_decisions(json.dumps({"decisions": [
        decision(True, contradictions=["different main garment"])]}), ["LOOK_001"], credit_notes=True)["LOOK_001"].accepted


@pytest.mark.parametrize("resume", [False, True])
def test_quota_during_rejection_audit_propagates_without_format_retry(tmp_path, resume):
    engine, rows = fixture(tmp_path)
    if resume:
        provider = Decisions([decision(False), ProviderLimitError("quota")])
        with pytest.raises(ProviderLimitError):
            engine._inspect_codex_credit_batch(provider, tmp_path, rows)
        provider = Decisions([ProviderLimitError("quota")])
    else:
        provider = Decisions([decision(False), ProviderLimitError("quota")])
    with pytest.raises(ProviderLimitError):
        engine._inspect_codex_credit_batch(provider, tmp_path, rows)
    assert len(provider.prompts) == (1 if resume else 2)


def test_real_shoe_substitution_is_a_durable_warning_not_a_wrong_catalogue_card(tmp_path):
    engine, rows = fixture(tmp_path)
    provider = Decisions([decision(True, styling=["Excel boots; reference brown pumps"])])
    result = engine._inspect_codex_credit_batch(provider, tmp_path, rows)["LOOK_001"]
    assert result.accepted
    assert "Проверить товары: Excel boots; reference brown pumps" in result.note
    assert len(provider.prompts) == 1
    assert "styling_differences" in result.raw


def test_quota_in_secondary_audit_preserves_other_primary_successes(tmp_path):
    engine, rows = fixture(tmp_path)
    rows.append({"look_id": "LOOK_002", "evidence_file": rows[0]["evidence_file"]})
    class Mixed(Decisions):
        def run_readonly_decision_vision(self, prompt, root, **kwargs):
            self.prompts.append(prompt)
            if "REJECTION_AUDIT" in prompt:
                raise ProviderLimitError("quota")
            rejected = {**decision(False), "look_id": "LOOK_002"}
            return json.dumps({"decisions": [decision(True), rejected]})
    with pytest.raises(ProviderLimitError) as caught:
        engine._inspect_codex_credit_batch(Mixed([]), tmp_path, rows)
    assert set(caught.value.partial_decisions) == {"LOOK_001"}
    assert caught.value.partial_decisions["LOOK_001"].accepted


def test_styling_warning_does_not_override_a_garment_contradiction():
    raw = json.dumps({"decisions": [decision(True, contradictions=["different skirt"], styling=["different shoes"])]})
    assert not _parse_codex_batch_decisions(raw, ["LOOK_001"], credit_notes=True)["LOOK_001"].accepted


def test_styling_text_cannot_break_controller_note_transport():
    raw = json.dumps({"decisions": [decision(True, styling=["boots\nversus pumps || extra text"])]})
    note = _parse_codex_batch_decisions(raw, ["LOOK_001"], credit_notes=True)["LOOK_001"].note
    assert "||" not in note and "\n" not in note
    assert "Проверить товары:" in note


def test_malformed_styling_is_not_silently_accepted():
    reply = decision(True)
    reply["styling_differences"] = "unknown"
    with pytest.raises(ProviderError, match="styling_differences"):
        _parse_codex_batch_decisions(json.dumps({"decisions": [reply]}), ["LOOK_001"], credit_notes=True)


def test_old_policy_bans_are_archived_once_only_for_unfinished_targets(tmp_path, monkeypatch):
    engine = PipelineEngine(StateStore(tmp_path / "test.db"))
    rows = [{"look_id": "LOOK_001", "excel_sheet": "W", "excel_look_number": "1", "visual_status": "PENDING"},
            {"look_id": "LOOK_002", "excel_sheet": "W", "excel_look_number": "2", "visual_status": "CONFIRMED"}]
    mapping = tmp_path / "control/work/caption-map.tsv"
    mapping.parent.mkdir(parents=True)
    _write_tsv(mapping, rows)
    manifest = tmp_path / "control/work/ui-overrides/targeted-credit-rematch.json"
    engine._initialize_rematch_manifest(manifest, rows, {r["look_id"]: r for r in rows}, ["LOOK_001"], {})
    data = engine._load_rematch_manifest(manifest)
    data.pop("credit_identity_policy")
    data["attempted_candidates"] = {"LOOK_001": ["W:1"], "LOOK_002": ["W:2"]}
    manifest.write_text(json.dumps(data))
    class Halt(Exception):
        pass
    monkeypatch.setattr(engine.controller, "script", lambda *args, **kwargs: (_ for _ in ()).throw(Halt()))
    with pytest.raises(Halt):
        engine._discover_credit_replacements(object(), tmp_path, ["LOOK_001"], manifest, {})
    saved = engine._load_rematch_manifest(manifest)
    assert saved["credit_identity_policy"] == CREDIT_POLICY_MARKER
    assert saved["attempted_candidates"] == {"LOOK_002": ["W:2"]}
    assert saved["identity_policy_history"][0]["attempted_candidates"] == {"LOOK_001": ["W:1"]}
    with pytest.raises(Halt):
        engine._discover_credit_replacements(object(), tmp_path, ["LOOK_001"], manifest, {})
    assert engine._load_rematch_manifest(manifest) == saved
    assert _read_tsv(mapping) == rows


def test_cached_none_is_not_reported_as_a_successful_candidate(tmp_path):
    engine, _ = fixture(tmp_path)
    logs = []
    engine.log = logs.append
    class Board(CodexProvider):
        def __init__(self):
            super().__init__("test-model")
            self.calls = 0
        def run_readonly_closed_board_vision(self, *args, **kwargs):
            self.calls += 1
            return json.dumps({"look_id": "LOOK_001", "matched": False, "choice": "NONE", "note": "garment=black coat; trousers=black wide trousers"})
    provider = Board()
    args = (provider, tmp_path, "LOOK_001", {("W", "1")}, [tmp_path / "cards/proof.jpg"])
    assert engine._inspect_codex_rematch_board(*args) is None
    assert engine._inspect_codex_rematch_board(*args) is None
    assert provider.calls == 1
    assert "совпадения нет" in logs[-1]
    assert "восстановлен кандидат" not in logs[-1]


@pytest.mark.parametrize("changed", [None, "pixels", "pair", "pending", "damaged"])
def test_product_warning_is_bound_to_current_confirmed_card_without_editing_operator_notes(tmp_path, changed):
    engine, rows = fixture(tmp_path)
    row = {**rows[0], "excel_sheet": "W", "excel_look_number": "3", "excel_image": "excel.png", "visual_status": "CONFIRMED"}
    proof = tmp_path / row["evidence_file"]
    observation = tmp_path / "control/work/caption-map-observations/LOOK_001.json"
    observation.parent.mkdir(parents=True)
    data = {**row, "evidence_sha256": hashlib.sha256(proof.read_bytes()).hexdigest(),
            "visual_identity_description": "garment=brown suit; shoes=brown pumps Проверить товары: boots versus pumps"}
    observation.write_text(json.dumps(data), encoding="utf-8")
    if changed == "pixels":
        proof.write_bytes(b"new photograph")
    elif changed == "pair":
        row["excel_look_number"] = "7"
    elif changed == "pending":
        row["visual_status"] = "PENDING"
    elif changed == "damaged":
        observation.write_text("not-json")
    assert credit_product_warning(tmp_path, row) == ("boots versus pumps" if changed is None else "")
    assert "note" not in row  # Warning never becomes a manual assignment.


def test_all_credit_providers_use_outfit_contract_and_commit_successes_before_quota(tmp_path, monkeypatch):
    from lookbookbot.domain import ProviderKind
    engine, rows = fixture(tmp_path)
    project = engine.store.save_project(name="test", source_dir=tmp_path, output_root=tmp_path,
                                       project_dir=tmp_path, show_date=date(2026, 10, 20),
                                       provider=ProviderKind.OPENAI, model="test-model")
    rows.append({"look_id": "LOOK_002", "evidence_file": rows[0]["evidence_file"]})
    (tmp_path / "control/work").mkdir(parents=True)
    _write_tsv(tmp_path / "control/work/caption-map.tsv", rows)
    inspected, committed = [], []
    def inspect(provider, root, batch):
        look = batch[0]["look_id"]
        inspected.append(look)
        if look == "LOOK_002":
            raise ProviderLimitError("quota")
        from lookbookbot.providers import VisionDecision
        return {look: VisionDecision(True, "garment=brown belted jacket; skirt=matching skirt")}
    monkeypatch.setattr(engine, "_inspect_codex_credit_batch", inspect)
    monkeypatch.setattr(engine, "_confirm_credit_batch_after_quota", lambda root, accepted: committed.extend(accepted))
    with pytest.raises(ProviderLimitError):
        engine._confirm_local_credit_proofs(project, object(), return_rejected=True)
    assert inspected == ["LOOK_001", "LOOK_002"]
    assert committed[0][0] == "LOOK_001"


def test_ui_warns_without_blocking_confirmation_or_overwriting_operator_note(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from PySide6.QtWidgets import QApplication, QTableWidget, QTableWidgetItem
    from lookbookbot.ui import MainWindow
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    proof = tmp_path / "proof.jpg"
    proof.write_bytes(b"visible card")
    row = {"look_id": "LOOK_001", "visual_status": "CONFIRMED", "evidence_file": "proof.jpg",
           "excel_sheet": "W", "excel_look_number": "3", "excel_image": "excel.png"}
    observation = tmp_path / "control/work/caption-map-observations/LOOK_001.json"
    observation.parent.mkdir(parents=True)
    observation.write_text(json.dumps({**row, "evidence_sha256": hashlib.sha256(proof.read_bytes()).hexdigest(),
                           "visual_identity_description": "garment=brown suit; shoes=brown pumps Проверить товары: boots versus pumps"}), encoding="utf-8")
    table = QTableWidget(1, 7)
    for column in range(7):
        table.setItem(0, column, QTableWidgetItem("operator note" if column == 5 else ""))
    window = SimpleNamespace(project=SimpleNamespace(project_dir=tmp_path), credits_table=table)
    MainWindow._style_credit_statuses(window, [row])
    assert table.item(0, 4).text() == "CONFIRMED*"
    assert "сборка продолжится" in table.item(0, 4).toolTip()
    assert table.item(0, 5).text() == "operator note"
    proof.write_bytes(b"changed evidence")
    MainWindow._style_credit_statuses(window, [row])
    assert table.item(0, 4).text() == "CONFIRMED"
    assert table.item(0, 5).toolTip() == ""
    table.close()
