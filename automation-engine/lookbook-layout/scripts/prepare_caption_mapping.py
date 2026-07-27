#!/usr/bin/env python3
"""Extract Excel look images and create a blank, review-only caption-map template."""
from __future__ import annotations

import argparse
import csv
import shutil
from pathlib import Path

from openpyxl import load_workbook
from PIL import Image, ImageDraw, ImageOps


REGISTRY_FIELDS = [
    "look_id", "spread_order", "pdf_spread", "left_filename", "right_filename",
    "indd_left_page", "indd_right_page",
]
MAP_FIELDS = [
    "look_id", "excel_sheet", "excel_look_number", "excel_image",
    "left_filename", "right_filename", "evidence_file", "visual_status",
]
PAIR_PANEL = (420, 590)


def read_registry(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source, delimiter="\t")
        if reader.fieldnames != REGISTRY_FIELDS:
            raise ValueError("look-register.tsv has an unexpected column schema.")
        rows = [{key: (value or "").strip() for key, value in row.items()} for row in reader]
    for index, row in enumerate(rows, start=1):
        if row["look_id"] != f"LOOK_{index:03}" or not row["left_filename"] or not row["right_filename"]:
            raise ValueError(f"Registry row {index + 1} is incomplete or out of order.")
    return rows


def image_extension(image: object) -> str:
    name = str(getattr(image, "path", "")).lower()
    if name.endswith((".png", ".gif", ".bmp")):
        return ".png" if name.endswith(".png") else Path(name).suffix
    return ".jpg"


def image_look_number(sheet: object, image: object) -> str:
    anchor = getattr(image, "anchor", None)
    start = int(anchor._from.row) + 1
    end = int(getattr(anchor, "to", anchor._from).row) + 1
    numbers: set[str] = set()
    for row in range(max(1, start - 2), min(sheet.max_row, end + 2) + 1):
        value = sheet.cell(row, 2).value
        text = str("" if value is None else value).strip()
        if text.isdigit():
            numbers.add(str(int(text)))
    if len(numbers) != 1:
        raise ValueError(f"{sheet.title}: image anchored at rows {start}-{end} cannot be assigned to one Excel look number.")
    return next(iter(numbers))


