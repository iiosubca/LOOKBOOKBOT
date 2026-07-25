from __future__ import annotations

import json
from pathlib import Path

from lookbookbot.visual_audit import load_visual_audit, visual_audit_blocker_message


def _write_audit(project_dir: Path, items: list[dict[str, str]]) -> None:
    path = project_dir / "control" / "visual" / "caption-clearance.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"items": items}), encoding="utf-8")


def test_visual_audit_lists_only_current_controller_rows(tmp_path: Path) -> None:
    _write_audit(tmp_path, [
        {"look_id": "LOOK_002", "status": "COLLISION", "reason": "model overlaps", "evidence_image": "control/visual/LOOK_002.png"},
        {"look_id": "LOOK_001", "status": "CLEAR", "reason": "background only", "evidence_image": "control/visual/LOOK_001.png"},
    ])

    rows = load_visual_audit(tmp_path)

    assert [row.look_id for row in rows] == ["LOOK_001", "LOOK_002"]
    assert rows[1].status == "COLLISION"


def test_visual_blocker_message_distinguishes_clean_audit_from_a_problem(tmp_path: Path) -> None:
    _write_audit(tmp_path, [{"look_id": "LOOK_001", "status": "CLEAR", "reason": "background only"}])
    assert "1 из 1 луков имеют статус CLEAR" in visual_audit_blocker_message(tmp_path)

    _write_audit(tmp_path, [{"look_id": "LOOK_035", "status": "COLLISION", "reason": "model overlaps"}])
    assert "LOOK_035" in visual_audit_blocker_message(tmp_path)
