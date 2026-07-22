from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import app_data_dir
from .domain import ProjectRecord, ProviderKind, STAGES, StageStatus


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class StateStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or app_data_dir() / "lookbookbot.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    source_dir TEXT NOT NULL,
                    output_root TEXT NOT NULL,
                    project_dir TEXT NOT NULL UNIQUE,
                    show_date TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL DEFAULT '',
                    active INTEGER NOT NULL DEFAULT 0,
                    approved INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS stages (
                    project_id TEXT NOT NULL,
                    stage_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    error TEXT NOT NULL DEFAULT '',
                    details_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY (project_id, stage_key),
                    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS looks (
                    project_id TEXT NOT NULL,
                    look_id TEXT NOT NULL,
                    spread_order INTEGER,
                    pdf_spread INTEGER,
                    left_filename TEXT NOT NULL DEFAULT '',
                    right_filename TEXT NOT NULL DEFAULT '',
                    indd_left_page INTEGER,
                    indd_right_page INTEGER,
                    status TEXT NOT NULL DEFAULT 'pending',
                    note TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (project_id, look_id),
                    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS credits (
                    project_id TEXT NOT NULL,
                    look_id TEXT NOT NULL,
                    excel_sheet TEXT NOT NULL DEFAULT '',
                    excel_look_number TEXT NOT NULL DEFAULT '',
                    excel_image TEXT NOT NULL DEFAULT '',
                    evidence_file TEXT NOT NULL DEFAULT '',
                    visual_status TEXT NOT NULL DEFAULT 'PENDING',
                    note TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (project_id, look_id),
                    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL,
                    stage_key TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    command TEXT NOT NULL DEFAULT '',
                    output TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )

    def save_project(
        self,
        *,
        name: str,
        source_dir: Path,
        output_root: Path,
        project_dir: Path,
        show_date: date,
        provider: ProviderKind,
        model: str,
    ) -> ProjectRecord:
        now = utc_now()
        with self.connect() as db:
            existing = db.execute("SELECT id FROM projects WHERE project_dir = ?", (str(project_dir),)).fetchone()
            project_id = str(existing["id"]) if existing else str(uuid.uuid4())
            db.execute("UPDATE projects SET active = 0")
            db.execute(
                """
                INSERT INTO projects(id,name,source_dir,output_root,project_dir,show_date,provider,model,active,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,1,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name, source_dir=excluded.source_dir, output_root=excluded.output_root,
                    show_date=excluded.show_date, provider=excluded.provider, model=excluded.model,
                    active=1, updated_at=excluded.updated_at
                """,
                (
                    project_id, name, str(source_dir), str(output_root), str(project_dir), show_date.isoformat(),
                    provider.value, model.strip(), now, now,
                ),
            )
            for stage in STAGES:
                db.execute(
                    "INSERT OR IGNORE INTO stages(project_id,stage_key,status) VALUES(?,?,?)",
                    (project_id, stage.key, StageStatus.PENDING.value),
                )
        return self.get_project(project_id)

    def get_project(self, project_id: str) -> ProjectRecord:
        with self.connect() as db:
            row = db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise KeyError(project_id)
        return ProjectRecord(
            id=row["id"], name=row["name"], source_dir=Path(row["source_dir"]),
            output_root=Path(row["output_root"]), project_dir=Path(row["project_dir"]),
            show_date=date.fromisoformat(row["show_date"]), provider=ProviderKind(row["provider"]), model=row["model"],
        )

    def active_project(self) -> ProjectRecord | None:
        with self.connect() as db:
            row = db.execute("SELECT id FROM projects WHERE active = 1 ORDER BY updated_at DESC LIMIT 1").fetchone()
        return self.get_project(row["id"]) if row else None

    def list_projects(self) -> list[ProjectRecord]:
        with self.connect() as db:
            rows = db.execute("SELECT id FROM projects ORDER BY updated_at DESC").fetchall()
        return [self.get_project(row["id"]) for row in rows]

    def set_active(self, project_id: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE projects SET active = CASE WHEN id = ? THEN 1 ELSE 0 END", (project_id,))

    def set_approved(self, project_id: str, approved: bool) -> None:
        with self.connect() as db:
            db.execute("UPDATE projects SET approved = ?, updated_at = ? WHERE id = ?", (int(approved), utc_now(), project_id))

    def is_approved(self, project_id: str) -> bool:
        with self.connect() as db:
            row = db.execute("SELECT approved FROM projects WHERE id = ?", (project_id,)).fetchone()
        return bool(row and row["approved"])

    def stage_rows(self, project_id: str) -> dict[str, dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM stages WHERE project_id = ?", (project_id,)).fetchall()
        return {row["stage_key"]: dict(row) for row in rows}

    def set_stage(
        self,
        project_id: str,
        stage_key: str,
        status: StageStatus,
        *,
        error: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        now = utc_now()
        started = now if status == StageStatus.RUNNING else None
        completed = now if status == StageStatus.PASSED else None
        with self.connect() as db:
            current = db.execute(
                "SELECT started_at FROM stages WHERE project_id = ? AND stage_key = ?", (project_id, stage_key)
            ).fetchone()
            db.execute(
                """
                UPDATE stages SET status=?, started_at=?, completed_at=?, error=?, details_json=?
                WHERE project_id=? AND stage_key=?
                """,
                (
                    status.value,
                    (current["started_at"] if current and current["started_at"] else started),
                    completed,
                    error,
                    json.dumps(details or {}, ensure_ascii=False),
                    project_id,
                    stage_key,
                ),
            )
            db.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (now, project_id))

    def first_incomplete_stage(self, project_id: str) -> str:
        rows = self.stage_rows(project_id)
        for stage in STAGES:
            if rows.get(stage.key, {}).get("status") != StageStatus.PASSED.value:
                return stage.key
        return STAGES[-1].key

    def reset_from(self, project_id: str, stage_key: str) -> None:
        keys = [stage.key for stage in STAGES]
        start = keys.index(stage_key)
        with self.connect() as db:
            for key in keys[start:]:
                db.execute(
                    "UPDATE stages SET status=?, started_at=NULL, completed_at=NULL, error='', details_json='{}' WHERE project_id=? AND stage_key=?",
                    (StageStatus.PENDING.value, project_id, key),
                )

    def replace_looks(self, project_id: str, rows: list[dict[str, str]]) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM looks WHERE project_id = ?", (project_id,))
            for row in rows:
                db.execute(
                    """INSERT INTO looks(project_id,look_id,spread_order,pdf_spread,left_filename,right_filename,indd_left_page,indd_right_page,status,note)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        project_id, row["look_id"], _int_or_none(row.get("spread_order")), _int_or_none(row.get("pdf_spread")),
                        row.get("left_filename", ""), row.get("right_filename", ""),
                        _int_or_none(row.get("indd_left_page")), _int_or_none(row.get("indd_right_page")),
                        row.get("status", "confirmed"), row.get("note", ""),
                    ),
                )

    def looks(self, project_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM looks WHERE project_id = ? ORDER BY spread_order,look_id", (project_id,)).fetchall()
        return [dict(row) for row in rows]

    def update_look(self, project_id: str, look_id: str, **changes: Any) -> None:
        allowed = {"left_filename", "right_filename", "status", "note", "pdf_spread"}
        changes = {key: value for key, value in changes.items() if key in allowed}
        if not changes:
            return
        columns = ", ".join(f"{key} = ?" for key in changes)
        with self.connect() as db:
            db.execute(f"UPDATE looks SET {columns} WHERE project_id = ? AND look_id = ?", (*changes.values(), project_id, look_id))

    def replace_credits(self, project_id: str, rows: list[dict[str, str]]) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM credits WHERE project_id = ?", (project_id,))
            for row in rows:
                db.execute(
                    """INSERT INTO credits(project_id,look_id,excel_sheet,excel_look_number,excel_image,evidence_file,visual_status,note)
                    VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        project_id, row["look_id"], row.get("excel_sheet", ""), row.get("excel_look_number", ""),
                        row.get("excel_image", ""), row.get("evidence_file", ""), row.get("visual_status", "PENDING"), row.get("note", ""),
                    ),
                )

    def credits(self, project_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM credits WHERE project_id = ? ORDER BY look_id", (project_id,)).fetchall()
        return [dict(row) for row in rows]

    def update_credit(self, project_id: str, look_id: str, **changes: Any) -> None:
        allowed = {"excel_sheet", "excel_look_number", "visual_status", "note"}
        changes = {key: value for key, value in changes.items() if key in allowed}
        if not changes:
            return
        columns = ", ".join(f"{key} = ?" for key in changes)
        with self.connect() as db:
            db.execute(f"UPDATE credits SET {columns} WHERE project_id = ? AND look_id = ?", (*changes.values(), project_id, look_id))

    def start_run(self, project_id: str, stage_key: str, command: str = "") -> int:
        with self.connect() as db:
            cursor = db.execute(
                "INSERT INTO runs(project_id,stage_key,started_at,status,command) VALUES(?,?,?,?,?)",
                (project_id, stage_key, utc_now(), StageStatus.RUNNING.value, command),
            )
            return int(cursor.lastrowid)

    def finish_run(self, run_id: int, status: StageStatus, output: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE runs SET finished_at=?, status=?, output=? WHERE id=?",
                (utc_now(), status.value, output[-100_000:], run_id),
            )

    def recent_runs(self, project_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM runs WHERE project_id = ? ORDER BY id DESC LIMIT ?", (project_id, limit)
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def get_setting(self, key: str, default: str = "") -> str:
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )


def _int_or_none(value: Any) -> int | None:
    text = str(value or "").strip()
    return int(text) if text.isdigit() else None

