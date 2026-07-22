from __future__ import annotations

import base64
import json
import subprocess
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import codex_binary
from .domain import ProviderKind


class ProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class VisionDecision:
    accepted: bool
    note: str
    raw: str = ""


class ModelProvider(ABC):
    kind: ProviderKind

    def __init__(self, model: str = "") -> None:
        self.model = model.strip()

    @abstractmethod
    def health(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def run_agent(self, prompt: str, workspace: Path, timeout: int = 7200) -> str:
        raise NotImplementedError

    def list_models(self) -> list[str]:
        return []

    def inspect_proof(self, prompt: str, images: list[Path]) -> VisionDecision:
        raise ProviderError(f"{self.kind.value} не поддерживает прямую проверку изображений.")


class CodexProvider(ModelProvider):
    kind = ProviderKind.CODEX

    def __init__(self, model: str = "", binary: Path | None = None) -> None:
        super().__init__(model)
        self.binary = binary or codex_binary()

    def health(self) -> str:
        if self.binary is None or not self.binary.is_file():
            raise ProviderError("Не найден Codex CLI. Установите или откройте приложение Codex.")
        try:
            result = subprocess.run(
                [str(self.binary), "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=15, stdin=subprocess.DEVNULL,
            )
        except OSError as error:
            raise ProviderError(f"Codex CLI найден, но не запускается: {error}") from error
        if result.returncode:
            raise ProviderError((result.stderr or result.stdout or "Codex CLI не запускается").strip())
        return result.stdout.strip() or f"Codex CLI: {self.binary}"

    def run_agent(self, prompt: str, workspace: Path, timeout: int = 7200) -> str:
        self.health()
        assert self.binary is not None
        command = [
            str(self.binary), "exec", "--json", "--sandbox", "danger-full-access",
            "--skip-git-repo-check", "--cd", str(workspace),
        ]
        if self.model:
            command.extend(["--model", self.model.removeprefix("codex/")])
        command.append(prompt)
        try:
            result = subprocess.run(
                command, cwd=workspace, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout, stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired as error:
            raise ProviderError("Codex превысил лимит времени. Контроллер будет перечитан перед повтором.") from error
        except OSError as error:
            raise ProviderError(f"Codex CLI не запустился: {error}") from error
        final = ""
        for line in result.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            item = event.get("item")
            if event.get("type") == "item.completed" and isinstance(item, dict):
                if item.get("type") in {"agent_message", "agentMessage"}:
                    candidate = item.get("text") or item.get("content")
                    if isinstance(candidate, str) and candidate.strip():
                        final = candidate.strip()
        if result.returncode and not final:
            detail = (result.stderr or result.stdout or "Codex error").strip()
            raise ProviderError(detail[-4000:])
        return final or result.stdout.strip()


class HttpVisionProvider(ModelProvider):
    endpoint: str

    def _request(self, url: str, payload: dict[str, Any] | None = None, method: str = "GET", timeout: int = 10) -> Any:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as error:
            raise ProviderError(f"Нет ответа от {url}: {error}") from error

    @staticmethod
    def _decision(text: str) -> VisionDecision:
        cleaned = text.strip()
        try:
            start, end = cleaned.find("{"), cleaned.rfind("}")
            value = json.loads(cleaned[start : end + 1])
            accepted = bool(value.get("match") if "match" in value else value.get("accepted"))
            note = str(value.get("note", "")).strip()
        except Exception as error:
            raise ProviderError(f"Модель вернула не JSON: {cleaned[:500]}") from error
        if accepted and len(note.split()) < 4:
            raise ProviderError("Модель не описала достаточные визуальные признаки совпадения.")
        return VisionDecision(accepted, note, cleaned)


class OllamaProvider(HttpVisionProvider):
    kind = ProviderKind.OLLAMA

    def __init__(self, model: str, endpoint: str = "http://127.0.0.1:11434") -> None:
        super().__init__(model)
        self.endpoint = endpoint.rstrip("/")

    def health(self) -> str:
        models = self.list_models()
        if self.model and self.model not in models:
            raise ProviderError(f"Модель Ollama не найдена: {self.model}")
        return f"Ollama: {len(models)} моделей"

    def list_models(self) -> list[str]:
        body = self._request(f"{self.endpoint}/api/tags")
        return sorted(str(item.get("name", "")) for item in body.get("models", []) if item.get("name"))

    def run_agent(self, prompt: str, workspace: Path, timeout: int = 7200) -> str:
        if not self.model:
            raise ProviderError("Не выбрана модель Ollama.")
        body = self._request(
            f"{self.endpoint}/api/chat",
            {"model": self.model, "stream": False, "messages": [{"role": "user", "content": prompt}]},
            method="POST", timeout=timeout,
        )
        return str(body.get("message", {}).get("content", "")).strip()

    def inspect_proof(self, prompt: str, images: list[Path]) -> VisionDecision:
        if not self.model:
            raise ProviderError("Не выбрана модель Ollama.")
        encoded = [base64.b64encode(path.read_bytes()).decode("ascii") for path in images]
        body = self._request(
            f"{self.endpoint}/api/chat",
            {
                "model": self.model,
                "stream": False,
                "format": "json",
                "messages": [{"role": "user", "content": prompt, "images": encoded}],
            },
            method="POST", timeout=600,
        )
        return self._decision(str(body.get("message", {}).get("content", "")))


class LlamaCppProvider(HttpVisionProvider):
    kind = ProviderKind.LLAMACPP

    def __init__(self, model: str = "local", endpoint: str = "http://127.0.0.1:8080") -> None:
        super().__init__(model or "local")
        self.endpoint = endpoint.rstrip("/")

    def health(self) -> str:
        self._request(f"{self.endpoint}/health")
        return f"llama.cpp: {self.endpoint}"

    def list_models(self) -> list[str]:
        try:
            body = self._request(f"{self.endpoint}/v1/models")
        except ProviderError:
            return [self.model]
        return [str(item.get("id")) for item in body.get("data", []) if item.get("id")]

    def _chat(self, messages: list[dict[str, Any]], timeout: int) -> str:
        body = self._request(
            f"{self.endpoint}/v1/chat/completions",
            {"model": self.model, "temperature": 0, "messages": messages},
            method="POST", timeout=timeout,
        )
        choices = body.get("choices", [])
        return str(choices[0].get("message", {}).get("content", "")).strip() if choices else ""

    def run_agent(self, prompt: str, workspace: Path, timeout: int = 7200) -> str:
        return self._chat([{"role": "user", "content": prompt}], timeout)

    def inspect_proof(self, prompt: str, images: list[Path]) -> VisionDecision:
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for path in images:
            mime = "image/png" if path.suffix.casefold() == ".png" else "image/jpeg"
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}})
        return self._decision(self._chat([{"role": "user", "content": content}], 600))


def make_provider(kind: ProviderKind, model: str, *, ollama_endpoint: str = "http://127.0.0.1:11434", llama_endpoint: str = "http://127.0.0.1:8080") -> ModelProvider:
    if kind == ProviderKind.CODEX:
        return CodexProvider(model)
    if kind == ProviderKind.OLLAMA:
        return OllamaProvider(model, ollama_endpoint)
    return LlamaCppProvider(model, llama_endpoint)
