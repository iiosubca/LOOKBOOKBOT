from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from lookbookbot.domain import ProviderKind
from lookbookbot.pipeline import PipelineEngine, _path_batches
from lookbookbot.providers import CodexProvider, VisionDecision
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

    def fake_confirm(_provider, _root: Path, batch: list[Path]):
        seen.append([proof.stem for proof in batch])
        return {
            proof.stem: VisionDecision(
                accepted=True,
                note="Кадры и кредиты видимы, модель не пересекает текст в safe area.",
            )
            for proof in batch
        }

    def fake_gate(action: str, _root: Path, *args: str, **_kwargs) -> None:
        records.append(action)
        if action == "confirm-visual-look":
            look_id = args[args.index("--look") + 1]
            confirmations.mkdir(parents=True, exist_ok=True)
            (confirmations / f"{look_id}.json").write_text(json.dumps({"look_id": look_id}), encoding="utf-8")

    monkeypatch.setattr(engine, "_confirm_codex_visual_batch", fake_confirm)
    monkeypatch.setattr(engine.controller, "gate", fake_gate)

    engine._run_parallel_codex_visual(project, CodexProvider())

    assert sorted(look for batch in seen for look in batch) == [proof.stem for proof in proofs]
    assert all(len(batch) <= 5 for batch in seen)
    assert records == ["confirm-visual-look"] * 6 + ["record-visual"]


def test_parallel_codex_visual_retries_only_unconfirmed_proof(tmp_path: Path, monkeypatch) -> None:
    store = StateStore(tmp_path / "state.db")
    project = _project(store, tmp_path)
    engine = PipelineEngine(store)
    proof = project.project_dir / "control" / "visual" / "proof" / "pairs" / "LOOK_041.jpg"
    proof.parent.mkdir(parents=True, exist_ok=True)
    proof.write_bytes(b"proof")
    confirmations = project.project_dir / "control" / "visual" / "confirmations"
    attempts: list[str] = []
    records: list[str] = []

    monkeypatch.setattr(engine, "_prepare_visual_proof", lambda _project: [proof])

    def fake_confirm(_provider, _root: Path, batch: list[Path]):
        attempts.append(batch[0].stem)
        accepted = len(attempts) == 2
        return {
            "LOOK_041": VisionDecision(
                accepted=accepted,
                note="Кадры корректны, кредиты читаемы и остаются в безопасной зоне страницы.",
            )
        }

    def fake_gate(action: str, _root: Path, *args: str, **_kwargs) -> None:
        records.append(action)
        if action == "confirm-visual-look":
            confirmations.mkdir(parents=True, exist_ok=True)
            (confirmations / "LOOK_041.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(engine, "_confirm_codex_visual_batch", fake_confirm)
    monkeypatch.setattr(engine.controller, "gate", fake_gate)

    engine._run_parallel_codex_visual(project, CodexProvider())

    assert attempts == ["LOOK_041", "LOOK_041"]
    assert records == ["confirm-visual-look", "record-visual"]


def test_codex_visual_batch_receives_explicit_proof_attachments(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    engine = PipelineEngine(store)
    proofs = [tmp_path / "LOOK_041.jpg", tmp_path / "LOOK_042.jpg"]
    for proof in proofs:
        proof.write_bytes(b"proof")
    received: list[Path] = []

    class FakeProvider:
        def run_readonly_agent(self, _prompt: str, _root: Path, **kwargs) -> str:
            received.extend(kwargs["images"])
            return json.dumps({
                "decisions": [
                    {
                        "look_id": proof.stem,
                        "accepted": True,
                        "note": "Кадры корректны, кредиты читаемы и остаются в безопасной зоне страницы.",
                    }
                    for proof in proofs
                ],
            })

    decisions = engine._confirm_codex_visual_batch(FakeProvider(), tmp_path, proofs)  # type: ignore[arg-type]

    assert received == proofs
    assert all(decision.accepted for decision in decisions.values())


def test_partial_visual_confirmation_reuses_current_proof_without_native_work(tmp_path: Path, monkeypatch) -> None:
    store = StateStore(tmp_path / "state.db")
    project = _project(store, tmp_path)
    engine = PipelineEngine(store)
    proof_root = project.project_dir / "control" / "visual" / "proof" / "pairs" / "session-current"
    proof_root.mkdir(parents=True, exist_ok=True)
    proofs = [proof_root / "LOOK_001.jpg", proof_root / "LOOK_002.jpg"]
    for proof in proofs:
        proof.write_bytes(b"proof")
    confirmations = project.project_dir / "control" / "visual" / "confirmations"
    confirmations.mkdir(parents=True)
    (confirmations / "LOOK_001.json").write_text("{}", encoding="utf-8")

    def unexpected_native_call(*_args, **_kwargs):
        raise AssertionError("Partially confirmed proof must not re-arm or re-render InDesign.")

    monkeypatch.setattr(engine.controller, "gate", unexpected_native_call)

    resumed = engine._prepare_visual_proof(project)

    assert resumed == proofs