def render_required_pair(left: Path, right: Path, target: Path, look_id: str) -> None:
    """Render the PDF-ordered hires pair that must be visually mapped to Excel."""
    card = Image.new("RGB", (PAIR_PANEL[0] * 2, PAIR_PANEL[1]), "#dddddd")
    try:
        for index, (source, side) in enumerate(((left, "LEFT"), (right, "RIGHT"))):
            with Image.open(source) as raw:
                image = ImageOps.exif_transpose(raw).convert("RGB")
                image.thumbnail((PAIR_PANEL[0] - 28, PAIR_PANEL[1] - 86), Image.Resampling.LANCZOS)
                panel = Image.new("RGB", PAIR_PANEL, "white")
                try:
                    panel.paste(image, ((PAIR_PANEL[0] - image.width) // 2, 54 + (PAIR_PANEL[1] - 90 - image.height) // 2))
                    draw = ImageDraw.Draw(panel)
                    draw.rectangle((0, 0, PAIR_PANEL[0] - 1, PAIR_PANEL[1] - 1), outline="black", width=2)
                    draw.text((14, 14), f"{look_id}  {side}", fill="black")
                    draw.text((14, 34), source.name, fill="black")
                    card.paste(panel, (index * PAIR_PANEL[0], 0))
                finally:
                    panel.close()
        target.parent.mkdir(parents=True, exist_ok=True)
        card.save(target, "JPEG", quality=92, optimize=True)
    finally:
        card.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare only visual inputs for a reviewed Excel-to-photo map.")
    parser.add_argument("project", type=Path)
    parser.add_argument("--workbook", type=Path, required=True)
    parser.add_argument("--registry", default="control/work/look-register.tsv")
    parser.add_argument("--copy-workbook", default="control/work/_mat/caption-source.xlsx")
    parser.add_argument("--excel-images", default="control/work/_mat/excel-images")
    parser.add_argument("--template", default="control/work/caption-map.tsv")
    parser.add_argument("--hires", default="control/work/_mat/hires")
    parser.add_argument("--pair-review", default="control/work/mapping-review/required-pdf-looks")
    parser.add_argument("--evidence-dir", default="control/work/mapping-evidence")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    project = args.project.resolve()
    registry = read_registry(project / args.registry)
    source = args.workbook.resolve()
    if not source.is_file():
        raise SystemExit(f"Workbook is missing: {source}")
    copied = project / args.copy_workbook
    copied.parent.mkdir(parents=True, exist_ok=True)
    if source != copied.resolve():
        shutil.copy2(source, copied)
    images_dir = project / args.excel_images
    if images_dir.exists() and any(images_dir.iterdir()) and not args.overwrite:
        raise SystemExit(f"Excel image folder already contains files: {images_dir}. Use --overwrite only to re-extract from the same source.")
    images_dir.mkdir(parents=True, exist_ok=True)
    book = load_workbook(copied, data_only=True, read_only=False)
    records: list[dict[str, str]] = []
    try:
        for sheet_name in ("W", "M"):
            if sheet_name not in book.sheetnames:
                raise ValueError(f"Workbook is missing worksheet {sheet_name}.")
            sheet = book[sheet_name]
            counters: dict[str, int] = {}
            for image in list(sheet._images):
                look_number = image_look_number(sheet, image)
                counters[look_number] = counters.get(look_number, 0) + 1
                filename = f"{sheet_name}_{int(look_number):03}_{counters[look_number]:02}{image_extension(image)}"
                target = images_dir / filename
                target.write_bytes(image._data())
                records.append({"excel_sheet": sheet_name, "excel_look_number": look_number, "excel_image": str(target.relative_to(project)).replace("\\", "/")})
    finally:
        book.close()
    if not records:
        raise SystemExit("Workbook contains no embedded images on W/M sheets.")
    index = images_dir / "index.tsv"
    with index.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=["excel_sheet", "excel_look_number", "excel_image"], delimiter="\t")
        writer.writeheader()
        writer.writerows(records)
    template = project / args.template
    if template.exists() and not args.overwrite:
        raise SystemExit(f"Caption-map template already exists: {template}")
    evidence_dir = Path(args.evidence_dir).as_posix().rstrip("/")
    if not evidence_dir or Path(evidence_dir).is_absolute():
        raise SystemExit("--evidence-dir must be a relative folder inside the project.")
    with template.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=MAP_FIELDS, delimiter="\t")
        writer.writeheader()
        for row in registry:
            writer.writerow({
                "look_id": row["look_id"], "excel_sheet": "", "excel_look_number": "", "excel_image": "",
                "left_filename": row["left_filename"], "right_filename": row["right_filename"],
                "evidence_file": f"{evidence_dir}/{row['look_id']}.jpg", "visual_status": "PENDING",
            })
    hires = project / args.hires
    if not hires.is_dir():
        raise SystemExit(f"Hires folder is missing: {hires}")
    pair_review = project / args.pair_review
    for row in registry:
        left = hires / row["left_filename"]
        right = hires / row["right_filename"]
        if not left.is_file() or not right.is_file():
            raise SystemExit(f"{row['look_id']}: required PDF-matched hires pair is missing.")
        render_required_pair(left, right, pair_review / f"{row['look_id']}.jpg", row["look_id"])
    print(f"Extracted {len(records)} embedded Excel images to {images_dir}.")
    print(f"Created review-only caption map: {template}")
    print(f"Rendered {len(registry)} required PDF-ordered photo-pair cards to {pair_review}.")
    print(f"The registry has {len(registry)} required PDF looks. A different Excel-image count is normal; every required look has an Excel match and extra Excel cards remain unused.")
    print("First map obvious visual matches as PENDING, then resolve only the remaining pairs/cards by visual elimination. Do not fill by order, gender, filename, or row number.")


if __name__ == "__main__":
    main()
