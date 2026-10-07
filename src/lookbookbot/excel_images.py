"""Fit existing catalogue pictures to their cells without changing image bytes."""
from __future__ import annotations

import math
from collections.abc import Callable
from xml.etree import ElementTree as ET


S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
D = "{http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
EMU_PER_PIXEL = 9525
EMU_PER_POINT = 12700
GUTTER = 2 * EMU_PER_PIXEL


class ImageFitError(ValueError):
    pass


def image_dimensions(content: bytes) -> tuple[int, int]:
    # Qt already ships with the desktop app. Read the native bitmap headers;
    # do not decode/resample/save the source image or add another dependency.
    from PySide6.QtCore import QByteArray, QBuffer, QIODevice
    from PySide6.QtGui import QImageReader

    buffer = QBuffer()
    buffer.setData(QByteArray(content))
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buffer)
    size = reader.size()
    if size.width() <= 0 or size.height() <= 0:
        raise ImageFitError("Не удалось прочитать размеры встроенной фотографии.")
    return size.width(), size.height()


def _address(value: str) -> tuple[int, int]:
    letters = "".join(char for char in value if char.isalpha())
    column = 0
    for char in letters:
        column = column * 26 + ord(char) - 64
    return column - 1, int(value[len(letters):]) - 1


def cell_bounds(sheet: ET.Element, column: int, row: int) -> tuple[int, int, int, int]:
    """Return the zero-based inclusive rectangle of the cell/merged cell."""
    for merge in sheet.findall(S + "mergeCells/" + S + "mergeCell"):
        first, last = merge.get("ref", "").split(":")
        left, top = _address(first)
        right, bottom = _address(last)
        if left <= column <= right and top <= row <= bottom:
            return left, top, right, bottom
    return column, row, column, row


def row_heights(sheet: ET.Element, last: int) -> list[int]:
    settings = sheet.find(S + "sheetFormatPr")
    default = float(settings.get("defaultRowHeight", "15")) if settings is not None else 15.0
    hidden_default = settings is not None and settings.get("zeroHeight") == "1"
    heights = [0 if hidden_default else round(default * EMU_PER_POINT)] * (last + 1)
    for row in sheet.findall(S + "sheetData/" + S + "row"):
        index = int(row.get("r", "1")) - 1
        if index <= last:
            heights[index] = 0 if row.get("hidden") == "1" else round(float(row.get("ht", str(default))) * EMU_PER_POINT)
    return heights


def column_widths(sheet: ET.Element, last: int) -> list[int]:
    settings = sheet.find(S + "sheetFormatPr")
    default = float(settings.get("defaultColWidth", "8.43")) if settings is not None else 8.43

    def width(characters: float) -> int:
        # Excel column widths include cell padding and are expressed in the
        # normal font's digit units. Standard catalogue fonts use a 7-px digit.
        pixels = math.floor(characters * 7 + 0.5) if characters >= 1 else math.floor(characters * 12 + 0.5)
        return pixels * EMU_PER_PIXEL

    widths = [width(default)] * (last + 1)
    for col in sheet.findall(S + "cols/" + S + "col"):
        for index in range(int(col.get("min", "1")) - 1, min(int(col.get("max", "1")), last + 1)):
            widths[index] = 0 if col.get("hidden") == "1" else width(float(col.get("width", str(default))))
    return widths


def _marker_offset(sizes: list[int], first: int, last: int, offset: int) -> tuple[int, int]:
    index = first
    while index < last and offset >= sizes[index]:
        offset -= sizes[index]
        index += 1
    return index, offset


def fit_images_in_drawing(
    sheet: ET.Element, drawing: ET.Element, dimensions: Callable[[str], tuple[int, int]],
) -> int:
    count = 0
    for anchor in drawing:
        picture = anchor.find(D + "pic")
        start = anchor.find(D + "from")
        if picture is None or start is None:
            continue
        column = int(start.findtext(D + "col", "0"))
        row = int(start.findtext(D + "row", "0"))
        if column != 0:
            continue
        left, top, right, bottom = cell_bounds(sheet, column, row)
        if left != 0 or right != 0:
            raise ImageFitError("Ячейка фотографии пересекает другие столбцы.")
        widths = column_widths(sheet, right)
        heights = row_heights(sheet, bottom)
        box_width = sum(widths[left:right + 1])
        box_height = sum(heights[top:bottom + 1])
        if box_width <= 2 * GUTTER or box_height <= 2 * GUTTER:
            raise ImageFitError("Ячейка фотографии скрыта или слишком мала.")
        blip = picture.find(".//" + A + "blip")
        relationship = blip.get(R + "embed", "") if blip is not None else ""
        if not relationship:
            raise ImageFitError("Фотография не содержит встроенного изображения.")
        image_width, image_height = dimensions(relationship)
        properties = picture.find(D + "spPr")
        if properties is None:
            raise ImageFitError("Фотография не содержит параметров размещения.")
        transform = properties.find(A + "xfrm")
        if transform is not None and int(transform.get("rot", "0")) % 10800000:
            raise ImageFitError("Подготовка повёрнутой фотографии требует обычного положения изображения.")
        crop = picture.find(D + "blipFill/" + A + "srcRect")
        if crop is not None:
            image_width *= 1 - (int(crop.get("l", "0")) + int(crop.get("r", "0"))) / 100000
            image_height *= 1 - (int(crop.get("t", "0")) + int(crop.get("b", "0"))) / 100000
        if image_width <= 0 or image_height <= 0:
            raise ImageFitError("У фотографии некорректные границы кадрирования.")
        scale = min((box_width - 2 * GUTTER) / image_width, (box_height - 2 * GUTTER) / image_height)
        cx, cy = int(image_width * scale), int(image_height * scale)
        x_offset, y_offset = (box_width - cx) // 2, (box_height - cy) // 2
        target_col, col_offset = _marker_offset(widths, left, right, x_offset)
        target_row, row_offset = _marker_offset(heights, top, bottom, y_offset)
        for tag, value in (("col", target_col), ("colOff", col_offset), ("row", target_row), ("rowOff", row_offset)):
            node = start.find(D + tag)
            if node is None:
                node = ET.SubElement(start, D + tag)
            node.text = str(value)
        # A single-cell anchor carries an exact size. A two-cell anchor would
        # stretch the photograph independently when rows/columns are resized.
        anchor.tag = D + "oneCellAnchor"
        anchor.attrib.pop("editAs", None)
        end = anchor.find(D + "to")
        if end is not None:
            anchor.remove(end)
        extent = anchor.find(D + "ext")
        if extent is None:
            extent = ET.Element(D + "ext")
            anchor.insert(list(anchor).index(start) + 1, extent)
        extent.attrib.update(cx=str(cx), cy=str(cy))
        if transform is None:
            transform = ET.Element(A + "xfrm")
            properties.insert(0, transform)
        for tag, attributes in (
            ("off", {"x": str(sum(widths[:left]) + x_offset), "y": str(sum(heights[:top]) + y_offset)}),
            ("ext", {"cx": str(cx), "cy": str(cy)}),
        ):
            node = transform.find(A + tag)
            if node is None:
                node = ET.SubElement(transform, A + tag)
            node.attrib.update(attributes)
        count += 1
    return count
