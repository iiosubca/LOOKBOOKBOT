#!/usr/bin/env python3
"""Create and independently confirm an Excel-to-look credit map by image identity.

The PDF-derived registry supplies the fixed left/right photo pair for every
look.  This tool compares each embedded Excel card with both members of every
registered pair, scores the complete pair as a unit, uses a one-to-one
assignment, and refuses weak or ambiguous matches.  It is deliberately separate
from the proof renderer: a proposed map is PENDING, the renderer freezes an
exact three-panel proof, and only then can the same visual resolver mark every
row CONFIRMED.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageOps

from credit_note_rules import visible_identity_cue_categories


REGISTRY_FIELDS = [
    "look_id", "spread_order", "pdf_spread", "left_filename", "right_filename",
    "indd_left_page", "indd_right_page",
]
MAP_FIELDS = [
    "look_id", "excel_sheet", "excel_look_number", "excel_image",
    "left_filename", "right_filename", "evidence_file", "visual_status",
]
INDEX_FIELDS = ["excel_sheet", "excel_look_number", "excel_image"]
THUMBNAIL = (96, 144)
PANEL = (520, 720)
MISSING_PREFIX = "__lbb_missing_"
# An Excel card contains one representative image, while the PDF registry
# contains the ordered full-length/close-up pair.  Keep both views in the
# score: direct view identity remains strongest, and pair appearance is a
# conservative tie-breaker.  The two appearance terms are deliberately small
# because a full-length Excel card is not expected to look like the close-up
# pixel-for-pixel.  This lets pair evidence correct near-ties without
# destabilising strong one-to-one matches.
DIRECT_PAIR_WEIGHT = 0.985
BEST_VIEW_APPEARANCE_WEIGHT = 0.010
PAIR_CONTEXT_WEIGHT = 0.005
ALTERNATIVE_FIELDS = [
    "look_id", "excel_sheet", "excel_look_number", "excel_image",
    "left_filename", "right_filename", "evidence_file", "evidence_sha256",
]

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


def child(project: Path, value: str) -> Path:
    path = (project / value).resolve()
    try:
        path.relative_to(project)
    except ValueError as error:
        fail(f"Path leaves the controlled project: {value}")
        raise error
    return path


def preview(image: Image.Image) -> Image.Image:
    try:
        image.draft("RGB", (THUMBNAIL[0] * 3, THUMBNAIL[1] * 3))
    except (AttributeError, OSError):
        pass
    oriented = ImageOps.exif_transpose(image).convert("RGB")
    return ImageOps.fit(oriented, THUMBNAIL, method=Image.Resampling.LANCZOS)


def _feature_arrays(image: Image.Image) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Build identity features while suppressing the shared studio background."""
    thumb = preview(image)
    rgb = np.asarray(thumb, dtype=np.float32) / 255.0
    grey = rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114
    edge = np.zeros_like(grey)
    edge[1:, :] += np.abs(grey[1:, :] - grey[:-1, :])
    edge[:, 1:] += np.abs(grey[:, 1:] - grey[:, :-1])
    spread = np.max(rgb, axis=2) - np.min(rgb, axis=2)
    foreground = np.clip((0.94 - grey) / 0.24, 0.0, 1.0)
    foreground = np.maximum(foreground, np.clip((spread - 0.055) / 0.18, 0.0, 1.0))

    # A global colour average cannot distinguish neighbouring looks with the
    # same white studio wall. Preserve where the garment colour occurs by
    # recording foreground-weighted colour in a stable 4x4 grid.
    grid: list[float] = []
    mask = np.maximum(foreground, 0.08)
    height, width = mask.shape
    for row in range(4):
        y0, y1 = round(row * height / 4), round((row + 1) * height / 4)
        for column in range(4):
            x0, x1 = round(column * width / 4), round((column + 1) * width / 4)
            region = mask[y0:y1, x0:x1]
            pixels = rgb[y0:y1, x0:x1]
            denominator = float(np.sum(region)) or 1.0
            grid.extend(float(value) for value in np.sum(pixels * region[..., None], axis=(0, 1)) / denominator)

    subject = rgb[foreground > 0.14]
    if len(subject) < 12:
        subject = rgb.reshape(-1, 3)
    histogram, _ = np.histogramdd(subject, bins=(4, 4, 4), range=((0.0, 1.0),) * 3)
    histogram = histogram.astype(np.float32).reshape(-1)
    histogram /= float(np.sum(histogram)) or 1.0
    profile = np.concatenate((np.mean(foreground, axis=1), np.mean(foreground, axis=0))).astype(np.float32)
    return rgb, edge, foreground, np.asarray(grid, dtype=np.float32), np.concatenate((histogram, profile))


