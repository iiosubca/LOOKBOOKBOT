from __future__ import annotations

import struct
import zlib
from xml.etree import ElementTree as ET

import pytest

from lookbookbot.excel_images import (
    S, D, A, R, EMU_PER_PIXEL, GUTTER, ImageFitError, cell_bounds,
    column_widths, fit_images_in_drawing, image_dimensions, row_heights,
)


def bitmap(width: int, height: int) -> bytes:
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)) +
            chunk(b'IDAT', zlib.compress((b'\x00' + b'\xff\xff\xff' * width) * height)) + chunk(b'IEND', b''))


def sheet(*, merged: bool = True) -> ET.Element:
    root = ET.Element(S + 'worksheet')
    ET.SubElement(root, S + 'sheetFormatPr', defaultRowHeight='15')
    cols = ET.SubElement(root, S + 'cols')
    ET.SubElement(cols, S + 'col', min='1', max='1', width='24.75')
    data = ET.SubElement(root, S + 'sheetData')
    for index in range(1, 10):
        ET.SubElement(data, S + 'row', r=str(index), ht='31.5')
    if merged:
        merges = ET.SubElement(root, S + 'mergeCells')
        ET.SubElement(merges, S + 'mergeCell', ref='A2:A9')
    return root


def drawing(*, two_cell: bool = False, column: int = 0) -> ET.Element:
    root = ET.Element(D + 'wsDr')
    anchor = ET.SubElement(root, D + ('twoCellAnchor' if two_cell else 'oneCellAnchor'))
    start = ET.SubElement(anchor, D + 'from')
    for tag, value in [('col', column), ('colOff', 0), ('row', 1), ('rowOff', 0)]:
        ET.SubElement(start, D + tag).text = str(value)
    if two_cell:
        end = ET.SubElement(anchor, D + 'to')
        for tag, value in [('col', 0), ('colOff', 100), ('row', 2), ('rowOff', 0)]:
            ET.SubElement(end, D + tag).text = str(value)
    else:
        ET.SubElement(anchor, D + 'ext', cx='257175', cy='400050')
    pic = ET.SubElement(anchor, D + 'pic')
    fill = ET.SubElement(pic, D + 'blipFill')
    ET.SubElement(fill, A + 'blip', {R + 'embed': 'rId1'})
    ET.SubElement(pic, D + 'spPr')
    return root


@pytest.mark.parametrize('size', [(1365, 2048), (853, 1280), (1200, 400), (500, 500), (100, 1000)])
@pytest.mark.parametrize('two_cell', [False, True])
def test_proportional_fit_is_centered_maximal_and_contained(size, two_cell):
    ws, dr = sheet(), drawing(two_cell=two_cell)
    assert fit_images_in_drawing(ws, dr, lambda _: size) == 1
    anchor = dr[0]
    start = anchor.find(D + 'from')
    extent = anchor.find(D + 'ext')
    cx, cy = int(extent.get('cx')), int(extent.get('cy'))
    widths, heights = column_widths(ws, 0), row_heights(ws, 8)
    x = int(start.findtext(D + 'colOff'))
    row = int(start.findtext(D + 'row'))
    y = sum(heights[1:row]) + int(start.findtext(D + 'rowOff'))
    box_width, box_height = widths[0], sum(heights[1:9])
    assert GUTTER <= x and x + cx <= box_width - GUTTER
    assert GUTTER <= y and y + cy <= box_height - GUTTER
    assert abs(2 * x + cx - box_width) <= 1
    assert abs(2 * y + cy - box_height) <= 1
    assert abs(cx / cy - size[0] / size[1]) < 0.00001
    assert min(box_width - 2 * GUTTER - cx, box_height - 2 * GUTTER - cy) <= 1
    assert int(start.findtext(D + 'rowOff')) < heights[row]
    assert anchor.tag == D + 'oneCellAnchor' and anchor.find(D + 'to') is None
    assert anchor.find('.//' + A + 'ext').attrib == extent.attrib
    assert cell_bounds(ws, 0, row) == (0, 1, 0, 8)
    after = ET.tostring(dr)
    fit_images_in_drawing(ws, dr, lambda _: size)
    assert ET.tostring(dr) == after


def test_unmerged_photo_stays_within_one_row():
    ws, dr = sheet(merged=False), drawing()
    fit_images_in_drawing(ws, dr, lambda _: (1365, 2048))
    assert int(dr[0].find(D + 'ext').get('cy')) == 42 * EMU_PER_PIXEL - 2 * GUTTER
    assert dr[0].findtext(D + 'from/' + D + 'row') == '1'


def test_other_column_pictures_are_preserved():
    dr = drawing(column=2)
    before = ET.tostring(dr)
    assert fit_images_in_drawing(sheet(), dr, lambda _: (500, 500)) == 0
    assert ET.tostring(dr) == before


def test_hidden_photo_cell_reports_error():
    ws = sheet()
    ws.find(S + 'cols/' + S + 'col').set('hidden', '1')
    with pytest.raises(ImageFitError, match='скрыта'):
        fit_images_in_drawing(ws, drawing(), lambda _: (500, 500))


def test_native_image_dimensions():
    assert image_dimensions(bitmap(30, 50)) == (30, 50)
    with pytest.raises(ImageFitError):
        image_dimensions(b'not a bitmap')


def test_preparation_resizes_real_embedded_photo(tmp_path):
    from zipfile import ZipFile
    from test_excel_preparation import fixture_book
    from lookbookbot.excel_preparation import prepare_workbook

    source = fixture_book(tmp_path / 'credits.xlsx')
    with ZipFile(source) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    parts['xl/drawings/drawing1.xml'] = ET.tostring(drawing())
    image = bitmap(30, 50)
    parts['xl/media/image1.png'] = image
    parts['xl/drawings/_rels/drawing1.xml.rels'] = b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="../media/image1.png"/></Relationships>'
    for name in ['xl/worksheets/sheet1.xml', 'xl/worksheets/sheet2.xml']:
        root = ET.fromstring(parts[name])
        ET.SubElement(root.find(S + 'mergeCells'), S + 'mergeCell', ref='A2:A3')
        parts[name] = ET.tostring(root)
    with ZipFile(source, 'w') as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    result = prepare_workbook(source)
    assert sum(report.resized_images for report in result.sheets) == 2
    with ZipFile(result.output) as archive:
        assert archive.read('xl/media/image1.png') == image
        extent = ET.fromstring(archive.read('xl/drawings/drawing1.xml'))[0].find(D + 'ext')
        assert int(extent.get('cy')) == 40 * EMU_PER_PIXEL - 2 * GUTTER
