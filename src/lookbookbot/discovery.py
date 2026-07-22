from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import ToolPaths
from .domain import SourceBundle


class SourceDiscoveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class DiscoveryReport:
    bundle: SourceBundle | None
    messages: tuple[str, ...]


def _unique_files(roots: list[Path], patterns: tuple[str, ...]) -> list[Path]:
    found: dict[str, Path] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for pattern in patterns:
            for item in root.glob(pattern):
                if item.is_file() and not item.name.startswith("~$"):
                    found[str(item.resolve()).casefold()] = item.resolve()
    return sorted(found.values(), key=lambda p: p.name.casefold())


def _candidate_roots(selected: Path) -> list[Path]:
    roots = [selected]
    for child in ("_mat", "SOURCES", "SOURCES/_mat"):
        roots.append(selected / child)
    if selected.name.casefold() == "_mat":
        roots.append(selected.parent)
    if selected.name.casefold() == "sources":
        roots.append(selected / "_mat")
    return [path.resolve() for path in roots]


def _select_reference(files: list[Path]) -> Path:
    usable = [path for path in files if not any(word in path.stem.casefold() for word in ("review", "10mb", "20mb", "40mb"))]
    if len(usable) != 1:
        names = ", ".join(path.name for path in usable) or "не найдено"
        raise SourceDiscoveryError(f"Нужен ровно один PDF-референс. Найдено: {names}")
    return usable[0]


def _select_workbook(files: list[Path]) -> Path:
    if len(files) != 1:
        names = ", ".join(path.name for path in files) or "не найдено"
        raise SourceDiscoveryError(f"Нужен ровно один Excel-каталог. Найдено: {names}")
    return files[0]


def _find_hires(roots: list[Path]) -> Path:
    candidates: list[Path] = []
    for root in roots:
        if root.is_dir() and root.name.casefold() == "hires":
            candidates.append(root)
        candidate = root / "hires"
        if candidate.is_dir():
            candidates.append(candidate)
    unique = {str(path.resolve()).casefold(): path.resolve() for path in candidates}
    populated = [path for path in unique.values() if any(path.glob("*.jpg")) or any(path.glob("*.jpeg"))]
    if len(populated) != 1:
        names = ", ".join(str(path) for path in populated) or "не найдено"
        raise SourceDiscoveryError(f"Нужна ровно одна папка hires с JPG. Найдено: {names}")
    return populated[0]


def discover_sources(selected: Path, tools: ToolPaths | None = None) -> DiscoveryReport:
    tools = tools or ToolPaths.defaults()
    selected = selected.expanduser().resolve()
    if not selected.is_dir():
        return DiscoveryReport(None, (f"Папка не существует: {selected}",))
    roots = _candidate_roots(selected)
    try:
        reference = _select_reference(_unique_files(roots, ("*.pdf", "*.PDF")))
        workbook = _select_workbook(_unique_files(roots, ("*.xlsx", "*.XLSX")))
        hires = _find_hires(roots)
        templates = _unique_files(roots, ("*_AUTOMATION.indd", "*_AUTOMATION.INDD"))
        template = templates[0] if len(templates) == 1 else tools.template
        if not template.is_file():
            raise SourceDiscoveryError(f"Не найден подготовленный automation-master: {template}")
    except SourceDiscoveryError as error:
        return DiscoveryReport(None, (str(error),))
    messages = (
        f"PDF: {reference.name}",
        f"Excel: {workbook.name}",
        f"Hires: {hires} ({sum(1 for _ in hires.glob('*.jpg')) + sum(1 for _ in hires.glob('*.jpeg'))} JPG)",
        f"Шаблон: {template.name}",
    )
    return DiscoveryReport(SourceBundle(selected, reference, workbook, hires, template), messages)


def infer_output_root(selected: Path) -> Path:
    selected = selected.resolve()
    if selected.name.casefold() == "_mat" and selected.parent.name.casefold() == "sources":
        return selected.parent.parent
    if selected.name.casefold() == "sources":
        return selected.parent
    return selected
