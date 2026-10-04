from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

sys.path.insert(0, str(Path(__file__).parents[1] / "automation-engine" / "lookbook-layout" / "scripts"))

import auto_caption_map
from auto_caption_map import appearance_signature, pair_distance


def _feature(colour: tuple[float, float, float]) -> tuple[object, ...]:
    rgb = np.full((4, 4, 3), colour, dtype=np.float32)
    edge = np.zeros((4, 4), dtype=np.float32)
    foreground = np.ones((4, 4), dtype=np.float32)
    grid = np.zeros(48, dtype=np.float32)
    signature = np.zeros(80, dtype=np.float32)
    return rgb, edge, foreground, grid, signature


def test_pair_score_uses_the_second_pdf_view_as_context() -> None:
    card = _feature((0.22, 0.22, 0.22))
    left = _feature((0.22, 0.22, 0.22))
    matching_right = _feature((0.22, 0.22, 0.22))
    unrelated_right = _feature((0.92, 0.92, 0.92))
    card_appearance = appearance_signature(card)  # type: ignore[arg-type]
    left_appearance = appearance_signature(left)  # type: ignore[arg-type]

    matching_score = pair_distance(
        card,
        left,
        matching_right,
        card_appearance,
        left_appearance,
        appearance_signature(matching_right),  # type: ignore[arg-type]
    )[0]
    unrelated_score = pair_distance(
        card,
        left,
        unrelated_right,
        card_appearance,
        left_appearance,
        appearance_signature(unrelated_right),  # type: ignore[arg-type]
    )[0]

    assert unrelated_score > matching_score


@pytest.mark.parametrize("mode", ["alternative-proofs", "select-alternatives", "confirm-review", "repair"])
def test_quick_placeholder_fallback_is_used_without_a_repeated_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str,
) -> None:
    """Later credit-map modes must see the PDF fallback after a quick build."""
    registry = tmp_path / "control" / "work" / "look-register.tsv"
    registry.parent.mkdir(parents=True)
    with registry.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=auto_caption_map.REGISTRY_FIELDS, delimiter="\t")
        writer.writeheader()
        writer.writerow({
            "look_id": "LOOK_001",
            "left_filename": "__lbb_missing_LOOK_001_LEFT.jpg",
            "right_filename": "__lbb_missing_LOOK_001_RIGHT.jpg",
        })
    index = tmp_path / "control" / "work" / "_mat" / "excel-images" / "index.tsv"
    index.parent.mkdir(parents=True)
    with index.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=auto_caption_map.INDEX_FIELDS, delimiter="\t")
        writer.writeheader()
        writer.writerow({"excel_sheet": "W", "excel_look_number": "1", "excel_image": "card.jpg"})

    observed: list[Path | None] = []

    class ReachedResolver(Exception):
        pass

    def capture_resolver(
        _project: Path, _registry: list[dict[str, str]], _cards: list[dict[str, str]],
        _hires: Path, missing_reference_dir: Path | None,
    ) -> None:
        observed.append(missing_reference_dir)
        raise ReachedResolver

    monkeypatch.setattr(auto_caption_map, "resolve", capture_resolver)
    monkeypatch.setattr(sys, "argv", ["auto_caption_map.py", str(tmp_path), "--mode", mode])
    with pytest.raises(ReachedResolver):
        auto_caption_map.main()
    assert observed == [tmp_path / "control" / "work" / "missing-photo-reference"]


@pytest.mark.parametrize("mode", ["quick-seed", "quick-fallback"])
def test_a_similarity_proposal_is_never_a_visual_confirmation(monkeypatch, tmp_path, mode):
    registry = tmp_path / "control/work/look-register.tsv"
    registry.parent.mkdir(parents=True)
    auto_caption_map.write_rows(registry, auto_caption_map.REGISTRY_FIELDS, [{
        "look_id": "LOOK_001", "left_filename": "left.jpg", "right_filename": "right.jpg",
    }])
    index = tmp_path / "control/work/_mat/excel-images/index.tsv"
    auto_caption_map.write_rows(index, auto_caption_map.INDEX_FIELDS, [{
        "excel_sheet": "W", "excel_look_number": "1", "excel_image": "card.jpg",
    }])
    monkeypatch.setattr(auto_caption_map, "resolve", lambda *_: ([0], [], []))
    monkeypatch.setattr(sys, "argv", ["auto_caption_map.py", str(tmp_path), "--mode", mode])
    auto_caption_map.main()
    rows = auto_caption_map.read_rows(tmp_path / "control/work/caption-map.tsv", auto_caption_map.MAP_FIELDS)
    assert rows[0]["visual_status"] == "PENDING"


@pytest.mark.parametrize("alternative", [False, True])
def test_quick_resume_keeps_43_confirmations_and_only_9_pending(monkeypatch, tmp_path, alternative):
    registry = []
    cards = []
    mapping = []
    for number in range(1, 53):
        look = f"LOOK_{number:03}"
        registry.append({"look_id": look, "left_filename": f"{look}-left.jpg", "right_filename": f"{look}-right.jpg"})
        cards.append({"excel_sheet": "W", "excel_look_number": str(number), "excel_image": f"{look}-excel.jpg"})
        mapping.append({**registry[-1], **cards[-1], "evidence_file": f"control/work/mapping-evidence/{look}.jpg",
                        "visual_status": "CONFIRMED" if number <= 43 else "PENDING"})
    auto_caption_map.write_rows(tmp_path / "control/work/look-register.tsv", auto_caption_map.REGISTRY_FIELDS, registry)
    auto_caption_map.write_rows(tmp_path / "control/work/_mat/excel-images/index.tsv", auto_caption_map.INDEX_FIELDS, cards)
    if alternative:
        for index, card_index in [(0, 1), (1, 0)]:
            mapping[index].update(cards[card_index])
            auto_caption_map.write_json(auto_caption_map.selection_path(tmp_path, mapping[index]["look_id"]), mapping[index])
    target = tmp_path / "control/work/caption-map.tsv"
    auto_caption_map.write_rows(target, auto_caption_map.MAP_FIELDS, mapping)
    before = target.read_bytes()
    monkeypatch.setattr(auto_caption_map, "resolve", lambda *_: (list(range(52)), [], []))
    monkeypatch.setattr(sys, "argv", ["auto_caption_map.py", str(tmp_path), "--mode", "quick-autonomous"])
    auto_caption_map.main()
    assert target.read_bytes() == before
    assert sum(x["visual_status"] == "CONFIRMED" for x in auto_caption_map.read_rows(target, auto_caption_map.MAP_FIELDS)) == 43
