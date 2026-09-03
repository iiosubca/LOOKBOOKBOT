from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "automation-engine" / "lookbook-layout" / "scripts"))

from pdf_page_geometry import draw_fully_covered_by_later, draw_intersects_page


class _Box:
    left = 0.0
    bottom = 0.0
    right = 1024.0
    top = 768.0


class _Page:
    cropbox = _Box()


def test_completely_off_page_image_is_ignored() -> None:
    assert not draw_intersects_page(_Page(), (720.2812, 0.0, 0.0, 1080.0, 1292.147, -312.0))


def test_visible_and_partially_clipped_images_are_candidates() -> None:
    assert draw_intersects_page(_Page(), (514.6317, 0.0, 0.0, 771.6461, -1.867958, -1.823063))
    assert draw_intersects_page(_Page(), (100.0, 0.0, 0.0, 100.0, 1000.0, 0.0))


def test_image_that_only_touches_page_edge_is_ignored() -> None:
    assert not draw_intersects_page(_Page(), (100.0, 0.0, 0.0, 100.0, 1024.0, 0.0))


def test_fully_overpainted_image_is_ignored_even_when_inside_page() -> None:
    old = (100.0, 0.0, 0.0, 100.0, 100.0, 100.0)
    replacement = (108.0, 0.0, 0.0, 108.0, 96.0, 96.0)
    assert draw_fully_covered_by_later(_Page(), old, [replacement])


def test_partially_visible_image_is_retained() -> None:
    old = (100.0, 0.0, 0.0, 100.0, 100.0, 100.0)
    replacement = (60.0, 0.0, 0.0, 100.0, 100.0, 100.0)
    assert not draw_fully_covered_by_later(_Page(), old, [replacement])


def test_pdf_rounding_hairline_is_treated_as_full_occlusion() -> None:
    old = (100.0, 0.0, 0.0, 100.0, 100.0, 100.0)
    replacement = (99.6, 0.0, 0.0, 100.0, 100.2, 100.0)
    assert draw_fully_covered_by_later(_Page(), old, [replacement])


def test_negligible_visible_strip_is_treated_as_full_occlusion() -> None:
    old = (100.0, 0.0, 0.0, 100.0, 100.0, 0.0)
    replacement = (100.0, 0.0, 0.0, 100.0, 100.0, 1.5)
    assert draw_fully_covered_by_later(_Page(), old, [replacement])
