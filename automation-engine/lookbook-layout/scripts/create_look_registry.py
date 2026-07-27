"""Create and validate the authoritative lookbook registry."""

from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path

FIELDS = [
    "look_id",
    "spread_order",
    "pdf_spread",
    "left_filename",
    "right_filename",
    "indd_left_page",
    "indd_right_page",
]
ID = re.compile(r"^LOOK_\d{3}$")


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source, delimiter="\t")
        if reader.fieldnames != FIELDS:
            raise ValueError("Registry columns do not match the required schema.")
        return list(reader)


def validate(rows: list[dict[str, str]], ready: bool) -> None:
    seen: set[str] = set()
    seen_pages: set[str] = set()
    seen_files: set[tuple[str, str]] = set()
    for number, row in enumerate(rows, start=2):
        look_id = row["look_id"].strip()
        if not ID.fullmatch(look_id) or look_id in seen:
            raise ValueError(f"Row {number}: look_id must be unique LOOK_###.")
        seen.add(look_id)
        if ready:
            missing = [name for name, value in row.items() if not value.strip()]
            if missing:
                raise ValueError(f"Row {number} ({look_id}): missing {', '.join(missing)}.")
            if not row["spread_order"].isdigit() or int(row["spread_order"]) != number - 1:
                raise ValueError(f"Row {number} ({look_id}): spread_order must be consecutive.")
            if not row["pdf_spread"].isdigit() or int(row["pdf_spread"]) < 1:
                raise ValueError(f"Row {number} ({look_id}): PDF spread must be positive.")
            if row["left_filename"] == row["right_filename"]:
                raise ValueError(f"Row {number} ({look_id}): left and right files must differ.")
            pages = (row["indd_left_page"], row["indd_right_page"])
            if pages[0] == pages[1] or any(page in seen_pages for page in pages):
                raise ValueError(f"Row {number} ({look_id}): InDesign pages must be unique.")
            seen_pages.update(pages)
            files = (row["left_filename"], row["right_filename"])
            if files in seen_files:
                raise ValueError(f"Row {number} ({look_id}): duplicate image pair.")
            seen_files.add(files)
    if not rows:
        raise ValueError("Registry has no looks.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("registry", type=Path)
    parser.add_argument("--count", type=int, help="Create a blank registry with this many look IDs.")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--validate-ready", action="store_true")
    args = parser.parse_args()

    if args.count is not None:
        if args.count < 1:
            raise SystemExit("--count must be at least 1.")
        if "control/work" in args.registry.as_posix().lower():
            raise SystemExit(
                "--count creates an incomplete fixture stub and is forbidden for a live control/work registry. "
                "Run build_reference_registry.py <project> first."
            )
        if args.registry.exists() and not args.overwrite:
            raise SystemExit("Registry already exists; use --overwrite only for an intentional replacement.")
        args.registry.parent.mkdir(parents=True, exist_ok=True)
        with args.registry.open("w", newline="", encoding="utf-8") as target:
            writer = csv.DictWriter(target, fieldnames=FIELDS, delimiter="\t")
            writer.writeheader()
            for index in range(1, args.count + 1):
                writer.writerow({"look_id": f"LOOK_{index:03}", "spread_order": str(index)})
        print(f"Created {args.registry} with {args.count} look IDs.")
        return

    rows = read_rows(args.registry)
    validate(rows, ready=args.validate_ready)
    state = "ready for binding/import" if args.validate_ready else "schema-valid"
    print(f"{args.registry}: {len(rows)} looks, {state}.")


if __name__ == "__main__":
    main()
