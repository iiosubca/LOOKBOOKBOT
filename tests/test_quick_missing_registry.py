from __future__ import annotations

import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

sys.path.insert(0, str(Path(__file__).parents[1] / "automation-engine" / "lookbook-layout" / "scripts"))

from build_reference_registry import _quick_missing_looks
from build_targeted_rematch_proofs import _reference_photo


def test_quick_mode_removes_an_uncertain_look_and_reassigns_remaining_hires() -> None:
    files = [Path(f"hire-{index}.jpg") for index in range(4)]
    cost = np.asarray(
        [
            [0.01, 0.80, 0.80, 0.80],
            [0.80, 0.02, 0.80, 0.80],
            [0.80, 0.80, 0.100, 0.101],
            [0.80, 0.80, 0.101, 0.100],
        ],
        dtype=np.float64,
    )

    assignment, missing, diagnostics, problems = _quick_missing_looks(cost, files, 4)

    assert missing == {2}
    assert assignment == {0: 0, 1: 1}
    assert [item["target"] for item in diagnostics] == [0, 1]
    assert len(problems) == 2
    assert all("LOOK_002" in item for item in problems)


def test_quick_mode_keeps_all_decisive_looks() -> None:
    files = [Path(f"hire-{index}.jpg") for index in range(4)]
    cost = np.asarray(
        [
            [0.01, 0.80, 0.80, 0.80],
            [0.80, 0.02, 0.80, 0.80],
            [0.80, 0.80, 0.03, 0.80],
            [0.80, 0.80, 0.80, 0.04],
        ],
        dtype=np.float64,
    )

    assignment, missing, diagnostics, problems = _quick_missing_looks(cost, files, 4)

    assert missing == set()
    assert assignment == {0: 0, 1: 1, 2: 2, 3: 3}
    assert len(diagnostics) == 4
    assert problems == []


def test_quick_mode_keeps_a_pair_when_one_side_has_a_narrow_individual_margin() -> None:
    files = [Path(f"hire-{index}.jpg") for index in range(4)]
    cost = np.asarray(
        [
            [0.100, 0.101, 0.80, 0.80],
            [0.80, 0.010, 0.80, 0.80],
            [0.80, 0.80, 0.020, 0.80],
            [0.80, 0.80, 0.80, 0.030],
        ],
        dtype=np.float64,
    )

    assignment, missing, _diagnostics, problems = _quick_missing_looks(cost, files, 4)

    assert assignment == {0: 0, 1: 1, 2: 2, 3: 3}
    assert missing == set()
    assert problems == []


def test_targeted_rematch_uses_extracted_reference_for_a_quick_placeholder(tmp_path: Path) -> None:
    hires = tmp_path / "hires"
    fallback = tmp_path / "missing-reference"
    hires.mkdir()
    fallback.mkdir()
    reference = fallback / "LOOK_025_LEFT.jpg"
    reference.write_bytes(b"reference-photo")

    selected = _reference_photo(
        {"look_id": "LOOK_025", "left_filename": "__lbb_missing_LOOK_025_LEFT.jpg"},
        "left",
        hires,
        fallback,
    )

    assert selected == reference
