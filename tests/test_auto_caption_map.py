from __future__ import annotations

import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

sys.path.insert(0, str(Path(__file__).parents[1] / "automation-engine" / "lookbook-layout" / "scripts"))

from auto_caption_map import appearance_signature, pair_distance


def _feature(colour: tuple[float, float, float]) -> tuple[object, ...]:
    rgb = np.full((4, 4, 3), colour, dtype=np.float32)
    edge = np.zeros((4, 4), dtype=np.float32)
    foreground = np.ones((4, 4), dtype=np.float32)
    grid = np.zeros(48, dtype=np.float32)
    signature = np.zeros(80, dtype=np.float32)
    return rgb, edge, foreground, grid, signature


def test_pair_score_uses_the_second_pdf_view_as_context() -> None:
    card = _feature((0.22, 0.22, 0.22))
    left = _feature((0.22, 0.22, 0.22))
    matching_right = _feature((0.22, 0.22, 0.22))
    unrelated_right = _feature((0.92, 0.92, 0.92))
    card_appearance = appearance_signature(card)  # type: ignore[arg-type]
    left_appearance = appearance_signature(left)  # type: ignore[arg-type]

    matching_score = pair_distance(
        card,
        left,
        matching_right,
        card_appearance,
        left_appearance,
        appearance_signature(matching_right),  # type: ignore[arg-type]
    )[0]
    unrelated_score = pair_distance(
        card,
        left,
        unrelated_right,
        card_appearance,
        left_appearance,
        appearance_signature(unrelated_right),  # type: ignore[arg-type]
    )[0]

    assert unrelated_score > matching_score
