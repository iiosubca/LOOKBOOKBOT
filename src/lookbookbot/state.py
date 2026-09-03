from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from .config import app_data_dir
from .domain import BuildMode, ProjectRecord, ProviderKind, STAGES, StageStatus


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
                    build_mode TEXT NOT NULL DEFAULT 'full',
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
                    manual_override INTEGER NOT NULL DEFAULT 0,
                    manual_confirmed_at TEXT,
                    needs_rematch INTEGER NOT NULL DEFAULT 0,
                    rematch_requested_at TEXT,
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
                CREATE TABLE IF NOT EXISTS google_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at_epoch INTEGER NOT NULL,
                    pacific_day TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_google_usage_time ON google_usage(created_at_epoch);
                CREATE INDEX IF NOT EXISTS idx_google_usage_day ON google_usage(pacific_day);
                """
            )
            credit_columns = {str(row["name"]) for row in db.execute("PRAGMA table_info(credits)").fetchall()}
            if "manual_override" not in credit_columns:
                db.execute("ALTER TABLE credits ADD COLUMN manual_override INTEGER NOT NULL DEFAULT 0")
            if "manual_confirmed_at" not in credit_columns:
                db.execute("ALTER TABLE credits ADD COLUMN manual_confirmed_at TEXT")
            if "needs_rematch" not in credit_columns:
                db.execute("ALTER TABLE credits ADD COLUMN needs_rematch INTEGER NOT NULL DEFAULT 0")
            if "rematch_requested_at" not in credit_columns:
                db.execute("ALTER TABLE credits ADD COLUMN rematch_requested_at TEXT")
            project_columns = {str(row["name"]) for row in db.execute("PRAGMA table_info(projects)").fetchall()}
            if "build_mode" not in project_columns:
                db.execute("ALTER TABLE projects ADD COLUMN build_mode TEXT NOT NULL DEFAULT 'full'")

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
        build_mode: BuildMode | str | None = None,
    ) -> ProjectRecord:
        now = utc_now()
        with self.connect() as db:
            existing = db.execute("SELECT id FROM projects WHERE project_dir = ?", (str(project_dir),)).fetchone()
            project_id = str(existing["id"]) if existing else str(uuid.uuid4())
            existing_mode = "full"
            if existing:
                existing_row = db.execute("SELECT build_mode FROM projects WHERE id = ?", (project_id,)).fetchone()
                existing_mode = str(existing_row["build_mode"]) if existing_row else "full"
            requested_mode = build_mode.value if isinstance(build_mode, BuildMode) else str(build_mode or existing_mode)
            if requested_mode not in {mode.value for mode in BuildMode}:
                requested_mode = BuildMode.FULL.value
            # A deleted delivery folder is a new attempt, even when its date
            # happens to reproduce a previous database path. Never resurrect
            # stale cards, stages, or manual decisions into that new project.
            if existing and not project_dir.exists():
                for table in ("stages", "looks", "credits", "runs"):
                    db.execute(f"DELETE FROM {table} WHERE project_id = ?", (project_id,))
            db.execute("UPDATE projects SET active = 0")
            db.execute(
                """
                INSERT INTO projects(id,name,source_dir,output_root,project_dir,show_date,provider,model,build_mode,active,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,1,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name, source_dir=excluded.source_dir, output_root=excluded.output_root,
                    show_date=excluded.show_date, provider=excluded.provider, model=excluded.model,
                    build_mode=excluded.build_mode,
                    active=1, updated_at=excluded.updated_at
                """,
                (
                    project_id, name, str(source_dir), str(output_root), str(project_dir), show_date.isoformat(),
                    provider.value, model.strip(), requested_mode, now, now,
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
        try:
            build_mode = BuildMode(str(row["build_mode"]))
        except (KeyError, ValueError):
            build_mode = BuildMode.FULL
        return ProjectRecord(
            id=row["id"], name=row["name"], source_dir=Path(row["source_dir"]),
            output_root=Path(row["output_root"]), project_dir=Path(row["project_dir"]),
            show_date=date.fromisoformat(row["show_date"]), provider=ProviderKind(row["provider"]), model=row["model"],
            build_mode=build_mode,
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

    def set_build_mode(self, project_id: str, build_mode: BuildMode | str) -> None:
        value = build_mode.value if isinstance(build_mode, BuildMode) else str(build_mode)
        if value not in {mode.value for mode in BuildMode}:
            raise ValueError(f"Неизвестный режим сборки: {value}")
        with self.connect() as db:
            db.execute(
                "UPDATE projects SET build_mode = ?, updated_at = ? WHERE id = ?",
                (value, utc_now(), project_id),
            )

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

    def confirm_prior_stages(self, project_id: str, stage_key: str, *, reason: str) -> None:
        """Align the desktop timeline with controller-proofed earlier gates."""
        keys = [stage.key for stage in STAGES]
        stop = keys.index(stage_key)
        now = utc_now()
        details = json.dumps({"controller_reconciled": True, "reason": reason}, ensure_ascii=False)
        with self.connect() as db:
            for key in keys[:stop]:
                db.execute(
                    """
                    UPDATE stages
                    SET status=?, started_at=COALESCE(started_at, ?), completed_at=?,
                        error='', details_json=?
                    WHERE project_id=? AND stage_key=?
                    """,
                    (StageStatus.PASSED.value, now, now, details, project_id, key),
                )
            db.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (now, project_id))

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
        """Refresh controller data without erasing an unconsumed human override.

        A manual Excel card choice is durable UI state. It is cleared only
        when the controller map returns the same pair, which means the
        controlled selection was successfully incorporated and re-checked.
        """
        with self.connect() as db:
            existing = {
                str(row["look_id"]): dict(row)
                for row in db.execute("SELECT * FROM credits WHERE project_id = ?", (project_id,)).fetchall()
            }
            incoming_ids = {str(row["look_id"]) for row in rows}
            for row in rows:
                look_id = row["look_id"]
                previous = existing.get(look_id)
                pair_matches = previous and _same_credit_pair(previous, row)
                if previous and previous.get("manual_override") and not pair_matches:
                    # Keep the human decision visible after a failed or
                    # incomplete pipeline pass. The new map is not allowed to
                    # silently replace it.
                    continue
                db.execute(
                    """
                    INSERT INTO credits(project_id,look_id,excel_sheet,excel_look_number,excel_image,evidence_file,visual_status,note,manual_override,manual_confirmed_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(project_id,look_id) DO UPDATE SET
                        excel_sheet=excluded.excel_sheet,
                        excel_look_number=excluded.excel_look_number,
                        excel_image=excluded.excel_image,
                        evidence_file=excluded.evidence_file,
                        visual_status=excluded.visual_status,
                        note=CASE WHEN credits.note <> '' THEN credits.note ELSE excluded.note END,
                        manual_override=excluded.manual_override,
                        manual_confirmed_at=excluded.manual_confirmed_at
                    """,
                    (
                        project_id, look_id, row.get("excel_sheet", ""), row.get("excel_look_number", ""),
                        row.get("excel_image", ""), row.get("evidence_file", ""), row.get("visual_status", "PENDING"),
                        row.get("note", ""), 0, None,
                    ),
                )
            for look_id, previous in existing.items():
                if look_id not in incoming_ids and not previous.get("manual_override"):
                    db.execute("DELETE FROM credits WHERE project_id = ? AND look_id = ?", (project_id, look_id))

    def credits(self, project_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM credits WHERE project_id = ? ORDER BY look_id", (project_id,)).fetchall()
        return [dict(row) for row in rows]

    def update_credit(self, project_id: str, look_id: str, **changes: Any) -> None:
        allowed = {"excel_sheet", "excel_look_number", "visual_status", "note", "manual_override", "manual_confirmed_at"}
        changes = {key: value for key, value in changes.items() if key in allowed}
        if not changes:
            return
        columns = ", ".join(f"{key} = ?" for key in changes)
        with self.connect() as db:
            db.execute(f"UPDATE credits SET {columns} WHERE project_id = ? AND look_id = ?", (*changes.values(), project_id, look_id))

    def save_credit_overrides(self, project_id: str, edits: list[dict[str, str]]) -> int:
        """Commit valid human corrections as one durable transaction."""
        changed = 0
        now = utc_now()
        with self.connect() as db:
            current = {
                str(row["look_id"]): dict(row)
                for row in db.execute("SELECT * FROM credits WHERE project_id = ?", (project_id,)).fetchall()
            }
            for edit in edits:
                look_id = str(edit["look_id"])
                previous = current.get(look_id)
                if previous is None:
                    continue
                sheet = str(edit.get("excel_sheet", "")).strip()
                number = str(edit.get("excel_look_number", "")).strip()
                note = str(edit.get("note", "")).strip()
                if not sheet or not number:
                    raise ValueError(f"{look_id}: укажите и лист Excel, и номер карточки.")
                if not number.isdigit():
                    raise ValueError(f"{look_id}: номер карточки Excel должен состоять только из цифр.")
                if (sheet, number, note) == (previous["excel_sheet"], previous["excel_look_number"], previous["note"]):
                    continue
                db.execute(
                    """
                    UPDATE credits
                    SET excel_sheet=?, excel_look_number=?, note=?, visual_status='CONFIRMED',
                        manual_override=1, manual_confirmed_at=?
                    WHERE project_id=? AND look_id=?
                    """,
                    (sheet, number, note, now, project_id, look_id),
                )
                changed += 1
            if changed:
                db.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (now, project_id))
        return changed

    def duplicate_credit_pairs(self, project_id: str) -> dict[tuple[str, str], list[str]]:
        pairs: dict[tuple[str, str], list[str]] = {}
        for row in self.credits(project_id):
            sheet = str(row["excel_sheet"] or "").strip()
            number = str(row["excel_look_number"] or "").strip()
            if not sheet or not number:
                continue
            key = (sheet.casefold(), number)
            pairs.setdefault(key, []).append(str(row["look_id"]))
        return {key: looks for key, looks in pairs.items() if len(looks) > 1}

    def set_credit_rematch_requested(self, project_id: str, look_id: str, requested: bool) -> None:
        """Persist the operator's target list without altering the map itself."""
        with self.connect() as db:
            db.execute(
                """
                UPDATE credits
                SET needs_rematch = ?, rematch_requested_at = ?,
                    manual_override = CASE WHEN ? THEN 0 ELSE manual_override END,
                    manual_confirmed_at = CASE WHEN ? THEN NULL ELSE manual_confirmed_at END
                WHERE project_id = ? AND look_id = ?
                """,
                (int(requested), utc_now() if requested else None, int(requested), int(requested), project_id, look_id),
            )

    def requested_credit_rematches(self, project_id: str) -> list[str]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT look_id FROM credits WHERE project_id = ? AND needs_rematch = 1 ORDER BY look_id",
                (project_id,),
            ).fetchall()
        return [str(row["look_id"]) for row in rows]

    def clear_credit_rematches(self, project_id: str, look_ids: list[str]) -> None:
        if not look_ids:
            return
        placeholders = ", ".join("?" for _ in look_ids)
        with self.connect() as db:
            db.execute(
                f"UPDATE credits SET needs_rematch = 0, rematch_requested_at = NULL "
                f"WHERE project_id = ? AND look_id IN ({placeholders})",
                (project_id, *look_ids),
            )

    def google_usage_status(self, *, now_epoch: int | None = None) -> dict[str, int]:
        """Return local accounting for the Google AI Studio project limits."""
        now = now_epoch if now_epoch is not None else int(datetime.now(timezone.utc).timestamp())
        day = datetime.fromtimestamp(now, ZoneInfo("America/Los_Angeles")).date().isoformat()
        with self.connect() as db:
            minute = db.execute(
                "SELECT COUNT(*) AS requests, COALESCE(SUM(input_tokens + output_tokens), 0) AS tokens "
                "FROM google_usage WHERE created_at_epoch > ?", (now - 60,)
            ).fetchone()
            daily = db.execute(
                "SELECT COUNT(*) AS requests FROM google_usage WHERE pacific_day = ?", (day,)
            ).fetchone()
        return {
            "rpm_used": int(minute["requests"]), "rpm_limit": 15,
            "tpm_used": int(minute["tokens"]), "tpm_limit": 250_000,
            "rpd_used": int(daily["requests"]), "rpd_limit": 500,
        }

    def reserve_google_request(self, estimated_input_tokens: int) -> int:
        """Reserve a request before sending it, so parallel workers stay below limits."""
        estimated = max(1, int(estimated_input_tokens))
        now = int(datetime.now(timezone.utc).timestamp())
        day = datetime.fromtimestamp(now, ZoneInfo("America/Los_Angeles")).date().isoformat()
        with self.connect() as db:
            # Check and reservation use one transaction, so parallel workers
            # cannot both consume the last available request.
            db.execute("BEGIN IMMEDIATE")
            minute = db.execute(
                "SELECT COUNT(*) AS requests, COALESCE(SUM(input_tokens + output_tokens), 0) AS tokens "
                "FROM google_usage WHERE created_at_epoch > ?", (now - 60,)
            ).fetchone()
            daily = db.execute(
                "SELECT COUNT(*) AS requests FROM google_usage WHERE pacific_day = ?", (day,)
            ).fetchone()
            if int(minute["requests"]) >= 15:
                raise ValueError("Google AI Studio: исчерпан лимит RPM 15/15. Повторите через минуту.")
            if int(minute["tokens"]) + estimated > 250_000:
                raise ValueError("Google AI Studio: следующий запрос превысит лимит TPM 250K. Повторите после обновления минутного окна.")
            if int(daily["requests"]) >= 500:
                raise ValueError("Google AI Studio: исчерпан суточный лимит RPD 500/500. Он обновится в полночь по Pacific Time.")
            cursor = db.execute(
                "INSERT INTO google_usage(created_at_epoch,pacific_day,input_tokens,output_tokens) VALUES(?,?,?,0)",
                (now, day, estimated),
            )
            return int(cursor.lastrowid)

    def settle_google_request(self, reservation_id: int, input_tokens: int | None, output_tokens: int | None) -> None:
        """Replace the conservative reservation with API-reported token usage when available."""
        if input_tokens is None and output_tokens is None:
            return
        with self.connect() as db:
            current = db.execute("SELECT input_tokens, output_tokens FROM google_usage WHERE id = ?", (reservation_id,)).fetchone()
            if current is None:
                return
            db.execute(
                "UPDATE google_usage SET input_tokens = ?, output_tokens = ? WHERE id = ?",
                (
                    max(0, int(input_tokens)) if input_tokens is not None else int(current["input_tokens"]),
                    max(0, int(output_tokens)) if output_tokens is not None else int(current["output_tokens"]),
                    reservation_id,
                ),
            )

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


def _same_credit_pair(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return (
        str(left.get("excel_sheet", "")).strip().casefold(),
        str(left.get("excel_look_number", "")).strip(),
    ) == (
        str(right.get("excel_sheet", "")).strip().casefold(),
        str(right.get("excel_look_number", "")).strip(),
    )
