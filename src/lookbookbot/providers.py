from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .ai_policy import read_only_vision_prompt
from .config import codex_binary
from .codex_vision import CodexVisionClient, CodexVisionError, CodexVisionLimitError
from .domain import ProviderKind


CODEX_STANDARD_MODEL = "gpt-5.6-terra"
"""Economical default for ordinary LOOKBOOKBOT vision work."""

CODEX_ESCALATION_MODEL = "gpt-5.6-sol"
"""Used only to recheck a bounded, unresolved visual decision."""


def _background_creationflags() -> int:
    """Keep short-lived CLI workers out of Windows Terminal.

    LOOKBOOKBOT is a windowed application.  Without this flag Windows can
    attach every concurrent ``codex exec`` worker to a fresh Terminal tab.
    The workers communicate only through captured stdout/stderr, so no
    visible console is required.
    """
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


class ProviderError(RuntimeError):
    pass


class ProviderLimitError(ProviderError):
    """A quota stop, not a formatting problem and never an immediate retry."""


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

    def close(self) -> None:
        """Release a reusable provider connection at the end of a pipeline."""

    def inspect_proof(self, prompt: str, images: list[Path]) -> VisionDecision:
        raise ProviderError(f"{self.kind.value} не поддерживает прямую проверку изображений.")

    def run_readonly_vision(
        self,
        prompt: str,
        workspace: Path,
        *,
        images: list[Path],
        timeout: int = 3600,
    ) -> str:
        """Return the raw read-only vision response for closed-board choices.

        HTTP vision providers already expose a parsed ``inspect_proof`` method;
        the raw response is retained there so the controller can validate a
        closed candidate label itself.  Codex overrides this to use its
        read-only CLI sandbox.
        """
        del workspace, timeout
        return self.inspect_proof(prompt, images).raw


