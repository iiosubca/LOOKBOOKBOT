from __future__ import annotations

import sys
import csv
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
Image = pytest.importorskip("PIL.Image")

sys.path.insert(0, str(Path(__file__).parents[1] / "automation-engine/lookbook-layout/scripts"))
from caption_photo_sources import caption_photo_source
from reference_photo_render import crop_rendered_photo
import render_caption_mapping_evidence as renderer
from lookbook_gate import GateError, validate_caption_map


def test_missing_hires_uses_the_visible_reference_in_credit_proofs(tmp_path):
    hires = tmp_path / "control/work/_mat/hires"
    hires.mkdir(parents=True)
    folder = tmp_path / "control/work/missing-photo-reference"
    folder.mkdir(parents=True)
    placeholder = "__lbb_missing_LOOK_001_LEFT.jpg"
    Image.new("RGB", (50, 100), "white").save(hires / placeholder)
    Image.new("RGB", (50, 100), "brown").save(folder / "LOOK_001_LEFT.jpg")
    source = caption_photo_source(tmp_path, {"look_id": "LOOK_001", "left_filename": placeholder}, hires, "left")
    canvas = Image.new("RGB", renderer.PANEL)
    renderer.paste_panel(canvas, source, 0, "PDF LEFT")
    pixel = canvas.getpixel((renderer.PANEL[0] // 2, renderer.PANEL[1] // 2))
    assert pixel[0] > pixel[1] * 2


def test_missing_reference_does_not_silently_reuse_a_blank_placeholder(tmp_path):
    hires = tmp_path / "hires"
    hires.mkdir()
    name = "__lbb_missing_LOOK_001_LEFT.jpg"
    (hires / name).touch()
    with pytest.raises(ValueError, match="missing visible"):
        caption_photo_source(tmp_path, {"look_id": "LOOK_001", "left_filename": name}, hires, "left")


def test_reference_pixels_are_hash_bound_as_well_as_the_placed_placeholder(tmp_path, monkeypatch):
    hires = tmp_path / "control/work/_mat/hires"
    hires.mkdir(parents=True)
    reference = tmp_path / "control/work/missing-photo-reference"
    reference.mkdir(parents=True)
    excel = tmp_path / "control/work/_mat/excel-images/W_001_01.jpg"
    excel.parent.mkdir(parents=True)
    Image.new("RGB", (800, 1200), "brown").save(excel)
    row = {"look_id": "LOOK_001", "excel_sheet": "W", "excel_look_number": "1",
           "excel_image": "control/work/_mat/excel-images/W_001_01.jpg",
           "left_filename": "__lbb_missing_LOOK_001_LEFT.jpg", "right_filename": "right.jpg",
           "evidence_file": "control/work/mapping-evidence/LOOK_001.jpg", "visual_status": "CONFIRMED"}
    Image.new("RGB", (800, 1200), "white").save(hires / row["left_filename"])
    Image.new("RGB", (800, 1200), "brown").save(hires / "right.jpg")
    fallback = reference / "LOOK_001_LEFT.jpg"
    Image.new("RGB", (800, 1200), "brown").save(fallback)
    mapping = tmp_path / "control/work/caption-map.tsv"
    with mapping.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=renderer.MAP_FIELDS, delimiter="\t")
        writer.writeheader()
        writer.writerow(row)
    monkeypatch.setattr(sys, "argv", ["renderer", str(tmp_path)])
    renderer.main()
    manifest = json.loads((tmp_path / "control/work/mapping-evidence/manifest.json").read_text())
    assert manifest["items"]["LOOK_001"]["left_reference_sha256"] == renderer.digest(fallback)
    assert validate_caption_map(tmp_path, mapping, [row], hires, 1)[0]["look_id"] == "LOOK_001"
    Image.new("RGB", (800, 1200), "yellow").save(fallback)
    with pytest.raises(GateError, match="does not match"):
        validate_caption_map(tmp_path, mapping, [row], hires, 1)


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_rendered_crop_respects_page_rotation_and_pdf_coordinates(rotation):
    page = SimpleNamespace(cropbox=SimpleNamespace(left=10, bottom=20, right=210, top=120), rotation=rotation)
    image = Image.new("RGB", (200, 100), "brown")
    cropped = crop_rendered_photo(page, (100, 0, 0, 100, 10, 20), image)
    assert cropped.size == ((100, 100) if rotation in (0, 180) else (200, 50))
