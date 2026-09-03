#!/usr/bin/env python3
"""Render controlled candidate boards for rejected credit-map rows.

The normal three-panel proof validates one proposed Excel card.  When that
proposal is visually rejected, this helper shows the PDF pair with every card
that remains unclaimed by confirmed looks.  It changes no TSV: a later,
explicit selection is still required before the map can be written.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps


MAP_FIELDS = [
    "look_id", "excel_sheet", "excel_look_number", "excel_image",
    "left_filename", "right_filename", "evidence_file", "visual_status",
]
INDEX_FIELDS = ["excel_sheet", "excel_look_number", "excel_image"]
PANEL = (430, 610)
PAIR_PANEL = (500, 650)
PLACEHOLDER_PREFIX = "__lbb_missing_"


def fail(message: str) -> None:
    raise SystemExit(f"BLOCKED: {message}")


def read_rows(path: Path, fields: list[str]) -> list[dict[str, str]]:
    if not path.is_file():
        fail(f"Missing required file: {path}")
    with path.open(newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source, delimiter="\t")
        if reader.fieldnames != fields:
            fail(f"Unexpected column schema: {path.name}")
        return [{key: (value or "").strip() for key, value in row.items()} for row in reader]


def child(root: Path, value: str) -> Path:
    path = (root / value).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        fail(f"Path leaves the controlled project: {value}")
        raise error
    return path


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def save_jpeg(image: Image.Image, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    image.save(temporary, "JPEG", quality=94, optimize=True)
    os.replace(temporary, target)


def make_panel(source: Path, title: str, size: tuple[int, int]) -> Image.Image:
    if not source.is_file():
        fail(f"Missing proof source: {source}")
    panel = Image.new("RGB", size, "white")
    try:
        with Image.open(source) as raw:
            image = ImageOps.exif_transpose(raw).convert("RGB")
            image.thumbnail((size[0] - 28, size[1] - 76), Image.Resampling.LANCZOS)
            x = (size[0] - image.width) // 2
            y = 54 + (size[1] - 76 - image.height) // 2
            panel.paste(image, (x, y))
        draw = ImageDraw.Draw(panel)
        draw.rectangle((0, 0, size[0] - 1, size[1] - 1), outline="black", width=3)
        draw.text((16, 16), title, fill="black")
        return panel
    except Exception:
        panel.close()
        raise


def _reference_photo(
    row: dict[str, str],
    side: str,
    hires: Path,
    missing_reference_dir: Path | None,
) -> Path:
    """Use extracted PDF-reference photos for quick-build placeholders."""
    filename = str(row[f"{side}_filename"]).strip()
    source = hires / filename
    if filename.casefold().startswith(PLACEHOLDER_PREFIX) and missing_reference_dir is not None:
        fallback = missing_reference_dir / f"{row['look_id']}_{side.upper()}.jpg"
        if fallback.is_file():
            return fallback
    return source


def render_pair(
    root: Path,
    row: dict[str, str],
    hires: Path,
    target: Path,
    missing_reference_dir: Path | None = None,
) -> None:
    left = _reference_photo(row, "left", hires, missing_reference_dir)
    right = _reference_photo(row, "right", hires, missing_reference_dir)
    canvas = Image.new("RGB", (PAIR_PANEL[0] * 2, PAIR_PANEL[1]), "#dddddd")
    try:
        for index, (source, label) in enumerate(((left, "PDF LEFT"), (right, "PDF RIGHT"))):
            panel = make_panel(source, label, PAIR_PANEL)
            try:
                canvas.paste(panel, (index * PAIR_PANEL[0], 0))
            finally:
                panel.close()
        save_jpeg(canvas, target)
    finally:
        canvas.close()


def render_candidate_sheet(root: Path, cards: list[dict[str, str]], target: Path, page: int) -> None:
    if not cards or len(cards) > 16:
        fail("A candidate evidence page must contain one to sixteen controlled Excel cards.")
    gap = 10
    columns = 2 if len(cards) <= 4 else 4
    rows = (len(cards) + columns - 1) // columns
    canvas = Image.new(
        "RGB",
        (PANEL[0] * columns + gap * (columns - 1), PANEL[1] * rows + gap * (rows - 1)),
        "#d8d8d8",
    )
    try:
        for index, card in enumerate(cards):
            source = child(root, card["excel_image"])
            panel = make_panel(source, f"EXCEL {card['excel_sheet']}:{card['excel_look_number']}", PANEL)
            try:
                x = (index % columns) * (PANEL[0] + gap)
                y = (index // columns) * (PANEL[1] + gap)
                canvas.paste(panel, (x, y))
            finally:
                panel.close()
        draw = ImageDraw.Draw(canvas)
        draw.rectangle((0, 0, canvas.width - 1, canvas.height - 1), outline="black", width=3)
        draw.text((16, canvas.height - 32), f"CANDIDATES {page}", fill="black")
        save_jpeg(canvas, target)
    finally:
        canvas.close()


def parse_looks(value: str, available: set[str]) -> list[str]:
    looks = [item.strip().upper() for item in value.split(",") if item.strip()]
    if not looks or len(set(looks)) != len(looks):
        fail("--looks requires unique comma-separated LOOK_### IDs.")
    if any(item not in available for item in looks):
        fail("--looks names a LOOK absent from caption-map.tsv.")
    return looks


def excluded_candidates(root: Path, value: str) -> tuple[dict[str, set[tuple[str, str]]], set[tuple[str, str]]]:
    """Read controller-owned per-LOOK exclusions without changing the map.

    A rejected proposal must not be presented again for that same LOOK.  Cards
    tentatively selected for other LOOKs are also hidden while a batch is
    resolved, which prevents two independent vision workers from choosing the
    same card.
    """
    if not value:
        return {}, set()
    path = child(root, value)
    if not path.is_file():
        fail(f"Missing rematch exclusion manifest: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        fail(f"Invalid rematch exclusion manifest: {path} ({error})")

    per_look: dict[str, set[tuple[str, str]]] = {}
    for look_id, values in dict(payload.get("attempted_candidates", {})).items():
        if not isinstance(values, list):
            continue
        pairs: set[tuple[str, str]] = set()
        for item in values:
            try:
                sheet, number = str(item).split(":", 1)
            except ValueError:
                continue
            sheet, number = sheet.strip().upper(), number.strip()
            if sheet in {"W", "M"} and number.isdigit():
                pairs.add((sheet, number))
        if pairs:
            per_look[str(look_id).upper()] = pairs

    reserved: set[tuple[str, str]] = set()
    for item in payload.get("reserved_candidates", []):
        try:
            sheet, number = str(item).split(":", 1)
        except ValueError:
            continue
        sheet, number = sheet.strip().upper(), number.strip()
        if sheet in {"W", "M"} and number.isdigit():
            reserved.add((sheet, number))
    return per_look, reserved


def main() -> None:
    parser = argparse.ArgumentParser(description="Render candidate proof boards for targeted credit rematching.")
    parser.add_argument("project", type=Path)
    parser.add_argument("--looks", required=True, help="comma-separated rejected LOOK_### IDs")
    parser.add_argument("--map", dest="caption_map", default="control/work/caption-map.tsv")
    parser.add_argument("--index", default="control/work/_mat/excel-images/index.tsv")
    parser.add_argument("--hires", default="control/work/_mat/hires")
    parser.add_argument(
        "--missing-reference-dir",
        default="",
        help="Quick-build fallback directory containing extracted PDF-reference photos.",
    )
    parser.add_argument("--output", default="control/work/rematch-evidence")
    parser.add_argument(
        "--exclude-json",
        default="",
        help="Project-relative controller manifest with rejected and reserved Excel cards.",
    )
    args = parser.parse_args()

    root = args.project.resolve()
    mapping = read_rows(child(root, args.caption_map), MAP_FIELDS)
    by_look = {row["look_id"]: row for row in mapping}
    looks = parse_looks(args.looks, set(by_look))
    if any(by_look[look_id]["visual_status"] != "PENDING" for look_id in looks):
        fail("Targeted rematch proofs may be rendered only for PENDING looks.")

    index = read_rows(child(root, args.index), INDEX_FIELDS)
    confirmed_pairs = {
        (row["excel_sheet"].upper(), row["excel_look_number"])
        for row in mapping
        if row["visual_status"] == "CONFIRMED"
    }
    cards_by_pair: dict[tuple[str, str], dict[str, str]] = {}
    for row in index:
        pair = (row["excel_sheet"].upper(), row["excel_look_number"])
        if pair in confirmed_pairs:
            continue
        cards_by_pair.setdefault(pair, row)
    cards = [
        cards_by_pair[pair]
        for pair in sorted(cards_by_pair, key=lambda value: (value[0], int(value[1])))
    ]
    per_look_excluded, reserved = excluded_candidates(root, args.exclude_json)

    output = child(root, args.output)
    hires = child(root, args.hires)
    missing_reference_dir = child(root, args.missing_reference_dir) if args.missing_reference_dir else None
    if missing_reference_dir is not None and not missing_reference_dir.is_dir():
        fail(f"Missing reference-photo directory: {missing_reference_dir}")
    if not hires.is_dir():
        fail(f"Hires folder is missing: {hires}")
    for look_id in looks:
        row = by_look[look_id]
        excluded = per_look_excluded.get(look_id, set()) | reserved
        candidates = [
            card for card in cards
            if (card["excel_sheet"].upper(), card["excel_look_number"]) not in excluded
        ]
        if not candidates:
            fail(f"{look_id}: no untried, unreserved Excel candidate remains.")
        folder = output / look_id
        pair_target = folder / "pdf-pair.jpg"
        render_pair(root, row, hires, pair_target, missing_reference_dir)
        pages: list[dict[str, object]] = []
        for page, offset in enumerate(range(0, len(candidates), 16), start=1):
            target = folder / f"candidates-{page:02}.jpg"
            batch = candidates[offset : offset + 16]
            render_candidate_sheet(root, batch, target, page)
            pages.append({
                "path": str(target.relative_to(root)).replace("\\", "/"),
                "sha256": digest(target),
                "cards": [f"{card['excel_sheet']}:{card['excel_look_number']}" for card in batch],
            })
        manifest = {
            "schema": 1,
            "look_id": look_id,
            "pdf_pair": str(pair_target.relative_to(root)).replace("\\", "/"),
            "pdf_pair_sha256": digest(pair_target),
            "pdf_reference_sources": {
                "left": str(_reference_photo(row, "left", hires, missing_reference_dir).relative_to(root)).replace("\\", "/"),
                "right": str(_reference_photo(row, "right", hires, missing_reference_dir).relative_to(root)).replace("\\", "/"),
            },
            "candidate_pool": [f"{card['excel_sheet']}:{card['excel_look_number']}" for card in candidates],
            "candidate_pages": pages,
        }
        if len(pages) > 4:
            fail(
                f"{look_id}: candidate pool has {len(candidates)} cards and cannot be shown in four readable boards. "
                "Narrow the controlled catalogue before visual rematching."
            )
        manifest_path = folder / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"TARGETED REMATCH PROOFS READY: {len(looks)} LOOKs with current untried Excel-card pools.")


if __name__ == "__main__":
    main()