def image_feature(path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    with Image.open(path) as source:
        return _feature_arrays(source)


def distance(
    left: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray],
    right: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray],
) -> float:
    # Most pixels are the same pale studio background.  A global average made
    # two different light looks appear deceptively close.  Weight the garment
    # and model silhouette heavily, while retaining a small background weight
    # to preserve pose and crop information.
    weight = 0.08 + 0.92 * np.maximum(left[2], right[2])
    colour = float(np.sum(np.mean(np.abs(left[0] - right[0]), axis=2) * weight) / np.sum(weight))
    edges = float(np.sum(np.abs(left[1] - right[1]) * weight) / np.sum(weight))
    silhouette = float(np.mean(np.abs(left[2] - right[2])))
    grid = float(np.mean(np.abs(left[3] - right[3])))
    signature = float(np.mean(np.abs(left[4] - right[4])))
    # Keep direct pixels and edges useful for exact same-shoot matches, but
    # make spatial garment colour and silhouette distribution decisive when
    # several male looks share the same pale background and pose.
    return (
        colour * 0.28
        + edges * 0.24
        + silhouette * 0.14
        + grid * 0.22
        + signature * 0.12
    )


def appearance_signature(
    features: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray],
) -> tuple[np.ndarray, np.ndarray]:
    """Extract foreground-weighted appearance evidence from an image feature."""
    rgb, _edge, foreground, _grid, _signature = features
    quantized = np.minimum((rgb * 8).astype(np.int32), 7)
    bins = (quantized[..., 0] * 64 + quantized[..., 1] * 8 + quantized[..., 2]).ravel()
    weights = foreground.ravel().astype(np.float64)
    histogram = np.bincount(bins, weights=weights, minlength=512).astype(np.float64)
    total = float(histogram.sum())
    if total > 0:
        histogram /= total
    weighted_colour = (rgb * foreground[..., None]).sum(axis=(0, 1)) / max(float(foreground.sum()), 1e-8)
    profile = np.concatenate((foreground.mean(axis=1), foreground.mean(axis=0)))
    summary = np.concatenate((weighted_colour, np.asarray([foreground.mean()]), profile)).astype(np.float64)
    return histogram, summary


def appearance_distance(
    left: tuple[np.ndarray, np.ndarray], right: tuple[np.ndarray, np.ndarray],
) -> float:
    histogram = float(np.abs(left[0] - right[0]).sum() / 2.0)
    summary = float(np.mean(np.abs(left[1] - right[1])))
    return histogram + 0.4 * summary


def pair_context_distance(
    card: tuple[np.ndarray, np.ndarray],
    left: tuple[np.ndarray, np.ndarray],
    right: tuple[np.ndarray, np.ndarray],
) -> float:
    """Compare a card with the combined appearance of both pair members."""
    pair_histogram = (left[0] + right[0]) / 2.0
    pair_summary = (left[1] + right[1]) / 2.0
    histogram = float(np.abs(card[0] - pair_histogram).sum() / 2.0)
    summary = float(np.mean(np.abs(card[1] - pair_summary)))
    return histogram + 0.4 * summary


def pair_distance(
    card: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray],
    left: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray],
    right: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray],
    card_appearance: tuple[np.ndarray, np.ndarray],
    left_appearance: tuple[np.ndarray, np.ndarray],
    right_appearance: tuple[np.ndarray, np.ndarray],
) -> tuple[float, str, float, float, float]:
    """Return one score for an Excel card against the complete photo pair."""
    left_score = distance(card, left)
    right_score = distance(card, right)
    matched_side = "LEFT" if left_score <= right_score else "RIGHT"
    best_view = min(left_score, right_score)
    best_appearance = min(
        appearance_distance(card_appearance, left_appearance),
        appearance_distance(card_appearance, right_appearance),
    )
    context = pair_context_distance(card_appearance, left_appearance, right_appearance)
    score = (
        DIRECT_PAIR_WEIGHT * best_view
        + BEST_VIEW_APPEARANCE_WEIGHT * best_appearance
        + PAIR_CONTEXT_WEIGHT * context
    )
    return score, matched_side, left_score, right_score, context


