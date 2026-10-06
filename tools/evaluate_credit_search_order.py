"""Read-only, no-AI evaluation of search order against supplied known matches.

Expected matches are test inputs, never application hints. Sources stay read
only; reports and ranked proof boards are written outside the project.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "automation-engine/lookbook-layout/scripts"))
from build_targeted_rematch_proofs import MAP_FIELDS, INDEX_FIELDS, read_rows, rank_candidates, render_candidate_sheet


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    parser.add_argument("--expected", required=True, help="Verified test cases: LOOK_001=W:3,LOOK_002=M:4")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root, output = args.project.resolve(), args.output.resolve()
    if output == root or root in output.parents or output.exists():
        raise SystemExit("Use a fresh evaluation directory outside the project.")
    mapping = root / "control/work/caption-map.tsv"
    before = hashlib.sha256(mapping.read_bytes()).hexdigest()
    rows = {row["look_id"]: row for row in read_rows(mapping, MAP_FIELDS)}
    cards = read_rows(root / "control/work/_mat/excel-images/index.tsv", INDEX_FIELDS)
    # Match the production builder's one-card-per-logical-credit policy.
    unique = {}
    for card in cards:
        unique.setdefault((card["excel_sheet"], card["excel_look_number"]), card)
    cards = [unique[pair] for pair in sorted(unique, key=lambda p: (p[0], int(p[1])))]
    cache, results = {}, []
    output.mkdir(parents=True)
    for case in args.expected.split(","):
        look, expected = case.split("=")
        ranked = rank_candidates(root, rows[look], cards, root / "_MAT/hires",
                                 root / "control/work/missing-photo-reference", cache)
        labels = [f"{card['excel_sheet']}:{card['excel_look_number']}" for card in ranked]
        assert len(labels) == len(set(labels)) == len(cards)
        assert expected in labels
        result = {"look": look, "expected": expected, "rank": labels.index(expected) + 1,
                  "catalogue_size": len(cards), "top_four": labels[:4]}
        render_candidate_sheet(root, ranked[:4], output / f"{look}-first-page.jpg", 1)
        results.append(result)
    assert before == hashlib.sha256(mapping.read_bytes()).hexdigest()
    report = {"results": results, "project_map_unchanged": True, "ai_requests": 0,
              "expected_matches_are_evaluation_inputs_only": True}
    (output / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
