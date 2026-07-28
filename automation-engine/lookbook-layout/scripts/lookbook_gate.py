#!/usr/bin/env python3
"""Evidence-first release controller for Adobe InDesign lookbooks.

The controller never edits an InDesign document.  It records a build session,
checks the project data, and accepts an InDesign audit only when the audit was
made for the current saved master and the currently armed gate.  A PDF permit
is produced only after the release audit passes; the produced PDF is then
checked and rendered before the session can be completed.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


SCHEMA = 1
GATES = ("map", "structure", "dates", "frames", "images", "captions", "visual", "release", "pdf")
INDESIGN_GATES = {"structure", "dates", "frames", "images", "captions", "release"}
# Exporting the current 102-page master can legitimately take several minutes
# in InDesign.  Recovery is for an idle/stalled worker, not a busy exporter.
VISUAL_PROOF_TIMEOUT_SECONDS = 720
# Full-length images are fitted 800 pt wide into a 600 pt fixed frame, leaving
# exactly 100 pt of safe horizontal crop travel on either side.
# A crop is an editorial correction, not a way to push a full-length model to
# an edge. Keep it within half an inch of the standard centered fill. If that
# is insufficient, the existing credits frame must use the down/right fallback.
MAX_CLEARANCE_SHIFT_POINTS = 36.0
# A credits block is permitted to move only inside its existing left page, after
# every possible horizontal photo crop has been ruled out.  These are planner
# bounds, not a licence to create or resize a text frame.
MAX_CAPTION_CLEARANCE_STEP_POINTS = 10.0
# Page margins are the hard editorial safe zone.  Leave a small additional
# interior breathing room when a correction is planned so a right-aligned
# credits block never visually clings to the magenta guide itself.
SAFE_AREA_INTERIOR_POINTS = 12.0
REGISTRY_FIELDS = [
    "look_id", "spread_order", "pdf_spread", "left_filename", "right_filename",
    "indd_left_page", "indd_right_page",
]
CAPTION_FIELDS = ["look_id", "type", "brand", "price", "article"]
COMPOSITION_PLAN_FIELDS = [
    "look_id", "orientation", "left_image_filename", "right_image_filename",
    "photo_adjustment_points", "plan_status",
]
CAPTION_MAP_FIELDS = [
    "look_id", "excel_sheet", "excel_look_number", "excel_image",
    "left_filename", "right_filename", "evidence_file", "visual_status",
]
COMPOSITION_PLAN_FIELDS = [
    "look_id", "orientation", "left_image_filename", "right_image_filename",
    "photo_adjustment_points", "plan_status",
]
VISUAL_RESULT = "FULL_LEFT_CLOSE_RIGHT_CREDITS_CLEAR"
CAPTION_CLEARANCE_SCHEMA = 1
CAPTION_CLEARANCE_CLEAR = "CLEAR"
CAPTION_CLEARANCE_COLLISION = "COLLISION"
CAPTION_CLEARANCE_REVIEW = "REVIEW"
CAPTION_CLEARANCE_OVERFLOW = "OVERFLOW"
REFERENCE_ORDER_SCHEMA = 1
# The review PDF and the current-master proof use different InDesign export
# resolutions. Their page pixels are therefore not byte-identical. The 96/120
# ppi native-export regression is stable below 400 bits, while actual swapped
# full/close pages exceed 1,500 bits. Keep a conservative gap and also require
# that a rendered page is not materially closer to another expected position.
MAX_PAGE_ORDER_DHASH_DISTANCE = 512
MAX_PAGE_ORDER_TONE_DISTANCE = 30.0
PAGE_ORDER_NEAREST_MARGIN = 96
WORK_RELATIVE = Path("control") / "work"
ROOT_DIRECTORIES = {"control", "control-history", "Gender"}
ROOT_FILE_SUFFIXES = {".indd", ".pdf", ".idlk"}
ROOT_FILE_NAMES = {"desktop.ini", "Thumbs.db", ".DS_Store"}


class GateError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def fail(message: str) -> None:
    raise GateError(message)


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        fail(f"Missing required file: {path}")
    except Exception as error:
        fail(f"Cannot read JSON {path}: {error}")
    if not isinstance(value, dict):
        fail(f"JSON object expected: {path}")
    return value


def project_path(raw: str | Path) -> Path:
    path = Path(raw).expanduser().resolve()
    if not path.exists() or not path.is_dir():
        fail(f"Project folder does not exist: {path}")
    return path


def control_path(project: Path) -> Path:
    return project / "control"


def work_path(project: Path) -> Path:
    """Return the only permitted location for source and temporary build files."""
    return project / WORK_RELATIVE


def require_work_path(project: Path, path: Path, label: str) -> None:
    """Keep controlled inputs out of the delivery folder root."""
    try:
        path.resolve().relative_to(work_path(project).resolve())
    except ValueError:
        fail(f"{label} must be inside {WORK_RELATIVE}; the project root is reserved for INDD and deliverable PDFs.")


def assert_project_root_clean(project: Path) -> None:
    """Fail a gate when temporary material escaped the controlled work area."""
    unexpected: list[str] = []
    for item in project.iterdir():
        if item.is_dir() and item.name in ROOT_DIRECTORIES:
            continue
        if item.is_file() and (item.suffix.lower() in ROOT_FILE_SUFFIXES or item.name in ROOT_FILE_NAMES):
            continue
        unexpected.append(item.name)
    if unexpected:
        names = ", ".join(sorted(unexpected, key=str.lower))
        fail(
            "Project-root hygiene failed. Move temporary material into "
            f"{WORK_RELATIVE} before continuing: {names}"
        )


def install_in_design_audit_launcher() -> Path | None:
    """Install current audited tools in InDesign's user Scripts panel.

    The audit deliberately lives in the global panel, because project subfolders
    and legacy scripts have repeatedly caused agents to run a similarly named,
    stale audit that opens an unrelated file picker.
    """
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    root = Path(appdata) / "Adobe" / "InDesign"
    if not root.exists():
        return None
    panels = sorted(root.glob("Version */*/Scripts/Scripts Panel"), key=lambda item: str(item).lower())
    if not panels:
        return None
    sources = {
        "00_LOOKBOOK_GATE__RUN_CURRENT_AUDIT.jsx": "audit_next_lookbook_gate.jsx",
        "00_LOOKBOOK_GATE__INSPECT_TEMPLATE.jsx": "inspect_lookbook_template.jsx",
    }
    for destination_name, source_name in sources.items():
        source = Path(__file__).resolve().with_name(source_name)
        if not source.exists():
            fail(f"The mandatory InDesign tool source is missing: {source}")
        shutil.copy2(source, panels[-1] / destination_name)
    return panels[-1] / "00_LOOKBOOK_GATE__RUN_CURRENT_AUDIT.jsx"


def state_file(project: Path) -> Path:
    return control_path(project) / "lookbook-state.json"


def load_state(project: Path) -> dict[str, Any]:
    state = read_json(state_file(project))
    if state.get("schema") != SCHEMA or state.get("gates") != list(GATES):
        fail("Unsupported or damaged controller state. Start a new session with init --restart.")
    return state


def child_of(project: Path, raw: str | Path) -> Path:
    path = Path(raw)
    path = (project / path).resolve() if not path.is_absolute() else path.resolve()
    try:
        path.relative_to(project)
    except ValueError:
        fail(f"Path must stay inside the project folder: {path}")
    return path


def state_artifact(project: Path, state: dict[str, Any], key: str) -> Path:
    return child_of(project, state[key])


def identity(path: Path) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        fail(f"Missing file: {path}")
    stat = path.stat()
    return {
        "path": str(path.resolve()),
        "name": path.name,
        "length": stat.st_size,
        "modified_ms": int(stat.st_mtime * 1000),
    }


def same_identity(expected: dict[str, Any], current: dict[str, Any]) -> bool:
    return (
        os.path.normcase(str(expected.get("path", ""))) == os.path.normcase(str(current.get("path", "")))
        and int(expected.get("length", -1)) == int(current.get("length", -2))
        and int(expected.get("modified_ms", -1)) == int(current.get("modified_ms", -2))
    )


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def evidence_file(project: Path, gate: str) -> Path:
    return control_path(project) / "evidence" / f"{gate}.json"


def arm_file(project: Path, gate: str) -> Path:
    return control_path(project) / "arms" / f"{gate}.json"


def progress_file(project: Path, gate: str) -> Path:
    return control_path(project) / "progress" / f"{gate}.json"


def final_deliverables_file(project: Path) -> Path:
    return control_path(project) / "final-deliverables.json"


def load_evidence(project: Path, state: dict[str, Any], gate: str) -> dict[str, Any] | None:
    path = evidence_file(project, gate)
    if not path.exists():
        return None
    try:
        evidence = read_json(path)
    except GateError:
        return None
    if evidence.get("schema") != SCHEMA or evidence.get("session_id") != state["session_id"]:
        return None
    if evidence.get("gate") != gate or evidence.get("passed") is not True:
        return None
    if gate == "map":
        if any(key not in state for key in ("caption_map", "caption_workbook", "caption_provenance", "reference_pdf")):
            return None
        required = {
            "registry_sha256": state_artifact(project, state, "registry"),
            "reference_pdf_sha256": state_artifact(project, state, "reference_pdf"),
            "caption_map_sha256": state_artifact(project, state, "caption_map"),
            "caption_workbook_sha256": state_artifact(project, state, "caption_workbook"),
            "caption_data_sha256": state_artifact(project, state, "captions"),
            "caption_provenance_sha256": state_artifact(project, state, "caption_provenance"),
        }
        try:
            manifest = validate_reference_order(project, state)
            expected_confirmation_fingerprint = reference_order_confirmation_fingerprint(project, state)
        except (GateError, OSError):
            return None
        if evidence.get("reference_order_manifest_sha256") != digest(reference_order_manifest_path(project)):
            return None
        if evidence.get("reference_order_confirmation_fingerprint") != expected_confirmation_fingerprint:
            return None
        if manifest.get("registry_sha256") != digest(state_artifact(project, state, "registry")):
            return None
        return evidence if all(evidence.get(key) == digest(artifact) for key, artifact in required.items()) else None
    arm = arm_file(project, gate)
    if not arm.exists():
        return None
    armed = read_json(arm)
    if evidence.get("nonce") != armed.get("nonce"):
        return None
    # Intermediate gates intentionally survive later, controlled edits.  The
    # release audit re-checks every substantive condition against the final
    # saved master. Visual/release/PDF proof must still match that final master;
    # intermediate edits are otherwise allowed until visual inspection starts.
    if gate not in {"visual", "release", "pdf"}:
        return evidence
    if gate == "visual":
        try:
            plan = composition_plan_path(project)
            applied = composition_applied_path(project)
            proof = visual_proof_manifest_path(project)
            validate_composition_plan(project, state)
            validate_composition_applied(project, state)
            validate_visual_proof(project, state)
            clearance = validate_caption_clearance(project, state)
            validate_visual_confirmations(project, state)
            clearance_fingerprint = hashlib.sha256(
                "\n".join(f"{item['look_id']}:{_caption_clearance_item_fingerprint(item)}" for item in clearance["items"]).encode("utf-8")
            ).hexdigest()
            if (
                evidence.get("composition_plan_sha256") != digest(plan)
                or evidence.get("composition_applied_sha256") != digest(applied)
                or evidence.get("visual_proof_manifest_sha256") != digest(proof)
                or evidence.get("caption_clearance_sha256") != digest(caption_clearance_report_path(project))
                or evidence.get("caption_clearance_fingerprint") != clearance_fingerprint
                or evidence.get("visual_confirmation_fingerprint") != visual_confirmation_fingerprint(project, state)
            ):
                return None
        except (GateError, OSError):
            return None
    master = state_artifact(project, state, "master")
    reported = evidence.get("master")
    return evidence if isinstance(reported, dict) and same_identity(reported, identity(master)) else None


def passed_gates(project: Path, state: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for gate in GATES:
        if load_evidence(project, state, gate) is None:
            break
        result.append(gate)
    return result


def current_gate(project: Path, state: dict[str, Any]) -> str | None:
    passed = passed_gates(project, state)
    return GATES[len(passed)] if len(passed) < len(GATES) else None


def csv_rows(path: Path, required: list[str]) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8-sig") as source:
            reader = csv.DictReader(source, delimiter="\t")
            if reader.fieldnames != required:
                fail(f"{path.name}: columns must be exactly: {', '.join(required)}")
            rows = [{key: (value or "").strip() for key, value in row.items()} for row in reader]
    except UnicodeDecodeError as error:
        fail(f"{path.name}: UTF-8 TSV required ({error})")
    return rows


def validate_registry(path: Path, look_count: int, hires: Path) -> list[dict[str, str]]:
    rows = csv_rows(path, REGISTRY_FIELDS)
    if len(rows) != look_count:
        fail(f"Registry has {len(rows)} looks; controller expects {look_count}.")
    seen_pages: set[str] = set()
    seen_pairs: set[tuple[str, str]] = set()
    for index, row in enumerate(rows, start=1):
        look_id = f"LOOK_{index:03}"
        if row["look_id"] != look_id:
            fail(f"Registry row {index + 1}: expected {look_id}, got {row['look_id']!r}.")
        if any(not value for value in row.values()):
            fail(f"Registry {look_id}: every field must be filled before map acceptance.")
        if row["spread_order"] != str(index):
            fail(f"Registry {look_id}: spread_order must be {index}.")
        if row["pdf_spread"] != str(index):
            fail(
                f"Registry {look_id}: pdf_spread must be {index}. "
                "PDF-reference order may not be renumbered from Excel or another catalogue."
            )
        if row["left_filename"] == row["right_filename"]:
            fail(f"Registry {look_id}: left and right image files cannot be identical.")
        for filename in (row["left_filename"], row["right_filename"]):
            source = hires / filename
            if not source.exists() or not source.is_file():
                fail(f"Registry {look_id}: source image is missing from hires: {filename}")
        pair = (row["left_filename"], row["right_filename"])
        if pair in seen_pairs:
            fail(f"Registry {look_id}: duplicate image pair.")
        seen_pairs.add(pair)
        pages = (row["indd_left_page"], row["indd_right_page"])
        if not all(page.isdigit() and int(page) > 1 for page in pages):
            fail(f"Registry {look_id}: InDesign pages must be integers after the front cover.")
        expected_pages = (str(index * 2), str(index * 2 + 1))
        if pages != expected_pages:
            fail(
                f"Registry {look_id}: pages must be {expected_pages[0]} and {expected_pages[1]} "
                "in the fixed PDF-reference sequence."
            )
        if pages[0] == pages[1] or any(page in seen_pages for page in pages):
            fail(f"Registry {look_id}: InDesign pages must be globally unique.")
        seen_pages.update(pages)
    return rows


def reference_order_root(project: Path) -> Path:
    """Return the controlled evidence root for the PDF-reference sequence."""
    return work_path(project) / "reference-order"


def reference_order_manifest_path(project: Path) -> Path:
    return reference_order_root(project) / "manifest.json"


def reference_order_confirmation_dir(project: Path) -> Path:
    return control_path(project) / "reference-order" / "confirmations"


def reference_order_confirmation_path(project: Path, look_id: str) -> Path:
    return reference_order_confirmation_dir(project) / f"{look_id}.json"


def reference_order_item_fingerprint(item: dict[str, Any]) -> str:
    fields = (
        "look_id", "reference_page", "left_filename", "right_filename",
        "left_sha256", "right_sha256", "reference_page_sha256", "evidence_sha256",
    )
    if any(field not in item for field in fields):
        fail("Reference-order manifest item is missing a required binding field.")
    return hashlib.sha256("\n".join(str(item[field]) for field in fields).encode("utf-8")).hexdigest()


def reference_order_confirmation_fingerprint(project: Path, state: dict[str, Any]) -> str:
    manifest = validate_reference_order_manifest(project, state)
    rows = validate_registry(
        state_artifact(project, state, "registry"), int(state["look_count"]), state_artifact(project, state, "hires")
    )
    confirmations: list[str] = []
    manifest_sha = digest(reference_order_manifest_path(project))
    for row in rows:
        look_id = row["look_id"]
        confirmation = read_json(reference_order_confirmation_path(project, look_id))
        item = manifest["looks"].get(look_id)
        if not isinstance(item, dict):
            fail(f"{look_id}: reference-order manifest item is missing.")
        if (
            confirmation.get("schema") != REFERENCE_ORDER_SCHEMA
            or confirmation.get("session_id") != state["session_id"]
            or confirmation.get("look_id") != look_id
            or confirmation.get("reference_order_manifest_sha256") != manifest_sha
            or confirmation.get("item_fingerprint") != reference_order_item_fingerprint(item)
            or len(str(confirmation.get("note", "")).strip()) < 20
        ):
            fail(f"{look_id}: reference-order confirmation is stale, incomplete, or belongs to another mapping.")
        confirmations.append(digest(reference_order_confirmation_path(project, look_id)))
    return hashlib.sha256("\n".join(confirmations).encode("utf-8")).hexdigest()


def validate_reference_order_manifest(project: Path, state: dict[str, Any]) -> dict[str, Any]:
    """Validate the frozen PDF-reference → registered-photo evidence pack.

    The manifest is generated only by ``prepare-reference-order``.  It binds
    each page of the source PDF to the exact two hires files that will later be
    placed in InDesign, before Excel is allowed to supply any credits.
    """
    if "reference_pdf" not in state:
        fail("PDF-reference order has not been prepared. Run prepare-reference-order before accepting the map.")
    reference = state_artifact(project, state, "reference_pdf")
    require_work_path(project, reference, "PDF reference")
    if reference.suffix.lower() != ".pdf" or not reference.is_file():
        fail("The controlled PDF reference is missing or is not a PDF file.")
    manifest_path = reference_order_manifest_path(project)
    manifest = read_json(manifest_path)
    registry = state_artifact(project, state, "registry")
    rows = validate_registry(registry, int(state["look_count"]), state_artifact(project, state, "hires"))
    if (
        manifest.get("schema") != REFERENCE_ORDER_SCHEMA
        or manifest.get("generator") != "lookbook_gate.py:prepare-reference-order"
        or manifest.get("session_id") != state["session_id"]
        or manifest.get("reference_pdf_sha256") != digest(reference)
        or manifest.get("registry_sha256") != digest(registry)
        or manifest.get("reference_page_count") != len(rows) + 1
    ):
        fail("PDF-reference order manifest is missing, stale, or bound to another registry/reference PDF.")
    looks = manifest.get("looks")
    if not isinstance(looks, dict) or set(looks) != {row["look_id"] for row in rows}:
        fail("PDF-reference order manifest must contain one exact item for every required look.")
    seen_pages: set[int] = set()
    hires = state_artifact(project, state, "hires")
    for row in rows:
        look_id = row["look_id"]
        item = looks.get(look_id)
        if not isinstance(item, dict):
            fail(f"{look_id}: reference-order manifest entry is invalid.")
        reference_page = int(row["pdf_spread"]) + 1
        expected = {
            "look_id": look_id,
            "reference_page": reference_page,
            "left_filename": row["left_filename"],
            "right_filename": row["right_filename"],
            "left_sha256": digest(hires / row["left_filename"]),
            "right_sha256": digest(hires / row["right_filename"]),
        }
        if any(item.get(key) != value for key, value in expected.items()):
            fail(f"{look_id}: PDF-reference item differs from the frozen PDF-order registry.")
        if reference_page in seen_pages:
            fail(f"{look_id}: PDF-reference page is duplicated.")
        seen_pages.add(reference_page)
        for path_key, hash_key in (("reference_page_image", "reference_page_sha256"), ("evidence_image", "evidence_sha256")):
            artifact = child_of(project, str(item.get(path_key, "")))
            require_work_path(project, artifact, f"{look_id} {path_key}")
            if not artifact.is_file() or artifact.stat().st_size < 1024 or item.get(hash_key) != digest(artifact):
                fail(f"{look_id}: PDF-reference evidence is absent or has changed.")
        reference_order_item_fingerprint(item)
    if seen_pages != set(range(2, len(rows) + 2)):
        fail("PDF-reference manifest does not cover every required source look page in order.")
    return manifest


def validate_reference_order(project: Path, state: dict[str, Any]) -> dict[str, Any]:
    manifest = validate_reference_order_manifest(project, state)
    reference_order_confirmation_fingerprint(project, state)
    return manifest


def validate_captions(path: Path, look_count: int) -> int:
    rows = csv_rows(path, CAPTION_FIELDS)
    grouped: dict[str, int] = {f"LOOK_{index:03}": 0 for index in range(1, look_count + 1)}
    for row_number, row in enumerate(rows, start=2):
        if any(not value for value in row.values()):
            fail(f"Caption row {row_number}: every product field must be filled.")
        if row["look_id"] not in grouped:
            fail(f"Caption row {row_number}: unknown look ID {row['look_id']!r}.")
        grouped[row["look_id"]] += 1
    missing = [look_id for look_id, count in grouped.items() if not count]
    if missing:
        fail("Caption data has no product rows for: " + ", ".join(missing))
    return len(rows)


def validate_caption_map(project: Path, path: Path, registry: list[dict[str, str]], hires: Path, look_count: int) -> list[dict[str, str]]:
    """Require one confirmed Excel card per required PDF look; extra workbook cards are allowed."""
    rows = csv_rows(path, CAPTION_MAP_FIELDS)
    if len(rows) != look_count:
        fail(f"Caption map has {len(rows)} looks; controller expects {look_count}.")
    registry_by_id = {row["look_id"]: row for row in registry}
    evidence_roots = {child_of(project, row["evidence_file"]).parent for row in rows if row.get("evidence_file")}
    if len(evidence_roots) != 1:
        fail("Caption-map evidence files must live together in one controlled work folder.")
    manifest_path = next(iter(evidence_roots)) / "manifest.json"
    manifest = read_json(manifest_path)
    if manifest.get("schema") != 1 or manifest.get("generator") != "render_caption_mapping_evidence.py":
        fail("Caption visual evidence manifest is missing or was not created by the required renderer.")
    manifest_items = manifest.get("items")
    if not isinstance(manifest_items, dict):
        fail("Caption visual evidence manifest has no item list.")
    seen_excel: set[tuple[str, str]] = set()
    for index, row in enumerate(rows, start=1):
        expected_id = f"LOOK_{index:03}"
        if row["look_id"] != expected_id or row["look_id"] not in registry_by_id:
            fail(f"Caption map row {index + 1}: expected {expected_id}.")
        source = registry_by_id[row["look_id"]]
        # The image pair belongs to the frozen layout registry.  The Excel row
        # deliberately does not: it is authoritative only in the separately
        # reviewed caption map.  A prior generator hard-coded W/M row numbers
        # in this registry, which silently turned a guessed order into credits.
        for key in ("left_filename", "right_filename"):
            if row[key] != source[key]:
                fail(f"{expected_id}: caption map {key} differs from the frozen look registry.")
        if row["excel_sheet"] not in {"W", "M"}:
            fail(f"{expected_id}: excel_sheet must be W or M.")
        if not row["excel_look_number"].isdigit() or int(row["excel_look_number"]) < 1:
            fail(f"{expected_id}: excel_look_number must be a positive integer.")
        if row["visual_status"] != "CONFIRMED":
            fail(f"{expected_id}: visual_status must be CONFIRMED after comparing the Excel image with both placed JPGs.")
        excel_key = (row["excel_sheet"], row["excel_look_number"])
        if excel_key in seen_excel:
            fail(f"{expected_id}: Excel look {excel_key[0]} {excel_key[1]} is mapped more than once.")
        seen_excel.add(excel_key)
        image = child_of(project, row["excel_image"])
        proof = child_of(project, row["evidence_file"])
        if not image.is_file() or image.stat().st_size < 1024:
            fail(f"{expected_id}: mapped embedded Excel image is missing or empty: {image}")
        expected_prefix = f"{row['excel_sheet']}_{int(row['excel_look_number']):03}_"
        if not image.name.startswith(expected_prefix):
            fail(f"{expected_id}: excel_image must identify its exact sheet and look number ({expected_prefix}...).")
        if not proof.is_file() or proof.stat().st_size < 1024:
            fail(f"{expected_id}: visual pair evidence is missing or empty: {proof}")
        item = manifest_items.get(expected_id)
        if not isinstance(item, dict):
            fail(f"{expected_id}: visual evidence manifest has no entry.")
        expected_hashes = {
            "left_sha256": digest(hires / row["left_filename"]),
            "right_sha256": digest(hires / row["right_filename"]),
            "excel_sha256": digest(image),
            "evidence_sha256": digest(proof),
        }
        if any(item.get(key) != value for key, value in expected_hashes.items()):
            fail(f"{expected_id}: visual evidence does not match the mapped Excel image and frozen photo pair.")
    return rows


def format_price(value: Any) -> str:
    if isinstance(value, bool) or value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        number = int(value) if float(value).is_integer() else value
        if isinstance(number, int):
            return f"{number:,}".replace(",", " ") + " ₽"
    return str(value).strip()


def workbook_caption_rows(workbook: Path, caption_map: list[dict[str, str]]) -> list[dict[str, str]]:
    try:
        from openpyxl import load_workbook
    except Exception as error:
        fail(f"Workbook reader is unavailable: {error}")
    try:
        book = load_workbook(workbook, data_only=True, read_only=False)
    except Exception as error:
        fail(f"Cannot read caption workbook {workbook.name}: {error}")
    grouped: dict[tuple[str, str], list[tuple[str, str, str, str]]] = {}
    try:
        for sheet in ("W", "M"):
            if sheet not in book.sheetnames:
                fail(f"Caption workbook is missing required sheet: {sheet}")
            current: str | None = None
            for values in book[sheet].iter_rows(min_row=2, values_only=True):
                number, kind, brand, price, article = (list(values) + [None] * 6)[1:6]
                if number not in (None, "") and str(number).strip().isdigit():
                    current = str(int(float(number)))
                if current and all(value not in (None, "") for value in (kind, brand, price, article)):
                    grouped.setdefault((sheet, current), []).append((str(kind).strip(), str(brand).strip(), format_price(price), str(article).strip()))
    finally:
        book.close()
    output: list[dict[str, str]] = []
    for mapped in caption_map:
        key = (mapped["excel_sheet"], mapped["excel_look_number"])
        products = grouped.get(key, [])
        if not products:
            fail(f"{mapped['look_id']}: workbook has no complete product rows for Excel look {key[0]} {key[1]}.")
        for kind, brand, price, article in products:
            output.append({"look_id": mapped["look_id"], "type": kind, "brand": brand, "price": price, "article": article})
    return output


def validate_verified_caption_inputs(project: Path, state: dict[str, Any], registry: list[dict[str, str]] | None = None) -> int:
    required = ("caption_map", "caption_workbook", "caption_provenance")
    if any(key not in state for key in required):
        fail("This session predates verified caption mapping. Start a new build from the prepared template; legacy captions are not certifiable.")
    registry_rows = registry or validate_registry(
        state_artifact(project, state, "registry"), int(state["look_count"]), state_artifact(project, state, "hires")
    )
    mapping = validate_caption_map(project, state_artifact(project, state, "caption_map"), registry_rows, state_artifact(project, state, "hires"), int(state["look_count"]))
    expected = workbook_caption_rows(state_artifact(project, state, "caption_workbook"), mapping)
    actual = csv_rows(state_artifact(project, state, "captions"), CAPTION_FIELDS)
    if actual != expected:
        fail("caption-data.tsv is not the exact product data derived from the visually confirmed caption map and Excel workbook.")
    provenance = read_json(state_artifact(project, state, "caption_provenance"))
    expected_hashes = {
        "caption_map_sha256": digest(state_artifact(project, state, "caption_map")),
        "caption_workbook_sha256": digest(state_artifact(project, state, "caption_workbook")),
        "caption_data_sha256": digest(state_artifact(project, state, "captions")),
    }
    if any(provenance.get(key) != value for key, value in expected_hashes.items()):
        fail("Caption provenance does not match the current map, workbook, and caption data.")
    if provenance.get("look_count") != int(state["look_count"]) or provenance.get("caption_rows") != len(actual):
        fail("Caption provenance has an unexpected look or product-row count.")
    return len(actual)


def composition_plan_path(project: Path) -> Path:
    return control_path(project) / "visual" / "composition-plan.tsv"


def composition_applied_path(project: Path) -> Path:
    return control_path(project) / "visual" / "composition-applied.json"


def visual_proof_dir(project: Path) -> Path:
    return control_path(project) / "visual" / "proof"


def visual_proof_manifest_path(project: Path) -> Path:
    return visual_proof_dir(project) / "manifest.json"


def visual_proof_export_run_path(project: Path) -> Path:
    """Durable ownership marker for the long native visual-proof export."""
    return visual_proof_dir(project) / "export-in-progress.json"


def visual_confirmation_dir(project: Path) -> Path:
    return control_path(project) / "visual" / "confirmations"


def caption_clearance_layout_path(project: Path) -> Path:
    """Read-only InDesign coordinate snapshot for the credit-clearance audit."""
    return control_path(project) / "visual" / "caption-clearance-layout.json"


def caption_clearance_report_path(project: Path) -> Path:
    """The hash-bound computer-vision clearance decision for every look."""
    return control_path(project) / "visual" / "caption-clearance.json"


def clearance_correction_plan_path(project: Path) -> Path:
    """The signed visual retry plan, including any permitted credits move."""
    return control_path(project) / "visual" / "clearance-correction-plan.json"


def caption_clearance_proof_dir(project: Path) -> Path:
    return control_path(project) / "visual" / "caption-clearance"


def validate_composition_plan(project: Path, state: dict[str, Any]) -> list[dict[str, str]]:
    """Validate a correction plan; it is not itself a visual confirmation."""
    path = composition_plan_path(project)
    if not path.exists():
        fail(f"Missing required composition plan: {path}")
    rows = csv_rows(path, COMPOSITION_PLAN_FIELDS)
    expected_count = int(state["look_count"])
    registry = validate_registry(state_artifact(project, state, "registry"), expected_count, state_artifact(project, state, "hires"))
    if len(rows) != expected_count:
        fail(f"Composition plan has {len(rows)} looks; controller expects {expected_count}.")
    for index, row in enumerate(rows, start=1):
        look_id = f"LOOK_{index:03}"
        if row["look_id"] != look_id:
            fail(f"Composition plan row {index + 1}: expected {look_id}.")
        if row["orientation"] != "FULL_LEFT_CLOSE_RIGHT":
            fail(f"{look_id}: left frame must be full-length and right frame must be the close-up.")
        registered = registry[index - 1]
        actual_pair = (row["left_image_filename"], row["right_image_filename"])
        allowed_pairs = {
            (registered["left_filename"], registered["right_filename"]),
            (registered["right_filename"], registered["left_filename"]),
        }
        if actual_pair not in allowed_pairs:
            fail(f"{look_id}: plan must name the two registry images in their intended fixed left/right containers.")
        if row["plan_status"] != "READY":
            fail(f"{look_id}: composition plan is not READY for the controlled native correction.")
        try:
            shift = float(row["photo_adjustment_points"])
        except ValueError:
            fail(f"{look_id}: photo_adjustment_points must be a number of horizontal points.")
        if not math.isfinite(shift):
            fail(f"{look_id}: photo_adjustment_points must be finite.")
        if abs(shift) > MAX_CLEARANCE_SHIFT_POINTS:
            fail(f"{look_id}: photo_adjustment_points exceeds the 36-pt centred-crop limit.")
    return rows


def _caption_corrections_for_current_plan(project: Path, state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Read the optional, exact existing-frame moves from a visual retry plan.

    A correction is intentionally separate from the image TSV: photo crops are
    source substitutions inside a fixed image container, whereas a credits move
    is an exceptional, explicitly recorded change to the *same* text frame.
    """
    path = clearance_correction_plan_path(project)
    if not path.exists():
        return {}
    data = read_json(path)
    if (
        data.get("schema") != SCHEMA
        or data.get("generator") != "lookbook_gate.py:plan-clearance-corrections"
        or data.get("session_id") != state["session_id"]
    ):
        fail("Caption-clearance correction plan is not bound to this visual session.")
    raw = data.get("caption_corrections", [])
    if not isinstance(raw, list):
        fail("Caption-clearance correction plan has invalid caption_corrections.")
    expected = {f"LOOK_{index:03}" for index in range(1, int(state["look_count"]) + 1)}
    by_look: dict[str, dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict):
            fail("Caption-clearance correction plan contains a non-object correction.")
        look_id = item.get("look_id")
        mode = item.get("mode")
        alignment = item.get("paragraph_alignment")
        if not isinstance(look_id, str) or look_id not in expected or look_id in by_look:
            fail("Caption-clearance correction plan has an invalid or duplicate look.")
        if mode not in {"DOWN", "RIGHT"}:
            fail(f"{look_id}: caption correction mode must be DOWN or RIGHT.")
        if (mode == "DOWN" and alignment != "STYLE") or (mode == "RIGHT" and alignment != "RIGHT_ALIGN"):
            fail(f"{look_id}: caption correction alignment does not match its mode.")
        before = _finite_bounds(item.get("from_frame_bounds"), f"{look_id} from_frame_bounds")
        after = _finite_bounds(item.get("to_frame_bounds"), f"{look_id} to_frame_bounds")
        prior_raw = item.get("prior_frame_bounds")
        prior = None if prior_raw is None else _finite_bounds(prior_raw, f"{look_id} prior_frame_bounds")
        if after[2] <= after[0] or after[3] <= after[1]:
            fail(f"{look_id}: caption correction has non-positive target bounds.")
        delta_x = after[1] - before[1]
        delta_y = after[0] - before[0]
        if mode == "DOWN" and (abs(delta_x) > 0.05 or delta_y <= 0.05):
            fail(f"{look_id}: DOWN correction may move only the existing frame downward.")
        if mode == "RIGHT" and (delta_x <= 0.05 or delta_y < -0.05):
            fail(f"{look_id}: RIGHT correction may move the existing frame only rightward and, if required, downward.")
        by_look[look_id] = {
            "look_id": look_id,
            "mode": mode,
            "paragraph_alignment": alignment,
            "from_frame_bounds": before,
            "to_frame_bounds": after,
            "prior_frame_bounds": prior,
        }
    return by_look


