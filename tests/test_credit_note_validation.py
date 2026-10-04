from __future__ import annotations

from pathlib import Path
import pytest


SCRIPT_DIR = Path(__file__).parents[1] / "automation-engine" / "lookbook-layout" / "scripts"


def _validate_note(note: str) -> set[str]:
    import importlib.util

    spec = importlib.util.spec_from_file_location("credit_note_rules", SCRIPT_DIR / "credit_note_rules.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.visible_identity_cue_categories(note)


def test_credit_proof_accepts_explicit_english_identity_cues() -> None:
    assert _validate_note("garment=white blazer; bag=black leather tote matches both PDF photos.") >= {
        "garment", "bag",
    }


def test_credit_proof_accepts_russian_identity_cues() -> None:
    assert _validate_note("Белый жакет и чёрная сумка совпадают на всех трёх фотографиях.") >= {
        "garment", "bag",
    }


def test_credit_proof_rejects_generic_confirmation() -> None:
    assert _validate_note("The images look like the same outfit and the proposed card is correct.") == set()


@pytest.mark.parametrize("note", [
    "garment=горчичная клетчатая накидка; bag=клетчатая сумка",
    "garment=mustard plaid cape; bag=plaid tote",
    "garment=navy peacoat; shoes=oxford brogues",
    "Горчичное пончо и клетчатая сумка совпадают с обеими фотографиями.",
])
def test_specific_clothing_does_not_require_a_finite_dictionary(note):
    assert len(_validate_note(note)) >= 2


@pytest.mark.parametrize("note", ["garment=; bag=", "garment=unknown; bag=none", "garment=<item>; bag=<item>"])
def test_bare_labels_and_placeholders_are_not_visual_evidence(note):
    assert _validate_note(note) == set()


def test_desktop_and_controller_share_the_same_note_contract():
    from lookbookbot.credit_notes import visual_observation_is_specific
    from lookbookbot.pipeline import _parse_codex_batch_decisions
    from lookbookbot.providers import ProviderError
    import json
    note = "garment=mustard plaid cape; bag=plaid tote"
    assert visual_observation_is_specific(note)
    payload = {"decisions": [{"look_id": "LOOK_009", "accepted": True, "note": note}]}
    assert _parse_codex_batch_decisions(json.dumps(payload), ["LOOK_009"], credit_notes=True)["LOOK_009"].accepted
    payload["decisions"][0]["note"] = "All images are the same and match the proposed look."
    with pytest.raises(ProviderError, match="LOOK_009"):
        _parse_codex_batch_decisions(json.dumps(payload), ["LOOK_009"], credit_notes=True)
