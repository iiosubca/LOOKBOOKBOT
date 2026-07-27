from __future__ import annotations

import base64
import json
import os
import subprocess
import urllib.error
import urllib.parse
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
        return self._run_agent(prompt, workspace, timeout=timeout, sandbox="danger-full-access")

    def run_readonly_agent(self, prompt: str, workspace: Path, timeout: int = 7200) -> str:
        """Run an evidence-only Codex worker without filesystem write authority."""
        return self._run_agent(prompt, workspace, timeout=timeout, sandbox="read-only")

    def _run_agent(self, prompt: str, workspace: Path, *, timeout: int, sandbox: str) -> str:
        self.health()
        assert self.binary is not None
        command = [
            str(self.binary), "exec", "--json", "--sandbox", sandbox,
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


class GoogleAiStudioProvider(ModelProvider):
    """Gemini 3.5 Flash-Lite through Google AI Studio's OpenAI-compatible API."""

    kind = ProviderKind.GOOGLE
    endpoint = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"

    def __init__(self, model: str, api_key: str = "", usage_store: Any | None = None) -> None:
        super().__init__(model or "gemini-3.5-flash-lite")
        self.api_key = api_key.strip() or os.environ.get("GOOGLE_API_KEY", "").strip()
        self.usage_store = usage_store

    def _require_key(self) -> str:
        if not self.api_key:
            raise ProviderError("Введите ключ Google AI Studio или задайте переменную окружения GOOGLE_API_KEY.")
        return self.api_key

    def health(self) -> str:
        key = self._require_key()
        url = "https://generativelanguage.googleapis.com/v1beta/models/" + urllib.parse.quote(self.model, safe=".-_")
        request = urllib.request.Request(url, method="GET", headers={"x-goog-api-key": key})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as error:
            raise ProviderError(f"Google AI Studio недоступен для {self.model}: {error}") from error
        if not data.get("name"):
            raise ProviderError(f"Google AI Studio не подтвердил модель {self.model}.")
        return f"Google AI Studio: {self.model}"

    def list_models(self) -> list[str]:
        return [self.model]

    def _chat(self, messages: list[dict[str, Any]], *, estimated_input_tokens: int, timeout: int) -> str:
        key = self._require_key()
        reservation_id: int | None = None
        if self.usage_store is not None:
            try:
                reservation_id = self.usage_store.reserve_google_request(estimated_input_tokens)
            except ValueError as error:
                raise ProviderError(str(error)) from error
        payload = {"model": self.model, "messages": messages}
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise ProviderError(f"Google AI Studio {error.code}: {detail[-1200:]}") from error
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise ProviderError(f"Google AI Studio: {error}") from error
        finally:
            # API errors may still consume a request; the conservative
            # reservation intentionally remains in the local quota ledger.
            pass
        if reservation_id is not None:
            prompt_tokens, completion_tokens = _google_usage_tokens(data)
            self.usage_store.settle_google_request(reservation_id, prompt_tokens, completion_tokens)
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError(f"Google AI Studio вернул ответ без choices: {str(data)[:800]}")
        content = choices[0].get("message", {}).get("content", "")
        if isinstance(content, list):
            content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
        return str(content).strip()

    def run_agent(self, prompt: str, workspace: Path, timeout: int = 7200) -> str:
        del workspace
        return self._chat(
            [{"role": "user", "content": prompt}],
            estimated_input_tokens=_estimate_google_tokens(prompt), timeout=timeout,
        )

    def inspect_proof(self, prompt: str, images: list[Path]) -> VisionDecision:
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for path in images:
            mime = "image/png" if path.suffix.casefold() == ".png" else "image/jpeg"
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}})
        result = self._chat(
            [{"role": "user", "content": content}],
            estimated_input_tokens=_estimate_google_tokens(prompt, image_count=len(images)), timeout=600,
        )
        return self._decision(result)


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


def _estimate_google_tokens(text: str, image_count: int = 0) -> int:
    return max(1, len(text.encode("utf-8")) // 4) + image_count * 4096


def _google_usage_tokens(payload: dict[str, Any]) -> tuple[int | None, int | None]:
    usage = payload.get("usage") or payload.get("usageMetadata") or {}
    if not isinstance(usage, dict):
        return None, None
    prompt = usage.get("prompt_tokens", usage.get("promptTokenCount", usage.get("input_tokens")))
    completion = usage.get("completion_tokens", usage.get("candidatesTokenCount", usage.get("output_tokens")))
    return _optional_int(prompt), _optional_int(completion)


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def make_provider(
    kind: ProviderKind,
    model: str,
    *,
    ollama_endpoint: str = "http://127.0.0.1:11434",
    llama_endpoint: str = "http://127.0.0.1:8080",
    google_api_key: str = "",
    usage_store: Any | None = None,
) -> ModelProvider:
    if kind == ProviderKind.CODEX:
        return CodexProvider(model)
    if kind == ProviderKind.OLLAMA:
        return OllamaProvider(model, ollama_endpoint)
    if kind == ProviderKind.GOOGLE:
        return GoogleAiStudioProvider(model, google_api_key, usage_store)
    return LlamaCppProvider(model, llama_endpoint)