def _finite_bounds(value: Any, label: str) -> list[float]:
    if not isinstance(value, list) or len(value) != 4:
        fail(f"{label} must contain four graphic bounds.")
    try:
        bounds = [float(item) for item in value]
    except (TypeError, ValueError):
        fail(f"{label} must contain numeric graphic bounds.")
    if any(not math.isfinite(item) for item in bounds):
        fail(f"{label} contains non-finite graphic bounds.")
    return bounds


def validate_composition_applied(project: Path, state: dict[str, Any]) -> dict[str, Any]:
    """Require native evidence for every source replacement and horizontal crop shift."""
    plan = composition_plan_path(project)
    rows = validate_composition_plan(project, state)
    path = composition_applied_path(project)
    evidence = read_json(path)
    if evidence.get("schema") != SCHEMA or evidence.get("generator") not in {
        "run_lookbook_gate_com.ps1:ApplyComposition",
        "run_lookbook_gate_com.ps1:ApplyCompositionDelta",
    }:
        fail("Composition application evidence was not created by the native InDesign worker.")
    if evidence.get("session_id") != state["session_id"] or evidence.get("nonce") != read_json(arm_file(project, "visual")).get("nonce"):
        fail("Composition application evidence does not belong to the armed visual session.")
    master = state_artifact(project, state, "master")
    if not isinstance(evidence.get("master"), dict) or not same_identity(evidence["master"], identity(master)):
        fail("Composition application evidence belongs to a different saved master.")
    if evidence.get("composition_plan_sha256") != digest(plan):
        fail("Composition application evidence does not match the current correction plan.")
    caption_corrections = _caption_corrections_for_current_plan(project, state)
    correction_path = clearance_correction_plan_path(project)
    expected_correction_hash = digest(correction_path) if correction_path.exists() else None
    if evidence.get("clearance_correction_plan_sha256") != expected_correction_hash:
        fail("Composition application evidence does not match the current caption-clearance correction plan.")
    items = evidence.get("items")
    if not isinstance(items, list) or len(items) != len(rows):
        fail("Composition application evidence does not cover every required look.")
    for index, (row, item) in enumerate(zip(rows, items), start=1):
        look_id = f"LOOK_{index:03}"
        if not isinstance(item, dict) or item.get("look_id") != look_id:
            fail(f"{look_id}: native composition evidence is missing or out of order.")
        if item.get("left_image_filename") != row["left_image_filename"] or item.get("right_image_filename") != row["right_image_filename"]:
            fail(f"{look_id}: native composition evidence names different image sources than the correction plan.")
        try:
            planned = float(row["photo_adjustment_points"])
            recorded = float(item.get("planned_shift_points"))
        except (TypeError, ValueError):
            fail(f"{look_id}: native composition evidence has an invalid horizontal adjustment.")
        if not math.isfinite(recorded) or abs(planned - recorded) > 0.01:
            fail(f"{look_id}: native composition evidence does not match the planned horizontal adjustment.")
        before = _finite_bounds(item.get("before_left_graphic_bounds"), f"{look_id} before_left_graphic_bounds")
        after = _finite_bounds(item.get("after_left_graphic_bounds"), f"{look_id} after_left_graphic_bounds")
        if abs(before[0] - after[0]) > 0.01 or abs(before[2] - after[2]) > 0.01:
            fail(f"{look_id}: a crop correction changed vertical graphic bounds.")
        if abs((after[1] - before[1]) - planned) > 0.1 or abs((after[3] - before[3]) - planned) > 0.1:
            fail(f"{look_id}: native horizontal crop shift was not actually applied.")
        recorded_caption = item.get("caption_correction")
        expected_caption = caption_corrections.get(look_id)
        if expected_caption is None:
            if recorded_caption not in (None, {}):
                fail(f"{look_id}: native composition evidence contains an unplanned credits-frame correction.")
        else:
            if not isinstance(recorded_caption, dict):
                fail(f"{look_id}: native composition evidence is missing its planned credits-frame correction.")
            if (
                recorded_caption.get("mode") != expected_caption["mode"]
                or recorded_caption.get("paragraph_alignment") != expected_caption["paragraph_alignment"]
                or any(abs(a - b) > 0.5 for a, b in zip(
                    _finite_bounds(recorded_caption.get("after_frame_bounds"), f"{look_id} after_frame_bounds"),
                    expected_caption["to_frame_bounds"],
                ))
            ):
                fail(f"{look_id}: native credits-frame correction differs from the signed visual plan.")
    return evidence


def _relative_project_path(project: Path, path: Path) -> str:
    return str(path.resolve().relative_to(project)).replace("\\", "/")


def validate_visual_proof(project: Path, state: dict[str, Any]) -> dict[str, Any]:
    """Bind every semantic confirmation to a current-master, native-rendered pair image."""
    plan = composition_plan_path(project)
    applied = composition_applied_path(project)
    manifest_path = visual_proof_manifest_path(project)
    manifest = read_json(manifest_path)
    if manifest.get("schema") != SCHEMA or manifest.get("generator") != "lookbook_gate.py:render-visual-proof":
        fail("Visual proof manifest was not generated from the current native InDesign PDF proof.")
    if manifest.get("session_id") != state["session_id"]:
        fail("Visual proof manifest belongs to a different session.")
    master = state_artifact(project, state, "master")
    if not isinstance(manifest.get("master"), dict) or not same_identity(manifest["master"], identity(master)):
        fail("Visual proof was not rendered from the current saved master.")
    if manifest.get("composition_plan_sha256") != digest(plan) or manifest.get("composition_applied_sha256") != digest(applied):
        fail("Visual proof was rendered before the current composition correction was applied.")
    proof_pdf = child_of(project, manifest.get("proof_pdf", ""))
    if (
        not proof_pdf.is_file()
        or proof_pdf.stat().st_size < 512
        or manifest.get("proof_pdf_sha256") != digest(proof_pdf)
        or manifest.get("expected_pages") != int(state["expected_pages"])
    ):
        fail("Native visual-proof PDF is missing or has changed.")
    pairs = manifest.get("look_pairs")
    registry = validate_registry(state_artifact(project, state, "registry"), int(state["look_count"]), state_artifact(project, state, "hires"))
    if not isinstance(pairs, dict) or set(pairs) != {row["look_id"] for row in registry}:
        fail("Visual proof manifest does not contain exactly one current rendered pair for each look.")
    for row in registry:
        look_id = row["look_id"]
        item = pairs.get(look_id)
        if not isinstance(item, dict):
            fail(f"{look_id}: visual proof pair is missing.")
        if item.get("left_page") != int(row["indd_left_page"]) or item.get("right_page") != int(row["indd_right_page"]):
            fail(f"{look_id}: visual proof does not use its exact InDesign page pair.")
        image = child_of(project, item.get("image", ""))
        if not image.is_file() or image.stat().st_size < 1024 or item.get("sha256") != digest(image):
            fail(f"{look_id}: current rendered visual pair is missing or changed.")
    overview = manifest.get("overview")
    if not isinstance(overview, list) or len(overview) != 5:
        fail("Visual proof is missing its five current-master overview renders.")
    for item in overview:
        if not isinstance(item, dict):
            fail("Visual proof overview entry is malformed.")
        image = child_of(project, item.get("image", ""))
        if not image.is_file() or image.stat().st_size < 1024 or item.get("sha256") != digest(image):
            fail("Visual proof overview render is missing or changed.")
    return manifest


