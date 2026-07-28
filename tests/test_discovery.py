from pathlib import Path

from lookbookbot.config import ToolPaths
from lookbookbot.discovery import discover_sources, infer_output_root


def test_discovers_standard_sources(tmp_path: Path) -> None:
    sources = tmp_path / "SOURCES"
    mat = sources / "_mat"
    hires = mat / "hires"
    hires.mkdir(parents=True)
    (mat / "reference.pdf").write_bytes(b"%PDF-test")
    (mat / "credits.xlsx").write_bytes(b"xlsx")
    (hires / "01.jpg").write_bytes(b"jpg")
    template = sources / "master_AUTOMATION.indd"
    template.write_bytes(b"indd")
    fake_python = tmp_path / "python.exe"
    fake_python.write_bytes(b"exe")
    scripts = tmp_path / "engine" / "scripts"
    scripts.mkdir(parents=True)
    gate = scripts / "lookbook_gate.py"
    gate.write_text("", encoding="utf-8")
    tools = ToolPaths(fake_python, scripts, gate, template)

    report = discover_sources(sources, tools)

    assert report.bundle is not None
    assert report.bundle.reference_pdf.name == "reference.pdf"
    assert report.bundle.workbook.name == "credits.xlsx"
    assert report.bundle.hires == hires.resolve()
    assert infer_output_root(sources) == tmp_path.resolve()


def test_rejects_multiple_reference_pdfs(tmp_path: Path) -> None:
    (tmp_path / "hires").mkdir()
    (tmp_path / "hires" / "1.jpg").write_bytes(b"x")
    (tmp_path / "a.pdf").write_bytes(b"x")
    (tmp_path / "b.pdf").write_bytes(b"x")
    (tmp_path / "a.xlsx").write_bytes(b"x")
    template = tmp_path / "x_AUTOMATION.indd"
    template.write_bytes(b"x")
    tools = ToolPaths(tmp_path / "python.exe", tmp_path, tmp_path / "gate.py", template)

    report = discover_sources(tmp_path, tools)

    assert report.bundle is None
    assert "ровно один PDF" in report.messages[0]
