"""Compare cold/warm numeric proposals on a copy; never call AI or InDesign."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'automation-engine/lookbook-layout/scripts'))
import auto_caption_map as mapping


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('project', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    source, target = args.project.resolve(), args.output.resolve()
    target.relative_to(REPO / 'tmp')
    if target.exists():
        raise RuntimeError('Use a fresh output folder.')
    names = ['control/work/look-register.tsv', 'control/work/_mat/excel-images/index.tsv']
    if (source / mapping.SEARCH_POLICY_PATH).is_file():
        names.append(mapping.SEARCH_POLICY_PATH)
    for name in names:
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / name, destination)
    registry = mapping.read_rows(target / names[0], mapping.REGISTRY_FIELDS)
    cards = mapping.read_rows(target / names[1], mapping.INDEX_FIELDS)
    image_paths = [Path(card['excel_image']) for card in cards]
    for row in registry:
        for side in ('left', 'right'):
            filename = row[f'{side}_filename']
            if filename.casefold().startswith(mapping.MISSING_PREFIX):
                image_paths.append(Path(f'control/work/missing-photo-reference/{row["look_id"]}_{side.upper()}.jpg'))
            else:
                image_paths.append(Path('_MAT/hires') / filename)
    for name in set(image_paths):
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.link(source / name, destination) # Images are read-only inputs, never edited by this benchmark.
    results, times = [], []
    for mode in ('cold', 'warm'):
        start = time.monotonic()
        result = mapping.resolve(target, registry, cards, target / '_MAT/hires', target / 'control/work/missing-photo-reference')
        results.append(result)
        times.append(round(time.monotonic() - start, 3))
        print(f'{mode}: {times[-1]} seconds', flush=True)
    assert results[0] == results[1], 'Warm proposal must be identical to the cold calculation.'
    report = {'looks': len(registry), 'cold_seconds': times[0], 'warm_seconds': times[1],
              'identical_numeric_proposals': True, 'AI_calls': 0, 'INDD_changes': 0}
    (target / 'benchmark.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report), flush=True)


if __name__ == '__main__':
    main()
