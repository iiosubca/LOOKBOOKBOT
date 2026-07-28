from __future__ import annotations

from pathlib import Path


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
