"""Isolated multimodal requests over the installed Codex app-server protocol."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any


class CodexVisionError(RuntimeError):
    pass


class CodexVisionLimitError(CodexVisionError):
    pass


def _vision_error(detail: Any) -> CodexVisionError:
    encoded = json.dumps(detail, ensure_ascii=False).casefold()
    message = str(detail.get("message", detail) if isinstance(detail, dict) else detail)
    if "usagelimitexceeded" in encoded or "hit your usage limit" in encoded:
        return CodexVisionLimitError(message)
    return CodexVisionError(message)


BASE_INSTRUCTIONS = """You compare supplied fashion photographs by visible identity.
Use the attached images and the task only. Describe each side independently
before judging a match. A different model, garment type, or garment colour is
evidence against a match. Do not invent details in blank or unreadable images.
Do not use tools, commands, files, skills, memory, or other projects. Return the
requested JSON. The application performs all document changes separately.
"""

VISION_CONFIG: dict[str, Any] = {
    "project_doc_max_bytes": 0,
    "web_search": "disabled",
    "features.apps": False,
    "features.shell_tool": False,
    "features.unified_exec": False,
    "features.view_image": False,
    "features.browser_use": False,
    "features.code_mode_host": False,
    "features.skill_search": False,
    "features.tool_suggest": False,
    "features.sleep_tool": False,
    "features.workspace_dependencies": False,
}


class CodexVisionClient:
    """One reusable server, concurrent independent ephemeral image threads.

    Replies are routed by RPC id and notifications by thread id. A completed
    item is not success: only the matching completed turn authorizes a result.
    """

    def __init__(self, binary: Path) -> None:
        self.binary = binary
        self._lock = threading.RLock()
        self._start_lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self._requests: dict[int, queue.Queue[dict[str, Any]]] = {}
        self._threads: dict[str, queue.Queue[dict[str, Any]]] = {}
        self._next_id = 0

    def _send(self, message: dict[str, Any]) -> None:
        with self._lock:
            if self._process is None or self._process.poll() is not None:
                raise CodexVisionError("Соединение с Codex закрыто.")
            assert self._process.stdin is not None
            try:
                self._process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
                self._process.stdin.flush()
            except (OSError, ValueError) as error:
                raise CodexVisionError("Codex прервал соединение.") from error

    def _read(self, process: subprocess.Popen[str]) -> None:
        assert process.stdout is not None
        try:
            for line in process.stdout:
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(message, dict):
                    continue
                with self._lock:
                    if "method" not in message and "id" in message:
                        target = self._requests.get(message["id"])
                    else:
                        params = message.get("params") or {}
                        target = self._threads.get(params.get("threadId"))
                    if target is not None:
                        target.put(message)
                if "method" in message and "id" in message:
                    # Vision requests do not authorize any interactive tool.
                    try:
                        self._send({"id": message["id"], "error": {
                            "code": -32601, "message": "LOOKBOOKBOT vision does not permit tools or approval requests.",
                        }})
                    except CodexVisionError:
                        pass
        except (OSError, ValueError):
            pass
        finally:
            with self._lock:
                failure = {"error": {"message": "Codex закрыл соединение до завершения запроса."}}
                for target in [*self._requests.values(), *self._threads.values()]:
                    target.put(failure)

    @staticmethod
    def _receive(target: queue.Queue[dict[str, Any]], deadline: float) -> dict[str, Any]:
        try:
            message = target.get(timeout=max(0.0, deadline - time.monotonic()))
        except queue.Empty as error:
            raise CodexVisionError("Codex не завершил визуальный запрос за отведённое время.") from error
        if "error" in message:
            detail = message["error"]
            raise _vision_error(detail)
        return message

    def _rpc(self, method: str, params: dict[str, Any], deadline: float) -> dict[str, Any]:
        target: queue.Queue[dict[str, Any]] = queue.Queue()
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            self._requests[request_id] = target
        try:
            self._send({"id": request_id, "method": method, "params": params})
            result = self._receive(target, deadline).get("result")
            if not isinstance(result, dict):
                raise CodexVisionError(f"Codex вернул неполный ответ {method}.")
            return result
        finally:
            with self._lock:
                self._requests.pop(request_id, None)

    def _ensure_started(self, deadline: float) -> None:
        with self._start_lock:
            if self._process is not None:
                if self._process.poll() is not None:
                    raise CodexVisionError("Codex App Server остановлен; повторите этап.")
                return
            self._temporary = tempfile.TemporaryDirectory(prefix="lookbookbot-vision-")
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            try:
                self._process = subprocess.Popen(
                    [str(self.binary), "app-server"], cwd=self._temporary.name,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True, encoding="utf-8", errors="replace", bufsize=1, creationflags=flags,
                )
                threading.Thread(target=self._read, args=(self._process,), daemon=True,
                                 name="lookbookbot-vision-rpc").start()
                self._rpc("initialize", {"clientInfo": {
                    "name": "lookbookbot", "title": "LOOKBOOKBOT vision", "version": "0.2.77",
                }}, deadline)
                self._send({"method": "initialized", "params": {}})
            except (OSError, CodexVisionError):
                self.close()
                raise

    def inspect(self, prompt: str, images: list[Path], *, model: str, effort: str,
                schema: dict[str, Any] | None, timeout: int) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        self._ensure_started(deadline)
        assert self._temporary is not None
        started = self._rpc("thread/start", {
            "model": model, "cwd": self._temporary.name, "ephemeral": True,
            "approvalPolicy": "never", "sandbox": "read-only",
            "baseInstructions": BASE_INSTRUCTIONS,
            "config": VISION_CONFIG,
        }, deadline)
        thread_id = str((started.get("thread") or {}).get("id") or "")
        if not thread_id:
            raise CodexVisionError("Codex не вернул идентификатор визуального запроса.")
        events: queue.Queue[dict[str, Any]] = queue.Queue()
        with self._lock:
            self._threads[thread_id] = events
        turn_id = ""
        completed = False
        try:
            params: dict[str, Any] = {
                "threadId": thread_id, "model": model,
                "input": [{"type": "text", "text": prompt}, *[
                    {"type": "localImage", "path": str(path.resolve())} for path in images
                ]],
            }
            if effort:
                params["effort"] = effort
            if schema is not None:
                params["outputSchema"] = schema
            turn = self._rpc("turn/start", params, deadline).get("turn") or {}
            turn_id = str(turn.get("id") or "")
            if not turn_id:
                raise CodexVisionError("Codex не вернул идентификатор хода.")
            final_text = ""
            usage: dict[str, Any] = {}
            while True:
                message = self._receive(events, deadline)
                method, data = message.get("method"), message.get("params") or {}
                if data.get("turnId") not in (None, turn_id):
                    continue
                if method == "thread/tokenUsage/updated":
                    usage = data.get("tokenUsage") or {}
                elif method == "item/completed":
                    item = data.get("item") or {}
                    if item.get("type") == "agentMessage" and item.get("phase") != "commentary":
                        final_text = str(item.get("text") or "")
                    elif item.get("type") in {"commandExecution", "fileChange", "mcpToolCall", "dynamicToolCall"}:
                        raise CodexVisionError("Codex попытался использовать инструмент вместо сверки прикреплённых изображений.")
                elif method == "turn/completed":
                    result = data.get("turn") or {}
                    if result.get("id") != turn_id:
                        continue
                    completed = True  # The server ended this turn, even if it failed.
                    if result.get("status") != "completed":
                        raise _vision_error(result.get("error") or {"message": f"Визуальный запрос Codex не завершён: {result.get('status')}."})
                    # Some versions include complete items only in this event.
                    for item in result.get("items", []):
                        if item.get("type") == "agentMessage" and item.get("phase") != "commentary":
                            final_text = str(item.get("text") or final_text)
                    if not final_text.strip():
                        raise CodexVisionError("Codex завершил запрос без итогового ответа.")
                    completed = True
                    return {"text": final_text.strip(), "thread_id": thread_id, "turn_id": turn_id,
                            "model": started.get("model", model), "effort": effort or started.get("reasoningEffort"),
                            "usage": usage, "status": "completed"}
        finally:
            if turn_id and not completed:
                try:
                    self._rpc("turn/interrupt", {"threadId": thread_id, "turnId": turn_id},
                              time.monotonic() + 3)
                except CodexVisionError:
                    pass
            with self._lock:
                self._threads.pop(thread_id, None)

    def close(self) -> None:
        process = self._process
        if process is not None:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    stream.close()
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None
