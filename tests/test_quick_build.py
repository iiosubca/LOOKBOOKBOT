from datetime import date
from pathlib import Path

import pytest

from lookbookbot.domain import BuildMode, ProviderKind, StageStatus
from lookbookbot.pipeline import PipelineEngine
from lookbookbot.providers import CodexProvider, ProviderError
from lookbookbot.state import StateStore


def _project(tmp_path: Path):
    store = StateStore(tmp_path / "state.db")
    project = store.save_project(
        name="TSUM_FS-0260828",
        source_dir=tmp_path / "sources",
        output_root=tmp_path,
        project_dir=tmp_path / "TSUM_FS-0260828",
        show_date=date(2026, 8, 28),
        provider=ProviderKind.CODEX,
        model="",
        build_mode=BuildMode.QUICK,
    )
    return store, project


def _photo_project(tmp_path: Path):
    store = StateStore(tmp_path / "state.db")
    project = store.save_project(
        name="TSUM_FS-0260829",
        source_dir=tmp_path / "sources",
        output_root=tmp_path,
        project_dir=tmp_path / "TSUM_FS-0260829",
        show_date=date(2026, 8, 29),
        provider=ProviderKind.CODEX,
        model="",
        build_mode=BuildMode.PHOTOS,
    )
    return store, project


def test_quick_build_stops_after_captions_and_marks_later_stages_skipped(tmp_path: Path, monkeypatch) -> None:
    store, project = _project(tmp_path)
    engine = PipelineEngine(store)
    monkeypatch.setattr(engine, "_provider", lambda _project: object())
    visited: list[str] = []
    monkeypatch.setattr(engine, "_run_stage", lambda _project, key, _provider: visited.append(key) or f"PASS {key}")

    result = engine.run(project)

    assert result.stopped_at is None
    assert visited == ["prepare", "looks", "credits_map", "map", "structure", "dates", "frames", "images", "captions"]
    rows = store.stage_rows(project.id)
    assert rows["captions"]["status"] == StageStatus.PASSED.value
    for key in ("visual", "review", "final"):
        assert rows[key]["status"] == StageStatus.PENDING.value
        assert '"skipped": "quick_build"' in rows[key]["details_json"]


def test_photo_only_build_skips_credits_and_stops_after_images(tmp_path: Path, monkeypatch) -> None:
    store, project = _photo_project(tmp_path)
    engine = PipelineEngine(store)
    monkeypatch.setattr(engine, "_provider", lambda _project: object())
    visited: list[str] = []
    monkeypatch.setattr(engine, "_run_stage", lambda _project, key, _provider: visited.append(key) or f"PASS {key}")

    result = engine.run(project)

    assert result.stopped_at is None
    assert visited == ["prepare", "looks", "map", "structure", "dates", "frames", "images"]
    rows = store.stage_rows(project.id)
    for key in ("credits_map", "captions", "visual", "review", "final"):
        assert rows[key]["status"] == StageStatus.PENDING.value
        assert '"skipped": "photos_only"' in rows[key]["details_json"]
    assert "только фотографии" in result.message.casefold()


def test_quick_build_does_not_start_visual_stages_again(tmp_path, monkeypatch) -> None:
    store, project = _project(tmp_path)
    engine = PipelineEngine(store)
    monkeypatch.setattr(engine, "_provider", lambda _project: object())
    for key in ("prepare", "looks", "credits_map", "map", "structure", "dates", "frames", "images", "captions"):
        store.set_stage(project.id, key, StageStatus.PASSED)

    result = engine.run(project)

    assert result.stopped_at is None
    assert "визуальная проверка" in result.message.casefold()


def test_project_build_mode_is_persistent_and_can_be_switched(tmp_path: Path) -> None:
    store, project = _project(tmp_path)
    assert project.build_mode == BuildMode.QUICK

    store.set_build_mode(project.id, BuildMode.FULL)

    assert store.get_project(project.id).build_mode == BuildMode.FULL


@pytest.mark.parametrize("codex", [True, False])
def test_visual_transport_failure_never_confirms_a_quick_guess(tmp_path, monkeypatch, codex):
    store, project = _project(tmp_path)
    registry = project.project_dir / "control/work/look-register.tsv"
    registry.parent.mkdir(parents=True)
    registry.touch()
    engine = PipelineEngine(store)
    scripts = []
    monkeypatch.setattr(engine.controller, "script", lambda *args, **kwargs: scripts.append(args))
    monkeypatch.setattr(engine, "_apply_credit_overrides", lambda _: [])
    attempts = []

    def broken_check(*args, **kwargs):
        attempts.append(True)
        raise ProviderError("transport interrupted before visual confirmation")

    monkeypatch.setattr(engine, "_confirm_autonomous_credit_proofs", broken_check)
    monkeypatch.setattr(engine, "_confirm_local_credit_proofs", broken_check)
    monkeypatch.setattr(engine, "_quick_caption_map_fallback", lambda _: pytest.fail("A failed model must not confirm a heuristic guess"))
    provider = CodexProvider("gpt-6.1-sol") if codex else object()
    if codex:
        monkeypatch.setattr(provider, "close", lambda: None)
    with pytest.raises(ProviderError, match="transport interrupted"):
        engine._credits_map(project, provider)
    assert len(attempts) == 2
    assert not any("quick-fallback" in args for args in scripts)
    assert not any(args[0] == "build_verified_caption_data.py" for args in scripts)
