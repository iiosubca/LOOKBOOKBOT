from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from lookbookbot.photo_recovery import request, PHOTO_POLICY, recover_photos
from lookbookbot.providers import CodexProvider, ProviderError, ProviderLimitError

sys.path.insert(0, str(Path(__file__).parents[1] / "automation-engine/lookbook-layout/scripts"))
from apply_photo_recovery import apply
from build_reference_registry import FIELDS, write_registry, existing_registry, restore_photo_recovery


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture(root):
    folder = root / "control/work/photo-recovery"
    folder.mkdir(parents=True)
    hires = root / "_MAT/hires"
    hires.mkdir(parents=True)
    reference = root / "control/work/_mat/reference.pdf"
    reference.parent.mkdir(parents=True)
    reference.write_bytes(b"reference PDF")
    pair = folder / "pair.jpg"
    pair.write_bytes(b"reference pair")
    proof = folder / "proof.jpg"
    proof.write_bytes(b"exact proof")
    rows = [{"look_id": "LOOK_001", "spread_order": "1", "pdf_spread": "1",
             "left_filename": "__lbb_missing_LOOK_001_LEFT.jpg", "right_filename": "__lbb_missing_LOOK_001_RIGHT.jpg",
             "indd_left_page": "2", "indd_right_page": "3"},
            {"look_id": "LOOK_002", "spread_order": "2", "pdf_spread": "2",
             "left_filename": "existing-left.jpg", "right_filename": "existing-right.jpg",
             "indd_left_page": "4", "indd_right_page": "5"}]
    registry = root / "control/work/look-register.tsv"
    write_registry(registry, rows)
    for name in ("new-left.jpg", "new-right.jpg", "existing-left.jpg"):
        (hires / name).write_bytes(name.encode())
    candidates = {label: {"filename": name, "sha256": sha(hires / name)} for label, name in
                  (("P1", "new-left.jpg"), ("P2", "new-right.jpg"), ("P3", "existing-left.jpg"))}
    queue = {"registry_sha256": sha(registry), "reference_sha256": sha(reference), "candidates": candidates,
             "targets": [{"look_id": "LOOK_001", "reference_pair": pair.relative_to(root).as_posix(),
                          "reference_pair_sha256": sha(pair)}]}
    (folder / "manifest.json").write_text(json.dumps(queue))
    item = {"look_id": "LOOK_001", "left": "P1", "right": "P2", "accepted": True,
            "note": "model=same person; pose=same head and hands",
            "proof": proof.relative_to(root).as_posix(), "proof_sha256": sha(proof)}
    decisions = folder / "decisions.json"
    decisions.write_text(json.dumps({"decisions": [item]}))
    return rows, registry, hires, folder, decisions, item


@pytest.mark.parametrize("partial", [False, True])
def test_only_verified_blank_slots_are_filled_and_frozen_rows_preserved(tmp_path, partial):
    rows, registry, hires, folder, decisions, item = fixture(tmp_path)
    if partial:
        item["right"] = "NONE"
        decisions.write_text(json.dumps({"decisions": [item]}))
    apply(tmp_path, decisions)
    actual = existing_registry(registry)
    assert actual[0]["left_filename"] == "new-left.jpg"
    assert actual[0]["right_filename"] == (rows[0]["right_filename"] if partial else "new-right.jpg")
    assert actual[1] == rows[1]
    assignment, missing = {}, {1}
    restore_photo_recovery(tmp_path, tmp_path / "control/work/_mat/reference.pdf",
                           [hires / "new-left.jpg", hires / "new-right.jpg"], assignment, missing, actual)
    assert assignment == ({0: 0} if partial else {0: 0, 1: 1})
    assert missing == ({1} if partial else set())


@pytest.mark.parametrize("damage", ["duplicate", "changed_source", "changed_proof", "reference", "registry", "initialized", "unconfirmed"])
def test_invalid_recovery_never_changes_registry(tmp_path, damage):
    rows, registry, hires, folder, decisions, item = fixture(tmp_path)
    if damage == "duplicate":
        item["right"] = "P1"
    elif damage == "changed_source":
        (hires / "new-left.jpg").write_bytes(b"new retouch")
    elif damage == "changed_proof":
        (folder / "proof.jpg").write_bytes(b"different pixels")
    elif damage == "reference":
        (tmp_path / "control/work/_mat/reference.pdf").write_bytes(b"different PDF")
    elif damage == "registry":
        rows[1]["pdf_spread"] = "9"
        write_registry(registry, rows)
    elif damage == "initialized":
        (tmp_path / "control/lookbook-state.json").write_text("{}")
    elif damage == "unconfirmed":
        item["accepted"] = False
    decisions.write_text(json.dumps({"decisions": [item]}))
    before = registry.read_bytes()
    with pytest.raises((ValueError, KeyError)):
        apply(tmp_path, decisions)
    assert registry.read_bytes() == before
    assert not (folder / "accepted.json").exists()


