from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import ToolPaths


class CommandError(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandResult:
    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    elapsed: float

    @property
    def text(self) -> str:
        return "\n".join(part for part in (self.stdout.strip(), self.stderr.strip()) if part)


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def evidence_passed(project: Path, gate: str) -> bool:
    return read_json(project / "control" / "evidence" / f"{gate}.json").get("passed") is True


def final_outputs_passed(project: Path) -> bool:
    manifest = read_json(project / "control" / "final-deliverables.json")
    status = str(manifest.get("status", "")).casefold()
    return manifest.get("passed") is True or manifest.get("complete") is True or status in {"pass", "passed", "complete"}


def reference_confirmation_count(project: Path) -> int:
    folder = project / "control" / "reference-order" / "confirmations"
    return sum(1 for path in folder.glob("LOOK_*.json") if path.is_file()) if folder.is_dir() else 0


class CommandRunner:
    def __init__(self, log: Callable[[str], None] | None = None) -> None:
        self.log = log or (lambda _message: None)

    def run(
        self,
        command: list[str | Path],
        *,
        cwd: Path | None = None,
        timeout: int = 1800,
        check: bool = True,
    ) -> CommandResult:
        args = [str(item) for item in command]
        self.log("▶ " + " ".join(_quote(item) for item in args))
        started = time.monotonic()
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            process = subprocess.run(
                args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout, stdin=subprocess.DEVNULL, creationflags=creationflags,
            )
        except subprocess.TimeoutExpired as error:
            raise CommandError(f"Команда превысила лимит {timeout} сек. Её состояние нужно сверить с контроллером.") from error
        result = CommandResult(tuple(args), process.returncode, process.stdout, process.stderr, time.monotonic() - started)
        if result.text:
            self.log(result.text[-8000:])
        if check and process.returncode:
            raise CommandError(result.text or f"Команда завершилась с кодом {process.returncode}")
        return result


class LookbookController:
    def __init__(self, tools: ToolPaths, runner: CommandRunner) -> None:
        self.tools = tools
        self.runner = runner

    def script(self, name: str, *args: str | Path, timeout: int = 1800, check: bool = True) -> CommandResult:
        return self.runner.run([self.tools.python, self.tools.scripts / name, *args], timeout=timeout, check=check)

    def gate(self, action: str, project: Path, *args: str | Path, timeout: int = 1800, check: bool = True) -> CommandResult:
        return self.runner.run([self.tools.python, self.tools.gate, action, project, *args], cwd=project, timeout=timeout, check=check)

    def status(self, project: Path) -> str:
        return self.gate("status", project, timeout=120, check=False).text

    def apply_native_gate(self, project: Path, gate: str, *, max_batches: int = 100) -> None:
        if evidence_passed(project, gate):
            return
        arm = self.gate("arm", project, "--gate", gate, timeout=120, check=False)
        if arm.returncode and not any(word in arm.text.casefold() for word in ("armed", "progress", "nonce", "already")):
            raise CommandError(arm.text)
        for _ in range(max_batches):
            if evidence_passed(project, gate):
                return
            self._wait_for_master(project)
            result = self.gate("apply", project, "--gate", gate, timeout=1800, check=False)
            self._wait_for_master(project)
            if evidence_passed(project, gate):
                return
            if result.returncode:
                raise CommandError(result.text)
            if "CHECKPOINT" not in result.text.upper() and "PASS" not in result.text.upper():
                raise CommandError(f"Контроллер не зафиксировал checkpoint для {gate}: {result.text}")
        raise CommandError(f"Превышено число безопасных пакетов этапа {gate}.")

    @staticmethod
    def _wait_for_master(project: Path, timeout: int = 900) -> None:
        deadline = time.monotonic() + timeout
        while any(project.glob("*.idlk")):
            if time.monotonic() >= deadline:
                raise CommandError("InDesign продолжает удерживать master (.idlk); второй процесс не запущен.")
            time.sleep(2)


def _quote(value: str) -> str:
    return f'"{value}"' if " " in value else value

