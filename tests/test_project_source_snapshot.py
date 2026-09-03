from __future__ import annotations

from datetime import date
from pathlib import Path

from lookbookbot.domain import ProviderKind
from lookbookbot.pipeline import PipelineEngine
from lookbookbot.state import StateStore


def _project(store: StateStore, tmp_path: Path):
    return store.save_project(
        name="source-snapshot",
        source_dir=tmp_path / "SOURCES",
        output_root=tmp_path,
        project_dir=tmp_path / "source-snapshot",
        show_date=date(2026, 8, 7),
        provider=ProviderKind.CODEX,
        model="",
    )


def _make_sources(source: Path) -> None:
    hires = source / "hires"
    hires.mkdir(parents=True)
    (source / "reference.pdf").write_bytes(b"reference-v1")
    (source / "catalog.xlsx").write_bytes(b"catalog-v1")
    (source / "TSUM_AUTOMATION.indd").write_bytes(b"template-v1")
    (hires / "look-001.jpg").write_bytes(b"hires-v1")


def test_prepare_freezes_project_local_sources_and_never_overwrites_them(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "SOURCES"
    _make_sources(source)
    store = StateStore(tmp_path / "state.db")
    project = _project(store, tmp_path)
    engine = PipelineEngine(store)
    monkeypatch.setattr(type(engine.tools), "validate", lambda _self: ())

    def create_work_area(_script: str, root: Path, **_kwargs) -> None:
        (root / "control" / "work" / "_mat" / "hires").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(engine.controller, "script", create_work_area)

    first = engine._prepare(project)
    controlled = project.project_dir / "control" / "work" / "_mat"
    frozen = {
        "reference": (controlled / "reference.pdf").read_bytes(),
        "catalog": (controlled / "caption-source.xlsx").read_bytes(),
        "template": (controlled / "automation-template.indd").read_bytes(),
        "hires": (controlled / "hires" / "look-001.jpg").read_bytes(),
    }
    assert "локальными копиями" in first
    visible = project.project_dir / "_MAT"
    assert (visible / "reference.pdf").read_bytes() == frozen["reference"]
    assert (visible / "catalog.xlsx").read_bytes() == frozen["catalog"]
    assert (visible / "TSUM_AUTOMATION.indd").read_bytes() == frozen["template"]
    assert (visible / "hires" / "look-001.jpg").read_bytes() == frozen["hires"]

    (source / "reference.pdf").write_bytes(b"reference-v2")
    (source / "catalog.xlsx").write_bytes(b"catalog-v2")
    (source / "TSUM_AUTOMATION.indd").write_bytes(b"template-v2")
    (source / "hires" / "look-001.jpg").write_bytes(b"hires-v2")

    resumed = engine._prepare(project)

    assert "SOURCES не читается" in resumed
    assert (controlled / "reference.pdf").read_bytes() == frozen["reference"]
    assert (controlled / "caption-source.xlsx").read_bytes() == frozen["catalog"]
    assert (controlled / "automation-template.indd").read_bytes() == frozen["template"]
    assert (controlled / "hires" / "look-001.jpg").read_bytes() == frozen["hires"]
