from __future__ import annotations

import json
from pathlib import Path

from lookbookbot.controller import final_outputs_passed


def _write_manifest(project: Path, *, export_format: str | None) -> None:
    outputs = [{"path": f"output-{index}.pdf"} for index in range(5)]
    if export_format is not None:
        for output in outputs:
            output["export_format"] = export_format
    control = project / "control"
    control.mkdir()
    (control / "final-deliverables.json").write_text(
        json.dumps({"status": "complete", "outputs": outputs}), encoding="utf-8"
    )


def test_legacy_interactive_final_outputs_are_not_accepted(tmp_path: Path) -> None:
    _write_manifest(tmp_path, export_format=None)

    assert final_outputs_passed(tmp_path) is False


def test_print_pdf_final_outputs_are_accepted(tmp_path: Path) -> None:
    _write_manifest(tmp_path, export_format="adobe-pdf-print-v1")

    assert final_outputs_passed(tmp_path) is True
