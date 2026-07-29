from __future__ import annotations

from pathlib import Path

import pytest

from lookbookbot.caption_editor import (
    CaptionEditorError,
    CaptionProduct,
    apply_caption_draft,
    caption_rows_sha256,
    format_caption_editor,
    parse_caption_editor,
    products_for_look,
    read_caption_rows,
    replace_look_products,
    write_caption_audit,
    write_caption_rows,
)


def test_editor_round_trip_preserves_four_product_fields() -> None:
    products = [
        CaptionProduct("Жакет", "ARMARIUM", "199 500 ₽", "7089248"),
        CaptionProduct("Сумка", "JIL SANDER", "290 500 ₽", "7083327"),
    ]

    assert parse_caption_editor(format_caption_editor(products)) == products


def test_editor_requires_full_four_line_product_cards() -> None:
    with pytest.raises(CaptionEditorError, match="четырёх"):
        parse_caption_editor("Жакет\nARMARIUM\n199 500 ₽")


def test_replacing_one_look_keeps_every_other_credit_byte_order(tmp_path: Path) -> None:
    path = tmp_path / "caption-data.tsv"
    rows = [
        {"look_id": "LOOK_001", "type": "Пальто", "brand": "MVST", "price": "100 ₽", "article": "01"},
        {"look_id": "LOOK_002", "type": "Сумка", "brand": "JIL SANDER", "price": "200 ₽", "article": "02"},
    ]
    write_caption_rows(path, rows)
    revised = replace_look_products(
        read_caption_rows(path), "LOOK_001", [CaptionProduct("Жакет", "ARMARIUM", "300 ₽", "03")]
    )

    assert products_for_look(revised, "LOOK_002") == [CaptionProduct("Сумка", "JIL SANDER", "200 ₽", "02")]
    assert products_for_look(revised, "LOOK_001") == [CaptionProduct("Жакет", "ARMARIUM", "300 ₽", "03")]


def test_caption_draft_accumulates_several_looks_before_one_revision(tmp_path: Path) -> None:
    project = tmp_path / "project"
    first = CaptionProduct("Пальто", "MVST", "100 ₽", "01")
    second = CaptionProduct("Сумка", "JIL SANDER", "200 ₽", "02")
    draft = write_caption_audit(project, "LOOK_001", [first], [second], before_caption_data_sha256="before", after_caption_data_sha256="after-1")
    write_caption_audit(project, "LOOK_002", [first], [second], before_caption_data_sha256="after-1", after_caption_data_sha256="after-2")

    payload = __import__("json").loads(draft.read_text(encoding="utf-8"))
    assert payload["look_ids"] == ["LOOK_001", "LOOK_002"]
    assert payload["before_caption_data_sha256"] == "before"
    assert payload["after_caption_data_sha256"] == "after-2"


def test_caption_draft_is_an_overlay_and_does_not_change_reviewed_tsv(tmp_path: Path) -> None:
    project = tmp_path / "project"
    path = project / "control" / "work" / "caption-data.tsv"
    original = [
        {"look_id": "LOOK_001", "type": "Пальто", "brand": "MVST", "price": "100 ₽", "article": "01"},
        {"look_id": "LOOK_002", "type": "Сумка", "brand": "JIL SANDER", "price": "200 ₽", "article": "02"},
    ]
    write_caption_rows(path, original)
    before_bytes = path.read_bytes()
    revised = replace_look_products(
        original,
        "LOOK_001",
        [CaptionProduct("Жакет", "ARMARIUM", "300 ₽", "03")],
    )
    audit = write_caption_audit(
        project,
        "LOOK_001",
        products_for_look(original, "LOOK_001"),
        products_for_look(revised, "LOOK_001"),
        before_caption_data_sha256="reviewed-baseline",
        after_caption_data_sha256=caption_rows_sha256(revised),
    )

    overlay = apply_caption_draft(read_caption_rows(path), __import__("json").loads(audit.read_text(encoding="utf-8")))

    assert path.read_bytes() == before_bytes
    assert products_for_look(overlay, "LOOK_001") == [CaptionProduct("Жакет", "ARMARIUM", "300 ₽", "03")]
    assert caption_rows_sha256(overlay) == __import__("json").loads(audit.read_text(encoding="utf-8"))["after_caption_data_sha256"]