class CodexProvider(ModelProvider):
    kind = ProviderKind.CODEX

    def __init__(self, model: str = "", binary: Path | None = None, reasoning_effort: str = "") -> None:
        # A blank model delegates to the desktop app's changing default.  That
        # previously promoted ordinary lookbook work to a frontier model after
        # a Codex update.  Keep the production baseline explicit and stable.
        super().__init__(model.strip() or CODEX_STANDARD_MODEL)
        self.binary = binary or codex_binary()
        self.reasoning_effort = reasoning_effort.strip()
        self._vision_client: CodexVisionClient | None = None
        self._vision_lock = threading.Lock()
        self._escalation: CodexProvider | None = None

    def escalation_provider(self) -> "CodexProvider":
        """Return the stronger, isolated reviewer without changing the main run.

        The caller uses this only after Terra has produced an unusable or
        disputed result.  Reusing the resolved binary keeps the provider
        session/authentication identical while changing only ``--model``.
        """
        if self.model == CODEX_ESCALATION_MODEL:
            return self
        # A recovery worker has its own model. Let that model's catalog default
        # apply instead of forwarding an effort the recovery model may reject.
        if self._escalation is None:
            self._escalation = CodexProvider(CODEX_ESCALATION_MODEL, binary=self.binary)
        return self._escalation

    def health(self) -> str:
        if self.binary is None or not self.binary.is_file():
            raise ProviderError("Не найден Codex CLI. Установите или откройте приложение Codex.")
        try:
            result = subprocess.run(
                [str(self.binary), "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=15, stdin=subprocess.DEVNULL, creationflags=_background_creationflags(),
            )
        except OSError as error:
            raise ProviderError(f"Codex CLI найден, но не запускается: {error}") from error
        if result.returncode:
            raise ProviderError((result.stderr or result.stdout or "Codex CLI не запускается").strip())
        return result.stdout.strip() or f"Codex CLI: {self.binary}"

    def run_agent(self, prompt: str, workspace: Path, timeout: int = 7200) -> str:
        return self._run_agent(prompt, workspace, timeout=timeout, sandbox="danger-full-access")

    def run_readonly_agent(
        self,
        prompt: str,
        workspace: Path,
        timeout: int = 7200,
        *,
        images: list[Path] | None = None,
    ) -> str:
        """Run an evidence-only Codex worker with explicit proof-card attachments.

        A CLI worker cannot infer that a textual local path should be interpreted
        as an image.  Passing the fixed proof cards via ``--image`` gives it the
        actual pixels while the read-only sandbox prevents any project mutation.
        """
        attachments = list(images or [])
        missing = [str(path) for path in attachments if not path.is_file()]
        if missing:
            raise ProviderError("Не найдены карточки для визуальной сверки: " + ", ".join(missing))
        return self._run_agent(
            read_only_vision_prompt(prompt), workspace, timeout=timeout, sandbox="read-only", images=attachments,
            ephemeral=True,
        )

    def run_readonly_vision(
        self,
        prompt: str,
        workspace: Path,
        *,
        images: list[Path],
        timeout: int = 3600,
    ) -> str:
        return self._run_vision(prompt, workspace, images=images, timeout=timeout)

    def close(self) -> None:
        if self._vision_client is not None:
            self._vision_client.close()
            self._vision_client = None
        if self._escalation is not None:
            self._escalation.close()
            self._escalation = None

    def _run_vision(
        self, prompt: str, workspace: Path, *, images: list[Path], timeout: int,
        output_schema: dict[str, Any] | None = None,
    ) -> str:
        missing = [str(path) for path in images if not path.is_file()]
        if missing:
            raise ProviderError("Не найдены изображения для Codex: " + ", ".join(missing))
        if self.binary is None or not self.binary.is_file():
            raise ProviderError("Не найден Codex App Server.")
        with self._vision_lock:
            if self._vision_client is None:
                self._vision_client = CodexVisionClient(self.binary)
        task = read_only_vision_prompt(prompt)
        start = time.monotonic()
        audit: dict[str, Any] = {
            "transport": "codex-app-server", "requested_model": self.model,
            "requested_effort": self.reasoning_effort, "prompt": task,
            "images": [{"path": str(path.resolve()), "bytes": path.stat().st_size,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path in images],
            "output_schema": output_schema,
        }
        try:
            result = self._vision_client.inspect(
                task, images, model=self.model.removeprefix("codex/"), effort=self.reasoning_effort,
                schema=output_schema, timeout=min(timeout, 600),
            )
            audit.update(result)
            return str(result["text"])
        except CodexVisionLimitError as error:
            audit.update(status="failed", error=str(error), error_code="usageLimitExceeded")
            raise ProviderLimitError(
                "Достигнут лимит Codex. Завершённые проверки сохранены; после обновления лимита "
                "нажмите «Продолжить с места остановки». " + str(error)
            ) from error
        except (CodexVisionError, OSError) as error:
            audit.update(status="failed", error=str(error))
            raise ProviderError(str(error)) from error
        finally:
            audit["elapsed_seconds"] = round(time.monotonic() - start, 3)
            # Record the exact pixels, final answer and transport outcome. No
            # authentication config or tokens are copied into project logs.
            try:
                folder = workspace / "control" / "work" / "ai-exchanges"
                folder.mkdir(parents=True, exist_ok=True)
                (folder / f"{time.time_ns()}-{uuid.uuid4().hex[:8]}.json").write_text(
                    json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
                )
            except OSError:
                pass

    def run_readonly_closed_board_vision(
        self,
        prompt: str,
        workspace: Path,
        *,
        images: list[Path],
        look_id: str,
        allowed_labels: list[str],
        allow_no_match: bool = False,
        timeout: int = 3600,
    ) -> str:
        """Choose one exact label from a controlled visual candidate board.

        Codex's final-message file is more stable than parsing its progress
        stream, and a CLI output schema prevents a free-form agent response
        from becoming a bogus Excel assignment.
        """
        labels = [str(label).strip() for label in allowed_labels if str(label).strip()]
        if not labels:
            raise ProviderError(f"{look_id}: закрытый список кандидатов пуст.")
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "look_id": {"type": "string", "enum": [look_id]},
                "choice": {"type": "string", "enum": labels + (["NONE"] if allow_no_match else [])},
                "note": {"type": "string", "minLength": 12},
            },
            "required": ["look_id", "choice", "note"],
        }
        if allow_no_match:
            schema["properties"]["matched"] = {"type": "boolean"}
            schema["required"].append("matched")
        return self._run_vision(prompt, workspace, timeout=timeout, images=images, output_schema=schema)

    def run_readonly_decision_vision(
        self, prompt: str, workspace: Path, *, images: list[Path], look_ids: list[str],
        timeout: int = 600,
    ) -> str:
        schema = {
            "type": "object", "additionalProperties": False,
            "properties": {"decisions": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "look_id": {"type": "string", "enum": look_ids},
                    "accepted": {"type": "boolean"}, "note": {"type": "string"},
                    "excel_observation": {"type": "string"},
                    "reference_observation": {"type": "string"},
                    "contradictions": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["look_id", "accepted", "note", "excel_observation",
                             "reference_observation", "contradictions"],
            }}}, "required": ["decisions"],
        }
        return self._run_vision(prompt, workspace, timeout=timeout, images=images, output_schema=schema)

    def _run_agent(
        self,
        prompt: str,
        workspace: Path,
        *,
        timeout: int,
        sandbox: str,
        images: list[Path] | None = None,
        output_schema: dict[str, Any] | None = None,
        ephemeral: bool = False,
    ) -> str:
        self.health()
        assert self.binary is not None
        with tempfile.TemporaryDirectory(prefix="lookbookbot-codex-") as temporary_dir:
            temporary_root = Path(temporary_dir)
            final_message = temporary_root / "last-message.txt"
            command = [
                str(self.binary), "exec", "--json", "--sandbox", sandbox,
                "--skip-git-repo-check", "--cd", str(workspace),
                "--output-last-message", str(final_message),
            ]
            if ephemeral:
                command.append("--ephemeral")
            if output_schema is not None:
                schema_path = temporary_root / "output-schema.json"
                schema_path.write_text(json.dumps(output_schema, ensure_ascii=False), encoding="utf-8")
                command.extend(["--output-schema", str(schema_path)])
            if self.model:
                command.extend(["--model", self.model.removeprefix("codex/")])
            if self.reasoning_effort:
                command.extend(["--config", f"model_reasoning_effort={self.reasoning_effort}"])
            if images:
                # ``--image`` accepts several values.  Use the ``--image=...``
                # form for every attachment so the final text prompt can never be
                # consumed as another image filename by the CLI parser.
                command.extend(f"--image={path}" for path in images)
            command.append(prompt)
            try:
                result = subprocess.run(
                    command, cwd=workspace, capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=timeout, stdin=subprocess.DEVNULL, creationflags=_background_creationflags(),
                )
            except subprocess.TimeoutExpired as error:
                raise ProviderError("Codex превысил лимит времени. Контроллер будет перечитан перед повтором.") from error
            except OSError as error:
                raise ProviderError(f"Codex CLI не запустился: {error}") from error

            final = ""
            try:
                final = final_message.read_text(encoding="utf-8").strip()
            except OSError:
                pass
            if not final:
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
            if result.returncode:
                detail = (result.stderr or result.stdout or "Codex error").strip()
                raise ProviderError(detail[-4000:])
            if not final:
                detail = (result.stderr or result.stdout or "Codex did not return a final message").strip()
                raise ProviderError(detail[-4000:])
            return final


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
        prompt = read_only_vision_prompt(prompt)
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


