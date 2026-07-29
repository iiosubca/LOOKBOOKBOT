from datetime import date

from lookbookbot.domain import master_filename, project_code, review_pdf_filename, visible_date_text


def test_tsums_names_are_stable() -> None:
    show_date = date(2026, 7, 26)
    assert project_code(show_date) == "TSUM_FS-0260726"
    assert master_filename(show_date) == "TSUM_FS-0260726_LB_WA_01.indd"
    assert review_pdf_filename(show_date) == "TSUM_FS-0260726_LB_WA_01_review.pdf"
    assert visible_date_text(show_date) == "26 ИЮЛЯ"


def test_revision_names_stay_linked_for_indesign_and_review_pdf() -> None:
    show_date = date(2026, 7, 26)

    assert master_filename(show_date, 2) == "TSUM_FS-0260726_LB_WA_02.indd"
    assert review_pdf_filename(show_date, 2) == "TSUM_FS-0260726_LB_WA_02_review.pdf"
    assert master_filename(show_date, 13) == "TSUM_FS-0260726_LB_WA_13.indd"
    assert review_pdf_filename(show_date, 13) == "TSUM_FS-0260726_LB_WA_13_review.pdf"
