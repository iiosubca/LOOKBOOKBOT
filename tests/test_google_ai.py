from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from lookbookbot.domain import ProviderKind
from lookbookbot.providers import GoogleAiStudioProvider, ProviderError
from lookbookbot.state import StateStore


class _Response:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None


def test_google_usage_reserves_and_accounts_actual_tokens(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")

    reservation = store.reserve_google_request(100)
    store.settle_google_request(reservation, 70, 30)
    status = store.google_usage_status()

    assert status["rpm_used"] == 1
    assert status["tpm_used"] == 100
    assert status["rpd_used"] == 1


def test_google_usage_blocks_sixteenth_request_in_current_minute(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    for _ in range(15):
        store.reserve_google_request(1)

    with pytest.raises(ValueError, match="RPM 15/15"):
        store.reserve_google_request(1)


def test_google_provider_uses_compatible_endpoint_and_updates_usage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = StateStore(tmp_path / "state.db")
    requests: list[dict] = []

    def fake_urlopen(request, timeout: int):
        assert timeout == 7200
        assert request.full_url.endswith("/openai/chat/completions")
        assert request.get_header("Authorization") == "Bearer secret"
        requests.append(json.loads(request.data.decode("utf-8")))
        return _Response({
            "choices": [{"message": {"content": "done"}}],
            "usage": {"prompt_tokens": 123, "completion_tokens": 45},
        })

    monkeypatch.setattr("lookbookbot.providers.urllib.request.urlopen", fake_urlopen)
    provider = GoogleAiStudioProvider("gemini-3.5-flash-lite", "secret", store)

    assert provider.run_agent("make proof", tmp_path) == "done"
    assert requests == [{"model": "gemini-3.5-flash-lite", "messages": [{"role": "user", "content": "make proof"}]}]
    assert store.google_usage_status()["tpm_used"] == 168


def test_google_provider_requires_a_key() -> None:
    provider = GoogleAiStudioProvider("gemini-3.5-flash-lite", "")
    with pytest.raises(ProviderError, match="Google AI Studio"):
        provider.run_agent("x", Path("."))
