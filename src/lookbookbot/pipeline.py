from __future__ import annotations

import csv
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import ToolPaths
from .controller import CommandError, CommandRunner, LookbookController, evidence_passed, final_outputs_passed
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
from .providers import CodexProvider, ModelProvider, ProviderError, make_provider
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

    def run(self, project: ProjectRecord, start_key: str | None = None, *, continue_after: bool = True) -> PipelineResult:
        keys = [stage.key for stage in STAGES]
        start_key = start_key or self.store.first_incomplete_stage(project.id)
        if start_key not in keys:
            raise PipelineError(f"Неизвестный этап: {start_key}")
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
            if not continue_after:
                break
        return PipelineResult(tuple(completed), None, "Все доступные этапы завершены.")

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
            self._delegate_codex(project, provider, "credits_map")
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
            self._delegate_codex(project, provider, "credits_rematch")
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
            self._delegate_codex(project, provider, "map")
        else:
            self._confirm_local_reference_proofs(project, provider)
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
            self._delegate_codex(project, provider, "visual")
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
        pdf = review_pdf_filename(project.show_date)
        self.controller.gate("pre-export", root, "--pdf", pdf, "--quarantine-existing", timeout=180)
        self.controller.gate("export-pdf", root, "--pdf", pdf, timeout=1800)
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
            "credits_map": """Заверши только визуальное сопоставление кредитов текущего проекта. Используй lookbook-layout skill. PDF-порядок уже находится в control/work/look-register.tsv. Если существует control/work/ui-overrides/caption-overrides.json, это вручную подтверждённые оператором выборы Excel: каждый такой LOOK_### обязан получить exact alternative proof, быть реально просмотрен на нём, затем выбран только через штатный select-alternatives в безопасной группе не более пяти связанных LOOK_###. Никогда не заменяй этот выбор автоматическим seed. После выбора перерисуй обычные трёхпанельные proof cards, просмотри их партиями не более пяти и подтверди только реально просмотренные совпадения. Закончи с нулём PENDING. Не переходи к init или InDesign.""",
            "credits_rematch": """Выполни только точечную повторную сверку кредитов. Список единственных разрешённых LOOK_### находится в control/work/ui-overrides/targeted-credit-rematch.json. Все остальные строки caption-map.tsv заморожены: не открывай для них proof cards, не подтверждай, не меняй лист/номер/статус и не запускай seed или reset-visual-review. Для каждого target LOOK визуально сравни его PDF-пару с контролируемыми Excel previews и выбери карту по одежде, цвету, аксессуарам, обуви, сумке, позе и модели — никогда по порядковому номеру. Если подходящая карта уже назначена другому target LOOK, выполни полный обмен только внутри этой связанной группы (максимум пять LOOK) через alternative-proofs и select-alternatives; карту у неотмеченного LOOK брать запрещено. Для каждого нового или оставленного выбора создай/просмотри точную proof-card и подтверди только этот target через confirm-review с конкретными визуальными признаками. Закончи, когда все и только target LOOK получат CONFIRMED. Не переходи к init, InDesign или следующим этапам.""",
            "map": """Заверши только текущий gate map этого проекта по lookbook-layout skill. Просмотри все reference-order cards партиями не более пяти, в той же итерации запиши immutable confirm-reference-look для просмотренных LOOK_### и доведи счётчик до N/N, затем выполни validate-map. Не начинай structure.""",
            "visual": """Заверши только gate visual текущего проекта по lookbook-layout skill. Не меняй страницы и фреймы. Проверь full-left/close-right, ссылки, CREDiTs, overflow, safe area с внутренним отступом 12 pt и caption clearance. Разрешены только горизонтальный сдвиг изображения и, если он не помогает, перенос того же кредитного фрейма вниз/вправо/вниз-вправо. Для единичной проблемы используй targeted calibration. Подтверди каждый реально просмотренный current-master proof и запиши PASS visual; не экспортируй review PDF.""",
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

    def _run_local_visual(self, project: ProjectRecord, provider: ModelProvider) -> None:
        root = project.project_dir
        arm = self.controller.gate("arm", root, "--gate", "visual", timeout=120, check=False)
        if arm.returncode and "armed" not in arm.text.casefold():
            self.log(arm.text)
        self.controller.script("prepare_composition_audit.py", root, timeout=600)
        for _ in range(100):
            result = self.controller.gate("apply-composition", root, timeout=1800, check=False)
            if result.returncode:
                raise PipelineError(result.text)
            if "PASS composition" in result.text or "PASS COMPOSITION" in result.text.upper():
                break
            if "CHECKPOINT" not in result.text.upper():
                break
        for _ in range(6):
            rendered = self.controller.gate("render-visual-proof", root, timeout=1800, check=False)
            if rendered.returncode == 0:
                break
            plan = self.controller.gate("plan-clearance-corrections", root, timeout=900, check=False)
            if plan.returncode:
                raise ReviewRequired(plan.text or rendered.text)
            for _batch in range(100):
                applied = self.controller.gate("apply-composition", root, timeout=1800, check=False)
                if applied.returncode:
                    raise PipelineError(applied.text)
                if "PASS composition" in applied.text or "CHECKPOINT" not in applied.text.upper():
                    break
        else:
            raise ReviewRequired("Caption clearance не удалось довести до CLEAR за шесть контролируемых итераций.")
        pairs = _latest_cards(root / "control" / "visual" / "proof" / "pairs")
        for proof in pairs:
            decision = provider.inspect_proof(
                "Проверь разворот лукбука. Ответь JSON {\"match\":true/false,\"note\":\"конкретное наблюдение\"}. "
                "Требования: полный рост слева, клоузап справа; кредиты читаемы, не обрезаны, не пересекают модель и находятся внутри safe area.",
                [proof],
            )
            if not decision.accepted:
                raise ReviewRequired(f"{proof.stem}: визуальная модель отклонила разворот — {decision.note}")
            self.controller.gate("confirm-visual-look", root, "--look", proof.stem, "--note", decision.note, timeout=120)
        self.controller.gate("record-visual", root, "--notes", "Проверены все current-master proofs и компьютерный caption clearance.", timeout=300)

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
