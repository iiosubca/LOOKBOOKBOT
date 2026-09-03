#!/usr/bin/env python3
"""Prepare a disposable 1- or 5-look controlled source set from the live inputs.

This script deliberately does not assign photo pairs itself.  It creates a
short PDF reference, lets build_reference_registry.py select the corresponding
hires from the complete source folder, copies only those selected hires, and
then runs the same registry builder again against the reduced source set.
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import sys
from pathlib import Path

from pypdf import PdfReader, PdfWriter


SCRIPT_DIR = Path(__file__).resolve().parent
WORK_AREA = SCRIPT_DIR / "create_lookbook_work_area.py"
REGISTRY_BUILDER = SCRIPT_DIR / "build_reference_registry.py"
sys.path.insert(0, str(SCRIPT_DIR))
from build_reference_registry import detect_cover_pages
FIELDS = ("look_id", "spread_order", "pdf_spread", "left_filename", "right_filename", "indd_left_page", "indd_right_page")


def fail(message: str) -> None:
    raise SystemExit(message)


def run(command: list[str]) -> None:
    completed = subprocess.run(command, text=True)
    if completed.returncode:
        fail(f"Fixture setup command failed ({completed.returncode}): {' '.join(command)}")


def read_registry(path: Path, look_count: int) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source, delimiter="\t")
        if tuple(reader.fieldnames or ()) != FIELDS:
            fail(f"Fixture candidate registry has an unexpected schema: {path}")
        rows = [{key: (row.get(key) or "").strip() for key in FIELDS} for row in reader]
    if len(rows) != look_count:
        fail(f"Fixture candidate registry has {len(rows)} looks, expected {look_count}.")
    if any(not row["left_filename"] or not row["right_filename"] for row in rows):
        fail("Fixture candidate registry contains a blank filename.")
    return rows


def write_short_reference(source: Path, destination: Path, looks: int) -> int:
    reader = PdfReader(str(source))
    cover_pages = detect_cover_pages(reader)
    if len(reader.pages) < looks + cover_pages:
        fail(f"Reference PDF has {len(reader.pages)} pages; need {cover_pages} cover page(s) plus {looks} look pages.")
    writer = PdfWriter()
    for page_number in range(cover_pages + looks):
        writer.add_page(reader.pages[page_number])
    with destination.open("wb") as target:
        writer.write(target)
    return cover_pages


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create a disposable PDF-authoritative 1- or 5-look test fixture for LOOKBOOKBOT."
    )
    parser.add_argument("project", type=Path, help="New dated project folder containing only its copied master INDD")
    parser.add_argument("--looks", type=int, choices=(1, 5), required=True)
    parser.add_argument("--reference", type=Path, required=True, help="Full authoritative source PDF")
    parser.add_argument("--hires", type=Path, required=True, help="Full immediate-JPG source folder")
    parser.add_argument("--workbook", type=Path, required=True, help="Full Excel credit catalogue")
    args = parser.parse_args()

    project = args.project.expanduser().resolve()
    reference = args.reference.expanduser().resolve()
    hires = args.hires.expanduser().resolve()
    workbook = args.workbook.expanduser().resolve()
    if not project.is_dir():
        fail(f"Fixture project folder does not exist: {project}")
    if (project / "control").exists():
        fail(f"Fixture project already has a control area and cannot be reused: {project}")
    if not reference.is_file() or not hires.is_dir() or not workbook.is_file():
        fail("Fixture sources must be an existing reference PDF, hires folder, and Excel workbook.")
    if not any(project.glob("*.indd")):
        fail("Fixture project must contain its copied master INDD before preparation.")

    run([sys.executable, str(WORK_AREA), str(project)])
    work = project / "control" / "work"
    target_reference = work / "_mat" / "reference.pdf"
    target_hires = work / "_mat" / "hires"
    candidates = work / "scratch" / "fixture-candidates"
    candidates.mkdir(parents=True, exist_ok=False)
    write_short_reference(reference, target_reference, args.looks)
    for source in sorted(hires.iterdir(), key=lambda item: item.name.casefold()):
        if source.is_file() and source.suffix.casefold() in {".jpg", ".jpeg"}:
            shutil.copy2(source, candidates / source.name)
    if not any(candidates.iterdir()):
        fail("Fixture source hires folder has no JPG files.")
    shutil.copy2(workbook, work / "_mat" / "caption-source.xlsx")

    candidate_registry = work / "scratch" / "fixture-candidate-register.tsv"
    run([
        sys.executable, str(REGISTRY_BUILDER), str(project),
        "--reference", "control/work/_mat/reference.pdf",
        "--hires", "control/work/scratch/fixture-candidates",
        "--registry", "control/work/scratch/fixture-candidate-register.tsv",
    ])
    rows = read_registry(candidate_registry, args.looks)
    selected = sorted({row["left_filename"] for row in rows} | {row["right_filename"] for row in rows})
    if len(selected) != args.looks * 2:
        fail("Fixture registry did not select exactly two distinct hires per look.")
    for filename in selected:
        source = candidates / filename
        if not source.is_file():
            fail(f"Fixture selection is missing its candidate JPG: {filename}")
        shutil.copy2(source, target_hires / filename)

    # The second pass is the production registry used by the controller.  It
    # verifies that the reduced reference and exact hires form a complete pair set.
    run([sys.executable, str(REGISTRY_BUILDER), str(project)])
    payload = {
        "schema": "lookbook-test-fixture/v1",
        "looks": args.looks,
        "source_reference": str(reference),
        "source_hires": str(hires),
        "source_workbook": str(workbook),
        "selected_hires": selected,
        "registry": "control/work/look-register.tsv",
    }
    (work / "fixture-manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"PASS fixture: {args.looks} PDF-ordered looks with {len(selected)} verified hires in {project}")


if __name__ == "__main__":
    main()
