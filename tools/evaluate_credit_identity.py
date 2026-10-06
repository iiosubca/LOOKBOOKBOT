"""Bounded live comparison of copied proof cards, without touching a lookbook.

Expected accept/reject values are test inputs, never production bindings.
Every request goes through the same outfit policy, schema, cache and rejection
audit as the installed application. No controller writes and no InDesign.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from lookbookbot.pipeline import PipelineEngine
from lookbookbot.providers import CodexProvider, ProviderLimitError
from lookbookbot.state import StateStore


class BoundedVision(CodexProvider):
    def __init__(self, model, effort, maximum):
        super().__init__(model, reasoning_effort=effort)
        self.maximum = maximum
        self.calls = 0

    def run_readonly_decision_vision(self, *args, **kwargs):
        if self.calls >= self.maximum:
            raise ProviderLimitError("Evaluation request budget exhausted; no automatic retry.")
        self.calls += 1
        return super().run_readonly_decision_vision(*args, **kwargs)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--accept", type=Path, action="append", default=[])
    parser.add_argument("--reject", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="gpt-6-luna")
    parser.add_argument("--effort", default="high")
    parser.add_argument("--max-requests", type=int, default=10)
    args = parser.parse_args()
    root = args.output.resolve()
    sources = [(path.resolve(), expected) for paths, expected in ((args.accept, True), (args.reject, False)) for path in paths]
    if root.exists() or not sources or any(root == path.parent or root in path.parents for path, _ in sources):
        raise SystemExit("Use a fresh isolated evaluation directory and explicit proof cases.")
    before = {str(path): digest(path) for path, _ in sources}
    root.mkdir(parents=True)
    engine = PipelineEngine(StateStore(root / "test.db"), log=lambda message: print(message, flush=True))
    provider = BoundedVision(args.model, args.effort, args.max_requests)
    results = []
    started = time.monotonic()
    try:
        for number, (source, expected) in enumerate(sources, 1):
            look = f"LOOK_{number:03}"
            card = root / "cards" / f"{look}.jpg"
            card.parent.mkdir(exist_ok=True)
            shutil.copy2(source, card)
            try:
                result = engine._inspect_codex_credit_batch(provider, root, [{"look_id": look, "evidence_file": str(card.relative_to(root))}])[look]
                record = {"source": str(source), "expected": expected, "accepted": result.accepted,
                          "passed": result.accepted == expected, "note": result.note}
            except Exception as error:
                record = {"source": str(source), "expected": expected, "passed": False, "error": str(error)}
                results.append(record)
                print(json.dumps(record, ensure_ascii=False), flush=True)
                if isinstance(error, ProviderLimitError):
                    break
                continue
            results.append(record)
            print(json.dumps(record, ensure_ascii=False), flush=True)
    finally:
        provider.close()
    unchanged = all(digest(Path(path)) == sha for path, sha in before.items())
    summary = {"model": args.model, "effort": args.effort, "requests": provider.calls,
               "elapsed_seconds": round(time.monotonic() - started, 2), "source_cards_unchanged": unchanged,
               "passed": sum(case["passed"] for case in results), "total": len(sources), "cases": results}
    (root / "result.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"EVALUATION {summary['passed']}/{summary['total']}; requests={provider.calls}; source unchanged={unchanged}", flush=True)
    if not unchanged or summary["passed"] != len(sources):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
