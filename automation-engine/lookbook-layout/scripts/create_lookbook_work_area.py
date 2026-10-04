#!/usr/bin/env python3
"""Create the controlled non-delivery area of a lookbook project."""
from __future__ import annotations

import argparse
from pathlib import Path


WORK_FOLDERS = (
    "_mat/excel-images",
    "mapping-review/required-pdf-looks",
    "mapping-review/excel",
    "mapping-evidence",
    "previews",
    "reference",
    "scratch",
)
ROOT_DIRECTORIES = {"control", "control-history", "Gender", "_MAT"}
ROOT_FILE_SUFFIXES = {".indd", ".pdf", ".idlk"}
ROOT_FILE_NAMES = {"desktop.ini", "Thumbs.db", ".DS_Store"}


def unexpected_root_items(project: Path) -> list[Path]:
    result: list[Path] = []
    for item in project.iterdir():
        if item.is_dir() and item.name.casefold() in {name.casefold() for name in ROOT_DIRECTORIES}:
            continue
        if item.is_file() and (item.suffix.lower() in ROOT_FILE_SUFFIXES or item.name in ROOT_FILE_NAMES):
            continue
        result.append(item)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare a clean controlled work area for one lookbook release.")
    parser.add_argument("project", type=Path)
    args = parser.parse_args()
    project = args.project.expanduser().resolve()
    if not project.is_dir():
        raise SystemExit(f"Project folder does not exist: {project}")
    unexpected = unexpected_root_items(project)
    if unexpected:
        names = ", ".join(item.name for item in sorted(unexpected, key=lambda item: item.name.lower()))
        raise SystemExit(
            "Project root is not clean. Keep only INDD, deliverable PDFs, Gender, _MAT, and control here; "
            f"move these items before starting: {names}"
        )
    work = project / "control" / "work"
    (project / "_MAT" / "hires").mkdir(parents=True, exist_ok=True)
    for relative in WORK_FOLDERS:
        (work / relative).mkdir(parents=True, exist_ok=True)
    print(f"WORK AREA READY: {work}")
    print("Store every source copy, registry, map, contact sheet, proof, temporary script, and scratch file under this folder.")


if __name__ == "__main__":
    main()
