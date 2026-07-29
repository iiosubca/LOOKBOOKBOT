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


class _ScaledTextMatrixPage:
    mediabox = type("MediaBox", (), {"top": 600.0, "bottom": 0.0})()

    def extract_text(self, *, visitor_text) -> None:
        # InDesign frequently uses a one-point ``Tf`` with the real 8-pt
        # character size stored in the text matrix.  The visual planner must
        # use that effective scale rather than collapse the printed line.
        visitor_text("BOTTEGA VENETA", [1, 0, 0, 1, 0, 0], [8, 0, 0, 8, 66, 500], None, 1)


class _ScaledTextMatrixReader:
    pages = [_ScaledTextMatrixPage()]


def test_visible_caption_bounds_use_page_space_for_nested_pdf_forms() -> None:
    bounds, count = gate._visible_caption_text_bounds(_FakeReader(), 1, [0, 0, 600, 600])

    assert count == 2
    assert bounds[0] == 98.0
    assert bounds[2] == 202.0
    assert bounds[1] == 98.0


def test_visible_caption_regions_do_not_turn_interline_whitespace_into_text() -> None:
    regions, count = gate._visible_caption_text_regions(_FakeReader(), 1, [0, 0, 600, 600])

    assert count == 2
    assert regions == [[98.0, 98.0, 102.0, regions[0][3]], [198.0, 98.0, 202.0, regions[1][3]]]
    # The union rectangle remains available for legacy callers, but the audit
    # must keep the 96-point gap between visible rows transparent.
    assert regions[0][2] < regions[1][0]


def test_visible_caption_bounds_use_effective_text_matrix_scale() -> None:
    bounds, count = gate._visible_caption_text_bounds(_ScaledTextMatrixReader(), 1, [0, 0, 600, 600])

    assert count == 1
    # 14 characters at 8 pt cannot be represented by the old 15-pt-wide
    # fallback column.  Its calculated extent must retain the matrix scale.
    assert bounds[3] > 150.0


def test_degenerate_pdf_text_geometry_falls_back_to_existing_credits_frame() -> None:
    bounds, source = gate._reliable_visible_caption_bounds(
        [66.0, 90.0, 80.9, 350.0], 24, 24, [66.0, 66.0, 186.5, 356.5],
    )

    assert bounds == [66.0, 66.0, 186.5, 356.5]
    assert source == "native-frame-fallback-degenerate-pdf-text-coordinates"


def test_degenerate_pdf_text_width_also_uses_existing_credits_frame() -> None:
    bounds, source = gate._reliable_visible_caption_bounds(
        [66.0, 66.0, 186.5, 80.9], 24, 24, [66.0, 66.0, 186.5, 186.5],
    )

    assert bounds == [66.0, 66.0, 186.5, 186.5]
    assert source == "native-frame-fallback-degenerate-pdf-text-coordinates"


def test_normal_pdf_text_geometry_keeps_measured_glyph_bounds() -> None:
    bounds, source = gate._reliable_visible_caption_bounds(
        [66.0, 90.0, 150.0, 350.0], 24, 24, [66.0, 66.0, 186.5, 356.5],
    )

    assert bounds == [66.0, 90.0, 150.0, 350.0]
    assert source == "pdf-glyph-coordinates"


def test_clearance_status_ignores_scattered_background_texture() -> None:
    status, _reason = gate._caption_clearance_status(0.009323, 0.000065)

    assert status == gate.CAPTION_CLEARANCE_CLEAR


def test_clearance_status_keeps_a_contiguous_model_component_blocked() -> None:
    status, _reason = gate._caption_clearance_status(0.009323, 0.009323)

    assert status == gate.CAPTION_CLEARANCE_COLLISION


def test_clearance_retry_rebases_stale_delta_baseline_to_current_master(tmp_path: Path) -> None:
    project = tmp_path / "project"
    control = project / "control" / "visual"
    control.mkdir(parents=True)
    master = project / "master.indd"
    master.write_bytes(b"master")
    state = {"session_id": "session", "master": "master.indd", "look_count": 1}
    active = control / "composition-applied.json"
    gate.write_json(active, {
        "schema": gate.SCHEMA,
        "generator": "run_lookbook_gate_com.ps1:ApplyCompositionDelta",
        "session_id": "session",
        "master": gate.identity(master),
        "items": [{"look_id": "LOOK_001"}],
    })
    plan = {"prior_proof_archive": "control/history/old-baseline"}

    changed = gate._rebase_clearance_plan_to_current_master(project, state, plan)

    assert changed is True
    baseline = project / plan["prior_proof_archive"] / "visual" / "composition-applied.json"
    assert baseline.is_file()
    assert gate.read_json(baseline)["master"] == gate.identity(master)


def test_controller_exposes_grounded_rejection_recovery_commands() -> None:
    source = GATE_PATH.read_text(encoding="utf-8")

    assert "def command_restart_visual_confirmations" in source
    assert 'restart-visual-confirmations' in source
    assert '--force-looks' in source
    assert "if entries:" in source
    assert "def _rebase_clearance_plan_to_current_master" in source
