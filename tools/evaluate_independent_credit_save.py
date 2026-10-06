"""Exercise actual controller saves on isolated copies with replayed decisions.

No live AI and no InDesign. Expected assignments are human-checked test inputs,
not application rules. Deliberately leave one fixture look unresolved and
verify that the others are selected/confirmed by real controller commands.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from lookbookbot.pipeline import PipelineEngine, ReviewRequired, _read_tsv, _write_tsv
from lookbookbot.providers import VisionDecision
from lookbookbot.domain import ProviderKind, BuildMode
from lookbookbot.state import StateStore


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    parser.add_argument("--expected", required=True)
    parser.add_argument("--unresolved", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source, root = args.project.resolve(), args.output.resolve()
    if root.exists() or root == source or source in root.parents:
        raise SystemExit("Use a fresh isolated directory outside the source project.")
    mapping = source / "control/work/caption-map.tsv"
    before = hashlib.sha256(mapping.read_bytes()).hexdigest()
    cases = [case.split("=") for case in args.expected.split(",")]
    source_ids = [look for look, _ in cases] + [args.unresolved]
    registry = {row["look_id"]: row for row in _read_tsv(source / "control/work/look-register.tsv")}
    cards = _read_tsv(source / "control/work/_mat/excel-images/index.tsv")

    def link(relative):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(source / relative, target)
        except OSError:
            shutil.copy2(source / relative, target)

    for card in cards:
        link(Path(card["excel_image"]))
    _write_tsv(root / "control/work/_mat/excel-images/index.tsv", cards)
    rows = []
    for number, look in enumerate(source_ids, 1):
        row = registry[look].copy()
        for side in ("left", "right"):
            link(Path("_MAT/hires") / row[f"{side}_filename"])
        row.update(look_id=f"LOOK_{number:03}", spread_order=str(number), pdf_spread=str(number),
                   indd_left_page=str(number * 2), indd_right_page=str(number * 2 + 1))
        rows.append(row)
    _write_tsv(root / "control/work/look-register.tsv", rows)
    store = StateStore(root / "test.db")
    project = store.save_project(name="partial-save-fixture", source_dir=source, output_root=root.parent,
                                 project_dir=root, show_date=date(2026, 10, 19), provider=ProviderKind.CODEX,
                                 model="replay-only", build_mode=BuildMode.QUICK)
    engine = PipelineEngine(store, log=print)
    engine.controller.script("auto_caption_map.py", root, "--mode", "seed")
    expected = {f"LOOK_{n:03}": tuple(pair.split(":")) for n, (_, pair) in enumerate(cases, 1)}
    # Install distinct deliberately incorrect cards through the real API.
    used = set(expected.values())
    wrong = []
    for card in cards:
        pair = (card["excel_sheet"], card["excel_look_number"])
        if pair not in used:
            wrong.append(pair)
            used.add(pair)
        if len(wrong) == len(rows):
            break
    assignments = ",".join(f"{row['look_id']}={s}:{n}" for row, (s, n) in zip(rows, wrong))
    targets = [row["look_id"] for row in rows]
    engine.controller.script("auto_caption_map.py", root, "--mode", "alternative-proofs", "--controller-batch",
                             "--looks", ",".join(targets), "--assignments", assignments)
    engine.controller.script("auto_caption_map.py", root, "--mode", "select-alternatives", "--controller-batch",
                             "--assignments", assignments)
    actual = _read_tsv(root / "control/work/caption-map.tsv")
    manifest = root / "control/work/ui-overrides/targeted-credit-rematch.json"
    manifest.parent.mkdir(parents=True)
    engine._initialize_rematch_manifest(manifest, actual, {row["look_id"]: row for row in actual}, targets, {})

    def choose(*_):
        error = ReviewRequired("Deliberately unresolved fixture look")
        error.partial_choices = expected.copy()
        raise error

    def inspect(_provider, _root, evidence):
        return {row["look_id"]: VisionDecision(True, "garment=distinctive coat and matching material; trousers=matching colour and cut; shoes=matching construction")
                for row in evidence}

    engine._choose_codex_rematch_candidates = choose
    engine._inspect_codex_credit_evidence_parallel = inspect
    try:
        engine._choose_and_verify_codex_rematch_candidates(object(), root, targets, manifest)
    except ReviewRequired:
        pass
    else:
        raise AssertionError("The intentionally unresolved look cannot be reported as complete.")
    after = _read_tsv(root / "control/work/caption-map.tsv")
    for row in after[:-1]:
        assert (row["excel_sheet"], row["excel_look_number"]) == expected[row["look_id"]], row
        assert row["visual_status"] == "CONFIRMED", row
    assert after[-1] == actual[-1]
    assert len({(row["excel_sheet"], row["excel_look_number"]) for row in after}) == len(after)
    assert hashlib.sha256(mapping.read_bytes()).hexdigest() == before
    print(f"PASS actual controller: {len(expected)} replay-verified replacements saved, unresolved row unchanged; no live AI, no source changes.")


if __name__ == "__main__":
    main()
