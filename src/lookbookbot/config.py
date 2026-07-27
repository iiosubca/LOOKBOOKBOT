from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


APP_NAME = "LOOKBOOKBOT"
DEFAULT_SKILL_ROOT = Path.home() / ".codex" / "skills" / "lookbook-layout"
DEFAULT_TEMPLATE = Path(
    r"C:\Users\vdiza\Desktop\TSUM\2026\Lookbook\SOURCES\TSUM_FS-0260712_LB_LLM_01_AUTOMATION.indd"
)
DEFAULT_RUNTIME = Path(
    r"C:\Users\vdiza\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
)


def bundled_skill_root() -> Path:
    """Return the versioned automation engine shipped with LOOKBOOKBOT.

    The global Codex skill remains a fallback for developer use, but release
    jobs must not silently change behaviour because another session updated a
    file under ``~/.codex`` after this application was tested.
    """
    if getattr(sys, "frozen", False):
        candidate = Path(getattr(sys, "_MEIPASS")) / "automation-engine" / "lookbook-layout"
    else:
        candidate = Path(__file__).resolve().parents[2] / "automation-engine" / "lookbook-layout"
    return candidate if candidate.is_dir() else DEFAULT_SKILL_ROOT


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
    for candidate in (
        home / ".sandbox-bin" / "codex.exe",
        home / "plugins" / ".plugin-appserver" / "codex.exe",
    ):
        if candidate.is_file():
            return candidate
    located = shutil.which("codex.exe") or shutil.which("codex")
    return Path(located) if located else None


@dataclass(frozen=True)
class ToolPaths:
    python: Path
    skill_root: Path
    scripts: Path
    gate: Path
    template: Path

    @classmethod
    def defaults(cls) -> "ToolPaths":
        root = Path(os.environ.get("LOOKBOOKBOT_SKILL_ROOT", bundled_skill_root()))
        template = Path(os.environ.get("LOOKBOOKBOT_TEMPLATE", DEFAULT_TEMPLATE))
        return cls(
            python=bundled_python(),
            skill_root=root,
            scripts=root / "scripts",
            gate=root / "scripts" / "lookbook_gate.py",
            template=template,
        )

    def validate(self) -> list[str]:
        missing: list[str] = []
        for label, path in (
            ("Python runtime", self.python),
            ("lookbook skill", self.skill_root / "SKILL.md"),
            ("controller", self.gate),
            ("automation template", self.template),
        ):
            if not path.exists():
                missing.append(f"{label}: {path}")
        return missing
