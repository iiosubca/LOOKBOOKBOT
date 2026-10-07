from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest

from lookbookbot.discovery import _select_workbook, SourceDiscoveryError
from lookbookbot.excel_preparation import (
    ExcelPreparationError, HEADERS, MANIFEST, NS, Q, parse_price,
    prepare_workbook, selected_prepared_workbook,
)


def fixture_book(path: Path, *, right_header: str = "Корректная цена", right_price: str = "12000",
                 invalid: bool = False, reordered: bool = False) -> Path:
    def sheet() -> bytes:
        root = ET.Element(Q + "worksheet")
        cols = ET.SubElement(root, Q + "cols")
        for index in range(1, 10):
            ET.SubElement(cols, Q + "col", min=str(index), max=str(index), width=str(10 + index))
        data = ET.SubElement(root, Q + "sheetData")
        headers = ["", "№", "Наименование", "Бренд", "Артикул", "КОД   ЦВЕТА", "Цена", "", right_header]
        first = ["", "1", "Пальто", "MVST", "00091843", "BLACK", "99 999,00", "Шерстяное пальто", right_price]
        second = ["", "", "Шарф", "MVST", "7142724", "GREY", "88 888,00", "Шёлковый шарф", "15000"]
        if invalid:
            second[-1] = "уточнить"
        if reordered:
            for values in [headers, first, second]:
                values[2], values[4] = values[4], values[2]
        for number, values in enumerate([headers, first, second], 1):
            row = ET.SubElement(data, Q + "row", r=str(number))
            for column, value in enumerate(values, 1):
                if value:
                    cell = ET.SubElement(row, Q + "c", r=f"{chr(64 + column)}{number}", t="inlineStr", s="0")
                    ET.SubElement(ET.SubElement(cell, Q + "is"), Q + "t").text = value
        merges = ET.SubElement(root, Q + "mergeCells", count="1")
        ET.SubElement(merges, Q + "mergeCell", ref="B2:B3")
        ET.SubElement(root, Q + "drawing", {"{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id": "rId1"})
        return ET.tostring(root)

    styles = f'<styleSheet xmlns="{NS}"><fonts count="1"><font/></fonts><fills count="1"><fill/></fills><borders count="1"><border/></borders><cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellXfs></styleSheet>'
    workbook = f'<workbook xmlns="{NS}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="W" sheetId="1" r:id="rId1"/><sheet name="M " sheetId="2" r:id="rId2"/></sheets></workbook>'
    relationships = '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Target="/xl/worksheets/sheet2.xml"/></Relationships>'
    with ZipFile(path, "w") as archive:
        archive.writestr("xl/styles.xml", styles)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", relationships)
        archive.writestr("xl/worksheets/sheet1.xml", sheet())
        archive.writestr("xl/worksheets/sheet2.xml", sheet())
        archive.writestr("xl/drawings/drawing1.xml", b'<wsDr xmlns="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"/>')
        archive.writestr("xl/drawings/_rels/drawing1.xml.rels", b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>')
        archive.writestr("xl/media/image1.jpeg", b"original image bytes")
        for index in (1, 2):
            archive.writestr(f"xl/worksheets/_rels/sheet{index}.xml.rels", b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="../drawings/drawing1.xml"/></Relationships>')
    return path


@pytest.mark.parametrize(("text", "expected"), [
    ("65 950,00", "65950"), ("65\xa0950 ₽", "65950"), ("65\u202f950,50 руб.", "65950.50"),
    ("1 920 000,00", "1920000"), ("65.950,00", "65950"), ("65,950.00", "65950"),
    ("65950.0", "65950"), ("65,950", "65950"), ("65.950", "65950"), ("0", "0"),
])
def test_price_normalization(text, expected):
    assert parse_price(text) == Decimal(expected)


@pytest.mark.parametrize("text", ["", "уточнить", "100 / 200", "NaN", "-100", "100 USD", "65,95,0", "12.3456"])
def test_ambiguous_prices_are_not_guessed(text):
    with pytest.raises(ExcelPreparationError):
        parse_price(text)


@pytest.mark.parametrize(("header", "column"), [("Корректная цена", "I"), ("Цена", "I"), ("", "I")])
def test_normalization_preserves_images_and_selects_price(tmp_path, header, column):
    source = fixture_book(tmp_path / "credits.xlsx", right_header=header)
    source_bytes = source.read_bytes()
    result = prepare_workbook(source)
    assert source.read_bytes() == source_bytes
    assert all(sheet.price_column == column and sheet.products == 2 for sheet in result.sheets)
    with ZipFile(source) as original, ZipFile(result.output) as output:
        for name in original.namelist():
            if name not in {"xl/styles.xml", "xl/worksheets/sheet1.xml", "xl/worksheets/sheet2.xml"}:
                assert original.read(name) == output.read(name)
        for name in ("xl/worksheets/sheet1.xml", "xl/worksheets/sheet2.xml"):
            root = ET.fromstring(output.read(name))
            cells = {cell.get("r"): cell for cell in root.iter(Q + "c")}
            assert [cells[f"{col}1"].find(Q + "is/" + Q + "t").text for col in "CDEF"] == list(HEADERS)
            assert cells["E2"].find(Q + "v").text == "12000"
            assert cells["E2"].get("t") is None
            assert cells["F2"].find(Q + "is/" + Q + "t").text == "00091843"
            assert root.find(Q + "mergeCells/" + Q + "mergeCell").get("ref") == "B2:B3"
            assert not any(address.startswith(("G", "H", "I")) for address in cells)
        styles = ET.fromstring(output.read("xl/styles.xml"))
        assert '₽' in styles.find(Q + "numFmts/" + Q + "numFmt").get("formatCode")
    assert _select_workbook([source, result.output]) == result.output
    repeated = prepare_workbook(source)
    assert repeated.reused and repeated.output == result.output
    assert prepare_workbook(result.output).output == result.output


def test_correct_price_on_left_beats_right_price(tmp_path):
    source = fixture_book(tmp_path / "credits.xlsx", right_header="Цена")
    with ZipFile(source) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    for name in ["xl/worksheets/sheet1.xml", "xl/worksheets/sheet2.xml"]:
        root = ET.fromstring(parts[name])
        root.find(f".//{Q}c[@r='G1']/{Q}is/{Q}t").text = "Корректная цена"
        parts[name] = ET.tostring(root)
    with ZipFile(source, "w") as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    result = prepare_workbook(source)
    assert all(sheet.price_column == "G" for sheet in result.sheets)


def test_reordered_product_fields(tmp_path):
    result = prepare_workbook(fixture_book(tmp_path / "credits.xlsx", reordered=True))
    with ZipFile(result.output) as archive:
        root = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        assert root.find(f".//{Q}c[@r='C2']/{Q}is/{Q}t").text == "Пальто"
        assert root.find(f".//{Q}c[@r='F2']/{Q}is/{Q}t").text == "00091843"


def test_invalid_price_never_creates_output_or_selection(tmp_path):
    source = fixture_book(tmp_path / "credits.xlsx", invalid=True)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    with pytest.raises(ExcelPreparationError, match="I3"):
        prepare_workbook(source)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert list(tmp_path.glob("*.xlsx")) == [source]
    assert not (tmp_path / MANIFEST).exists()


def test_bad_price_in_unlabelled_right_column_does_not_fall_back(tmp_path):
    source = fixture_book(tmp_path / "credits.xlsx", right_header="", invalid=True)
    with pytest.raises(ExcelPreparationError, match="I3"):
        prepare_workbook(source)
    assert not (tmp_path / MANIFEST).exists()


def test_missing_product_name_is_reported(tmp_path):
    source = fixture_book(tmp_path / "credits.xlsx")
    with ZipFile(source) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    root = ET.fromstring(parts["xl/worksheets/sheet1.xml"])
    root.find(f".//{Q}row[@r='3']").remove(root.find(f".//{Q}c[@r='C3']"))
    parts["xl/worksheets/sheet1.xml"] = ET.tostring(root)
    with ZipFile(source, "w") as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    with pytest.raises(ExcelPreparationError, match="строка 3"):
        prepare_workbook(source)
    assert not (tmp_path / MANIFEST).exists()


def test_existing_outputs_are_not_overwritten(tmp_path):
    source = fixture_book(tmp_path / "credits.xlsx")
    earlier = tmp_path / "credits prepared.xlsx"
    earlier.write_bytes(b"user's earlier file")
    result = prepare_workbook(source)
    assert result.output.name == "credits prepared_02.xlsx"
    assert earlier.read_bytes() == b"user's earlier file"


@pytest.mark.parametrize("which", ["source", "output"])
def test_stale_selection_is_not_used(tmp_path, which):
    source = fixture_book(tmp_path / "credits.xlsx")
    result = prepare_workbook(source)
    target = source if which == "source" else result.output
    with target.open("ab") as stream:
        stream.write(b"changed")
    assert selected_prepared_workbook([source, result.output]) is None
    with pytest.raises(SourceDiscoveryError):
        _select_workbook([source, result.output])


def test_selection_cannot_escape_source_folder(tmp_path):
    source = fixture_book(tmp_path / "credits.xlsx")
    result = prepare_workbook(source)
    metadata = json.loads((tmp_path / MANIFEST).read_text(encoding="utf-8"))
    metadata["output"] = "../outside.xlsx"
    (tmp_path / MANIFEST).write_text(json.dumps(metadata), encoding="utf-8")
    assert selected_prepared_workbook([source, result.output]) is None


def test_preparation_upgrades_older_output_from_original(tmp_path):
    source = fixture_book(tmp_path / "credits.xlsx")
    earlier = prepare_workbook(source)
    earlier_bytes = earlier.output.read_bytes()
    metadata = json.loads((tmp_path / MANIFEST).read_text(encoding="utf-8"))
    metadata.pop("preparation_version")
    for sheet in metadata["sheets"]:
        sheet.pop("resized_images")
    (tmp_path / MANIFEST).write_text(json.dumps(metadata), encoding="utf-8")
    result = prepare_workbook(earlier.output)
    assert result.source == source
    assert result.output.name == "credits prepared_02.xlsx"
    assert not result.reused
    assert earlier.output.read_bytes() == earlier_bytes
    assert prepare_workbook(result.output).reused
