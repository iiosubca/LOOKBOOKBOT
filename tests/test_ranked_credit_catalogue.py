from __future__ import annotations

import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
Image = pytest.importorskip("PIL.Image")
ImageDraw = pytest.importorskip("PIL.ImageDraw")
sys.path.insert(0, str(Path(__file__).parents[1] / "automation-engine/lookbook-layout/scripts"))
from auto_caption_map import _feature_arrays
from build_targeted_rematch_proofs import rank_candidates


@pytest.mark.parametrize("background", ["#eeeeee", "#aaaaaa", "#35485d"])
def test_background_is_estimated_not_assumed_white(background):
    image = Image.new("RGB", (96, 144), background)
    ImageDraw.Draw(image).rectangle((30, 20, 65, 125), fill="#aa1010")
    mask = _feature_arrays(image, adaptive_background=True)[2]
    assert mask[0, 0] == 0
    assert mask[60, 48] > .9


def test_pair_ranking_keeps_whole_catalogue_and_ignores_labels(tmp_path):
    hires = tmp_path / "_MAT/hires"
    hires.mkdir(parents=True)
    image = Image.new("RGB", (96, 144), "#aaaaaa")
    ImageDraw.Draw(image).rectangle((30, 20, 65, 125), fill="#aa1010")
    for name in ("left.jpg", "right.jpg"):
        image.save(hires / name)
    image.save(tmp_path / "match.jpg")
    other = Image.new("RGB", (96, 144), "#aaaaaa")
    ImageDraw.Draw(other).rectangle((30, 20, 65, 125), fill="#1035aa")
    other.save(tmp_path / "wrong.jpg")
    cards = [{"excel_sheet": "W", "excel_look_number": "1", "excel_image": "wrong.jpg"},
             {"excel_sheet": "M", "excel_look_number": "99", "excel_image": "match.jpg"}]
    cache = {}
    ranked = rank_candidates(tmp_path, {"left_filename": "left.jpg", "right_filename": "right.jpg"},
                             cards, hires, None, cache)
    assert ranked[0] == cards[1]
    assert len(ranked) == len(cards)
    assert len(cache) == 4
    assert cards[0]["excel_look_number"] == "1"  # no source-list mutation


def test_ranking_a_placeholder_uses_pdf_reference_not_blank_frame(tmp_path):
    hires = tmp_path / "_MAT/hires"
    fallback = tmp_path / "control/work/missing-photo-reference"
    hires.mkdir(parents=True)
    fallback.mkdir(parents=True)
    Image.new("RGB", (96, 144), "red").save(tmp_path / "match.jpg")
    Image.new("RGB", (96, 144), "blue").save(tmp_path / "wrong.jpg")
    for side in ("LEFT", "RIGHT"):
        Image.new("RGB", (96, 144), "red").save(fallback / f"LOOK_001_{side}.jpg")
    row = {"look_id": "LOOK_001", "left_filename": "__lbb_missing_left.jpg",
           "right_filename": "__lbb_missing_right.jpg"}
    cards = [{"excel_sheet": "W", "excel_look_number": str(i), "excel_image": name}
             for i, name in enumerate(("wrong.jpg", "match.jpg"), 1)]
    assert rank_candidates(tmp_path, row, cards, hires, fallback, {})[0] == cards[1]
