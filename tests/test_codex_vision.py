from __future__ import annotations

import io
import json
import queue
from pathlib import Path
from types import SimpleNamespace

import pytest

from lookbookbot.codex_vision import CodexVisionClient, CodexVisionError


def _client(tmp_path: Path, monkeypatch, status: str):
    client = CodexVisionClient(tmp_path / "codex.exe")
    client._temporary = SimpleNamespace(name=str(tmp_path))
    monkeypatch.setattr(client, "_ensure_started", lambda _: None)
    calls = []

    def rpc(method, params, _deadline):
        calls.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": "thread-1"}, "model": "gpt-6.1-sol"}
        if method == "turn/start":
            # Notifications can precede the turn/start RPC acknowledgement.
            events = client._threads["thread-1"]
            events.put({"method": "item/completed", "params": {"threadId": "thread-1", "turnId": "turn-1",
                        "item": {"type": "agentMessage", "phase": "final_answer", "text": '{"accepted":false}'}}})
            events.put({"method": "turn/completed", "params": {"threadId": "thread-1",
                        "turn": {"id": "turn-1", "status": status, "items": []}}})
            return {"turn": {"id": "turn-1"}}
        return {}

    monkeypatch.setattr(client, "_rpc", rpc)
    return client, calls


def test_model_effort_pixels_and_schema_are_explicit(tmp_path, monkeypatch):
    client, calls = _client(tmp_path, monkeypatch, "completed")
    schema = {"type": "object"}
    result = client.inspect("compare", [tmp_path / "photo.jpg"], model="gpt-6.1-sol", effort="low",
                            schema=schema, timeout=10)
    thread, turn = calls
    assert thread[1]["ephemeral"] is True
    assert thread[1]["cwd"] == str(tmp_path)
    assert thread[1]["config"]["project_doc_max_bytes"] == 0
    assert thread[1]["config"]["features.shell_tool"] is False
    assert thread[1]["config"]["features.apps"] is False
    assert thread[1]["config"]["web_search"] == "disabled"
    assert turn[1]["model"] == "gpt-6.1-sol"
    assert turn[1]["effort"] == "low"
    assert turn[1]["input"][1] == {"type": "localImage", "path": str((tmp_path / "photo.jpg").resolve())}
    assert turn[1]["outputSchema"] == schema
    assert result["text"] == '{"accepted":false}'
    assert result["status"] == "completed"


@pytest.mark.parametrize("status", ["failed", "interrupted"])
def test_partial_final_message_is_not_a_success(tmp_path, monkeypatch, status):
    client, calls = _client(tmp_path, monkeypatch, status)
    with pytest.raises(CodexVisionError, match="не завершён"):
        client.inspect("compare", [], model="gpt-6.1-sol", effort="low", schema=None, timeout=10)
    assert calls[-1][0] == "turn/start"  # A failed/completed turn is already over.


def test_usage_limit_has_a_distinct_non_retryable_error(tmp_path, monkeypatch):
    from lookbookbot.codex_vision import CodexVisionLimitError
    client, _ = _client(tmp_path, monkeypatch, "failed")
    original = client._rpc

    def rpc(method, params, deadline):
        result = original(method, params, deadline)
        if method == "turn/start":
            events = client._threads["thread-1"]
            events.get()
            events.get()
            events.put({"method": "turn/completed", "params": {"threadId": "thread-1", "turn": {
                "id": "turn-1", "status": "failed", "error": {"codexErrorInfo": "usageLimitExceeded", "message": "Quota ended"},
            }}})
        return result

    monkeypatch.setattr(client, "_rpc", rpc)
    with pytest.raises(CodexVisionLimitError, match="Quota ended"):
        client.inspect("compare", [], model="gpt-6-luna", effort="low", schema=None, timeout=10)


def test_interleaved_replies_are_routed_to_their_own_thread(tmp_path):
    client = CodexVisionClient(tmp_path / "codex.exe")
    first, second = queue.Queue(), queue.Queue()
    client._threads = {"a": first, "b": second}
    messages = [{"method": "item/completed", "params": {"threadId": key, "item": {"text": value}}}
                for key, value in [("b", "second"), ("a", "first")]]
    process = SimpleNamespace(stdout=io.StringIO("\n".join(json.dumps(x) for x in messages)))
    client._read(process)
    assert first.get()["params"]["item"]["text"] == "first"
    assert second.get()["params"]["item"]["text"] == "second"
