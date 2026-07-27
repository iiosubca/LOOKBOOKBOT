from __future__ import annotations

import pytest

from lookbookbot.pipeline import _parse_codex_batch_decisions, _safe_confirmation_note
from lookbookbot.providers import ProviderError


def test_parses_exact_parallel_batch_decisions() -> None:
    decisions = _parse_codex_batch_decisions(
        """Completed.
        {"decisions":[
          {"look_id":"LOOK_001","accepted":true,"note":"Белый жакет и мини-сумка совпадают с обеими фотографиями."},
          {"look_id":"LOOK_002","accepted":false,"note":"На карточке другой цвет платья и отсутствует видимая сумка."}
        ]}
        """,
        ["LOOK_001", "LOOK_002"],
    )

    assert decisions["LOOK_001"].accepted is True
    assert decisions["LOOK_002"].accepted is False


def test_rejects_missing_or_unassigned_batch_decision() -> None:
    with pytest.raises(ProviderError, match="всех назначенных"):
        _parse_codex_batch_decisions(
            '{"decisions":[{"look_id":"LOOK_001","accepted":true,"note":"Белый жакет и маленькая сумка совпадают с изображением."}]}',
            ["LOOK_001", "LOOK_002"],
        )

    with pytest.raises(ProviderError, match="лишний или повторённый"):
        _parse_codex_batch_decisions(
            '{"decisions":[{"look_id":"LOOK_001","accepted":true,"note":"Белый жакет и маленькая сумка совпадают с изображением."},{"look_id":"LOOK_003","accepted":true,"note":"Силуэт, обувь и сумка совпадают с изображением на карточке."}]}',
            ["LOOK_001", "LOOK_002"],
        )


def test_confirmation_note_cannot_break_controller_batch_transport() -> None:
    assert _safe_confirmation_note("  Платье||сумка\nи обувь совпадают с PDF-луком.  ") == "Платье;сумка и обувь совпадают с PDF-луком."
