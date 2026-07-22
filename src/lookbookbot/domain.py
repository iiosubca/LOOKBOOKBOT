from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path


class StageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    REVIEW = "review"
    FAILED = "failed"
    BLOCKED = "blocked"


class ProviderKind(StrEnum):
    CODEX = "codex"
    OLLAMA = "ollama"
    LLAMACPP = "llamacpp"


@dataclass(frozen=True)
class StageSpec:
    key: str
    title: str
    description: str
    gate: str | None = None
    visual: bool = False
    human_review: bool = False


STAGES: tuple[StageSpec, ...] = (
    StageSpec("prepare", "Подготовка проекта", "Проверка исходников, создание чистой папки проекта и control/work."),
    StageSpec("looks", "Список луков", "PDF-референс определяет порядок и пары: полный рост слева, клоузап справа.", visual=True, human_review=True),
    StageSpec("credits_map", "Список кредитов", "Визуальное сопоставление PDF-луков с карточками Excel; лишние карточки допустимы.", visual=True, human_review=True),
    StageSpec("map", "Контроль карты", "Инициализация контроллера, проверка PDF-порядка и блокировка карты.", gate="map", visual=True),
    StageSpec("structure", "Структура разворотов", "Дублирование только готового рабочего разворота перед финальной обложкой.", gate="structure"),
    StageSpec("dates", "Дата", "Изменение даты показа и юридической даты в существующих фреймах.", gate="dates"),
    StageSpec("frames", "Привязка фреймов", "Привязка существующих контейнеров к LOOK_### без создания новых фреймов.", gate="frames"),
    StageSpec("images", "Фотографии", "Замена содержимого существующих контейнеров пакетами по четыре лука.", gate="images"),
    StageSpec("captions", "Кредиты", "Заполнение существующих фреймов с точным стилем CREDiTs и контролем переполнения.", gate="captions"),
    StageSpec("visual", "Визуальная проверка", "Порядок фото, ссылки, кредиты, безопасная зона и отсутствие пересечений.", gate="visual", visual=True),
    StageSpec("review", "PDF на проверку", "Нативный аудит, экспорт и проверка review-PDF.", gate="pdf"),
    StageSpec("final", "Финальные PDF", "После согласования: 120/220/300 ppi и версии Gender M/W 300 ppi.", human_review=True),
)

STAGE_BY_KEY = {stage.key: stage for stage in STAGES}


RUSSIAN_MONTHS = (
    "",
    "ЯНВАРЯ",
    "ФЕВРАЛЯ",
    "МАРТА",
    "АПРЕЛЯ",
    "МАЯ",
    "ИЮНЯ",
    "ИЮЛЯ",
    "АВГУСТА",
    "СЕНТЯБРЯ",
    "ОКТЯБРЯ",
    "НОЯБРЯ",
    "ДЕКАБРЯ",
)


def project_code(show_date: date) -> str:
    return f"TSUM_FS-0{show_date:%y%m%d}"


def master_filename(show_date: date, revision: int = 1) -> str:
    return f"{project_code(show_date)}_LB_WA_{revision:02}.indd"


def review_pdf_filename(show_date: date, revision: int = 1) -> str:
    return f"{project_code(show_date)}_LB_WA_{revision:02}_review.pdf"


def visible_date_text(show_date: date) -> str:
    return f"{show_date.day} {RUSSIAN_MONTHS[show_date.month]}"


@dataclass(frozen=True)
class SourceBundle:
    selected_root: Path
    reference_pdf: Path
    workbook: Path
    hires: Path
    template: Path


@dataclass(frozen=True)
class ProjectRecord:
    id: str
    name: str
    source_dir: Path
    output_root: Path
    project_dir: Path
    show_date: date
    provider: ProviderKind
    model: str

