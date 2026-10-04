from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


APP_NAME = "LOOKBOOKBOT"
DEFAULT_TEMPLATE = Path(
    r"C:\Users\vdiza\Desktop\TSUM\2026\Lookbook\SOURCES\TSUM_FS-0260712_LB_LLM_01_AUTOMATION.indd"
)
DEFAULT_RUNTIME = Path(
    r"C:\Users\vdiza\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
)


def bundled_engine_scripts() -> Path:
    """Return the internal, versioned LOOKBOOKBOT automation scripts.

    Release jobs deliberately have no fallback to ``~/.codex/skills``.  The
    application ships the exact controller version it was tested with, so an
    unrelated Codex session cannot alter a running production pipeline.
    """
    if getattr(sys, "frozen", False):
        candidate = Path(getattr(sys, "_MEIPASS")) / "automation-engine" / "core" / "scripts"
    else:
        candidate = Path(__file__).resolve().parents[2] / "automation-engine" / "lookbook-layout" / "scripts"
    return candidate


def app_data_dir() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / APP_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def bundled_python() -> Path:
    if DEFAULT_RUNTIME.is_file():
        return DEFAULT_RUNTIME
    located = shutil.which("python.exe") or shutil.which("python")
    if located:
        return Path(located)
    return Path(os.sys.executable)


def codex_binary() -> Path | None:
    explicit = os.environ.get("LOOKBOOKBOT_CODEX")
    if explicit and Path(explicit).is_file():
        return Path(explicit)
    home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    # ``.sandbox-bin`` is an implementation detail of an already running
    # Codex session.  It can remain on disk after the desktop app has upgraded
    # and, unlike the app-server CLI, may no longer support the configured
    # model.  Prefer the current application CLI (or the user's PATH) and use
    # the sandbox copy only as a last-resort compatibility fallback.
    for candidate in (
        home / "plugins" / ".plugin-appserver" / "codex.exe",
    ):
        if candidate.is_file():
            return candidate
    located = shutil.which("codex.exe") or shutil.which("codex")
    if located:
        return Path(located)
    fallback = home / ".sandbox-bin" / "codex.exe"
    return fallback if fallback.is_file() else None


@dataclass(frozen=True)
class ToolPaths:
    python: Path
    engine_scripts: Path
    gate: Path
    template: Path

    @classmethod
    def defaults(cls) -> "ToolPaths":
        scripts = Path(os.environ.get("LOOKBOOKBOT_ENGINE_SCRIPTS", bundled_engine_scripts()))
        template = Path(os.environ.get("LOOKBOOKBOT_TEMPLATE", DEFAULT_TEMPLATE))
        return cls(
            python=bundled_python(),
            engine_scripts=scripts,
            gate=scripts / "lookbook_gate.py",
            template=template,
        )

    def validate(self) -> list[str]:
        missing: list[str] = []
        for label, path in (
            ("Python runtime", self.python),
            ("LOOKBOOKBOT engine", self.engine_scripts / "lookbook_gate.py"),
            ("controller", self.gate),
            ("automation template", self.template),
        ):
            if not path.exists():
                missing.append(f"{label}: {path}")
        return missing
