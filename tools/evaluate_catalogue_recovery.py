"""Bounded end-to-end credit-map regression on copies; never opens InDesign."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path
from datetime import date

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from lookbookbot.pipeline import PipelineEngine, _read_tsv, _write_tsv
from lookbookbot.domain import BuildMode, ProviderKind
from lookbookbot.providers import CodexProvider
from lookbookbot.state import StateStore


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="gpt-6-luna")
    parser.add_argument("--effort", default="low")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--case", choices=("ownership", "accessories"), default="ownership")
    args = parser.parse_args()
    source = args.project.resolve()
    root = args.output.resolve()
    if root == source or source in root.parents:
        raise SystemExit("Use an isolated evaluation directory outside the production project.")
    if root.exists():
        raise SystemExit("Use a fresh directory so a previous result cannot mask this regression.")
    mapping = source / "control/work/caption-map.tsv"
    prior_hash = hashlib.sha256(mapping.read_bytes()).hexdigest()
    source_ids = ("LOOK_003", "LOOK_005") if args.case == "ownership" else ("LOOK_048", "LOOK_050")
    rows = [row for row in _read_tsv(mapping) if row["look_id"] in set(source_ids)]
    if len(rows) != 2:
        raise SystemExit(f"This regression requires {source_ids}.")
    (root / "control/work/_mat/excel-images").mkdir(parents=True)
    (root / "_MAT/hires").mkdir(parents=True)
    index = _read_tsv(source / "control/work/_mat/excel-images/index.tsv")
    for card in index:
        target = root / card["excel_image"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / card["excel_image"], target)
    _write_tsv(root / "control/work/_mat/excel-images/index.tsv", index)
    for row in rows:
        for side in ("left", "right"):
            name = row[f"{side}_filename"]
            shutil.copy2(source / "_MAT/hires" / name, root / "_MAT/hires" / name)
            if name.startswith("__lbb_missing_"):
                relative = Path("control/work/missing-photo-reference") / f"{row['look_id']}_{side.upper()}.jpg"
                (root / relative).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source / relative, root / relative)
    # Initial wrong assignments are reproduced through the controller below.
    _write_tsv(root / "control/work/caption-map.tsv", rows)
    registry = [row for row in _read_tsv(source / "control/work/look-register.tsv") if row["look_id"] in set(source_ids)]
    # The official controller requires consecutive IDs even in a two-look
    # disposable fixture. Photo inputs remain the exact source pairs.
    for number, (row, registered) in enumerate(zip(rows, registry), 1):
        old_id, new_id = row["look_id"], f"LOOK_{number:03}"
        for side in ("LEFT", "RIGHT"):
            old = root / "control/work/missing-photo-reference" / f"{old_id}_{side}.jpg"
            if old.is_file():
                shutil.copy2(old, old.with_name(f"{new_id}_{side}.jpg"))
        row.update(look_id=new_id, evidence_file=f"control/work/mapping-evidence/{new_id}.jpg")
        registered.update(look_id=new_id, spread_order=str(number), pdf_spread=str(number),
                          indd_left_page=str(2 * number), indd_right_page=str(2 * number + 1))
    _write_tsv(root / "control/work/caption-map.tsv", rows)
    _write_tsv(root / "control/work/look-register.tsv", registry)
    if not args.live:
        print("Prepared isolated reproduction.")
        return
    store = StateStore(root / "test-state.db")
    project = store.save_project(name="isolated-regression", source_dir=source, output_root=root.parent,
                                 project_dir=root, show_date=date(2026, 10, 7), provider=ProviderKind.CODEX,
                                 model=args.model, build_mode=BuildMode.QUICK, reasoning_effort=args.effort)
    engine = PipelineEngine(store, log=lambda line: print(line, flush=True))
    # Produce the initial deliberately bad assignment through the official
    # alternative selection API. A hand-written map would fail provenance
    # validation before reaching the candidate-pool regression.
    engine.controller.script("auto_caption_map.py", root, "--mode", "seed", "--map", "control/work/fixture-initial-map.tsv")
    seeded = _read_tsv(root / "control/work/fixture-initial-map.tsv")
    _write_tsv(root / "control/work/caption-map.tsv", seeded)
    initial_assignments = ("LOOK_001=M:18,LOOK_002=W:22" if args.case == "ownership"
                           else "LOOK_001=W:31,LOOK_002=M:13")
    engine.controller.script("auto_caption_map.py", root, "--mode", "alternative-proofs", "--controller-batch",
                             "--looks", "LOOK_001,LOOK_002", "--assignments", initial_assignments)
    engine.controller.script("auto_caption_map.py", root, "--mode", "select-alternatives", "--controller-batch",
                             "--assignments", initial_assignments)
    rows = _read_tsv(root / "control/work/caption-map.tsv")
    if args.case == "ownership":
        rows[1]["visual_status"] = "CONFIRMED"  # intentionally wrong test fixture, never a production proof
    _write_tsv(root / "control/work/caption-map.tsv", rows)
    manifest = root / "control/work/ui-overrides/targeted-credit-rematch.json"
    targets = ["LOOK_001"] if args.case == "ownership" else ["LOOK_001", "LOOK_002"]
    engine._initialize_rematch_manifest(manifest, rows, {row["look_id"]: row for row in rows}, targets, {})
    if args.case == "accessories":
        saved = engine._load_rematch_manifest(manifest)
        saved.update(search_algorithm="full-catalogue-v1", attempted_candidates={"LOOK_001": ["W:10"], "LOOK_002": ["M:1"]})
        manifest.write_text(json.dumps(saved), encoding="utf-8")
    provider = CodexProvider(args.model, reasoning_effort=args.effort)
    try:
        if args.case == "accessories":
            # Real negative controls: these are wrong clothes, not optional
            # sunglasses or a differently held version of the same bag.
            negative_rows = engine._alternative_evidence_rows(root, {"LOOK_001": ("W", "31"), "LOOK_002": ("M", "13")})
            negative = engine._inspect_codex_credit_evidence_parallel(provider, root, negative_rows)
            assert all(not value.accepted for value in negative.values()), negative
        engine._resolve_codex_targeted_credit_rematch(project, provider, targets, manifest)
        after = {row["look_id"]: row for row in _read_tsv(root / "control/work/caption-map.tsv")}
        expected = ({"LOOK_001": ("W", "22"), "LOOK_002": ("W", "20")} if args.case == "ownership"
                    else {"LOOK_001": ("W", "10"), "LOOK_002": ("M", "1")})
        for look, pair in expected.items():
            row = after[look]
            assert (row["excel_sheet"], row["excel_look_number"]) == pair, (look, row)
            assert row["visual_status"] == "CONFIRMED", row
        assert hashlib.sha256(mapping.read_bytes()).hexdigest() == prior_hash
        result = {"status": "passed", "model": args.model, "effort": args.effort, "expected": expected,
                  "source_looks": dict(zip(("LOOK_001", "LOOK_002"), source_ids)), "case": args.case,
                  "map": after, "expanded_targets": targets, "original_project_unchanged": True}
        (root / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"PASS: {args.case} resolved {expected} through actual controller proofs, atomic selection and confirmations.")
    finally:
        provider.close()


if __name__ == "__main__":
    main()
