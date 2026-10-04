import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "automation-engine/lookbook-layout/scripts"))
from project_materials import project_hires
from lookbook_gate import state_artifact, require_work_path, GateError
from build_reference_registry import require_in_work


def test_all_consumers_prefer_canonical_even_for_legacy_state(tmp_path):
    legacy = tmp_path / "control/work/_mat/hires"
    legacy.mkdir(parents=True)
    (legacy / "photo.jpg").write_bytes(b"old")
    canonical = tmp_path / "_MAT/hires"
    canonical.mkdir(parents=True)
    (canonical / "photo.jpg").write_bytes(b"manual")
    for requested in ("_MAT/hires", "control/work/_mat/hires", legacy):
        assert project_hires(tmp_path, requested) == canonical
    assert state_artifact(tmp_path, {"hires": str(legacy)}, "hires") == canonical
    require_work_path(tmp_path, canonical, "Hires folder")
    require_in_work(tmp_path, canonical, "Hires folder")
    with pytest.raises(GateError):
        require_work_path(tmp_path, canonical, "Registry")
    (canonical / "photo.jpg").unlink()
    assert not (project_hires(tmp_path) / "photo.jpg").exists()


def test_legacy_compatibility_and_explicit_fixture_paths(tmp_path):
    legacy = tmp_path / "control/work/_mat/hires"
    legacy.mkdir(parents=True)
    assert project_hires(tmp_path) == legacy
    fixture = tmp_path / "control/work/scratch/fixture-candidates"
    fixture.mkdir(parents=True)
    assert project_hires(tmp_path, fixture) == fixture
    with pytest.raises(ValueError):
        project_hires(tmp_path, "../external")
