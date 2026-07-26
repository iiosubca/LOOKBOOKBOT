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

    def export_review_pdf(self, project: Path, pdf: str) -> None:
        """Export a review PDF with one safe retry for a lost InDesign COM client.

        A release/export COM dropout leaves the saved master untouched.  A
        fresh permit also quarantines a partial PDF, so retrying from this
        boundary is both idempotent and safer than asking the operator to
        press Continue.
        """
        for attempt in range(2):
            self._wait_for_master(project)
            permit = self.gate("pre-export", project, "--pdf", pdf, "--quarantine-existing", timeout=180, check=False)
            if permit.returncode:
                raise CommandError(permit.text)
            exported = self.gate("export-pdf", project, "--pdf", pdf, timeout=1800, check=False)
            if not exported.returncode:
                self._wait_for_master(project)
                return
            if attempt == 0 and self._is_com_disconnect(project, exported.text):
                if self._restart_controlled_indesign(project):
                    self.runner.log(
                        "InDesign потерял COM-соединение при экспорте review-PDF; "
                        "безопасно перезапускаю только управляемый master и повторяю экспорт."
                    )
                    time.sleep(4)
                    continue
            raise CommandError(exported.text)
        raise CommandError("Не удалось безопасно восстановить экспорт review-PDF.")

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
                if self._is_com_disconnect(project, result.text):
                    # The native controller already tries a safe cold restart
                    # for an idle InDesign instance. This remains one bounded
                    # fallback attempt; never re-arm, skip, or run concurrently.
                    self.runner.log(
                        f"InDesign потерял COM-соединение на этапе {gate}; "
                        "выполняется одна безопасная повторная попытка из сохранённой точки."
                    )
                    # The native driver already makes one idle-server retry.
                    # For the read-only release audit it can still leave the
                    # exact controlled master open after the COM client dies.
                    # Close only that master, reject any other document, and
                    # restart one provably automation-owned InDesign instance.
                    if gate == "release":
                        self._restart_controlled_indesign(project)
                    # Give a just-restarted InDesign server time to register
                    # its COM endpoint before the fallback command attaches.
                    time.sleep(4)
                    self._wait_for_master(project)
                    retry = self.gate("apply", project, "--gate", gate, timeout=1800, check=False)
                    self._wait_for_master(project)
                    if evidence_passed(project, gate):
                        return
                    if retry.returncode:
                        raise CommandError(
                            f"InDesign снова потерял связь на этапе {gate}. "
                            "Документ не был пропущен или перезаписан; повторите этот же этап позднее."
                        )
                    result = retry
                else:
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

    @staticmethod
    def _is_com_disconnect(project: Path, message: str) -> bool:
        if _is_com_disconnect(message):
            return True
        error_path = project / "control" / "progress" / "native-last-error.txt"
        try:
            return _is_com_disconnect(error_path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            return False

    @staticmethod
    def _restart_controlled_indesign(project: Path) -> bool:
        """Restart only a disconnected, automation-owned release/export session.

        This deliberately refuses to touch InDesign when another document is
        open.  The exact controlled master is read-only at release/export, so
        closing it without saving after an RPC disconnect cannot discard a
        layout edit.
        """
        state = read_json(project / "control" / "lookbook-state.json")
        master_name = str(state.get("master", "")).strip()
        if not master_name:
            return False
        project_root = project.resolve()
        master = (project_root / master_name).resolve()
        try:
            master.relative_to(project_root)
        except ValueError:
            return False
        if not master.is_file():
            return False
        script = r'''
param([string]$Master)
$ErrorActionPreference = 'Stop'
$SAVE_NO = 1852776480
try {
    $app = New-Object -ComObject InDesign.Application
    $wanted = [System.IO.Path]::GetFullPath($Master).ToLowerInvariant()
    $matches = @()
    foreach ($document in @($app.Documents)) {
        $full = [System.IO.Path]::GetFullPath([string]$document.FullName).ToLowerInvariant()
        if ($full -eq $wanted) { $matches += $document }
        else { exit 4 }
    }
    if ($matches.Count -gt 1) { exit 5 }
    foreach ($document in $matches) { $document.Close($SAVE_NO) }
    if ($app.Documents.Count -ne 0) { exit 6 }
    # With no document left in the sole automation session, a project-local
    # lock can only be stale.  Remove only this controlled master's lock.
    $project = Split-Path -Parent $Master
    Get-ChildItem -LiteralPath $project -Filter '*.idlk' -Force -ErrorAction SilentlyContinue |
        Remove-Item -Force -Recurse -ErrorAction Stop
    $instances = @(Get-Process -Name InDesign -ErrorAction SilentlyContinue)
    if ($instances.Count -eq 0) {
        $candidate = Join-Path $env:ProgramFiles 'Adobe\Adobe InDesign 2026\InDesign.exe'
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { exit 7 }
        Start-Process -FilePath $candidate -WindowStyle Hidden
        exit 0
    }
    if ($instances.Count -ne 1 -or -not $instances[0].Path) { exit 8 }
    $exe = $instances[0].Path
    Stop-Process -Id $instances[0].Id -Force
    Start-Sleep -Seconds 2
    Start-Process -FilePath $exe -WindowStyle Hidden
    exit 0
} catch { exit 9 }
'''
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            completed = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script, "-Master", str(master)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            return False
        return completed.returncode == 0


def _quote(value: str) -> str:
    return f'"{value}"' if " " in value else value


def _is_com_disconnect(text: str) -> bool:
    normalized = text.casefold()
    return "rpc_e_disconnected" in normalized or "0x80010108" in normalized
