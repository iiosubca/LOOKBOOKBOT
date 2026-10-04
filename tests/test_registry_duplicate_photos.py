from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
sys.path.insert(0, str(Path(__file__).parents[1] / "automation-engine/lookbook-layout/scripts"))
import build_reference_registry as builder


def _photos(root, names_contents):
    paths = []
    for name, content in names_contents:
        path = root / name
        path.write_bytes(content)
        paths.append(path)
    return paths


def test_only_exact_bytes_define_duplicates_and_sources_are_untouched(tmp_path):
    files = _photos(tmp_path, [("A copy.jpg", b"abcd"), ("A.jpg", b"abcd"),
                               ("unrelated-name.jpeg", b"abcd"), ("retouch copy.jpg", b"abce")])
    before = {file.name: file.read_bytes() for file in files}
    selected, groups = builder._distinct_hires(files)
    assert [file.name for file in selected] == ["A.jpg", "retouch copy.jpg"]
    assert groups == [{"selected": "A.jpg", "copies": ["A copy.jpg", "unrelated-name.jpeg"],
                       "sha256": hashlib.sha256(b"abcd").hexdigest(), "bytes": 4}]
    assert {file.name: file.read_bytes() for file in files} == before


def test_existing_registry_copy_is_retained_on_resume(tmp_path):
    files = _photos(tmp_path, [("A.jpg", b"left"), ("A copy.jpg", b"left")])
    selected, groups = builder._distinct_hires(files, {"A COPY.JPG"})
    assert selected == [tmp_path / "A copy.jpg"]
    assert groups[0]["copies"] == ["A.jpg"]


def test_representative_is_stable_when_input_order_changes(tmp_path):
    files = _photos(tmp_path, [("z.jpg", b"1"), ("b copy.jpg", b"1"),
                               ("a.jpg", b"1"), ("a copy.jpg", b"2")])
    assert builder._distinct_hires(files) == builder._distinct_hires(list(reversed(files)))


@pytest.mark.parametrize("count", [1, 5, 52])
def test_duplicates_do_not_turn_available_photos_into_quick_placeholders(tmp_path, count):
    files = _photos(tmp_path, [(f"photo-{n:03}{suffix}.jpg", str(n).encode())
                               for n in range(count * 2) for suffix in ("", " copy")])
    selected, groups = builder._distinct_hires(files)
    assert len(selected) == count * 2 and len(groups) == count * 2
    cost = np.full((count * 2, len(selected)), .8)
    np.fill_diagonal(cost, .01)
    assignment, missing, _diagnostics, problems = builder._quick_missing_looks(cost, selected, count * 2)
    assert assignment == {n: n for n in range(count * 2)}
    assert missing == set() and problems == []
    # The normal mode uses the same distinct candidate pool and margins.
    ordinary = builder.minimum_assignment(cost)
    assert ordinary == list(range(count * 2))
    assert builder._diagnose_assignment(list(range(count * 2)), ordinary, cost, selected)[1] == []


def test_real_ambiguity_is_not_hidden_by_deduplication(tmp_path):
    files = _photos(tmp_path, [(f"photo-{n}.jpg", str(n).encode()) for n in range(4)])
    selected, groups = builder._distinct_hires(files)
    assert len(selected) == 4 and groups == []
    cost = np.array([[.10, .101, .8, .8], [.101, .10, .8, .8]])
    assert builder._quick_missing_looks(cost, selected, 2)[1] == {1}


def test_copies_cannot_fake_enough_distinct_photos(tmp_path):
    files = _photos(tmp_path, [("A.jpg", b"left"), ("A copy.jpg", b"left")])
    selected, _groups = builder._distinct_hires(files)
    assert len(selected) == 1
    assignment, missing, _diagnostics, _problems = builder._quick_missing_looks(np.array([[.01], [.8]]), selected, 2)
    assert assignment == {} and missing == {1}


@pytest.mark.parametrize("allow_missing,photo_only", [(False, False), (True, False), (True, True)])
def test_real_builder_uses_distinct_images_in_every_mode(tmp_path, monkeypatch, allow_missing, photo_only):
    from PIL import Image
    import lookbook_gate
    import reference_photo_render
    import shutil
    hires = tmp_path / "_MAT/hires"
    material = tmp_path / "control/work/_mat"
    hires.mkdir(parents=True)
    material.mkdir(parents=True)
    reference = material / "reference.pdf"
    reference.write_bytes(b"test-pdf-with-mocked-reader")
    left = Image.new("RGB", (360, 540), "red")
    right = Image.new("RGB", (360, 540), "blue")
    left.save(hires / "left.jpg")
    right.save(hires / "right.jpg")
    shutil.copy2(hires / "left.jpg", hires / "left copy.jpg")
    shutil.copy2(hires / "right.jpg", hires / "unrelated-name.jpg")
    originals = {file.name: file.read_bytes() for file in hires.iterdir()}
    class Reader:
        pages = [object()]
    monkeypatch.setattr(builder, "PdfReader", lambda *_: Reader())
    monkeypatch.setattr(builder, "visible_pdf_draws", lambda *_: [(None, left), (None, right)])
    monkeypatch.setattr(lookbook_gate, "render_pdf_pages", lambda *_args, **_kwargs: {1: "not-used"})
    monkeypatch.setattr(reference_photo_render, "rendered_photo_pair", lambda *_: (left.copy(), right.copy()))
    registry = tmp_path / "control/work/look-register.tsv"
    builder.build(tmp_path, reference, hires, registry, None, False, allow_missing, photo_only)
    rows = builder.existing_registry(registry)
    assert rows[0]["left_filename"] == "left.jpg"
    assert rows[0]["right_filename"] == "right.jpg"
    manifest = json.loads(next((tmp_path / "control/work/registry-build").glob("*/manifest.json")).read_text())
    assert manifest["hires_available"] == 4 and manifest["hires_unique"] == 2
    assert manifest["hires_used"] == 2 and manifest["hires_duplicate_files"] == 2
    assert manifest["missing_looks"] == []
    assert {file.name: file.read_bytes() for file in hires.iterdir()} == originals
