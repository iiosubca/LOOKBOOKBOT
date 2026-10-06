from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'automation-engine/lookbook-layout/scripts'))
import auto_caption_map as resolver


def _fixture(tmp_path):
    hires = tmp_path / '_MAT/hires'
    hires.mkdir(parents=True)
    for name in ('left.jpg', 'right.jpg'):
        (hires / name).write_bytes(name.encode())
    (tmp_path / 'card.jpg').write_bytes(b'card')
    rows = [{'look_id': 'LOOK_001', 'left_filename': 'left.jpg', 'right_filename': 'right.jpg'}]
    cards = [{'excel_sheet': 'W', 'excel_look_number': '1', 'excel_image': 'card.jpg'}]
    return rows, cards, hires


def _result():
    return [0], [{'look_id': 'LOOK_001', 'excel_sheet': 'W', 'excel_look_number': '1'}], ['proposal requires visual check']


def test_numeric_result_reused_but_never_confirms_the_map(tmp_path, monkeypatch):
    args = _fixture(tmp_path)
    calls = []
    monkeypatch.setattr(resolver, '_resolve_uncached', lambda *_args: calls.append(1) or _result())
    first = resolver.resolve(tmp_path, *args)
    assert resolver.resolve(tmp_path, *args) == first
    assert calls == [1]
    assert not (tmp_path / 'control/work/caption-map.tsv').exists()


@pytest.mark.parametrize('change', ['left', 'right', 'card', 'policy', 'registry', 'fallback', 'corrupt', 'duplicate'])
def test_cache_invalidated_by_real_inputs_or_damage(tmp_path, monkeypatch, change):
    rows, cards, hires = _fixture(tmp_path)
    calls = []
    monkeypatch.setattr(resolver, '_resolve_uncached', lambda *_args: calls.append(1) or _result())
    resolver.resolve(tmp_path, rows, cards, hires)
    fallback = None
    if change in {'left', 'right'}:
        (hires / f'{change}.jpg').write_bytes(b'new pixels')
    elif change == 'card':
        (tmp_path / 'card.jpg').write_bytes(b'new card')
    elif change == 'policy':
        resolver.write_json(tmp_path / resolver.SEARCH_POLICY_PATH, {'algorithm': resolver.SEARCH_POLICY})
    elif change == 'registry':
        rows[0]['pdf_spread'] = '2'
    elif change == 'fallback':
        rows[0]['left_filename'] = '__lbb_missing_LOOK_001_LEFT.jpg'
        fallback = tmp_path / 'fallback'
        fallback.mkdir()
        (fallback / 'LOOK_001_LEFT.jpg').write_bytes(b'pdf pixels')
    else:
        path = tmp_path / 'control/work/cache/caption-resolution.json'
        if change == 'corrupt':
            path.write_text('broken json', encoding='utf-8')
        else:
            record = json.loads(path.read_text(encoding='utf-8'))
            record['result'][0] = [0, 0]
            record['result_sha256'] = resolver._resolution_result_hash(record['result'])
            resolver.write_json(path, record)
    resolver.resolve(tmp_path, rows, cards, hires, fallback)
    assert calls == [1, 1]


def test_sources_changing_during_resolution_do_not_get_cached(tmp_path, monkeypatch):
    args = _fixture(tmp_path)
    def compute(*_args):
        (tmp_path / 'card.jpg').write_bytes(b'changed during matching')
        return _result()
    monkeypatch.setattr(resolver, '_resolve_uncached', compute)
    with pytest.raises(SystemExit):
        resolver.resolve(tmp_path, *args)
    assert not (tmp_path / 'control/work/cache/caption-resolution.json').exists()
