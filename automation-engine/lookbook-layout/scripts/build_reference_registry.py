"""Build the complete PDF-authoritative photo registry before controller init.

The source PDF defines the order and the left/right position of every look.  This
tool renders the two visible photographs from each look page, matches them against
the supplied hires by image content, writes the full registry, and leaves a visual
proof pack in ``control/work``.  It deliberately never reads an Excel workbook.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import gc
import hashlib
import io
import json
import math
import re
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageOps
from pypdf import PdfReader

from pdf_page_geometry import draw_fully_covered_by_later, draw_intersects_page
from project_materials import project_hires, is_project_hires


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
PLACEHOLDER_PREFIX = "__lbb_missing_"
MISSING_REFERENCE_DIR = Path("control/work/missing-photo-reference")
# The original RGB/edge score is useful for near-identical exports, but it can
# confuse two garments with a similar silhouette.  The foreground appearance
# score supplies colour/texture evidence from the model and garment while the
# pair score below makes the left/right decision jointly.
BASE_MATCH_WEIGHT = 0.75
APPEARANCE_MATCH_WEIGHT = 0.25
PAIR_MARGIN = 0.004
PAIR_SCORE_LIMIT = 0.32
DRAW = re.compile(
    rb"q\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+cm\s+/([^\s/]+)\s+Do"
)


def fail(message: str) -> None:
    raise SystemExit(f"Registry build blocked: {message}")


def path_from(project: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else project / path


def require_in_work(project: Path, path: Path, label: str) -> None:
    if label == "Hires folder" and is_project_hires(project, path):
        return
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


def visible_pdf_draws(page) -> list[tuple[tuple[float, float, float, float, float, float], Image.Image]]:
    """Return the actually visible photo placements, ordered left to right.

    This is deliberately separate from ``visible_pdf_pair`` so the builder can
    inspect the first page before deciding whether it is a cover.  A cover may
    contain a logo or a decorative image, while a look page has exactly two
    visible photo placements.
    """
    names = {item.name.rsplit(".", 1)[0]: item.image for item in page.images}
    content = page.get_contents()
    if content is None:
        return []
    placements: list[tuple[tuple[float, float, float, float, float, float], Image.Image]] = []
    for match in DRAW.finditer(content.get_data()):
        matrix = tuple(float(match.group(index)) for index in range(1, 7))
        if not draw_intersects_page(page, matrix):
            # Ignore image XObjects whose complete transformed bounds are
            # outside the page.  InDesign/PDF exports may retain such stale
            # placements alongside the two photographs that are actually
            # visible on the reference page.
            continue
        name = match.group(7).decode("latin-1")
        image = names.get(name)
        if image is None:
            continue
        placements.append((matrix, image))
    visible = [
        (matrix, image)
        for index, (matrix, image) in enumerate(placements)
        if not draw_fully_covered_by_later(
            page,
            matrix,
            [later_matrix for later_matrix, _later_image in placements[index + 1 :]],
        )
    ]
    return sorted(visible, key=lambda item: item[0][4])


def visible_pdf_pair(page, page_number: int) -> tuple[Image.Image, Image.Image]:
    """Return the two visible source images, ordered by horizontal page position.

    A PDF can retain an extra image XObject below the final right image.  Counting
    XObjects would treat that hidden source as a third look photo.  The page content
    stream instead identifies actual placements; at one rectangle only the final
    draw is visible.
    """
    draws = visible_pdf_draws(page)
    if len(draws) != 2:
        fail(
            f"PDF page {page_number} must visibly place exactly two look photographs; found {len(draws)}."
        )
    return draws[0][1], draws[1][1]


def detect_cover_pages(reader) -> int:
    """Detect the optional leading cover without assuming page one is a cover.

    A controlled source may be either ``cover + N look pages`` or simply ``N look
    pages``.  We only accept an automatic decision when the first page is clearly a
    two-photo look page or the second page is clearly the first look after a
    non-look cover.  Other layouts remain explicit errors instead of silently
    shifting every look by one page.
    """
    if not reader.pages:
        fail("Reference PDF has no pages.")
    first_count = len(visible_pdf_draws(reader.pages[0]))
    if first_count == 2:
        return 0
    if len(reader.pages) > 1 and len(visible_pdf_draws(reader.pages[1])) == 2:
        return 1
    fail(
        "Cannot automatically determine the optional cover page: the first page "
        "is not a two-photo look page and the next page is not a valid look page. "
        "Use --cover-pages 0 or --cover-pages 1 after checking the reference PDF."
    )


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


def appearance_signature(features: tuple[np.ndarray, np.ndarray, np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
    """Return foreground-weighted colour and spatial appearance evidence.

    PDF reference images and hires can have different crops and JPEG quality.
    A coarse foreground colour histogram plus low-resolution colour/mask
    profiles is therefore more stable than comparing every pixel.  The
    foreground mask is the same conservative mask used by ``feature`` and is
    intentionally not a face/name/filename heuristic.
    """
    rgb, _edge, foreground = features
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
    """Compare the foreground appearance of two same-role photographs."""
    histogram = float(np.abs(left[0] - right[0]).sum() / 2.0)
    summary = float(np.mean(np.abs(left[1] - right[1])))
    return histogram + 0.4 * summary


def matching_distance(
    left: tuple[np.ndarray, np.ndarray, np.ndarray],
    right: tuple[np.ndarray, np.ndarray, np.ndarray],
    left_appearance: tuple[np.ndarray, np.ndarray],
    right_appearance: tuple[np.ndarray, np.ndarray],
) -> float:
    """Blend pixel structure with foreground appearance for registry matching."""
    return (
        BASE_MATCH_WEIGHT * distance(left, right)
        + APPEARANCE_MATCH_WEIGHT * appearance_distance(left_appearance, right_appearance)
    )


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


def _is_placeholder(path: Path) -> bool:
    return path.name.casefold().startswith(PLACEHOLDER_PREFIX)


def _distinct_hires(
    files: list[Path], preferred_names: set[str] | None = None,
) -> tuple[list[Path], list[dict[str, object]]]:
    """Collapse exact byte copies BEFORE assignment and confidence margins.

    Filenames only choose a stable representative of an already proven byte
    group; they never establish image identity. In particular, a retouched
    ``copy`` with different bytes remains an independent candidate. No file
    is removed, renamed, rewritten, or changed into a placeholder here.
    Keep an existing registry's member when re-verifying a completed project.
    """
    preferred = {name.casefold() for name in (preferred_names or set())}
    sizes: dict[int, list[Path]] = {}
    for file in files:
        sizes.setdefault(file.stat().st_size, []).append(file)
    representatives: list[Path] = []
    duplicates: list[dict[str, object]] = []
    for size, same_size in sizes.items():
        if len(same_size) == 1:
            representatives.extend(same_size)
            continue
        groups: dict[str, list[Path]] = {}
        for file in same_size:
            with file.open("rb") as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
            groups.setdefault(digest, []).append(file)
        for digest, members in groups.items():
            def preference(file: Path) -> tuple[bool, bool, str, str]:
                copy_suffix = bool(re.search(r"(?:[ _-]copy(?:\s*\(\d+\)|\s+\d+)?|[ _-]копия(?:\s*\(\d+\)|\s+\d+)?)$", file.stem, re.IGNORECASE))
                return file.name.casefold() not in preferred, copy_suffix, file.name.casefold(), file.name
            selected = min(members, key=preference)
            representatives.append(selected)
            if len(members) > 1:
                duplicates.append({
                    "selected": selected.name, "sha256": digest, "bytes": size,
                    "copies": sorted(file.name for file in members if file != selected),
                })
    representatives.sort(key=lambda file: (file.name.casefold(), file.name))
    duplicates.sort(key=lambda group: str(group["selected"]).casefold())
    return representatives, duplicates


def _write_blank_placeholder(path: Path) -> Image.Image:
    """Create a neutral image that can be placed in a fixed InDesign frame.

    Quick mode intentionally leaves the frame usable but visually empty.  The
    actual PDF-reference image is stored separately for credit mapping; it is
    never placed into the INDD as a substitute for the missing retouch.
    """
    image = Image.new("RGB", (360, 540), "white")
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, "JPEG", quality=95, optimize=True)
    return image


def _clear_previous_quick_artifacts(project: Path, hires: Path) -> None:
    """Remove only placeholders owned by an earlier quick registry pass."""
    manifest_path = project / "control" / "work" / "quick-missing-photos.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest = {}
        placeholders = manifest.get("placeholders", {}) if isinstance(manifest, dict) else {}
        if isinstance(placeholders, dict):
            for value in placeholders.values():
                if not isinstance(value, dict):
                    continue
                for filename in value.values():
                    if isinstance(filename, str) and filename.startswith(PLACEHOLDER_PREFIX):
                        (hires / filename).unlink(missing_ok=True)
        manifest_path.unlink(missing_ok=True)
    reference_dir = project / MISSING_REFERENCE_DIR
    if reference_dir.is_dir():
        for path in reference_dir.glob("LOOK_*.jpg"):
            path.unlink(missing_ok=True)


def _diagnose_assignment(
    target_indices: list[int],
    assignment: list[int],
    cost: np.ndarray,
    files: list[Path],
) -> tuple[list[dict[str, object]], list[int]]:
    diagnostics: list[dict[str, object]] = []
    problems: list[int] = []
    for position, target_index in enumerate(target_indices):
        source_index = assignment[position]
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
            problems.append(target_index)
    return diagnostics, problems


def _pair_decisive_looks(
    target_indices: list[int], assignment: list[int], cost: np.ndarray,
) -> set[int]:
    """Return looks whose assigned left/right pair is jointly decisive.

    A single full-length image can be visually close to several other looks,
    while its close-up is an exact anchor (or the other way around).  Treating
    the two images as independent therefore creates false ``missing`` looks.
    The pair check keeps the one-to-one assignment, but compares the selected
    ordered pair with every other distinct hire pair before declaring it
    unresolved.
    """
    by_target = {target: assignment[position] for position, target in enumerate(target_indices)}
    decisive: set[int] = set()
    for look in sorted({target // 2 + 1 for target in target_indices}):
        left_target = (look - 1) * 2
        right_target = left_target + 1
        if left_target not in by_target or right_target not in by_target:
            continue
        left_source = by_target[left_target]
        right_source = by_target[right_target]
        if left_source == right_source:
            continue
        pair_scores = cost[left_target, :, None] + cost[right_target, None, :]
        pair_scores = np.asarray(pair_scores, dtype=np.float64)
        pair_scores[np.diag_indices_from(pair_scores)] = np.inf
        flattened = pair_scores.ravel()
        order = np.argsort(flattened)
        selected_index = left_source * pair_scores.shape[1] + right_source
        selected_rank = int(np.where(order == selected_index)[0][0]) + 1
        best = float(flattened[order[0]])
        second = float(flattened[order[1]]) if len(order) > 1 else math.inf
        selected = float(pair_scores[left_source, right_source])
        if (
            selected_rank == 1
            and selected <= PAIR_SCORE_LIMIT
            and second - best >= PAIR_MARGIN
        ):
            decisive.add(look)
    return decisive


def _quick_missing_looks(
    cost: np.ndarray,
    files: list[Path],
    target_count: int,
) -> tuple[dict[int, int], set[int], list[dict[str, object]], list[str]]:
    """Resolve reliable photos and quarantine whole uncertain looks.

    The initial Hungarian pass can let a wrong file consume the best candidate
    for another look.  In quick mode, an uncertain pair is therefore removed as
    a complete two-photo look and the remaining hires are assigned again.  This
    preserves correct one-to-one matches for all available looks while leaving
    every unresolved look explicitly blank instead of silently placing a wrong
    photograph.
    """
    all_targets = list(range(target_count))
    missing_looks: set[int] = set()
    final_diagnostics: list[dict[str, object]] = []
    final_assignment: dict[int, int] = {}
    while True:
        active = [target for target in all_targets if target // 2 + 1 not in missing_looks]
        while len(active) > len(files):
            # There are not enough source files for the remaining targets. Put
            # the least visually supported complete look into the explicit
            # missing set until the rectangular assignment is possible.
            candidate_looks = sorted({target // 2 + 1 for target in active})
            look = max(
                candidate_looks,
                key=lambda value: max(float(np.min(cost[target])) for target in active if target // 2 + 1 == value),
            )
            missing_looks.add(look)
            active = [target for target in active if target // 2 + 1 != look]
        if not active:
            final_diagnostics = []
            break
        assignment = minimum_assignment(cost[active, :])
        diagnostics, problems = _diagnose_assignment(active, assignment, cost, files)
        pair_decisive = _pair_decisive_looks(active, assignment, cost)
        new_looks = {
            target // 2 + 1 for target in problems
            if target // 2 + 1 not in pair_decisive
        }
        if not new_looks - missing_looks:
            final_diagnostics = diagnostics
            final_assignment = {target: assignment[position] for position, target in enumerate(active)}
            break
        missing_looks.update(new_looks)
    descriptions: list[str] = []
    by_target = {int(item["target"]): item for item in final_diagnostics}
    for target in all_targets:
        look = target // 2 + 1
        if look in missing_looks:
            descriptions.append(f"LOOK_{look:03}: PDF image {target + 1} has no decisive hire match; the selected non-review mode leaves this photo blank.")
        elif target in by_target and int(by_target[target]["rank"]) != 1:
            descriptions.append(f"PDF image {target + 1}: {by_target[target]['selected']} remains non-decisive.")
    return final_assignment, missing_looks, final_diagnostics, descriptions


def _file_sha256(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def restore_photo_recovery(project, reference, files, assignment, missing, prior_rows):
    """Replay only hash-bound, exact-pair visual recoveries, never scores."""
    receipt = project / "control/work/photo-recovery/accepted.json"
    if not receipt.is_file():
        return
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    if payload.get("reference_sha256") != _file_sha256(reference):
        fail("Photo recovery belongs to a different reference PDF.")
    indexed = {file.name: index for index, file in enumerate(files)}
    previous = {row["look_id"]: row for row in (prior_rows or [])}
    for look, entry in payload.get("assignments", {}).items():
        row = previous.get(look)
        if row is None:
            fail("Recovered photo registry is missing; do not reconstruct it silently.")
        proof = (project / entry.get("proof", "")).resolve()
        try:
            proof.relative_to((project / "control/work/photo-recovery").resolve())
        except ValueError:
            fail(f"{look}: recovered proof is outside the controlled evidence folder.")
        if not proof.is_file() or _file_sha256(proof) != entry.get("proof_sha256"):
            fail(f"{look}: exact photo recovery proof changed or is missing.")
        for side, offset in (("left", 0), ("right", 1)):
            filename = entry.get(side)
            if not filename:
                continue
            if row[f"{side}_filename"] != filename or filename not in indexed:
                fail(f"{look}: recovered photograph no longer agrees with the saved registry.")
            index = indexed[filename]
            if _file_sha256(files[index]) != entry[f"{side}_sha256"]:
                fail(f"{look}: recovered source photograph changed; a fresh visual check is required.")
            assignment[(int(look[5:]) - 1) * 2 + offset] = index
        targets = [(int(look[5:]) - 1) * 2 + offset for offset in (0, 1)]
        if all(target in assignment for target in targets):
            missing.discard(int(look[5:]))
    if len(set(assignment.values())) != len(assignment):
        fail("Recovered source photograph is also assigned to another PDF slot.")


def write_photo_recovery_queue(project, reference, registry, files, previews, pages, cost, assignment, missing):
    """Readable exhaustive unused-photo search, separate from 'file absent'."""
    folder = project / "control/work/photo-recovery"
    folder.mkdir(parents=True, exist_ok=True)
    unused = [index for index in range(len(files)) if index not in set(assignment.values())] if missing else []
    candidates = {}
    for index in unused:
        label = f"P{index + 1:04}"
        candidates[label] = {"filename": files[index].name, "sha256": _file_sha256(files[index])}
    targets = []
    for number in sorted(missing):
        look = f"LOOK_{number:03}"
        target_folder = folder / look
        target_folder.mkdir(exist_ok=True)
        pair = Image.new("RGB", (720, 570), "white")
        draw = ImageDraw.Draw(pair)
        for offset, image in enumerate(pages[number - 1]):
            pair.paste(image, (offset * 360, 30))
            draw.text((offset * 360 + 10, 10), "PDF LEFT" if offset == 0 else "PDF RIGHT", fill="black")
        reference_pair = target_folder / "reference-pair.jpg"
        pair.save(reference_pair, quality=95)
        pair.close()
        ranked = sorted(unused, key=lambda index: min(cost[(number - 1) * 2, index], cost[(number - 1) * 2 + 1, index]))
        boards = []
        for start in range(0, len(ranked), 4):
            selected = ranked[start:start + 4]
            board = Image.new("RGB", (720, 1140), "white")
            draw = ImageDraw.Draw(board)
            labels = []
            for position, index in enumerate(selected):
                x, y = (position % 2) * 360, (position // 2) * 570
                label = f"P{index + 1:04}"
                labels.append(label)
                draw.text((x + 10, y + 10), label, fill="black")
                board.paste(previews[index], (x, y + 30))
            path = target_folder / f"candidates-{start // 4 + 1:02}.jpg"
            board.save(path, quality=92)
            board.close()
            boards.append({"path": path.relative_to(project).as_posix(), "sha256": _file_sha256(path), "labels": labels})
        target = {"look_id": look, "reference_pair": reference_pair.relative_to(project).as_posix(),
                  "reference_pair_sha256": _file_sha256(reference_pair), "boards": boards}
        for side, offset in (("left", 0), ("right", 1)):
            index = assignment.get((number - 1) * 2 + offset)
            if index is not None:
                target[f"current_{side}"] = {"filename": files[index].name, "sha256": _file_sha256(files[index])}
        targets.append(target)
    payload = {"schema": 1, "reference_sha256": _file_sha256(reference),
               "registry_sha256": _file_sha256(registry), "candidates": candidates, "targets": targets}
    (folder / "manifest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def build(
    project: Path,
    reference: Path,
    hires: Path,
    registry: Path,
    cover_pages: int | None,
    dry_run: bool,
    allow_missing: bool = False,
    photo_only: bool = False,
    defer_review: bool = False,
) -> None:
    project = project.resolve()
    require_in_work(project, reference, "Reference PDF")
    require_in_work(project, hires, "Hires folder")
    require_in_work(project, registry, "Registry")
    if not reference.is_file():
        fail(f"Reference PDF does not exist: {reference}")
    if not hires.is_dir():
        fail(f"Hires folder does not exist: {hires}")
    if cover_pages is not None and cover_pages < 0:
        fail("--cover-pages cannot be negative.")
    if allow_missing:
        _clear_previous_quick_artifacts(project, hires)
    files = sorted(
        (
            item for item in hires.iterdir()
            if item.is_file()
            and item.suffix.lower() in {".jpg", ".jpeg"}
            and not _is_placeholder(item)
        ),
        key=lambda item: item.name.lower(),
    )
    if not files:
        fail("The hires folder does not contain JPG files.")
    all_files = files
    prior_rows: list[dict[str, str]] | None = None
    if registry.exists() and not registry_is_blank_bootstrap(registry):
        prior_rows = existing_registry(registry)
    preferred_names = {
        row[field] for row in (prior_rows or []) for field in ("left_filename", "right_filename")
    }
    files, duplicate_groups = _distinct_hires(all_files, preferred_names)
    if duplicate_groups:
        print(
            f"HIRES DEDUPLICATED: {len(all_files)} files, {len(files)} distinct photographs, "
            f"{len(all_files) - len(files)} exact copies excluded from candidate competition; "
            "all source files preserved."
        )
    reader = PdfReader(str(reference))
    if cover_pages is None:
        cover_pages = detect_cover_pages(reader)
    if len(reader.pages) <= cover_pages:
        fail("Reference PDF has no look pages after the cover.")

    pdf_features: list[tuple[np.ndarray, np.ndarray, np.ndarray]] = []
    page_images: list[tuple[Image.Image, Image.Image]] = []
    # Embedded JPEG decoding can miss PDF Decode arrays, CMYK profiles and
    # clipping, producing false colours or a doubled photo. Match the pixels
    # actually displayed by a PDF viewer instead of those raw XObjects.
    from lookbook_gate import render_pdf_pages
    from reference_photo_render import rendered_photo_pair
    rendered_pages = render_pdf_pages(
        reference, project / "control" / "work" / "reference-rendered-photos",
        list(range(cover_pages + 1, len(reader.pages) + 1)), 120, crop_box=True,
    )
    for page_number in range(cover_pages, len(reader.pages)):
        page = reader.pages[page_number]
        draws = visible_pdf_draws(page)
        if len(draws) != 2:
            fail(f"PDF page {page_number + 1} must visibly place exactly two look photographs; found {len(draws)}.")
        pair = rendered_photo_pair(page, rendered_pages[page_number + 1], [matrix for matrix, _image in draws])
        # Keep only a proof-sized image. Retaining 100 decoded 25-MB source JPEGs
        # consumes several gigabytes and can abort preparation before a registry exists.
        page_images.append((fit_preview(pair[0], (360, 540)), fit_preview(pair[1], (360, 540))))
        pdf_features.extend((feature(pair[0]), feature(pair[1])))
        del pair
        if page_number % 8 == 0:
            gc.collect()
    if len(files) < len(pdf_features) and not allow_missing:
        fail(f"Reference requires {len(pdf_features)} photographs, but only {len(files)} hires are available.")
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
    pdf_appearance = [appearance_signature(item) for item in pdf_features]
    hire_appearance = [appearance_signature(item) for item in hire_features]
    cost = np.asarray(
        [
            [
                matching_distance(pdf_item, hire_item, pdf_appearance[target], hire_appearance[source])
                for source, hire_item in enumerate(hire_features)
            ]
            for target, pdf_item in enumerate(pdf_features)
        ],
        dtype=np.float64,
    )
    if allow_missing:
        assignment_by_target, missing_looks, diagnostics, problems = _quick_missing_looks(
            cost, files, len(pdf_features),
        )
        restore_photo_recovery(project, reference, files, assignment_by_target, missing_looks, prior_rows)
        problems = [problem for problem in problems if not problem.startswith("LOOK_") or
                    int(problem[5:8]) in missing_looks]
    else:
        assignment = minimum_assignment(cost)
        diagnostics, problem_targets = _diagnose_assignment(
            list(range(len(pdf_features))), assignment, cost, files,
        )
        assignment_by_target = {target: source for target, source in enumerate(assignment)}
        missing_looks = set()
        problems = [
            f"PDF image {target + 1}: {files[assignment_by_target[target]].name} "
            f"(rank {diagnostics[target]['rank']}, score {float(diagnostics[target]['selected_score']):.4f}, "
            f"margin {float(diagnostics[target]['margin']):.4f})"
            for target in problem_targets
        ]

    placeholder_previews: dict[tuple[int, str], Image.Image] = {}
    missing_reference_dir = project / MISSING_REFERENCE_DIR
    missing_reference_dir.mkdir(parents=True, exist_ok=True)
    placeholder_paths: dict[tuple[int, str], Path] = {}
    for look_number in sorted(missing_looks):
        look_id = f"LOOK_{look_number:03}"
        for side, side_index in (("left", 0), ("right", 1)):
            reference_target = page_images[look_number - 1][side_index]
            reference_target.save(missing_reference_dir / f"{look_id}_{side.upper()}.jpg", "JPEG", quality=95, optimize=True)
            if (look_number - 1) * 2 + side_index in assignment_by_target:
                continue
            placeholder = hires / f"{PLACEHOLDER_PREFIX}{look_id}_{side.upper()}.jpg"
            placeholder_preview = _write_blank_placeholder(placeholder)
            placeholder_paths[(look_number, side)] = placeholder
            placeholder_previews[(look_number, side)] = placeholder_preview
    if missing_looks:
        missing_manifest = {
            "schema": 1,
            "mode": "photos" if photo_only else ("pending_review" if defer_review else "quick"),
            "looks": [f"LOOK_{look:03}" for look in sorted(missing_looks)],
            "reference_dir": str(MISSING_REFERENCE_DIR).replace("\\", "/"),
            "placeholders": {
                f"LOOK_{look:03}": {
                    side: (files[assignment_by_target[(look - 1) * 2 + offset]].name
                           if (look - 1) * 2 + offset in assignment_by_target else placeholder_paths[(look, side)].name)
                    for side, offset in (("left", 0), ("right", 1))
                }
                for look in sorted(missing_looks)
            },
        }
        (project / "control" / "work" / "quick-missing-photos.json").write_text(
            json.dumps(missing_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
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
        look_missing = index in missing_looks
        left_missing = (index - 1) * 2 not in assignment_by_target
        right_missing = (index - 1) * 2 + 1 not in assignment_by_target
        left_file = placeholder_paths[(index, "left")] if left_missing else files[assignment_by_target[(index - 1) * 2]]
        right_file = placeholder_paths[(index, "right")] if right_missing else files[assignment_by_target[(index - 1) * 2 + 1]]
        left_preview = placeholder_previews[(index, "left")] if left_missing else hire_previews[assignment_by_target[(index - 1) * 2]]
        right_preview = placeholder_previews[(index, "right")] if right_missing else hire_previews[assignment_by_target[(index - 1) * 2 + 1]]
        card = draw_card(
            left_target,
            right_target,
            left_preview,
            right_preview,
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
    used = {files[index].name for index in assignment_by_target.values()}
    payload = {
        "schema": "lookbook-reference-registry/v1",
        "reference": str(reference),
        "reference_pages": len(reader.pages),
        "cover_pages": cover_pages,
        "looks": len(rows),
        "hires_available": len(all_files),
        "hires_unique": len(files),
        "hires_duplicate_files": len(all_files) - len(files),
        "duplicate_groups": duplicate_groups,
        "hires_used": len(used),
        "unused_hires": [file.name for file in all_files if file.name not in used],
        "registry": str(registry),
        "contacts": contacts,
        "problems": problems,
        "quick_mode": bool(allow_missing and not photo_only and not defer_review),
        "photo_review_pending": bool(defer_review and missing_looks),
        "photo_only": bool(photo_only),
        "missing_looks": [f"LOOK_{look:03}" for look in sorted(missing_looks)],
        "missing_photo_reference_dir": str(MISSING_REFERENCE_DIR).replace("\\", "/") if missing_looks else "",
    }
    (evidence / "manifest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if problems and not allow_missing:
        fail("Automatic visual matching is not decisive; inspect registry-build proof cards and correct source inputs. " + "; ".join(problems[:4]))
    if prior_rows is not None and prior_rows != rows:
        fail(
            "Existing complete registry does not match the current PDF-reference proof. "
            "Do not overwrite it; inspect the registry-build proof cards and resolve the source change."
        )
    if not dry_run and prior_rows is None:
        write_registry(registry, rows)
    if allow_missing and registry.is_file() and not dry_run:
        write_photo_recovery_queue(project, reference, registry, files, hire_previews, page_images,
                                   cost, assignment_by_target, missing_looks)
    state = "existing registry re-verified" if prior_rows is not None else "registry written"
    suffix = ""
    if missing_looks:
        mode_label = "Режим только фотографий" if photo_only else ("Предварительный поиск фотографий" if defer_review else "Быстрая сборка")
        suffix_tail = "кредиты не сопоставляются." if photo_only else "кредиты будут сопоставлены по PDF-референсу."
        suffix = f" {mode_label}: пустые фото оставлены для " + ", ".join(f"LOOK_{look:03}" for look in sorted(missing_looks)) + f"; {suffix_tail}"
    print(f"PASS registry: {len(rows)} PDF-ordered looks, {len(used)} hires selected, {len(files) - len(used)} unused hires; {state}.{suffix}")
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
    target_appearance = [appearance_signature(item) for item in targets]
    hire_appearance = [appearance_signature(item) for item in hires]
    assignment = minimum_assignment(np.asarray([
        [matching_distance(target, hire, target_appearance[target_index], hire_appearance[hire_index])
         for hire_index, hire in enumerate(hires)]
        for target_index, target in enumerate(targets)
    ]))
    expected = list(reversed(range(len(images))))
    if assignment != expected:
        raise SystemExit(f"Self-test failed: {assignment} != {expected}")
    print("PASS build_reference_registry self-test")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a complete PDF-authoritative look-register.tsv from reference and hires.")
    parser.add_argument("project", nargs="?", type=Path)
    parser.add_argument("--reference", default="control/work/_mat/reference.pdf")
    parser.add_argument("--hires", default="_MAT/hires")
    parser.add_argument("--registry", default="control/work/look-register.tsv")
    parser.add_argument(
        "--cover-pages", type=lambda value: None if value.strip().lower() == "auto" else int(value),
        default=None,
        help="Optional leading cover pages. Default: auto-detect 0 or 1 from visible photo placements.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--defer-photo-review", action="store_true", help="Provisional photo search for a full build; unresolved slots still require visual recovery.")
    parser.add_argument(
        "--allow-missing",
        action="store_true",
        help="Non-review build modes: leave explicitly unresolved whole looks as blank fixed-frame placeholders.",
    )
    parser.add_argument(
        "--photo-only",
        action="store_true",
        help="Use photo-only wording and evidence for unresolved looks; credits are not part of this build.",
    )
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    if args.project is None:
        parser.error("project is required unless --self-test is used")
    project = args.project.resolve()
    build(
        project, path_from(project, args.reference), project_hires(project, args.hires),
        path_from(project, args.registry), args.cover_pages, args.dry_run, args.allow_missing, args.photo_only, args.defer_photo_review,
    )


if __name__ == "__main__":
    main()
