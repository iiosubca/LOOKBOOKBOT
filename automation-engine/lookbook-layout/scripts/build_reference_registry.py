"""Build the complete PDF-authoritative photo registry before controller init.

The source PDF defines the order and the left/right position of every look.  This
tool reads the two embedded photographs from each look page, matches them against
the supplied hires by image content, writes the full registry, and leaves a visual
proof pack in ``control/work``.  It deliberately never reads an Excel workbook.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import gc
import io
import json
import math
import re
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageOps
from pypdf import PdfReader


FIELDS = [
    "look_id",
    "spread_order",
    "pdf_spread",
    "left_filename",
    "right_filename",
    "indd_left_page",
    "indd_right_page",
]
THUMBNAIL = (48, 72)
DRAW = re.compile(
    rb"q\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+cm\s+/([^\s/]+)\s+Do"
)


def fail(message: str) -> None:
    raise SystemExit(f"Registry build blocked: {message}")


def path_from(project: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else project / path


def require_in_work(project: Path, path: Path, label: str) -> None:
    work = (project / "control" / "work").resolve()
    try:
        path.resolve().relative_to(work)
    except ValueError:
        fail(f"{label} must be inside control/work: {path}")


def reduced_rgb(image: Image.Image, size: tuple[int, int], *, crop: bool) -> Image.Image:
    """Decode a JPEG at proof resolution instead of retaining its full camera size."""
    try:
        image.draft("RGB", (size[0] * 2, size[1] * 2))
    except (AttributeError, OSError):
        pass
    prepared = ImageOps.exif_transpose(image).convert("RGB")
    method = ImageOps.fit if crop else ImageOps.contain
    return method(prepared, size, method=Image.Resampling.LANCZOS)


def visible_pdf_pair(page, page_number: int) -> tuple[Image.Image, Image.Image]:
    """Return the two visually topmost source images, ordered by horizontal page position.

    A PDF can retain an extra image XObject below the final right image.  Counting
    XObjects would treat that hidden source as a third look photo.  The page content
    stream instead identifies actual placements; at one rectangle only the final
    draw is visible.
    """
    names = {item.name.rsplit(".", 1)[0]: item.image for item in page.images}
    content = page.get_contents()
    if content is None:
        fail(f"PDF page {page_number} has no drawing content.")
    visible: dict[tuple[float, float], tuple[float, Image.Image]] = {}
    for match in DRAW.finditer(content.get_data()):
        width, _b, _c, height, x, y = (float(match.group(index)) for index in range(1, 7))
        name = match.group(7).decode("latin-1")
        image = names.get(name)
        if image is None:
            continue
        # PDF export can round two stacked draws to slightly different widths.
        # Their shared placement coordinates, not dimensions, identify the same slot.
        key = (round(x, 2), round(y, 2))
        visible[key] = (x, image)
    draws = sorted(visible.values(), key=lambda item: item[0])
    if len(draws) != 2:
        fail(
            f"PDF page {page_number} must visibly place exactly two look photographs; found {len(draws)}."
        )
    return draws[0][1], draws[1][1]


def feature(image: Image.Image) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return colour, edge, and subject-silhouette features robust to JPEG saving."""
    fitted = reduced_rgb(image, THUMBNAIL, crop=True)
    rgb = np.asarray(fitted, dtype=np.float32) / 255.0
    grey = rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114
    edge = np.zeros_like(grey)
    edge[1:, :] += np.abs(grey[1:, :] - grey[:-1, :])
    edge[:, 1:] += np.abs(grey[:, 1:] - grey[:, :-1])
    spread = np.max(rgb, axis=2) - np.min(rgb, axis=2)
    foreground = np.clip((0.94 - grey) / 0.24, 0.0, 1.0)
    foreground = np.maximum(foreground, np.clip((spread - 0.055) / 0.18, 0.0, 1.0))
    return rgb, edge, foreground


def distance(left: tuple[np.ndarray, np.ndarray, np.ndarray], right: tuple[np.ndarray, np.ndarray, np.ndarray]) -> float:
    colour = float(np.mean(np.abs(left[0] - right[0])))
    edges = float(np.mean(np.abs(left[1] - right[1])))
    silhouette = float(np.mean(np.abs(left[2] - right[2])))
    return colour * 0.42 + edges * 0.38 + silhouette * 0.20


