from __future__ import annotations

import csv
import json
import re
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .ai_policy import read_only_vision_prompt, targeted_credit_rematch_prompt
from .config import ToolPaths
from .controller import CommandError, CommandRunner, LookbookController, evidence_passed, final_outputs_passed, read_json
from .discovery import discover_sources
from .domain import (
    ProjectRecord,
    ProviderKind,
    STAGES,
    StageStatus,
    master_filename,
    review_pdf_filename,
    visible_date_text,
)
from .providers import CodexProvider, ModelProvider, ProviderError, VisionDecision, make_provider
from .secrets import get_google_api_key
from .state import StateStore
from .visual_audit import visual_audit_blocker_message


class PipelineError(RuntimeError):
    pass


@dataclass(frozen=True)
class PipelineResult:
    completed: tuple[str, ...]
    stopped_at: str | None
    message: str


class PipelineEngine:
    def __init__(
        self,
        store: StateStore,
        *,
        tools: ToolPaths | None = None,
        log: Callable[[str], None] | None = None,
        progress: Callable[[str, StageStatus, str], None] | None = None,
    ) -> None:
        self.store = store
        self.tools = tools or ToolPaths.defaults()
        self.log = log or (lambda _text: None)
        self.progress = progress or (lambda _key, _status, _message: None)
        self.runner = CommandRunner(self.log)
        self.controller = LookbookController(self.tools, self.runner)

    def run(
        self,
        project: ProjectRecord,
        start_key: str | None = None,
        *,
        continue_after: bool = True,
        stop_after: str | None = None,
    ) -> PipelineResult:
        keys = [stage.key for stage in STAGES]
        start_key = start_key or self.store.first_incomplete_stage(project.id)
        if start_key not in keys:
            raise PipelineError(f"Неизвестный этап: {start_key}")
        self._bind_copied_revision_structure(project, start_key)
        provider = self._provider(project)
        completed: list[str] = []
        for stage in STAGES[keys.index(start_key) :]:
            current = self.store.stage_rows(project.id).get(stage.key, {})
            if current.get("status") == StageStatus.PASSED.value:
                completed.append(stage.key)
                if continue_after:
                    continue
                break
            self.store.set_stage(project.id, stage.key, StageStatus.RUNNING)
            self.progress(stage.key, StageStatus.RUNNING, stage.title)
            run_id = self.store.start_run(project.id, stage.key)
            try:
                message = self._run_stage(project, stage.key, provider)
            except (PipelineError, CommandError, ProviderError, OSError, ValueError) as error:
                message = str(error)
                status = StageStatus.REVIEW if isinstance(error, ReviewRequired) else StageStatus.FAILED
                self.store.set_stage(project.id, stage.key, status, error=message)
                self.store.finish_run(run_id, status, message)
                self.progress(stage.key, status, message)
                return PipelineResult(tuple(completed), stage.key, message)
            self.store.set_stage(project.id, stage.key, StageStatus.PASSED, details={"message": message})
            self.store.finish_run(run_id, StageStatus.PASSED, message)
            self.progress(stage.key, StageStatus.PASSED, message)
            completed.append(stage.key)
            if stage.key == stop_after:
                return PipelineResult(tuple(completed), None, message)
            if not continue_after:
                break
        return PipelineResult(tuple(completed), None, "Все доступные этапы завершены.")

    def _bind_copied_revision_structure(self, project: ProjectRecord, start_key: str) -> None:
        """Refresh copied-INDD object IDs before the first captions pass.

        InDesign preserves the visible layout when it copies a reviewed INDD,
        but gives every page item a new internal ID. The native controller
        rebinds its read-only geometry profile before it can alter CREDiTs.
        """
        if start_key != "captions":
            return
        state = read_json(project.project_dir / "control" / "lookbook-state.json")
        try:
            revision = int(state.get("current_revision", 1))
            bound_revision = int(state.get("structure_revision", 0))
        except (TypeError, ValueError) as error:
            raise PipelineError(f"Некорректный маркер структуры ревизии: {error}") from error
        if revision <= 1 or bound_revision == revision:
            return
        self.log(
            f"Ревизия {revision:02}: привязываю профиль существующих фреймов к новой копии INDD перед применением кредитов."
        )
        self.controller.gate("rebind-revision-structure", project.project_dir, timeout=300)

    def begin_caption_revision(self, project: ProjectRecord, audit: Path) -> None:
        """Copy the reviewed master and return the new revision to captions."""
        root = project.project_dir
        try:
            relative_audit = audit.resolve().relative_to(root.resolve())
        except ValueError as error:
            raise PipelineError("Файл правок должен находиться внутри папки проекта.") from error
        payload = read_json(audit)
        values = payload.get("look_ids") if isinstance(payload.get("look_ids"), list) else []
        look_text = ", ".join(str(value) for value in values)
        notes = f"Правки кредитов: {look_text}" if look_text else "Правки кредитов из вкладки Правки"
        self.controller.gate(
            "begin-revision", root,
            "--notes", notes,
            "--reset-from", "captions",
            "--caption-audit", str(relative_audit),
            timeout=300,
        )
        self.store.set_approved(project.id, False)
        self.store.reset_from(project.id, "captions")

    def prepare_caption_revision(self, project: ProjectRecord, audit: Path) -> None:
        """Keep the reviewed source pristine before completing its release/PDF."""
        root = project.project_dir
        try:
            relative_audit = audit.resolve().relative_to(root.resolve())
        except ValueError as error:
            raise PipelineError("Файл правок должен находиться внутри папки проекта.") from error
        self.controller.gate(
            "prepare-caption-revision", root,
            "--caption-audit", str(relative_audit),
            timeout=120,
        )

    def finish_review_before_caption_revision(self, project: ProjectRecord) -> PipelineResult:
        """Finish the unedited master before copying a caption revision.

        A correction draft may be saved while the original review PDF still
        lacks its release evidence.  The draft itself is deliberately outside
        the reviewed TSV, so it is safe to resume only the release/PDF stage
        here.  Resetting the local stage marker prevents an old failed or
        falsely-completed desktop status from skipping the controller proof.
        """
        if evidence_passed(project.project_dir, "pdf"):
            return PipelineResult((), None, "Исходный PDF на проверку уже подтверждён контроллером.")
        self.store.reset_from(project.id, "review")
        return self.run(project, "review", continue_after=False, stop_after="review")

    def _provider(self, project: ProjectRecord) -> ModelProvider:
        return make_provider(
            project.provider,
            project.model,
            ollama_endpoint=self.store.get_setting("ollama_endpoint", "http://127.0.0.1:11434"),
            llama_endpoint=self.store.get_setting("llama_endpoint", "http://127.0.0.1:8080"),
            google_api_key=get_google_api_key(),
            usage_store=self.store,
        )

    def _run_stage(self, project: ProjectRecord, key: str, provider: ModelProvider) -> str:
        handlers = {
            "prepare": self._prepare,
            "looks": self._looks,
            "credits_map": lambda p: self._credits_map(p, provider),
            "map": lambda p: self._map_gate(p, provider),
            "structure": lambda p: self._native(p, "structure"),
            "dates": lambda p: self._native(p, "dates"),
            "frames": lambda p: self._native(p, "frames"),
            "images": lambda p: self._native(p, "images"),
            "captions": lambda p: self._native(p, "captions"),
            "visual": lambda p: self._visual(p, provider),
            "review": self._review,
            "final": self._final,
        }
        return handlers[key](project)

    def _prepare(self, project: ProjectRecord) -> str:
        missing = self.tools.validate()
        if missing:
            raise PipelineError("Не найдены обязательные инструменты:\n" + "\n".join(missing))
        report = discover_sources(project.source_dir, self.tools)
        if report.bundle is None:
            raise PipelineError("\n".join(report.messages))
        bundle = report.bundle
        root = project.project_dir
        root.mkdir(parents=True, exist_ok=True)
        master = root / master_filename(project.show_date)
        if not master.exists():
            shutil.copy2(bundle.template, master)
        self.controller.script("create_lookbook_work_area.py", root, timeout=120)
        controlled = root / "control" / "work" / "_mat"
        controlled.mkdir(parents=True, exist_ok=True)
        shutil.copy2(bundle.reference_pdf, controlled / "reference.pdf")
        shutil.copy2(bundle.workbook, controlled / "caption-source.xlsx")
        target_hires = controlled / "hires"
        target_hires.mkdir(parents=True, exist_ok=True)
        for image in sorted((*bundle.hires.glob("*.jpg"), *bundle.hires.glob("*.jpeg")), key=lambda p: p.name.casefold()):
            target = target_hires / image.name
            if not target.exists() or target.stat().st_size != image.stat().st_size:
                shutil.copy2(image, target)
        return f"Проект подготовлен: {root.name}. Все рабочие файлы находятся в control/work."

    def _looks(self, project: ProjectRecord) -> str:
        self._require_prepared(project)
        registry = project.project_dir / "control" / "work" / "look-register.tsv"
        manual = {row["look_id"]: row for row in self.store.looks(project.id) if row.get("status") == "manual"}
        if not registry.is_file() or not manual:
            self.controller.script("build_reference_registry.py", project.project_dir, timeout=1800)
        rows = _read_tsv(registry)
        applied: list[dict[str, str]] = []
        for row in rows:
            override = manual.get(row["look_id"])
            if not override:
                continue
            left = str(override.get("left_filename", "")).strip()
            right = str(override.get("right_filename", "")).strip()
            if left and right and left != right:
                row["left_filename"], row["right_filename"] = left, right
                applied.append({"look_id": row["look_id"], "left_filename": left, "right_filename": right})
        if applied:
            _write_tsv(registry, rows)
            audit = project.project_dir / "control" / "work" / "ui-overrides" / "look-register-overrides.json"
            audit.parent.mkdir(parents=True, exist_ok=True)
            audit.write_text(json.dumps({"overrides": applied}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.controller.script("create_look_registry.py", registry, "--validate-ready", timeout=120)
        self.store.replace_looks(project.id, rows)
        suffix = f" Применено ручных исправлений: {len(applied)}." if applied else ""
        return f"Список построен по PDF-референсу: {len(rows)} луков. Пары доступны для ручной проверки.{suffix}"

    def _credits_map(self, project: ProjectRecord, provider: ModelProvider) -> str:
        root = project.project_dir
        registry = root / "control" / "work" / "look-register.tsv"
        if not registry.is_file():
            raise PipelineError("Сначала должен быть построен список луков.")
        targeted_rematch = self.store.requested_credit_rematches(project.id)
        if targeted_rematch:
            return self._targeted_credit_rematch(project, provider, targeted_rematch)
        workbook = root / "control" / "work" / "_mat" / "caption-source.xlsx"
        caption_map = root / "control" / "work" / "caption-map.tsv"
        if not caption_map.is_file():
            self.controller.script("prepare_caption_mapping.py", root, "--workbook", workbook, timeout=1800)
        self.controller.script("auto_caption_map.py", root, "--mode", "seed", timeout=1800)
        manual_assignments = self._apply_credit_overrides(project)
        self.controller.script("render_caption_mapping_evidence.py", root, "--map", "control/work/caption-map.tsv", "--hires", "control/work/_mat/hires", timeout=1800)

        if isinstance(provider, CodexProvider):
            rejected = self._confirm_codex_credit_proofs_parallel(project, provider)
            if rejected:
                # A proposed mapping is never silently accepted after a visual
                # rejection.  Freeze the proven rows and let the existing
                # one-to-one rematch workflow handle only the disputed cards.
                self.log(
                    "Кредитные карточки требуют точечной пересверки: "
                    + ", ".join(rejected)
                )
                self._targeted_credit_rematch(project, provider, rejected)
        else:
            self._select_local_credit_overrides(project, provider, manual_assignments)
            if manual_assignments:
                self.controller.script("render_caption_mapping_evidence.py", root, "--map", "control/work/caption-map.tsv", "--hires", "control/work/_mat/hires", timeout=1800)
            self._confirm_local_credit_proofs(project, provider)

        rows = _read_tsv(caption_map)
        self.store.replace_credits(project.id, rows)
        pending = [row["look_id"] for row in rows if row.get("visual_status") != "CONFIRMED"]
        if pending:
            raise ReviewRequired(
                f"Осталось {len(pending)} неоднозначных кредитных карточек. Откройте этап «Список кредитов», исправьте только их и продолжите."
            )
        captions = root / "control" / "work" / "caption-data.tsv"
        provenance = root / "control" / "work" / "caption-provenance.json"
        self.controller.script("build_verified_caption_data.py", caption_map, workbook, captions, "--provenance", provenance, timeout=600)
        return f"Все {len(rows)} кредитных карточек подтверждены и привязаны один-к-одному."

    def _targeted_credit_rematch(
        self,
        project: ProjectRecord,
        provider: ModelProvider,
        requested_looks: list[str],
    ) -> str:
        """Re-evaluate only operator-marked credit rows; freeze every other map row."""
        root = project.project_dir
        caption_map = root / "control" / "work" / "caption-map.tsv"
        workbook = root / "control" / "work" / "_mat" / "caption-source.xlsx"
        if not caption_map.is_file():
            raise PipelineError("Не найдена существующая карта кредитов для точечной перепроверки.")
        targets = sorted(set(requested_looks))
        before = _read_tsv(caption_map)
        before_by_look = {row["look_id"]: row.copy() for row in before}
        unknown = [look_id for look_id in targets if look_id not in before_by_look]
        if unknown:
            raise PipelineError("В карте кредитов отсутствуют отмеченные луки: " + ", ".join(unknown))

        for row in before:
            if row["look_id"] in targets:
                row["visual_status"] = "PENDING"
        _write_tsv(caption_map, before)
        manifest = root / "control" / "work" / "ui-overrides" / "targeted-credit-rematch.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "target_looks": targets,
                    "frozen_looks": [row["look_id"] for row in before if row["look_id"] not in targets],
                    "prior_assignments": {
                        look_id: {
                            "excel_sheet": before_by_look[look_id]["excel_sheet"],
                            "excel_look_number": before_by_look[look_id]["excel_look_number"],
                        }
                        for look_id in targets
                    },
                },
                ensure_ascii=False,
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )
        observations = root / "control" / "work" / "caption-map-observations"
        for look_id in targets:
            (observations / f"{look_id}.json").unlink(missing_ok=True)

        # Re-rendering leaves every map entry intact; it only gives the model
        # current proof images for the target rows and their candidate cards.
        self.controller.script(
            "render_caption_mapping_evidence.py", root,
            "--map", "control/work/caption-map.tsv", "--hires", "control/work/_mat/hires", timeout=1800,
        )
        if isinstance(provider, CodexProvider):
            result = provider.run_agent(
                targeted_credit_rematch_prompt(str(root), targets), root, timeout=7200,
            )
            if result:
                self.log("Codex: " + result[-4000:])
        else:
            self._confirm_local_credit_proofs(project, provider, only_looks=set(targets))

        after = _read_tsv(caption_map)
        after_by_look = {row["look_id"]: row for row in after}
        if set(after_by_look) != set(before_by_look):
            _write_tsv(caption_map, list(before_by_look.values()))
            raise PipelineError("Точечная перепроверка изменила состав карты; исходная карта восстановлена.")
        changed_non_targets: list[str] = []
        for row in after:
            if row["look_id"] not in targets and row != before_by_look[row["look_id"]]:
                row.update(before_by_look[row["look_id"]])
                changed_non_targets.append(row["look_id"])
        if changed_non_targets:
            _write_tsv(caption_map, after)
            self.log("Восстановлены замороженные строки карты: " + ", ".join(changed_non_targets))
        after = _read_tsv(caption_map)
        pending = [look_id for look_id in targets if after_by_look.get(look_id, {}).get("visual_status") != "CONFIRMED"]
        if pending:
            raise ReviewRequired(
                "Точечная перепроверка не подтвердила: " + ", ".join(pending) + ". Остальные луки не изменялись."
            )
        self.store.replace_credits(project.id, after)
        self.store.clear_credit_rematches(project.id, targets)
        captions = root / "control" / "work" / "caption-data.tsv"
        provenance = root / "control" / "work" / "caption-provenance.json"
        self.controller.script("build_verified_caption_data.py", caption_map, workbook, captions, "--provenance", provenance, timeout=600)
        return f"Точечно перепроверены только отмеченные луки: {', '.join(targets)}. Остальные строки карты сохранены без изменений."

    def _map_gate(self, project: ProjectRecord, provider: ModelProvider) -> str:
        root = project.project_dir
        # A targeted credit review can finish with the same approved mapping.
        # In that case the controller still legitimately points to structure;
        # trying to accept the identical map again produces a false block.
        if evidence_passed(root, "map"):
            return "Карта не изменилась после точечной сверки и уже подтверждена контроллером; продолжаем с этапа «Структура разворотов»."
        state = root / "control" / "lookbook-state.json"
        rows = self.store.looks(project.id)
        if not rows:
            raise PipelineError("Реестр луков пуст.")
        if not state.is_file():
            self.controller.gate(
                "init", root,
                "--master", master_filename(project.show_date),
                "--registry", "control/work/look-register.tsv",
                "--captions", "control/work/caption-data.tsv",
                "--caption-map", "control/work/caption-map.tsv",
                "--caption-workbook", "control/work/_mat/caption-source.xlsx",
                "--caption-provenance", "control/work/caption-provenance.json",
                "--hires", "control/work/_mat/hires",
                "--reference", "control/work/_mat/reference.pdf",
                "--looks", str(len(rows)),
                "--show-date", project.show_date.strftime("%d.%m.%Y"),
                "--show-text", visible_date_text(project.show_date),
                timeout=300,
            )
        self.controller.gate("prepare-reference-order", root, timeout=1800)
        if isinstance(provider, CodexProvider):
            self._confirm_codex_reference_proofs_parallel(project, provider)
        else:
            self._confirm_local_reference_proofs(project, provider)
        # The visual mapper may legitimately run validate-map itself after the
        # last reference confirmation.  Calling it a second time is not a
        # failure of the map: the controller correctly reports that the next
        # gate is structure.  Accept its durable PASS instead of surfacing a
        # false "control map" error to the operator.
        if evidence_passed(root, "map"):
            return "PDF-порядок, пары изображений и кредитная карта уже подтверждены контроллером."
        self.controller.gate("validate-map", root, timeout=600)
        if not evidence_passed(root, "map"):
            raise PipelineError("Контроллер не записал PASS map.")
        return "PDF-порядок, пары изображений и кредитная карта зафиксированы контроллером."

    def _native(self, project: ProjectRecord, gate: str) -> str:
        self._require_initialized(project)
        self.controller.apply_native_gate(project.project_dir, gate)
        return f"PASS {gate}: контроллер подтвердил сохранённый InDesign master."

    def _visual(self, project: ProjectRecord, provider: ModelProvider) -> str:
        if evidence_passed(project.project_dir, "visual"):
            return "Визуальная проверка уже подтверждена текущим master."
        if isinstance(provider, CodexProvider):
            self._run_parallel_codex_visual(project, provider)
        else:
            self._run_local_visual(project, provider)
        if not evidence_passed(project.project_dir, "visual"):
            raise ReviewRequired(visual_audit_blocker_message(project.project_dir))
        return "Все развороты подтверждены: порядок фото, ссылки, кредиты, safe area и clearance."

    def _review(self, project: ProjectRecord) -> str:
        root = project.project_dir
        self.controller.apply_native_gate(root, "release")
        if evidence_passed(root, "pdf"):
            return "Review-PDF уже проверен контроллером."
        state = read_json(root / "control" / "lookbook-state.json")
        try:
            revision = max(1, int(state.get("current_revision", 1)))
        except (TypeError, ValueError):
            revision = 1
        pdf = review_pdf_filename(project.show_date, revision)
        self.controller.export_review_pdf(root, pdf)
        self.controller.gate("verify-pdf", root, "--pdf", pdf, timeout=1800)
        self.controller.gate("complete", root, timeout=180)
        if not evidence_passed(root, "pdf"):
            raise PipelineError("Контроллер не подтвердил review-PDF.")
        return f"PDF на проверку готов: {root / pdf}"

    def _final(self, project: ProjectRecord) -> str:
        if not self.store.is_approved(project.id):
            raise ReviewRequired("Финальный выпуск ожидает отметки «Согласовано».")
        self.controller.gate("publish-final", project.project_dir, "--approval-note", "согласовано", timeout=3600)
        if not final_outputs_passed(project.project_dir):
            raise PipelineError("Контроллер не подтвердил комплект финальных PDF.")
        return "Финальные 10mb/20mb/40mb и Gender M/W PDF записаны и проверены."

    def _delegate_codex(self, project: ProjectRecord, provider: CodexProvider, stage: str) -> None:
        prompts = {
            "credits_map": """Заверши только визуальное сопоставление кредитов текущего проекта по встроенному протоколу LOOKBOOKBOT. PDF-порядок уже находится в control/work/look-register.tsv. Если существует control/work/ui-overrides/caption-overrides.json, это вручную подтверждённые оператором выборы Excel: каждый такой LOOK_### обязан получить exact alternative proof, быть реально просмотрен на нём, затем выбран только через штатный select-alternatives в безопасной группе не более пяти связанных LOOK_###. Никогда не заменяй этот выбор автоматическим seed. После выбора перерисуй обычные трёхпанельные proof cards, просмотри их партиями не более пяти и подтверди только реально просмотренные совпадения. Закончи с нулём PENDING. Не переходи к init или InDesign.""",
            "credits_rematch": """Выполни только точечную повторную сверку кредитов. Список единственных разрешённых LOOK_### находится в control/work/ui-overrides/targeted-credit-rematch.json. Все остальные строки caption-map.tsv заморожены: не открывай для них proof cards, не подтверждай, не меняй лист/номер/статус и не запускай seed или reset-visual-review. Для каждого target LOOK визуально сравни его PDF-пару с контролируемыми Excel previews и выбери карту по одежде, цвету, аксессуарам, обуви, сумке, позе и модели — никогда по порядковому номеру. Если подходящая карта уже назначена другому target LOOK, выполни полный обмен только внутри этой связанной группы (максимум пять LOOK) через alternative-proofs и select-alternatives; карту у неотмеченного LOOK брать запрещено. Для каждого нового или оставленного выбора создай/просмотри точную proof-card и подтверди только этот target через confirm-review с конкретными визуальными признаками. Закончи, когда все и только target LOOK получат CONFIRMED. Не переходи к init, InDesign или следующим этапам.""",
            "map": """Заверши только текущий gate map этого проекта по встроенному протоколу LOOKBOOKBOT. Просмотри все reference-order cards партиями не более пяти, в той же итерации запиши immutable confirm-reference-look для просмотренных LOOK_### и доведи счётчик до N/N, затем выполни validate-map. Не начинай structure.""",
            "visual": """Заверши только gate visual текущего проекта по встроенному протоколу LOOKBOOKBOT. Не меняй страницы и фреймы. Проверь full-left/close-right, ссылки, CREDiTs, overflow, safe area с внутренним отступом 12 pt и caption clearance. Разрешены только горизонтальный сдвиг изображения и, если он не помогает, перенос того же кредитного фрейма вниз/вправо/вниз-вправо. Для единичной проблемы используй targeted calibration. Подтверди каждый реально просмотренный current-master proof и запиши PASS visual; не экспортируй review PDF.""",
        }
        prompt = (
            "Ты выполняешь один ограниченный этап приложения LOOKBOOKBOT. Не спрашивай пользователя и не сообщай частичный успех.\n"
            f"Проект: {project.project_dir}\nЭтап: {stage}\n{prompts[stage]}\n"
            "Контроллер lookbook_gate.py — единственный источник завершения."
        )
        result = provider.run_agent(prompt, project.project_dir)
        if result:
            self.log("Codex: " + result[-4000:])

    def _confirm_local_credit_proofs(
        self, project: ProjectRecord, provider: ModelProvider, only_looks: set[str] | None = None,
    ) -> None:
        root = project.project_dir
        mapping = _read_tsv(root / "control" / "work" / "caption-map.tsv")
        accepted: list[tuple[str, str]] = []
        for row in mapping:
            if only_looks is not None and row["look_id"] not in only_looks:
                continue
            if row.get("visual_status") == "CONFIRMED":
                continue
            proof = root / row["evidence_file"]
            decision = provider.inspect_proof(
                "На карточке слева Excel-look, далее две фотографии одного PDF-лука. Ответь только JSON "
                "{\"match\":true/false,\"note\":\"минимум два конкретных видимых признака: одежда, цвет, сумка, обувь, поза\"}. "
                "Фон и порядковый номер не являются доказательством.",
                [proof],
            )
            if decision.accepted:
                accepted.append((row["look_id"], decision.note))
            if len(accepted) == 5:
                self._confirm_credit_batch(root, accepted)
                accepted.clear()
        if accepted:
            self._confirm_credit_batch(root, accepted)

    def _confirm_credit_batch(self, root: Path, accepted: list[tuple[str, str]]) -> None:
        looks = ",".join(look for look, _note in accepted)
        notes = "||".join(f"{look}={note}" for look, note in accepted)
        self.controller.script("auto_caption_map.py", root, "--mode", "confirm-review", "--looks", looks, "--notes", notes, timeout=600)

    def _confirm_codex_credit_proofs_parallel(self, project: ProjectRecord, provider: CodexProvider) -> list[str]:
        """Inspect disjoint proposed credit cards concurrently, then commit serially.

        `auto_caption_map.py` rewrites the shared TSV and therefore remains a
        single-writer operation.  Codex workers only read a fixed group of up
        to five already-rendered proof cards and return structured decisions;
        the coordinator validates and records those decisions afterwards.
        """
        root = project.project_dir
        mapping = _read_tsv(root / "control" / "work" / "caption-map.tsv")
        pending = [
            row for row in mapping
            if row.get("visual_status") != "CONFIRMED"
        ]
        if not pending:
            return []
        batches = _row_batches(pending, size=5)
        workers = min(4, len(batches))
        self.log(
            f"Сверка кредитов: {len(pending)} карточек в {len(batches)} независимых пакетах, параллельно до {workers}."
        )
        decisions: dict[str, VisionDecision] = {}
        failures: list[str] = []
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="lookbook-credits") as executor:
            futures = {
                executor.submit(self._inspect_codex_credit_batch, provider, root, batch): batch
                for batch in batches
            }
            for future in as_completed(futures):
                batch = futures[future]
                labels = ", ".join(str(row["look_id"]) for row in batch)
                try:
                    decisions.update(future.result())
                except (ProviderError, ValueError, OSError) as error:
                    failures.append(f"{labels}: {error}")
                else:
                    self.log(f"Пакет кредитов просмотрен: {labels}")
        if failures:
            raise ReviewRequired("Не завершены независимые пакеты сверки кредитов:\n" + "\n".join(failures))

        rows_by_look = {str(row["look_id"]): row for row in pending}
        accepted = [
            (str(row["look_id"]), _safe_confirmation_note(decisions[str(row["look_id"])].note))
            for row in pending
            if decisions.get(str(row["look_id"])) and decisions[str(row["look_id"])].accepted
        ]
        for batch in _pair_batches(accepted, size=5):
            try:
                self._confirm_credit_batch(root, batch)
            except CommandError as error:
                # The map writer has not changed the TSV when it rejects a
                # batch of proof notes, so it is safe to re-inspect only the
                # named cards and retry the same batch.  This bridges wording
                # differences between models without weakening visual proof.
                if "visual observation is not specific enough" not in str(error).casefold():
                    raise
                failed_looks = [
                    look_id for look_id in _look_ids_in_text(str(error))
                    if look_id in rows_by_look and any(look_id == item[0] for item in batch)
                ]
                if not failed_looks:
                    raise
                self.log(
                    "Уточняю визуальные признаки только для: " + ", ".join(failed_looks)
                )
                repaired = self._inspect_codex_credit_batch(
                    provider, root, [rows_by_look[look_id] for look_id in failed_looks], strict_notes=True,
                )
                decisions.update(repaired)
                retry_batch = [
                    (look_id, _safe_confirmation_note(decisions[look_id].note))
                    for look_id, _note in batch
                    if decisions.get(look_id) and decisions[look_id].accepted
                ]
                if retry_batch:
                    self._confirm_credit_batch(root, retry_batch)
        rejected = [
            str(row["look_id"]) for row in pending
            if not decisions.get(str(row["look_id"])) or not decisions[str(row["look_id"])].accepted
        ]
        return rejected

    def _inspect_codex_credit_batch(
        self, provider: CodexProvider, root: Path, rows: list[dict[str, str]], *, strict_notes: bool = False,
    ) -> dict[str, VisionDecision]:
        expected = [str(row["look_id"]) for row in rows]
        cards = []
        attachments: list[Path] = []
        for row in rows:
            evidence = root / str(row.get("evidence_file", ""))
            if not evidence.is_file():
                raise ValueError(f"{row['look_id']}: отсутствует proof-карточка {evidence}")
            cards.append(f"- {row['look_id']}: {evidence}")
            attachments.append(evidence)
        prompt = f"""Выполни только независимую визуальную сверку предложенных кредитных карточек.

Проект: {root}
Тебе разрешено смотреть только эти proof-карточки:
{chr(10).join(cards)}

Каждая карточка состоит из Excel-лука и двух фотографий PDF-лука. Для каждого LOOK проверь реальное совпадение по модели, одежде, цвету, аксессуарам, обуви, сумке, позе и силуэту. Нельзя использовать порядок, номера строк, гендер, названия файлов или фон как доказательство.

Ничего не записывай, не запускай команды, не меняй TSV и не открывай InDesign. Верни только JSON без Markdown:
{{"decisions":[{{"look_id":"LOOK_001","accepted":true,"note":"не менее двух конкретных видимых признаков"}}]}}

В JSON должны быть ровно эти LOOK: {", ".join(expected)}. Если карточка не совпадает, верни accepted=false и укажи конкретную причину."""
        note_contract = (
            "\nFor every accepted decision, write note as at least two explicit, visible labels with values: "
            "`garment=<item and colour>; bag=<item>` or `garment=<item>; shoes=<item>; accessory=<item>`. "
            "Use only what is visibly present in the supplied card. The note must be at least 28 characters; "
            "never write a generic phrase such as 'the images match'."
        )
        if strict_notes:
            note_contract += (
                " This is a recovery pass after a note-format rejection: comply with the label format exactly "
                "for every accepted LOOK."
            )
        raw = provider.run_readonly_agent(prompt + note_contract, root, timeout=3600, images=attachments)
        return _parse_codex_batch_decisions(raw, expected)

    def _confirm_codex_reference_proofs_parallel(self, project: ProjectRecord, provider: CodexProvider) -> None:
        """Confirm PDF-reference evidence in parallel without concurrent controller writes."""
        root = project.project_dir
        cards = _latest_cards(root / "control" / "work" / "reference-order")
        confirmations = root / "control" / "reference-order" / "confirmations"
        pending = [card for card in cards if not (confirmations / f"{card.stem}.json").is_file()]
        if not pending:
            return
        batches = _path_batches(pending, size=5)
        workers = min(4, len(batches))
        self.log(
            f"Сверка PDF-референса: {len(pending)} карточек в {len(batches)} независимых пакетах, параллельно до {workers}."
        )
        decisions: dict[str, VisionDecision] = {}
        failures: list[str] = []
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="lookbook-reference") as executor:
            futures = {
                executor.submit(self._inspect_codex_reference_batch, provider, root, batch): batch
                for batch in batches
            }
            for future in as_completed(futures):
                batch = futures[future]
                labels = ", ".join(card.stem for card in batch)
                try:
                    decisions.update(future.result())
                except (ProviderError, ValueError, OSError) as error:
                    failures.append(f"{labels}: {error}")
                else:
                    self.log(f"Пакет PDF-референса просмотрен: {labels}")
        if failures:
            raise ReviewRequired("Не завершены независимые пакеты PDF-референса:\n" + "\n".join(failures))
        rejected = [card.stem for card in pending if not decisions.get(card.stem) or not decisions[card.stem].accepted]
        if rejected:
            details = "; ".join(
                f"{look}: {decisions[look].note}" for look in rejected if look in decisions
            )
            raise ReviewRequired("PDF-референс не подтвердил зарегистрированные пары: " + (details or ", ".join(rejected)))
        # Immutable confirmations are written one by one by the coordinator.
        # This keeps a single durable controller sequence while vision work is parallel.
        for card in pending:
            self.controller.gate(
                "confirm-reference-look", root, "--look", card.stem,
                "--note", _safe_confirmation_note(decisions[card.stem].note), timeout=120,
            )

    def _inspect_codex_reference_batch(
        self, provider: CodexProvider, root: Path, cards: list[Path],
    ) -> dict[str, VisionDecision]:
        expected = [card.stem for card in cards]
        card_lines = "\n".join(f"- {card.stem}: {card}" for card in cards)
        prompt = f"""Выполни только независимую проверку соответствия PDF-референса и пар hires.

Проект: {root}
Разрешены только эти карточки:
{card_lines}

На каждой карточке сверху PDF-разворот, ниже — назначенные full-length слева и close-up справа. Для каждого LOOK проверь, что это один и тот же лук по модели, одежде, цвету, обуви, сумке, аксессуарам, позе и кадрированию. Номера, имена файлов и порядок не являются доказательством.

Ничего не записывай, не запускай команды, не меняй реестр и не открывай InDesign. Верни только JSON без Markdown:
{{"decisions":[{{"look_id":"LOOK_001","accepted":true,"note":"не менее двух конкретных видимых признаков"}}]}}

В JSON должны быть ровно эти LOOK: {", ".join(expected)}."""
        raw = provider.run_readonly_agent(prompt, root, timeout=3600, images=cards)
        return _parse_codex_batch_decisions(raw, expected)

    def _confirm_local_reference_proofs(self, project: ProjectRecord, provider: ModelProvider) -> None:
        root = project.project_dir
        cards = _latest_cards(root / "control" / "work" / "reference-order")
        confirmation_dir = root / "control" / "reference-order" / "confirmations"
        for card in cards:
            if (confirmation_dir / f"{card.stem}.json").is_file():
                continue
            decision = provider.inspect_proof(
                "Карточка показывает PDF-референс и зарегистрированную пару hires. Ответь JSON "
                "{\"match\":true/false,\"note\":\"конкретные признаки\"}. Совпадение допустимо только если это тот же лук, "
                "полный рост назначен слева, клоузап справа.",
                [card],
            )
            if not decision.accepted:
                raise ReviewRequired(f"{card.stem}: локальная модель не подтвердила пару. Исправьте строку списка луков.")
            self.controller.gate(
                "confirm-reference-look", root, "--look", card.stem,
                "--note", decision.note or "Reference pair visually matches; full length left, close-up right.", timeout=120,
            )

    def _prepare_visual_proof(self, project: ProjectRecord) -> list[Path]:
        """Run native visual work, or reuse the current proof being confirmed.

        Once even one immutable visual confirmation exists, the saved proof
        session is already the object under review.  Re-arming composition,
        reconciling a clearance plan, or rendering a new proof session at that
        point could invalidate the 49 confirmations which are already bound to
        the current master.  Resume the exact proof queue instead.
        """
        root = project.project_dir
        confirmations = root / "control" / "visual" / "confirmations"
        current_pairs = _latest_cards(root / "control" / "visual" / "proof" / "pairs")
        confirmed = list(confirmations.glob("*.json")) if confirmations.is_dir() else []
        if confirmed:
            if not current_pairs:
                raise PipelineError(
                    "Есть visual-proof подтверждения, но отсутствуют их текущие proof-развороты; "
                    "для сохранности подтверждённого master нужна новая ревизия."
                )
            self.log(
                f"Возобновляю текущую visual-proof сессию: {len(confirmed)} подтверждений сохранены; "
                "InDesign, composition plan и proof-рендер не запускаются повторно."
            )
            return current_pairs
        arm = self.controller.gate("arm", root, "--gate", "visual", timeout=120, check=False)
        if arm.returncode and "armed" not in arm.text.casefold():
            self.log(arm.text)
        self._prepare_or_resume_visual_composition(root)
        self._apply_composition_until_saved(root)
        # A visual proof is expensive, but a retry count is not a clearance
        # rule.  One plan may fix many looks at once, while a later proof can
        # expose another one of the 50 looks.  Continue until the proof is
        # clear.  The duplicate-plan guard below is the real convergence
        # boundary: it prevents an unattended job from spending hours applying
        # the same geometry again and again.
        seen_clearance_plans: set[str] = set()
        attempt = 0
        while True:
            attempt += 1
            rendered = self.controller.gate("render-visual-proof", root, timeout=1800, check=False)
            if rendered.returncode == 0:
                pairs = _latest_cards(root / "control" / "visual" / "proof" / "pairs")
                if not pairs:
                    raise PipelineError("Визуальный proof не содержит разворотов для проверки.")
                return pairs
            plan = self.controller.gate("plan-clearance-corrections", root, timeout=900, check=False)
            if plan.returncode:
                raise ReviewRequired(plan.text or rendered.text)
            plan_path = root / "control" / "visual" / "clearance-correction-plan.json"
            if not plan_path.is_file():
                raise PipelineError("Контроллер не сохранил план caption-clearance после блокировки visual-proof.")
            plan_data = json.loads(plan_path.read_text(encoding="utf-8"))
            # The controller archives every retry with a fresh timestamp, so
            # hashing the file itself would make an identical no-op plan look
            # new. Compare only executable geometry before spending another
            # full InDesign proof export.
            plan_digest = _clearance_plan_digest(plan_data)
            unresolved = [
                str(item.get("look_id"))
                for item in plan_data.get("unresolved", [])
                if isinstance(item, dict) and isinstance(item.get("look_id"), str)
            ]
            executable = list(plan_data.get("corrections", [])) + list(plan_data.get("caption_corrections", []))
            if not executable:
                suffix = ": " + ", ".join(unresolved) if unresolved else ""
                raise PipelineError(
                    "Контроллер не нашёл следующей допустимой коррекции caption-clearance"
                    + suffix
                    + ". Документ не изменён."
                )
            if plan_digest in seen_clearance_plans:
                raise PipelineError(
                    "Контроллер повторил уже применённый план caption-clearance без новой допустимой коррекции"
                    + (": " + ", ".join(unresolved) if unresolved else ".")
                )
            seen_clearance_plans.add(plan_digest)
            self.log(
                f"Caption-clearance: выполняю автономную коррекцию №{attempt} "
                f"(фото: {len(plan_data.get('corrections', []))}, кредиты: {len(plan_data.get('caption_corrections', []))})."
            )
            self._apply_composition_until_saved(root)

    def _apply_composition_until_saved(self, root: Path) -> None:
        """Resume composition only while its durable checkpoint advances.

        ``apply-composition`` owns one short native transaction.  The previous
        100-call ceiling was unrelated to the number of looks in the current
        signed plan.  A checkpoint carries the exact completed LOOK IDs, so it
        is the authority for both safe continuation and convergence.
        """
        seen_checkpoints: set[str] = set()
        while True:
            before = _composition_checkpoint_signature(root)
            result = self.controller.gate("apply-composition", root, timeout=1800, check=False)
            if result.returncode:
                raise PipelineError(result.text)
            output = result.text.upper()
            if "PASS COMPOSITION" in output:
                return
            if "CHECKPOINT" not in output:
                raise PipelineError(
                    "Контроллер не зафиксировал PASS или checkpoint для применения composition plan."
                )
            after = _composition_checkpoint_signature(root)
            if after is None:
                raise PipelineError(
                    "Composition вернул checkpoint без сохранённых данных о выполненных LOOK."
                )
            if after == before or after in seen_checkpoints:
                raise PipelineError(
                    "Composition повторил checkpoint без нового сохранённого прогресса; master не изменён."
                )
            seen_checkpoints.add(after)

    def _run_parallel_codex_visual(
        self, project: ProjectRecord, provider: CodexProvider, *, repaired_plans: set[str] | None = None,
    ) -> None:
        """Inspect proof cards concurrently and commit accepted checks serially.

        A vision worker is intentionally read-only: it receives the actual JPG
        proof cards and returns a JSON decision.  The coordinator is the only
        component allowed to write a ``confirm-visual-look`` record.  This
        avoids a fragile failure mode where an agent correctly inspects a card
        but forgets to invoke the confirmation command afterwards.
        """
        root = project.project_dir
        pairs = self._prepare_visual_proof(project)
        confirmations = root / "control" / "visual" / "confirmations"
        pending = [proof for proof in pairs if not (confirmations / f"{proof.stem}.json").is_file()]
        decisions: dict[str, VisionDecision] = {}
        unresolved = pending
        diagnostics: dict[str, str] = {}
        repaired_plans = set() if repaired_plans is None else repaired_plans
        repeated_observation_states: set[tuple[tuple[str, str], ...]] = set()

        # A missing decision is a transient worker failure, not a reason to
        # ask the operator to click Continue. Retry only those exact proof
        # cards while the response state changes. A repeated identical failure
        # has no new evidence to act on, so it must not become an infinite
        # unattended loop.
        while unresolved:
            batches = _path_batches(unresolved, size=5)
            workers = min(4, len(batches))
            self.log(
                f"Визуальная проверка: {len(unresolved)} разворотов "
                f"в {len(batches)} независимых пакетах, одновременно до {workers}."
            )
            attempt_decisions: dict[str, VisionDecision] = {}
            failures: list[str] = []
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="lookbook-visual") as executor:
                futures = {
                    executor.submit(self._confirm_codex_visual_batch, provider, root, batch): batch
                    for batch in batches
                }
                for future in as_completed(futures):
                    batch = futures[future]
                    labels = ", ".join(proof.stem for proof in batch)
                    try:
                        batch_decisions = future.result()
                    except (ProviderError, CommandError, OSError, ValueError) as error:
                        failures.append(f"{labels}: {error}")
                        for proof in batch:
                            diagnostics[proof.stem] = str(error)
                    else:
                        attempt_decisions.update(batch_decisions)
                        self.log(f"Визуальный пакет просмотрен: {labels}")

            for look_id, decision in attempt_decisions.items():
                decisions[look_id] = decision
                diagnostics[look_id] = decision.note
            unresolved = [
                proof for proof in unresolved
                if not attempt_decisions.get(proof.stem) or not attempt_decisions[proof.stem].accepted
            ]
            if not unresolved:
                break
            rejected = {
                proof.stem: decisions[proof.stem]
                for proof in unresolved
                if proof.stem in decisions and not decisions[proof.stem].accepted
            }
            if rejected:
                plan_digest = self._repair_rejected_visual_proofs(root, rejected)
                if plan_digest is None:
                    details = "; ".join(
                        f"{look_id}: {decision.note}" for look_id, decision in rejected.items()
                    )
                    raise ReviewRequired(
                        "Контроллер не нашёл новую допустимую visual-коррекцию: " + details
                    )
                if plan_digest in repaired_plans:
                    raise ReviewRequired(
                        "Контроллер повторил уже применённую visual-коррекцию без новой геометрии: "
                        + ", ".join(sorted(rejected))
                    )
                repaired_plans.add(plan_digest)
                self.log(
                    "Контролируемая коррекция visual-proof подготовлена; "
                    "перезапускаю только новый цикл доказательств."
                )
                return self._run_parallel_codex_visual(project, provider, repaired_plans=repaired_plans)

            missing = [proof for proof in unresolved if proof.stem not in decisions]
            if not missing:
                raise PipelineError(
                    "Визуальная модель вернула неиспользуемое решение без подтверждения или корректировки."
                )
            observation_state = tuple(
                (proof.stem, _normalise_observation_error(diagnostics.get(proof.stem, "нет решения")))
                for proof in missing
            )
            if observation_state in repeated_observation_states:
                details = "; ".join(f"{look}: {reason}" for look, reason in observation_state)
                raise ProviderError(
                    "Визуальная модель повторила неизменный сбой без новых наблюдений: " + details
                )
            repeated_observation_states.add(observation_state)
            labels = ", ".join(proof.stem for proof in missing)
            self.log(
                f"Автоповтор visual-proof только для: {labels}. "
                + ("Ошибки пакетов: " + "; ".join(failures) if failures else "")
            )
            unresolved = missing

        # Keep the durable state single-writer.  A worker never calls a
        # controller command; only accepted decisions become immutable proof
        # confirmations here.
        for proof in pending:
            self.controller.gate(
                "confirm-visual-look", root, "--look", proof.stem,
                "--note", _safe_confirmation_note(decisions[proof.stem].note), timeout=120,
            )
        missing = [proof.stem for proof in pairs if not (confirmations / f"{proof.stem}.json").is_file()]
        if missing:
            raise PipelineError("Контроллер не записал visual-proof подтверждения: " + ", ".join(missing))
        self.controller.gate(
            "record-visual", root,
            "--notes", "Все current-master proofs проверены в независимых пакетах; компьютерный caption clearance сохранён.",
            timeout=300,
        )

    def _confirm_codex_visual_batch(
        self, provider: CodexProvider, root: Path, proofs: list[Path],
    ) -> dict[str, VisionDecision]:
        look_ids = [proof.stem for proof in proofs]
        proof_lines = "\n".join(f"- {proof.stem}: {proof}" for proof in proofs)
        prompt = f"""Выполни только независимую визуальную проверку лукбука.

Проект: {root}
Тебе разрешены только эти LOOK: {", ".join(look_ids)}.
К этому запросу приложены JPG proof-развороты, которые нужно реально просмотреть:
{proof_lines}

Для каждого проверь: full-length слева, close-up справа, видимые кредиты читаемы, текст кредитов не пересекает модель и расположен внутри safe area. Не меняй фото, страницы, фреймы, InDesign или composition plan.

Ничего не записывай, не запускай команды и не открывай InDesign. Верни только JSON без Markdown:
{{"decisions":[{{"look_id":"LOOK_001","accepted":true,"note":"не менее двух конкретных наблюдений о кадрах, кредитах и safe area"}}]}}

В JSON должны быть ровно эти LOOK: {", ".join(look_ids)}. Если разворот нельзя честно подтвердить, верни accepted=false и конкретную причину."""
        raw = provider.run_readonly_agent(prompt, root, timeout=3600, images=proofs)
        return _parse_codex_batch_decisions(raw, look_ids)

    def _repair_rejected_visual_proofs(
        self, root: Path, rejected: dict[str, VisionDecision],
    ) -> str | None:
        """Turn a grounded visual rejection into a state-checked controller repair.

        The computer clearance audit is renewed before its plan is made.  The
        controller then archives only the superseded partial confirmations and
        plans the identified LOOK IDs with its legal image/credits-frame
        options.  It never edits InDesign directly and never touches an already
        passed visual/review gate.
        """
        look_ids = sorted(rejected)
        notes = " || ".join(
            f"{look_id}: {_safe_confirmation_note(rejected[look_id].note)}" for look_id in look_ids
        )
        self.log("Визуальная модель отклонила " + ", ".join(look_ids) + "; обновляю controller clearance-аудит.")
        refreshed = self.controller.gate("refresh-caption-clearance", root, timeout=900, check=False)
        if refreshed.returncode:
            self.log("Не удалось обновить caption-clearance для автокоррекции: " + refreshed.text)
            return None
        discarded = self.controller.gate(
            "restart-visual-confirmations", root, "--notes", notes, timeout=180, check=False,
        )
        if discarded.returncode:
            self.log("Не удалось безопасно архивировать partial visual confirmations: " + discarded.text)
            return None
        planned = self.controller.gate(
            "plan-clearance-corrections", root, "--force-looks", ",".join(look_ids), timeout=1800, check=False,
        )
        if planned.returncode:
            self.log("Контроллер не смог построить безопасную visual-коррекцию: " + planned.text)
            return None
        plan_path = root / "control" / "visual" / "clearance-correction-plan.json"
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            self.log("Контроллер не сохранил читаемый план visual-коррекции.")
            return None
        if not isinstance(plan, dict):
            self.log("Контроллер сохранил план visual-коррекции в неверном формате.")
            return None
        executable = list(plan.get("corrections", [])) + list(plan.get("caption_corrections", []))
        if not executable:
            self.log("План visual-коррекции не содержит допустимых изменений.")
            return None
        self.log(planned.text)
        return _clearance_plan_digest(plan)

    def _run_local_visual(
        self, project: ProjectRecord, provider: ModelProvider, *, repaired_plans: set[str] | None = None,
    ) -> None:
        root = project.project_dir
        repaired_plans = set() if repaired_plans is None else repaired_plans
        pairs = self._prepare_visual_proof(project)
        rejected: dict[str, VisionDecision] = {}
        for proof in pairs:
            decision = provider.inspect_proof(
                "Проверь разворот лукбука. Ответь JSON {\"match\":true/false,\"note\":\"конкретное наблюдение\"}. "
                "Требования: полный рост слева, клоузап справа; кредиты читаемы, не обрезаны, не пересекают модель и находятся внутри safe area.",
                [proof],
            )
            if not decision.accepted:
                rejected[proof.stem] = decision
                continue
            self.controller.gate("confirm-visual-look", root, "--look", proof.stem, "--note", decision.note, timeout=120)
        if rejected:
            plan_digest = self._repair_rejected_visual_proofs(root, rejected)
            if plan_digest is not None and plan_digest not in repaired_plans:
                repaired_plans.add(plan_digest)
                return self._run_local_visual(project, provider, repaired_plans=repaired_plans)
            details = "; ".join(f"{look}: {decision.note}" for look, decision in rejected.items())
            raise ReviewRequired("Автоматическая visual-коррекция не смогла безопасно исправить: " + details)
        self.controller.gate("record-visual", root, "--notes", "Проверены все current-master proofs и компьютерный caption clearance.", timeout=300)

    def _prepare_or_resume_visual_composition(self, root: Path) -> None:
        """Create the initial composition plan once, then resume that exact plan.

        A visual stage can stop after the initial plan has been saved but before
        the first native batch.  Recreating that plan would discard its signed
        retry state and causes the controller's deliberate "already exists"
        guard.  Resume instead; a safe pre-flight reconciliation can restore a
        missing prior credits position from same-master native evidence without
        opening InDesign.
        """
        plan = root / "control" / "visual" / "composition-plan.tsv"
        if not plan.is_file():
            self.controller.script("prepare_composition_audit.py", root, timeout=600)
            return
        correction = root / "control" / "visual" / "clearance-correction-plan.json"
        if correction.is_file():
            confirmations = root / "control" / "visual" / "confirmations"
            if confirmations.is_dir() and any(confirmations.glob("*.json")):
                self.log(
                    "Пропускаю reconciliation: текущий visual master уже имеет подтверждённые proof-развороты."
                )
                return
            reconciled = self.controller.gate(
                "reconcile-clearance-plan-priors", root, timeout=180, check=False,
            )
            if reconciled.returncode:
                raise PipelineError(reconciled.text)
            if reconciled.text:
                self.log(reconciled.text)
        self.log("Возобновляю сохранённый composition plan без его пересоздания.")

    def _apply_credit_overrides(self, project: ProjectRecord) -> list[tuple[str, str, str]]:
        root = project.project_dir
        caption_map = root / "control" / "work" / "caption-map.tsv"
        if not caption_map.is_file():
            return []
        duplicates = self.store.duplicate_credit_pairs(project.id)
        if duplicates:
            rendered = "; ".join(
                f"{sheet}/{number}: {', '.join(looks)}" for (sheet, number), looks in duplicates.items()
            )
            raise ReviewRequired(f"В списке кредитов есть повторяющиеся карточки Excel: {rendered}. Исправьте красные строки до запуска.")
        current = {row["look_id"]: row for row in _read_tsv(caption_map)}
        assignments: list[tuple[str, str, str]] = []
        manual_rows: list[dict[str, str]] = []
        for row in self.store.credits(project.id):
            existing = current.get(row["look_id"])
            if not existing:
                continue
            if not row.get("manual_override"):
                continue
            manual_rows.append({
                "look_id": row["look_id"],
                "excel_sheet": row["excel_sheet"],
                "excel_look_number": row["excel_look_number"],
                "note": row.get("note", ""),
            })
            if row["excel_sheet"] and row["excel_look_number"] and (
                row["excel_sheet"] != existing["excel_sheet"] or row["excel_look_number"] != existing["excel_look_number"]
            ):
                assignments.append((row["look_id"], row["excel_sheet"], row["excel_look_number"]))
        manifest = root / "control" / "work" / "ui-overrides" / "caption-overrides.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            json.dumps({"schema": 1, "human_confirmed": manual_rows}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if assignments:
            # A card exchange must be checked as one atomic connected group.
            # Calculating the groups before producing any proof avoids a
            # half-applied swap and also keeps every controller call within
            # the hard five-look boundary.
            for batch in _credit_override_batches(assignments, current):
                looks = ",".join(look_id for look_id, _sheet, _number in batch)
                value = ",".join(f"{look_id}={sheet}:{number}" for look_id, sheet, number in batch)
                self.controller.script(
                    "auto_caption_map.py", root, "--mode", "alternative-proofs",
                    "--looks", looks, "--assignments", value, timeout=900,
                )
        return assignments

    def _select_local_credit_overrides(
        self,
        project: ProjectRecord,
        provider: ModelProvider,
        assignments: list[tuple[str, str, str]],
    ) -> None:
        if not assignments:
            return
        root = project.project_dir
        current = {row["look_id"]: row for row in _read_tsv(root / "control" / "work" / "caption-map.tsv")}
        for batch in _credit_override_batches(assignments, current):
            for look_id, sheet, number in batch:
                proof = root / "control" / "work" / "caption-map-alternatives" / look_id / f"{sheet}_{int(number):03}.jpg"
                decision = provider.inspect_proof(
                    "На карточке слева выбранная оператором Excel-карточка, далее две фотографии PDF-лука. "
                    "Ответь JSON {\"match\":true/false,\"note\":\"минимум два конкретных признака\"}. "
                    "Проверь одежду, цвет, аксессуары, обувь, сумку или позу; номер и фон не являются доказательством.",
                    [proof],
                )
                if not decision.accepted:
                    raise ReviewRequired(f"{look_id}: локальная модель не подтвердила вручную выбранную Excel-карточку.")
            value = ",".join(f"{look_id}={sheet}:{number}" for look_id, sheet, number in batch)
            self.controller.script("auto_caption_map.py", root, "--mode", "select-alternatives", "--assignments", value, timeout=900)

    def _require_prepared(self, project: ProjectRecord) -> None:
        if not (project.project_dir / "control" / "work").is_dir():
            raise PipelineError("Проект ещё не подготовлен.")

    def _require_initialized(self, project: ProjectRecord) -> None:
        if not (project.project_dir / "control" / "lookbook-state.json").is_file():
            raise PipelineError("Контроллер ещё не инициализирован; сначала завершите карту.")


