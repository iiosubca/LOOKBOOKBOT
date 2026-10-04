"""Run the real photo-registry builder on isolated 1/5/full source fixtures.

No AI calls, InDesign access, workbook changes or production-project recovery.
Source images are read-only hardlinks in the fixture (copy fallback), never
modified by the registry builder. Proofs and placeholders stay in the fixture.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "automation-engine/lookbook-layout/scripts"))
from build_reference_registry import build, detect_cover_pages
from create_lookbook_test_fixture import write_short_reference
from pypdf import PdfReader


def hashes(paths):
    result = {}
    for path in paths:
        with path.open("rb") as stream:
            result[path.name] = hashlib.file_digest(stream, "sha256").hexdigest()
    return result


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--hires", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.output.resolve()
    source = args.hires.resolve()
    if root.exists() or root == source or source in root.parents or root in source.parents:
        raise SystemExit("Use a new isolated directory, outside the source directory.")
    files = sorted(path for path in source.iterdir() if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg"}
                   and not path.name.startswith("__lbb_missing_"))
    before = hashes(files)
    total = len(PdfReader(args.reference).pages) - detect_cover_pages(PdfReader(args.reference))
    results = []
    for count in dict.fromkeys([1, 5, total]):
        project = root / f"looks-{count:03}"
        material = project / "control/work/_mat"
        hires = project / "_MAT/hires"
        material.mkdir(parents=True)
        hires.mkdir(parents=True)
        reference = material / "reference.pdf"
        if count == total:
            shutil.copy2(args.reference, reference)
        else:
            write_short_reference(args.reference, reference, count)
        for file in files:
            try:
                os.link(file, hires / file.name)
            except OSError:
                shutil.copy2(file, hires / file.name)
        registry = project / "control/work/look-register.tsv"
        build(project, reference, hires, registry, None, False, allow_missing=True)
        manifest_path = sorted((project / "control/work/registry-build").glob("*/manifest.json"))[-1]
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        with registry.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream, delimiter="\t"))
        assert len(rows) == count
        assert manifest["hires_duplicate_files"] == len(files) - len(set(before.values()))
        used = [row[field] for row in rows for field in ("left_filename", "right_filename")
                if not row[field].startswith("__lbb_missing_")]
        assert len({before[name] for name in used}) == len(used)
        assert all(before[name] == hashes([hires / name])[name] for name in used)
        record = {"looks": count, "source_files": manifest["hires_available"],
                  "unique_photos": manifest["hires_unique"], "exact_copies": manifest["hires_duplicate_files"],
                  "placed_photo_candidates": manifest["hires_used"], "missing_looks": manifest["missing_looks"],
                  "registry": str(registry)}
        results.append(record)
        print(json.dumps(record, ensure_ascii=False), flush=True)
    assert before == hashes(files), "Source photograph bytes changed"
    (root / "result.json").write_text(json.dumps({"source_photos_unchanged": True, "cases": results}, indent=2), encoding="utf-8")
    print("PASS: 1/5/full registry fixtures completed; original photos unchanged.")


if __name__ == "__main__":
    main()