def minimum_assignment(cost: np.ndarray) -> list[int]:
    """Hungarian assignment for an N x M cost matrix where N <= M."""
    rows, columns = cost.shape
    if rows > columns:
        fail("There are fewer hires than PDF-reference photographs.")
    u = np.zeros(rows + 1, dtype=np.float64)
    v = np.zeros(columns + 1, dtype=np.float64)
    p = np.zeros(columns + 1, dtype=np.int32)
    way = np.zeros(columns + 1, dtype=np.int32)
    for row in range(1, rows + 1):
        p[0] = row
        column0 = 0
        minimum = np.full(columns + 1, np.inf)
        used = np.zeros(columns + 1, dtype=bool)
        while True:
            used[column0] = True
            row0 = p[column0]
            delta = np.inf
            column1 = 0
            for column in range(1, columns + 1):
                if used[column]:
                    continue
                current = cost[row0 - 1, column - 1] - u[row0] - v[column]
                if current < minimum[column]:
                    minimum[column] = current
                    way[column] = column0
                if minimum[column] < delta:
                    delta = minimum[column]
                    column1 = column
            for column in range(columns + 1):
                if used[column]:
                    u[p[column]] += delta
                    v[column] -= delta
                else:
                    minimum[column] -= delta
            column0 = column1
            if p[column0] == 0:
                break
        while True:
            column1 = way[column0]
            p[column0] = p[column1]
            column0 = column1
            if column0 == 0:
                break
    assignment = [-1] * rows
    for column in range(1, columns + 1):
        if p[column]:
            assignment[p[column] - 1] = column - 1
    if any(item < 0 for item in assignment):
        fail("One or more PDF-reference photographs could not be assigned a hire.")
    return assignment


