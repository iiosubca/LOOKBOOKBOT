"""Measure actual caption transactions only on disposable copies, without AI.

This is a narrow native integration benchmark, NOT a new release/pipeline smoke
test. The source master and every source control file remain byte-identical.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import subprocess
import time
import uuid
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
DRIVER = REPO / 'automation-engine/lookbook-layout/scripts/run_lookbook_gate_com.ps1'


def sha(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            hasher.update(chunk)
    return hasher.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('project', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--looks', type=int, choices=(1, 5, 50), default=50)
    parser.add_argument('--batches', type=int, default=2)
    parser.add_argument('--pristine', action='store_true')
    parser.add_argument('--driver', type=Path, default=DRIVER)
    args = parser.parse_args()
    root = args.project.resolve()
    target = args.output.resolve()
    target.relative_to(REPO / 'tmp')
    if target.exists():
        raise RuntimeError('Use a fresh disposable output folder.')
    # Connecting is diagnostic only; never start a transaction while another
    # native worker or a user document is open. No process is closed or killed.
    probe = r'''$ErrorActionPreference='Stop'; $workers=@(Get-CimInstance Win32_Process | Where-Object {$_.ProcessId -ne $PID -and $_.CommandLine -match '-File\s+[^\r\n]*[\\/]run_lookbook_gate_com.ps1'}); if($workers.Count){throw 'An active native worker exists'}; $processes=@(Get-Process -Name InDesign -ErrorAction SilentlyContinue); if($processes.Count -ne 1){throw 'Exactly one running InDesign instance is required'}; $app=New-Object -ComObject InDesign.Application; if($app.Documents.Count -ne 0){throw 'Close InDesign documents before the disposable benchmark'}; 'SAFE: no native worker or open document' '''
    checked = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', probe],
                             capture_output=True, text=True, errors='replace', timeout=60)
    if checked.returncode:
        raise RuntimeError(checked.stdout + checked.stderr)
    state = json.loads((root / 'control/lookbook-state.json').read_text(encoding='utf-8'))
    source_master = (root.parent / 'SOURCES/TSUM_FS-0260712_LB_LLM_01_AUTOMATION.indd') if args.looks != 50 else root / ('control/checkpoints/captions-before.indd' if args.pristine else state['master'])
    master_hash = sha(source_master)
    files = ['control/lookbook-state.json', 'control/arms/captions.json', 'control/evidence/structure.json',
             state['registry'], state['captions']]
    progress = root / 'control/progress/captions.json'
    if progress.is_file() and not args.pristine:
        files.append('control/progress/captions.json')
    original_hashes = {name: sha(root / name) for name in files}
    for name in files:
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / name, destination)
    shutil.copy2(source_master, target / state['master'])
    # Small cases start with the real four-page automation template, then use
    # the official structure/frames transactions to obtain matching geometry.
    # This remains a caption integration benchmark, not a full-release PASS.
    if args.looks != 50:
        registry_path = target / state['registry']
        with registry_path.open(encoding='utf-8-sig', newline='') as stream:
            reader = csv.DictReader(stream, delimiter='\t')
            fields = reader.fieldnames
            registry = list(reader)[:args.looks]
        with registry_path.open('w', encoding='utf-8', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=fields, delimiter='\t')
            writer.writeheader(); writer.writerows(registry)
        state['look_count'] = args.looks
        state['expected_pages'] = 2 * args.looks + 2
        (target / 'control/lookbook-state.json').write_text(json.dumps(state), encoding='utf-8')
        for gate in ('structure', 'frames'):
            arm = {'schema': 1, 'session_id': state['session_id'], 'gate': gate, 'nonce': uuid.uuid4().hex}
            (target / f'control/arms/{gate}.json').write_text(json.dumps(arm), encoding='utf-8')
            setup = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                                    '-File', str(args.driver.resolve()), '-Action', 'ApplyGate', '-Project', str(target),
                                    '-Gate', gate], capture_output=True, text=True, errors='replace', timeout=900)
            if setup.returncode:
                raise RuntimeError(setup.stdout + setup.stderr)
            print(f'Disposable {args.looks}-look fixture: {gate} passed.', flush=True)
    timings = []
    for number in range(args.batches):
        start = time.monotonic()
        command = ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                   '-File', str(args.driver.resolve()), '-Action', 'ApplyGate', '-Project', str(target),
                   '-Gate', 'captions', '-BatchSize', '4']
        completed = subprocess.run(command, capture_output=True, text=True, errors='replace', timeout=960)
        progress = json.loads((target / 'control/progress/captions.json').read_text(encoding='utf-8'))
        timing = {'batch': number + 1, 'seconds': round(time.monotonic() - start, 3),
                  'completed': progress['completed_count'], 'total': progress['look_count'],
                  'returncode': completed.returncode, 'output': completed.stdout + completed.stderr}
        timings.append(timing)
        print(json.dumps(timing, ensure_ascii=False), flush=True)
        if completed.returncode:
            raise RuntimeError(completed.stdout + completed.stderr)
        if (target / 'control/evidence/captions.json').is_file():
            break
    assert sha(source_master) == master_hash, 'The source master changed during the benchmark.'
    assert all(sha(root / name) == original_hashes[name] for name in files), 'Source control files changed.'
    result = {'scope': 'caption-gate-only; no AI or PDF; not a full pipeline smoke test',
              'source_unchanged': True, 'looks': args.looks, 'timings': timings}
    (target / 'benchmark.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print('PASS isolated native caption benchmark; source files unchanged.', flush=True)


if __name__ == '__main__':
    main()
