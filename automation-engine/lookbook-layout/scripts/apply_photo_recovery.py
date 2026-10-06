"""Commit only exact-photo visual recoveries before InDesign initialization."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from build_reference_registry import existing_registry, write_registry, PLACEHOLDER_PREFIX
from project_materials import project_hires


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def render(project: Path, decisions: Path):
    from PIL import Image
    from build_reference_registry import draw_card, fit_preview
    folder = project / "control/work/photo-recovery"
    queue = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    targets = {row["look_id"]: row for row in queue["targets"]}
    for item in json.loads(decisions.read_text(encoding="utf-8"))["decisions"]:
        target = targets[item["look_id"]]
        with Image.open(project / target["reference_pair"]) as pair:
            references = [pair.crop((offset * 360, 30, (offset + 1) * 360, 570)) for offset in (0, 1)]
        sources, names = [], []
        for side in ("left", "right"):
            label = item[side]
            current = target.get(f"current_{side}")
            if label == "NONE" and not current:
                sources.append(Image.new("RGB", (360, 540), "white"))
                names.append("UNRESOLVED - not a replacement photograph")
            else:
                candidate = current if label == "NONE" else queue["candidates"][label]
                source = project_hires(project) / candidate["filename"]
                if digest(source) != candidate["sha256"]:
                    raise ValueError("Source changed before rendering the exact photo proof.")
                with Image.open(source) as image:
                    sources.append(fit_preview(image, (360, 540)))
                names.append(candidate["filename"])
        proof = folder / item["look_id"] / "exact-proof.jpg"
        card = draw_card(*references, *sources, item["look_id"], tuple(names))
        card.save(proof, quality=95)
        card.close()
        for image in references + sources:
            image.close()
        print(f"PHOTO PROOF READY: {proof}")


def apply(project: Path, decisions: Path):
    project = project.resolve()
    if (project / "control/lookbook-state.json").exists():
        raise ValueError("Photo recovery cannot change an initialized or finished lookbook.")
    folder = project / "control/work/photo-recovery"
    queue = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    registry = project / "control/work/look-register.tsv"
    if queue["registry_sha256"] != digest(registry):
        raise ValueError("Photo registry changed during recovery; nothing committed.")
    reference = project / "control/work/_mat/reference.pdf"
    if queue["reference_sha256"] != digest(reference):
        raise ValueError("Reference PDF changed during recovery.")
    evidence = json.loads(decisions.read_text(encoding="utf-8"))
    targets = {row["look_id"]: row for row in queue["targets"]}
    rows = existing_registry(registry)
    by_look = {row["look_id"]: row for row in rows}
    used = {row[key] for row in rows for key in ("left_filename", "right_filename") if not row[key].startswith(PLACEHOLDER_PREFIX)}
    receipt_path = folder / "accepted.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8")) if receipt_path.exists() else {
        "schema": 1, "reference_sha256": queue["reference_sha256"], "assignments": {}}
    if receipt["reference_sha256"] != queue["reference_sha256"]:
        raise ValueError("Old photo recovery belongs to another reference.")
    for item in evidence["decisions"]:
        look = item["look_id"]
        target = targets[look]
        proof = (project / item["proof"]).resolve()
        proof.relative_to(folder.resolve())
        if digest(proof) != item["proof_sha256"] or item.get("accepted") is not True:
            raise ValueError("Exact photo proof was not confirmed or has changed.")
        if digest(project / target["reference_pair"]) != target["reference_pair_sha256"]:
            raise ValueError("Reference pair changed during recovery.")
        if len(str(item.get("note", "")).strip()) < 25:
            raise ValueError("Exact photo confirmation needs visible identity cues.")
        saved = receipt["assignments"].setdefault(look, {})
        for side in ("left", "right"):
            current = target.get(f"current_{side}")
            if current and (by_look[look][f"{side}_filename"] != current["filename"] or
                            digest(project_hires(project) / current["filename"]) != current["sha256"]):
                raise ValueError("Previously confirmed photo slot changed during partial recovery.")
            label = item.get(side, "NONE")
            if label == "NONE":
                continue
            candidate = queue["candidates"][label]
            filename = candidate["filename"]
            source = (project_hires(project) / filename).resolve()
            source.relative_to(project_hires(project).resolve())
            if filename in used or digest(source) != candidate["sha256"]:
                raise ValueError("Photograph is already assigned, absent, or changed.")
            if not by_look[look][f"{side}_filename"].startswith(PLACEHOLDER_PREFIX):
                raise ValueError("Recovery may replace only a blank photo slot.")
            used.add(filename)
            by_look[look][f"{side}_filename"] = filename
            saved.update({side: filename, f"{side}_sha256": candidate["sha256"]})
        saved.update(proof=item["proof"], proof_sha256=item["proof_sha256"], note=item["note"])
    # Validate every entry first. No partial file write on a duplicate/conflict.
    write_registry(registry, rows)
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    queue["registry_sha256"] = digest(registry)
    (folder / "manifest.json").write_text(json.dumps(queue, ensure_ascii=False, indent=2), encoding="utf-8")
    remaining = [row["look_id"] for row in rows if any(row[key].startswith(PLACEHOLDER_PREFIX) for key in ("left_filename", "right_filename"))]
    print(f"PHOTO RECOVERY SAVED: {len(evidence['decisions'])} independently verified looks; unresolved: {', '.join(remaining) or 'none'}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--render", action="store_true")
    args = parser.parse_args()
    if args.render:
        render(args.project.resolve(), args.decisions)
    else:
        apply(args.project, args.decisions)


if __name__ == "__main__":
    main()
