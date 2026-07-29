"""Safe, plain-text editing helpers for an existing CREDiTs story.

The InDesign controller owns all typography.  This module deliberately stores
only the four semantic fields of every product; the native captions gate then
applies the prepared CREDiTs paragraph style and its nested character styles.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


CAPTION_FIELDS = ("look_id", "type", "brand", "price", "article")


class CaptionEditorError(ValueError):
    """A readable validation error for the corrections panel."""


@dataclass(frozen=True)
class CaptionProduct:
    type: str
    brand: str
    price: str
    article: str

    def as_dict(self, look_id: str) -> dict[str, str]:
        return {"look_id": look_id, **asdict(self)}


def _clean(value: str) -> str:
    return value.strip().replace("\t", " ")


def format_caption_editor(products: Iterable[CaptionProduct]) -> str:
    """Render one product as four InDesign-ready lines, separated by a blank."""
    blocks = ["\n".join((item.type, item.brand, item.price, item.article)) for item in products]
    return "\n\n".join(blocks)


def parse_caption_editor(text: str) -> list[CaptionProduct]:
    """Parse editable four-line product cards without exposing TSV internals."""
    lines = [_clean(line) for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    values = [line for line in lines if line]
    if not values:
        raise CaptionEditorError("Добавьте хотя бы один товар: четыре строки — тип, бренд, цена, артикул.")
    if len(values) % 4:
        raise CaptionEditorError(
            "Каждый товар должен состоять ровно из четырёх непустых строк: тип, бренд, цена, артикул."
        )
    products = [CaptionProduct(*values[index : index + 4]) for index in range(0, len(values), 4)]
    invalid = [item.article for item in products if "\n" in item.article or "\t" in item.article]
    if invalid:
        raise CaptionEditorError("Артикул не должен содержать перенос строки или табуляцию.")
    return products


def read_caption_rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise CaptionEditorError("Не найден control/work/caption-data.tsv. Сначала должен быть создан PDF на проверку.")
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source, delimiter="\t")
        if tuple(reader.fieldnames or ()) != CAPTION_FIELDS:
            raise CaptionEditorError("caption-data.tsv имеет неверную структуру.")
        rows = [{field: _clean(str(row.get(field, ""))) for field in CAPTION_FIELDS} for row in reader]
    return rows


def products_for_look(rows: Iterable[dict[str, str]], look_id: str) -> list[CaptionProduct]:
    return [
        CaptionProduct(row["type"], row["brand"], row["price"], row["article"])
        for row in rows
        if row.get("look_id") == look_id
    ]


def replace_look_products(rows: list[dict[str, str]], look_id: str, products: Iterable[CaptionProduct]) -> list[dict[str, str]]:
    replacement = [product.as_dict(look_id) for product in products]
    if not replacement:
        raise CaptionEditorError("У лука должен остаться хотя бы один товарный кредит.")
    result: list[dict[str, str]] = []
    inserted = False
    seen = False
    for row in rows:
        if row.get("look_id") == look_id:
            seen = True
            if not inserted:
                result.extend(replacement)
                inserted = True
            continue
        result.append({field: _clean(str(row.get(field, ""))) for field in CAPTION_FIELDS})
    if not seen:
        raise CaptionEditorError(f"В caption-data.tsv нет {look_id}.")
    return result


def write_caption_rows(path: Path, rows: Iterable[dict[str, str]]) -> None:
    content = _caption_rows_content(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def caption_rows_sha256(rows: Iterable[dict[str, str]]) -> str:
    """Hash a prospective TSV without changing the reviewed source file."""
    return hashlib.sha256(_caption_rows_content(rows).encode("utf-8")).hexdigest()


def _caption_rows_content(rows: Iterable[dict[str, str]]) -> str:
    content = io.StringIO(newline="")
    writer = csv.DictWriter(content, fieldnames=CAPTION_FIELDS, delimiter="\t", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        clean = {field: _clean(str(row.get(field, ""))) for field in CAPTION_FIELDS}
        if any(not value for value in clean.values()):
            raise CaptionEditorError("У каждого товара должны быть заполнены тип, бренд, цена и артикул.")
        writer.writerow(clean)
    return content.getvalue()


def apply_caption_draft(rows: list[dict[str, str]], draft: dict) -> list[dict[str, str]]:
    """Overlay saved draft corrections for display, never touching caption-data.tsv."""
    changes = draft.get("changes") if isinstance(draft, dict) else None
    if not isinstance(changes, list):
        return rows
    result = list(rows)
    for change in changes:
        if not isinstance(change, dict):
            raise CaptionEditorError("Черновик правок имеет неверную структуру.")
        look_id = _clean(str(change.get("look_id", ""))).upper()
        products = change.get("after")
        if not look_id or not isinstance(products, list):
            raise CaptionEditorError("Черновик правок не содержит корректный лук и кредиты.")
        replacement: list[CaptionProduct] = []
        for product in products:
            if not isinstance(product, dict):
                raise CaptionEditorError(f"Черновик {look_id} содержит неверный товар.")
            replacement.append(
                CaptionProduct(
                    _clean(str(product.get("type", ""))),
                    _clean(str(product.get("brand", ""))),
                    _clean(str(product.get("price", ""))),
                    _clean(str(product.get("article", ""))),
                )
            )
        result = replace_look_products(result, look_id, replacement)
    return result


def write_caption_audit(
    project_dir: Path,
    look_id: str,
    before: Iterable[CaptionProduct],
    after: Iterable[CaptionProduct],
    *,
    before_caption_data_sha256: str,
    after_caption_data_sha256: str,
) -> Path:
    """Merge one look into the next revision's draft correction request.

    The controller copies this draft into immutable history when it creates the
    next INDD revision.  Keeping the draft separate lets an operator edit
    several looks before requesting one review PDF.
    """
    destination = project_dir / "control" / "work" / "manual-caption-revisions" / "draft.json"
    existing: dict = {}
    try:
        candidate = json.loads(destination.read_text(encoding="utf-8"))
        if isinstance(candidate, dict) and candidate.get("schema") == "lookbookbot-caption-revision-v1":
            existing = candidate
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        pass
    changes = {
        str(item.get("look_id")): item
        for item in existing.get("changes", [])
        if isinstance(item, dict) and item.get("look_id")
    }
    original_before = changes.get(look_id, {}).get("before")
    changes[look_id] = {
        "look_id": look_id,
        "before": original_before if isinstance(original_before, list) else [asdict(item) for item in before],
        "after": [asdict(item) for item in after],
    }
    payload = {
        "schema": "lookbookbot-caption-revision-v1",
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "look_ids": sorted(changes),
        "before_caption_data_sha256": str(existing.get("before_caption_data_sha256") or before_caption_data_sha256),
        "after_caption_data_sha256": after_caption_data_sha256,
        "changes": [changes[key] for key in sorted(changes)],
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return destination
