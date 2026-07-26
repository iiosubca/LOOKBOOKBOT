from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from lookbookbot.domain import ProviderKind
from lookbookbot.pipeline import PipelineEngine, _path_batches
from lookbookbot.providers import CodexProvider
from lookbookbot.state import StateStore


def _project(store: StateStore, tmp_path: Path):
    return store.save_project(
        name="demo",
        source_dir=tmp_path / "SOURCES",
        output_root=tmp_path,
        project_dir=tmp_path / "demo",
        show_date=date(2026, 8, 7),
        provider=ProviderKind.CODEX,
        model="",
    )


def test_visual_proof_batches_are_fixed_and_non_overlapping(tmp_path: Path) -> None:
    paths = [tmp_path / f"LOOK_{index:03}.jpg" for index in range(1, 12)]

    batches = _path_batches(paths, size=5)

    assert [[path.stem for path in batch] for batch in batches] == [
        [f"LOOK_{index:03}" for index in range(1, 6)],
        [f"LOOK_{index:03}" for index in range(6, 11)],
        ["LOOK_011"],
    ]
    assert _path_batches([], size=5) == []


def test_parallel_codex_visual_records_once_after_all_batches(tmp_path: Path, monkeypatch) -> None:
    store = StateStore(tmp_path / "state.db")
    project = _project(store, tmp_path)
    engine = PipelineEngine(store)
    proofs: list[Path] = []
    for index in range(1, 7):
        proof = project.project_dir / "control" / "visual" / "proof" / "pairs" / f"LOOK_{index:03}.jpg"
        proof.parent.mkdir(parents=True, exist_ok=True)
        proof.write_bytes(b"proof")
        proofs.append(proof)
    confirmations = project.project_dir / "control" / "visual" / "confirmations"
    seen: list[list[str]] = []
    records: list[str] = []

    monkeypatch.setattr(engine, "_prepare_visual_proof", lambda _project: proofs)

    def fake_confirm(_provider, _root: Path, batch: list[Path]) -> None:
        seen.append([proof.stem for proof in batch])
        confirmations.mkdir(parents=True, exist_ok=True)
        for proof in batch:
            (confirmations / f"{proof.stem}.json").write_text(json.dumps({"look_id": proof.stem}), encoding="utf-8")

    monkeypatch.setattr(engine, "_confirm_codex_visual_batch", fake_confirm)
    monkeypatch.setattr(engine.controller, "gate", lambda action, *_args, **_kwargs: records.append(action))

    engine._run_parallel_codex_visual(project, CodexProvider())

    assert sorted(look for batch in seen for look in batch) == [proof.stem for proof in proofs]
    assert all(len(batch) <= 5 for batch in seen)
    assert records == ["record-visual"]
