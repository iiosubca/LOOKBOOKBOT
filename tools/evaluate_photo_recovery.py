"""Run the actual bounded photo recovery on an isolated registry copy.

Never opens or edits INDD, never copies PDF photographs into hi-res slots.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from lookbookbot.pipeline import PipelineEngine, _read_tsv
from lookbookbot.photo_recovery import recover_photos
from lookbookbot.providers import CodexProvider, ProviderLimitError
from lookbookbot.state import StateStore


class Bounded(CodexProvider):
    def __init__(self, model, effort, maximum):
        super().__init__(model, reasoning_effort=effort)
        self.calls = 0
        self.maximum = maximum
    def run_readonly_photo_pair_vision(self, *args, **kwargs):
        if self.calls >= self.maximum:
            raise ProviderLimitError("Evaluation request budget reached; no further requests.")
        self.calls += 1
        return super().run_readonly_photo_pair_vision(*args, **kwargs)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="gpt-6-luna")
    parser.add_argument("--effort", default="high")
    parser.add_argument("--max-requests", type=int, default=12)
    parser.add_argument("--expect-complete", action="store_true")
    args = parser.parse_args()
    source, root = args.project.resolve(), args.output.resolve()
    if root.exists() or root in source.parents or source in root.parents:
        raise SystemExit("Use a fresh, separate evaluation directory.")
    mapping = source / "control/work/look-register.tsv"
    before = hashlib.sha256(mapping.read_bytes()).hexdigest()
    shutil.copytree(source / "control", root / "control")
    target_hires = root / "_MAT/hires"
    target_hires.mkdir(parents=True)
    for image in (source / "_MAT/hires").iterdir():
        if image.is_file():
            try:
                os.link(image, target_hires / image.name)
            except OSError:
                shutil.copy2(image, target_hires / image.name)
    engine = PipelineEngine(StateStore(root / "control/work/test.db"), log=print)
    provider = Bounded(args.model, args.effort, args.max_requests)
    error = None
    try:
        unresolved = recover_photos(provider, root, engine.controller, print)
    except Exception as failure:
        error = str(failure)
        unresolved = [row["look_id"] for row in _read_tsv(root / "control/work/look-register.tsv")
                      if any(row[key].startswith("__lbb_missing_") for key in ("left_filename", "right_filename"))]
    finally:
        provider.close()
    unchanged = hashlib.sha256(mapping.read_bytes()).hexdigest() == before
    summary = {"model": args.model, "effort": args.effort, "requests": provider.calls,
               "unresolved": unresolved, "error": error, "source_registry_unchanged": unchanged}
    (root / "result.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    if error or not unchanged or (args.expect_complete and unresolved):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
