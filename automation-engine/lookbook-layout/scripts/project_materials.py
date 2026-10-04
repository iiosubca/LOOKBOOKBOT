"""Resolve project-local photographs without falling back to stale duplicates."""
from pathlib import Path

HIRES_RELATIVE = Path("_MAT/hires")
LEGACY_HIRES_RELATIVE = Path("control/work/_mat/hires")


def project_hires(project: Path, requested: str | Path = HIRES_RELATIVE) -> Path:
    root = project.resolve()
    candidate = Path(requested)
    candidate = (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    candidate.relative_to(root)
    canonical = (root / HIRES_RELATIVE).resolve()
    canonical.relative_to(root)
    legacy = (root / LEGACY_HIRES_RELATIVE).resolve()
    if candidate in (canonical, legacy):
        # Folder-level compatibility only. Never fill a missing/removed photo
        # from the legacy duplicate when the authoritative folder exists.
        return canonical if canonical.is_dir() else legacy
    return candidate


def is_project_hires(project: Path, path: Path) -> bool:
    root = project.resolve()
    canonical = (root / HIRES_RELATIVE).resolve()
    try:
        canonical.relative_to(root)
    except ValueError:
        return False
    return path.resolve() == canonical
