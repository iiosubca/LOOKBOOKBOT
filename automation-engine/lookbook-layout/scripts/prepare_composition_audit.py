#!/usr/bin/env python3
"""Create the native composition *plan* used before lookbook visual proof.

The TSV is deliberately only a correction plan. It cannot confirm that credits
are clear: the native worker later records applied graphic bounds and the
controller requires a fresh rendered InDesign proof plus one confirmation per
look pair.
"""
from __future__ import annotations

import argparse
import csv
import os
from pathlib import Path


REGISTRY_FIELDS = [
    "look_id", "spread_order", "pdf_spread", "left_filename", "right_filename",
    "indd_left_page", "indd_right_page",
]
PLAN_FIELDS = [
    "look_id", "orientation", "left_image_filename", "right_image_filename",
    "photo_adjustment_points", "plan_status",
]


def fail(message: str) -> None:
    raise SystemExit("ERROR: " + message)


def inside_project(project: Path, candidate: Path) -> Path:
    resolved = candidate.resolve()
    try:
        resolved.relative_to(project)
    except ValueError:
        fail(f"Path must stay inside project: {resolved}")
    return resolved


def read_registry(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8-sig") as source:
            reader = csv.DictReader(source, delimiter="\t")
            if reader.fieldnames != REGISTRY_FIELDS:
                fail("look-register.tsv has unexpected columns.")
            rows = [{key: (value or "").strip() for key, value in row.items()} for row in reader]
    except FileNotFoundError:
        fail(f"Registry not found: {path}")
    if not rows:
        fail("Registry has no looks.")
    for position, row in enumerate(rows, start=1):
        expected = f"LOOK_{position:03}"
        if row["look_id"] != expected:
            fail(f"Registry row {position + 1} must be {expected}, not {row['look_id'] or '(blank)'}.")
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a blank all-look composition correction plan for a controlled lookbook.")
    parser.add_argument("project", help="controlled project folder")
    # The controlled work-area is the single canonical location for the
    # registry.  Keeping this default aligned with `lookbook-state.json`
    # avoids a false "Registry not found" block after project hygiene moves
    # temporary inputs out of the delivery root.
    parser.add_argument("--registry", default="control/work/look-register.tsv")
    parser.add_argument("--output", default="control/visual/composition-plan.tsv")
    parser.add_argument("--overwrite", action="store_true", help="replace an existing plan only when starting a fresh visual cycle")
    args = parser.parse_args()

    project = Path(args.project).expanduser().resolve()
    if not project.is_dir():
        fail(f"Project folder not found: {project}")
    registry = inside_project(project, project / args.registry)
    output = inside_project(project, project / args.output)
    if output.exists() and not args.overwrite:
        fail(f"Composition plan already exists: {output}. Continue it, or use --overwrite only for a new visual cycle.")

    looks = read_registry(registry)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=PLAN_FIELDS, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for row in looks:
            writer.writerow({
                "look_id": row["look_id"],
                "orientation": "FULL_LEFT_CLOSE_RIGHT",
                "left_image_filename": row["left_filename"],
                "right_image_filename": row["right_filename"],
                "photo_adjustment_points": "0",
                "plan_status": "READY",
            })
    os.replace(temporary, output)
    print(f"COMPOSITION PLAN CREATED: {output}")
    print(f"Review all {len(looks)} spreads and set the required fixed-container sources plus signed horizontal shift:")
    print("  orientation = FULL_LEFT_CLOSE_RIGHT")
    print("  left_image_filename / right_image_filename = intended sources in the fixed left and right frames")
    print("  photo_adjustment_points = signed move of the left placed graphic, not its frame")
    print("  plan_status = READY")
    print("Then use apply-composition, render-visual-proof, and confirm-visual-look for every rendered pair. Do not write CLEAR or CONFIRMED into this plan; those values are deliberately no longer accepted.")


if __name__ == "__main__":
    main()
