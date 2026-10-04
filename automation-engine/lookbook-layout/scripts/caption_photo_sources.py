"""The same visible photo inputs for proposal, proof and map validation."""
from __future__ import annotations

from pathlib import Path


PLACEHOLDER_PREFIX = "__lbb_missing_"


def caption_photo_source(project: Path, row: dict[str, str], hires: Path, side: str) -> Path:
    filename = str(row[f"{side}_filename"])
    if filename.casefold().startswith(PLACEHOLDER_PREFIX):
        source = project / "control" / "work" / "missing-photo-reference" / f"{row['look_id']}_{side.upper()}.jpg"
    else:
        source = hires / filename
    if not source.is_file():
        raise ValueError(f"{row['look_id']}: missing visible {side} photo for credit comparison: {source}")
    return source
