from __future__ import annotations

from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "automation-engine" / "lookbook-layout" / "scripts" / "run_lookbook_gate_com.ps1"


def test_final_export_passes_gender_ranges_to_com_as_scalars() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "$Preferences.PageRange = if ($range -eq 'ALL')" not in source
    assert "if ([string]::IsNullOrWhiteSpace($RequestedPageRange) -or $RequestedPageRange -eq 'ALL') { $Preferences.PageRange = $ALL_PAGES }" in source
    assert "else { $Preferences.PageRange = [string]$RequestedPageRange }" in source


def test_final_export_writes_active_file_progress_before_exporting() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "status = 'exporting'" in source
    assert "active = $active" in source


def test_final_export_uses_adobe_print_pdf_jpeg_downsampling() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "$app.PDFExportPreferences" in source
    assert "$app.InteractivePDFExportPreferences" not in source
    assert "$doc.Export($PRINT_PDF, $destination, $false)" in source
    assert "$Preferences.ColorBitmapCompression = $BITMAP_COMPRESSION_JPEG" in source
    assert "$Preferences.ColorBitmapQuality = $COMPRESSION_QUALITY_MEDIUM" in source
    assert "$Preferences.ColorBitmapSamplingDPI = $Resolution" in source
    assert "$Preferences.GrayscaleBitmapSamplingDPI = $Resolution" in source
    assert "$COMPRESSION_QUALITY_MEDIUM = 1701727588" in source
