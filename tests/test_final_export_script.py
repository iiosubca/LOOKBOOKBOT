from __future__ import annotations

from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "automation-engine" / "lookbook-layout" / "scripts" / "run_lookbook_gate_com.ps1"


def test_final_export_passes_gender_ranges_to_com_as_scalars() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "$prefs.PageRange = if ($range -eq 'ALL')" not in source
    assert "if ($range -eq 'ALL') { $prefs.PageRange = $ALL_PAGES }" in source
    assert "else { $prefs.PageRange = [string]$range }" in source


def test_final_export_writes_active_file_progress_before_exporting() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "status = 'exporting'" in source
    assert "active = $active" in source