class Vision(CodexProvider):
    def __init__(self, answer):
        super().__init__("test-model")
        self.answer = answer
        self.calls = 0
    def run_readonly_photo_pair_vision(self, *args, **kwargs):
        self.calls += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return json.dumps(self.answer)


def test_exact_photo_contract_and_none_answers_are_cached(tmp_path):
    proof = tmp_path / "card.jpg"
    proof.write_bytes(b"pixels")
    provider = Vision({"look_id": "LOOK_001", "left": "P1", "right": "NONE", "note": "same person, head direction and hands"})
    args = (provider, tmp_path, "LOOK_001", PHOTO_POLICY, [proof], ["P1"])
    assert request(*args)["right"] == "NONE"
    assert request(*args)["left"] == "P1"
    assert provider.calls == 1
    assert "EXACT requested photographs" in PHOTO_POLICY
    assert "another person or in another pose is NOT" in PHOTO_POLICY


def test_photo_quota_is_not_a_format_error_and_is_not_retried(tmp_path):
    proof = tmp_path / "card.jpg"
    proof.write_bytes(b"pixels")
    provider = Vision(ProviderLimitError("quota"))
    with pytest.raises(ProviderLimitError):
        request(provider, tmp_path, "LOOK_001", PHOTO_POLICY, [proof], ["P1"])
    assert provider.calls == 1


@pytest.mark.parametrize("left,right", [("P1", "P1"), ("outside", "NONE")])
def test_wrong_photo_labels_or_same_photo_for_both_slots_are_rejected(tmp_path, left, right):
    proof = tmp_path / "card.jpg"
    proof.write_bytes(b"pixels")
    provider = Vision({"look_id": "LOOK_001", "left": left, "right": right, "note": "same person, head direction and hands"})
    with pytest.raises(ProviderError):
        request(provider, tmp_path, "LOOK_001", PHOTO_POLICY, [proof], ["P1"])
    assert provider.calls == 2


def test_finished_project_is_never_recovered_or_sent_to_ai(tmp_path):
    rows, registry, hires, folder, decisions, item = fixture(tmp_path)
    (tmp_path / "control/lookbook-state.json").write_text("{}")
    provider = Vision(AssertionError("No request allowed"))
    before = registry.read_bytes()
    assert recover_photos(provider, tmp_path, object(), lambda _: None) == ["LOOK_001"]
    assert provider.calls == 0 and registry.read_bytes() == before


def test_old_positive_photo_recovery_is_invalidated_when_exact_proof_changes(tmp_path):
    rows, registry, hires, folder, decisions, item = fixture(tmp_path)
    apply(tmp_path, decisions)
    (folder / "proof.jpg").write_bytes(b"changed proof")
    with pytest.raises(SystemExit, match="proof changed"):
        restore_photo_recovery(tmp_path, tmp_path / "control/work/_mat/reference.pdf",
                               [hires / "new-left.jpg", hires / "new-right.jpg"], {}, {1}, existing_registry(registry))


def test_current_confirmed_slot_is_guarded_during_partial_recovery(tmp_path):
    rows, registry, hires, folder, decisions, item = fixture(tmp_path)
    rows[0]["left_filename"] = "new-left.jpg"
    write_registry(registry, rows)
    queue_file = folder / "manifest.json"
    queue = json.loads(queue_file.read_text())
    queue["registry_sha256"] = sha(registry)
    queue["targets"][0]["current_left"] = {"filename": "new-left.jpg", "sha256": sha(hires / "new-left.jpg")}
    queue_file.write_text(json.dumps(queue))
    item["left"] = "NONE"
    decisions.write_text(json.dumps({"decisions": [item]}))
    (hires / "new-left.jpg").write_bytes(b"changed confirmed source")
    before = registry.read_bytes()
    with pytest.raises(ValueError, match="Previously confirmed"):
        apply(tmp_path, decisions)
    assert registry.read_bytes() == before
