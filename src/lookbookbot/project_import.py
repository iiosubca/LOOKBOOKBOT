from __future__ import annotations

import csv
import re
from datetime import date
from pathlib import Path

from .controller import evidence_passed, final_outputs_passed
from .domain import ProjectRecord, ProviderKind, STAGES, StageStatus
from .state import StateStore


class ExistingProjectError(RuntimeError):
    pass


_DATE_FROM_NAME = re.compile(r"TSUM_FS-0(?P<date>\d{6})", re.IGNORECASE)


def open_existing_project(
    store: StateStore,
    project_dir: Path,
    *,
    provider: ProviderKind,
    model: str,
    reasoning_effort: str = "",
) -> ProjectRecord:
    """Import durable controller evidence from an existing delivery folder."""
    project_dir = project_dir.resolve()
    control = project_dir / "control"
    if not control.is_dir():
        raise ExistingProjectError("В выбранной папке нет control — это не контролируемый проект LOOKBOOKBOT.")
    masters = sorted(project_dir.glob("*.indd"), key=lambda path: path.name.casefold())
    match = next((_DATE_FROM_NAME.search(master.name) for master in reversed(masters) if _DATE_FROM_NAME.search(master.name)), None)
    if match is None:
        raise ExistingProjectError("Не удалось определить дату по имени INDD `TSUM_FS-0YYMMDD...`.")
    raw = match.group("date")
    show_date = date(2000 + int(raw[:2]), int(raw[2:4]), int(raw[4:6]))
    project = store.save_project(
        name=project_dir.name,
        source_dir=control / "work" / "_mat",
        output_root=project_dir.parent,
        project_dir=project_dir,
        show_date=show_date,
        provider=provider,
        model=model,
        reasoning_effort=reasoning_effort,
    )
    registry = control / "work" / "look-register.tsv"
    if registry.is_file():
        store.replace_looks(project.id, _read_tsv(registry))
    caption_map = control / "work" / "caption-map.tsv"
    if caption_map.is_file():
        store.replace_credits(project.id, _read_tsv(caption_map))

    store.reset_from(project.id, "prepare")
    passed = {
        "prepare": (control / "work").is_dir(),
        "looks": registry.is_file(),
        "credits_map": caption_map.is_file() and all(
            row.get("visual_status") == "CONFIRMED" for row in _read_tsv(caption_map)
        ),
        "map": evidence_passed(project_dir, "map"),
        "structure": evidence_passed(project_dir, "structure"),
        "dates": evidence_passed(project_dir, "dates"),
        "frames": evidence_passed(project_dir, "frames"),
        "images": evidence_passed(project_dir, "images"),
        "captions": evidence_passed(project_dir, "captions"),
        "visual": evidence_passed(project_dir, "visual"),
        "review": evidence_passed(project_dir, "pdf"),
        "final": final_outputs_passed(project_dir),
    }
    for stage in STAGES:
        if passed.get(stage.key):
            store.set_stage(project.id, stage.key, StageStatus.PASSED, details={"restored_from": "controller evidence"})
    return project


def _read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as source:
        return [{key: (value or "").strip() for key, value in row.items()} for row in csv.DictReader(source, delimiter="\t")]
