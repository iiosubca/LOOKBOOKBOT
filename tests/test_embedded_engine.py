from __future__ import annotations

from pathlib import Path

from lookbookbot.config import bundled_engine_scripts


ROOT = Path(__file__).parents[1]


def test_development_engine_is_internal_to_the_application() -> None:
    scripts = bundled_engine_scripts()

    assert scripts == ROOT / "automation-engine" / "lookbook-layout" / "scripts"
    assert scripts.joinpath("lookbook_gate.py").is_file()


def test_windows_build_excludes_codex_skill_metadata() -> None:
    build = (ROOT / "build_windows.bat").read_text(encoding="utf-8")
    spec = (ROOT / "LOOKBOOKBOT.spec").read_text(encoding="utf-8")

    assert "LOOKBOOKBOT.spec" in build
    assert r"automation-engine\\lookbook-layout\\scripts', 'automation-engine\\core\\scripts" in spec
    assert "automation-engine\\lookbook-layout;automation-engine\\lookbook-layout" not in build