class OpenAiApiProvider(ModelProvider):
    """Direct OpenAI Responses API provider for text and proof-card vision."""

    kind = ProviderKind.OPENAI
    endpoint = "https://api.openai.com/v1/responses"
    models_endpoint = "https://api.openai.com/v1/models"

    def __init__(self, model: str, api_key: str = "") -> None:
        super().__init__(model or "gpt-5.6")
        self.api_key = api_key.strip() or os.environ.get("OPENAI_API_KEY", "").strip()

    def _require_key(self) -> str:
        if not self.api_key:
            raise ProviderError("Введите ключ OpenAI API или задайте переменную окружения OPENAI_API_KEY.")
        return self.api_key

    def health(self) -> str:
        key = self._require_key()
        url = self.models_endpoint + "/" + urllib.parse.quote(self.model, safe=".-_")
        request = urllib.request.Request(url, method="GET", headers={"Authorization": f"Bearer {key}"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise ProviderError(f"OpenAI API {error.code}: {detail[-1200:]}") from error
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise ProviderError(f"OpenAI API недоступен для {self.model}: {error}") from error
        if data.get("id") != self.model:
            raise ProviderError(f"OpenAI API не подтвердил модель {self.model}.")
        return f"OpenAI API: {self.model}"

    def list_models(self) -> list[str]:
        # The editable combobox retains the model explicitly chosen by the
        # user.  Avoid a second billed or permission-sensitive API call after
        # the health check merely to populate a one-item list.
        return [self.model]

    def _responses(self, input_data: str | list[dict[str, Any]], timeout: int) -> str:
        key = self._require_key()
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps({"model": self.model, "input": input_data}).encode("utf-8"),
            method="POST",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise ProviderError(f"OpenAI API {error.code}: {detail[-1200:]}") from error
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise ProviderError(f"OpenAI API: {error}") from error
        return _openai_response_text(data)

    def run_agent(self, prompt: str, workspace: Path, timeout: int = 7200) -> str:
        del workspace
        return self._responses(prompt, timeout)

    def inspect_proof(self, prompt: str, images: list[Path]) -> VisionDecision:
        prompt = read_only_vision_prompt(prompt)
        content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
        for path in images:
            mime = "image/png" if path.suffix.casefold() == ".png" else "image/jpeg"
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            content.append({
                "type": "input_image",
                "image_url": f"data:{mime};base64,{encoded}",
                "detail": "high",
            })
        result = self._responses([{"role": "user", "content": content}], 600)
        return HttpVisionProvider._decision(result)


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


class OpenRouterProvider(HttpVisionProvider):
    """OpenRouter's OpenAI-compatible chat API for text and image proofs."""

    kind = ProviderKind.OPENROUTER
    endpoint = "https://openrouter.ai/api/v1/chat/completions"
    models_endpoint = "https://openrouter.ai/api/v1/models"

    def __init__(self, model: str, api_key: str = "") -> None:
        super().__init__(model or "google/gemini-3.8-flash")
        self.api_key = api_key.strip() or os.environ.get("OPENROUTER_API_KEY", "").strip()

    def _require_key(self) -> str:
        if not self.api_key:
            raise ProviderError("Введите ключ OpenRouter API или задайте переменную окружения OPENROUTER_API_KEY.")
        return self.api_key

    def _request_openrouter(
        self,
        url: str,
        payload: dict[str, Any] | None = None,
        *,
        method: str = "GET",
        timeout: int = 20,
    ) -> Any:
        key = self._require_key()
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/lookbookbot",
                "X-OpenRouter-Title": "LOOKBOOKBOT",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise ProviderError(f"OpenRouter API {error.code}: {detail[-1600:]}") from error
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise ProviderError(f"OpenRouter API недоступен: {error}") from error

    def health(self) -> str:
        body = self._request_openrouter(self.models_endpoint)
        models = body.get("data", []) if isinstance(body, dict) else []
        available = {str(item.get("id", "")) for item in models if isinstance(item, dict)}
        if self.model not in available:
            raise ProviderError(f"OpenRouter не подтвердил модель {self.model}.")
        return f"OpenRouter: {self.model}"

    def list_models(self) -> list[str]:
        return [self.model]

    @staticmethod
    def _message_text(body: Any) -> str:
        choices = body.get("choices", []) if isinstance(body, dict) else []
        if not choices or not isinstance(choices[0], dict):
            raise ProviderError(f"OpenRouter вернул ответ без choices: {str(body)[:800]}")
        message = choices[0].get("message", {})
        content = message.get("content", "") if isinstance(message, dict) else ""
        if isinstance(content, list):
            content = "".join(
                str(part.get("text", "")) for part in content if isinstance(part, dict)
            )
        text = str(content).strip()
        if not text:
            raise ProviderError(f"OpenRouter вернул ответ без текста: {str(body)[:800]}")
        return text

    def _chat(self, messages: list[dict[str, Any]], timeout: int) -> str:
        return self._message_text(self._request_openrouter(
            self.endpoint,
            {"model": self.model, "temperature": 0, "messages": messages},
            method="POST",
            timeout=timeout,
        ))

    def run_agent(self, prompt: str, workspace: Path, timeout: int = 7200) -> str:
        del workspace
        return self._chat([{"role": "user", "content": prompt}], timeout)

    def inspect_proof(self, prompt: str, images: list[Path]) -> VisionDecision:
        prompt = read_only_vision_prompt(prompt)
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for path in images:
            mime = "image/png" if path.suffix.casefold() == ".png" else "image/jpeg"
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}})
        return self._decision(self._chat([{"role": "user", "content": content}], 600))


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
        prompt = read_only_vision_prompt(prompt)
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
        prompt = read_only_vision_prompt(prompt)
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


