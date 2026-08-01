from __future__ import annotations

from types import SimpleNamespace

from lookbookbot import ui
from lookbookbot.pipeline import PipelineResult


def test_create_caption_revision_worker_reports_ready_result_without_name_error(monkeypatch) -> None:
    """The successful revision path must emit a result, not fail after saving."""

    class Store:
        def get_project(self, _project_id: str):
            return SimpleNamespace(project_dir="C:/project")

    class Engine:
        def __init__(self, _store, *, log, progress) -> None:
            self.log = log
            self.progress = progress

        def prepare_caption_revision(self, _project, _audit) -> None:
            return None

        def recover_missing_caption_revision_master(self, _project) -> None:
            return None

        def finish_review_before_caption_revision(self, _project) -> PipelineResult:
            raise AssertionError("Creating a revision must not require the prior review PDF.")

        def begin_caption_revision(self, _project, _audit) -> None:
            return None

        def set_caption_revision_visual_mode(self, _project, *, targeted: bool) -> None:
            raise AssertionError("Creating a revision must configure visual mode only after the credits pass.")

        def run(self, _project, _start_key, *, continue_after: bool, stop_after: str) -> PipelineResult:
            assert continue_after is False
            assert stop_after == "captions"
            return PipelineResult(("captions",), None, "Кредиты применены.")

        def caption_revision_ready_message(self, _project) -> str:
            return "Версия TSUM_FS-0261115_LB_WA_02.indd готова к просмотру."

    monkeypatch.setattr(ui, "PipelineEngine", Engine)
    worker = ui.PipelineWorker(
        Store(),
        "project-id",
        None,
        False,
        caption_revision_audit="C:/project/control/work/manual-caption-revisions/draft.json",
        caption_revision_action="create",
    )
    completed: list[PipelineResult] = []
    failures: list[str] = []
    worker.finished.connect(completed.append)
    worker.failed.connect(failures.append)

    worker.run()

    assert not failures
    assert len(completed) == 1
    assert completed[0].completed == ("captions",)
    assert completed[0].message == "Версия TSUM_FS-0261115_LB_WA_02.indd готова к просмотру."


def test_export_revision_finishes_pending_captions_before_visual_or_pdf(monkeypatch) -> None:
    """A direct PDF click after restart must not skip the copied revision's captions gate."""

    calls: list[str] = []

    class Store:
        def get_project(self, _project_id: str):
            return SimpleNamespace(project_dir="C:/project")

    class Engine:
        def __init__(self, _store, *, log, progress) -> None:
            self.log = log
            self.progress = progress

        def recover_missing_caption_revision_master(self, _project) -> None:
            calls.append("recover")

        def ensure_caption_revision_captions(self, _project) -> None:
            calls.append("captions")

        def set_caption_revision_visual_mode(self, _project, *, targeted: bool) -> None:
            assert targeted is True
            calls.append("visual-mode")

        def run(self, _project, start_key, *, continue_after: bool, stop_after: str) -> PipelineResult:
            assert start_key is None
            assert continue_after is True
            assert stop_after == "review"
            calls.append("review")
            return PipelineResult(("review",), None, "PDF готов.")

    monkeypatch.setattr(ui, "PipelineEngine", Engine)
    worker = ui.PipelineWorker(
        Store(), "project-id", None, True,
        caption_revision_action="export-review", targeted_caption_visual=True,
    )
    completed: list[PipelineResult] = []
    failures: list[str] = []
    worker.finished.connect(completed.append)
    worker.failed.connect(failures.append)

    worker.run()

    assert not failures
    assert calls == ["recover", "captions", "visual-mode", "review"]
    assert completed[0].message == "PDF готов."
