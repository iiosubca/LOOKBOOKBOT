from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


def mapping_module():
    pytest.importorskip('PIL')
    path = Path(__file__).resolve().parents[1] / 'automation-engine/lookbook-layout/scripts/prepare_caption_mapping.py'
    spec = importlib.util.spec_from_file_location('excel_image_mapping_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_centered_photo_uses_owning_merged_look():
    openpyxl = pytest.importorskip('openpyxl')
    module = mapping_module()
    book = openpyxl.Workbook()
    sheet = book.active
    sheet['B2'] = '1'
    sheet.merge_cells('B2:B9')
    sheet['B10'] = '2'
    sheet.merge_cells('B10:B17')
    image = SimpleNamespace(anchor=SimpleNamespace(_from=SimpleNamespace(row=5)))
    assert module.image_look_number(sheet, image) == '1'
    image.anchor._from.row = 14
    assert module.image_look_number(sheet, image) == '2'
    book.close()


def test_photo_spanning_two_look_cells_is_rejected():
    openpyxl = pytest.importorskip('openpyxl')
    module = mapping_module()
    book = openpyxl.Workbook()
    sheet = book.active
    sheet['B2'] = '1'
    sheet.merge_cells('B2:B9')
    sheet['B10'] = '2'
    sheet.merge_cells('B10:B17')
    image = SimpleNamespace(anchor=SimpleNamespace(_from=SimpleNamespace(row=5), to=SimpleNamespace(row=12)))
    with pytest.raises(ValueError, match='cannot be assigned'):
        module.image_look_number(sheet, image)
    book.close()