class ReviewRequired(PipelineError):
    pass


def _read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as source:
        return [{key: (value or "").strip() for key, value in row.items()} for row in csv.DictReader(source, delimiter="\t")]


def _write_tsv(path: Path, rows: list[dict[str, str]]) -> None:
    if not rows:
        raise ValueError("Нельзя записать пустой TSV.")
    fields = list(rows[0].keys())
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=fields, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def _latest_cards(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    candidates = [path for path in root.rglob("LOOK_*.jpg") if path.is_file()]
    if not candidates:
        return []
    by_parent: dict[Path, list[Path]] = {}
    for path in candidates:
        by_parent.setdefault(path.parent, []).append(path)
    parent = max(by_parent, key=lambda path: max(item.stat().st_mtime_ns for item in by_parent[path]))
    return sorted(by_parent[parent], key=lambda path: path.stem)


def _path_batches(paths: list[Path], *, size: int) -> list[list[Path]]:
    """Split proof paths into fixed, non-overlapping review assignments."""
    if size < 1:
        raise ValueError("Batch size must be positive.")
    return [paths[index : index + size] for index in range(0, len(paths), size)]


def _row_batches(rows: list[dict[str, str]], *, size: int) -> list[list[dict[str, str]]]:
    """Split a fixed TSV order into bounded, independent proof assignments."""
    if size < 1:
        raise ValueError("Batch size must be positive.")
    return [rows[index : index + size] for index in range(0, len(rows), size)]


def _pair_batches(rows: list[tuple[str, str]], *, size: int) -> list[list[tuple[str, str]]]:
    if size < 1:
        raise ValueError("Batch size must be positive.")
    return [rows[index : index + size] for index in range(0, len(rows), size)]


def _safe_confirmation_note(note: str) -> str:
    """Keep controller's ``LOOK=note||LOOK=note`` transport unambiguous."""
    cleaned = " ".join(str(note).replace("||", ";").replace("\r", " ").replace("\n", " ").split())
    if len(cleaned) < 25:
        raise ValueError("Визуальное подтверждение должно содержать конкретное наблюдение не короче 25 символов.")
    return cleaned


def _clearance_plan_digest(plan: dict[str, object]) -> str:
    """Return only the signed geometry that can change a visual retry.

    Timestamps and archive locations change on every controller call, even
    when a plan would apply exactly the same crop/frame positions.  They must
    not reset the autonomous convergence guard.
    """
    return json.dumps(
        {
            "corrections": plan.get("corrections", []),
            "caption_corrections": plan.get("caption_corrections", []),
            "unresolved": plan.get("unresolved", []),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _composition_checkpoint_signature(root: Path) -> str | None:
    """Return only durable composition progress, never a timestamp.

    The controller's progress files may be rewritten while a worker reports a
    failure.  ``updated_at`` alone is not progress: the completed LOOK IDs and
    the signed plan identities must change before another native batch is
    allowed.
    """
    snapshots: list[dict[str, object]] = []
    progress_root = root / "control" / "progress"
    for name in ("composition.json", "composition-delta.json"):
        path = progress_root / name
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        completed_looks = payload.get("completed_looks")
        if not isinstance(completed_looks, list):
            items = payload.get("items")
            completed_looks = [
                item.get("look_id")
                for item in items
                if isinstance(item, dict) and isinstance(item.get("look_id"), str)
            ] if isinstance(items, list) else []
        snapshots.append(
            {
                "file": name,
                "gate": payload.get("gate"),
                "nonce": payload.get("nonce"),
                "completed_count": payload.get("completed_count", len(completed_looks)),
                "completed_looks": sorted(str(value) for value in completed_looks),
                "composition_plan": payload.get("composition_plan_sha256"),
                "correction_plan": payload.get("clearance_correction_plan_sha256"),
                "baseline": payload.get("baseline_composition_sha256"),
            }
        )
    if not snapshots:
        return None
    return json.dumps(snapshots, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _normalise_observation_error(value: str) -> str:
    """Make repeated worker failures comparable without timestamps/whitespace."""
    compact = " ".join(str(value).split())
    return re.sub(r"\b\d{4}-\d{2}-\d{2}[T ][0-9:.+-]+\b", "<time>", compact)


def _look_ids_in_text(value: str) -> list[str]:
    """Return de-duplicated LOOK IDs in the order reported by a controller."""
    return list(dict.fromkeys(re.findall(r"\bLOOK_\d{3,}\b", str(value).upper())))


def _parse_codex_batch_decisions(raw: str, expected_looks: list[str]) -> dict[str, VisionDecision]:
    """Validate a no-write Codex batch response before any controller mutation."""
    text = str(raw).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ProviderError("Codex не вернул JSON с результатом независимой визуальной сверки.")
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError as error:
        raise ProviderError("Codex вернул некорректный JSON для независимой визуальной сверки.") from error
    raw_decisions = payload.get("decisions") if isinstance(payload, dict) else None
    if not isinstance(raw_decisions, list):
        raise ProviderError("В ответе Codex отсутствует список decisions.")
    expected = set(expected_looks)
    parsed: dict[str, VisionDecision] = {}
    for item in raw_decisions:
        if not isinstance(item, dict):
            raise ProviderError("Codex вернул некорректную запись решения.")
        look_id = str(item.get("look_id", "")).strip()
        if look_id not in expected or look_id in parsed:
            raise ProviderError("Codex вернул лишний или повторённый LOOK в независимом пакете.")
        accepted = item.get("accepted", item.get("match"))
        if not isinstance(accepted, bool):
            raise ProviderError(f"{look_id}: поле accepted должно быть true или false.")
        note = _safe_confirmation_note(str(item.get("note", "")))
        parsed[look_id] = VisionDecision(accepted=accepted, note=note, raw=text)
    if set(parsed) != expected:
        missing = ", ".join(sorted(expected - set(parsed)))
        raise ProviderError("Codex не вернул решение для всех назначенных LOOK: " + missing)
    return parsed


def _credit_override_batches(
    assignments: list[tuple[str, str, str]], current: dict[str, dict[str, str]],
) -> list[list[tuple[str, str, str]]]:
    """Keep an Excel-card swap atomic while respecting the five-look limit."""
    desired = {look_id: (sheet.casefold(), number) for look_id, sheet, number in assignments}
    if len(desired) != len(assignments):
        raise ReviewRequired("Один LOOK_### указан в ручных правках кредитов больше одного раза.")
    owners = {
        (row.get("excel_sheet", "").casefold(), row.get("excel_look_number", "")): look_id
        for look_id, row in current.items()
    }
    graph: dict[str, set[str]] = {look_id: set() for look_id in desired}
    for look_id, pair in desired.items():
        owner = owners.get(pair)
        if owner and owner != look_id:
            if owner not in desired:
                raise ReviewRequired(
                    f"{look_id}: карточка Excel уже принадлежит {owner}. Добавьте полную взаимную замену, а не копию карточки."
                )
            graph[look_id].add(owner)
            graph[owner].add(look_id)
    assignment_by_look = {look_id: (look_id, sheet, number) for look_id, sheet, number in assignments}
    seen: set[str] = set()
    batches: list[list[tuple[str, str, str]]] = []
    for start in graph:
        if start in seen:
            continue
        stack = [start]
        component: list[str] = []
        while stack:
            look_id = stack.pop()
            if look_id in seen:
                continue
            seen.add(look_id)
            component.append(look_id)
            stack.extend(graph[look_id] - seen)
        if len(component) > 5:
            raise ReviewRequired(
                "Ручная перестановка затрагивает больше пяти связанных луков. Разбейте её на независимые правки или "
                "выполните один контролируемый цикл в интерфейсе после следующего обновления."
            )
        batches.append([assignment_by_look[look_id] for look_id in sorted(component)])
    return batches