def _caption_clearance_item_fingerprint(item: dict[str, Any]) -> str:
    """Stable per-look binding used by the later human visual confirmation."""
    encoded = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_caption_clearance_layout(project: Path, state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Accept only a current, read-only native coordinate snapshot."""
    layout = read_json(caption_clearance_layout_path(project))
    master = state_artifact(project, state, "master")
    registry_path = state_artifact(project, state, "registry")
    captions_path = state_artifact(project, state, "captions")
    if (
        layout.get("schema") != CAPTION_CLEARANCE_SCHEMA
        or layout.get("generator") != "run_lookbook_gate_com.ps1:AuditCaptionClearance"
        or layout.get("session_id") != state["session_id"]
        or not isinstance(layout.get("master"), dict)
        or not same_identity(layout["master"], identity(master))
        or layout.get("registry_sha256") != digest(registry_path)
        or layout.get("captions_sha256") != digest(captions_path)
    ):
        fail("Caption-clearance layout was not read from the current saved InDesign master.")
    registry = validate_registry(registry_path, int(state["look_count"]), state_artifact(project, state, "hires"))
    raw_items = layout.get("items")
    if not isinstance(raw_items, list) or len(raw_items) != len(registry):
        fail("Caption-clearance layout does not cover every required look.")
    items: dict[str, dict[str, Any]] = {}
    for row, item in zip(registry, raw_items):
        look_id = row["look_id"]
        if not isinstance(item, dict) or item.get("look_id") != look_id:
            fail(f"{look_id}: caption-clearance layout is missing or out of order.")
        if item.get("left_page") != int(row["indd_left_page"]) or item.get("source_filename") != row["left_filename"]:
            fail(f"{look_id}: caption-clearance layout does not describe the registered left photograph.")
        if not isinstance(item.get("credits_overflow"), bool):
            fail(f"{look_id}: caption-clearance layout does not state whether the fixed credits frame overflows.")
        graphic = _finite_bounds(item.get("graphic_bounds"), f"{look_id} graphic_bounds")
        caption = _finite_bounds(item.get("caption_bounds"), f"{look_id} caption_bounds")
        page = _finite_bounds(item.get("page_bounds"), f"{look_id} page_bounds")
        safe = _finite_bounds(item.get("safe_bounds"), f"{look_id} safe_bounds")
        if graphic[2] <= graphic[0] or graphic[3] <= graphic[1] or caption[2] <= caption[0] or caption[3] <= caption[1] or page[2] <= page[0] or page[3] <= page[1] or safe[2] <= safe[0] or safe[3] <= safe[1]:
            fail(f"{look_id}: caption-clearance layout has non-positive bounds.")
        if safe[0] < page[0] - 0.05 or safe[1] < page[1] - 0.05 or safe[2] > page[2] + 0.05 or safe[3] > page[3] + 0.05:
            fail(f"{look_id}: safe area is outside its parent page.")
        for name in ("graphic_rotation", "caption_rotation"):
            try:
                rotation = float(item.get(name))
            except (TypeError, ValueError):
                fail(f"{look_id}: caption-clearance layout has an invalid {name}.")
            if not math.isfinite(rotation) or abs(rotation) > 0.01:
                fail(f"{look_id}: rotated images or credits frames require a manual-safe template, not an inferred clearance check.")
        items[look_id] = item
    return layout, items


def _smooth_background_profile(profile: Any, np: Any) -> Any:
    """Smooth a per-row studio-background sample without a CV dependency."""
    height = int(profile.shape[0])
    # The studio sweep changes slowly down the image.  The window is deliberately
    # much smaller than a person, so it keeps genuine vertical lighting changes
    # while suppressing JPEG noise and accidental edge pixels.
    window = max(25, min(161, (height // 120) | 1))
    if window % 2 == 0:
        window += 1
    pad = window // 2
    padded = np.pad(profile.astype(np.float32), ((pad, pad), (0, 0)), mode="edge")
    cumulative = np.concatenate((np.zeros((1, 3), dtype=np.float32), np.cumsum(padded, axis=0)), axis=0)
    return (cumulative[window:] - cumulative[:-window]) / float(window)


def _dominant_studio_background(image: Any, np: Any) -> tuple[Any, float]:
    """Build a local studio-background model from both safe image edges.

    A single global grey value falsely marks a normal studio gradient as a
    garment (for example, pale upper backgrounds versus a darker floor).  The
    approved source format has background at one or both outer edges of every
    full-length image, so the detector samples those edges per row and linearly
    interpolates only the expected background under the credits.  It remains a
    conservative source-pixel check: any material that differs from that local
    surface still blocks the visual gate.
    """
    height, width = image.shape[:2]
    if height < 32 or width < 32:
        fail("Caption-clearance audit received an unusably small source image.")
    edge_width = max(24, min(width // 5, 360))
    # The bright 85th percentile rejects dark bags/garments that enter an edge
    # strip, while retaining the neutral studio background seen around them.
    left_profile = np.percentile(image[:, :edge_width].astype(np.float32), 85, axis=1)
    right_profile = np.percentile(image[:, width - edge_width:].astype(np.float32), 85, axis=1)
    left_profile = _smooth_background_profile(left_profile, np)
    right_profile = _smooth_background_profile(right_profile, np)
    # The model must absorb normal JPEG texture but not pale clothes.  A fixed,
    # documented allowance is safer than deriving it from the whole photograph,
    # whose dominant colour may be a garment rather than the studio sweep.
    return ({"left": left_profile, "right": right_profile, "width": int(width)}, 18.0)


def _local_studio_background(background: Any, x0: int, x1: int, y0: int, y1: int, np: Any) -> Any:
    """Return the expected RGB studio surface for one source-pixel rectangle."""
    if not isinstance(background, dict):
        return background.reshape((1, 1, 3))
    width = max(1, int(background["width"]) - 1)
    rows_left = background["left"][y0:y1].astype(np.float32)
    rows_right = background["right"][y0:y1].astype(np.float32)
    positions = np.arange(x0, x1, dtype=np.float32) / float(width)
    return rows_left[:, None, :] * (1.0 - positions[None, :, None]) + rows_right[:, None, :] * positions[None, :, None]


def _load_caption_clearance_source(source: Path, np: Any, Image: Any, ImageOps: Any) -> Any:
    """Load one full-length source once; all credit blocks reuse its pixels."""
    try:
        with Image.open(source) as opened:
            return np.asarray(ImageOps.exif_transpose(opened).convert("RGB")).copy()
    except Exception as error:
        fail(f"Cannot inspect caption-clearance source {source.name}: {error}")


def _morphology(mask: Any, np: Any) -> Any:
    """Remove isolated JPEG/texture pixels without an undeclared CV package."""
    height, width = mask.shape
    padded = np.pad(mask.astype(bool), 1, mode="constant", constant_values=False)
    neighborhoods = [padded[row:row + height, column:column + width] for row in range(3) for column in range(3)]
    # Opening (erode, then dilate) removes one-pixel JPEG speckle.  The former
    # implementation accidentally performed closing, which preserves those
    # dots and can make a clean graded backdrop look like a collision.
    eroded = np.logical_and.reduce(neighborhoods)
    padded = np.pad(eroded, 1, mode="constant", constant_values=False)
    neighborhoods = [padded[row:row + height, column:column + width] for row in range(3) for column in range(3)]
    return np.logical_or.reduce(neighborhoods)


def _caption_clearance_metrics_from_image(image: Any, item: dict[str, Any], background: Any, tolerance: float, np: Any) -> tuple[dict[str, Any], Any, Any]:
    """Inspect one visible credits block in a preloaded original photograph.

    Unlike a PDF-only image test, this intentionally has no glyphs to mask:
    the placed source photo is mapped through its actual InDesign graphic bounds
    and sampled directly.  Any figure under the credits remains visible to the
    detector.
    """
    height, width = image.shape[:2]
    graphic = _finite_bounds(item.get("graphic_bounds"), f"{item.get('look_id', 'look')} graphic_bounds")
    caption = _finite_bounds(item.get("caption_bounds"), f"{item.get('look_id', 'look')} caption_bounds")
    top, left, bottom, right = graphic
    caption_top, caption_left, caption_bottom, caption_right = caption
    graphic_width = right - left
    graphic_height = bottom - top
    # The crop must sit inside the image's visible page-space extent.  If it
    # does not, a detector cannot prove a clean background and must block.
    if caption_left < left - 0.05 or caption_right > right + 0.05 or caption_top < top - 0.05 or caption_bottom > bottom + 0.05:
        return ({"status": CAPTION_CLEARANCE_REVIEW, "reason": "credits frame is outside the mapped left graphic", "coverage": 1.0, "largest_component": 1.0}, image, np.zeros((1, 1), dtype=np.uint8))
    x0 = max(0, int(math.floor((caption_left - left) * width / graphic_width)))
    x1 = min(width, int(math.ceil((caption_right - left) * width / graphic_width)))
    y0 = max(0, int(math.floor((caption_top - top) * height / graphic_height)))
    y1 = min(height, int(math.ceil((caption_bottom - top) * height / graphic_height)))
    if x1 - x0 < 12 or y1 - y0 < 12:
        return ({"status": CAPTION_CLEARANCE_REVIEW, "reason": "mapped credits area is too small to inspect", "coverage": 1.0, "largest_component": 1.0}, image, np.zeros((1, 1), dtype=np.uint8))
    crop = image[y0:y1, x0:x1]
    expected_background = _local_studio_background(background, x0, x1, y0, y1, np)
    distance = np.linalg.norm(crop.astype(np.float32) - expected_background, axis=2)
    gray = crop.astype(np.float32).dot(np.array([0.299, 0.587, 0.114], dtype=np.float32))
    vertical, horizontal = np.gradient(gray)
    gradient = np.hypot(horizontal, vertical)
    colour_foreground = distance > tolerance
    # Pale clothing sometimes resembles the white background chromatically;
    # its material edges still distinguish it.  Edge evidence is intentionally
    # allowed only where the pixel is at least mildly distinct from background.
    edge_foreground = (gradient > max(34.0, tolerance * 1.25)) & (distance > tolerance * 0.32)
    retained = _morphology(colour_foreground | edge_foreground, np)
    area = retained.shape[0] * retained.shape[1]
    coverage = float(retained.mean())
    # A PASS is deliberately reserved for a uniformly background-like block.
    # Coverage is a safer criterion than a heuristic external CV dependency.
    largest_ratio = coverage
    if coverage >= 0.015 or largest_ratio >= 0.006:
        status = CAPTION_CLEARANCE_COLLISION
        reason = "model or garment pixels occupy the credits rectangle"
    elif coverage >= 0.002 or largest_ratio >= 0.0008:
        status = CAPTION_CLEARANCE_REVIEW
        reason = "credits rectangle is not provably empty of the model"
    else:
        status = CAPTION_CLEARANCE_CLEAR
        reason = "credits rectangle contains only the studio background"
    return ({
        "status": status, "reason": reason, "coverage": round(coverage, 6), "largest_component": round(largest_ratio, 6),
        "background_rgb": [round(float(value), 2) for value in np.median(expected_background, axis=(0, 1))], "background_tolerance": round(tolerance, 2),
        "source_pixel_rect": [x0, y0, x1, y1],
    }, crop, retained)


def _caption_clearance_metrics(source: Path, item: dict[str, Any], np: Any, Image: Any, ImageOps: Any) -> tuple[dict[str, Any], Any, Any]:
    """Compatibility wrapper for isolated diagnostic calls."""
    image = _load_caption_clearance_source(source, np, Image, ImageOps)
    background, tolerance = _dominant_studio_background(image, np)
    return _caption_clearance_metrics_from_image(image, item, background, tolerance, np)


def _fast_caption_clearance_evaluator(image: Any, background: Any, tolerance: float, np: Any):
    """Build one source-pixel foreground mask for many candidate positions.

    The audit itself renders an overlay for one final rectangle.  A planner may
    test hundreds of rectangles on the same source, so recomputing colour,
    gradient and morphology for each one was needlessly quadratic.  This cache
    uses the same source-resolution criteria once, then counts mask pixels in a
    candidate rectangle through an integral image.  Whole-image morphology is
    at least as conservative at a candidate edge as a cropped calculation.
    """
    height, width = image.shape[:2]
    gray = image.astype(np.float32).dot(np.array([0.299, 0.587, 0.114], dtype=np.float32))
    vertical = np.zeros_like(gray)
    horizontal = np.zeros_like(gray)
    vertical[1:, :] = np.abs(gray[1:, :] - gray[:-1, :])
    horizontal[:, 1:] = np.abs(gray[:, 1:] - gray[:, :-1])
    np.maximum(vertical, horizontal, out=vertical)
    del horizontal, gray
    foreground = np.zeros((height, width), dtype=bool)
    # Work in narrow source strips to keep peak memory bounded even for large
    # hires photographs; no thumbnail or proxy is used.
    for x0 in range(0, width, 384):
        x1 = min(width, x0 + 384)
        crop = image[:, x0:x1].astype(np.float32)
        expected = _local_studio_background(background, x0, x1, 0, height, np)
        distance = np.linalg.norm(crop - expected, axis=2)
        foreground[:, x0:x1] = (distance > tolerance) | ((vertical[:, x0:x1] > max(34.0, tolerance * 1.25)) & (distance > tolerance * 0.32))
        del crop, expected, distance
    del vertical
    retained = _morphology(foreground, np)
    del foreground
    integral = np.zeros((height + 1, width + 1), dtype=np.int32)
    integral[1:, 1:] = retained.astype(np.int32)
    del retained
    np.cumsum(integral, axis=0, dtype=np.int32, out=integral)
    np.cumsum(integral, axis=1, dtype=np.int32, out=integral)

    def evaluate(graphic_bounds: list[float], caption_bounds: list[float]) -> dict[str, Any]:
        top, left, bottom, right = graphic_bounds
        caption_top, caption_left, caption_bottom, caption_right = caption_bounds
        graphic_width = right - left
        graphic_height = bottom - top
        if caption_left < left - 0.05 or caption_right > right + 0.05 or caption_top < top - 0.05 or caption_bottom > bottom + 0.05:
            return {"status": CAPTION_CLEARANCE_REVIEW, "reason": "credits frame is outside the mapped left graphic", "coverage": 1.0, "largest_component": 1.0}
        x0 = max(0, int(math.floor((caption_left - left) * width / graphic_width)))
        x1 = min(width, int(math.ceil((caption_right - left) * width / graphic_width)))
        y0 = max(0, int(math.floor((caption_top - top) * height / graphic_height)))
        y1 = min(height, int(math.ceil((caption_bottom - top) * height / graphic_height)))
        if x1 - x0 < 12 or y1 - y0 < 12:
            return {"status": CAPTION_CLEARANCE_REVIEW, "reason": "mapped credits area is too small to inspect", "coverage": 1.0, "largest_component": 1.0}
        occupied = int(integral[y1, x1] - integral[y0, x1] - integral[y1, x0] + integral[y0, x0])
        coverage = occupied / float((x1 - x0) * (y1 - y0))
        if coverage >= 0.015 or coverage >= 0.006:
            status = CAPTION_CLEARANCE_COLLISION
            reason = "model or garment pixels occupy the credits rectangle"
        elif coverage >= 0.002 or coverage >= 0.0008:
            status = CAPTION_CLEARANCE_REVIEW
            reason = "credits rectangle is not provably empty of the model"
        else:
            status = CAPTION_CLEARANCE_CLEAR
            reason = "credits rectangle contains only the studio background"
        return {"status": status, "reason": reason, "coverage": round(coverage, 6), "largest_component": round(coverage, 6)}

    return evaluate


def _write_caption_clearance_overlay(crop: Any, mask: Any, status: str, destination: Path, np: Any, Image: Any, ImageDraw: Any) -> None:
    """Keep a visible, per-look proof of the pixels used for the decision."""
    if crop.size == 0:
        fail(f"Cannot write an empty caption-clearance proof: {destination}")
    preview = crop.copy()
    red = np.array([235, 55, 55], dtype=np.uint8)
    if mask.shape == preview.shape[:2]:
        preview[mask.astype(bool)] = (0.38 * preview[mask.astype(bool)] + 0.62 * red).astype(np.uint8)
    border = (35, 150, 70) if status == CAPTION_CLEARANCE_CLEAR else (235, 55, 55)
    thickness = max(2, min(preview.shape[0], preview.shape[1]) // 180)
    rendered = Image.fromarray(preview, "RGB")
    ImageDraw.Draw(rendered).rectangle((0, 0, rendered.width - 1, rendered.height - 1), outline=border, width=thickness)
    scale = min(1.0, 900.0 / max(preview.shape[0], preview.shape[1]))
    if scale < 1.0:
        rendered = rendered.resize((max(1, round(rendered.width * scale)), max(1, round(rendered.height * scale))), Image.Resampling.LANCZOS)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        rendered.save(destination, "PNG")
    except Exception as error:
        fail(f"Cannot write caption-clearance proof image {destination}: {error}")


def _visible_caption_text_bounds(reader: Any, page_number: int, caption_bounds: list[float]) -> tuple[list[float], int]:
    """Read the actual printed credit rows and return their proof-page bounds.

    Pypdf exposes text baselines through the visitor callback.  On these
    unrotated look pages the PDF bottom-origin baseline maps directly to the
    InDesign top-origin page coordinate.  The fixed credits frame can contain
    deliberate empty space, so collision detection must use these printed rows
    rather than treating the entire container as visible typography.
    """
    try:
        page = reader.pages[page_number - 1]
        page_height = float(page.mediabox.top) - float(page.mediabox.bottom)
        top, left, bottom, right = caption_bounds
        baselines: list[float] = []
        glyph_lefts: list[float] = []
        glyph_rights: list[float] = []

        def visit(text: str, cm: Any, tm: Any, _font: Any, _font_size: Any) -> None:
            if not text.strip():
                return
            try:
                # InDesign places captions in nested PDF forms.  ``tm`` is
                # local to that form, so it can place every printed row at the
                # same apparent coordinate.  Compose it with the current
                # graphics matrix to obtain real proof-page coordinates.
                a, b, c, _d, e, f = (float(value) for value in cm[:6])
                text_x = float(tm[4])
                text_y = float(tm[5])
                x = a * text_x + c * text_y + e
                y = b * text_x + _d * text_y + f
            except (TypeError, ValueError, IndexError):
                return
            converted = page_height - y
            # The captions gate has established that only intended credit text
            # lives in this frame.  A one-point allowance absorbs PDF rounding.
            if left - 1 <= x <= right + 1 and top - 1 <= converted <= bottom + 1:
                baselines.append(converted)
                # Pypdf exposes reliable text origins but not a portable glyph
                # box for every embedded InDesign font. Estimate the line end
                # deliberately wide, then add padding. This remains conservative
                # while no longer treating empty frame space as visible text.
                try:
                    font_size = max(1.0, float(_font_size))
                except (TypeError, ValueError):
                    font_size = 6.0
                for line in text.replace("\r", "\n").split("\n"):
                    printed = line.strip()
                    if not printed:
                        continue
                    glyph_lefts.append(max(left, x - 2.0))
                    estimated_right = x + max(font_size, len(printed) * font_size * 0.85) + 3.0
                    glyph_rights.append(min(right, estimated_right))

        page.extract_text(visitor_text=visit)
    except Exception as error:
        fail(f"Cannot read visible credits from proof PDF page {page_number}: {error}")
    if not baselines:
        return caption_bounds, 0
    # Credit text is an unrotated single column. Use the actual printed block
    # on both axes. The former frame-width approximation could push a model to
    # the page edge merely because its credits frame had empty space.
    text_left = max(left, min(glyph_lefts) if glyph_lefts else left)
    text_right = min(right, max(glyph_rights) if glyph_rights else right)
    return [max(top, min(baselines) - 2.0), text_left, min(bottom, max(baselines) + 2.0), text_right], len(baselines)


def _reliable_visible_caption_bounds(
    visible_bounds: list[float], visible_field_count: int, expected_field_count: int, frame_bounds: list[float],
) -> tuple[list[float], str]:
    """Reject a degenerate PDF visitor result before planning a correction.

    InDesign may emit all text fields from a nested form with one reported
    baseline even though the rendered credits occupy several rows.  A 24-field
    block with a one-line height is impossible visual evidence.  In that exact
    case the existing fixed credits frame is more conservative and truthful
    than allowing the audit to inspect only its first apparent line.
    """
    visible = _finite_bounds(visible_bounds, "visible credits bounds")
    frame = _finite_bounds(frame_bounds, "credits frame bounds")
    height = visible[2] - visible[0]
    frame_height = frame[2] - frame[0]
    minimum_height = max(8.0, min(frame_height * 0.5, float(expected_field_count) * 1.5))
    if expected_field_count >= 4 and visible_field_count >= expected_field_count and height < minimum_height:
        return frame, "native-frame-fallback-degenerate-pdf-text-coordinates"
    return visible, "pdf-glyph-coordinates"


def _status_rank(status: str) -> int:
    return {
        CAPTION_CLEARANCE_CLEAR: 0,
        CAPTION_CLEARANCE_REVIEW: 1,
        CAPTION_CLEARANCE_COLLISION: 2,
        CAPTION_CLEARANCE_OVERFLOW: 3,
    }[status]


def build_caption_clearance_audit(project: Path, state: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    """Create the non-negotiable computer-vision evidence for every credit block."""
    try:
        import numpy as np
        from PIL import Image, ImageDraw, ImageOps
        from pypdf import PdfReader
    except Exception as error:
        fail(f"Caption-clearance dependencies are unavailable: {error}")
    layout, layout_items = _validate_caption_clearance_layout(project, state)
    registry = validate_registry(state_artifact(project, state, "registry"), int(state["look_count"]), state_artifact(project, state, "hires"))
    hires = state_artifact(project, state, "hires")
    proof_pdf = child_of(project, manifest["proof_pdf"])
    try:
        proof_document = PdfReader(str(proof_pdf), strict=True)
    except Exception as error:
        fail(f"Cannot read visible credits from proof PDF: {error}")
    caption_rows = csv_rows(state_artifact(project, state, "captions"), CAPTION_FIELDS)
    products_by_look: dict[str, int] = {}
    for row in caption_rows:
        products_by_look[row["look_id"]] = products_by_look.get(row["look_id"], 0) + 1
    stamp = utc_now().replace(":", "-")
    proof_folder = caption_clearance_proof_dir(project) / stamp
    items: list[dict[str, Any]] = []
    for row in registry:
        look_id = row["look_id"]
        source = child_of(hires, row["left_filename"])
        if not source.is_file():
            fail(f"{look_id}: required left source is missing for caption-clearance audit.")
        caption_bounds = _finite_bounds(layout_items[look_id]["caption_bounds"], f"{look_id} caption_bounds")
        visible_text_bounds, visible_field_count = _visible_caption_text_bounds(
            proof_document, int(row["indd_left_page"]), caption_bounds
        )
        expected_field_count = products_by_look.get(look_id, 0) * 4
        visible_text_bounds, visible_bounds_source = _reliable_visible_caption_bounds(
            visible_text_bounds, visible_field_count, expected_field_count, caption_bounds,
        )
        image = _load_caption_clearance_source(source, np, Image, ImageOps)
        background, tolerance = _dominant_studio_background(image, np)
        inspected = dict(layout_items[look_id])
        inspected["caption_bounds"] = visible_text_bounds
        metrics, crop, mask = _caption_clearance_metrics_from_image(image, inspected, background, tolerance, np)
        metrics = dict(metrics)
        metrics["visible_text_bounds"] = visible_text_bounds
        metrics["selected_text_block_bounds"] = visible_text_bounds
        metrics["visible_text_bounds_source"] = visible_bounds_source
        metrics["visible_text_field_count"] = visible_field_count
        metrics["expected_text_field_count"] = expected_field_count
        if layout_items[look_id]["credits_overflow"] or visible_field_count < expected_field_count:
            metrics["status"] = CAPTION_CLEARANCE_OVERFLOW
            metrics["reason"] = "credits text overflows or does not fully render inside the fixed credits frame"
        else:
            safe_bounds = _finite_bounds(layout_items[look_id]["safe_bounds"], f"{look_id} safe_bounds")
            frame_bounds = _finite_bounds(layout_items[look_id]["caption_bounds"], f"{look_id} credits frame")
            if (
                frame_bounds[0] < safe_bounds[0] - 0.5 or frame_bounds[1] < safe_bounds[1] - 0.5
                or frame_bounds[2] > safe_bounds[2] + 0.5 or frame_bounds[3] > safe_bounds[3] + 0.5
            ):
                metrics["status"] = CAPTION_CLEARANCE_REVIEW
                metrics["reason"] = "credits frame lies outside the page safe area"
            metrics["safe_bounds"] = safe_bounds
        proof = proof_folder / f"{look_id}.png"
        _write_caption_clearance_overlay(crop, mask, metrics["status"], proof, np, Image, ImageDraw)
        items.append({
            "look_id": look_id, "source_filename": row["left_filename"], "source_sha256": digest(source),
            "proof_pair_sha256": manifest["look_pairs"][look_id]["sha256"],
            "status": metrics.pop("status"), "reason": metrics.pop("reason"), "metrics": metrics,
            "evidence_image": _relative_project_path(project, proof), "evidence_sha256": digest(proof),
        })
        # Crops are NumPy views of the full-resolution original. Drop every
        # reference before opening the next source, keeping a 50-look audit
        # bounded in memory.
        del image, background, crop, mask
    report = {
        "schema": CAPTION_CLEARANCE_SCHEMA, "generator": "lookbook_gate.py:caption-clearance-audit",
        "session_id": state["session_id"], "created_at": utc_now(), "master": identity(state_artifact(project, state, "master")),
        "layout_sha256": digest(caption_clearance_layout_path(project)), "visual_proof_manifest_sha256": digest(visual_proof_manifest_path(project)),
        "passed": all(item["status"] == CAPTION_CLEARANCE_CLEAR for item in items), "items": items,
    }
    write_json(caption_clearance_report_path(project), report)
    return report


def validate_caption_clearance(project: Path, state: dict[str, Any]) -> dict[str, Any]:
    """Block visual confirmation, release, and export until every block is clear."""
    manifest = validate_visual_proof(project, state)
    try:
        _layout, layout_items = _validate_caption_clearance_layout(project, state)
    except GateError:
        # `plan-clearance-corrections` archives its proof bundle before the
        # next composition is applied.  During that narrow interval the saved
        # master is unchanged, so its newest matching native layout remains a
        # valid source for one-look calibration.
        layout_items = {}
        master_identity = identity(state_artifact(project, state, "master"))
        for candidate in sorted((control_path(project) / "history").glob("visual-clearance-*/visual/caption-clearance-layout.json"), key=lambda path: path.stat().st_mtime, reverse=True):
            try:
                archived_layout = read_json(candidate)
            except GateError:
                continue
            if archived_layout.get("session_id") != state["session_id"] or not same_identity(archived_layout.get("master", {}), master_identity):
                continue
            for item in archived_layout.get("items", []):
                if isinstance(item, dict) and isinstance(item.get("look_id"), str):
                    layout_items[item["look_id"]] = item
            if layout_items:
                break
        if not layout_items:
            fail("No current-master native layout is available for targeted safe-area calibration.")
    report = read_json(caption_clearance_report_path(project))
    master = state_artifact(project, state, "master")
    if (
        report.get("schema") != CAPTION_CLEARANCE_SCHEMA
        or report.get("generator") != "lookbook_gate.py:caption-clearance-audit"
        or report.get("session_id") != state["session_id"]
        or not isinstance(report.get("master"), dict)
        or not same_identity(report["master"], identity(master))
        or report.get("layout_sha256") != digest(caption_clearance_layout_path(project))
        or report.get("visual_proof_manifest_sha256") != digest(visual_proof_manifest_path(project))
    ):
        fail("Caption-clearance audit is missing or not bound to the current rendered master.")
    registry = validate_registry(state_artifact(project, state, "registry"), int(state["look_count"]), state_artifact(project, state, "hires"))
    raw_items = report.get("items")
    if not isinstance(raw_items, list) or len(raw_items) != len(registry):
        fail("Caption-clearance audit does not contain exactly one result per required look.")
    checked: dict[str, dict[str, Any]] = {}
    hires = state_artifact(project, state, "hires")
    for row, item in zip(registry, raw_items):
        look_id = row["look_id"]
        if not isinstance(item, dict) or item.get("look_id") != look_id:
            fail(f"{look_id}: caption-clearance audit is missing or out of order.")
        source = child_of(hires, row["left_filename"])
        if (
            item.get("source_filename") != row["left_filename"]
            or not source.is_file()
            or item.get("source_sha256") != digest(source)
            or item.get("proof_pair_sha256") != manifest["look_pairs"][look_id]["sha256"]
            or item.get("status") not in {CAPTION_CLEARANCE_CLEAR, CAPTION_CLEARANCE_COLLISION, CAPTION_CLEARANCE_REVIEW, CAPTION_CLEARANCE_OVERFLOW}
        ):
            fail(f"{look_id}: caption-clearance audit does not describe the current registered source and rendered proof.")
        evidence = child_of(project, item.get("evidence_image", ""))
        if not evidence.is_file() or evidence.stat().st_size < 512 or item.get("evidence_sha256") != digest(evidence):
            fail(f"{look_id}: caption-clearance evidence image is missing or changed.")
        if look_id not in layout_items:
            fail(f"{look_id}: caption-clearance layout item is missing.")
        checked[look_id] = item
    if report.get("passed") is not True:
        blocked = [item["look_id"] for item in raw_items if item.get("status") != CAPTION_CLEARANCE_CLEAR]
        details = ", ".join(f"{item['look_id']} ({item['status']})" for item in raw_items if item.get("status") != CAPTION_CLEARANCE_CLEAR)
        fail(
            "Caption-clearance audit blocked visual release for " + details + ". "
            "Fix OVERFLOW in the current captions gate; for COLLISION or REVIEW exhaust the horizontal photo crop, then the signed existing-credits-frame down/right fallback, before regenerating proof and audit."
        )
    return report


def visual_confirmation_fingerprint(project: Path, state: dict[str, Any]) -> str:
    folder = visual_confirmation_dir(project)
    registry = validate_registry(state_artifact(project, state, "registry"), int(state["look_count"]), state_artifact(project, state, "hires"))
    entries: list[str] = []
    for row in registry:
        path = folder / f"{row['look_id']}.json"
        if not path.exists():
            return ""
        entries.append(f"{row['look_id']}:{digest(path)}")
    return hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()


def validate_visual_confirmations(project: Path, state: dict[str, Any]) -> list[dict[str, Any]]:
    manifest = validate_visual_proof(project, state)
    clearance = validate_caption_clearance(project, state)
    clearance_by_look = {item["look_id"]: item for item in clearance["items"]}
    manifest_digest = digest(visual_proof_manifest_path(project))
    pairs = manifest["look_pairs"]
    folder = visual_confirmation_dir(project)
    registry = validate_registry(state_artifact(project, state, "registry"), int(state["look_count"]), state_artifact(project, state, "hires"))
    master_identity = identity(state_artifact(project, state, "master"))
    expected_names = {f"{row['look_id']}.json" for row in registry}
    found_names = {path.name for path in folder.glob("*.json")} if folder.exists() else set()
    if found_names != expected_names:
        fail("Every rendered look pair needs one and only one visual confirmation; no bulk status file is accepted.")
    confirmations: list[dict[str, Any]] = []
    for row in registry:
        look_id = row["look_id"]
        confirmation = read_json(folder / f"{look_id}.json")
        if (
            confirmation.get("schema") != SCHEMA
            or confirmation.get("session_id") != state["session_id"]
            or confirmation.get("look_id") != look_id
            or confirmation.get("result") != VISUAL_RESULT
            or confirmation.get("proof_manifest_sha256") != manifest_digest
            or confirmation.get("proof_pair_sha256") != pairs[look_id]["sha256"]
            or confirmation.get("caption_clearance_item_sha256") != _caption_clearance_item_fingerprint(clearance_by_look[look_id])
            or not isinstance(confirmation.get("master"), dict)
            or not same_identity(confirmation["master"], master_identity)
        ):
            fail(f"{look_id}: confirmation is not bound to the current rendered pair proof.")
        note = confirmation.get("note")
        if not isinstance(note, str) or len(note.strip()) < 25:
            fail(f"{look_id}: visual confirmation needs a specific inspection note.")
        confirmations.append(confirmation)
    return confirmations


def clear_for_new_arm(project: Path, gate: str) -> None:
    evidence = evidence_file(project, gate)
    if evidence.exists():
        history = control_path(project) / "history" / utc_now().replace(":", "-")
        history.mkdir(parents=True, exist_ok=True)
        shutil.move(str(evidence), str(history / evidence.name))


def command_init(args: argparse.Namespace) -> None:
    project = project_path(args.project)
    master = child_of(project, args.master)
    if master.suffix.lower() != ".indd" or master.parent != project:
        fail("Master must be a saved .indd file directly in the project root; this anchors InDesign audits.")
    registry = child_of(project, args.registry)
    captions = child_of(project, args.captions)
    caption_map = child_of(project, args.caption_map)
    caption_workbook = child_of(project, args.caption_workbook)
    caption_provenance = child_of(project, args.caption_provenance)
    hires = child_of(project, args.hires)
    reference = child_of(project, args.reference)
    for label, artifact in (
        ("Registry", registry), ("Caption data", captions), ("Caption map", caption_map),
        ("Caption workbook", caption_workbook), ("Caption provenance", caption_provenance),
        ("Hires folder", hires), ("PDF reference", reference),
    ):
        require_work_path(project, artifact, label)
    assert_project_root_clean(project)
    if not hires.exists() or not hires.is_dir():
        fail(f"Hires folder does not exist: {hires}")
    if reference.suffix.lower() != ".pdf" or not reference.is_file():
        fail("--reference must name the copied PDF reference inside control\\work.")
    try:
        show_date = datetime.strptime(args.show_date, "%d.%m.%Y").date()
    except ValueError:
        fail("--show-date must use DD.MM.YYYY.")
    if args.looks < 1:
        fail("--looks must be at least 1.")
    existing = state_file(project)
    if existing.exists() and not args.restart:
        fail("A controller session already exists. Use status, or start intentionally with init --restart.")
    if existing.exists():
        archive = project / "control-history" / ("restarted-" + utc_now().replace(":", "-"))
        archive.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(control_path(project)), str(archive))
    control_path(project).mkdir(parents=True, exist_ok=True)
    work_path(project).mkdir(parents=True, exist_ok=True)
    for folder in ("evidence", "arms", "history", "progress", "visual", "quarantine"):
        (control_path(project) / folder).mkdir(exist_ok=True)
    state = {
        "schema": SCHEMA,
        "session_id": str(uuid.uuid4()),
        "created_at": utc_now(),
        "gates": list(GATES),
        "master": str(master.relative_to(project)),
        "registry": str(registry.relative_to(project)),
        "captions": str(captions.relative_to(project)),
        "caption_map": str(caption_map.relative_to(project)),
        "caption_workbook": str(caption_workbook.relative_to(project)),
        "caption_provenance": str(caption_provenance.relative_to(project)),
        "hires": str(hires.relative_to(project)),
        "reference_pdf": str(reference.relative_to(project)),
        "look_count": args.looks,
        "expected_pages": args.looks * 2 + 2,
        "show_date": args.show_date,
        "show_text": args.show_text,
        "legal_date": (show_date + timedelta(days=args.legal_offset_days)).strftime("%d.%m.%Y"),
    }
    write_json(state_file(project), state)
    launcher = install_in_design_audit_launcher()
    print(f"INITIALIZED session {state['session_id']}")
    print("Next required action: prepare-reference-order, confirm every PDF-reference pair, then validate-map")
    print("IN DESIGN: native COM automation is the normal route; do not open the master while an apply or export command is running.")
    if launcher:
        print(f"MANUAL FALLBACK ONLY: {launcher}")
    else:
        print("Scripts Panel was not found; this does not block native COM automation.")


def _reference_order_card(reference_page: Path, left_source: Path, right_source: Path, destination: Path) -> None:
    """Render one inspectable source-page → exact-hires proof card."""
    try:
        from PIL import Image, ImageDraw, ImageOps
    except Exception as error:
        fail(f"PDF-reference proof renderer dependency unavailable: {error}")
    def proof_preview(source: Path, size: tuple[int, int]) -> Any:
        """Decode proof-sized data, never a full camera image just to make a card."""
        with Image.open(source) as loaded:
            try:
                loaded.draft("RGB", (size[0] * 2, size[1] * 2))
            except (AttributeError, OSError):
                pass
            image = ImageOps.exif_transpose(loaded).convert("RGB")
            return ImageOps.contain(image, size, method=Image.Resampling.LANCZOS).copy()

    reference = proof_preview(reference_page, (1400, 714))
    left = proof_preview(left_source, (680, 465))
    right = proof_preview(right_source, (680, 465))
    width, top_height, lower_height = 1440, 760, 520
    canvas = Image.new("RGB", (width, top_height + lower_height + 70), "white")
    draw = ImageDraw.Draw(canvas)
    draw.text((20, 14), "PDF REFERENCE SPREAD", fill="black")
    top = ImageOps.contain(reference, (width - 40, top_height - 46), method=Image.Resampling.LANCZOS)
    canvas.paste(top, ((width - top.width) // 2, 42))
    draw.text((20, top_height + 8), "REGISTERED LEFT: FULL LENGTH", fill="black")
    draw.text((width // 2 + 20, top_height + 8), "REGISTERED RIGHT: CLOSE-UP", fill="black")
    left_render = ImageOps.contain(left, (width // 2 - 40, lower_height - 55), method=Image.Resampling.LANCZOS)
    right_render = ImageOps.contain(right, (width // 2 - 40, lower_height - 55), method=Image.Resampling.LANCZOS)
    left_y = top_height + 45 + (lower_height - 55 - left_render.height) // 2
    right_y = top_height + 45 + (lower_height - 55 - right_render.height) // 2
    canvas.paste(left_render, ((width // 2 - left_render.width) // 2, left_y))
    canvas.paste(right_render, (width // 2 + (width // 2 - right_render.width) // 2, right_y))
    destination.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(destination, "JPEG", quality=92, optimize=True)
    if not destination.is_file() or destination.stat().st_size < 1024:
        fail(f"Generated PDF-reference proof is unexpectedly empty: {destination}")


def command_prepare_reference_order(args: argparse.Namespace) -> None:
    """Create a non-editable evidence pack for the authoritative PDF order."""
    project = project_path(args.project)
    assert_project_root_clean(project)
    state = load_state(project)
    if current_gate(project, state) != "map":
        fail(f"PDF-reference order can be prepared only before map acceptance; next gate is {current_gate(project, state) or 'complete'}.")
    requested_reference = str(getattr(args, "reference", "") or "").strip()
    if requested_reference:
        reference = child_of(project, requested_reference)
        require_work_path(project, reference, "PDF reference")
        if reference.suffix.lower() != ".pdf" or not reference.is_file():
            fail("--reference must name a PDF file inside control\\work.")
        if state.get("reference_pdf") != str(reference.relative_to(project)):
            state["reference_pdf"] = str(reference.relative_to(project))
            write_json(state_file(project), state)
    if "reference_pdf" not in state:
        fail("A controlled PDF reference is required. Supply --reference inside control\\work.")
    reference = state_artifact(project, state, "reference_pdf")
    registry = state_artifact(project, state, "registry")
    hires = state_artifact(project, state, "hires")
    rows = validate_registry(registry, int(state["look_count"]), hires)
    existing_manifest = reference_order_manifest_path(project)
    if existing_manifest.is_file():
        validate_reference_order_manifest(project, state)
        print(f"PDF-REFERENCE ORDER READY: {len(rows)} existing proof cards retained. Inspect and confirm every LOOK_### before validate-map.")
        return
    confirmations = reference_order_confirmation_dir(project)
    if confirmations.exists() and any(confirmations.glob("LOOK_*.json")):
        fail("PDF-reference confirmations already exist and cannot be overwritten. Start a new controlled session to replace the source order.")
    stale_map = evidence_file(project, "map")
    if stale_map.exists():
        archive = control_path(project) / "history" / f"reference-order-{utc_now().replace(':', '-')}"
        archive.mkdir(parents=True, exist_ok=True)
        shutil.move(str(stale_map), str(archive / "map-before-reference-order.json"))
    try:
        from pypdf import PdfReader
    except Exception as error:
        fail(f"PDF-reference parser dependency unavailable: {error}")
    try:
        reference_pages = len(PdfReader(str(reference), strict=True).pages)
    except Exception as error:
        fail(f"PDF reference cannot be parsed: {error}")
    expected_reference_pages = len(rows) + 1
    if reference_pages != expected_reference_pages:
        fail(
            f"PDF reference has {reference_pages} pages; expected front cover plus {len(rows)} look spreads "
            f"({expected_reference_pages} pages total)."
        )
    stamp = utc_now().replace(":", "-")
    artifact_root = reference_order_root(project) / "rendered" / stamp
    rendered = render_pdf_pages(reference, artifact_root / "pages", list(range(1, reference_pages + 1)), 72)
    card_root = artifact_root / "cards"
    looks: dict[str, dict[str, Any]] = {}
    for row in rows:
        look_id = row["look_id"]
        reference_page = int(row["pdf_spread"]) + 1
        page_image = rendered.get(reference_page)
        if page_image is None:
            fail(f"{look_id}: source PDF did not render its required reference page {reference_page}.")
        card = card_root / f"{look_id}.jpg"
        _reference_order_card(page_image, hires / row["left_filename"], hires / row["right_filename"], card)
        looks[look_id] = {
            "look_id": look_id,
            "reference_page": reference_page,
            "left_filename": row["left_filename"], "right_filename": row["right_filename"],
            "left_sha256": digest(hires / row["left_filename"]), "right_sha256": digest(hires / row["right_filename"]),
            "reference_page_image": _relative_project_path(project, page_image), "reference_page_sha256": digest(page_image),
            "evidence_image": _relative_project_path(project, card), "evidence_sha256": digest(card),
        }
    manifest = {
        "schema": REFERENCE_ORDER_SCHEMA, "generator": "lookbook_gate.py:prepare-reference-order",
        "session_id": state["session_id"], "created_at": utc_now(), "reference_pdf": identity(reference),
        "reference_pdf_sha256": digest(reference), "reference_page_count": reference_pages,
        "registry_sha256": digest(registry), "looks": looks,
    }
    write_json(reference_order_manifest_path(project), manifest)
    validate_reference_order_manifest(project, state)
    print(f"PDF-REFERENCE ORDER READY: {len(rows)} proof cards. Inspect and confirm every LOOK_### before validate-map.")


def command_confirm_reference_look(args: argparse.Namespace) -> None:
    project = project_path(args.project)
    state = load_state(project)
    if current_gate(project, state) != "map":
        fail(f"PDF-reference confirmation is unavailable; next gate is {current_gate(project, state) or 'complete'}.")
    manifest = validate_reference_order_manifest(project, state)
    look_id = str(args.look).strip().upper()
    item = manifest.get("looks", {}).get(look_id)
    if not isinstance(item, dict):
        fail(f"Unknown required PDF-reference look: {look_id}")
    note = str(args.note).strip()
    if len(note) < 20:
        fail("--note must record the actual visual comparison of the PDF-reference spread and registered left/right photos.")
    destination = reference_order_confirmation_path(project, look_id)
    if destination.exists():
        fail(f"{look_id}: PDF-reference confirmation already exists and is immutable.")
    write_json(destination, {
        "schema": REFERENCE_ORDER_SCHEMA, "session_id": state["session_id"], "created_at": utc_now(),
        "look_id": look_id, "reference_order_manifest_sha256": digest(reference_order_manifest_path(project)),
        "item_fingerprint": reference_order_item_fingerprint(item), "note": note,
    })
    print(f"PDF-REFERENCE CONFIRMED {look_id}")


def command_validate_map(args: argparse.Namespace) -> None:
    project = project_path(args.project)
    assert_project_root_clean(project)
    state = load_state(project)
    if current_gate(project, state) != "map":
        fail(f"Map cannot be accepted now; next gate is {current_gate(project, state) or 'complete'}.")
    registry = state_artifact(project, state, "registry")
    rows = validate_registry(registry, int(state["look_count"]), state_artifact(project, state, "hires"))
    validate_reference_order(project, state)
    caption_rows = validate_verified_caption_inputs(project, state, rows)
    mapping = state_artifact(project, state, "caption_map")
    workbook = state_artifact(project, state, "caption_workbook")
    captions = state_artifact(project, state, "captions")
    provenance = state_artifact(project, state, "caption_provenance")
    evidence = {
        "schema": SCHEMA, "session_id": state["session_id"], "gate": "map", "passed": True,
        "created_at": utc_now(), "registry": identity(registry), "registry_sha256": digest(registry),
        "reference_pdf_sha256": digest(state_artifact(project, state, "reference_pdf")),
        "reference_order_manifest_sha256": digest(reference_order_manifest_path(project)),
        "reference_order_confirmation_fingerprint": reference_order_confirmation_fingerprint(project, state),
        "caption_map_sha256": digest(mapping), "caption_workbook_sha256": digest(workbook),
        "caption_data_sha256": digest(captions), "caption_provenance_sha256": digest(provenance),
        "look_count": len(rows), "expected_pages": state["expected_pages"],
    }
    write_json(evidence_file(project, "map"), evidence)
    print(f"PASS map: {len(rows)} PDF-reference-bound photo pairs and {caption_rows} caption rows are frozen from visually confirmed Excel matches.")


def command_arm(args: argparse.Namespace) -> None:
    project = project_path(args.project)
    state = load_state(project)
    gate = args.gate
    if gate not in GATES or gate in {"map", "pdf"}:
        fail("arm accepts one of: structure, dates, frames, images, captions, visual, release.")
    if current_gate(project, state) != gate:
        fail(f"Cannot arm {gate}; next required gate is {current_gate(project, state) or 'complete'}.")
    existing_arm = arm_file(project, gate)
    if existing_arm.exists() and load_evidence(project, state, gate) is None:
        active = read_json(existing_arm)
        if active.get("session_id") == state["session_id"] and active.get("gate") == gate:
            fail(f"{gate} is already armed. Resume it with apply; do not re-arm and discard its recovery state.")
    master = state_artifact(project, state, "master")
    if not master.exists():
        fail("Master is missing.")
    if gate in {"captions", "release"}:
        validate_verified_caption_inputs(project, state)
    clear_for_new_arm(project, gate)
    arm = {
        "schema": SCHEMA, "session_id": state["session_id"], "gate": gate,
        "nonce": str(uuid.uuid4()), "armed_at": utc_now(), "master_name": master.name,
    }
    write_json(arm_file(project, gate), arm)
    if gate in INDESIGN_GATES:
        if gate == "images":
            print(f"ARMED images. apply processes four looks per saved COM batch, resumes after a restart, and writes PASS only after all links are verified.")
        elif gate == "captions":
            print(f"ARMED captions. apply formats four looks per saved COM batch, resumes after a restart, and writes PASS only after all credit frames are verified.")
        else:
            print(f"ARMED {gate}. Run `lookbook_gate.py apply {project} --gate {gate}`. It performs only this gate through InDesign's native object model, saves it, and writes proof.")
    else:
        print("ARMED visual. Create the composition plan, run apply-composition, render-visual-proof, inspect and confirm every pair, then run record-visual.")


def _driver_project(arguments: list[str]) -> Path | None:
    for index, value in enumerate(arguments):
        if value == "-Project" and index + 1 < len(arguments):
            return Path(arguments[index + 1]).expanduser().resolve()
    return None


def safe_restart_stalled_indesign(project: Path) -> bool:
    """Restart only a provably idle or already-terminated automation worker.

    A neutral one-process InDesign session may be restarted.  If COM has
    already terminated InDesign, its project-local `.idlk` is stale by
    definition; remove it only after confirming that *no* InDesign process
    remains, then start the installed 2026 executable.  A visible document,
    multiple processes, or an unknown installation is never touched.
    """
    script = r'''
param([string]$Project)
$ErrorActionPreference = 'Stop'
$instances = @(Get-Process -Name InDesign -ErrorAction SilentlyContinue)
$locks = @(Get-ChildItem -LiteralPath $Project -Filter '*.idlk' -Force -ErrorAction SilentlyContinue)
if ($instances.Count -eq 0) {
    # No process can own a document lock.  These project-local locks were
    # left by the crashed automation client and block only the safe retry.
    $candidate = Join-Path $env:ProgramFiles 'Adobe\Adobe InDesign 2026\InDesign.exe'
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { exit 3 }
    foreach ($lock in $locks) { Remove-Item -LiteralPath $lock.FullName -Recurse -Force }
    Start-Process -FilePath $candidate -WindowStyle Hidden
    exit 0
}
if ($instances.Count -ne 1) { exit 4 }
$instance = $instances[0]
if ($instance.MainWindowTitle -notmatch '^Adobe InDesign(?: 2026)?$') { exit 5 }
if (-not $instance.Path) { exit 6 }
# A neutral title proves there is no open document, so any remaining lock in
# this project is stale. Never remove one while a document title is visible.
foreach ($lock in $locks) { Remove-Item -LiteralPath $lock.FullName -Recurse -Force }
$exe = $instance.Path
Stop-Process -Id $instance.Id -Force
Start-Sleep -Seconds 2
Start-Process -FilePath $exe -WindowStyle Hidden
exit 0
'''
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script, "-Project", str(project)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return completed.returncode == 0


def is_com_disconnect(output: str) -> bool:
    """Return true only for the known, recoverable InDesign RPC dropout."""
    normalized = output.casefold()
    return "rpc_e_disconnected" in normalized or "0x80010108" in normalized


def restart_caption_repair_indesign(project: Path, state: dict[str, Any]) -> bool:
    """Recover only the saved controlled master after a native repair disconnect."""
    master = state_artifact(project, state, "master")
    script = r'''
param([string]$Master)
$ErrorActionPreference = 'Stop'
$SAVE_NO = 1852776480
try {
    $app = New-Object -ComObject InDesign.Application
    $wanted = [System.IO.Path]::GetFullPath($Master).ToLowerInvariant()
    $matches = @()
    foreach ($document in $app.Documents) {
        if ([System.IO.Path]::GetFullPath([string]$document.FullName).ToLowerInvariant() -eq $wanted) { $matches += $document }
    }
    # This helper is called only after a failed controlled caption-repair batch.
    # The durable checkpoint was written before the batch, so discard any
    # unsaved tail from this exact master rather than preserving an ambiguous
    # half-transaction. Other open documents remain an immediate hard stop.
    foreach ($document in $matches) { $document.Close($SAVE_NO) }
    if ($app.Documents.Count -ne 0) { exit 4 }
    $instances = @(Get-Process -Name InDesign -ErrorAction SilentlyContinue)
    if ($instances.Count -ne 1 -or -not $instances[0].Path) { exit 5 }
    $exe = $instances[0].Path
    Stop-Process -Id $instances[0].Id -Force
    Start-Sleep -Seconds 2
    Start-Process -FilePath $exe -WindowStyle Hidden
    exit 0
} catch { exit 6 }
'''
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script, "-Master", str(master)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return completed.returncode == 0


def run_com_driver(arguments: list[str], timeout_seconds: int = 900) -> str:
    """Run one native COM transaction with one safe cold-restart recovery.

    A 0x80010108 error means the temporary PowerShell COM client lost the
    InDesign server. It is not a content-validation failure. When the document
    is already closed, unlocked and InDesign exposes only its neutral start
    window, the controller may restart that one idle instance and repeat the
    same command. Any ambiguity remains a hard stop.
    """
    driver = Path(__file__).resolve().with_name("run_lookbook_gate_com.ps1")
    if not driver.exists():
        fail(f"Native InDesign automation driver is missing: {driver}")
    command = [
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(driver),
        *arguments,
    ]
    project = _driver_project(arguments)
    restarted = False
    while True:
        try:
            completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            if project is not None and not restarted and safe_restart_stalled_indesign(project):
                restarted = True
                print("RECOVERY: InDesign timed out while idle; restarted the automation instance and rerunning the same gate.")
                time.sleep(4)
                continue
            fail(f"InDesign automation timed out after {timeout_seconds // 60} minutes. The last saved batch remains recoverable; the controller did not restart InDesign because a document or lock could not be ruled out.")
        output = "\n".join(part.strip() for part in (completed.stdout, completed.stderr) if part.strip())
        if completed.returncode == 0:
            return output
        if project is not None and not restarted and is_com_disconnect(output) and safe_restart_stalled_indesign(project):
            restarted = True
            print("RECOVERY: InDesign disconnected from COM; restarted the idle automation instance and rerunning the same gate.")
            time.sleep(4)
            continue
        fail("InDesign automation failed for the armed gate" + (f": {output}" if output else "."))


def gate_progress(project: Path, state: dict[str, Any], arm: dict[str, Any], gate: str) -> dict[str, Any] | None:
    path = progress_file(project, gate)
    if not path.exists():
        return None
    try:
        progress = read_json(path)
    except GateError:
        return None
    if (
        progress.get("schema") != SCHEMA
        or progress.get("session_id") != state["session_id"]
        or progress.get("gate") != gate
        or progress.get("nonce") != arm.get("nonce")
    ):
        return None
    return progress


def image_progress(project: Path, state: dict[str, Any], arm: dict[str, Any]) -> dict[str, Any] | None:
    return gate_progress(project, state, arm, "images")


def caption_progress(project: Path, state: dict[str, Any], arm: dict[str, Any]) -> dict[str, Any] | None:
    return gate_progress(project, state, arm, "captions")


def command_apply(args: argparse.Namespace) -> None:
    project = project_path(args.project)
    state = load_state(project)
    gate = args.gate
    if gate not in INDESIGN_GATES:
        fail("apply accepts one of: structure, dates, frames, images, captions, release.")
    if current_gate(project, state) != gate:
        fail(f"Cannot apply {gate}; next required gate is {current_gate(project, state) or 'complete'}.")
    armed_path = arm_file(project, gate)
    if not armed_path.exists():
        fail(f"Gate {gate} is not armed. Run arm --gate {gate} first.")
    armed = read_json(armed_path)
    if gate in {"images", "captions"}:
        total = int(state["look_count"])
        progress = image_progress if gate == "images" else caption_progress
        batch_message = "linked" if gate == "images" else "formatted"
        # One CLI invocation owns exactly one saved four-look transaction.
        # A full 50-look loop exceeds the agent shell deadline, after which an
        # agent can mistake a still-running worker for a failed operation and
        # launch an unsafe duplicate.  Returning a durable checkpoint after
        # every batch makes the next invocation unambiguous and resumable.
        before = progress(project, state, armed)
        before_count = int(before.get("completed_count", 0)) if before else 0
        run_com_driver(["-Action", "ApplyGate", "-Project", str(project), "-Gate", gate, "-BatchSize", "4"], timeout_seconds=180)
        if load_evidence(project, state, gate) is not None:
            print(f"PASS {gate}: {total} looks {batch_message}, saved, and verified in resumable batches.")
            return
        after = progress(project, state, armed)
        if after is None:
            fail(f"InDesign finished a {gate} batch without durable recovery progress. {gate.capitalize()} remain blocked.")
        completed = int(after.get("completed_count", 0))
        if completed <= before_count:
            fail(f"{gate.capitalize()} batch did not advance durable progress. Retry apply without re-arming.")
        print(f"CHECKPOINT {gate}: {completed}/{total} looks saved and verified. Run the same apply command again only after this command has returned.")
        return
    run_com_driver(["-Action", "ApplyGate", "-Project", str(project), "-Gate", gate])
    if load_evidence(project, state, gate) is None:
        fail(f"InDesign completed without current PASS evidence for {gate}. The gate remains blocked.")
    print(f"PASS {gate}: native InDesign automation completed and evidence is current.")


def composition_progress(project: Path, state: dict[str, Any]) -> dict[str, Any] | None:
    path = progress_file(project, "composition")
    if not path.exists():
        return None
    try:
        progress = read_json(path)
        armed = read_json(arm_file(project, "visual"))
    except GateError:
        return None
    if (
        progress.get("schema") != SCHEMA
        or progress.get("session_id") != state["session_id"]
        or progress.get("gate") != "visual-composition"
        or progress.get("nonce") != armed.get("nonce")
        or progress.get("composition_plan_sha256") != digest(composition_plan_path(project))
    ):
        return None
    return progress


def _validate_blocked_clearance_report(project: Path, state: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate current clearance evidence without requiring every look CLEAR."""
    manifest = validate_visual_proof(project, state)
    _layout, _items = _validate_caption_clearance_layout(project, state)
    report = read_json(caption_clearance_report_path(project))
    master = state_artifact(project, state, "master")
    if (
        report.get("schema") != CAPTION_CLEARANCE_SCHEMA
        or report.get("generator") != "lookbook_gate.py:caption-clearance-audit"
        or report.get("session_id") != state["session_id"]
        or not isinstance(report.get("master"), dict)
        or not same_identity(report["master"], identity(master))
        or report.get("layout_sha256") != digest(caption_clearance_layout_path(project))
        or report.get("visual_proof_manifest_sha256") != digest(visual_proof_manifest_path(project))
    ):
        fail("Caption-clearance report is not bound to the current saved master and proof.")
    registry = validate_registry(state_artifact(project, state, "registry"), int(state["look_count"]), state_artifact(project, state, "hires"))
    raw_items = report.get("items")
    if not isinstance(raw_items, list) or len(raw_items) != len(registry):
        fail("Caption-clearance report does not cover every required look.")
    checked: list[dict[str, Any]] = []
    hires = state_artifact(project, state, "hires")
    for row, item in zip(registry, raw_items):
        look_id = row["look_id"]
        source = child_of(hires, row["left_filename"])
        if (
            not isinstance(item, dict)
            or item.get("look_id") != look_id
            or item.get("source_filename") != row["left_filename"]
            or not source.is_file()
            or item.get("source_sha256") != digest(source)
            or item.get("proof_pair_sha256") != manifest["look_pairs"][look_id]["sha256"]
            or item.get("status") not in {CAPTION_CLEARANCE_CLEAR, CAPTION_CLEARANCE_COLLISION, CAPTION_CLEARANCE_REVIEW, CAPTION_CLEARANCE_OVERFLOW}
        ):
            fail(f"{look_id}: blocked caption-clearance evidence is incomplete or stale.")
        checked.append(item)
    return checked


def _best_horizontal_clearance_shift(source: Path, layout_item: dict[str, Any], current_shift: float) -> tuple[float, dict[str, Any]]:
    """Test both crop directions against the original photo before changing it.

    The required blank space can be on either side of a model depending on pose,
    bag, or garment volume.  A fixed "move right" rule is therefore unsafe:
    choose the best bounded horizontal candidate from the same source pixels.
    """
    try:
        import numpy as np
        from PIL import Image, ImageOps
    except Exception as error:
        fail(f"Horizontal clearance planner dependencies are unavailable: {error}")
    image = _load_caption_clearance_source(source, np, Image, ImageOps)
    # Crop decisions are made at the original resolution.  A reduced proxy can
    # erase a thin garment edge or JPEG detail and incorrectly prefer the
    # current crop over a genuinely clear horizontal position.  This is a
    # bounded, offline operation (21 candidates per blocked look), and the
    # resulting determinism is more important than a small planning shortcut.
    background, tolerance = _dominant_studio_background(image, np)
    graphic = _finite_bounds(layout_item["graphic_bounds"], f"{layout_item.get('look_id', 'look')} graphic_bounds")
    baseline = [graphic[0], graphic[1] - current_shift, graphic[2], graphic[3] - current_shift]
    evaluate = _fast_caption_clearance_evaluator(image, background, tolerance, np)
    candidates: list[tuple[tuple[Any, ...], float, dict[str, Any]]] = []

    def score_candidate(candidate: float) -> None:
        metrics = evaluate(
            [baseline[0], baseline[1] + candidate, baseline[2], baseline[3] + candidate],
            _finite_bounds(layout_item["caption_bounds"], f"{layout_item.get('look_id', 'look')} caption_bounds"),
        )
        candidates.append((
            (_status_rank(metrics["status"]), metrics.get("coverage", 1.0), metrics.get("largest_component", 1.0), abs(candidate), candidate),
            candidate,
            metrics,
        ))

    candidate_points = set(range(-int(MAX_CLEARANCE_SHIFT_POINTS), int(MAX_CLEARANCE_SHIFT_POINTS) + 1, 10))
    if abs(current_shift) <= MAX_CLEARANCE_SHIFT_POINTS:
        candidate_points.add(int(round(current_shift)))
    for points in sorted(candidate_points):
        score_candidate(float(points))
    # A broad 10-pt pass is enough for normal corrections.  If it does not
    # find a clear crop, exhaust the bounded range at one-point resolution
    # before declaring the fixed-frame rule physically impossible.  This avoids
    # overlooking a narrow clean strip next to a hand, bag, or garment edge.
    if min(candidates, key=lambda candidate: candidate[0])[2]["status"] != CAPTION_CLEARANCE_CLEAR:
        for points in range(-int(MAX_CLEARANCE_SHIFT_POINTS), int(MAX_CLEARANCE_SHIFT_POINTS) + 1):
            if points not in candidate_points:
                score_candidate(float(points))
    del image, background, evaluate
    _rank, shift, metrics = min(candidates, key=lambda candidate: candidate[0])
    return shift, metrics


def _best_caption_frame_clearance(
    source: Path,
    layout_item: dict[str, Any],
    visible_bounds: list[float],
    *,
    graphic_override: list[float] | None = None,
    evaluator_override: Any | None = None,
    exhaustive: bool = True,
) -> dict[str, Any] | None:
    """Find the first safe move of the existing credits frame after crop failure.

    The policy is deliberately asymmetric and mirrors the approved art direction:
    first try the smallest downward move, preserving the credits style; only if
    no downward position is clear, try the right side with right alignment.  The
    detector follows the actual rendered glyph rectangle.  A text frame can
    contain deliberate empty area below or beside its paragraphs, so treating
    the entire frame as ink would falsely reject an otherwise clear placement.
    For RIGHT, the measured visible text block is anchored to the target
    frame's right edge, matching the native right-aligned paragraphs.
    """
    try:
        import numpy as np
        from PIL import Image, ImageOps
    except Exception as error:
        fail(f"Caption-clearance planner dependencies are unavailable: {error}")
    frame = _finite_bounds(layout_item.get("caption_bounds"), f"{layout_item.get('look_id', 'look')} caption_bounds")
    visible = _finite_bounds(visible_bounds, f"{layout_item.get('look_id', 'look')} visible_text_bounds")
    page = _finite_bounds(layout_item.get("page_bounds"), f"{layout_item.get('look_id', 'look')} page_bounds")
    safe = _finite_bounds(layout_item.get("safe_bounds"), f"{layout_item.get('look_id', 'look')} safe_bounds")
    graphic = _finite_bounds(
        graphic_override if graphic_override is not None else layout_item.get("graphic_bounds"),
        f"{layout_item.get('look_id', 'look')} graphic_bounds",
    )
    if frame[0] < page[0] - 0.05 or frame[1] < page[1] - 0.05 or frame[2] > page[2] + 0.05 or frame[3] > page[3] + 0.05:
        fail(f"{layout_item.get('look_id', 'look')}: existing credits frame is outside its left page.")
    if safe[0] < page[0] - 0.05 or safe[1] < page[1] - 0.05 or safe[2] > page[2] + 0.05 or safe[3] > page[3] + 0.05:
        fail(f"{layout_item.get('look_id', 'look')}: page safe area is invalid.")
    # The page margins are the non-negotiable outer boundary.  Planning exactly
    # on that guide leaves the credit block looking clipped to the page edge in
    # a rendered PDF, so keep every candidate inside a narrow interior gutter.
    planning_safe = [
        safe[0] + SAFE_AREA_INTERIOR_POINTS,
        safe[1] + SAFE_AREA_INTERIOR_POINTS,
        safe[2] - SAFE_AREA_INTERIOR_POINTS,
        safe[3] - SAFE_AREA_INTERIOR_POINTS,
    ]
    if planning_safe[2] <= planning_safe[0] or planning_safe[3] <= planning_safe[1]:
        fail(f"{layout_item.get('look_id', 'look')}: page safe area is too small for credits clearance planning.")
    owns_evaluator = evaluator_override is None
    if evaluator_override is None:
        image = _load_caption_clearance_source(source, np, Image, ImageOps)
        background, tolerance = _dominant_studio_background(image, np)
        evaluate = _fast_caption_clearance_evaluator(image, background, tolerance, np)
    else:
        image = background = None
        evaluate = evaluator_override

    def visible_after_move(delta_x: float, delta_y: float, right_aligned: bool) -> list[float]:
        """Predict the rendered ink rectangle, not the full text-frame bounds."""
        height = visible[2] - visible[0]
        width = visible[3] - visible[1]
        top = visible[0] + delta_y
        bottom = top + height
        if right_aligned:
            right_inset = frame[3] - visible[3]
            right = frame[3] + delta_x - right_inset
            left = right - width
        else:
            left = visible[1] + delta_x
            right = left + width
        return [top, left, bottom, right]

    def metrics_for(delta_x: float, delta_y: float, right_aligned: bool) -> dict[str, Any]:
        return evaluate(graphic, visible_after_move(delta_x, delta_y, right_aligned))

    def candidates_for_delta(limit: float, axis: str, use_frame: bool) -> list[tuple[float, dict[str, Any]]]:
        values: list[float] = []
        maximum = int(math.floor(limit + 0.001))
        for delta in range(int(MAX_CAPTION_CLEARANCE_STEP_POINTS), maximum + 1, int(MAX_CAPTION_CLEARANCE_STEP_POINTS)):
            values.append(float(delta))
        if maximum > 0 and (not values or abs(values[-1] - maximum) > 0.01):
            values.append(float(maximum))
        tested: list[tuple[float, dict[str, Any]]] = []
        for delta in values:
            tested.append((delta, metrics_for(
                0.0 if axis == "down" else delta,
                delta if axis == "down" else 0.0,
                axis != "down",
            )))
        # A coarse pass keeps ordinary planning fast.  When it finds no clear
        # position, exhaust the permitted axis one point at a time before giving
        # up; a model edge can leave a narrow but valid studio-background strip.
        if exhaustive and tested and not any(metrics["status"] == CAPTION_CLEARANCE_CLEAR for _delta, metrics in tested):
            known = {int(round(delta)) for delta, _metrics in tested}
            for delta in range(1, maximum + 1):
                if delta not in known:
                    tested.append((float(delta), metrics_for(
                        0.0 if axis == "down" else float(delta),
                        float(delta) if axis == "down" else 0.0,
                        axis != "down",
                    )))
        return tested

    # Downward first: keep the text's original left alignment and never move it
    # upward, sideways, or beyond the same page's bottom edge.
    down_limit = planning_safe[2] - frame[2]
    down = candidates_for_delta(down_limit, "down", use_frame=False)
    down_clear = [(delta, metrics) for delta, metrics in down if metrics["status"] == CAPTION_CLEARANCE_CLEAR]
    if down_clear:
        delta, metrics = min(down_clear, key=lambda item: item[0])
        after = [frame[0] + delta, frame[1], frame[2] + delta, frame[3]]
        if owns_evaluator:
            del image, background, evaluate
        return {
            "mode": "DOWN", "paragraph_alignment": "STYLE", "from_frame_bounds": frame,
            "to_frame_bounds": after, "delta_x": 0.0, "delta_y": delta,
            "predicted_status": metrics["status"], "predicted_coverage": metrics.get("coverage"),
        }

    # The final permitted fallback is the page's right side.  Right alignment
    # and a downward component are tested together against the measured glyph
    # bounds, so a harmless empty portion of the existing frame cannot block a
    # valid lower-right placement.
    right_limit = planning_safe[3] - frame[3]
    max_down = planning_safe[2] - frame[2]
    right_x = [float(value) for value in range(int(MAX_CAPTION_CLEARANCE_STEP_POINTS), int(math.floor(right_limit)) + 1, int(MAX_CAPTION_CLEARANCE_STEP_POINTS))]
    if right_limit > 0 and (not right_x or abs(right_x[-1] - right_limit) > 0.01):
        right_x.append(float(math.floor(right_limit)))
    down_y = [0.0] + [float(value) for value in range(int(MAX_CAPTION_CLEARANCE_STEP_POINTS), int(math.floor(max_down)) + 1, int(MAX_CAPTION_CLEARANCE_STEP_POINTS))]
    if max_down > 0 and abs(down_y[-1] - max_down) > 0.01:
        down_y.append(float(math.floor(max_down)))
    right_candidates: list[tuple[tuple[float, float], dict[str, Any]]] = []
    for delta_y in down_y:
        for delta_x in right_x:
            metrics = metrics_for(delta_x, delta_y, True)
            if metrics["status"] == CAPTION_CLEARANCE_CLEAR:
                right_candidates.append(((delta_y, delta_x), metrics))
    # As with photo crops, only fall back to point precision when the coarse
    # grid proved that no clear right-side location exists.
    if exhaustive and not right_candidates and right_limit > 0:
        for delta_y in range(0, int(math.floor(max_down)) + 1):
            for delta_x in range(1, int(math.floor(right_limit)) + 1):
                if delta_y % int(MAX_CAPTION_CLEARANCE_STEP_POINTS) == 0 and delta_x % int(MAX_CAPTION_CLEARANCE_STEP_POINTS) == 0:
                    continue
                metrics = metrics_for(float(delta_x), float(delta_y), True)
                if metrics["status"] == CAPTION_CLEARANCE_CLEAR:
                    right_candidates.append(((float(delta_y), float(delta_x)), metrics))
    if right_candidates:
        (delta_y, delta_x), metrics = min(right_candidates, key=lambda item: (item[0][0], item[0][1]))
        after = [frame[0] + delta_y, frame[1] + delta_x, frame[2] + delta_y, frame[3] + delta_x]
        if owns_evaluator:
            del image, background, evaluate
        return {
            "mode": "RIGHT", "paragraph_alignment": "RIGHT_ALIGN", "from_frame_bounds": frame,
            "to_frame_bounds": after, "delta_x": delta_x, "delta_y": delta_y,
            "predicted_status": metrics["status"], "predicted_coverage": metrics.get("coverage"),
        }
    if owns_evaluator:
        del image, background, evaluate
    return None


def _best_combined_caption_clearance(
    source: Path,
    layout_item: dict[str, Any],
    visible_bounds: list[float],
    current_shift: float,
    preferred_shift: float,
) -> dict[str, Any] | None:
    """Find a clear lower/right credits position *with* a photo crop.

    The former planner treated photo movement and credits movement as mutually
    exclusive alternatives.  That misses a common editorial layout: move the
    full-length image slightly left or right, then place right-aligned credits
    in the resulting lower-right studio background.  The two operations are
    both allowed, operate only on existing objects, and must be evaluated as
    one composed result.
    """
    try:
        import numpy as np
        from PIL import Image, ImageOps
    except Exception as error:
        fail(f"Combined caption-clearance planner dependencies are unavailable: {error}")
    graphic = _finite_bounds(layout_item.get("graphic_bounds"), f"{layout_item.get('look_id', 'look')} graphic_bounds")
    baseline = [graphic[0], graphic[1] - current_shift, graphic[2], graphic[3] - current_shift]
    image = _load_caption_clearance_source(source, np, Image, ImageOps)
    background, tolerance = _dominant_studio_background(image, np)
    evaluate = _fast_caption_clearance_evaluator(image, background, tolerance, np)

    def graphic_at(shift: float) -> list[float]:
        return [baseline[0], baseline[1] + shift, baseline[2], baseline[3] + shift]

    def attach(candidate: dict[str, Any], shift: float) -> dict[str, Any]:
        result = dict(candidate)
        result["photo_adjustment_points"] = shift
        result["photo_from_points"] = current_shift
        return result

    # Search the normal studio layout grid first.  It covers combined photo +
    # down and photo + right/down positions in a few thousand cached-mask
    # reads, which keeps a 50-look planning pass responsive.
    coarse = set(range(-int(MAX_CLEARANCE_SHIFT_POINTS), int(MAX_CLEARANCE_SHIFT_POINTS) + 1, 10))
    if abs(current_shift) <= MAX_CLEARANCE_SHIFT_POINTS:
        coarse.add(int(round(current_shift)))
    if abs(preferred_shift) <= MAX_CLEARANCE_SHIFT_POINTS:
        coarse.add(int(round(preferred_shift)))
    ordered_coarse = sorted(coarse, key=lambda value: (abs(value - preferred_shift), abs(value - current_shift), abs(value), value))
    found: list[dict[str, Any]] = []
    for shift in ordered_coarse:
        candidate = _best_caption_frame_clearance(
            source, layout_item, visible_bounds,
            graphic_override=graphic_at(float(shift)), evaluator_override=evaluate, exhaustive=False,
        )
        if candidate is not None:
            found.append(attach(candidate, float(shift)))
    if not found:
        # A narrow clean region can sit between the regular 10-point grid.  The
        # existing crop planner has already identified the most promising photo
        # position; exhaust every frame position there before declaring a
        # combined placement unavailable.
        candidate = _best_caption_frame_clearance(
            source, layout_item, visible_bounds,
            graphic_override=graphic_at(preferred_shift), evaluator_override=evaluate, exhaustive=True,
        )
        if candidate is not None:
            found.append(attach(candidate, preferred_shift))
    del image, background, evaluate
    if not found:
        return None
    return min(
        found,
        key=lambda item: (
            abs(float(item["photo_adjustment_points"]) - preferred_shift),
            abs(float(item["photo_adjustment_points"]) - current_shift),
            0 if item["mode"] == "DOWN" else 1,
            float(item["delta_y"]), float(item["delta_x"]),
        ),
    )


def _archive_visual_cycle_for_clearance_retry(project: Path) -> Path:
    """Preserve superseded proof before a new, unconfirmed crop plan is applied."""
    archive = control_path(project) / "history" / f"visual-clearance-{utc_now().replace(':', '-')}"
    for relative in (
        "visual/composition-plan.tsv", "visual/composition-applied.json", "visual/proof",
        "visual/caption-clearance", "visual/caption-clearance-layout.json", "visual/caption-clearance.json",
        "visual/clearance-correction-plan.json",
        "progress/composition.json",
    ):
        source = control_path(project) / relative
        if not source.exists():
            continue
        destination = archive / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
    return archive


def command_restart_visual_confirmations(args: argparse.Namespace) -> None:
    """Archive partial visual attestations after a grounded proof rejection.

    This does not change the INDD. It retires only the incomplete confirmation
    queue at the visual gate so the controller can prepare a new signed visual
    correction from the same current proof. A completed visual gate remains
    immutable because its current gate is no longer ``visual``.
    """
    project = project_path(args.project)
    state = load_state(project)
    if current_gate(project, state) != "visual":
        fail("Partial visual confirmations can be restarted only at the visual gate.")
    notes = args.notes.strip()
    if len(notes) < 25:
        fail("--notes must record the specific visual rejection that restarts the partial proof queue.")
    confirmations = visual_confirmation_dir(project)
    entries = sorted(confirmations.glob("*.json")) if confirmations.is_dir() else []
    manifest = validate_visual_proof(project, state)
    _validate_blocked_clearance_report(project, state)
    archive = control_path(project) / "history" / f"visual-rejected-{utc_now().replace(':', '-')}"
    destination = archive / "visual" / "confirmations"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if entries:
        shutil.move(str(confirmations), str(destination))
    confirmations.mkdir(parents=True, exist_ok=True)
    write_json(archive / "rejection.json", {
        "schema": SCHEMA, "generator": "lookbook_gate.py:restart-visual-confirmations",
        "session_id": state["session_id"], "created_at": utc_now(),
        "master": identity(state_artifact(project, state, "master")),
        "proof_manifest_sha256": digest(visual_proof_manifest_path(project)),
        "confirmation_count": len(entries), "notes": notes,
        "proof_looks": sorted(manifest["look_pairs"]),
    })
    print(f"PARTIAL VISUAL CONFIRMATIONS ARCHIVED: {len(entries)} proof records were retired for a controller-planned correction.")


def _same_bounds(left: list[float], right: list[float], tolerance: float = 0.5) -> bool:
    return len(left) == 4 and len(right) == 4 and all(abs(a - b) <= tolerance for a, b in zip(left, right))


def _current_master_composition_item(project: Path, state: dict[str, Any], look_id: str) -> dict[str, Any] | None:
    """Return the newest native composition record for this exact saved master.

    Visual retry plans are deliberately allowed to change before their next
    native batch is applied.  The plan therefore cannot be used as a statement
    of what is in the INDD right now.  A same-master native record is the only
    reliable source for an in-place crop or credits-frame position.
    """
    master = identity(state_artifact(project, state, "master"))
    candidates = [composition_applied_path(project)]
    history = control_path(project) / "history"
    if history.is_dir():
        candidates.extend(sorted(history.glob("visual-clearance-*/visual/composition-applied.json"), key=lambda path: path.stat().st_mtime, reverse=True))
    for path in candidates:
        if not path.is_file():
            continue
        try:
            evidence = read_json(path)
        except GateError:
            continue
        if (
            evidence.get("schema") != SCHEMA
            or evidence.get("generator") not in {
                "run_lookbook_gate_com.ps1:ApplyComposition",
                "run_lookbook_gate_com.ps1:ApplyCompositionDelta",
            }
            or evidence.get("session_id") != state["session_id"]
            or not isinstance(evidence.get("master"), dict)
            or not same_identity(evidence["master"], master)
        ):
            continue
        for item in evidence.get("items", []):
            if isinstance(item, dict) and item.get("look_id") == look_id:
                return item
    return None


def _reconcile_caption_priors_with_current_master(project: Path, state: dict[str, Any], corrections: list[dict[str, Any]]) -> None:
    """Let a retry plan name a pre-existing, same-master credits position.

    This records a temporary *prior* geometry only when a previous native plan
    was applied and a newer plan now replaces it.  It does not bless an
    arbitrary document edit: the geometry must come from native evidence bound
    to the exact master that remains on disk.
    """
    for correction in corrections:
        look_id = correction.get("look_id")
        if not isinstance(look_id, str):
            fail("Caption-clearance correction plan contains an invalid look id.")
        item = _current_master_composition_item(project, state, look_id)
        caption = item.get("caption_correction") if isinstance(item, dict) else None
        actual_raw = caption.get("after_frame_bounds") if isinstance(caption, dict) else None
        if not isinstance(actual_raw, list):
            continue
        actual = _finite_bounds(actual_raw, f"{look_id} native prior_frame_bounds")
        before = _finite_bounds(correction.get("from_frame_bounds"), f"{look_id} from_frame_bounds")
        after = _finite_bounds(correction.get("to_frame_bounds"), f"{look_id} to_frame_bounds")
        if _same_bounds(actual, after):
            correction.pop("prior_frame_bounds", None)
        elif not _same_bounds(actual, before):
            correction["prior_frame_bounds"] = actual


def command_reconcile_clearance_plan_priors(args: argparse.Namespace) -> None:
    """Repair an unstarted retry plan using only existing native evidence.

    A visual retry can replace a prior delta correction.  Older releases only
    recognised ``ApplyComposition`` evidence, not ``ApplyCompositionDelta``,
    and therefore omitted the already-saved frame position from the new plan.
    This reconciliation is deliberately pre-flight only: it refuses a durable
    delta checkpoint and never opens or changes InDesign.
    """
    project = project_path(args.project)
    state = load_state(project)
    if current_gate(project, state) != "visual":
        fail("Clearance-plan reconciliation can run only at the visual gate.")
    if any(visual_confirmation_dir(project).glob("*.json")):
        fail("Clearance-plan reconciliation cannot alter a visually confirmed master; begin a revision.")
    plan = composition_plan_path(project)
    correction_path = clearance_correction_plan_path(project)
    if not plan.is_file() or not correction_path.is_file():
        print("PASS visual resume: no signed clearance retry plan needs reconciliation.")
        return
    if progress_file(project, "composition-delta").exists():
        fail("Clearance retry already has a durable composition checkpoint; resume it without changing its signed plan.")
    validate_composition_plan(project, state)
    data = read_json(correction_path)
    corrections = data.get("caption_corrections", [])
    if not isinstance(corrections, list):
        fail("Caption-clearance correction plan has invalid caption_corrections.")
    before = json.dumps(corrections, ensure_ascii=False, sort_keys=True)
    _reconcile_caption_priors_with_current_master(project, state, corrections)
    after = json.dumps(corrections, ensure_ascii=False, sort_keys=True)
    if before != after:
        data["caption_corrections"] = corrections
        data["created_at"] = utc_now()
        write_json(correction_path, data)
        print("PASS visual resume: signed prior credits geometry was restored from same-master native composition evidence.")
    else:
        print("PASS visual resume: existing signed composition plan is already consistent with the saved master.")


def command_plan_clearance_corrections(args: argparse.Namespace) -> None:
    """Create an ordered safe correction plan without creating a frame."""
    project = project_path(args.project)
    state = load_state(project)
    if current_gate(project, state) != "visual":
        fail(f"Clearance corrections can run only at the visual gate; next required gate is {current_gate(project, state) or 'complete'}.")
    confirmations = visual_confirmation_dir(project)
    if confirmations.exists() and any(confirmations.glob("*.json")):
        fail("Visual confirmations already exist. Begin a revision instead of changing a confirmed master.")
    forced_looks = {item.strip() for item in str(getattr(args, "force_looks", "") or "").split(",") if item.strip()}
    if any(not re.fullmatch(r"LOOK_\d{3}", item) for item in forced_looks):
        fail("--force-looks must be a comma-separated list of exact LOOK_### ids.")
    report_items = _validate_blocked_clearance_report(project, state)
    report_ids = {str(item["look_id"]) for item in report_items}
    unknown_forced = sorted(forced_looks - report_ids)
    if unknown_forced:
        fail("--force-looks contains unknown visual proofs: " + ", ".join(unknown_forced))
    if any(item["status"] == CAPTION_CLEARANCE_OVERFLOW for item in report_items):
        fail("Credits overflow in the current proof. Return to captions; a horizontal image shift cannot repair missing text.")
    try:
        _layout, layout_items = _validate_caption_clearance_layout(project, state)
    except GateError:
        layout_items = {}
        master_identity = identity(state_artifact(project, state, "master"))
        for candidate in sorted((control_path(project) / "history").glob("visual-clearance-*/visual/caption-clearance-layout.json"), key=lambda path: path.stat().st_mtime, reverse=True):
            try:
                archived_layout = read_json(candidate)
            except GateError:
                continue
            if archived_layout.get("session_id") != state["session_id"] or not same_identity(archived_layout.get("master", {}), master_identity):
                continue
            for item in archived_layout.get("items", []):
                if isinstance(item, dict) and isinstance(item.get("look_id"), str):
                    layout_items[item["look_id"]] = item
            if layout_items:
                break
        if not layout_items:
            fail("No current-master native layout is available for targeted safe-area calibration.")
    # A retry begins from the current saved master. Preserve every earlier
    # verified existing-frame correction that is no longer blocked, otherwise a
    # later native batch would correctly restore that frame to baseline and
    # accidentally undo a previously cleared look.
    prior_caption_corrections = _caption_corrections_for_current_plan(project, state)
    plan_path = composition_plan_path(project)
    with plan_path.open(newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source, delimiter="\t")
        if reader.fieldnames != COMPOSITION_PLAN_FIELDS:
            fail("Composition plan has unexpected columns.")
        plan_rows = [{key: (value or "").strip() for key, value in row.items()} for row in reader]
    if len(plan_rows) != int(state["look_count"]):
        fail("Composition plan does not cover every required look.")
    report_by_look = {str(item["look_id"]): item for item in report_items}
    corrections: list[dict[str, Any]] = []
    caption_corrections: list[dict[str, Any]] = [dict(value) for _look_id, value in sorted(prior_caption_corrections.items())]
    caption_correction_by_look: dict[str, int] = {item["look_id"]: index for index, item in enumerate(caption_corrections)}
    unresolved: list[dict[str, Any]] = []
    for row in plan_rows:
        evidence = report_by_look.get(row["look_id"])
        if evidence is None:
            fail(f"{row['look_id']}: caption-clearance report is missing.")
        force_correction = row["look_id"] in forced_looks
        if evidence["status"] == CAPTION_CLEARANCE_CLEAR and not force_correction:
            continue
        if evidence["status"] not in {CAPTION_CLEARANCE_COLLISION, CAPTION_CLEARANCE_REVIEW}:
            fail(f"{row['look_id']}: unsupported clearance state {evidence['status']}.")
        try:
            prior = float(row["photo_adjustment_points"])
        except (TypeError, ValueError):
            fail(f"{row['look_id']}: current crop plan is invalid.")
        source = child_of(state_artifact(project, state, "hires"), evidence["source_filename"])
        # The pixel audit must use the actually rendered text rectangle, whereas
        # an existing-frame move must preserve the full text-frame geometry. Do
        # not replace one with the other: a tall frame may have harmless space
        # below the last SKU, but its top/left/bottom/right are still the only
        # legal object bounds to carry into InDesign.
        frame_geometry = dict(layout_items[row["look_id"]])
        current_frame_bounds = _finite_bounds(frame_geometry["caption_bounds"], f"{row['look_id']} current credits frame")
        prior_caption_correction = prior_caption_corrections.get(row["look_id"])
        planning_frame_geometry = dict(frame_geometry)
        planning_frame_bounds = current_frame_bounds
        if prior_caption_correction is not None:
            # A previously applied right/down correction may itself be what
            # needs recalibration (for example after a newly enforced safe
            # area).  Plan from its immutable captions-stage geometry, not
            # from the already displaced frame, so the permitted target remains
            # a positive right/down move within the same existing page.
            planning_frame_bounds = _finite_bounds(
                prior_caption_correction.get("from_frame_bounds"),
                f"{row['look_id']} original credits frame",
            )
            planning_frame_geometry["caption_bounds"] = planning_frame_bounds
        geometry = dict(frame_geometry)
        visible_bounds = (evidence.get("metrics") or {}).get("visible_text_bounds")
        if isinstance(visible_bounds, list) and len(visible_bounds) == 4:
            geometry["caption_bounds"] = visible_bounds
        planning_visible_bounds = visible_bounds if isinstance(visible_bounds, list) else geometry["caption_bounds"]
        if planning_frame_bounds != current_frame_bounds:
            delta_y = planning_frame_bounds[0] - current_frame_bounds[0]
            delta_x = planning_frame_bounds[1] - current_frame_bounds[1]
            planning_visible_bounds = [
                float(planning_visible_bounds[0]) + delta_y,
                float(planning_visible_bounds[1]) + delta_x,
                float(planning_visible_bounds[2]) + delta_y,
                float(planning_visible_bounds[3]) + delta_x,
            ]
        proposed, predicted = _best_horizontal_clearance_shift(source, geometry, prior)
        candidate = {
            "look_id": row["look_id"], "from_points": prior, "to_points": proposed,
            "status": CAPTION_CLEARANCE_REVIEW if force_correction else evidence["status"],
            "coverage": float((evidence.get("metrics") or {}).get("coverage", 1.0)),
            "predicted_status": predicted["status"], "predicted_coverage": predicted.get("coverage"),
        }
        # Never send a knowingly blocked crop to InDesign.  It would consume a
        # full export cycle and still be rejected by the final source-pixel
        # audit.  Record it for a precise template decision, while applying all
        # independently safe corrections in the same run.
        safe_area_review = str(evidence.get("reason", "")) == "credits frame lies outside the page safe area"
        if safe_area_review or predicted["status"] != CAPTION_CLEARANCE_CLEAR or abs(proposed - prior) <= 0.001:
            caption_candidate = _best_caption_frame_clearance(source, planning_frame_geometry, _finite_bounds(
                planning_visible_bounds,
                f"{row['look_id']} visible_text_bounds",
            ))
            if caption_candidate is None:
                caption_candidate = _best_combined_caption_clearance(
                    source,
                    planning_frame_geometry,
                    _finite_bounds(
                        planning_visible_bounds,
                        f"{row['look_id']} visible_text_bounds",
                    ),
                    prior,
                    proposed,
                )
            if caption_candidate is None:
                candidate["reason"] = "no permissible horizontal crop, credits move, or combined photo-and-credits move clears the existing page"
                unresolved.append(candidate)
                continue
            candidate["reason"] = "photo crop does not safely resolve the rendered collision; use the approved existing-frame fallback"
            candidate["caption_fallback"] = caption_candidate["mode"]
            combined_shift = caption_candidate.get("photo_adjustment_points")
            if isinstance(combined_shift, (int, float)) and abs(float(combined_shift) - prior) > 0.001:
                row["photo_adjustment_points"] = f"{float(combined_shift):.2f}".rstrip("0").rstrip(".")
                corrections.append({
                    **candidate,
                    "to_points": float(combined_shift),
                    "predicted_status": CAPTION_CLEARANCE_CLEAR,
                    "predicted_coverage": caption_candidate.get("predicted_coverage"),
                    "reason": "combined horizontal photo crop and approved existing-frame credits correction",
                })
            correction = {"look_id": row["look_id"], **caption_candidate}
            if row["look_id"] in caption_correction_by_look:
                caption_corrections[caption_correction_by_look[row["look_id"]]] = correction
            else:
                caption_correction_by_look[row["look_id"]] = len(caption_corrections)
                caption_corrections.append(correction)
            continue
        row["photo_adjustment_points"] = f"{proposed:.2f}".rstrip("0").rstrip(".")
        corrections.append(candidate)
    if not corrections and not caption_corrections:
        details = ", ".join(item["look_id"] for item in unresolved) or "none"
        fail("No blocked credits area has a safe permitted horizontal correction. Unresolved looks: " + details)
    _reconcile_caption_priors_with_current_master(project, state, caption_corrections)
    archive = _archive_visual_cycle_for_clearance_retry(project)
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = plan_path.with_suffix(plan_path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=COMPOSITION_PLAN_FIELDS, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(plan_rows)
    os.replace(temporary, plan_path)
    write_json(clearance_correction_plan_path(project), {
        "schema": SCHEMA, "generator": "lookbook_gate.py:plan-clearance-corrections", "session_id": state["session_id"],
        "created_at": utc_now(), "prior_proof_archive": _relative_project_path(project, archive),
        "corrections": corrections, "caption_corrections": caption_corrections, "unresolved": unresolved,
    })
    message = f"CLEARANCE CORRECTION PLAN: {len(corrections)} left graphics will move horizontally inside their existing fixed containers."
    if caption_corrections:
        message += f" {len(caption_corrections)} existing credits frames will use the approved downward/right fallback."
    if unresolved:
        message += " Unresolved after every permitted photo and existing-credits-frame correction: " + ", ".join(item["look_id"] for item in unresolved) + "."
    print(message)


def command_calibrate_safe_area(args: argparse.Namespace) -> None:
    """Replan one already-applied credits correction inside the native safe area.

    This is a targeted calibration path: it reuses the newest current-master
    per-look proof from control history, reads fresh InDesign coordinates, and
    changes only the named visual-plan row.  It never re-renders all 102 pages.
    """
    project = project_path(args.project)
    state = load_state(project)
    look_id = str(args.look)
    if current_gate(project, state) != "visual" or not re.fullmatch(r"LOOK_\d{3}", look_id):
        fail("Safe-area calibration requires the visual gate and an exact LOOK_### id.")
    if any(visual_confirmation_dir(project).glob("*.json")):
        fail("Safe-area calibration cannot alter a visually confirmed master; begin a revision.")
    # The active retry plan can intentionally differ from the saved master
    # until its next composition batch.  Reopening InDesign here would make the
    # native baseline reject that harmless intermediate state.  Use the fresh
    # native coordinate snapshot already recorded for this unchanged master.
    try:
        _layout, layout_items = _validate_caption_clearance_layout(project, state)
    except GateError:
        layout_items = {}
        master_identity = identity(state_artifact(project, state, "master"))
        for candidate in sorted((control_path(project) / "history").glob("visual-clearance-*/visual/caption-clearance-layout.json"), key=lambda path: path.stat().st_mtime, reverse=True):
            try:
                archived_layout = read_json(candidate)
            except GateError:
                continue
            if archived_layout.get("session_id") != state["session_id"] or not same_identity(archived_layout.get("master", {}), master_identity):
                continue
            for item in archived_layout.get("items", []):
                if isinstance(item, dict) and isinstance(item.get("look_id"), str):
                    layout_items[item["look_id"]] = item
            if layout_items:
                break
        if not layout_items:
            fail("No current-master native layout is available for targeted safe-area calibration.")
    current_item = layout_items.get(look_id)
    if current_item is None:
        fail(f"{look_id}: native clearance layout is missing.")
    archived: dict[str, Any] | None = None
    for candidate in sorted((control_path(project) / "history").glob("visual-clearance-*/visual/caption-clearance.json"), key=lambda path: path.stat().st_mtime, reverse=True):
        try:
            report = read_json(candidate)
        except GateError:
            continue
        if report.get("session_id") != state["session_id"] or not same_identity(report.get("master", {}), identity(state_artifact(project, state, "master"))):
            continue
        archived = next((item for item in report.get("items", []) if item.get("look_id") == look_id), None)
        if isinstance(archived, dict):
            break
        archived = None
    if archived is None:
        fail(f"{look_id}: no current-master per-look proof is available for targeted safe-area calibration.")
    visible = (archived.get("metrics") or {}).get("visible_text_bounds")
    if not isinstance(visible, list) or len(visible) != 4:
        fail(f"{look_id}: archived proof has no visible credits bounds.")
    plan_path = composition_plan_path(project)
    with plan_path.open(newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source, delimiter="\t")
        if reader.fieldnames != COMPOSITION_PLAN_FIELDS:
            fail("Composition plan has unexpected columns.")
        rows = [{key: (value or "").strip() for key, value in row.items()} for row in reader]
    row = next((entry for entry in rows if entry["look_id"] == look_id), None)
    if row is None:
        fail(f"{look_id}: composition row is missing.")
    correction_path = clearance_correction_plan_path(project)
    correction_plan = read_json(correction_path)
    if correction_plan.get("session_id") != state["session_id"]:
        fail("Current clearance-correction plan belongs to another session.")
    corrections = list(correction_plan.get("caption_corrections") or [])
    previous = next((item for item in corrections if item.get("look_id") == look_id), None)
    if not isinstance(previous, dict):
        fail(f"{look_id}: no existing credits correction is available to calibrate.")
    current_frame = _finite_bounds(current_item["caption_bounds"], f"{look_id} current credits frame")
    original_frame = _finite_bounds(previous.get("from_frame_bounds"), f"{look_id} original credits frame")
    visible_base = [
        float(visible[0]) + original_frame[0] - current_frame[0],
        float(visible[1]) + original_frame[1] - current_frame[1],
        float(visible[2]) + original_frame[0] - current_frame[0],
        float(visible[3]) + original_frame[1] - current_frame[1],
    ]
    geometry = dict(current_item); geometry["caption_bounds"] = original_frame
    native_item = _current_master_composition_item(project, state, look_id)
    if native_item is None:
        fail(f"{look_id}: no same-master native composition record is available for safe-area calibration.")
    try:
        native_before = _finite_bounds(native_item.get("before_left_graphic_bounds"), f"{look_id} native left graphic before")
        native_after = _finite_bounds(native_item.get("after_left_graphic_bounds"), f"{look_id} native left graphic after")
    except GateError:
        raise
    if abs((native_after[1] - native_before[1]) - (native_after[3] - native_before[3])) > 0.1:
        fail(f"{look_id}: native composition record has an inconsistent horizontal image shift.")
    prior = native_after[1] - native_before[1]
    source_image = child_of(state_artifact(project, state, "hires"), str(archived["source_filename"]))
    proposed, _predicted = _best_horizontal_clearance_shift(source_image, {**current_item, "caption_bounds": visible}, prior)
    candidate = _best_caption_frame_clearance(source_image, geometry, visible_base)
    if candidate is None:
        candidate = _best_combined_caption_clearance(source_image, geometry, visible_base, prior, proposed)
    if candidate is None:
        fail(f"{look_id}: no clear credits position exists inside the page safe area.")
    target_shift = float(candidate.get("photo_adjustment_points", prior))
    row["photo_adjustment_points"] = f"{target_shift:.2f}".rstrip("0").rstrip(".")
    # `current_frame` is a native snapshot from this unchanged master.  The
    # earlier plan target may never have been saved, so using it here was the
    # source of the false baseline block that left a calculated correction
    # unapplied.  Carry the actual saved position as the permitted prior state.
    replacement = {"look_id": look_id, **candidate, "prior_frame_bounds": current_frame}
    correction_plan["caption_corrections"] = [item for item in corrections if item.get("look_id") != look_id] + [replacement]
    _reconcile_caption_priors_with_current_master(project, state, correction_plan["caption_corrections"])
    correction_plan["corrections"] = [item for item in correction_plan.get("corrections", []) if item.get("look_id") != look_id] + [{
        "look_id": look_id, "from_points": prior, "to_points": target_shift,
        "status": "REVIEW", "predicted_status": CAPTION_CLEARANCE_CLEAR,
        "predicted_coverage": candidate.get("predicted_coverage"),
        "reason": "targeted safe-area calibration",
    }]
    correction_plan["unresolved"] = [item for item in correction_plan.get("unresolved", []) if item.get("look_id") != look_id]
    # Keep the native plan contract stable: the correction itself carries the
    # calibration evidence, while InDesign accepts the same signed plan shape
    # as its ordinary clearance planner.
    correction_plan["generator"] = "lookbook_gate.py:plan-clearance-corrections"; correction_plan["created_at"] = utc_now()
    temporary = plan_path.with_suffix(plan_path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=COMPOSITION_PLAN_FIELDS, delimiter="\t", lineterminator="\n")
        writer.writeheader(); writer.writerows(rows)
    os.replace(temporary, plan_path)
    write_json(correction_path, correction_plan)
    print(f"SAFE-AREA CALIBRATION: {look_id} photo={target_shift:g} credits={candidate['mode']} dx={candidate['delta_x']:g} dy={candidate['delta_y']:g}.")


def _load_current_look_calibration(project: Path, state: dict[str, Any], look_id: str) -> dict[str, Any]:
    """Validate one native calibration record against the saved master and plan."""
    root = control_path(project) / "visual" / "calibration"
    candidates = sorted(root.glob(f"{look_id}-*.json"), key=lambda path: path.stat().st_mtime, reverse=True) if root.is_dir() else []
    master = identity(state_artifact(project, state, "master"))
    plan_hash = digest(composition_plan_path(project))
    correction_path = clearance_correction_plan_path(project)
    correction_hash = digest(correction_path) if correction_path.is_file() else None
    for path in candidates:
        try:
            record = read_json(path)
        except GateError:
            continue
        if (
            record.get("schema") != SCHEMA
            or record.get("generator") != "run_lookbook_gate_com.ps1:ApplyLookCalibration"
            or record.get("session_id") != state["session_id"]
            or record.get("look_id") != look_id
            or record.get("composition_plan_sha256") != plan_hash
            or record.get("clearance_correction_plan_sha256") != correction_hash
            or not isinstance(record.get("master"), dict)
            or not same_identity(record["master"], master)
        ):
            continue
        correction = record.get("caption_correction")
        if not isinstance(correction, dict) or not isinstance(correction.get("after_frame_bounds"), list):
            continue
        _finite_bounds(correction["after_frame_bounds"], f"{look_id} calibrated credits bounds")
        _finite_bounds(record.get("before_left_graphic_bounds"), f"{look_id} calibrated graphic baseline")
        _finite_bounds(record.get("after_left_graphic_bounds"), f"{look_id} calibrated graphic result")
        _finite_bounds(record.get("interior_safe_bounds"), f"{look_id} calibrated interior safe area")
        if record.get("caption_overflows") is not False:
            continue
        return record
    fail(f"{look_id}: no current native calibration record is available for this saved master and plan.")


def command_apply_safe_area_calibration(args: argparse.Namespace) -> None:
    """Apply exactly one signed safe-area correction through native InDesign."""
    project = project_path(args.project)
    state = load_state(project)
    look_id = str(args.look)
    if current_gate(project, state) != "visual" or not re.fullmatch(r"LOOK_\d{3}", look_id):
        fail("Targeted safe-area application requires the visual gate and an exact LOOK_### id.")
    if any(visual_confirmation_dir(project).glob("*.json")):
        fail("Targeted safe-area application cannot alter a visually confirmed master; begin a revision.")
    validate_composition_plan(project, state)
    corrections = _caption_corrections_for_current_plan(project, state)
    if look_id not in corrections:
        fail(f"{look_id}: no signed existing-frame correction is available to apply.")
    run_com_driver(["-Action", "ApplyLookCalibration", "-Project", str(project), "-LookId", look_id], timeout_seconds=240)
    record = _load_current_look_calibration(project, state, look_id)
    target = corrections[look_id]["to_frame_bounds"]
    actual = _finite_bounds(record["caption_correction"]["after_frame_bounds"], f"{look_id} applied credits bounds")
    if not _same_bounds(actual, target):
        fail(f"{look_id}: native calibration did not retain the signed credits target.")
    print(f"PASS safe-area calibration: {look_id} alone was saved with its credits inside the interior safe zone.")


def command_render_safe_area_preview(args: argparse.Namespace) -> None:
    """Export and pixel-check only the two pages of one calibrated look."""
    project = project_path(args.project)
    state = load_state(project)
    look_id = str(args.look)
    if current_gate(project, state) != "visual" or not re.fullmatch(r"LOOK_\d{3}", look_id):
        fail("Targeted preview requires the visual gate and an exact LOOK_### id.")
    record = _load_current_look_calibration(project, state, look_id)
    registry = validate_registry(
        state_artifact(project, state, "registry"), int(state["look_count"]), state_artifact(project, state, "hires")
    )
    row = next((item for item in registry if item["look_id"] == look_id), None)
    if row is None:
        fail(f"{look_id}: registry row is missing.")
    stamp = utc_now().replace(":", "-")
    root = control_path(project) / "visual" / "calibration-previews" / f"{look_id}-{stamp}"
    root.mkdir(parents=True, exist_ok=True)
    pdf = root / f"{look_id}.pdf"
    page_range = f"{row['indd_left_page']}-{row['indd_right_page']}"
    run_com_driver([
        "-Action", "ExportPdf", "-Project", str(project), "-Pdf", str(pdf),
        "-RasterResolution", "150", "-PageRange", page_range,
    ], timeout_seconds=240)
    pages, _samples = inspect_exported_pdf(pdf, 2, root / "pdf-samples")
    if pages != 2:
        fail(f"{look_id}: targeted preview did not contain exactly its two look pages.")
    try:
        from PIL import Image
        from pypdf import PdfReader
        import numpy as np
        from PIL import ImageOps
    except Exception as error:
        fail(f"Targeted preview dependencies are unavailable: {error}")
    rendered = render_pdf_pages(pdf, root / "rendered", [1, 2], 150)
    with Image.open(rendered[1]) as left, Image.open(rendered[2]) as right:
        canvas = Image.new("RGB", (left.width + right.width, max(left.height, right.height)), "white")
        canvas.paste(left.convert("RGB"), (0, 0)); canvas.paste(right.convert("RGB"), (left.width, 0))
        pair = root / f"{look_id}.jpg"
        canvas.save(pair, "JPEG", quality=93, optimize=True)
    caption_bounds = _finite_bounds(record["caption_correction"]["after_frame_bounds"], f"{look_id} calibrated caption bounds")
    interior = _finite_bounds(record["interior_safe_bounds"], f"{look_id} calibrated interior safe bounds")
    if (
        caption_bounds[0] < interior[0] - 0.5 or caption_bounds[1] < interior[1] - 0.5
        or caption_bounds[2] > interior[2] + 0.5 or caption_bounds[3] > interior[3] + 0.5
    ):
        fail(f"{look_id}: saved calibration sits outside its interior safe area.")
    reader = PdfReader(str(pdf), strict=True)
    visible_bounds, visible_fields = _visible_caption_text_bounds(reader, 1, caption_bounds)
    products = sum(1 for item in csv_rows(state_artifact(project, state, "captions"), CAPTION_FIELDS) if item["look_id"] == look_id)
    if visible_fields < products * 4:
        fail(f"{look_id}: targeted preview is missing rendered credit fields.")
    inspection = {
        "look_id": look_id,
        "graphic_bounds": _finite_bounds(record["after_left_graphic_bounds"], f"{look_id} calibrated graphic bounds"),
        "caption_bounds": visible_bounds,
    }
    source = child_of(state_artifact(project, state, "hires"), row["left_filename"])
    metrics, _crop, _mask = _caption_clearance_metrics(source, inspection, np, Image, ImageOps)
    if metrics["status"] != CAPTION_CLEARANCE_CLEAR:
        fail(f"{look_id}: targeted preview still intersects the model ({metrics['reason']}).")
    report = {
        "schema": CAPTION_CLEARANCE_SCHEMA, "generator": "lookbook_gate.py:render-safe-area-preview",
        "session_id": state["session_id"], "created_at": utc_now(), "master": identity(state_artifact(project, state, "master")),
        "look_id": look_id, "preview_pdf": _relative_project_path(project, pdf), "preview_image": _relative_project_path(project, pair),
        "safe_bounds": interior, "frame_bounds": caption_bounds, "visible_text_bounds": visible_bounds,
        "visible_text_field_count": visible_fields, "expected_text_field_count": products * 4, "clearance": metrics,
    }
    write_json(root / "report.json", report)
    print(f"PASS targeted preview: {look_id} has two rendered pages, visible credits, zero model overlap, and an interior-safe credits frame.")
    print(f"Preview image: {pair}")
    print(f"Preview PDF: {pdf}")


def command_prune_unsafe_clearance_corrections(args: argparse.Namespace) -> None:
    """Repair a pre-rule correction plan without touching the master document.

    Early controller runs could include a best-effort crop even when that crop
    was still predicted to collide.  This command is a transactional migration:
    it restores only those entries to their recorded absolute starting position
    and preserves the safe corrections.  It never moves a page, frame, or image
    in InDesign; the normal native composition action remains the sole writer.
    """
    project = project_path(args.project)
    state = load_state(project)
    if current_gate(project, state) != "visual":
        fail(f"Unsafe-plan pruning can run only at the visual gate; next required gate is {current_gate(project, state) or 'complete'}.")
    confirmations = visual_confirmation_dir(project)
    if confirmations.exists() and any(confirmations.glob("*.json")):
        fail("Visual confirmations already exist. Begin a revision instead of changing a confirmed master.")
    correction_path = control_path(project) / "visual" / "clearance-correction-plan.json"
    data = read_json(correction_path)
    if data.get("schema") != SCHEMA or data.get("session_id") != state["session_id"]:
        fail("Clearance correction plan is not bound to this project session.")
    raw = data.get("corrections")
    if not isinstance(raw, list) or not raw:
        fail("Clearance correction plan has no corrections to inspect.")
    plan_path = composition_plan_path(project)
    with plan_path.open(newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source, delimiter="\t")
        if reader.fieldnames != COMPOSITION_PLAN_FIELDS:
            fail("Composition plan has unexpected columns.")
        rows = [{key: (value or "").strip() for key, value in row.items()} for row in reader]
    by_look = {row["look_id"]: row for row in rows}
    safe: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = list(data.get("unresolved") or [])
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("look_id"), str):
            fail("Clearance correction plan contains an invalid entry.")
        row = by_look.get(item["look_id"])
        if row is None:
            fail(f"{item['look_id']}: correction plan does not match the composition plan.")
        if item.get("predicted_status") == CAPTION_CLEARANCE_CLEAR:
            safe.append(item)
            continue
        try:
            baseline = float(item["from_points"])
        except (KeyError, TypeError, ValueError):
            fail(f"{item['look_id']}: unsafe correction has no valid recorded baseline.")
        row["photo_adjustment_points"] = f"{baseline:.2f}".rstrip("0").rstrip(".")
        archived = dict(item)
        archived["reason"] = "no permissible horizontal crop clears the fixed credits area"
        unresolved.append(archived)
    temporary = plan_path.with_suffix(plan_path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=COMPOSITION_PLAN_FIELDS, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, plan_path)
    data["generator"] = "lookbook_gate.py:prune-unsafe-clearance-corrections"
    data["created_at"] = utc_now()
    data["corrections"] = safe
    data["unresolved"] = unresolved
    write_json(correction_path, data)
    print(f"CLEARANCE PLAN PRUNED: {len(safe)} safe horizontal corrections retained; {len(unresolved)} unresolvable entries remain locked for explicit template handling.")


def command_reset_composition(args: argparse.Namespace) -> None:
    """Return every placed left graphic to its normal fixed-frame fit, not a frame move."""
    project = project_path(args.project)
    state = load_state(project)
    if current_gate(project, state) != "visual":
        fail(f"Composition reset can run only at the visual gate; next required gate is {current_gate(project, state) or 'complete'}.")
    confirmations = visual_confirmation_dir(project)
    if confirmations.exists() and any(confirmations.glob("*.json")):
        fail("Visual confirmations already exist. Begin a revision instead of resetting a confirmed master.")
    plan_path = composition_plan_path(project)
    with plan_path.open(newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source, delimiter="\t")
        if reader.fieldnames != COMPOSITION_PLAN_FIELDS:
            fail("Composition plan has unexpected columns.")
        rows = [{key: (value or "").strip() for key, value in row.items()} for row in reader]
    if len(rows) != int(state["look_count"]):
        fail("Composition plan does not cover every required look.")
    archive = _archive_visual_cycle_for_clearance_retry(project)
    for row in rows:
        row["photo_adjustment_points"] = "0"
        row["plan_status"] = "READY"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = plan_path.with_suffix(plan_path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=COMPOSITION_PLAN_FIELDS, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, plan_path)
    write_json(control_path(project) / "visual" / "composition-reset.json", {
        "schema": SCHEMA, "generator": "lookbook_gate.py:reset-composition", "session_id": state["session_id"],
        "created_at": utc_now(), "prior_visual_archive": _relative_project_path(project, archive), "look_count": len(rows),
    })
    print(f"COMPOSITION RESET PLAN: {len(rows)} left graphics will return to their standard fixed-frame fit before the next visual audit.")


def command_apply_composition(args: argparse.Namespace) -> None:
    """Run only the declared source/crop corrections, in saved native batches."""
    project = project_path(args.project)
    state = load_state(project)
    if current_gate(project, state) != "visual":
        fail(f"Composition correction cannot run now; next required gate is {current_gate(project, state) or 'complete'}.")
    validate_composition_plan(project, state)
    confirmations = visual_confirmation_dir(project)
    if confirmations.exists() and any(confirmations.glob("*.json")):
        fail("Visual confirmations already exist. Do not change a confirmed master; begin a revision for a new visual cycle.")
    try:
        validate_composition_applied(project, state)
        print("PASS composition: the current plan is already applied to this saved master.")
        return
    except GateError:
        pass
    total = int(state["look_count"])
    # A clearance retry often changes only a few looks.  Rewriting all 50
    # already-proven spreads makes visual recovery needlessly slow and creates
    # extra COM exposure.  The delta worker retains the prior native evidence,
    # rechecks every unchanged look read-only and opens saved transactions only
    # for the exact photo/frame corrections in the newly signed plan.
    correction_path = clearance_correction_plan_path(project)
    correction = read_json(correction_path) if correction_path.exists() else {}
    archive_relative = str(correction.get("prior_proof_archive", "")).strip()
    delta_base = None
    if archive_relative:
        try:
            archive = child_of(project, archive_relative)
        except GateError:
            archive = None
        if archive is not None:
            candidate = archive / "visual" / "composition-applied.json"
            if candidate.is_file():
                delta_base = candidate
    if delta_base is not None:
        delta_progress_path = progress_file(project, "composition-delta")
        # The first targeted composition call has no checkpoint yet.  Absence
        # before the native worker starts is expected; only a missing record
        # *after* an incomplete worker return is evidence of a failed save.
        before = read_json(delta_progress_path) if delta_progress_path.exists() else {}
        before_count = len(before.get("completed_looks", [])) if isinstance(before.get("completed_looks"), list) else 0
        run_com_driver(["-Action", "ApplyCompositionDelta", "-Project", str(project), "-BatchSize", "4"], timeout_seconds=240)
        try:
            validate_composition_applied(project, state)
            print(f"PASS composition: targeted native corrections are recorded against all {total} fixed containers.")
            return
        except GateError:
            if not delta_progress_path.exists():
                fail("Targeted composition did not write a durable checkpoint or final evidence. The visual gate remains blocked.")
            after = read_json(delta_progress_path)
            completed = len(after.get("completed_looks", [])) if isinstance(after.get("completed_looks"), list) else 0
            if completed <= before_count:
                fail("Targeted composition batch did not advance durable progress. Retry apply-composition without changing the plan.")
            print(f"CHECKPOINT composition: {completed} targeted looks saved; unchanged spreads retain their verified native evidence.")
            return
    # Match image/caption semantics: one command performs exactly one saved
    # four-look transaction.  A full 50-look loop can outlive a shell wrapper,
    # which makes a second call indistinguishable from a duplicate native edit.
    # The next identical command resumes only after this checkpoint is durable.
    before = composition_progress(project, state)
    before_count = int(before.get("completed_count", 0)) if before else 0
    run_com_driver(["-Action", "ApplyComposition", "-Project", str(project), "-BatchSize", "4"], timeout_seconds=240)
    try:
        validate_composition_applied(project, state)
        print(f"PASS composition: {total} fixed containers were checked; declared source changes and horizontal shifts are recorded.")
        return
    except GateError:
        after = composition_progress(project, state)
        if after is None:
            fail("InDesign completed a composition batch without durable recovery progress. The visual gate remains blocked.")
        completed = int(after.get("completed_count", 0))
        if completed <= before_count:
            fail("Composition batch did not advance durable progress. Retry apply-composition without changing the plan.")
        print(f"CHECKPOINT composition: {completed}/{total} looks corrected and saved. Run the same command again after this checkpoint is visible.")


def command_repair_links(args: argparse.Namespace) -> None:
    """Reapply the frozen composition once to repair stale or missing live links."""
    project = project_path(args.project)
    state = load_state(project)
    if current_gate(project, state) != "visual":
        fail(f"Link repair can run only at the visual gate; next required gate is {current_gate(project, state) or 'complete'}.")
    validate_composition_plan(project, state)
    if any(visual_confirmation_dir(project).glob("LOOK_*.json")):
        fail("Visual confirmations already exist. Begin a revision instead of changing live links in a confirmed master.")
    # Existing composition evidence names the correct sources but cannot prove a
    # later manual relink still targets the project work area. The native worker
    # resets every declared graphic to its frozen source and rechecks link path
    # plus status before it writes replacement evidence.
    run_com_driver(["-Action", "ReapplyComposition", "-Project", str(project), "-BatchSize", "4"], timeout_seconds=300)
    validate_composition_applied(project, state)
    print("PASS links: every placed image was relinked or verified against this project's frozen hires path.")


def _archive_visual_evidence_after_content_repair(project: Path) -> Path:
    """Archive only stale visual evidence; keep the declared composition plan."""
    archive = control_path(project) / "history" / f"visual-content-repair-{utc_now().replace(':', '-')}"
    for relative in (
        "visual/composition-applied.json", "visual/proof", "visual/caption-clearance",
        "visual/caption-clearance-layout.json", "visual/caption-clearance.json",
        "visual/confirmations", "visual/clearance-correction-plan.json",
        "progress/composition.json", "evidence/visual.json", "evidence/release.json", "evidence/pdf.json",
    ):
        source = control_path(project) / relative
        if not source.exists():
            continue
        destination = archive / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
    return archive


def command_repair_captions(args: argparse.Namespace) -> None:
    """Restore frozen caption data and exact existing credit styles at visual stage."""
    project = project_path(args.project)
    state = load_state(project)
    if current_gate(project, state) != "visual":
        fail(f"Caption repair can run only at the visual gate; next required gate is {current_gate(project, state) or 'complete'}.")
    if any(visual_confirmation_dir(project).glob("LOOK_*.json")):
        fail("Visual confirmations already exist. Begin a revision instead of changing a confirmed master.")
    arm = read_json(arm_file(project, "visual"))
    total = int(state["look_count"])
    progress_path = progress_file(project, "caption-repair")
    # Before the first saved repair batch the current composition proof must be
    # exact. Once repair has a valid durable checkpoint, the master is expected
    # to differ from that old proof; resume from the checkpoint instead.
    resume = None
    if progress_path.exists():
        candidate = read_json(progress_path)
        if (
            candidate.get("schema") == SCHEMA
            and candidate.get("session_id") == state["session_id"]
            and candidate.get("gate") == "visual-caption-repair"
            and candidate.get("nonce") == arm.get("nonce")
            and int(candidate.get("completed_count", 0)) > 0
        ):
            resume = candidate
    if resume is None:
        validate_composition_applied(project, state)
    elif not same_identity(resume.get("master", {}), identity(state_artifact(project, state, "master"))):
        fail("Caption-repair checkpoint does not match the current saved master.")
    for _ in range(total):
        repair_path = control_path(project) / "visual" / "caption-repair.json"
        if repair_path.exists():
            repair = read_json(repair_path)
            if repair.get("session_id") != state["session_id"] or not same_identity(repair.get("master", {}), identity(state_artifact(project, state, "master"))):
                fail("Caption repair did not produce current-master native evidence.")
            geometry = control_path(project) / "evidence" / "caption-geometry.json"
            if not geometry.exists():
                fail("Caption repair passed without controlled credits-frame geometry evidence.")
            archive = _archive_visual_evidence_after_content_repair(project)
            # The operation's own durable evidence is the one visual artifact that
            # must remain live after old proof is archived.
            repaired = archive / "visual" / "caption-repair.json"
            if repaired.exists():
                target = control_path(project) / "visual" / "caption-repair.json"
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(repaired), str(target))
            print("PASS caption repair: frozen credits and their exact field styles were restored; stale visual proof archived at " + _relative_project_path(project, archive) + ". Reapply composition before the next proof.")
            return
        before = 0
        if progress_path.exists():
            candidate = read_json(progress_path)
            if (
                candidate.get("schema") == SCHEMA
                and candidate.get("session_id") == state["session_id"]
                and candidate.get("gate") == "visual-caption-repair"
                and candidate.get("nonce") == arm.get("nonce")
            ):
                before = int(candidate.get("completed_count", 0))
        try:
            run_com_driver(["-Action", "RepairCaptions", "-Project", str(project), "-BatchSize", "4"], timeout_seconds=300)
        except GateError:
            # InDesign can drop a COM client after a saved batch while it is
            # enumerating the next collection. A durable checkpoint is the
            # authority: if it advanced, continue from it automatically rather
            # than making an operator rerun a safe, resumable repair.
            if progress_path.exists():
                recovered = read_json(progress_path)
                if (
                    recovered.get("schema") == SCHEMA
                    and recovered.get("session_id") == state["session_id"]
                    and recovered.get("gate") == "visual-caption-repair"
                    and recovered.get("nonce") == arm.get("nonce")
                    and int(recovered.get("completed_count", 0)) > before
                ):
                    print(f"CHECKPOINT caption repair: {int(recovered['completed_count'])}/{total} looks saved before native reconnect; continuing automatically.")
                    continue
            if restart_caption_repair_indesign(project, state):
                print("RECOVERY caption repair: restarted the saved, document-free InDesign worker; continuing automatically.")
                continue
            raise
        if not progress_path.exists():
            fail("Caption-repair batch returned without durable progress or PASS evidence.")
        after = read_json(progress_path)
        if (
            after.get("schema") != SCHEMA
            or after.get("session_id") != state["session_id"]
            or after.get("gate") != "visual-caption-repair"
            or after.get("nonce") != arm.get("nonce")
        ):
            fail("Caption-repair progress does not belong to this visual session.")
        completed = int(after.get("completed_count", 0))
        if completed <= before:
            fail("Caption-repair batch did not advance durable progress. Retry without re-arming.")
        print(f"CHECKPOINT caption repair: {completed}/{total} looks saved and verified.")
    fail("Caption-repair batch safety limit reached before full verification. Retry without re-arming.")


