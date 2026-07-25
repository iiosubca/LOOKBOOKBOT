from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class VisualAuditItem:
    look_id: str
    status: str
    reason: str
    evidence_image: str


def load_visual_audit(project_dir: Path) -> list[VisualAuditItem]:
    """Read the controller's current visual-clearance audit without guessing state."""
    path = project_dir / "control" / "visual" / "caption-clearance.json"
    if not path.is_file():
        return []
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    raw_items = payload.get("items", []) if isinstance(payload, dict) else []
    if not isinstance(raw_items, list):
        return []
    rows: list[VisualAuditItem] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        look_id = str(item.get("look_id", "")).strip()
        if not look_id:
            continue
        rows.append(
            VisualAuditItem(
                look_id=look_id,
                status=str(item.get("status", "REVIEW")).strip().upper() or "REVIEW",
                reason=str(item.get("reason", "Нет пояснения от аудита.")).strip(),
                evidence_image=str(item.get("evidence_image", "")).strip(),
            )
        )
    return sorted(rows, key=lambda row: row.look_id)


def visual_audit_blocker_message(project_dir: Path) -> str:
    """Explain a missing PASS visual using current controller evidence."""
    rows = load_visual_audit(project_dir)
    if not rows:
        return (
            "Визуальная проверка не завершена: controller-evidence ещё не содержит "
            "caption-clearance audit. Откройте вкладку «Визуальная проверка» после следующего прогона."
        )
    blocked = [row for row in rows if row.status != "CLEAR"]
    if blocked:
        looks = ", ".join(row.look_id for row in blocked)
        return (
            f"Визуальная проверка не завершена: {len(blocked)} из {len(rows)} луков требуют внимания "
            f"({looks}). Откройте вкладку «Визуальная проверка»: там показаны причина и доказательство для каждого лука."
        )
    return (
        f"Аудит clearance чист: {len(rows)} из {len(rows)} луков имеют статус CLEAR, "
        "но controller ещё не записал PASS visual. Откройте вкладку «Визуальная проверка» и журнал: "
        "корректировка луков не требуется, нужно завершить запись подтверждений текущего proof-набора."
    )
