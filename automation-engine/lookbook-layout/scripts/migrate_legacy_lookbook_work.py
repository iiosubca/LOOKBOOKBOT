#!/usr/bin/env python3
"""Move a completed legacy lookbook's working material under control/work safely."""
from __future__ import annotations

import argparse
import csv
import shutil
from datetime import datetime, timezone
from pathlib import Path

from lookbook_gate import (
    CAPTION_MAP_FIELDS,
    assert_project_root_clean,
    current_gate,
    digest,
    load_state,
    read_json,
    state_file,
    work_path,
    write_json,
)


STATE_WORK_KEYS = ("registry", "captions", "caption_map", "caption_workbook", "caption_provenance", "hires")
ROOT_WORK_DIRECTORIES = ("_mat", "mapping-evidence", "mapping-review")


def stamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(":", "-").replace("+00-00", "Z")


def relative(project: Path, path: Path) -> str:
    return str(path.relative_to(project)).replace("\\", "/")


def remap_state_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    if normalized.startswith("control/work/"):
        return normalized
    if normalized.startswith(("_mat/", "mapping-evidence/", "mapping-review/")):
        return "control/work/" + normalized
    return "control/work/" + Path(normalized).name


def remap_caption_paths(path: Path) -> None:
    with path.open(newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source, delimiter="\t")
        if reader.fieldnames != CAPTION_MAP_FIELDS:
            raise SystemExit(f"Unexpected caption-map columns: {path}")
        rows = [{key: (value or "") for key, value in row.items()} for row in reader]
    for row in rows:
        for key in ("excel_image", "evidence_file"):
            value = row[key].replace("\\", "/")
            if value and not value.startswith("control/work/"):
                row[key] = "control/work/" + value
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=CAPTION_MAP_FIELDS, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Safely move a completed legacy project's work files below control/work.")
    parser.add_argument("project", type=Path)
    parser.add_argument("--apply", action="store_true", help="perform the migration; omit for a dry run")
    args = parser.parse_args()
    project = args.project.expanduser().resolve()
    if not project.is_dir():
        raise SystemExit(f"Project folder does not exist: {project}")
    if list(project.glob("*.idlk")):
        raise SystemExit("Close InDesign before migrating a project.")
    state = load_state(project)
    if current_gate(project, state) is not None:
        raise SystemExit("Migrate only after every controlled gate has passed; an active build must keep its frozen paths.")
    work = work_path(project)
    visible_mat = project / "_MAT"
    preserve_visible_mat = visible_mat.is_dir() and (work / "_mat").is_dir()
    moves: list[tuple[Path, Path]] = []
    for name in ROOT_WORK_DIRECTORIES:
        source = project / name
        if name == "_mat" and preserve_visible_mat:
            continue
        if source.exists():
            moves.append((source, work / name))
    for key in STATE_WORK_KEYS:
        source = project / state[key]
        target = project / remap_state_path(str(state[key]))
        if source.exists() and source.resolve() != target.resolve() and not any(source == planned[0] for planned in moves):
            moves.append((source, target))
    protected = {source.resolve() for source, _ in moves}
    for item in project.iterdir():
        if (
            item.name.casefold() in {"control", "control-history", "gender"}
            or (preserve_visible_mat and item.name.casefold() == "_mat")
            or item.suffix.lower() in {".indd", ".pdf", ".idlk"}
        ):
            continue
        if item.resolve() not in protected:
            moves.append((item, work / "legacy-root" / item.name))
    if not args.apply:
        print("DRY RUN: no files were moved.")
        for source, target in moves:
            print(f"{relative(project, source)} -> {relative(project, target)}")
        return
    targets = [target.resolve() for _, target in moves]
    if len(targets) != len(set(targets)):
        raise SystemExit("Migration plan has conflicting targets; no files were moved.")
    existing_targets = [str(target) for target in targets if target.exists()]
    if existing_targets:
        raise SystemExit("Migration will not overwrite existing targets: " + ", ".join(existing_targets))
    history = project / "control" / "history" / f"work-area-migration-{stamp()}"
    history.mkdir(parents=True, exist_ok=False)
    shutil.copy2(state_file(project), history / "lookbook-state.before.json")
    work.mkdir(parents=True, exist_ok=True)
    completed: list[dict[str, str]] = []
    for source, target in moves:
        if not source.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        completed.append({"from": relative(project, source), "to": relative(project, target)})
    for key in STATE_WORK_KEYS:
        state[key] = remap_state_path(str(state[key]))
    caption_map = project / state["caption_map"]
    remap_caption_paths(caption_map)
    provenance_path = project / state["caption_provenance"]
    provenance = read_json(provenance_path)
    provenance["caption_map_sha256"] = digest(caption_map)
    write_json(provenance_path, provenance)
    map_evidence_path = project / "control" / "evidence" / "map.json"
    map_evidence = read_json(map_evidence_path)
    for evidence_key, state_key in (
        ("registry_sha256", "registry"),
        ("caption_map_sha256", "caption_map"),
        ("caption_workbook_sha256", "caption_workbook"),
        ("caption_data_sha256", "captions"),
        ("caption_provenance_sha256", "caption_provenance"),
    ):
        map_evidence[evidence_key] = digest(project / state[state_key])
    write_json(map_evidence_path, map_evidence)
    state["work_area_migrated_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    write_json(state_file(project), state)
    write_json(history / "manifest.json", {"schema": 1, "moves": completed, "state_after": state})
    assert_project_root_clean(project)
    if current_gate(project, load_state(project)) is not None:
        raise SystemExit("Migration changed the controlled gate state; inspect the preserved state backup before continuing.")
    print(f"WORK AREA MIGRATED: {work}")
    print(f"History: {history}")


if __name__ == "__main__":
    main()
