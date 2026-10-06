from __future__ import annotations

import csv
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPTS = Path(__file__).parents[1] / 'automation-engine/lookbook-layout/scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('performance_gate', SCRIPTS / 'lookbook_gate.py')
assert spec and spec.loader
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def _reference(tmp_path: Path):
    work = tmp_path / 'control/work'
    work.mkdir(parents=True)
    hires = tmp_path / '_MAT/hires'
    hires.mkdir(parents=True)
    (hires / 'left.jpg').write_bytes(b'left' * 1024)
    (hires / 'right.jpg').write_bytes(b'right' * 1024)
    (work / 'reference.pdf').write_bytes(b'reference')
    rows = [{'look_id': 'LOOK_001', 'spread_order': '1', 'pdf_spread': '1',
             'left_filename': 'left.jpg', 'right_filename': 'right.jpg',
             'indd_left_page': '2', 'indd_right_page': '3'}]
    with (work / 'look-register.tsv').open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=gate.REGISTRY_FIELDS, delimiter='\t')
        writer.writeheader()
        writer.writerows(rows)
    state = {'session_id': 'test', 'look_count': 1, 'hires': '_MAT/hires',
             'registry': 'control/work/look-register.tsv', 'reference_pdf': 'control/work/reference.pdf',
             'reference_cover_pages': 0}
    root = gate.reference_order_root(tmp_path)
    root.mkdir()
    for name in ('reference.jpg', 'evidence.jpg'):
        (root / name).write_bytes(name.encode() * 1024)
    item = {**{key: rows[0][key] for key in ('look_id', 'left_filename', 'right_filename')},
            'reference_page': 1, 'left_sha256': gate.digest(hires / 'left.jpg'),
            'right_sha256': gate.digest(hires / 'right.jpg')}
    for key, name, hash_key in [('reference_page_image', 'reference.jpg', 'reference_page_sha256'),
                                ('evidence_image', 'evidence.jpg', 'evidence_sha256')]:
        item[key] = str((root / name).relative_to(tmp_path))
        item[hash_key] = gate.digest(root / name)
    manifest = {'schema': gate.REFERENCE_ORDER_SCHEMA, 'generator': 'lookbook_gate.py:prepare-reference-order',
                'session_id': 'test', 'reference_pdf_sha256': gate.digest(work / 'reference.pdf'),
                'registry_sha256': gate.digest(work / 'look-register.tsv'), 'reference_page_count': 1,
                'reference_cover_pages': 0, 'looks': {'LOOK_001': item}}
    gate.write_json(gate.reference_order_manifest_path(tmp_path), manifest)
    gate.write_json(gate.reference_order_confirmation_path(tmp_path, 'LOOK_001'), {
        'schema': gate.REFERENCE_ORDER_SCHEMA, 'session_id': 'test', 'look_id': 'LOOK_001',
        'reference_order_manifest_sha256': gate.digest(gate.reference_order_manifest_path(tmp_path)),
        'item_fingerprint': gate.reference_order_item_fingerprint(item),
        'note': 'Exact reference pair visually checked: full-length left and close-up right.'})
    return state


def test_reference_snapshot_reads_the_photos_once_and_rechecks_next_call(tmp_path, monkeypatch):
    state = _reference(tmp_path)
    original = gate.digest
    reads = []
    monkeypatch.setattr(gate, 'digest', lambda path: reads.append(Path(path)) or original(path))
    gate.validate_reference_order(tmp_path, state)
    photo = tmp_path / '_MAT/hires/left.jpg'
    assert reads.count(photo) == 1
    photo.write_bytes(b'changed' * 1024)
    with pytest.raises(gate.GateError, match='differs'):
        gate.validate_reference_order(tmp_path, state)


def test_confirmation_corruption_is_still_rejected(tmp_path):
    state = _reference(tmp_path)
    path = gate.reference_order_confirmation_path(tmp_path, 'LOOK_001')
    record = json.loads(path.read_text(encoding='utf-8'))
    record['item_fingerprint'] = 'wrong'
    gate.write_json(path, record)
    with pytest.raises(gate.GateError, match='stale'):
        gate.validate_reference_order(tmp_path, state)


def test_status_uses_one_validated_gate_chain(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(gate, 'load_state', lambda _root: {'session_id': 'test', 'look_count': 50})
    calls = []
    monkeypatch.setattr(gate, 'passed_gates', lambda *_args: calls.append(1) or list(gate.GATES[:5]))
    monkeypatch.setattr(gate, 'current_gate', lambda *_args: pytest.fail('duplicate full validation'))
    gate.command_status(SimpleNamespace(project=str(tmp_path)))
    assert calls == [1]
    assert 'NEXT captions' in capsys.readouterr().out


def test_caption_batches_allow_the_final_audit_and_keep_four_look_transactions(tmp_path, monkeypatch):
    state = {'session_id': 'test', 'look_count': 50}
    monkeypatch.setattr(gate, 'load_state', lambda _root: state)
    monkeypatch.setattr(gate, 'current_gate', lambda *_args: 'captions')
    gate.write_json(gate.arm_file(tmp_path, 'captions'), {'nonce': 'n'})
    monkeypatch.setattr(gate, 'load_evidence', lambda *_args: None)
    monkeypatch.setattr(gate, 'caption_progress', lambda *_args: {'completed_count': 4 if called else 0})
    called = []
    def driver(arguments, timeout_seconds):
        assert arguments[-2:] == ['-BatchSize', '4']
        assert timeout_seconds == 900
        called.append(arguments)
    monkeypatch.setattr(gate, 'run_com_driver', driver)
    gate.command_apply(SimpleNamespace(project=str(tmp_path), gate='captions'))
    assert len(called) == 1


@pytest.mark.skipif(os.name != 'nt', reason='Windows PowerShell contract test')
def test_native_receipts_and_typography_contracts(tmp_path):
    harness = Path(__file__).with_name('native_caption_performance_contract.ps1')
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-File', str(harness),
                             '-Driver', str(SCRIPTS / 'run_lookbook_gate_com.ps1'), '-Root', str(tmp_path)],
                            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'PASS native caption receipt' in result.stdout
