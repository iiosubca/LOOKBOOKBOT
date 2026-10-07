"""Check preparation against a supplied manually corrected reference on copies."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from lookbookbot.excel_preparation import Q, _value, prepare_workbook


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("reference", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    before = hashlib.sha256(args.source.read_bytes()).hexdigest()
    copied = args.output / args.source.name
    shutil.copy2(args.source, copied)
    result = prepare_workbook(copied)
    with ZipFile(copied) as original, ZipFile(result.output) as prepared, ZipFile(args.reference) as reference:
        def strings(archive):
            return ["".join(node.text or "" for node in item.iter(Q + "t"))
                    for item in ET.fromstring(archive.read("xl/sharedStrings.xml"))]
        source_strings, reference_strings = strings(prepared), strings(reference)
        worksheets = {"xl/worksheets/sheet1.xml", "xl/worksheets/sheet2.xml"}
        changed = worksheets | {"xl/styles.xml", "xl/drawings/drawing1.xml", "xl/drawings/drawing2.xml"}
        assert original.namelist() == prepared.namelist()
        for name in original.namelist():
            if name not in changed:
                assert original.read(name) == prepared.read(name), name
        checked = 0
        for name in sorted(worksheets):
            expected = {cell.get("r"): _value(cell, reference_strings)
                        for cell in ET.fromstring(reference.read(name)).iter(Q + "c")}
            for cell in ET.fromstring(prepared.read(name)).iter(Q + "c"):
                address = cell.get("r")
                value = _value(cell, source_strings)
                if address[0] in "CDEF" and address[1:] != "1" and value:
                    assert " ".join(value.split()) == " ".join(expected.get(address, "").split()), (address, value)
                    checked += 1
    assert hashlib.sha256(args.source.read_bytes()).hexdigest() == before
    report = {
        "products": sum(sheet.products for sheet in result.sheets),
        "reference_cells_matched": checked,
        "embedded_media_unchanged": len([name for name in original.namelist() if name.startswith("xl/media/")]),
        "photos_resized": sum(sheet.resized_images for sheet in result.sheets),
        "other_archive_parts_unchanged": len(original.namelist()) - len(changed),
        "source_unchanged": True,
        "prepared_workbook": str(result.output),
    }
    (args.output / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
