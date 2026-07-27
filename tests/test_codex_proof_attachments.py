from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from lookbookbot.providers import CodexProvider, _background_creationflags


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
    assert kwargs["creationflags"] == _background_creationflags()
