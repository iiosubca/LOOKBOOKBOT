from __future__ import annotations

import importlib.util
from pathlib import Path


GATE_PATH = Path(__file__).parents[1] / "automation-engine" / "lookbook-layout" / "scripts" / "lookbook_gate.py"
SPEC = importlib.util.spec_from_file_location("lookbook_gate_caption_coordinates", GATE_PATH)
assert SPEC and SPEC.loader
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


class _FakePage:
    mediabox = type("MediaBox", (), {"top": 600.0, "bottom": 0.0})()

    def extract_text(self, *, visitor_text) -> None:
        # The two credit fields share the same local text matrix but live in
        # different nested-form positions.  The page-space geometry must use
        # the current graphics matrix as well as the local text matrix.
        visitor_text("пальто", [1, 0, 0, 1, 100, 500], [1, 0, 0, 1, 0, 0], None, 10)
        visitor_text("бренд", [1, 0, 0, 1, 100, 400], [1, 0, 0, 1, 0, 0], None, 10)


class _FakeReader:
    pages = [_FakePage()]


def test_visible_caption_bounds_use_page_space_for_nested_pdf_forms() -> None:
    bounds, count = gate._visible_caption_text_bounds(_FakeReader(), 1, [0, 0, 600, 600])

    assert count == 2
    assert bounds[0] == 98.0
    assert bounds[2] == 202.0
    assert bounds[1] == 98.0


def test_controller_exposes_grounded_rejection_recovery_commands() -> None:
    source = GATE_PATH.read_text(encoding="utf-8")

    assert "def command_restart_visual_confirmations" in source
    assert 'restart-visual-confirmations' in source
    assert '--force-looks' in source
