from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "automation-engine" / "lookbook-layout" / "scripts"))

pytest.importorskip("numpy")

import build_reference_registry as registry


class _Reader:
    def __init__(self, counts: list[int]) -> None:
        self.pages = [object() for _ in counts]
        self.counts = counts


def _patch_visible_counts(monkeypatch: pytest.MonkeyPatch, reader: _Reader) -> None:
    def visible(page: object) -> list[tuple[object, object]]:
        return [(None, None)] * reader.counts[reader.pages.index(page)]

    monkeypatch.setattr(registry, "visible_pdf_draws", visible)


def test_reference_without_cover_keeps_first_pdf_page(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = _Reader([2, 2, 2])
    _patch_visible_counts(monkeypatch, reader)

    assert registry.detect_cover_pages(reader) == 0


def test_reference_with_one_cover_starts_on_second_pdf_page(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = _Reader([1, 2, 2])
    _patch_visible_counts(monkeypatch, reader)

    assert registry.detect_cover_pages(reader) == 1


def test_ambiguous_reference_layout_is_not_silently_shifted(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = _Reader([1, 3, 2])
    _patch_visible_counts(monkeypatch, reader)

    with pytest.raises(SystemExit, match="Cannot automatically determine"):
        registry.detect_cover_pages(reader)
