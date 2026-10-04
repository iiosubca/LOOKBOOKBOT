from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import codex_binary


class CodexCatalogError(RuntimeError):
    pass


@dataclass(frozen=True)
class CodexModel:
    model: str
    display_name: str
    reasoning_efforts: tuple[str, ...]
    default_reasoning_effort: str


def _parse_model(entry: dict[str, Any]) -> CodexModel | None:
    if entry.get("hidden") is True:
        return None
    # Older Codex catalogs omit modalities; the app-server contract treats
    # that omission as support for text and image.
    modalities = entry.get("inputModalities")
    if isinstance(modalities, list) and "image" not in modalities:
        return None
    model = str(entry.get("model") or entry.get("id") or "").strip()
    if not model:
        return None
    raw_efforts = entry.get("supportedReasoningEfforts")
    efforts = tuple(dict.fromkeys(
        str(item.get("reasoningEffort", "")).strip()
        for item in raw_efforts if isinstance(item, dict) and item.get("reasoningEffort")
    )) if isinstance(raw_efforts, list) else ()
    default = str(entry.get("defaultReasoningEffort") or "").strip()
    if default not in efforts:
        default = efforts[0] if efforts else ""
    return CodexModel(
        model=model,
        display_name=str(entry.get("displayName") or model).strip(),
        reasoning_efforts=efforts,
        default_reasoning_effort=default,
    )


def list_codex_models(*, binary: Path | None = None, timeout: float = 25.0) -> list[CodexModel]:
    """Read this account's picker catalog from the installed Codex app-server.

    A separate short-lived stdio connection keeps discovery independent from
    production workers and avoids reading undocumented desktop cache files.
    The whole paginated exchange shares one deadline.
    """
    binary = binary or codex_binary()
    if binary is None or not binary.is_file():
        raise CodexCatalogError("Не найден Codex CLI для проверки списка моделей.")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        process = subprocess.Popen(
            [str(binary), "app-server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=flags,
        )
    except OSError as error:
        raise CodexCatalogError(f"Не удалось открыть каталог Codex: {error}") from error

    messages: queue.Queue[str | None] = queue.Queue()

    def read_stdout() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            messages.put(line)
        messages.put(None)

    threading.Thread(target=read_stdout, name="lookbookbot-codex-catalog-reader", daemon=True).start()
    deadline = time.monotonic() + timeout
    models: list[CodexModel] = []
    seen: set[str] = set()

    def send(payload: dict[str, Any]) -> None:
        try:
            assert process.stdin is not None
            process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            process.stdin.flush()
        except (OSError, BrokenPipeError) as error:
            raise CodexCatalogError("Codex закрыл соединение до получения списка моделей.") from error

    try:
        send({
            "method": "initialize", "id": 1,
            "params": {"clientInfo": {"name": "lookbookbot", "title": "LOOKBOOKBOT", "version": "0.2.77"}},
        })
        send({"method": "initialized", "params": {}})
        send({"method": "model/list", "id": 2, "params": {"limit": 100, "includeHidden": False}})
        request_id = 2
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodexCatalogError("Codex не ответил на запрос списка моделей вовремя.")
            try:
                line = messages.get(timeout=remaining)
            except queue.Empty as error:
                raise CodexCatalogError("Codex не ответил на запрос списка моделей вовремя.") from error
            if line is None:
                raise CodexCatalogError("Codex закрыл каталог без полного списка моделей.")
            try:
                reply = json.loads(line)
            except json.JSONDecodeError:
                continue
            if reply.get("id") == 1 and "error" in reply:
                raise CodexCatalogError("Codex отклонил подключение к каталогу моделей.")
            if reply.get("id") != request_id:
                continue
            if "error" in reply:
                error = reply["error"]
                detail = error.get("message", "неизвестная ошибка") if isinstance(error, dict) else str(error)
                raise CodexCatalogError(f"Codex не предоставил список моделей: {detail}")
            result = reply.get("result")
            if not isinstance(result, dict) or not isinstance(result.get("data"), list):
                raise CodexCatalogError("Codex вернул неполный формат списка моделей.")
            for item in result["data"]:
                if not isinstance(item, dict):
                    continue
                parsed = _parse_model(item)
                if parsed and parsed.model not in seen:
                    seen.add(parsed.model)
                    models.append(parsed)
            cursor = result.get("nextCursor")
            if not cursor:
                break
            request_id += 1
            send({"method": "model/list", "id": request_id, "params": {
                "limit": 100, "includeHidden": False, "cursor": str(cursor),
            }})
        if not models:
            raise CodexCatalogError("Codex не вернул ни одной доступной модели с поддержкой изображений.")
        return models
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
