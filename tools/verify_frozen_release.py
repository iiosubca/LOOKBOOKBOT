"""Reject stale packaged app bytecode or missing/stale engine source files."""
from __future__ import annotations

import argparse
from pathlib import Path

from PyInstaller.archive.readers import CArchiveReader


def verify(executable: Path, repo: Path) -> tuple[int, int]:
    archive = CArchiveReader(executable)
    modules = archive.open_embedded_archive("PYZ.pyz")
    checked_modules = 0
    for source in sorted((repo / "src/lookbookbot").rglob("*.py")):
        parts = list(source.relative_to(repo / "src").with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        name = ".".join(parts)
        if name not in modules.toc:
            raise RuntimeError(f"Packaged module missing: {name}")
        actual = modules.extract(name)
        expected = compile(source.read_bytes(), actual.co_filename, "exec", dont_inherit=True, optimize=0)
        if actual != expected:
            raise RuntimeError(f"Packaged module differs from current source: {name}; build with --clean.")
        checked_modules += 1

    names = {name.replace("\\", "/"): name for name in archive.toc}
    prefix = "automation-engine/core/scripts/"
    checked_scripts = 0
    for source in sorted((repo / "automation-engine/lookbook-layout/scripts").rglob("*")):
        if not source.is_file() or "__pycache__" in source.parts or source.suffix in {".pyc", ".pyo"}:
            continue
        target = prefix + source.relative_to(repo / "automation-engine/lookbook-layout/scripts").as_posix()
        if target not in names:
            raise RuntimeError(f"Packaged engine source missing: {target}")
        if archive.extract(names[target]) != source.read_bytes():
            raise RuntimeError(f"Packaged engine source differs: {target}")
        checked_scripts += 1
    if any(name.startswith(prefix) and name.endswith((".pyc", ".pyo")) for name in names):
        raise RuntimeError("Packaged engine contains development bytecode instead of source-only resources.")
    return checked_modules, checked_scripts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    modules, scripts = verify(args.executable.resolve(), args.repo.resolve())
    print(f"PASS frozen release: {modules} app modules and {scripts} engine files match current source.")


if __name__ == "__main__":
    main()
