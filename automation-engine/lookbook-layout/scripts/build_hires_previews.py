"""Create lightweight visual indexes for a lookbook hires folder."""

from __future__ import annotations

import argparse
import csv
import gc
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}
THUMB_SIZE = (300, 420)
CELL_SIZE = (340, 480)


def make_preview(source: Path, target: Path) -> tuple[int, int]:
    with Image.open(source) as raw:
        image = ImageOps.exif_transpose(raw).convert("RGB")
        original_size = image.size
        image.thumbnail(THUMB_SIZE, Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", THUMB_SIZE, "white")
        x = (THUMB_SIZE[0] - image.width) // 2
        y = (THUMB_SIZE[1] - image.height) // 2
        canvas.paste(image, (x, y))
        canvas.save(target, "JPEG", quality=82, optimize=True)
        canvas.close()
        return original_size


def make_sheet(previews: list[Path], sheet_path: Path, columns: int) -> None:
    rows = (len(previews) + columns - 1) // columns
    sheet = Image.new("RGB", (columns * CELL_SIZE[0], rows * CELL_SIZE[1]), "white")
    draw = ImageDraw.Draw(sheet)
    for index, preview in enumerate(previews):
        with Image.open(preview) as image:
            x = (index % columns) * CELL_SIZE[0] + 20
            y = (index // columns) * CELL_SIZE[1] + 20
            sheet.paste(image, (x, y))
        draw.text((x, y + THUMB_SIZE[1] + 15), preview.name, fill="black")
    sheet.save(sheet_path, "JPEG", quality=86, optimize=True)
    sheet.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("hires", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--columns", type=int, default=4)
    parser.add_argument("--per-sheet", type=int, default=20)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--offset", type=int, default=0)
    args = parser.parse_args()

    files = sorted(path for path in args.hires.iterdir() if path.suffix.lower() in EXTENSIONS)
    files = files[args.offset :]
    if args.limit:
        files = files[: args.limit]
    if not files:
        raise SystemExit("No supported image files found.")

    preview_dir = args.output / "previews"
    sheet_dir = args.output / "sheets"
    preview_dir.mkdir(parents=True, exist_ok=True)
    sheet_dir.mkdir(parents=True, exist_ok=True)

    preview_paths: list[Path] = []
    mode = "w" if args.offset == 0 else "a"
    with (args.output / "manifest.tsv").open(mode, newline="", encoding="utf-8") as manifest:
        writer = csv.writer(manifest, delimiter="\t")
        if args.offset == 0:
            writer.writerow(["source_filename", "preview_filename", "width", "height"])
        for source in files:
            preview = preview_dir / f"{source.stem}.jpg"
            width, height = make_preview(source, preview)
            preview_paths.append(preview)
            writer.writerow([source.name, preview.name, width, height])
            gc.collect()

    for start in range(0, len(preview_paths), args.per_sheet):
        number = (args.offset + start) // args.per_sheet + 1
        make_sheet(preview_paths[start : start + args.per_sheet], sheet_dir / f"contact-{number:02}.jpg", args.columns)


if __name__ == "__main__":
    main()
