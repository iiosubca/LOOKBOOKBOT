#!/usr/bin/env python3
"""Render one proof card per proposed Excel-to-hires caption mapping."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps
from caption_photo_sources import caption_photo_source
from project_materials import project_hires


MAP_FIELDS = [
    "look_id", "excel_sheet", "excel_look_number", "excel_image",
    "left_filename", "right_filename", "evidence_file", "visual_status",
]
PANEL = (520, 720)


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def child(project: Path, value: str) -> Path:
    path = (project / value).resolve()
    try:
        path.relative_to(project)
    except ValueError as error:
        raise ValueError(f"Path must stay inside the project: {value}") from error
    return path


def read_map(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source, delimiter="\t")
        if reader.fieldnames != MAP_FIELDS:
            raise ValueError("caption-map.tsv has an unexpected column schema.")
        rows = [{key: (value or "").strip() for key, value in row.items()} for row in reader]
    seen: set[tuple[str, str]] = set()
    for index, row in enumerate(rows, start=1):
        expected = f"LOOK_{index:03}"
        key = (row["excel_sheet"], row["excel_look_number"])
        if row["look_id"] != expected or row["excel_sheet"] not in {"W", "M"} or not row["excel_look_number"].isdigit():
            raise ValueError(f"Caption map row {index + 1} is not a complete explicit mapping.")
        if key in seen:
            raise ValueError(f"{expected}: Excel group {key[0]} {key[1]} is repeated.")
        seen.add(key)
        if row["visual_status"] not in {"PENDING", "CONFIRMED"}:
            raise ValueError(f"{expected}: visual_status must be PENDING or CONFIRMED.")
    return rows


def paste_panel(canvas: Image.Image, source: Path, x: int, title: str) -> None:
    with Image.open(source) as raw:
        image = ImageOps.exif_transpose(raw).convert("RGB")
        image.thumbnail((PANEL[0] - 28, PANEL[1] - 88), Image.Resampling.LANCZOS)
        background = Image.new("RGB", PANEL, "white")
        background.paste(image, ((PANEL[0] - image.width) // 2, 52 + (PANEL[1] - 88 - image.height) // 2))
        draw = ImageDraw.Draw(background)
        draw.rectangle((0, 0, PANEL[0] - 1, PANEL[1] - 1), outline="black", width=2)
        draw.text((14, 16), title, fill="black")
        canvas.paste(background, (x, 0))
        background.close()


def write_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Render auditable visual proof for every proposed caption map row.")
    parser.add_argument("project", type=Path)
    parser.add_argument("--map", dest="caption_map", default="control/work/caption-map.tsv")
    parser.add_argument("--hires", default="_MAT/hires")
    args = parser.parse_args()
    project = args.project.resolve()
    rows = read_map(child(project, args.caption_map))
    hires = project_hires(project, args.hires)
    manifest: dict[str, object] = {"schema": 1, "generator": "render_caption_mapping_evidence.py", "items": {}}
    evidence_roots: set[Path] = set()
    for row in rows:
        excel = child(project, row["excel_image"])
        left = caption_photo_source(project, row, hires, "left")
        right = caption_photo_source(project, row, hires, "right")
        proof = child(project, row["evidence_file"])
        evidence_roots.add(proof.parent)
        for path in (excel, left, right):
            if not path.is_file():
                raise ValueError(f"{row['look_id']}: missing mapping source {path}")
        proof.parent.mkdir(parents=True, exist_ok=True)
        card = Image.new("RGB", (PANEL[0] * 3, PANEL[1]), "#dddddd")
        try:
            paste_panel(card, excel, 0, f"EXCEL {row['excel_sheet']} {row['excel_look_number']}")
            paste_panel(card, left, PANEL[0], f"LEFT {row['left_filename']}")
            paste_panel(card, right, PANEL[0] * 2, f"RIGHT {row['right_filename']}")
            card.save(proof, "JPEG", quality=92, optimize=True)
        finally:
            card.close()
        if proof.stat().st_size < 1024:
            raise ValueError(f"{row['look_id']}: generated visual proof is unexpectedly small.")
        manifest["items"][row["look_id"]] = {
            "left_sha256": digest(hires / row["left_filename"]),
            "right_sha256": digest(hires / row["right_filename"]),
            "excel_sha256": digest(excel), "evidence_sha256": digest(proof),
        }
        for side, source in (("left", left), ("right", right)):
            if source != hires / row[f"{side}_filename"]:
                manifest["items"][row["look_id"]][f"{side}_reference_sha256"] = digest(source)
    if len(evidence_roots) != 1:
        raise ValueError("All caption-map evidence files must live in one controlled folder.")
    write_json(next(iter(evidence_roots)) / "manifest.json", manifest)
    print(f"Rendered {len(rows)} exact Excel + left + right proof cards.")
    print("Inspect every card. Only after the image identity is visually correct may each map row be changed to CONFIRMED.")


if __name__ == "__main__":
    main()