def minimum_assignment(cost: np.ndarray) -> list[int]:
    """Hungarian assignment for a rectangular N x M matrix where N <= M."""
    rows, columns = cost.shape
    if rows > columns:
        fail("There are fewer Excel image cards than required PDF looks.")
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
    result = [-1] * rows
    for column in range(1, columns + 1):
        if p[column]:
            result[p[column] - 1] = column - 1
    if any(value < 0 for value in result):
        fail("Could not assign every PDF look to one unique Excel card.")
    return result


def write_rows(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=fields, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def write_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def paste_panel(canvas: Image.Image, source: Path, x: int, title: str) -> None:
    with Image.open(source) as raw:
        image = ImageOps.exif_transpose(raw).convert("RGB")
        image.thumbnail((PANEL[0] - 28, PANEL[1] - 88), Image.Resampling.LANCZOS)
        background = Image.new("RGB", PANEL, "white")
        try:
            background.paste(image, ((PANEL[0] - image.width) // 2, 52 + (PANEL[1] - 88 - image.height) // 2))
            draw = ImageDraw.Draw(background)
            draw.rectangle((0, 0, PANEL[0] - 1, PANEL[1] - 1), outline="black", width=2)
            draw.text((14, 16), title, fill="black")
            canvas.paste(background, (x, 0))
        finally:
            background.close()


def render_alternative_proof(project: Path, row: dict[str, str], card: dict[str, str], hires: Path, proof: Path) -> None:
    excel = child(project, card["excel_image"])
    left, right = hires / row["left_filename"], hires / row["right_filename"]
    for source in (excel, left, right):
        if not source.is_file():
            fail(f"{row['look_id']}: missing alternative-proof source {source}")
    proof.parent.mkdir(parents=True, exist_ok=True)
    card_image = Image.new("RGB", (PANEL[0] * 3, PANEL[1]), "#dddddd")
    try:
        paste_panel(card_image, excel, 0, f"EXCEL {card['excel_sheet']} {card['excel_look_number']}")
        paste_panel(card_image, left, PANEL[0], f"LEFT {row['left_filename']}")
        paste_panel(card_image, right, PANEL[0] * 2, f"RIGHT {row['right_filename']}")
        temporary = proof.with_suffix(proof.suffix + ".tmp")
        card_image.save(temporary, "JPEG", quality=92, optimize=True)
        os.replace(temporary, proof)
    finally:
        card_image.close()
    if proof.stat().st_size < 1024:
        fail(f"{row['look_id']}: alternative proof is unexpectedly small.")


def _caption_source(
    project: Path,
    row: dict[str, str],
    hires: Path,
    missing_reference_dir: Path | None,
    side: str,
) -> Path:
    filename = row[f"{side}_filename"]
    source = hires / filename
    if filename.casefold().startswith(MISSING_PREFIX):
        if missing_reference_dir is None:
            fail(
                f"{row['look_id']}: quick-build placeholder has no PDF-reference fallback. "
                "Run the quick registry build again."
            )
        source = missing_reference_dir / f"{row['look_id']}_{side.upper()}.jpg"
    return source


def resolve(
    project: Path,
    registry: list[dict[str, str]],
    cards: list[dict[str, str]],
    hires: Path,
    missing_reference_dir: Path | None = None,
) -> tuple[list[int], list[dict[str, object]], list[str]]:
    if len(cards) < len(registry):
        fail(f"Excel has {len(cards)} visual cards but the PDF requires {len(registry)} looks.")
    card_features: list[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = []
    for card in cards:
        image = child(project, card["excel_image"])
        if not image.is_file():
            fail(f"Excel visual card is missing: {image}")
        card_features.append(image_feature(image))
    pairs: list[
        tuple[
            tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray],
            tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray],
        ]
    ] = []
    for row in registry:
        left = _caption_source(project, row, hires, missing_reference_dir, "left")
        right = _caption_source(project, row, hires, missing_reference_dir, "right")
        if not left.is_file() or not right.is_file():
            fail(f"{row['look_id']}: caption-mapping source pair is missing: {left}, {right}.")
        pairs.append((image_feature(left), image_feature(right)))
    # Excel contains one representative frame per credit card.  Precompute a
    # compact appearance signature once per image so the assignment can use
    # both views of each PDF look without repeatedly decoding or analysing them.
    card_appearances = [appearance_signature(feature) for feature in card_features]
    pair_appearances = [
        (appearance_signature(left), appearance_signature(right))
        for left, right in pairs
    ]
    costs = np.empty((len(registry), len(cards)), dtype=np.float64)
    side: list[list[str]] = [["" for _ in cards] for _ in registry]
    for look_index, (left, right) in enumerate(pairs):
        for card_index, card_feature in enumerate(card_features):
            score, matched_side, _left_score, _right_score, _context = pair_distance(
                card_feature,
                left,
                right,
                card_appearances[card_index],
                pair_appearances[look_index][0],
                pair_appearances[look_index][1],
            )
            costs[look_index, card_index] = score
            side[look_index][card_index] = matched_side
    assignment = minimum_assignment(costs)
    diagnostics: list[dict[str, object]] = []
    issues: list[str] = []
    for look_index, card_index in enumerate(assignment):
        ranking = np.argsort(costs[look_index])
        rank = int(np.where(ranking == card_index)[0][0]) + 1
        selected = float(costs[look_index, card_index])
        best = float(costs[look_index, ranking[0]])
        alternative = float(costs[look_index, ranking[1]]) if len(ranking) > 1 else math.inf
        margin = alternative - best
        record = {
            "look_id": registry[look_index]["look_id"],
            "excel_sheet": cards[card_index]["excel_sheet"],
            "excel_look_number": cards[card_index]["excel_look_number"],
            "matched_side": side[look_index][card_index],
            "assigned_score": round(selected, 7),
            "best_score": round(best, 7),
            "second_score": round(alternative, 7),
            "rank": rank,
            "margin": round(margin, 7),
        }
        diagnostics.append(record)
        # The score limit catches a non-identical styling image; the margin
        # limit catches visually interchangeable candidates.  Both are
        # intentionally conservative: uncertainty must remain PENDING.
        if needs_visual_review(record):
            issues.append(
                f"{record['look_id']} -> {record['excel_sheet']} {record['excel_look_number']} "
                f"(rank {rank}, score {selected:.4f}, margin {margin:.4f})"
            )
    return assignment, diagnostics, issues


def needs_visual_review(record: dict[str, object]) -> bool:
    """Keep only unambiguous image-identity matches out of the review queue."""
    return (
        int(record["rank"]) != 1
        or float(record["assigned_score"]) > 0.155
        or float(record["margin"]) < 0.0045
    )


def same_identity(left: dict[str, str], right: dict[str, str]) -> bool:
    return all(
        left[key] == right[key]
        for key in ("look_id", "excel_sheet", "excel_look_number", "excel_image", "left_filename", "right_filename", "evidence_file")
    )


def selection_path(project: Path, look_id: str) -> Path:
    return child(project, f"control/work/caption-map-selections/{look_id}.json")


def selected_identity_is_registered(project: Path, row: dict[str, str]) -> bool:
    path = selection_path(project, row["look_id"])
    if not path.is_file():
        return False
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return all(record.get(key) == row[key] for key in ("look_id", "excel_sheet", "excel_look_number", "excel_image", "left_filename", "right_filename"))


def assert_allowed_map(project: Path, existing: list[dict[str, str]], proposed: list[dict[str, str]]) -> None:
    if len(existing) != len(proposed):
        fail("Caption map length differs from the visual resolver output.")
    for expected, actual in zip(proposed, existing):
        if not same_identity(actual, expected) and not selected_identity_is_registered(project, actual):
            fail(
                f"{expected['look_id']}: saved caption map differs from the visual resolver without a "
                "recorded alternative-proof selection."
            )


def require_proof(project: Path, row: dict[str, str]) -> None:
    proof = child(project, row["evidence_file"])
    if not proof.is_file() or proof.stat().st_size < 1024:
        fail(f"{row['look_id']}: exact three-panel visual proof is missing.")


def parse_requested_looks(value: str, action: str, *, maximum: int = 5) -> list[str]:
    requested = [item.strip().upper() for item in value.split(",") if item.strip()]
    if not requested or len(requested) > maximum or len(set(requested)) != len(requested):
        quantity = "one to five" if maximum == 5 else f"one to {maximum}"
        fail(f"{action} requires {quantity} unique comma-separated LOOK_### IDs.")
    if any(not item.startswith("LOOK_") or not item[5:].isdigit() for item in requested):
        fail(f"{action} accepts only exact LOOK_### IDs.")
    return requested


def parse_observations(value: str, requested: list[str]) -> dict[str, str]:
    """Require one grounded visual description per actually viewed proof card."""
    observations: dict[str, str] = {}
    for item in (part.strip() for part in value.split("||") if part.strip()):
        if "=" not in item:
            fail("--notes must use LOOK_###=visual description, with entries separated by ||.")
        look_id, note = (part.strip() for part in item.split("=", 1))
        look_id = look_id.upper()
        if look_id in observations:
            fail(f"{look_id}: duplicate visual observation.")
        observations[look_id] = note
    if set(observations) != set(requested):
        fail("--notes must provide one visual observation for every requested LOOK_### and no others.")
    for look_id, note in observations.items():
        cue_categories = visible_identity_cue_categories(note)
        if len(note) < 28 or len(cue_categories) < 2:
            fail(
                f"{look_id}: visual observation is not specific enough. Describe at least two visible identity cues "
                "(model, garment, colour, bag, shoes, accessories). Use explicit labels such as "
                "'garment=white blazer; bag=black tote' when possible."
            )
    return observations


def alternative_catalog_path(project: Path) -> Path:
    return child(project, "control/work/caption-map-alternatives.tsv")


def parse_assignments(value: str, *, maximum: int = 5) -> dict[str, tuple[str, str]]:
    assignments: dict[str, tuple[str, str]] = {}
    for item in (part.strip() for part in value.split(",") if part.strip()):
        if "=" not in item or item.count(":") != 1:
            fail("--assignments must use LOOK_###=W:12 or LOOK_###=M:9, separated by commas.")
        look_id, excel = (part.strip() for part in item.split("=", 1))
        sheet, number = (part.strip().upper() for part in excel.split(":", 1))
        look_id = look_id.upper()
        if not look_id.startswith("LOOK_") or not look_id[5:].isdigit() or sheet not in {"W", "M"} or not number.isdigit():
            fail(f"Invalid alternative assignment: {item}")
        if look_id in assignments:
            fail(f"{look_id}: duplicate alternative assignment.")
        assignments[look_id] = (sheet, number)
    if not assignments or len(assignments) > maximum:
        quantity = "one to five" if maximum == 5 else f"one to {maximum}"
        fail(f"--assignments requires {quantity} explicit alternatives.")
    if len(set(assignments.values())) != len(assignments):
        fail("Alternative assignments must use different Excel cards.")
    return assignments


def main() -> None:
    parser = argparse.ArgumentParser(description="Resolve the verified Excel credit map from visual image identity.")
    parser.add_argument("project", type=Path)
    parser.add_argument(
        "--mode",
        choices=(
            "propose", "seed", "reset-visual-review", "repair", "confirm-strong", "confirm-review",
            "alternative-proofs", "select-alternatives", "confirm", "audit",
            "quick-seed", "quick-autonomous", "quick-fallback",
        ),
        default="propose",
    )
    parser.add_argument("--registry", default="control/work/look-register.tsv")
    parser.add_argument("--index", default="control/work/_mat/excel-images/index.tsv")
    parser.add_argument("--hires", default="control/work/_mat/hires")
    parser.add_argument(
        "--missing-reference-dir",
        default="",
        help="Quick-build only: use extracted PDF-reference photos for registry placeholders while mapping Excel credits.",
    )
    parser.add_argument("--map", dest="caption_map", default="control/work/caption-map.tsv")
    parser.add_argument("--candidates", default="control/work/caption-map-candidates.tsv")
    parser.add_argument("--looks", default="", help="comma-separated LOOK_### IDs for one reviewed batch (maximum five)")
    parser.add_argument("--notes", default="", help="LOOK_###=grounded visual description entries separated by ||")
    parser.add_argument("--assignments", default="", help="LOOK_###=W:12 or LOOK_###=M:9 entries separated by commas")
    parser.add_argument(
        "--controller-batch",
        action="store_true",
        help="Allow LOOKBOOKBOT to atomically save a larger already-inspected rematch batch.",
    )
    args = parser.parse_args()
    project = args.project.resolve()
    registry = read_rows(child(project, args.registry), REGISTRY_FIELDS)
    cards = read_rows(child(project, args.index), INDEX_FIELDS)
    if not registry:
        fail("The PDF-authoritative registry has no looks.")
    for index, row in enumerate(registry, start=1):
        if row["look_id"] != f"LOOK_{index:03}" or not row["left_filename"] or not row["right_filename"]:
            fail(f"Registry row {index} is incomplete.")
    missing_reference_dir = child(project, args.missing_reference_dir) if args.missing_reference_dir else None
    assignment, diagnostics, issues = resolve(
        project, registry, cards, child(project, args.hires), missing_reference_dir,
    )
    candidate_rows = [{key: str(value) for key, value in record.items()} for record in diagnostics]
    write_rows(child(project, args.candidates), list(candidate_rows[0]) if candidate_rows else [], candidate_rows)
    if issues and args.mode in {"propose", "confirm", "audit"}:
        fail("Automatic visual mapping is not decisive; keep the listed rows PENDING and inspect only those proof cards. " + "; ".join(issues[:6]))
    proposed: list[dict[str, str]] = []
    for index, card_index in enumerate(assignment):
        source, card = registry[index], cards[card_index]
        proposed.append({
            "look_id": source["look_id"],
            "excel_sheet": card["excel_sheet"],
            "excel_look_number": card["excel_look_number"],
            "excel_image": card["excel_image"],
            "left_filename": source["left_filename"],
            "right_filename": source["right_filename"],
            "evidence_file": f"control/work/mapping-evidence/{source['look_id']}.jpg",
            # ``quick-seed`` is retained as a low-level compatibility command
            # for callers that explicitly want the old non-review behaviour.
            # The application uses ``quick-autonomous``: it keeps every
            # proposal PENDING until the selected vision provider has checked
            # the exact proof card and, when needed, chosen a replacement.
            "visual_status": "CONFIRMED" if args.mode in {"quick-seed", "quick-fallback"} else "PENDING",
        })
    map_path = child(project, args.caption_map)
    review = {str(record["look_id"]): record for record in diagnostics if needs_visual_review(record)}
    batch_limit = 50 if args.controller_batch else 5
    if args.mode == "propose":
        write_rows(map_path, MAP_FIELDS, proposed)
        print(f"PASS caption-map proposal: {len(proposed)} unique Excel cards resolved from image identity. Render proof cards before confirmation.")
    elif args.mode == "seed":
        if map_path.exists():
            existing = read_rows(map_path, MAP_FIELDS)
            # `prepare_caption_mapping.py` deliberately pre-populates the
            # immutable PDF-hire side of every blank row.  Only an Excel
            # identity is a prior credit decision that must never be
            # overwritten by the seeder.
            nonempty = any(
                row[key]
                for row in existing
                for key in ("excel_sheet", "excel_look_number", "excel_image")
            )
            if nonempty:
                assert_allowed_map(project, existing, proposed)
            else:
                write_rows(map_path, MAP_FIELDS, proposed)
        else:
            write_rows(map_path, MAP_FIELDS, proposed)
        queue_fields = ["look_id", "excel_sheet", "excel_look_number", "matched_side", "assigned_score", "best_score", "second_score", "rank", "margin", "reason"]
        queue_rows = []
        for record in diagnostics:
            queue_rows.append({
                **{key: str(record[key]) for key in queue_fields if key in record},
                "reason": (
                    "automatic suggestion requires three-panel visual identity review"
                    if str(record["look_id"]) not in review else "ambiguous suggestion requires alternative visual review"
                ),
            })
        queue_path = child(project, "control/work/caption-map-review-queue.tsv")
        write_rows(queue_path, queue_fields, queue_rows)
        print(
            f"CAPTION MAP SEEDED: {len(proposed)} proposed one-to-one image assignments; no row is automatically "
            "confirmed. Render all proof cards and visually confirm every exact card in batches of at most five."
        )
        print(f"Review queue: {queue_path}")
    elif args.mode in {"quick-seed", "quick-autonomous", "quick-fallback"}:
        if map_path.exists():
            existing = read_rows(map_path, MAP_FIELDS)
            has_prior_decisions = any(
                row[key]
                for row in existing
                for key in ("excel_sheet", "excel_look_number", "excel_image")
            )
            if args.mode != "quick-fallback" and (len(existing) != len(proposed) or (
                has_prior_decisions
                and any(not same_identity(actual, expected) for actual, expected in zip(existing, proposed))
            )):
                fail("Quick caption map would replace an existing different map; start a new quick build instead.")
        write_rows(map_path, MAP_FIELDS, proposed)
        if issues and args.mode == "quick-seed":
            print(
                "QUICK CAPTION MAP WARNING: automatic identity was not decisive for "
                + ", ".join(record["look_id"] for record in diagnostics if any(record["look_id"] in issue for issue in issues))
                + "; assignments were retained for this non-review quick build and are listed in the candidate report."
            )
        if args.mode == "quick-autonomous":
            print(
                f"QUICK AUTONOMOUS CAPTION MAP READY: {len(proposed)} one-to-one proposals queued for "
                "automatic vision adjudication before the credits stage."
            )
        elif args.mode == "quick-fallback":
            print(
                f"QUICK FALLBACK CAPTION MAP READY: {len(proposed)} deterministic one-to-one assignments "
                "restored after a temporary automatic vision-provider failure."
            )
        else:
            print(
                f"QUICK CAPTION MAP READY: {len(proposed)} one-to-one Excel credit assignments confirmed from "
                "PDF/reference image identity. The low-level quick-seed command skips visual credit-proof inspection."
            )
    elif args.mode == "reset-visual-review":
        # A stale test or a rejected visual observation must never retain a
        # CONFIRMED flag.  Resetting restores only the frozen original visual
        # proposal; it does not invent credit data and is safe before init.
        write_rows(map_path, MAP_FIELDS, proposed)
        selections = child(project, "control/work/caption-map-selections")
        if selections.is_dir():
            for record in selections.glob("LOOK_*.json"):
                record.unlink()
        print(f"CAPTION MAP RESET: {len(proposed)} rows returned to PENDING original visual proposals.")
    elif args.mode == "confirm-strong":
        existing = read_rows(map_path, MAP_FIELDS)
        assert_allowed_map(project, existing, proposed)
        for row in existing:
            require_proof(project, row)
        pending = sum(1 for row in existing if row["visual_status"] != "CONFIRMED")
        print(
            f"NO AUTOMATIC CONFIRMATION: {pending}/{len(existing)} rows remain PENDING until their exact three-panel "
            "proof is actually viewed and confirmed with grounded identity cues."
        )
    elif args.mode == "repair":
        existing = read_rows(map_path, MAP_FIELDS)
        repaired: list[str] = []
        for expected, actual in zip(proposed, existing):
            look_id = expected["look_id"]
            if same_identity(actual, expected) or selected_identity_is_registered(project, actual):
                continue
            else:
                actual.update(expected)
                actual["visual_status"] = "PENDING"
                repaired.append(look_id)
        write_rows(map_path, MAP_FIELDS, existing)
        print("CAPTION MAP REPAIRED: " + (", ".join(repaired) if repaired else "no drift found") + ".")
    elif args.mode == "alternative-proofs":
        requested = parse_requested_looks(args.looks, "alternative-proofs", maximum=batch_limit)
        requested_assignments = parse_assignments(args.assignments, maximum=batch_limit)
        if set(requested_assignments) != set(requested):
            fail("alternative-proofs must name the same LOOK_### IDs in --looks and --assignments.")
        existing = read_rows(map_path, MAP_FIELDS)
        assert_allowed_map(project, existing, proposed)
        by_look = {row["look_id"]: row for row in existing}
        alternatives: list[dict[str, str]] = []
        root = child(project, "control/work/caption-map-alternatives")
        for look_id in requested:
            row = by_look.get(look_id)
            if row is None or row["visual_status"] != "PENDING":
                fail(f"{look_id}: alternative candidates may be generated only for a PENDING row.")
            requested_pair = requested_assignments[look_id]
            for card in cards:
                if (card["excel_sheet"], card["excel_look_number"]) != requested_pair:
                    continue
                filename = f"{card['excel_sheet']}_{int(card['excel_look_number']):03}.jpg"
                proof = root / look_id / filename
                render_alternative_proof(project, row, card, child(project, args.hires), proof)
                alternatives.append({
                    "look_id": look_id,
                    "excel_sheet": card["excel_sheet"],
                    "excel_look_number": card["excel_look_number"],
                    "excel_image": card["excel_image"],
                    "left_filename": row["left_filename"],
                    "right_filename": row["right_filename"],
                    "evidence_file": str(proof.relative_to(project)).replace("\\", "/"),
                    "evidence_sha256": digest(proof),
                })
        # Keep entries for prior requested looks as well: a later atomic swap
        # may use them, but every selected row must have a current exact card.
        catalog = alternative_catalog_path(project)
        retained: list[dict[str, str]] = []
        if catalog.is_file():
            retained = [row for row in read_rows(catalog, ALTERNATIVE_FIELDS) if row["look_id"] not in set(requested)]
        write_rows(catalog, ALTERNATIVE_FIELDS, retained + alternatives)
        print(
            f"ALTERNATIVE PROOFS READY: {len(alternatives)} exact three-panel cards for {', '.join(requested)}. "
            "View the selected candidate cards before select-alternatives."
        )
        print(f"Alternatives: {catalog}")
    elif args.mode == "select-alternatives":
        assignments = parse_assignments(args.assignments, maximum=batch_limit)
        existing = read_rows(map_path, MAP_FIELDS)
        assert_allowed_map(project, existing, proposed)
        by_look = {row["look_id"]: row for row in existing}
        for look_id in assignments:
            row = by_look.get(look_id)
            if row is None or row["visual_status"] != "PENDING":
                fail(f"{look_id}: an alternative may replace only a PENDING row.")
        catalog = read_rows(alternative_catalog_path(project), ALTERNATIVE_FIELDS)
        alternatives = {(row["look_id"], row["excel_sheet"], row["excel_look_number"]): row for row in catalog}
        index_by_pair = {(row["excel_sheet"], row["excel_look_number"]): row for row in cards}
        current_owners = {(row["excel_sheet"], row["excel_look_number"]): row["look_id"] for row in existing}
        requested = set(assignments)
        for look_id, pair in assignments.items():
            candidate = alternatives.get((look_id, *pair))
            if candidate is None:
                fail(f"{look_id}: generate and view the exact alternative proof for {pair[0]} {pair[1]} before selecting it.")
            proof = child(project, candidate["evidence_file"])
            if not proof.is_file() or digest(proof) != candidate["evidence_sha256"]:
                fail(f"{look_id}: alternative proof changed or is missing; regenerate and view it again.")
            owner = current_owners.get(pair)
            if owner is not None and owner not in requested:
                fail(
                    f"{look_id}: Excel {pair[0]} {pair[1]} is still owned by {owner}. "
                    "Select the complete PENDING swap atomically; never steal a confirmed assignment."
                )
            if pair not in index_by_pair:
                fail(f"{look_id}: Excel {pair[0]} {pair[1]} is not in the controlled catalogue index.")
        for look_id, pair in assignments.items():
            row, card = by_look[look_id], index_by_pair[pair]
            row.update({
                "excel_sheet": card["excel_sheet"],
                "excel_look_number": card["excel_look_number"],
                "excel_image": card["excel_image"],
                "evidence_file": f"control/work/mapping-evidence/{look_id}.jpg",
                "visual_status": "PENDING",
            })
            write_json(selection_path(project, look_id), {
                "schema": 1,
                "look_id": look_id,
                "excel_sheet": card["excel_sheet"],
                "excel_look_number": card["excel_look_number"],
                "excel_image": card["excel_image"],
                "left_filename": row["left_filename"],
                "right_filename": row["right_filename"],
                "alternative_evidence": alternatives[(look_id, *pair)]["evidence_file"],
                "alternative_evidence_sha256": alternatives[(look_id, *pair)]["evidence_sha256"],
            })
        pairs = [(row["excel_sheet"], row["excel_look_number"]) for row in existing]
        if len(set(pairs)) != len(pairs):
            fail("Alternative selection would leave duplicate Excel cards; map was not written.")
        write_rows(map_path, MAP_FIELDS, existing)
        print(
            "ALTERNATIVE SELECTION SAVED: " + ", ".join(
                f"{look_id}={sheet}:{number}" for look_id, (sheet, number) in assignments.items()
            ) + ". Re-render the ordinary exact proof cards, view them, then confirm-review with grounded notes."
        )
    elif args.mode == "confirm-review":
        requested = parse_requested_looks(args.looks, "confirm-review")
        observations = parse_observations(args.notes, requested)
        existing = read_rows(map_path, MAP_FIELDS)
        assert_allowed_map(project, existing, proposed)
        by_look = {row["look_id"]: row for row in existing}
        for look_id in requested:
            row = by_look.get(look_id)
            if row is None or row["visual_status"] != "PENDING":
                fail(f"{look_id}: must be a still-PENDING visually reviewed row.")
            require_proof(project, row)
            proof = child(project, row["evidence_file"])
            write_json(child(project, f"control/work/caption-map-observations/{look_id}.json"), {
                "schema": 1,
                "look_id": look_id,
                "excel_sheet": row["excel_sheet"],
                "excel_look_number": row["excel_look_number"],
                "excel_image": row["excel_image"],
                "left_filename": row["left_filename"],
                "right_filename": row["right_filename"],
                "evidence_file": row["evidence_file"],
                "evidence_sha256": digest(proof),
                "visual_identity_description": observations[look_id],
            })
            row["visual_status"] = "CONFIRMED"
        write_rows(map_path, MAP_FIELDS, existing)
        remaining = sum(1 for row in existing if row["visual_status"] != "CONFIRMED")
        print(f"VISUAL REVIEW CONFIRMED: {', '.join(requested)}. Remaining rows: {remaining}.")
    elif args.mode == "confirm":
        fail("Bulk confirmation is forbidden. Inspect one to five exact proof cards and use confirm-review with grounded notes.")
    else:
        print(f"PASS caption-map audit: {len(proposed)} visual assignments are decisive. No map was changed.")
    print(f"Candidates: {child(project, args.candidates)}")


if __name__ == "__main__":
    main()
