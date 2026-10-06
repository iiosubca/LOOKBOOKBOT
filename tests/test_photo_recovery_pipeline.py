from datetime import date
from pathlib import Path

import pytest

from lookbookbot.domain import BuildMode, ProviderKind
from lookbookbot.pipeline import PipelineEngine, ReviewRequired, _write_tsv
from lookbookbot.state import StateStore


def fixture(root, mode):
    store = StateStore(root / "test.db")
    project = store.save_project(name="test", source_dir=root, output_root=root,
                                 project_dir=root / "book", show_date=date(2026, 10, 21),
                                 provider=ProviderKind.CODEX, model="test-model", build_mode=mode)
    registry = project.project_dir / "control/work/look-register.tsv"
    registry.parent.mkdir(parents=True)
    rows = [{"look_id": "LOOK_001", "spread_order": "1", "pdf_spread": "1",
             "left_filename": "__lbb_missing_LOOK_001_LEFT.jpg", "right_filename": "__lbb_missing_LOOK_001_RIGHT.jpg",
             "indd_left_page": "2", "indd_right_page": "3"}]
    _write_tsv(registry, rows)
    return PipelineEngine(store), project, registry


@pytest.mark.parametrize("mode", [BuildMode.FULL, BuildMode.QUICK, BuildMode.PHOTOS])
def test_photo_recovery_precedes_import_and_only_full_mode_blocks_unresolved_slots(tmp_path, monkeypatch, mode):
    engine, project, registry = fixture(tmp_path, mode)
    monkeypatch.setattr(engine, "_require_prepared", lambda _: None)
    scripts, searched = [], []
    monkeypatch.setattr(engine.controller, "script", lambda *args, **kwargs: scripts.append(args))
    monkeypatch.setattr("lookbookbot.pipeline.recover_photos", lambda *args, **kwargs: searched.append(kwargs))
    if mode == BuildMode.FULL:
        with pytest.raises(ReviewRequired):
            engine._looks(project, object())
    else:
        assert "LOOK_001" in engine._looks(project, object())
        assert "LOOK_001" in engine._photo_warning(project)
    assert len(searched) == 1
    assert "--allow-missing" in scripts[0]
    assert ("--defer-photo-review" in scripts[0]) == (mode == BuildMode.FULL)
    assert engine.store.looks(project.id)[0]["status"] == "photos_unresolved"
    assert not (project.project_dir / "control/lookbook-state.json").exists()


def test_finished_project_is_not_rebuilt_or_sent_for_photo_search(tmp_path, monkeypatch):
    engine, project, registry = fixture(tmp_path, BuildMode.QUICK)
    (project.project_dir / "control/lookbook-state.json").write_text("{}")
    monkeypatch.setattr(engine, "_require_prepared", lambda _: None)
    scripts = []
    monkeypatch.setattr(engine.controller, "script", lambda *args, **kwargs: scripts.append(args))
    monkeypatch.setattr("lookbookbot.pipeline.recover_photos", lambda *args, **kwargs: pytest.fail("Finished project must stay unchanged"))
    before = registry.read_bytes()
    engine._looks(project, object())
    assert not any(args[0] == "build_reference_registry.py" for args in scripts)
    assert registry.read_bytes() == before


def test_manual_photo_selections_are_reserved_and_do_not_pollute_registry_schema(tmp_path, monkeypatch):
    engine, project, registry = fixture(tmp_path, BuildMode.QUICK)
    monkeypatch.setattr(engine, "_require_prepared", lambda _: None)
    engine.store.replace_looks(project.id, [{"look_id": "LOOK_001", "status": "manual", "left_filename": "chosen-left.jpg", "right_filename": "chosen-right.jpg"}])
    monkeypatch.setattr(engine.controller, "script", lambda *args, **kwargs: None)
    received = []
    monkeypatch.setattr("lookbookbot.pipeline.recover_photos", lambda *args, **kwargs: received.append(kwargs))
    engine._looks(project, object())
    assert received[0]["exclude_looks"] == {"LOOK_001"}
    assert received[0]["reserved_filenames"] == {"chosen-left.jpg", "chosen-right.jpg"}
    assert "status" not in registry.read_text().splitlines()[0]
    assert engine.store.looks(project.id)[0]["status"] == "manual"
