"""Prepare the credit catalogue without reserializing embedded Excel pictures.

Worksheet cells, price styles and picture placement are rewritten. Media and
relationship parts retain their original bytes and look identities.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import posixpath
import re
import tempfile
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

from .excel_images import ImageFitError, fit_images_in_drawing, image_dimensions


NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
Q = "{" + NS + "}"
MANIFEST = ".lookbookbot-excel.json"
HEADERS = ("Наименование", "Бренд", "Цена", "Артикул")
RUBLE_FORMAT = '#,##0" ₽"'
PREPARATION_VERSION = 2


class ExcelPreparationError(ValueError):
    pass


@dataclass(frozen=True)
class SheetPreparation:
    name: str
    products: int
    price_column: str
    price_header: str
    removed_columns: tuple[str, ...]
    resized_images: int = 0


@dataclass(frozen=True)
class PreparationResult:
    source: Path
    output: Path
    sheets: tuple[SheetPreparation, ...]
    reused: bool = False

    def summary(self) -> str:
        lines = [f"Готово: {self.output.name}", "Наименование / Бренд / Цена / Артикул"]
        for sheet in self.sheets:
            lines.append(
                f"{sheet.name.strip()}: {sheet.products} товаров; цена из {sheet.price_column} "
                f"«{sheet.price_header or 'без заголовка'}»."
            )
        lines.append("Цены — числа с форматом ₽. Фотографии и номера луков сохранены.")
        images = sum(sheet.resized_images for sheet in self.sheets)
        if images:
            lines.append(f"Фотографии увеличены пропорционально и центрированы в своих ячейках: {images}.")
        return "\n".join(lines)


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _clean(value: str) -> str:
    return " ".join(value.split())


def _header(value: str) -> str:
    return re.sub(r"[^а-яa-z0-9]", "", value.casefold().replace("ё", "е"))


def _column(address: str) -> int:
    match = re.fullmatch(r"([A-Z]+)[1-9][0-9]*", address)
    if not match:
        raise ExcelPreparationError(f"Некорректный адрес ячейки: {address}")
    value = 0
    for character in match[1]:
        value = value * 26 + ord(character) - 64
    return value


def _letter(value: int) -> str:
    result = ""
    while value:
        value, rest = divmod(value - 1, 26)
        result = chr(65 + rest) + result
    return result


def _value(cell: ET.Element | None, strings: list[str]) -> str:
    if cell is None:
        return ""
    if cell.get("t") == "inlineStr":
        return "".join(node.text or "" for node in cell.iter(Q + "t"))
    node = cell.find(Q + "v")
    value = node.text or "" if node is not None else ""
    if cell.get("t") == "s" and value:
        return strings[int(value)]
    return value


def parse_price(value: str) -> Decimal:
    """Accept Russian/English grouping and decimals, reject guessed amounts."""
    text = re.sub(r"(?:₽|руб\.?|р\.|rub|rur)", "", value, flags=re.I).strip()
    text = re.sub(r"\s", "", text)
    if not re.fullmatch(r"\d+(?:[.,]\d+)*", text):
        raise ExcelPreparationError(f"Не удалось распознать цену «{value}».")
    if "," in text and "." in text:
        decimal_separator = "," if text.rfind(",") > text.rfind(".") else "."
        grouping = "." if decimal_separator == "," else ","
        whole, fraction = text.rsplit(decimal_separator, 1)
        if not re.fullmatch(r"\d{1,3}(?:" + re.escape(grouping) + r"\d{3})+", whole):
            raise ExcelPreparationError(f"Неоднозначная цена «{value}».")
        text = whole.replace(grouping, "") + "." + fraction
    elif "," in text or "." in text:
        separator = "," if "," in text else "."
        if re.fullmatch(r"\d{1,3}(?:" + re.escape(separator) + r"\d{3})+", text):
            text = text.replace(separator, "")
        elif text.count(separator) == 1:
            text = text.replace(separator, ".")
        else:
            raise ExcelPreparationError(f"Неоднозначная цена «{value}».")
    try:
        amount = Decimal(text)
    except InvalidOperation as error:
        raise ExcelPreparationError(f"Не удалось распознать цену «{value}».") from error
    if not amount.is_finite() or amount < 0 or amount.as_tuple().exponent < -2:
        raise ExcelPreparationError(f"Неоднозначная цена «{value}».")
    return amount


def _set_text(cell: ET.Element, value: str) -> None:
    for node in list(cell):
        if node.tag in {Q + "v", Q + "f", Q + "is"}:
            cell.remove(node)
    cell.set("t", "inlineStr")
    ET.SubElement(ET.SubElement(cell, Q + "is"), Q + "t").text = value


def _serialize(root: ET.Element, original: bytes) -> bytes:
    # Retain prefixes referenced by mc:Ignorable, even if ET sees no element
    # using them. Losing those declarations causes an Excel repair prompt.
    namespaces = dict(value for _, value in ET.iterparse(io.BytesIO(original), events=("start-ns",)))
    for prefix, uri in namespaces.items():
        if not re.fullmatch(r"ns\d+", prefix):
            ET.register_namespace(prefix, uri)
    serialized = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    for prefix, uri in namespaces.items():
        attribute = "xmlns" + (":" + prefix if prefix else "")
        if (attribute + "=").encode() not in serialized:
            root.set(attribute, uri)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _price_style(styles: ET.Element, base: str, cache: dict[str, str], *, fractional: bool) -> str:
    key = base + (":fractional" if fractional else ":whole")
    if key in cache:
        return cache[key]
    format_code = '#,##0.00" ₽"' if fractional else RUBLE_FORMAT
    formats = styles.find(Q + "numFmts")
    if formats is None:
        formats = ET.Element(Q + "numFmts")
        styles.insert(0, formats)
    code = next((node.get("numFmtId") for node in formats if node.get("formatCode") == format_code), None)
    if code is None:
        code = str(max([163] + [int(node.get("numFmtId", "0")) for node in formats]) + 1)
        ET.SubElement(formats, Q + "numFmt", numFmtId=code, formatCode=format_code)
    formats.set("count", str(len(formats)))
    xfs = styles.find(Q + "cellXfs")
    if xfs is None or int(base) >= len(xfs):
        raise ExcelPreparationError("В Excel повреждены стили ячеек.")
    style = copy.deepcopy(xfs[int(base)])
    style.set("numFmtId", code)
    style.set("applyNumberFormat", "1")
    cache[key] = str(len(xfs))
    xfs.append(style)
    xfs.set("count", str(len(xfs)))
    return cache[key]


def _prepare_sheet(name: str, root: ET.Element, strings: list[str], styles: ET.Element,
                   style_cache: dict[str, str]) -> SheetPreparation:
    data = root.find(Q + "sheetData")
    if data is None:
        raise ExcelPreparationError(f"Лист {name}: нет таблицы товаров.")
    rows = [(row, {_column(cell.get("r", "")): cell for cell in row}) for row in data]
    aliases = {
        "Наименование": {"наименование", "наименованиетовара", "название", "товар", "name", "product"},
        "Бренд": {"бренд", "brand", "марка"},
        "Артикул": {"артикул", "артикултовара", "sku", "article"},
    }
    fields: dict[str, int] = {}
    header_index = -1
    labels: dict[int, str] = {}
    for index, (_, cells) in enumerate(rows[:25]):
        labels = {col: _clean(_value(cell, strings)) for col, cell in cells.items()}
        fields = {key: col for key, values in aliases.items() for col, label in labels.items() if _header(label) in values}
        if len(fields) == 3:
            header_index = index
            break
    if header_index < 0:
        raise ExcelPreparationError(f"Лист {name}: не найдены Наименование, Бренд и Артикул.")
    if any(col < 3 for col in fields.values()):
        raise ExcelPreparationError(f"Лист {name}: первые два столбца должны содержать фото и номер лука.")
    candidates = [col for col, label in labels.items() if col >= 3 and
                  ("цен" in _header(label) or _header(label) in {"price", "correctprice", "корректная"})]
    marked = [col for col in candidates if "некоррект" not in _header(labels[col]) and
              (re.search(r"(?:^|\s)корректн", labels[col].casefold()) or _header(labels[col]) == "correctprice")]
    if len(marked) > 1:
        raise ExcelPreparationError(f"Лист {name}: несколько столбцов «Корректная цена».")
    if not candidates:
        raise ExcelPreparationError(f"Лист {name}: не найден столбец цены.")
    if not marked:
        # Managers also leave the second price header blank. Infer only blank
        # columns predominantly containing prices. Validation below catches
        # blanks/bad amounts in the chosen column instead of falling back.
        samples = [cells for _, cells in rows[header_index + 1:] if _value(cells.get(fields['Наименование']), strings)][:50]
        all_columns = {col for cells in samples for col in cells}
        for col in sorted(all_columns):
            if col <= max(candidates) or labels.get(col) or col in fields.values() or not samples:
                continue
            valid = 0
            for cells in samples:
                try:
                    parse_price(_value(cells.get(col), strings))
                except ExcelPreparationError:
                    continue
                valid += 1
            if valid >= max(1, len(samples) * 0.5):
                candidates.append(col)
    price_col = marked[0] if marked else max(candidates)
    fields["Цена"] = price_col
    remap = {1: 1, 2: 2, **{fields[key]: column for column, key in enumerate(HEADERS, 3)}}
    # Do not silently move native objects/controls or leave stale references.
    unsupported = {"tableParts", "hyperlinks", "dataValidations", "conditionalFormatting", "oleObjects", "controls", "extLst"}
    if any(node.tag.rsplit("}", 1)[-1] in unsupported for node in root):
        raise ExcelPreparationError(f"Лист {name}: таблица содержит дополнительные объекты или правила; подготовка остановлена.")
    products = 0
    for index, (row, cells) in enumerate(rows):
        row_number = int(row.get("r", "0"))
        product = index > header_index and any(_clean(_value(cells.get(fields[key]), strings)) for key in HEADERS)
        if product:
            products += 1
            for key in HEADERS:
                source_cell = cells.get(fields[key])
                if not _clean(_value(source_cell, strings)) or (source_cell is not None and source_cell.get("t") in {"e", "b"}):
                    raise ExcelPreparationError(f"Лист {name}, строка {row_number}: отсутствует или некорректно поле «{key}».")
        new_cells = []
        for source_column, target_column in remap.items():
            original = cells.get(source_column)
            if original is None and index == header_index and target_column >= 3:
                original = ET.Element(Q + "c", r=f"{_letter(source_column)}{row_number}",
                                      s=cells[fields['Наименование']].get("s", "0"))
            if original is None:
                continue
            cell = copy.deepcopy(original)
            cell.set("r", f"{_letter(target_column)}{row_number}")
            value = _value(original, strings)
            if index == header_index and target_column >= 3:
                _set_text(cell, HEADERS[target_column - 3])
            elif index > header_index and target_column >= 3 and value:
                if target_column == 5:
                    try:
                        amount = parse_price(value)
                    except ExcelPreparationError as error:
                        raise ExcelPreparationError(f"Лист {name}, {original.get('r')}: {error}") from error
                    for child in list(cell):
                        if child.tag in {Q + "v", Q + "f", Q + "is"}:
                            cell.remove(child)
                    cell.attrib.pop("t", None)
                    cell.set("s", _price_style(styles, original.get("s", "0"), style_cache,
                                              fractional=amount != amount.to_integral_value()))
                    ET.SubElement(cell, Q + "v").text = format(amount, "f")
                else:
                    if target_column == 6 and original.get("t") not in {"s", "inlineStr"}:
                        try:
                            number = Decimal(value)
                            if number == number.to_integral_value():
                                value = str(int(number))
                        except InvalidOperation:
                            pass
                    _set_text(cell, _clean(value))
            if cell.find(Q + "f") is not None and source_column >= 3:
                raise ExcelPreparationError(f"Лист {name}, {original.get('r')}: формула не содержит сохранённого значения. Пересчитайте файл в Excel.")
            new_cells.append(cell)
        row[:] = sorted(new_cells, key=lambda cell: _column(cell.get("r", "")))
        if "spans" in row.attrib:
            row.set("spans", "1:6")
    if not products:
        raise ExcelPreparationError(f"Лист {name}: нет товаров.")
    cols = root.find(Q + "cols")
    if cols is not None:
        old_columns = list(cols)
        cols[:] = []
        for source_column, target_column in sorted(remap.items(), key=lambda pair: pair[1]):
            original = next((col for col in old_columns if int(col.get("min", "0")) <= source_column <= int(col.get("max", "0"))), None)
            col = copy.deepcopy(original) if original is not None else ET.Element(Q + "col", width="24", customWidth="1")
            col.set("min", str(target_column))
            col.set("max", str(target_column))
            cols.append(col)
    merges = root.find(Q + "mergeCells")
    if merges is not None:
        for merge in list(merges):
            first, last = merge.get("ref", "").split(":")
            first_col, last_col = _column(first), _column(last)
            if first_col == last_col and first_col in remap:
                merge.set("ref", re.sub(r"[A-Z]+", _letter(remap[first_col]), first) + ":" + re.sub(r"[A-Z]+", _letter(remap[last_col]), last))
            elif first_col != last_col and any(col in remap for col in range(first_col, last_col + 1)):
                raise ExcelPreparationError(f"Лист {name}: объединение {merge.get('ref')} пересекает товарные столбцы.")
            else:
                merges.remove(merge)
        merges.set("count", str(len(merges)))
    dimension = root.find(Q + "dimension")
    if dimension is not None:
        dimension.set("ref", f"A1:F{max(int(row.get('r', '1')) for row, _ in rows)}")
    auto_filter = root.find(Q + "autoFilter")
    if auto_filter is not None:
        root.remove(auto_filter)
    kept = set(fields.values()) | {1, 2}
    removed = tuple(f"{_letter(col)}: {label or 'без заголовка'}" for col, label in sorted(labels.items())
                    if col not in kept and (label or any(_value(cells.get(col), strings) for _, cells in rows)))
    return SheetPreparation(name, products, _letter(price_col), labels.get(price_col, ""), removed)


def _read_selection(folder: Path) -> dict | None:
    try:
        data = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))
        if data.get("schema") != 1:
            return None
        for key in ("source", "output"):
            filename = data[key]
            if not isinstance(filename, str) or Path(filename).name != filename or "/" in filename or "\\" in filename:
                return None
            path = folder / filename
            if path.suffix.casefold() != ".xlsx" or not path.is_file() or _digest(path) != data[key + "_sha256"]:
                return None
        return data
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def selected_prepared_workbook(files: list[Path]) -> Path | None:
    candidates = []
    available = {str(path.resolve()).casefold() for path in files}
    for folder in {path.parent for path in files}:
        selection = _read_selection(folder)
        if selection:
            output = (folder / selection["output"]).resolve()
            if str(output).casefold() in available:
                candidates.append(output)
    return candidates[0] if len(candidates) == 1 else None


def _related_part(part: str, target: str) -> str:
    return target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join(posixpath.dirname(part), target))


def _fit_sheet_images(archive: ZipFile, part: str, sheet: ET.Element, patches: dict[str, bytes]) -> int:
    node = sheet.find(Q + "drawing")
    if node is None:
        return 0
    relationships_part = posixpath.join(posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels")
    relationships = {node.get("Id"): node.get("Target", "") for node in ET.fromstring(archive.read(relationships_part))}
    drawing_part = _related_part(part, relationships[node.get("{" + REL + "}id")])
    original = archive.read(drawing_part)
    drawing = ET.fromstring(original)
    rels_part = posixpath.join(posixpath.dirname(drawing_part), "_rels", posixpath.basename(drawing_part) + ".rels")
    media = {node.get("Id"): node.get("Target", "") for node in ET.fromstring(archive.read(rels_part))}
    sizes: dict[str, tuple[int, int]] = {}

    def dimensions(relationship: str) -> tuple[int, int]:
        if relationship not in sizes:
            sizes[relationship] = image_dimensions(archive.read(_related_part(drawing_part, media[relationship])))
        return sizes[relationship]

    try:
        count = fit_images_in_drawing(sheet, drawing, dimensions)
    except ImageFitError as error:
        raise ExcelPreparationError(f"Фотографии: {error}") from error
    if count:
        patches[drawing_part] = _serialize(drawing, original)
    return count


def prepare_workbook(source: Path) -> PreparationResult:
    source = source.expanduser().resolve()
    if source.suffix.casefold() != ".xlsx" or not source.is_file():
        raise ExcelPreparationError("Выберите существующий файл .xlsx.")
    selection = _read_selection(source.parent)
    if selection and source.name in {selection['source'], selection['output']}:
        if selection.get("preparation_version") == PREPARATION_VERSION:
            sheets = tuple(SheetPreparation(**{**item, 'removed_columns': tuple(item['removed_columns'])}) for item in selection['sheets'])
            return PreparationResult(source.parent / selection['source'], source.parent / selection['output'], sheets, True)
        # Re-running the button after an app update upgrades the original
        # catalogue, rather than reusing an older copy with tiny pictures.
        source = source.parent / selection['source']
    before = _digest(source)
    patches: dict[str, bytes] = {}
    reports = []
    try:
        with ZipFile(source) as archive:
            styles_bytes = archive.read("xl/styles.xml")
            styles = ET.fromstring(styles_bytes)
            strings = ["".join(node.text or "" for node in item.iter(Q + "t")) for item in ET.fromstring(archive.read("xl/sharedStrings.xml"))] if "xl/sharedStrings.xml" in archive.namelist() else []
            book = ET.fromstring(archive.read("xl/workbook.xml"))
            relations = {node.get("Id"): node.get("Target", "") for node in ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))}
            found = set()
            cache: dict[str, str] = {}
            for sheet in book.findall(Q + "sheets/" + Q + "sheet"):
                name = sheet.get("name", "")
                if name.strip().upper() not in {"W", "M"}:
                    continue
                if name.strip().upper() in found:
                    raise ExcelPreparationError(f"В Excel несколько листов {name.strip()}.")
                found.add(name.strip().upper())
                target = relations[sheet.get("{" + REL + "}id")]
                part = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))
                original = archive.read(part)
                root = ET.fromstring(original)
                report = _prepare_sheet(name, root, strings, styles, cache)
                resized = _fit_sheet_images(archive, part, root, patches)
                reports.append(replace(report, resized_images=resized))
                patches[part] = _serialize(root, original)
            if found != {"W", "M"}:
                raise ExcelPreparationError("Для лукбука нужны оба листа: W и M.")
            patches["xl/styles.xml"] = _serialize(styles, styles_bytes)
            if _digest(source) != before:
                raise ExcelPreparationError("Исходный Excel изменился во время подготовки. Повторите подготовку.")
            output = source.with_name(source.stem + " prepared.xlsx")
            index = 2
            while output.exists():
                output = source.with_name(f"{source.stem} prepared_{index:02}.xlsx")
                index += 1
            handle, temp_name = tempfile.mkstemp(suffix=".xlsx", prefix=".lookbookbot-", dir=source.parent)
            os.close(handle)
            temporary = Path(temp_name)
            try:
                with ZipFile(temporary, "w") as result:
                    for item in archive.infolist():
                        result.writestr(item, patches.get(item.filename, archive.read(item.filename)))
                # Windows rename refuses an existing destination. Never overwrite
                # the manager's file, reference copy or an earlier preparation.
                temporary.rename(output)
            finally:
                temporary.unlink(missing_ok=True)
    except (BadZipFile, ET.ParseError, KeyError, IndexError) as error:
        raise ExcelPreparationError("Не удалось прочитать структуру Excel. Откройте его в Excel и сохраните как .xlsx.") from error
    from dataclasses import asdict
    manifest = {
        "schema": 1, "preparation_version": PREPARATION_VERSION, "source": source.name, "source_sha256": before,
        "output": output.name, "output_sha256": _digest(output),
        "sheets": [asdict(sheet) for sheet in reports],
    }
    handle, temp_name = tempfile.mkstemp(prefix=".lookbookbot-selection-", dir=source.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2)
        os.replace(temp_name, source.parent / MANIFEST)
    finally:
        Path(temp_name).unlink(missing_ok=True)
    return PreparationResult(source, output, tuple(reports))
