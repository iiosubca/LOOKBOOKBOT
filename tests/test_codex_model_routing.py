from __future__ import annotations

from datetime import date
from pathlib import Path

from lookbookbot.domain import ProviderKind
from lookbookbot.pipeline import PipelineEngine
from lookbookbot.providers import CODEX_ESCALATION_MODEL, CODEX_STANDARD_MODEL, CodexProvider
from lookbookbot.state import StateStore


def _project(store: StateStore, tmp_path: Path, model: str):
    return store.save_project(
        name="routing",
        source_dir=tmp_path / "SOURCES",
        output_root=tmp_path,
        project_dir=tmp_path / "routing",
        show_date=date(2026, 9, 12),
        provider=ProviderKind.CODEX,
        model=model,
    )


def test_blank_uses_terra_but_an_explicit_astra_selection_is_preserved(tmp_path: Path, monkeypatch) -> None:
    store = StateStore(tmp_path / "state.db")
    engine = PipelineEngine(store)
    models: list[str] = []
    monkeypatch.setattr(
        "lookbookbot.pipeline.make_provider",
        lambda _kind, model, **_kwargs: models.append(model) or object(),
    )

    engine._provider(_project(store, tmp_path, ""))
    engine._provider(_project(store, tmp_path, "gpt-6-astra"))

    assert models == [CODEX_STANDARD_MODEL, "gpt-6-astra"]


def test_only_a_bounded_problem_is_routed_to_sol(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    engine = PipelineEngine(store)
    binary = tmp_path / "codex.exe"
    binary.touch()
    terra = CodexProvider(CODEX_STANDARD_MODEL, binary=binary)

    sol = engine._sol_for_problem(terra, "test")

    assert isinstance(sol, CodexProvider)
    assert sol.model == CODEX_ESCALATION_MODEL
    assert terra.model == CODEX_STANDARD_MODEL
    assert engine._sol_for_problem(sol, "test") is sol
    explicitly_selected = CodexProvider("gpt-6-luna", binary=binary, reasoning_effort="high")
    assert engine._sol_for_problem(explicitly_selected, "test") is explicitly_selected


def test_selected_reasoning_effort_persists_into_codex_worker(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    project = store.save_project(
        name="routing", source_dir=tmp_path / "SOURCES", output_root=tmp_path,
        project_dir=tmp_path / "routing", show_date=date(2026, 9, 23),
        provider=ProviderKind.CODEX, model="gpt-6-sol", reasoning_effort="xhigh",
    )
    engine = PipelineEngine(store)
    provider = engine._provider(store.get_project(project.id))

    assert isinstance(provider, CodexProvider)
    assert provider.model == "gpt-6-sol"
    assert provider.reasoning_effort == "xhigh"

    store.set_project_provider(project.id, ProviderKind.CODEX, "gpt-6-luna", "low")
    updated = engine._provider(store.get_project(project.id))
    assert isinstance(updated, CodexProvider)
    assert (updated.model, updated.reasoning_effort) == ("gpt-6-luna", "low")