def timestamp() -> str:
    return dt.datetime.now(tz=dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def png_bytes(image: Image.Image) -> bytes:
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def fit_preview(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    return reduced_rgb(image, size, crop=False)


def draw_card(
    target_left: Image.Image,
    target_right: Image.Image,
    source_left: Image.Image,
    source_right: Image.Image,
    label: str,
    filenames: tuple[str, str],
) -> Image.Image:
    width, height, gutter, header = 1120, 720, 18, 72
    canvas = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(canvas)
    draw.text((18, 16), f"{label} — PDF reference / registered hires", fill="black")
    cell_width = (width - gutter * 5) // 4
    cell_height = height - header - 28
    images = (target_left, target_right, source_left, source_right)
    captions = ("PDF LEFT", "PDF RIGHT", filenames[0], filenames[1])
    for index, (image, caption) in enumerate(zip(images, captions)):
        x = gutter + index * (cell_width + gutter)
        preview = fit_preview(image, (cell_width, cell_height - 28))
        y = header + (cell_height - 28 - preview.height) // 2
        canvas.paste(preview, (x + (cell_width - preview.width) // 2, y))
        draw.text((x, height - 24), caption, fill="black")
    return canvas


def write_contact_sheets(cards: list[Image.Image], destination: Path) -> list[str]:
    names: list[str] = []
    columns, rows = 2, 5
    small = (540, 347)
    for start in range(0, len(cards), columns * rows):
        subset = cards[start:start + columns * rows]
        sheet = Image.new("RGB", (columns * small[0], rows * small[1]), "#e7e7e7")
        for index, card in enumerate(subset):
            preview = ImageOps.fit(card, small, method=Image.Resampling.LANCZOS)
            x = (index % columns) * small[0]
            y = (index // columns) * small[1]
            sheet.paste(preview, (x, y))
        name = f"contact-{start // (columns * rows) + 1:02}.jpg"
        sheet.save(destination / name, quality=90, optimize=True)
        names.append(name)
    return names


def registry_is_blank_bootstrap(path: Path) -> bool:
    if not path.exists():
        return True
    with path.open(newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source, delimiter="\t")
        if reader.fieldnames != FIELDS:
            return False
        rows = list(reader)
    return bool(rows) and all(
        row.get("look_id", "").strip()
        and row.get("spread_order", "").strip()
        and not row.get("pdf_spread", "").strip()
        and not row.get("left_filename", "").strip()
        and not row.get("right_filename", "").strip()
        and not row.get("indd_left_page", "").strip()
        and not row.get("indd_right_page", "").strip()
        for row in rows
    )


def existing_registry(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source, delimiter="\t")
        if reader.fieldnames != FIELDS:
            fail(f"Existing registry has an unexpected schema: {path}")
        return [{field: (row.get(field) or "").strip() for field in FIELDS} for row in reader]


def write_registry(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=FIELDS, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)


def build(project: Path, reference: Path, hires: Path, registry: Path, cover_pages: int, dry_run: bool) -> None:
    project = project.resolve()
    require_in_work(project, reference, "Reference PDF")
    require_in_work(project, hires, "Hires folder")
    require_in_work(project, registry, "Registry")
    if not reference.is_file():
        fail(f"Reference PDF does not exist: {reference}")
    if not hires.is_dir():
        fail(f"Hires folder does not exist: {hires}")
    if cover_pages < 0:
        fail("--cover-pages cannot be negative.")
    files = sorted(
        (item for item in hires.iterdir() if item.is_file() and item.suffix.lower() in {".jpg", ".jpeg"}),
        key=lambda item: item.name.lower(),
    )
    if not files:
        fail("The hires folder does not contain JPG files.")
    reader = PdfReader(str(reference))
    if len(reader.pages) <= cover_pages:
        fail("Reference PDF has no look pages after the cover.")

    pdf_features: list[tuple[np.ndarray, np.ndarray, np.ndarray]] = []
    page_images: list[tuple[Image.Image, Image.Image]] = []
    for page_number in range(cover_pages, len(reader.pages)):
        pair = visible_pdf_pair(reader.pages[page_number], page_number + 1)
        # Keep only a proof-sized image. Retaining 100 decoded 25-MB source JPEGs
        # consumes several gigabytes and can abort preparation before a registry exists.
        page_images.append((fit_preview(pair[0], (360, 540)), fit_preview(pair[1], (360, 540))))
        pdf_features.extend((feature(pair[0]), feature(pair[1])))
        del pair
        if page_number % 8 == 0:
            gc.collect()
    if len(files) < len(pdf_features):
        fail(f"Reference requires {len(pdf_features)} photographs, but only {len(files)} hires are available.")
    prior_rows: list[dict[str, str]] | None = None
    if registry.exists() and not registry_is_blank_bootstrap(registry):
        prior_rows = existing_registry(registry)

    # Decode each hire exactly once.  The old implementation decoded every
    # 25-MB source again while drawing the proof cards.  On a 100-photo job
    # that could exceed the Codex command limit even after the registry had
    # been determined.  These proof-sized previews retain sufficient visual
    # detail for the cards and keep the complete bootstrap atomic.
    hire_features: list[tuple[np.ndarray, np.ndarray, np.ndarray]] = []
    hire_previews: list[Image.Image] = []
    for file in files:
        with Image.open(file) as image:
            preview = fit_preview(image, (360, 540))
            hire_previews.append(preview)
            hire_features.append(feature(preview))
    cost = np.asarray(
        [[distance(pdf_item, hire_item) for hire_item in hire_features] for pdf_item in pdf_features],
        dtype=np.float64,
    )
    assignment = minimum_assignment(cost)
    diagnostics: list[dict[str, object]] = []
    problems: list[str] = []
    for target_index, source_index in enumerate(assignment):
        ranking = np.argsort(cost[target_index])
        rank = int(np.where(ranking == source_index)[0][0]) + 1
        selected = float(cost[target_index, source_index])
        best = float(cost[target_index, ranking[0]])
        alternative = float(cost[target_index, ranking[1]]) if len(ranking) > 1 else math.inf
        margin = alternative - best
        diagnostics.append({
            "target": target_index,
            "selected": files[source_index].name,
            "selected_score": round(selected, 7),
            "best_score": round(best, 7),
            "second_score": round(alternative, 7),
            "rank": rank,
            "margin": round(margin, 7),
        })
        if rank != 1 or selected > 0.145 or margin < 0.004:
            problems.append(
                f"PDF image {target_index + 1}: {files[source_index].name} (rank {rank}, score {selected:.4f}, margin {margin:.4f})"
            )

    evidence = project / "control" / "work" / "registry-build" / timestamp()
    cards_dir = evidence / "cards"
    cards_dir.mkdir(parents=True, exist_ok=False)
    with (evidence / "match-candidates.tsv").open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=["pdf_image", "assigned_hire", "score", "best_score", "second_score", "rank", "margin"], delimiter="\t")
        writer.writeheader()
        for item in diagnostics:
            writer.writerow({
                "pdf_image": int(item["target"]) + 1,
                "assigned_hire": item["selected"],
                "score": item["selected_score"],
                "best_score": item["best_score"],
                "second_score": item["second_score"],
                "rank": item["rank"],
                "margin": item["margin"],
            })

    rows: list[dict[str, str]] = []
    contact_previews: list[Image.Image] = []
    for index, (left_target, right_target) in enumerate(page_images, start=1):
        left_file = files[assignment[(index - 1) * 2]]
        right_file = files[assignment[(index - 1) * 2 + 1]]
        card = draw_card(
            left_target,
            right_target,
            hire_previews[assignment[(index - 1) * 2]],
            hire_previews[assignment[(index - 1) * 2 + 1]],
            f"LOOK_{index:03}",
            (left_file.name, right_file.name),
        )
        card.save(cards_dir / f"LOOK_{index:03}.jpg", quality=90, optimize=True)
        contact_previews.append(ImageOps.fit(card, (540, 347), method=Image.Resampling.LANCZOS))
        del card
        rows.append({
            "look_id": f"LOOK_{index:03}",
            "spread_order": str(index),
            "pdf_spread": str(index),
            "left_filename": left_file.name,
            "right_filename": right_file.name,
            "indd_left_page": str(index * 2),
            "indd_right_page": str(index * 2 + 1),
        })
    contacts = write_contact_sheets(contact_previews, evidence)
    used = {files[index].name for index in assignment}
    payload = {
        "schema": "lookbook-reference-registry/v1",
        "reference": str(reference),
        "reference_pages": len(reader.pages),
        "cover_pages": cover_pages,
        "looks": len(rows),
        "hires_available": len(files),
        "hires_used": len(used),
        "unused_hires": [file.name for file in files if file.name not in used],
        "registry": str(registry),
        "contacts": contacts,
        "problems": problems,
    }
    (evidence / "manifest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if problems:
        fail("Automatic visual matching is not decisive; inspect registry-build proof cards and correct source inputs. " + "; ".join(problems[:4]))
    if prior_rows is not None and prior_rows != rows:
        fail(
            "Existing complete registry does not match the current PDF-reference proof. "
            "Do not overwrite it; inspect the registry-build proof cards and resolve the source change."
        )
    if not dry_run and prior_rows is None:
        write_registry(registry, rows)
    state = "existing registry re-verified" if prior_rows is not None else "registry written"
    print(f"PASS registry: {len(rows)} PDF-ordered looks, {len(used)} hires selected, {len(files) - len(used)} unused hires; {state}.")
    print(f"Visual proof pack: {evidence}")
    print(f"Registry: {registry}{' (dry run only)' if dry_run else ''}")


def self_test() -> None:
    """Exercise feature matching and rectangular assignment without production files."""
    images: list[Image.Image] = []
    for index in range(6):
        image = Image.new("RGB", (240, 360), (245, 245, 245))
        draw = ImageDraw.Draw(image)
        draw.rectangle((20 + index * 7, 45, 205, 315), fill=(20 + index * 30, 70 + index * 19, 135 + index * 11))
        draw.ellipse((65, 82 + index * 3, 165, 182 + index * 3), fill=(190 - index * 17, 80 + index * 23, 60 + index * 13))
        images.append(image)
    targets = [feature(image.resize((120, 180))) for image in images]
    hires = [feature(image) for image in reversed(images)] + [feature(Image.new("RGB", (240, 360), "white"))]
    assignment = minimum_assignment(np.asarray([[distance(target, hire) for hire in hires] for target in targets]))
    expected = list(reversed(range(len(images))))
    if assignment != expected:
        raise SystemExit(f"Self-test failed: {assignment} != {expected}")
    print("PASS build_reference_registry self-test")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a complete PDF-authoritative look-register.tsv from reference and hires.")
    parser.add_argument("project", nargs="?", type=Path)
    parser.add_argument("--reference", default="control/work/_mat/reference.pdf")
    parser.add_argument("--hires", default="control/work/_mat/hires")
    parser.add_argument("--registry", default="control/work/look-register.tsv")
    parser.add_argument("--cover-pages", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    if args.project is None:
        parser.error("project is required unless --self-test is used")
    project = args.project.resolve()
    build(project, path_from(project, args.reference), path_from(project, args.hires), path_from(project, args.registry), args.cover_pages, args.dry_run)


if __name__ == "__main__":
    main()
