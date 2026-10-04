"""Durable, content-bound responses for resumable read-only credit checks."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any

from .ai_policy import read_only_vision_prompt


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def set_review_generation(root: Path, look_id: str, token: str) -> None:
    if not look_id.startswith("LOOK_") or not look_id[5:].isdigit():
        raise ValueError("Invalid review-generation look id.")
    path = root / "control/work/ai-checkpoints/generations" / f"{look_id}.json"
    if _json(path).get("token") != token:
        atomic_json(path, {"token": token})


class VisionCheckpoint:
    def __init__(self, root: Path, provider: Any, prompt: str, images: list[Path], looks: list[str]):
        self.root, self.images = root, images
        kind = getattr(provider, "kind", type(provider).__name__)
        self.identity = {
            "model": getattr(provider, "model", ""),
            "effort": getattr(provider, "reasoning_effort", ""),
            "provider": getattr(kind, "value", str(kind)),
        }
        self.generations = {
            look: _json(root / "control/work/ai-checkpoints/generations" / f"{look}.json").get("token", "")
            for look in looks
        }
        self.prompt = read_only_vision_prompt(prompt)
        self.hashes = [hashlib.sha256(path.read_bytes()).hexdigest() for path in images]
        self.signature = {"schema": 1, **self.identity, "generations": self.generations,
                          "prompt": self.prompt, "images": self.hashes}
        key = hashlib.sha256(json.dumps(self.signature, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        self.path = root / "control/work/ai-checkpoints/results" / f"{key}.json"

    def read(self) -> str | None:
        saved = _json(self.path)
        if saved.get("signature") == self.signature and saved.get("status") == "completed" and isinstance(saved.get("text"), str):
            return saved["text"]
        # Recover successful exact-image responses from 0.2.76's audit log.
        # Never reuse them for an operator-requested fresh review.
        audits = self.root / "control/work/ai-exchanges"
        if self.identity["provider"] != "codex" or any(self.generations.values()) or not audits.is_dir():
            return None
        for path in sorted(audits.glob("*.json"), reverse=True)[:500]:
            record = _json(path)
            if (record.get("status") == "completed"
                    and record.get("transport") == "codex-app-server"
                    and record.get("requested_model") == self.identity["model"]
                    and record.get("requested_effort", "") == self.identity["effort"]
                    and record.get("prompt") == self.prompt
                    and [item.get("sha256") for item in record.get("images", []) if isinstance(item, dict)] == self.hashes
                    and isinstance(record.get("text"), str)):
                return record["text"]
        return None

    def save(self, raw: str) -> None:
        current = [hashlib.sha256(path.read_bytes()).hexdigest() for path in self.images]
        if current != self.hashes:
            raise ValueError("Proof images changed during the visual check; its result cannot be reused.")
        atomic_json(self.path, {"signature": self.signature, "status": "completed", "text": raw})