def _openai_response_text(payload: dict[str, Any]) -> str:
    """Extract the public Responses API text shape without an SDK dependency."""
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    texts: list[str] = []
    output = payload.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "output_text":
                    continue
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    texts.append(text.strip())
    result = "\n".join(texts).strip()
    if not result:
        raise ProviderError(f"OpenAI API вернул ответ без текста: {str(payload)[:800]}")
    return result


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def make_provider(
    kind: ProviderKind,
    model: str,
    *,
    reasoning_effort: str = "",
    ollama_endpoint: str = "http://127.0.0.1:11434",
    llama_endpoint: str = "http://127.0.0.1:8080",
    google_api_key: str = "",
    openai_api_key: str = "",
    openrouter_api_key: str = "",
    usage_store: Any | None = None,
) -> ModelProvider:
    if kind == ProviderKind.CODEX:
        return CodexProvider(model, reasoning_effort=reasoning_effort)
    if kind == ProviderKind.OLLAMA:
        return OllamaProvider(model, ollama_endpoint)
    if kind == ProviderKind.GOOGLE:
        return GoogleAiStudioProvider(model, google_api_key, usage_store)
    if kind == ProviderKind.OPENAI:
        return OpenAiApiProvider(model, openai_api_key)
    if kind == ProviderKind.OPENROUTER:
        return OpenRouterProvider(model, openrouter_api_key)
    return LlamaCppProvider(model, llama_endpoint)
