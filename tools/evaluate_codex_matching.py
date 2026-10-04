"""Read-only regression evaluation of known matching mistakes; never opens INDD."""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "automation-engine/lookbook-layout/scripts"))

from PIL import Image
from pypdf import PdfReader
from build_reference_registry import visible_pdf_draws
from lookbook_gate import render_pdf_pages
from reference_photo_render import rendered_photo_pair
from render_caption_mapping_evidence import PANEL, paste_panel
from build_targeted_rematch_proofs import render_candidate_sheet, render_pair
from lookbookbot.pipeline import PipelineEngine, _parse_codex_rematch_candidate
from lookbookbot.providers import CodexProvider


CASES = [(3, "W", 22, True), (3, "M", 18, False),
         (5, "W", 20, True), (5, "M", 22, False),
         (52, "W", 21, True), (52, "W", 28, False)]


def prepare(project: Path, output: Path) -> list[dict]:
    reference = project / "control/work/_mat/reference.pdf"
    reader = PdfReader(reference)
    pages = render_pdf_pages(reference, output / "reference-pages", sorted({x[0] for x in CASES}), 120, crop_box=True)
    cards = []
    for page_number, sheet, number, expected in CASES:
        look_id = f"LOOK_{page_number:03}"
        case_id = f"{look_id}-{sheet}{number}"
        excel = next((project / "control/work/_mat/excel-images").glob(f"{sheet}_{number:03}_01.*"))
        page = reader.pages[page_number - 1]
        draws = visible_pdf_draws(page)
        photos = rendered_photo_pair(page, pages[page_number], [matrix for matrix, _ in draws])
        sources = [excel]
        for side, photo in zip(("LEFT", "RIGHT"), photos):
            target = output / "photos" / f"{look_id}_{side}.jpg"
            target.parent.mkdir(parents=True, exist_ok=True)
            photo.save(target, "JPEG", quality=95)
            photo.close()
            sources.append(target)
        evidence = output / "cards" / f"{case_id}.jpg"
        evidence.parent.mkdir(parents=True, exist_ok=True)
        card = Image.new("RGB", (PANEL[0] * 3, PANEL[1]), "white")
        for i, (source, title) in enumerate(zip(sources, (f"EXCEL {sheet}:{number}", "PDF LEFT", "PDF RIGHT"))):
            paste_panel(card, source, i * PANEL[0], title)
        card.save(evidence, "JPEG", quality=95)
        card.close()
        cards.append({"case": case_id, "look_id": look_id, "expected": expected,
                      "evidence_file": str(evidence.relative_to(output))})
    (output / "cases.json").write_text(json.dumps(cards, ensure_ascii=False, indent=2), encoding="utf-8")
    return cards


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="gpt-6.1-sol")
    parser.add_argument("--effort", default="low")
    parser.add_argument("--live", action="store_true", help="Use the signed-in Codex account for six bounded comparisons.")
    parser.add_argument("--choices-only", action="store_true", help="Test two bounded selections from a six-card candidate board.")
    args = parser.parse_args()
    output = args.output.resolve()
    cases = json.loads((output / "cases.json").read_text(encoding="utf-8")) if (
        args.choices_only and (output / "cases.json").is_file()
    ) else prepare(args.project.resolve(), output)
    if not args.live and not args.choices_only:
        print(f"PREPARED {len(cases)} independent regression cards: {output}")
        return
    provider = CodexProvider(args.model, reasoning_effort=args.effort)
    engine = object.__new__(PipelineEngine)
    start = time.monotonic()
    results = []
    try:
        if args.choices_only:
            labels = [(sheet, str(number)) for _, sheet, number, _ in CASES]
            cards = [{"excel_sheet": sheet, "excel_look_number": number,
                      "excel_image": str(next((args.project / "control/work/_mat/excel-images").glob(f"{sheet}_{int(number):03}_01.*")).relative_to(args.project))}
                     for sheet, number in labels]
            board = output / "candidate-board.jpg"
            render_candidate_sheet(args.project.resolve(), cards, board, 1)
            for look_id, expected in [("LOOK_003", ("W", "22")), ("LOOK_052", ("W", "21"))]:
                pair = output / f"{look_id}-pair.jpg"
                render_pair(output, {"look_id": look_id, "left_filename": f"{look_id}_LEFT.jpg", "right_filename": f"{look_id}_RIGHT.jpg"}, output / "photos", pair)
                raw = provider.run_readonly_closed_board_vision(
                    f"Choose the Excel card showing the same outfit as both photos in the FIRST image for {look_id}. "
                    "The SECOND image is the labelled Excel candidate board. Independently compare garment type, colour, "
                    "model, bag and shoes. Reply with look_id, choice as a printed board label, and two specific visible cues in note.",
                    output, images=[pair, board], look_id=look_id,
                    allowed_labels=[f"{sheet}:{number}" for sheet, number in labels], timeout=600,
                )
                selected = _parse_codex_rematch_candidate(raw, look_id, set(labels))
                result = {"case": f"{look_id}-selection", "expected": expected, "selected": selected, "passed": selected == expected}
                results.append(result)
                print(json.dumps(result, ensure_ascii=False), flush=True)
        else:
            with ThreadPoolExecutor(max_workers=2) as executor:
                jobs = {executor.submit(engine._inspect_codex_credit_batch, provider, output, [case]): case for case in cases}
                for job in as_completed(jobs):
                    case = jobs[job]
                    try:
                        decision = job.result()[case["look_id"]]
                        result = {**case, "accepted": decision.accepted, "note": decision.note,
                                  "passed": decision.accepted == case["expected"]}
                    except Exception as error:
                        result = {**case, "passed": False, "error": str(error)}
                    results.append(result)
                    print(json.dumps(result, ensure_ascii=False), flush=True)
    finally:
        provider.close()
    summary = {"model": args.model, "effort": args.effort, "elapsed_seconds": round(time.monotonic() - start, 2),
               "passed": sum(x["passed"] for x in results), "total": len(results), "cases": results}
    (output / ("selection-results.json" if args.choices_only else "results.json")).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"EVALUATION {summary['passed']}/{summary['total']} in {summary['elapsed_seconds']}s", flush=True)
    if summary["passed"] != summary["total"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