def command_restore_failed_caption_repair(args: argparse.Namespace) -> None:
    """Restore archived visual evidence only when an unsaved repair left the master unchanged."""
    project = project_path(args.project)
    state = load_state(project)
    if current_gate(project, state) != "visual":
        fail("Failed caption-repair evidence can be restored only at the visual gate.")
    if composition_applied_path(project).exists() or visual_proof_manifest_path(project).exists():
        fail("Live visual evidence already exists; refusing to overwrite it during recovery.")
    candidates = sorted((control_path(project) / "history").glob("visual-content-repair-*"), key=lambda item: item.stat().st_mtime, reverse=True)
    if not candidates:
        fail("No caption-repair recovery archive exists.")
    archive = candidates[0]
    archived_applied = archive / "visual" / "composition-applied.json"
    if not archived_applied.exists():
        fail("Latest caption-repair archive lacks composition evidence.")
    evidence = read_json(archived_applied)
    if not same_identity(evidence.get("master", {}), identity(state_artifact(project, state, "master"))):
        fail("Master changed during the failed repair; archived visual evidence must not be restored.")
    restored = 0
    for source in sorted(archive.rglob("*"), key=lambda item: len(item.parts)):
        if not source.is_file():
            continue
        relative = source.relative_to(archive)
        if relative.parts[0] not in {"visual", "progress", "evidence"}:
            continue
        target = control_path(project) / relative
        if target.exists():
            fail(f"Recovery target already exists: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        restored += 1
    validate_composition_applied(project, state)
    validate_visual_proof(project, state)
    print(f"RESTORED FAILED CAPTION REPAIR: {restored} current-master visual evidence files returned from {_relative_project_path(project, archive)}.")


def command_export_pdf(args: argparse.Namespace) -> None:
    project = project_path(args.project)
    state = load_state(project)
    if current_gate(project, state) != "pdf":
        fail(f"PDF export is BLOCKED. Next required gate is {current_gate(project, state) or 'complete'}.")
    permit = read_json(control_path(project) / "export-permit.json")
    if permit.get("session_id") != state["session_id"] or parse_utc(permit["expires_at"]) < datetime.now(timezone.utc):
        fail("Export permit is missing, belongs to another session, or expired. Run pre-export again.")
    master = state_artifact(project, state, "master")
    if not same_identity(permit.get("master", {}), identity(master)):
        fail("Master changed after the export permit. Re-audit release before exporting again.")
    pdf = child_of(project, args.pdf)
    if os.path.normcase(str(pdf)) != os.path.normcase(str(Path(permit["destination"]).resolve())):
        fail("PDF path does not match the issued export permit.")
    if pdf.exists():
        fail("Permitted PDF already exists. Issue a new permit with --quarantine-existing before exporting.")
    run_com_driver(["-Action", "ExportPdf", "-Project", str(project), "-Pdf", str(pdf)])
    if not pdf.exists() or pdf.stat().st_size < 512:
        fail("InDesign reported success but did not create a usable PDF. The permit stays unverified.")
    print(f"PDF EXPORTED: {pdf}")


def command_confirm(args: argparse.Namespace) -> None:
    project = project_path(args.project)
    state = load_state(project)
    gate = args.gate
    if gate not in GATES:
        fail(f"Unknown gate: {gate}")
    for predecessor in GATES[:GATES.index(gate)]:
        if load_evidence(project, state, predecessor) is None:
            fail(f"Cannot confirm {gate}; predecessor {predecessor} has no current PASS evidence.")
    evidence = load_evidence(project, state, gate)
    if evidence is None:
        fail(f"No current PASS evidence for {gate}. Run its audit against the saved current master.")
    if gate == "captions":
        count = validate_verified_caption_inputs(project, state)
        print(f"PASS captions: evidence accepted; source contains {count} workbook-derived product rows.")
    else:
        print(f"PASS {gate}: evidence accepted.")


def command_record_visual(args: argparse.Namespace) -> None:
    project = project_path(args.project)
    state = load_state(project)
    if current_gate(project, state) != "visual":
        fail(f"Visual proof cannot be recorded now; next required gate is {current_gate(project, state) or 'complete'}.")
    armed = read_json(arm_file(project, "visual"))
    plan = composition_plan_path(project)
    applied = composition_applied_path(project)
    proof = visual_proof_manifest_path(project)
    validate_composition_plan(project, state)
    validate_composition_applied(project, state)
    manifest = validate_visual_proof(project, state)
    clearance = validate_caption_clearance(project, state)
    validate_visual_confirmations(project, state)
    if len(args.notes.strip()) < 20:
        fail("--notes must state what was checked (at least 20 characters).")
    master = state_artifact(project, state, "master")
    evidence = {
        "schema": SCHEMA, "session_id": state["session_id"], "gate": "visual", "passed": True,
        "nonce": armed["nonce"], "created_at": utc_now(), "master": identity(master),
        "overview": manifest["overview"], "composition_plan_sha256": digest(plan),
        "composition_applied_sha256": digest(applied), "visual_proof_manifest_sha256": digest(proof),
        "caption_clearance_sha256": digest(caption_clearance_report_path(project)),
        "caption_clearance_fingerprint": hashlib.sha256(
            "\n".join(f"{item['look_id']}:{_caption_clearance_item_fingerprint(item)}" for item in clearance["items"]).encode("utf-8")
        ).hexdigest(),
        "visual_confirmation_fingerprint": visual_confirmation_fingerprint(project, state), "notes": args.notes.strip(),
    }
    write_json(evidence_file(project, "visual"), evidence)
    print("PASS visual: every look was confirmed against its current-master rendered pair; composition evidence is stored.")


def command_pre_export(args: argparse.Namespace) -> None:
    project = project_path(args.project)
    assert_project_root_clean(project)
    state = load_state(project)
    if current_gate(project, state) != "pdf":
        fail(f"Export is BLOCKED. Next required gate is {current_gate(project, state) or 'complete'}.")
    master = state_artifact(project, state, "master")
    destination = child_of(project, args.pdf)
    if destination.suffix.lower() != ".pdf":
        fail("Export destination must end in .pdf.")
    if destination.exists():
        if not args.quarantine_existing:
            fail("Destination already exists. Use --quarantine-existing so an old PDF cannot be mistaken for this build.")
        stamped = utc_now().replace(":", "-")
        quarantine = control_path(project) / "quarantine" / f"{destination.stem}-{stamped}{destination.suffix}"
        shutil.move(str(destination), str(quarantine))
    nonce = str(uuid.uuid4())
    arm = {"schema": SCHEMA, "session_id": state["session_id"], "gate": "pdf", "nonce": nonce, "armed_at": utc_now()}
    write_json(arm_file(project, "pdf"), arm)
    permit = {
        "schema": SCHEMA, "session_id": state["session_id"], "nonce": nonce, "issued_at": utc_now(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=45)).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "master": identity(master), "expected_pages": state["expected_pages"], "destination": str(destination),
        "release_evidence_sha256": digest(evidence_file(project, "release")),
    }
    write_json(control_path(project) / "export-permit.json", permit)
    print("EXPORT PERMIT ISSUED")
    print(f"Export only to: {destination}")
    print("After export, run verify-pdf. Do not change or resave the master between these two commands.")


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def render_pdf_pages(pdf: Path, destination: Path, pages: list[int], resolution: int = 150) -> dict[int, Path]:
    """Rasterise named PDF pages through the bundled Poppler binary.

    The controlled runtime intentionally contains no PyMuPDF/fitz.  Poppler is
    bundled with Codex and is used here for every proof and PDF verification,
    so the visual gate has no undeclared Python-package dependency.
    """
    # The PATH wrapper is a .cmd shim.  Python's direct subprocess call can
    # invoke that shim without its nested Poppler path on some Windows hosts,
    # so prefer the verified executable bundled beside this runtime.
    bundled_renderer = Path(sys.executable).resolve().parent.parent / "native" / "poppler" / "Library" / "bin" / "pdftoppm.exe"
    renderer = str(bundled_renderer) if bundled_renderer.is_file() else shutil.which("pdftoppm")
    if not renderer:
        fail("Bundled PDF renderer pdftoppm is unavailable.")
    if resolution < 72 or resolution > 300:
        fail("PDF proof resolution is outside the controlled 72–300 ppi range.")
    destination.mkdir(parents=True, exist_ok=True)
    requested = sorted(set(pages))
    if any(number < 1 for number in requested):
        fail("PDF rendering requested an invalid page number.")
    # Group consecutive pages into one Poppler invocation.  A 102-page proof
    # becomes one native process instead of 102 short-lived Windows processes.
    batches: list[tuple[int, int]] = []
    for number in requested:
        if not batches or number != batches[-1][1] + 1:
            batches.append((number, number))
        else:
            batches[-1] = (batches[-1][0], number)
    output: dict[int, Path] = {}
    for start, end in batches:
        prefix = destination / f"batch-{start:03}-{end:03}"
        command = [renderer, "-f", str(start), "-l", str(end), "-r", str(resolution), "-png"]
        if start == end:
            command.append("-singlefile")
        completed = subprocess.run(
            [*command, str(pdf), str(prefix)], capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        detail = "\n".join(value.strip() for value in (completed.stdout, completed.stderr) if value.strip())
        if completed.returncode != 0:
            fail(f"Bundled PDF renderer could not render pages {start}–{end} of {pdf.name}" + (f": {detail}" if detail else "."))
        generated_by_page: dict[int, Path] = {}
        if start != end:
            for candidate in destination.glob(f"{prefix.name}-*.png"):
                suffix = candidate.stem.rsplit("-", 1)[-1]
                if suffix.isdigit():
                    generated_by_page[int(suffix)] = candidate
        for number in range(start, end + 1):
            generated = prefix.with_suffix(".png") if start == end else generated_by_page.get(number)
            target = destination / f"page-{number:03}.png"
            # Poppler uses either `-1` or `-001` depending on the source page
            # count, so the preceding discovery is numeric rather than a
            # guessed filename format.  Valid all-white test pages are small.
            if generated is None or not generated.is_file() or generated.stat().st_size < 128:
                fail(f"Bundled PDF renderer did not produce page {number} of {pdf.name}" + (f": {detail}" if detail else "."))
            generated.replace(target)
            output[number] = target
    return output


def render_pdf_samples(pdf: Path, destination: Path, pages: list[int]) -> list[str]:
    return [str(path) for _number, path in sorted(render_pdf_pages(pdf, destination, pages, 150).items())]


def inspect_exported_pdf(pdf: Path, expected_pages: int, sample_dir: Path) -> tuple[int, list[str]]:
    """Parse a PDF and retain visual proof for the exact exported artifact."""
    if not pdf.exists() or pdf.stat().st_size < 512:
        fail(f"Exported PDF is missing or too small to be usable: {pdf}")
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(pdf), strict=True)
        pages = len(reader.pages)
        if pages != expected_pages:
            fail(f"{pdf.name} has {pages} pages; expected {expected_pages}.")
        boxes = [tuple(float(value) for value in reader.pages[index].mediabox) for index in (0, pages - 1)]
        if any(box[2] <= box[0] or box[3] <= box[1] for box in boxes):
            fail(f"{pdf.name} has an invalid page size.")
    except GateError:
        raise
    except Exception as error:
        fail(f"PDF parser rejected {pdf.name}: {error}")
    samples = sorted({1, min(2, pages), max(1, (pages + 1) // 2), max(1, pages - 1), pages})
    rendered = render_pdf_samples(pdf, sample_dir, samples)
    return pages, rendered


def page_visual_signature(path: Path) -> dict[str, Any]:
    """Return a resolution-independent visual signature for a rendered page."""
    try:
        from PIL import Image, ImageOps
    except Exception as error:
        fail(f"PDF page-order verifier dependency unavailable: {error}")
    with Image.open(path) as loaded:
        image = loaded.convert("L")
        edge = ImageOps.fit(image, (65, 64), method=Image.Resampling.LANCZOS)
        tone = ImageOps.fit(image, (16, 16), method=Image.Resampling.LANCZOS)
    edge_pixels = list(edge.get_flattened_data())
    bits = bytearray()
    accumulator = 0
    bit_count = 0
    for row in range(64):
        offset = row * 65
        for column in range(64):
            accumulator = (accumulator << 1) | int(edge_pixels[offset + column] >= edge_pixels[offset + column + 1])
            bit_count += 1
            if bit_count == 8:
                bits.append(accumulator)
                accumulator = 0
                bit_count = 0
    return {"dhash": bytes(bits).hex(), "tone": list(tone.get_flattened_data())}


def _page_signature_distance(expected: dict[str, Any], actual: dict[str, Any]) -> tuple[int, float]:
    try:
        expected_hash = bytes.fromhex(str(expected["dhash"]))
        actual_hash = bytes.fromhex(str(actual["dhash"]))
        expected_tone = [int(value) for value in expected["tone"]]
        actual_tone = [int(value) for value in actual["tone"]]
    except Exception as error:
        fail(f"PDF page-order signature is malformed: {error}")
    if len(expected_hash) != len(actual_hash) or len(expected_tone) != len(actual_tone):
        fail("PDF page-order signatures have incompatible dimensions.")
    dhash_distance = sum((left ^ right).bit_count() for left, right in zip(expected_hash, actual_hash))
    tone_distance = sum(abs(left - right) for left, right in zip(expected_tone, actual_tone)) / len(expected_tone)
    return dhash_distance, tone_distance


def verify_exported_pdf_order(project: Path, state: dict[str, Any], review_pdf: Path) -> dict[str, Any]:
    """Prove that every review-PDF look page remains in current proof order.

    The PDF-reference confirmations bind the registry to the reference source.
    This second comparison binds the actual exported review PDF to the current
    InDesign proof page-by-page, so a reordered export cannot inherit a PASS
    merely from matching page count.
    """
    reference_manifest = validate_reference_order(project, state)
    visual_manifest = validate_visual_proof(project, state)
    proof_pdf = child_of(project, str(visual_manifest.get("proof_pdf", "")))
    if not proof_pdf.is_file() or visual_manifest.get("proof_pdf_sha256") != digest(proof_pdf):
        fail("Current-master visual proof PDF is absent or changed; review-PDF order cannot be verified.")
    registry = validate_registry(
        state_artifact(project, state, "registry"), int(state["look_count"]), state_artifact(project, state, "hires")
    )
    pages = sorted({int(row[key]) for row in registry for key in ("indd_left_page", "indd_right_page")})
    stamp = utc_now().replace(":", "-")
    root = control_path(project) / "visual" / "pdf-order" / stamp
    proof_pages = render_pdf_pages(proof_pdf, root / "current-master-proof", pages, 96)
    review_pages = render_pdf_pages(review_pdf, root / "review-pdf", pages, 96)
    expected_signatures = {page: page_visual_signature(proof_pages[page]) for page in pages}
    actual_signatures = {page: page_visual_signature(review_pages[page]) for page in pages}
    items: list[dict[str, Any]] = []
    for row in registry:
        look_id = row["look_id"]
        comparison: dict[str, Any] = {"look_id": look_id, "reference_page": int(row["pdf_spread"]) + 1, "pages": []}
        for side, page in (("left", int(row["indd_left_page"])), ("right", int(row["indd_right_page"]))):
            expected = expected_signatures[page]
            actual = actual_signatures[page]
            dhash_distance, tone_distance = _page_signature_distance(expected, actual)
            nearest_page, nearest_dhash, nearest_tone = min(
                (
                    (candidate, *_page_signature_distance(candidate_signature, actual))
                    for candidate, candidate_signature in expected_signatures.items()
                ),
                key=lambda item: (item[1], item[2]),
            )
            if dhash_distance > MAX_PAGE_ORDER_DHASH_DISTANCE or tone_distance > MAX_PAGE_ORDER_TONE_DISTANCE:
                fail(
                    f"{look_id} {side} page {page}: exported PDF does not match the current proof at this PDF-reference position "
                    f"(dHash {dhash_distance}, tone {tone_distance:.1f})."
                )
            if nearest_page != page and nearest_dhash + PAGE_ORDER_NEAREST_MARGIN < dhash_distance:
                fail(
                    f"{look_id} {side} page {page}: exported PDF is materially closer to current-proof page "
                    f"{nearest_page} than its required page (required dHash {dhash_distance}, nearest {nearest_dhash})."
                )
            comparison["pages"].append({
                "side": side, "page": page, "proof_page_sha256": digest(proof_pages[page]),
                "review_page_sha256": digest(review_pages[page]), "dhash_distance": dhash_distance,
                "tone_distance": round(tone_distance, 3), "nearest_proof_page": nearest_page,
                "nearest_dhash_distance": nearest_dhash, "nearest_tone_distance": round(nearest_tone, 3),
            })
        items.append(comparison)
    return {
        "schema": REFERENCE_ORDER_SCHEMA, "generator": "lookbook_gate.py:verify-exported-pdf-order",
        "reference_order_manifest_sha256": digest(reference_order_manifest_path(project)),
        "reference_order_confirmation_fingerprint": reference_order_confirmation_fingerprint(project, state),
        "reference_pdf_sha256": reference_manifest["reference_pdf_sha256"],
        "current_master_proof_pdf_sha256": digest(proof_pdf), "review_pdf_sha256": digest(review_pdf),
        "items": items,
    }


def _render_visual_proof(project: Path, state: dict[str, Any]) -> dict[str, Any]:
    """Export the saved master and derive all proof images from that one PDF."""
    try:
        from PIL import Image
    except Exception as error:
        fail(f"Visual-proof renderer dependency unavailable: {error}")
    registry = validate_registry(
        state_artifact(project, state, "registry"), int(state["look_count"]), state_artifact(project, state, "hires")
    )
    proof_root = visual_proof_dir(project)
    stamp = utc_now().replace(":", "-")
    proof_root.mkdir(parents=True, exist_ok=True)
    proof_pdf = proof_root / f"current-master-{stamp}.pdf"
    run_com_driver([
        "-Action", "ExportPdf", "-Project", str(project), "-Pdf", str(proof_pdf),
        # This is an internal proof only: source-pixel clearance remains full
        # resolution, and 96 ppi keeps the visual pair proof readable while
        # materially reducing the repeated 102-page InDesign export time.
        "-RasterResolution", "96", "-PageRange", "ALL",
    ], timeout_seconds=VISUAL_PROOF_TIMEOUT_SECONDS)
    pages, _ = inspect_exported_pdf(proof_pdf, int(state["expected_pages"]), proof_root / "pdf-samples" / stamp)
    master = state_artifact(project, state, "master")
    master_after = identity(master)
    pair_folder = proof_root / "pairs" / stamp
    overview_folder = proof_root / "overview" / stamp
    pair_folder.mkdir(parents=True, exist_ok=True)
    overview_folder.mkdir(parents=True, exist_ok=True)
    pairs: dict[str, dict[str, Any]] = {}
    rendered_pages = render_pdf_pages(proof_pdf, proof_root / "rendered-pages" / stamp, list(range(1, pages + 1)), 150)

    def page_image(number: int) -> Any:
        source = rendered_pages.get(number)
        if source is None:
            fail(f"Current visual proof is missing rendered page {number}.")
        with Image.open(source) as loaded:
            return loaded.convert("RGB").copy()

    for row in registry:
        left_page = int(row["indd_left_page"])
        right_page = int(row["indd_right_page"])
        left = page_image(left_page)
        right = page_image(right_page)
        canvas = Image.new("RGB", (left.width + right.width, max(left.height, right.height)), "white")
        canvas.paste(left, (0, 0))
        canvas.paste(right, (left.width, 0))
        target = pair_folder / f"{row['look_id']}.jpg"
        canvas.save(target, "JPEG", quality=92, optimize=True)
        if target.stat().st_size < 1024:
            fail(f"{row['look_id']}: generated visual proof pair is unexpectedly empty.")
        pairs[row["look_id"]] = {
            "left_page": left_page, "right_page": right_page,
            "image": _relative_project_path(project, target), "sha256": digest(target),
        }
    overview_pages = [
        ("01-front", 1),
        ("02-first-look", int(registry[0]["indd_left_page"])),
        ("03-middle-look", int(registry[(len(registry) - 1) // 2]["indd_left_page"])),
        ("04-last-look", int(registry[-1]["indd_right_page"])),
        ("05-back", pages),
    ]
    overview: list[dict[str, Any]] = []
    for name, page_number in overview_pages:
        target = overview_folder / f"{name}.png"
        page_image(page_number).save(target, "PNG")
        if target.stat().st_size < 1024:
            fail(f"Generated visual proof overview is unexpectedly empty: {target}")
        overview.append({"name": name, "page": page_number, "image": _relative_project_path(project, target), "sha256": digest(target)})
    return {
        "schema": SCHEMA, "generator": "lookbook_gate.py:render-visual-proof", "session_id": state["session_id"],
        "created_at": utc_now(), "master": master_after, "proof_pdf": _relative_project_path(project, proof_pdf),
        "proof_pdf_sha256": digest(proof_pdf), "expected_pages": pages,
        "composition_plan_sha256": digest(composition_plan_path(project)),
        "composition_applied_sha256": digest(composition_applied_path(project)),
        "look_pairs": pairs, "overview": overview,
    }


def command_render_visual_proof(args: argparse.Namespace) -> None:
    project = project_path(args.project)
    state = load_state(project)
    if current_gate(project, state) != "visual":
        fail(f"Visual proof cannot be rendered now; next required gate is {current_gate(project, state) or 'complete'}.")
    validate_composition_plan(project, state)
    validate_composition_applied(project, state)
    run_path = visual_proof_export_run_path(project)
    if run_path.exists():
        prior = read_json(run_path)
        try:
            started_at = parse_utc(str(prior["started_at"]))
            age_seconds = (datetime.now(timezone.utc) - started_at).total_seconds()
        except (KeyError, TypeError, ValueError):
            fail("Visual-proof export ownership record is malformed; do not start another export.")
        if age_seconds < VISUAL_PROOF_TIMEOUT_SECONDS:
            fail(
                "Visual-proof export is already in progress. Wait for its InDesign lock to clear and inspect the "
                "existing run; do not launch a second export after a shell timeout."
            )
        if any(project.glob("*.idlk")):
            fail("A stale visual-proof ownership record still has an active InDesign lock; automatic restart is unsafe.")
        archive = control_path(project) / "quarantine" / f"stale-visual-proof-run-{utc_now().replace(':', '-')}"
        archive.mkdir(parents=True, exist_ok=True)
        shutil.move(str(run_path), str(archive / run_path.name))

    run = {
        "schema": SCHEMA, "session_id": state["session_id"], "nonce": str(uuid.uuid4()),
        "started_at": utc_now(), "master": identity(state_artifact(project, state, "master")),
    }
    write_json(run_path, run)
    try:
        manifest = _render_visual_proof(project, state)
        write_json(visual_proof_manifest_path(project), manifest)
        # Validate the written manifest immediately, so a partial proof can never be
        # mistaken for a completed proof pack.
        validate_visual_proof(project, state)
        run_com_driver(["-Action", "AuditCaptionClearance", "-Project", str(project)], timeout_seconds=900)
        clearance = build_caption_clearance_audit(project, state, manifest)
        blocked = [item["look_id"] for item in clearance["items"] if item["status"] != CAPTION_CLEARANCE_CLEAR]
        if blocked:
            print("CAPTION CLEARANCE BLOCKED: " + ", ".join(blocked))
            validate_caption_clearance(project, state)
        validate_caption_clearance(project, state)
        print(
            f"VISUAL PROOF RENDERED: {len(manifest['look_pairs'])} exact page pairs and "
            "computer-checked caption-clearance evidence from the current saved INDD."
        )
    finally:
        # Clear only our own completed marker. If the caller was terminated, the
        # marker survives and blocks a duplicate native export on the next run.
        try:
            current = read_json(run_path)
            if current.get("nonce") == run["nonce"]:
                run_path.unlink(missing_ok=True)
        except GateError:
            pass


def command_refresh_caption_clearance(args: argparse.Namespace) -> None:
    """Refresh the read-only source-pixel audit after a native layout snapshot.

    This deliberately does not render a new PDF and does not touch the master.
    It is used when the native coordinate snapshot is renewed during diagnosis;
    otherwise a valid existing proof would be paired with a stale layout hash.
    """
    project = project_path(args.project)
    state = load_state(project)
    if current_gate(project, state) != "visual":
        fail(f"Caption-clearance refresh can run only at the visual gate; next required gate is {current_gate(project, state) or 'complete'}.")
    validate_composition_applied(project, state)
    manifest = validate_visual_proof(project, state)
    run_com_driver(["-Action", "AuditCaptionClearance", "-Project", str(project)], timeout_seconds=900)
    report = build_caption_clearance_audit(project, state, manifest)
    blocked = [item["look_id"] for item in report["items"] if item["status"] != CAPTION_CLEARANCE_CLEAR]
    if blocked:
        print("CAPTION CLEARANCE REFRESHED — BLOCKED: " + ", ".join(blocked))
    else:
        print("CAPTION CLEARANCE REFRESHED — CLEAR: every credits block is clear of the model.")


def command_confirm_visual_look(args: argparse.Namespace) -> None:
    project = project_path(args.project)
    state = load_state(project)
    if current_gate(project, state) != "visual":
        fail(f"A visual look can only be confirmed at the visual gate; next required gate is {current_gate(project, state) or 'complete'}.")
    if not re.fullmatch(r"LOOK_\d{3}", args.look):
        fail("--look must use the exact form LOOK_001.")
    manifest = validate_visual_proof(project, state)
    clearance = validate_caption_clearance(project, state)
    if args.look not in manifest["look_pairs"]:
        fail(f"Unknown required look: {args.look}")
    note = args.note.strip()
    if len(note) < 25:
        fail("--note must record a specific visual observation for this rendered look pair (at least 25 characters).")
    target = visual_confirmation_dir(project) / f"{args.look}.json"
    if target.exists():
        fail(f"{args.look} is already confirmed. A confirmation cannot be overwritten or bulk-replaced.")
    master = state_artifact(project, state, "master")
    write_json(target, {
        "schema": SCHEMA, "session_id": state["session_id"], "created_at": utc_now(), "look_id": args.look,
        "result": VISUAL_RESULT, "note": note, "master": identity(master),
        "proof_manifest_sha256": digest(visual_proof_manifest_path(project)),
        "proof_pair_sha256": manifest["look_pairs"][args.look]["sha256"],
        "caption_clearance_item_sha256": _caption_clearance_item_fingerprint(
            next(item for item in clearance["items"] if item["look_id"] == args.look)
        ),
    })
    completed = len(list(visual_confirmation_dir(project).glob("LOOK_*.json")))
    print(f"VISUAL LOOK CONFIRMED: {args.look} ({completed}/{state['look_count']})")


def compact_page_range(pages: list[int]) -> str:
    if not pages:
        fail("A gender PDF cannot be created without any look pages.")
    ordered = sorted(set(pages))
    ranges: list[str] = []
    start = previous = ordered[0]
    for page in ordered[1:]:
        if page == previous + 1:
            previous = page
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = page
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(ranges)


def final_output_specs(project: Path, state: dict[str, Any]) -> list[dict[str, Any]]:
    """Build the five exact post-approval output specifications from frozen data."""
    master = state_artifact(project, state, "master")
    registry = validate_registry(state_artifact(project, state, "registry"), int(state["look_count"]), state_artifact(project, state, "hires"))
    mapping = csv_rows(state_artifact(project, state, "caption_map"), CAPTION_MAP_FIELDS)
    if len(mapping) != len(registry):
        fail("Caption map and registry must have the same number of looks for gender export.")
    registry_by_id = {row["look_id"]: row for row in registry}
    gender_pages: dict[str, list[int]] = {"M": [], "W": []}
    for row in mapping:
        look = registry_by_id.get(row["look_id"])
        gender = row["excel_sheet"].upper()
        if look is None or gender not in gender_pages:
            fail(f"{row['look_id']}: caption-map excel_sheet must be M or W for gender export.")
        gender_pages[gender].extend([int(look["indd_left_page"]), int(look["indd_right_page"])])
    for gender, pages in gender_pages.items():
        if not pages:
            fail(f"Gender export has no {gender} looks; both M and W PDFs are required.")
    stem = master.stem
    expected_pages = int(state["expected_pages"])
    return [
        {"key": "full_10mb", "path": f"{stem}_10mb.pdf", "raster_ppi": 120, "page_range": "ALL", "expected_pages": expected_pages, "image_compression": "jpeg", "jpeg_quality": "high"},
        {"key": "full_20mb", "path": f"{stem}_20mb.pdf", "raster_ppi": 220, "page_range": "ALL", "expected_pages": expected_pages, "image_compression": "jpeg", "jpeg_quality": "high"},
        {"key": "full_40mb", "path": f"{stem}_40mb.pdf", "raster_ppi": 300, "page_range": "ALL", "expected_pages": expected_pages, "image_compression": "jpeg", "jpeg_quality": "high"},
        {"key": "male_300ppi", "path": f"Gender/{stem}_M.pdf", "raster_ppi": 300, "page_range": compact_page_range(gender_pages["M"]), "expected_pages": len(gender_pages["M"]), "image_compression": "jpeg", "jpeg_quality": "high"},
        {"key": "female_300ppi", "path": f"Gender/{stem}_W.pdf", "raster_ppi": 300, "page_range": compact_page_range(gender_pages["W"]), "expected_pages": len(gender_pages["W"]), "image_compression": "jpeg", "jpeg_quality": "high"},
    ]


def command_verify_pdf(args: argparse.Namespace) -> None:
    project = project_path(args.project)
    state = load_state(project)
    if current_gate(project, state) != "pdf":
        fail(f"PDF cannot be verified now; next required gate is {current_gate(project, state) or 'complete'}.")
    permit = read_json(control_path(project) / "export-permit.json")
    if permit.get("session_id") != state["session_id"] or parse_utc(permit["expires_at"]) < datetime.now(timezone.utc):
        fail("Export permit is missing, belongs to another session, or expired. Run pre-export again.")
    master = state_artifact(project, state, "master")
    if not same_identity(permit.get("master", {}), identity(master)):
        fail("Master changed after the export permit. Re-audit release before exporting again.")
    pdf = child_of(project, args.pdf)
    if os.path.normcase(str(pdf)) != os.path.normcase(str(Path(permit["destination"]).resolve())):
        fail("PDF path does not match the issued export permit.")
    pages, rendered = inspect_exported_pdf(pdf, int(state["expected_pages"]), control_path(project) / "visual" / "pdf")
    order_evidence = verify_exported_pdf_order(project, state, pdf)
    armed = read_json(arm_file(project, "pdf"))
    evidence = {
        "schema": SCHEMA, "session_id": state["session_id"], "gate": "pdf", "passed": True,
        "nonce": armed["nonce"], "created_at": utc_now(), "master": identity(master), "pdf": identity(pdf),
        "pdf_sha256": digest(pdf), "page_count": pages, "rendered_samples": rendered,
        "reference_order": order_evidence,
    }
    write_json(evidence_file(project, "pdf"), evidence)
    print(f"PASS pdf: {pages} pages parsed, {len(rendered)} samples rendered, and all {len(order_evidence['items'])} PDF-reference-bound look pairs verified in order.")


def command_complete(args: argparse.Namespace) -> None:
    project = project_path(args.project)
    assert_project_root_clean(project)
    state = load_state(project)
    if current_gate(project, state) is not None:
        fail(f"Cannot complete: {current_gate(project, state)} is not accepted.")
    print("REVIEW READY: master, proof, release audit, and the review PDF belong to one current session.")
    print(f"Review PDF: {load_evidence(project, state, 'pdf')['pdf']['path']}")
    print("Wait for human review. On corrections, run begin-revision; only after explicit approval run publish-final.")


def revision_target(source: Path) -> tuple[int, Path]:
    match = re.fullmatch(r"(.+)_(\d{2,})", source.stem)
    if not match:
        fail(f"Revision master must end in _NN.indd, for example *_01.indd: {source.name}")
    prefix, raw_number = match.groups()
    revision = int(raw_number) + 1
    target = source.with_name(f"{prefix}_{revision:0{len(raw_number)}d}{source.suffix}")
    if target.exists():
        fail(f"Next revision already exists and will not be overwritten: {target.name}")
    return revision, target


def archive_for_revision(project: Path, revision: int) -> Path:
    control = control_path(project)
    archive = control / "history" / f"revision-{revision:02}-{utc_now().replace(':', '-') }"
    for relative in (
        "visual", "evidence/visual.json", "evidence/release.json", "evidence/pdf.json",
        "arms/visual.json", "arms/release.json", "arms/pdf.json", "progress/composition.json", "export-permit.json",
    ):
        source = control / relative
        if not source.exists():
            continue
        destination = archive / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
    (control / "visual").mkdir(parents=True, exist_ok=True)
    return archive


def command_begin_revision(args: argparse.Namespace) -> None:
    project = project_path(args.project)
    state = load_state(project)
    if current_gate(project, state) is not None:
        fail("A revision can begin only after the current review PDF has passed every gate and complete has been run.")
    if final_deliverables_file(project).exists():
        fail("This version is already approved and published. Start a new requested lookbook revision instead of changing an approved master.")
    notes = args.notes.strip()
    if len(notes) < 8:
        fail("--notes must record the received page-by-page correction request.")
    source = state_artifact(project, state, "master")
    revision, target = revision_target(source)
    shutil.copy2(source, target)
    archive = archive_for_revision(project, revision)
    state["master"] = target.name
    state["current_revision"] = revision
    write_json(state_file(project), state)
    write_json(control_path(project) / "revisions" / f"revision-{revision:02}.json", {
        "schema": SCHEMA, "revision": revision, "created_at": utc_now(), "source_master": identity(source),
        "master": identity(target), "corrections": notes, "archived_proof": str(archive),
    })
    if current_gate(project, state) != "visual":
        fail("Revision safety reset failed: visual review was not unlocked for the new master.")
    print(f"REVISION {revision:02} CREATED: {target.name}")
    print("Apply only the recorded corrections to this new INDD, then repeat composition plan, native crop application, pair proof, visual confirmations, release, and review-PDF verification.")


def command_publish_final(args: argparse.Namespace) -> None:
    project = project_path(args.project)
    assert_project_root_clean(project)
    state = load_state(project)
    if current_gate(project, state) is not None:
        fail("Final publishing is blocked until the review PDF has passed every gate and complete has been run.")
    approval = args.approval_note.strip()
    if len(approval) < 2:
        fail("--approval-note must record the user's explicit approval (for example: согласовано).")
    master = state_artifact(project, state, "master")
    release = load_evidence(project, state, "release")
    review = load_evidence(project, state, "pdf")
    if release is None or review is None:
        fail("Current release and review-PDF evidence are required before final publishing.")
    manifest_path = final_deliverables_file(project)
    specs = final_output_specs(project, state)
    if manifest_path.exists():
        manifest = read_json(manifest_path)
        if manifest.get("session_id") != state["session_id"] or not same_identity(manifest.get("master", {}), identity(master)):
            fail("Existing final-deliverables record belongs to another master or session.")
        if manifest.get("release_evidence_sha256") != digest(evidence_file(project, "release")):
            fail("Release evidence changed after final publishing began.")
        # Older interrupted releases did not record compression metadata.
        # Their page plan remains valid, so backfill the fixed export settings
        # rather than refusing a safe resume.
        spec_keys = ("key", "path", "raster_ppi", "page_range", "expected_pages")
        existing_specs = [{key: entry.get(key) for key in spec_keys} for entry in manifest.get("outputs", [])]
        planned_specs = [{key: entry.get(key) for key in spec_keys} for entry in specs]
        if existing_specs != planned_specs:
            fail("Existing final-deliverables plan does not match current gender mapping or required filenames.")
        for entry in manifest["outputs"]:
            entry["image_compression"] = "jpeg"
            entry["jpeg_quality"] = "high"
        write_json(manifest_path, manifest)
    else:
        for spec in specs:
            if child_of(project, spec["path"]).exists():
                fail(f"Final output already exists and will not be overwritten: {spec['path']}")
        manifest = {
            "schema": SCHEMA, "session_id": state["session_id"], "status": "in_progress", "created_at": utc_now(),
            "approval_note": approval, "master": identity(master), "release_evidence_sha256": digest(evidence_file(project, "release")),
            "review_pdf_sha256": review["pdf_sha256"], "outputs": specs,
        }
        write_json(manifest_path, manifest)
    pending_export = [
        {
            "path": entry["path"], "raster_ppi": int(entry["raster_ppi"]), "page_range": str(entry["page_range"]),
        }
        for entry in manifest["outputs"]
        if "identity" not in entry and not child_of(project, entry["path"]).exists()
    ]
    if pending_export:
        plan_path = control_path(project) / "work" / "final-export-plan.json"
        write_json(plan_path, {
            "schema": SCHEMA, "session_id": state["session_id"], "master": identity(master),
            "created_at": utc_now(), "outputs": pending_export,
        })
        # Native InDesign remains a single writer, but a normal first publish
        # no longer pays five document-open cycles.  The COM worker writes a
        # durable checkpoint after every file; an interrupted rerun simply
        # verifies the files already present and exports only the rest.
        run_com_driver([
            "-Action", "ExportPdfSet", "-Project", str(project), "-ExportPlan", str(plan_path),
        ], timeout_seconds=3600)
    to_verify = [entry for entry in manifest["outputs"] if "identity" not in entry]
    verification: dict[str, tuple[int, list[str]]] = {}
    if to_verify:
        # These reads never touch InDesign and use separate destination folders,
        # so bounded parallel rendering reduces the post-export wait without
        # weakening the per-file page-count and sample checks.
        workers = min(3, len(to_verify))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="lookbook-final-pdf") as executor:
            futures = {
                executor.submit(
                    inspect_exported_pdf,
                    child_of(project, entry["path"]),
                    int(entry["expected_pages"]),
                    control_path(project) / "visual" / "final" / entry["key"],
                ): entry
                for entry in to_verify
            }
            for future in as_completed(futures):
                entry = futures[future]
                verification[entry["key"]] = future.result()
    for entry in manifest["outputs"]:
        if not same_identity(manifest["master"], identity(master)) or load_evidence(project, state, "release") is None:
            fail("Master or release evidence changed while final PDFs were being published.")
        destination = child_of(project, entry["path"])
        if "identity" in entry:
            if not destination.exists() or not same_identity(entry["identity"], identity(destination)) or entry.get("sha256") != digest(destination):
                fail(f"Recorded final output changed after verification: {entry['path']}")
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            fail(f"Final export set did not create the required PDF: {entry['path']}")
        page_count, samples = verification[entry["key"]]
        entry["identity"] = identity(destination)
        entry["sha256"] = digest(destination)
        entry["page_count"] = page_count
        entry["rendered_samples"] = samples
        entry["verified_at"] = utc_now()
        write_json(manifest_path, manifest)
        print(f"FINAL CHECKPOINT: {entry['path']} (JPEG High, {entry['raster_ppi']} ppi, {page_count} pages)")
    manifest["status"] = "complete"
    manifest["completed_at"] = utc_now()
    write_json(manifest_path, manifest)
    print("FINAL DELIVERABLES PASS: 5 verified PDFs written.")
    for entry in manifest["outputs"]:
        print(child_of(project, entry["path"]))


def command_status(args: argparse.Namespace) -> None:
    project = project_path(args.project)
    state = load_state(project)
    passed = set(passed_gates(project, state))
    for gate in GATES:
        print(f"{'PASS' if gate in passed else 'LOCK'}  {gate}")
    next_gate = current_gate(project, state)
    print(f"SESSION {state['session_id']}")
    print(f"NEXT {next_gate or 'COMPLETE'}")
    final = final_deliverables_file(project)
    if final.exists():
        try:
            manifest = read_json(final)
            print(f"FINAL {str(manifest.get('status', 'unknown')).upper()} {len(manifest.get('outputs', []))}/5")
        except GateError:
            print("FINAL DAMAGED")
    if next_gate == "map":
        if "reference_pdf" not in state:
            print("REFERENCE_ORDER REQUIRED: run prepare-reference-order --reference control/work/_mat/reference.pdf, inspect every card, then confirm every LOOK_###.")
        elif not reference_order_manifest_path(project).exists():
            print("REFERENCE_ORDER REQUIRED: run prepare-reference-order, inspect every card, then confirm every LOOK_###.")
        else:
            confirmations = reference_order_confirmation_dir(project)
            count = len(list(confirmations.glob("LOOK_*.json"))) if confirmations.exists() else 0
            print(f"REFERENCE_ORDER_CONFIRMATIONS {count}/{state['look_count']}")
    if next_gate == "images":
        armed_path = arm_file(project, "images")
        if armed_path.exists():
            progress = image_progress(project, state, read_json(armed_path))
            if progress:
                print(f"IMAGE_PROGRESS {progress.get('completed_count', 0)}/{progress.get('look_count', state['look_count'])}")
    if next_gate == "captions":
        armed_path = arm_file(project, "captions")
        if armed_path.exists():
            progress = caption_progress(project, state, read_json(armed_path))
            if progress:
                print(f"CAPTION_PROGRESS {progress.get('completed_count', 0)}/{progress.get('look_count', state['look_count'])}")
    if next_gate == "visual":
        progress = composition_progress(project, state)
        if progress:
            print(f"COMPOSITION_PROGRESS {progress.get('completed_count', 0)}/{progress.get('look_count', state['look_count'])}")
        confirmations = visual_confirmation_dir(project)
        count = len(list(confirmations.glob("LOOK_*.json"))) if confirmations.exists() else 0
        print(f"VISUAL_CONFIRMATIONS {count}/{state['look_count']}")


def command_self_test(_: argparse.Namespace) -> None:
    """Exercise controller persistence, permit, parsing and rendering without InDesign."""
    def must_block(action: Any, description: str) -> None:
        try:
            action()
        except GateError:
            return
        raise GateError("Self-test failure: controller did not block " + description)

    def write_order_test_pdf(destination: Path, page_order: list[int]) -> None:
        """Make visually distinct test pages so a swapped order is detectable."""
        try:
            from reportlab.pdfgen import canvas
            from reportlab.lib.colors import Color
        except Exception as error:
            raise GateError(f"Self-test requires reportlab: {error}")
        colours = {
            1: (0.95, 0.25, 0.20), 2: (0.20, 0.55, 0.95), 3: (0.20, 0.75, 0.40),
            4: (0.95, 0.75, 0.20), 5: (0.65, 0.35, 0.85), 6: (0.20, 0.75, 0.75),
        }
        document = canvas.Canvas(str(destination), pagesize=(300, 500))
        for marker in page_order:
            red, green, blue = colours[marker]
            document.setFillColor(Color(red, green, blue))
            document.rect(0, 0, 300, 500, stroke=0, fill=1)
            document.setFillColor(Color(0, 0, 0))
            document.setFont("Helvetica-Bold", 48)
            document.drawString(28, 230, f"PAGE {marker}")
            document.showPage()
        document.save()

    with tempfile.TemporaryDirectory(prefix="lookbook-gate-") as raw:
        project = Path(raw)
        master = project / "master_01.indd"
        master.write_bytes(b"saved indesign checkpoint")
        work = work_path(project)
        hires = work / "_mat" / "hires"
        hires.mkdir(parents=True)
        from PIL import Image
        for index, name in enumerate(("left.jpg", "right.jpg", "left2.jpg", "right2.jpg"), start=1):
            Image.new("RGB", (120, 180), (index * 40, index * 30, index * 20)).save(hires / name, "JPEG")
        reference = work / "_mat" / "reference.pdf"
        from pypdf import PdfWriter
        reference_writer = PdfWriter()
        for _ in range(3):
            reference_writer.add_blank_page(width=300, height=500)
        with reference.open("wb") as target:
            reference_writer.write(target)
        registry = work / "look-register.tsv"
        registry.write_text(
            "\t".join(REGISTRY_FIELDS) + "\n" +
            "LOOK_001\t1\t1\tleft.jpg\tright.jpg\t2\t3\n" +
            "LOOK_002\t2\t2\tleft2.jpg\tright2.jpg\t4\t5\n", encoding="utf-8")
        workbook = work / "_mat" / "captions.xlsx"
        workbook.parent.mkdir(parents=True, exist_ok=True)
        try:
            from openpyxl import Workbook
        except Exception as error:
            raise GateError(f"Self-test requires openpyxl: {error}")
        book = Workbook()
        women = book.active
        women.title = "W"
        men = book.create_sheet("M")
        women.append([None] * 6)
        men.append([None] * 6)
        women.append([None, "1", "TYPE", "BRAND", 1000, "A"])
        men.append([None, "1", "TYPE", "BRAND", 2000, "B"])
        book.save(workbook)
        book.close()
        excel_images = work / "_mat" / "excel-images"
        proofs = work / "mapping-evidence"
        excel_images.mkdir(parents=True)
        proofs.mkdir(parents=True)
        for name in ("W_001_01.jpg", "M_001_01.jpg"):
            (excel_images / name).write_bytes(b"excel-image" * 256)
        for name in ("LOOK_001.jpg", "LOOK_002.jpg"):
            (proofs / name).write_bytes(b"visual-evidence" * 256)
        caption_map = work / "caption-map.tsv"
        caption_map.write_text(
            "\t".join(CAPTION_MAP_FIELDS) + "\n" +
            "LOOK_001\tW\t1\tcontrol/work/_mat/excel-images/W_001_01.jpg\tleft.jpg\tright.jpg\tcontrol/work/mapping-evidence/LOOK_001.jpg\tCONFIRMED\n" +
            "LOOK_002\tM\t1\tcontrol/work/_mat/excel-images/M_001_01.jpg\tleft2.jpg\tright2.jpg\tcontrol/work/mapping-evidence/LOOK_002.jpg\tCONFIRMED\n", encoding="utf-8")
        write_json(proofs / "manifest.json", {
            "schema": 1,
            "generator": "render_caption_mapping_evidence.py",
            "items": {
                "LOOK_001": {"left_sha256": digest(hires / "left.jpg"), "right_sha256": digest(hires / "right.jpg"), "excel_sha256": digest(excel_images / "W_001_01.jpg"), "evidence_sha256": digest(proofs / "LOOK_001.jpg")},
                "LOOK_002": {"left_sha256": digest(hires / "left2.jpg"), "right_sha256": digest(hires / "right2.jpg"), "excel_sha256": digest(excel_images / "M_001_01.jpg"), "evidence_sha256": digest(proofs / "LOOK_002.jpg")},
            },
        })
        captions = work / "caption-data.tsv"
        captions.write_text("\t".join(CAPTION_FIELDS) + "\nLOOK_001\tTYPE\tBRAND\t1 000 ₽\tA\nLOOK_002\tTYPE\tBRAND\t2 000 ₽\tB\n", encoding="utf-8")
        provenance = work / "caption-provenance.json"
        write_json(provenance, {
            "schema": 1,
            "caption_map_sha256": digest(caption_map),
            "caption_workbook_sha256": digest(workbook),
            "caption_data_sha256": digest(captions),
            "look_count": 2,
            "caption_rows": 2,
        })
        init = argparse.Namespace(project=str(project), master="master_01.indd", registry="control/work/look-register.tsv", captions="control/work/caption-data.tsv", caption_map="control/work/caption-map.tsv", caption_workbook="control/work/_mat/captions.xlsx", caption_provenance="control/work/caption-provenance.json", hires="control/work/_mat/hires", reference="control/work/_mat/reference.pdf", looks=2, show_date="26.07.2026", show_text="26 ИЮЛЯ", legal_offset_days=14, restart=False)
        command_init(init)
        command_prepare_reference_order(argparse.Namespace(project=str(project), reference=None))
        must_block(lambda: command_validate_map(argparse.Namespace(project=str(project))), "a registry without every PDF-reference confirmation")
        command_confirm_reference_look(argparse.Namespace(project=str(project), look="LOOK_001", note="Reference spread matches the registered full-length left and close-up right pair."))
        command_confirm_reference_look(argparse.Namespace(project=str(project), look="LOOK_002", note="Reference spread matches the registered full-length left and close-up right pair."))
        reference_manifest = validate_reference_order_manifest(project, load_state(project))
        reference_card = child_of(project, reference_manifest["looks"]["LOOK_001"]["evidence_image"])
        stray = project / "temporary-contact-sheet.jpg"
        stray.write_bytes(b"must-not-live-in-root")
        must_block(lambda: command_validate_map(argparse.Namespace(project=str(project))), "temporary material in the project root")
        stray.unlink()
        must_block(lambda: command_arm(argparse.Namespace(project=str(project), gate="images")), "a skipped image gate")
        original_captions = captions.read_bytes()
        captions.write_text("\t".join(CAPTION_FIELDS) + "\nLOOK_001\tWRONG\tBRAND\t1 000 ₽\tA\nLOOK_002\tTYPE\tBRAND\t2 000 ₽\tB\n", encoding="utf-8")
        must_block(lambda: command_validate_map(argparse.Namespace(project=str(project))), "hand-edited captions that do not come from the workbook")
        captions.write_bytes(original_captions)
        original_proof = (proofs / "LOOK_001.jpg").read_bytes()
        (proofs / "LOOK_001.jpg").write_bytes(b"wrong-proof" * 256)
        must_block(lambda: command_validate_map(argparse.Namespace(project=str(project))), "a proof card whose pixels no longer match its manifest")
        (proofs / "LOOK_001.jpg").write_bytes(original_proof)
        original_reference_card = reference_card.read_bytes()
        reference_card.write_bytes(b"wrong-reference-order-proof" * 128)
        must_block(lambda: command_validate_map(argparse.Namespace(project=str(project))), "a changed PDF-reference order proof")
        reference_card.write_bytes(original_reference_card)
        command_validate_map(argparse.Namespace(project=str(project)))
        must_block(lambda: command_pre_export(argparse.Namespace(project=str(project), pdf="deliverable.pdf", quarantine_existing=False)), "an early PDF export")
        state = load_state(project)
        for gate in ("structure", "dates", "frames", "images", "captions", "visual", "release"):
            command_arm(argparse.Namespace(project=str(project), gate=gate))
            if gate == "structure":
                must_block(lambda: command_arm(argparse.Namespace(project=str(project), gate=gate)), "a second arm that discards recovery state")
            if gate == "visual":
                must_block(
                    lambda: command_record_visual(argparse.Namespace(project=str(project), notes="Visual audit checks the complete lookbook layout.")),
                    "visual proof without a controlled composition plan",
                )
                composition = composition_plan_path(project)
                composition.parent.mkdir(parents=True, exist_ok=True)
                composition.write_text(
                    "\t".join(COMPOSITION_PLAN_FIELDS) + "\n" +
                    "LOOK_001\tFULL_LEFT_CLOSE_RIGHT\tleft.jpg\tright.jpg\t0\tREADY\n" +
                    "LOOK_002\tFULL_LEFT_CLOSE_RIGHT\tright2.jpg\tleft2.jpg\t-12.5\tREADY\n",
                    encoding="utf-8",
                )
                applied = composition_applied_path(project)
                write_json(applied, {
                    "schema": SCHEMA, "generator": "run_lookbook_gate_com.ps1:ApplyComposition", "session_id": state["session_id"],
                    "nonce": read_json(arm_file(project, "visual"))["nonce"], "master": identity(master),
                    "composition_plan_sha256": digest(composition), "items": [
                        {"look_id": "LOOK_001", "left_image_filename": "left.jpg", "right_image_filename": "right.jpg", "planned_shift_points": 0, "before_left_graphic_bounds": [0, 0, 100, 50], "after_left_graphic_bounds": [0, 0, 100, 50]},
                        {"look_id": "LOOK_002", "left_image_filename": "right2.jpg", "right_image_filename": "left2.jpg", "planned_shift_points": -12.5, "before_left_graphic_bounds": [0, 10, 100, 60], "after_left_graphic_bounds": [0, -2.5, 100, 47.5]},
                    ],
                })
                original_applied = applied.read_bytes()
                invalid_applied = read_json(applied)
                invalid_applied["items"][1]["after_left_graphic_bounds"][1] = -1.0
                write_json(applied, invalid_applied)
                must_block(
                    lambda: validate_composition_applied(project, state),
                    "a native composition record whose claimed shift was not actually applied",
                )
                applied.write_bytes(original_applied)
                proof_root = visual_proof_dir(project)
                pair_root = proof_root / "pairs" / "self-test"
                overview_root = proof_root / "overview" / "self-test"
                pair_root.mkdir(parents=True, exist_ok=True)
                overview_root.mkdir(parents=True, exist_ok=True)
                proof_pdf = proof_root / "current-master.pdf"
                write_order_test_pdf(proof_pdf, [1, 2, 3, 4, 5, 6])
                pairs: dict[str, dict[str, Any]] = {}
                for row in validate_registry(registry, 2, hires):
                    pair = pair_root / f"{row['look_id']}.jpg"
                    pair.write_bytes(b"rendered-current-master-pair" * 128)
                    pairs[row["look_id"]] = {"left_page": int(row["indd_left_page"]), "right_page": int(row["indd_right_page"]), "image": _relative_project_path(project, pair), "sha256": digest(pair)}
                overview = []
                for number, name in enumerate(("01-front", "02-first-look", "03-middle-look", "04-last-look", "05-back"), start=1):
                    image = overview_root / f"{name}.png"
                    image.write_bytes(b"rendered-current-master-overview" * 128)
                    overview.append({"name": name, "page": number, "image": _relative_project_path(project, image), "sha256": digest(image)})
                write_json(visual_proof_manifest_path(project), {
                    "schema": SCHEMA, "generator": "lookbook_gate.py:render-visual-proof", "session_id": state["session_id"], "master": identity(master),
                    "proof_pdf": _relative_project_path(project, proof_pdf), "proof_pdf_sha256": digest(proof_pdf), "expected_pages": 6,
                    "composition_plan_sha256": digest(composition), "composition_applied_sha256": digest(applied), "look_pairs": pairs, "overview": overview,
                })
                layout_items = []
                clearance_items = []
                clearance_proofs = caption_clearance_proof_dir(project) / "self-test"
                for row in validate_registry(registry, 2, hires):
                    look_id = row["look_id"]
                    layout_items.append({
                        "look_id": look_id, "left_page": int(row["indd_left_page"]), "source_filename": row["left_filename"],
                        "graphic_bounds": [0, 0, 100, 50], "caption_bounds": [10, 5, 25, 25], "page_bounds": [0, 0, 100, 50], "safe_bounds": [5, 5, 95, 45],
                        "credits_overflow": False, "graphic_rotation": 0, "caption_rotation": 0,
                    })
                    proof = clearance_proofs / f"{look_id}.png"
                    proof.parent.mkdir(parents=True, exist_ok=True)
                    proof.write_bytes(b"caption-clearance-proof" * 128)
                    clearance_items.append({
                        "look_id": look_id, "source_filename": row["left_filename"], "source_sha256": digest(hires / row["left_filename"]),
                        "proof_pair_sha256": pairs[look_id]["sha256"], "status": CAPTION_CLEARANCE_CLEAR,
                        "reason": "test background is clear", "metrics": {"coverage": 0.0, "largest_component": 0.0},
                        "evidence_image": _relative_project_path(project, proof), "evidence_sha256": digest(proof),
                    })
                write_json(caption_clearance_layout_path(project), {
                    "schema": CAPTION_CLEARANCE_SCHEMA, "generator": "run_lookbook_gate_com.ps1:AuditCaptionClearance",
                    "session_id": state["session_id"], "master": identity(master), "registry_sha256": digest(registry),
                    "captions_sha256": digest(captions), "items": layout_items,
                })
                write_json(caption_clearance_report_path(project), {
                    "schema": CAPTION_CLEARANCE_SCHEMA, "generator": "lookbook_gate.py:caption-clearance-audit",
                    "session_id": state["session_id"], "master": identity(master),
                    "layout_sha256": digest(caption_clearance_layout_path(project)), "visual_proof_manifest_sha256": digest(visual_proof_manifest_path(project)),
                    "passed": True, "items": clearance_items,
                })
                original_clearance = caption_clearance_report_path(project).read_bytes()
                blocked_clearance = read_json(caption_clearance_report_path(project))
                blocked_clearance["passed"] = False
                blocked_clearance["items"][0]["status"] = CAPTION_CLEARANCE_COLLISION
                write_json(caption_clearance_report_path(project), blocked_clearance)
                must_block(lambda: validate_caption_clearance(project, state), "a caption-clearance collision passed as visual proof")
                caption_clearance_report_path(project).write_bytes(original_clearance)
                must_block(
                    lambda: command_record_visual(argparse.Namespace(project=str(project), notes="Visual audit checks the complete lookbook layout.")),
                    "a bulk visual status without every individual proof-pair confirmation",
                )
                command_confirm_visual_look(argparse.Namespace(project=str(project), look="LOOK_001", note="Credits are clear of the model; full length is left and close-up is right."))
                must_block(
                    lambda: command_confirm_visual_look(argparse.Namespace(project=str(project), look="LOOK_001", note="This attempt must not overwrite the original visual confirmation.")),
                    "a visual confirmation overwrite",
                )
                command_confirm_visual_look(argparse.Namespace(project=str(project), look="LOOK_002", note="Credits are clear of the model; full length is left and close-up is right."))
                command_record_visual(argparse.Namespace(project=str(project), notes="Проверены обложки, развороты, даты и отсутствие пустых страниц."))
                original_composition = composition.read_bytes()
                composition.write_text(
                    "\t".join(COMPOSITION_PLAN_FIELDS) + "\n" +
                    "LOOK_001\tFULL_LEFT_CLOSE_RIGHT\tleft.jpg\tright.jpg\t0\tREADY\n" +
                    "LOOK_002\tFULL_LEFT_CLOSE_RIGHT\tleft2.jpg\tright2.jpg\t-12.5\tREADY\n",
                    encoding="utf-8",
                )
                must_block(
                    lambda: command_arm(argparse.Namespace(project=str(project), gate="release")),
                    "visual evidence whose controlled composition plan changed after recording",
                )
                composition.write_bytes(original_composition)
                pair_file = pair_root / "LOOK_001.jpg"
                original_pair = pair_file.read_bytes()
                pair_file.write_bytes(b"substituted-mapping-card" * 256)
                must_block(
                    lambda: command_arm(argparse.Namespace(project=str(project), gate="release")),
                    "visual evidence whose current-master rendered pair was replaced after confirmation",
                )
                pair_file.write_bytes(original_pair)
                continue
            arm = read_json(arm_file(project, gate))
            write_json(evidence_file(project, gate), {"schema": SCHEMA, "session_id": state["session_id"], "gate": gate, "passed": True, "nonce": arm["nonce"], "created_at": utc_now(), "master": identity(master)})
            command_confirm(argparse.Namespace(project=str(project), gate=gate))
        output = "deliverable.pdf"
        command_pre_export(argparse.Namespace(project=str(project), pdf=output, quarantine_existing=False))
        from pypdf import PdfWriter
        writer = PdfWriter()
        for _ in range(5):
            writer.add_blank_page(width=300, height=500)
        with (project / output).open("wb") as target:
            writer.write(target)
        must_block(lambda: command_verify_pdf(argparse.Namespace(project=str(project), pdf=output)), "a wrong-page-count PDF")
        writer = PdfWriter()
        for _ in range(6):
            writer.add_blank_page(width=300, height=500)
        with (project / output).open("wb") as target:
            writer.write(target)
        write_order_test_pdf(project / output, [1, 4, 3, 2, 5, 6])
        must_block(lambda: command_verify_pdf(argparse.Namespace(project=str(project), pdf=output)), "a PDF whose look pages were reordered after export")
        write_order_test_pdf(project / output, [1, 2, 3, 4, 5, 6])
        command_verify_pdf(argparse.Namespace(project=str(project), pdf=output))
        command_complete(argparse.Namespace(project=str(project)))
        specs = final_output_specs(project, load_state(project))
        expected_specs = [
            ("full_10mb", "master_01_10mb.pdf", 120, "ALL", 6),
            ("full_20mb", "master_01_20mb.pdf", 220, "ALL", 6),
            ("full_40mb", "master_01_40mb.pdf", 300, "ALL", 6),
            ("male_300ppi", "Gender/master_01_M.pdf", 300, "4-5", 2),
            ("female_300ppi", "Gender/master_01_W.pdf", 300, "2-3", 2),
        ]
        actual_specs = [(item["key"], item["path"], item["raster_ppi"], item["page_range"], item["expected_pages"]) for item in specs]
        if actual_specs != expected_specs:
            fail(f"Self-test failure: final output plan was {actual_specs!r}.")
        command_begin_revision(argparse.Namespace(project=str(project), notes="Page 2: adjust the approved crop."))
        revised_state = load_state(project)
        if revised_state["master"] != "master_02.indd" or not (project / "master_02.indd").exists() or current_gate(project, revised_state) != "visual":
            fail("Self-test failure: revision did not safely return to visual proof.")
    print("SELF-TEST PASS")


def parser() -> argparse.ArgumentParser:
    main = argparse.ArgumentParser(description="Evidence-first lookbook release controller")
    commands = main.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="start a new controlled build session")
    init.add_argument("project")
    init.add_argument("--master", required=True)
    init.add_argument("--registry", default="look-register.tsv")
    init.add_argument("--captions", default="caption-data.tsv")
    init.add_argument("--caption-map", required=True, help="visually confirmed caption-map.tsv")
    init.add_argument("--caption-workbook", required=True, help="copied source Excel workbook inside this project")
    init.add_argument("--caption-provenance", required=True, help="hash manifest generated beside caption-data.tsv")
    init.add_argument("--hires", required=True, help="folder containing exactly the image filenames in the registry")
    init.add_argument("--reference", required=True, help="copied authoritative PDF reference inside control/work")
    init.add_argument("--looks", type=int, required=True)
    init.add_argument("--show-date", required=True, help="DD.MM.YYYY")
    init.add_argument("--show-text", required=True, help="exact visible show-date text")
    init.add_argument("--legal-offset-days", type=int, default=14)
    init.add_argument("--restart", action="store_true")
    init.set_defaults(func=command_init)
    valid = commands.add_parser("validate-map", help="accept only photo pairs with visually confirmed Excel credit mapping")
    valid.add_argument("project")
    valid.set_defaults(func=command_validate_map)
    reference_prepare = commands.add_parser("prepare-reference-order", help="render PDF-reference versus registered-pair proof cards before map acceptance")
    reference_prepare.add_argument("project")
    reference_prepare.add_argument("--reference", help="controlled PDF reference inside control/work; required for a legacy session")
    reference_prepare.set_defaults(func=command_prepare_reference_order)
    reference_confirm = commands.add_parser("confirm-reference-look", help="record one inspected PDF-reference to registered-photo-pair comparison")
    reference_confirm.add_argument("project")
    reference_confirm.add_argument("--look", required=True)
    reference_confirm.add_argument("--note", required=True)
    reference_confirm.set_defaults(func=command_confirm_reference_look)
    arm = commands.add_parser("arm", help="unlock exactly the next InDesign or visual gate")
    arm.add_argument("project")
    arm.add_argument("--gate", required=True, choices=GATES)
    arm.set_defaults(func=command_arm)
    apply = commands.add_parser("apply", help="run the armed InDesign gate through native COM automation")
    apply.add_argument("project")
    apply.add_argument("--gate", required=True, choices=sorted(INDESIGN_GATES))
    apply.set_defaults(func=command_apply)
    composition = commands.add_parser("apply-composition", help="apply the reviewed fixed-frame image and horizontal-crop plan in native saved batches")
    composition.add_argument("project")
    composition.set_defaults(func=command_apply_composition)
    repair_links = commands.add_parser("repair-links", help="reapply the frozen composition to repair or reject stale image links")
    repair_links.add_argument("project")
    repair_links.set_defaults(func=command_repair_links)
    repair_captions = commands.add_parser("repair-captions", help="rewrite frozen credits to remove local typography overrides at visual stage")
    repair_captions.add_argument("project")
    repair_captions.set_defaults(func=command_repair_captions)
    restore_caption_repair = commands.add_parser("restore-failed-caption-repair", help="restore matching visual evidence after an unsaved caption-repair failure")
    restore_caption_repair.add_argument("project")
    restore_caption_repair.set_defaults(func=command_restore_failed_caption_repair)
    clearance_plan = commands.add_parser("plan-clearance-corrections", help="replace a blocked visual plan with bounded horizontal-only corrections")
    clearance_plan.add_argument("project")
    clearance_plan.add_argument("--force-looks", default="", help="controller-recorded rejected LOOK ids that require a conservative correction plan")
    clearance_plan.set_defaults(func=command_plan_clearance_corrections)
    restart_visual = commands.add_parser("restart-visual-confirmations", help="archive a partial visual-proof queue after a grounded rejection")
    restart_visual.add_argument("project")
    restart_visual.add_argument("--notes", required=True)
    restart_visual.set_defaults(func=command_restart_visual_confirmations)
    reconcile_clearance_plan = commands.add_parser("reconcile-clearance-plan-priors", help="repair an unstarted visual retry plan from same-master native evidence")
    reconcile_clearance_plan.add_argument("project")
    reconcile_clearance_plan.set_defaults(func=command_reconcile_clearance_plan_priors)
    safe_calibration = commands.add_parser("calibrate-safe-area", help="replan one existing credits correction inside the native page safe area")
    safe_calibration.add_argument("project")
    safe_calibration.add_argument("--look", required=True)
    safe_calibration.set_defaults(func=command_calibrate_safe_area)
    apply_safe_calibration = commands.add_parser("apply-safe-area-calibration", help="apply one signed safe-area credits correction without recomposing other looks")
    apply_safe_calibration.add_argument("project")
    apply_safe_calibration.add_argument("--look", required=True)
    apply_safe_calibration.set_defaults(func=command_apply_safe_area_calibration)
    safe_preview = commands.add_parser("render-safe-area-preview", help="export and pixel-check only one calibrated two-page look preview")
    safe_preview.add_argument("project")
    safe_preview.add_argument("--look", required=True)
    safe_preview.set_defaults(func=command_render_safe_area_preview)
    prune_clearance_plan = commands.add_parser("prune-unsafe-clearance-corrections", help="remove pre-rule crop moves that are still predicted to collide")
    prune_clearance_plan.add_argument("project")
    prune_clearance_plan.set_defaults(func=command_prune_unsafe_clearance_corrections)
    reset_composition = commands.add_parser("reset-composition", help="archive blocked visual proof and restore zero absolute image-crop offsets")
    reset_composition.add_argument("project")
    reset_composition.set_defaults(func=command_reset_composition)
    proof = commands.add_parser("render-visual-proof", help="export the current INDD and render every exact look pair for visual review")
    proof.add_argument("project")
    proof.set_defaults(func=command_render_visual_proof)
    clearance_refresh = commands.add_parser("refresh-caption-clearance", help="renew the read-only native credits-clearance audit without exporting a PDF")
    clearance_refresh.add_argument("project")
    clearance_refresh.set_defaults(func=command_refresh_caption_clearance)
    look_confirm = commands.add_parser("confirm-visual-look", help="bind one inspected look result to its current rendered pair proof")
    look_confirm.add_argument("project")
    look_confirm.add_argument("--look", required=True)
    look_confirm.add_argument("--note", required=True)
    look_confirm.set_defaults(func=command_confirm_visual_look)
    confirm = commands.add_parser("confirm", help="accept current InDesign audit evidence")
    confirm.add_argument("project")
    confirm.add_argument("--gate", required=True, choices=GATES)
    confirm.set_defaults(func=command_confirm)
    visual = commands.add_parser("record-visual", help="record current-master visual proof only after every rendered look pair has its own confirmation")
    visual.add_argument("project")
    visual.add_argument("--notes", required=True)
    visual.set_defaults(func=command_record_visual)
    permit = commands.add_parser("pre-export", help="issue the only permitted export destination")
    permit.add_argument("project")
    permit.add_argument("--pdf", required=True)
    permit.add_argument("--quarantine-existing", action="store_true")
    permit.set_defaults(func=command_pre_export)
    export = commands.add_parser("export-pdf", help="export the currently permitted PDF through native InDesign automation")
    export.add_argument("project")
    export.add_argument("--pdf", required=True)
    export.set_defaults(func=command_export_pdf)
    verify = commands.add_parser("verify-pdf", help="parse and render the permitted PDF")
    verify.add_argument("project")
    verify.add_argument("--pdf", required=True)
    verify.set_defaults(func=command_verify_pdf)
    status = commands.add_parser("status", help="show durable evidence status")
    status.add_argument("project")
    status.set_defaults(func=command_status)
    revision = commands.add_parser("begin-revision", help="copy the approved review master to the next _NN INDD for recorded corrections")
    revision.add_argument("project")
    revision.add_argument("--notes", required=True, help="received page-by-page correction request")
    revision.set_defaults(func=command_begin_revision)
    publish = commands.add_parser("publish-final", help="after explicit user approval, write the five final PDF variants")
    publish.add_argument("project")
    publish.add_argument("--approval-note", required=True, help="the user's explicit approval, for example: согласовано")
    publish.set_defaults(func=command_publish_final)
    complete = commands.add_parser("complete", help="mark the verified review PDF ready for human review")
    complete.add_argument("project")
    complete.set_defaults(func=command_complete)
    test = commands.add_parser("self-test", help="run controller self-test in a temporary folder")
    test.set_defaults(func=command_self_test)
    return main


def main() -> int:
    args = parser().parse_args()
    try:
        args.func(args)
        return 0
    except GateError as error:
        print(f"BLOCKED: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
