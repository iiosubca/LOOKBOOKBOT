from __future__ import annotations

import base64
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


def _gate_checkpoint_signature(project: Path, gate: str) -> str | None:
    """Return meaningful native progress for a resumable gate.

    A fresh timestamp is not a reason to run another InDesign transaction. The
    checkpoint must name a different completed LOOK set or count.
    """
    payload = read_json(project / "control" / "progress" / f"{gate}.json")
    if not payload:
        return None
    completed = payload.get("completed_looks")
    if not isinstance(completed, list):
        completed = []
    return json.dumps(
        {
            "gate": payload.get("gate"),
            "nonce": payload.get("nonce"),
            "completed_count": payload.get("completed_count"),
            "completed_looks": sorted(str(value) for value in completed),
            "look_count": payload.get("look_count"),
            "master": payload.get("master"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _artifact_signature(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_size, stat.st_mtime_ns


def _controlled_master(project: Path) -> Path:
    """Return the saved master only when state keeps it inside the project."""
    state = read_json(project / "control" / "lookbook-state.json")
    name = str(state.get("master", "")).strip()
    if not name:
        raise CommandError("Контроллер не указал master-файл для безопасного восстановления InDesign.")
    root = project.resolve()
    master = (root / name).resolve()
    try:
        master.relative_to(root)
    except ValueError as error:
        raise CommandError("Master-файл контроллера находится вне папки проекта.") from error
    if not master.is_file():
        raise CommandError("Сохранённый master-файл не найден; автоматическое восстановление остановлено.")
    return master


def _assert_unchanged_master(master: Path, expected: tuple[int, int] | None, context: str) -> None:
    """Never retry a read-only stage after its master changed on disk."""
    actual = _artifact_signature(master)
    if expected is None or actual != expected:
        raise CommandError(
            f"Master-файл изменился во время {context}; автоматический повтор остановлен, чтобы не перезаписать выпуск."
        )


def _stable_failure_text(value: str) -> str:
    return " ".join(str(value).split())


def final_outputs_passed(project: Path) -> bool:
    manifest = read_json(project / "control" / "final-deliverables.json")
    status = str(manifest.get("status", "")).casefold()
    complete = manifest.get("passed") is True or manifest.get("complete") is True or status in {"pass", "passed", "complete"}
    outputs = manifest.get("outputs")
    # Do not treat the legacy Interactive-PDF outputs as final: their PPI was
    # not a placed-image downsampling setting. A resumed final stage archives
    # those files safely and regenerates them with Adobe PDF (Print), JPEG Medium.
    return bool(
        complete
        and isinstance(outputs, list)
        and len(outputs) == 5
        and all(isinstance(item, dict) and item.get("export_format") == "adobe-pdf-print-jpeg-medium-v1" for item in outputs)
    )


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
        return self.runner.run([self.tools.python, self.tools.engine_scripts / name, *args], timeout=timeout, check=check)

    def gate(self, action: str, project: Path, *args: str | Path, timeout: int = 1800, check: bool = True) -> CommandResult:
        return self.runner.run([self.tools.python, self.tools.gate, action, project, *args], cwd=project, timeout=timeout, check=check)

    def status(self, project: Path) -> str:
        return self.gate("status", project, timeout=120, check=False).text

    def export_review_pdf(self, project: Path, pdf: str) -> None:
        """Export a review PDF while every COM retry proves master stability.

        A release/export COM dropout leaves the saved master untouched.  A
        fresh permit also quarantines a partial PDF, so retrying from this
        boundary is both idempotent and safer than asking the operator to
        press Continue.
        """
        master = _controlled_master(project)
        master_before_export = _artifact_signature(master)
        while True:
            self._wait_for_master(project)
            permit = self.gate("pre-export", project, "--pdf", pdf, "--quarantine-existing", timeout=180, check=False)
            if permit.returncode:
                raise CommandError(permit.text)
            exported = self.gate("export-pdf", project, "--pdf", pdf, timeout=1800, check=False)
            if not exported.returncode:
                self._wait_for_master(project)
                return
            if self._is_com_disconnect(project, exported.text):
                _assert_unchanged_master(master, master_before_export, "экспорта PDF")
                if self._restart_controlled_indesign(project):
                    self.runner.log(
                        "InDesign потерял COM-соединение при экспорте review-PDF; "
                        "безопасно перезапускаю только управляемый master и повторяю экспорт."
                    )
                    self._wait_for_indesign_ready()
                    continue
            raise CommandError(exported.text)

    def apply_native_gate(self, project: Path, gate: str) -> None:
        if evidence_passed(project, gate):
            return
        arm = self.gate("arm", project, "--gate", gate, timeout=120, check=False)
        if arm.returncode and not any(word in arm.text.casefold() for word in ("armed", "progress", "nonce", "already")):
            raise CommandError(arm.text)
        seen_checkpoints: set[str] = set()
        seen_com_failures: set[tuple[str | None, str]] = set()
        release_master = _controlled_master(project) if gate == "release" else None
        release_master_signature = _artifact_signature(release_master) if release_master else None
        while True:
            if evidence_passed(project, gate):
                return
            checkpoint_before = _gate_checkpoint_signature(project, gate)
            self._wait_for_master(project)
            result = self.gate("apply", project, "--gate", gate, timeout=1800, check=False)
            self._wait_for_master(project)
            if evidence_passed(project, gate):
                return
            if result.returncode:
                if self._is_com_disconnect(project, result.text):
                    if gate == "release":
                        # release is intentionally read-only.  A verified
                        # unchanged master plus a neutral InDesign restart is
                        # a new safe recovery state, even when Adobe repeats
                        # the same RPC diagnostic.  Do not replace this with
                        # an arbitrary retry count: that was the source of
                        # false stops on otherwise valid lookbooks.
                        _assert_unchanged_master(release_master, release_master_signature, "контрольного выпуска")
                        self.runner.log(
                            "InDesign потерял COM-соединение на release; master не изменён. "
                            "Безопасно перезапускаю document-free экземпляр и продолжаю тот же этап."
                        )
                        if not self._restart_controlled_indesign(project):
                            raise CommandError(
                                "InDesign потерял COM-соединение на этапе release, но приложение не смогло "
                                "доказать, что экземпляр безопасно перезапустить. Документ не изменён."
                            )
                        self._wait_for_indesign_ready()
                        continue
                    failure_state = (checkpoint_before, _stable_failure_text(result.text))
                    if failure_state in seen_com_failures:
                        raise CommandError(
                            f"InDesign повторил тот же COM-сбой на этапе {gate} без нового checkpoint. "
                            "Документ не изменён; повтор не даст нового безопасного действия."
                        )
                    seen_com_failures.add(failure_state)
                    self.runner.log(
                        f"InDesign потерял COM-соединение на этапе {gate}; повторяю тот же arm "
                        "из сохранённой точки."
                    )
                    # Other native gates may have durable checkpoints that
                    # the underlying controller can resume itself.  Do not
                    # close a possible in-progress transactional document.
                    time.sleep(4)
                    # Return to the guarded loop.  It preserves the existing
                    # arm and rechecks both lock and evidence before retrying.
                    continue
                else:
                    raise CommandError(result.text)
            if "CHECKPOINT" not in result.text.upper() and "PASS" not in result.text.upper():
                raise CommandError(f"Контроллер не зафиксировал checkpoint для {gate}: {result.text}")
            checkpoint_after = _gate_checkpoint_signature(project, gate)
            if checkpoint_after is None:
                raise CommandError(
                    f"Контроллер сообщил checkpoint для {gate}, но не сохранил его состояние."
                )
            if checkpoint_after == checkpoint_before or checkpoint_after in seen_checkpoints:
                raise CommandError(
                    f"Этап {gate} повторил checkpoint без нового сохранённого прогресса."
                )
            seen_checkpoints.add(checkpoint_after)

    @staticmethod
    def _wait_for_master(project: Path, timeout: int = 900) -> None:
        deadline = time.monotonic() + timeout
        while any(project.glob("*.idlk")):
            if time.monotonic() >= deadline:
                raise CommandError("InDesign продолжает удерживать master (.idlk); второй процесс не запущен.")
            time.sleep(2)

    @staticmethod
    def _wait_for_indesign_ready(timeout: int = 60) -> None:
        """Wait until one restarted, document-free InDesign instance is ready.

        The neutral title is a conservative proof that there is no document.
        It also avoids creating a new COM client while the broken client is
        still being torn down.
        """
        script = r'''
$deadline = (Get-Date).AddSeconds(__TIMEOUT__)
do {
    $instances = @(Get-Process -Name InDesign -ErrorAction SilentlyContinue)
    if ($instances.Count -eq 1 -and [string]$instances[0].MainWindowTitle -match '^Adobe InDesign(?: 2026)?$') {
        exit 0
    }
    Start-Sleep -Seconds 1
} while ((Get-Date) -lt $deadline)
exit 1
'''.replace("__TIMEOUT__", str(int(timeout)))
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            completed = subprocess.run(
                _powershell_encoded_command(script),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
                timeout=timeout + 5,
            )
        except subprocess.TimeoutExpired as error:
            raise CommandError("InDesign не подтвердил готовность после безопасного перезапуска.") from error
        if completed.returncode:
            raise CommandError("InDesign не подтвердил безопасное состояние после перезапуска.")

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
        """Restart only a disconnected, automation-owned InDesign session.

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
        master_literal = str(master).replace("'", "''")
        script = r'''
$Master = '__MASTER_PATH__'
$ErrorActionPreference = 'Stop'
$SAVE_NO = 1852776480

function Get-InDesignInstances {
    return @(Get-Process -Name InDesign -ErrorAction SilentlyContinue)
}

function Test-NeutralStartWindow([System.Diagnostics.Process]$Instance) {
    return [string]$Instance.MainWindowTitle -match '^Adobe InDesign(?: 2026)?$'
}

function Get-KnownInDesignExecutable([System.Diagnostics.Process]$Instance) {
    if ($Instance -and $Instance.Path -and (Test-Path -LiteralPath $Instance.Path -PathType Leaf)) {
        return [string]$Instance.Path
    }
    $candidate = Join-Path $env:ProgramFiles 'Adobe\Adobe InDesign 2026\InDesign.exe'
    if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    return $null
}

function Remove-ControlledLocks {
    $project = Split-Path -Parent $Master
    Get-ChildItem -LiteralPath $project -Filter '*.idlk' -Force -ErrorAction SilentlyContinue |
        Remove-Item -Force -Recurse -ErrorAction Stop
}

function Wait-NeutralStartWindow {
    for ($second = 0; $second -lt 45; $second++) {
        $current = @(Get-InDesignInstances)
        if ($current.Count -eq 1 -and (Test-NeutralStartWindow $current[0])) { return $true }
        Start-Sleep -Seconds 1
    }
    return $false
}

function Start-NeutralInstance([string]$Executable) {
    # InDesign needs a normal GUI window even when no document is open.  A
    # hidden start produces an empty window title, which is indistinguishable
    # from an unsafe unknown document after a later COM dropout.
    Start-Process -FilePath $Executable -PassThru | Out-Null
    return Wait-NeutralStartWindow
}

function Restart-NeutralInstance {
    $instances = @(Get-InDesignInstances)
    if ($instances.Count -gt 1) { exit 8 }
    if ($instances.Count -eq 1 -and -not (Test-NeutralStartWindow $instances[0])) { exit 9 }
    $existing = if ($instances.Count -eq 1) { $instances[0] } else { $null }
    $exe = Get-KnownInDesignExecutable $existing
    if (-not $exe) { exit 10 }
    Remove-ControlledLocks
    if ($existing) {
        Stop-Process -Id $existing.Id -Force
        Wait-Process -Id $existing.Id -Timeout 20 -ErrorAction SilentlyContinue
    }
    if (Start-NeutralInstance $exe) { exit 0 }

    # A cold InDesign launch occasionally terminates before creating its COM
    # server.  With no process left there is still provably no open document,
    # so one more launch is safe and avoids handing a transient startup race
    # back to the operator.  Never retry when any ambiguous process remains.
    $afterFirstLaunch = @(Get-InDesignInstances)
    if ($afterFirstLaunch.Count -ne 0) { exit 11 }
    Start-Sleep -Seconds 2
    if (-not (Start-NeutralInstance $exe)) { exit 12 }
    exit 0
}

try {
    # A neutral title proves the absence of a document.  This is the normal
    # RPC_E_DISCONNECTED path, so avoid a fragile COM enumeration first.
    $instances = @(Get-InDesignInstances)
    if ($instances.Count -eq 0 -or ($instances.Count -eq 1 -and (Test-NeutralStartWindow $instances[0]))) {
        Restart-NeutralInstance
    }

    # A non-neutral instance might contain user work.  Attach only here and
    # proceed exclusively when it contains this exact saved master.
    if ($instances.Count -ne 1) { exit 12 }
    $app = New-Object -ComObject InDesign.Application
    $wanted = [System.IO.Path]::GetFullPath($Master).ToLowerInvariant()
    $matches = @()
    foreach ($document in @($app.Documents)) {
        $full = [System.IO.Path]::GetFullPath([string]$document.FullName).ToLowerInvariant()
        if ($full -eq $wanted) { $matches += $document } else { exit 13 }
    }
    if ($matches.Count -gt 1) { exit 14 }
    foreach ($document in $matches) { $document.Close($SAVE_NO) }
    if ($app.Documents.Count -ne 0) { exit 15 }
    Restart-NeutralInstance
} catch { exit 16 }
'''.replace("__MASTER_PATH__", master_literal)
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            completed = subprocess.run(
                _powershell_encoded_command(script),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            return False
        return completed.returncode == 0


def _quote(value: str) -> str:
    return f'"{value}"' if " " in value else value


def _is_com_disconnect(text: str) -> bool:
    normalized = text.casefold()
    return "rpc_e_disconnected" in normalized or "0x80010108" in normalized


def _powershell_encoded_command(script: str) -> list[str]:
    """Return a Windows-safe, argument-free PowerShell invocation.

    PowerShell can silently discard named parameters supplied after a long
    ``-Command`` string.  In COM recovery that turns a valid master path into
    an empty string and incorrectly triggers the safety refusal.  Encoding
    the complete script removes that parser boundary entirely.
    """
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded]
