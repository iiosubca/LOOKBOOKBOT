from __future__ import annotations

import json
from pathlib import Path

import pytest

from lookbookbot.domain import ProviderKind
from lookbookbot.providers import OpenAiApiProvider, ProviderError, make_provider


class _Response:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None


def test_openai_provider_uses_responses_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[dict] = []

    def fake_urlopen(request, timeout: int):
        assert timeout == 7200
        assert request.full_url == "https://api.openai.com/v1/responses"
        assert request.get_header("Authorization") == "Bearer secret"
        requests.append(json.loads(request.data.decode("utf-8")))
        return _Response({"output": [{"type": "message", "content": [{"type": "output_text", "text": "done"}]}]})

    monkeypatch.setattr("lookbookbot.providers.urllib.request.urlopen", fake_urlopen)
    provider = OpenAiApiProvider("gpt-5.6", "secret")

    assert provider.run_agent("make proof", tmp_path) == "done"
    assert requests == [{"model": "gpt-5.6", "input": "make proof"}]


def test_openai_provider_passes_proof_card_as_responses_image_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    image = tmp_path / "proof.png"
    image.write_bytes(b"png proof")
    requests: list[dict] = []

    def fake_urlopen(request, timeout: int):
        assert timeout == 600
        requests.append(json.loads(request.data.decode("utf-8")))
        return _Response({"output_text": '{"accepted": true, "note": "garment and bag visibly match"}'})

    monkeypatch.setattr("lookbookbot.providers.urllib.request.urlopen", fake_urlopen)
    decision = OpenAiApiProvider("gpt-5.6", "secret").inspect_proof("Inspect this", [image])

    assert decision.accepted is True
    content = requests[0]["input"][0]["content"]
    assert content[0]["type"] == "input_text"
    assert content[1]["type"] == "input_image"
    assert content[1]["image_url"].startswith("data:image/png;base64,")
    assert content[1]["detail"] == "high"


def test_openai_provider_requires_a_key() -> None:
    provider = OpenAiApiProvider("gpt-5.6", "")
    with pytest.raises(ProviderError, match="OpenAI API"):
        provider.run_agent("x", Path("."))


def test_provider_factory_selects_openai_api() -> None:
    provider = make_provider(ProviderKind.OPENAI, "gpt-5.6", openai_api_key="secret")
    assert isinstance(provider, OpenAiApiProvider)
