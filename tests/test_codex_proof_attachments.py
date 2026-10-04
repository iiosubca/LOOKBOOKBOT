from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from lookbookbot.providers import (
    CODEX_ESCALATION_MODEL,
    CODEX_STANDARD_MODEL,
    CodexProvider,
    _background_creationflags,
)


def test_codex_uses_terra_by_default_and_keeps_sol_as_an_isolated_escalation(tmp_path: Path) -> None:
    binary = tmp_path / "codex.exe"
    binary.touch()
    provider = CodexProvider(binary=binary)

    escalation = provider.escalation_provider()

    assert provider.model == CODEX_STANDARD_MODEL
    assert escalation.model == CODEX_ESCALATION_MODEL
    assert escalation.binary == binary
    assert provider.model == CODEX_STANDARD_MODEL


def test_readonly_codex_worker_receives_proof_card_pixels(tmp_path: Path, monkeypatch) -> None:
    binary = tmp_path / "codex.exe"
    binary.touch()
    card = tmp_path / "LOOK_001.jpg"
    card.write_bytes(b"proof")
    provider = CodexProvider(binary=binary)
    calls: list[tuple[list[str], dict[str, object]]] = []

    monkeypatch.setattr(provider, "health", lambda: "ready")

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append((command, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout='{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\n',
            stderr="",
        )

    monkeypatch.setattr("lookbookbot.providers.subprocess.run", fake_run)

    assert provider.run_readonly_agent("inspect", tmp_path, images=[card]) == "ok"
    command, kwargs = calls[0]
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert f"--image={card}" in command
    assert "--output-last-message" in command
    assert "--ephemeral" in command
    assert kwargs["creationflags"] == _background_creationflags()


def test_codex_worker_passes_selected_reasoning_effort_to_cli(tmp_path: Path, monkeypatch) -> None:
    binary = tmp_path / "codex.exe"
    binary.touch()
    provider = CodexProvider("gpt-6-luna", binary=binary, reasoning_effort="high")
    commands: list[list[str]] = []
    monkeypatch.setattr(provider, "health", lambda: "ready")

    def fake_run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        commands.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout='{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\n',
            stderr="",
        )

    monkeypatch.setattr("lookbookbot.providers.subprocess.run", fake_run)
    assert provider.run_readonly_agent("inspect", tmp_path) == "ok"
    assert ["--model", "gpt-6-luna"] == commands[0][commands[0].index("--model"):commands[0].index("--model") + 2]
    assert "model_reasoning_effort=high" in commands[0]


def test_closed_board_worker_uses_app_server_image_and_output_schema(tmp_path: Path, monkeypatch) -> None:
    binary = tmp_path / "codex.exe"
    binary.touch()
    card = tmp_path / "LOOK_001.jpg"
    card.write_bytes(b"proof")
    provider = CodexProvider(binary=binary)
    captured_schema: dict[str, object] = {}

    class Client:
        def inspect(self, _prompt, images, **kwargs):
            assert images == [card]
            captured_schema.update(kwargs["schema"])
            return {"text": '{"look_id":"LOOK_001","choice":"W:7","note":"white jacket; black boots"}',
                    "status": "completed"}

    provider._vision_client = Client()

    assert provider.run_readonly_closed_board_vision(
        "inspect", tmp_path, images=[card], look_id="LOOK_001", allowed_labels=["W:7", "M:3"],
    ) == '{"look_id":"LOOK_001","choice":"W:7","note":"white jacket; black boots"}'
    assert captured_schema["properties"]["choice"]["enum"] == ["W:7", "M:3"]
    audit = next((tmp_path / "control/work/ai-exchanges").glob("*.json"))
    assert json.loads(audit.read_text(encoding="utf-8"))["transport"] == "codex-app-server"
