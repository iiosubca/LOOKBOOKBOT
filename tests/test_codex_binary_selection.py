from __future__ import annotations

from pathlib import Path

from lookbookbot import config


def test_codex_binary_prefers_current_app_cli_over_stale_sandbox_copy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / ".codex"
    stale = home / ".sandbox-bin" / "codex.exe"
    current = home / "plugins" / ".plugin-appserver" / "codex.exe"
    stale.parent.mkdir(parents=True)
    current.parent.mkdir(parents=True)
    stale.touch()
    current.touch()
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.delenv("LOOKBOOKBOT_CODEX", raising=False)
    monkeypatch.setattr(config.shutil, "which", lambda _name: None)

    assert config.codex_binary() == current


def test_codex_binary_uses_sandbox_copy_only_when_no_current_cli_exists(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / ".codex"
    fallback = home / ".sandbox-bin" / "codex.exe"
    fallback.parent.mkdir(parents=True)
    fallback.touch()
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.delenv("LOOKBOOKBOT_CODEX", raising=False)
    monkeypatch.setattr(config.shutil, "which", lambda _name: None)

    assert config.codex_binary() == fallback
