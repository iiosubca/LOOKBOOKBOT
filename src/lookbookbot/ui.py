from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

from PySide6.QtCore import QDate, QObject, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .config import ToolPaths
from .controller import read_json
from .discovery import discover_sources, infer_output_root
from .domain import ProviderKind, STAGES, StageStatus, project_code
from .pipeline import PipelineEngine
from .project_import import ExistingProjectError, open_existing_project
from .providers import ProviderError, make_provider
from .secrets import get_google_api_key, save_google_api_key
from .state import StateStore
from .visual_audit import load_visual_audit


STATUS_ICON = {
    StageStatus.PENDING.value: "○",
    StageStatus.RUNNING.value: "◌",
    StageStatus.PASSED.value: "✓",
    StageStatus.REVIEW.value: "!",
    StageStatus.FAILED.value: "×",
    StageStatus.BLOCKED.value: "■",
}

STATUS_COLOR = {
    StageStatus.PENDING.value: "#7c8798",
    StageStatus.RUNNING.value: "#f59e0b",
    StageStatus.PASSED.value: "#10b981",
    StageStatus.REVIEW.value: "#f59e0b",
    StageStatus.FAILED.value: "#ef4444",
    StageStatus.BLOCKED.value: "#ef4444",
}


def _asset_path(filename: str) -> Path:
    """Return a bundled asset both from source and a PyInstaller build."""
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        return Path(bundle_root) / "assets" / filename
    return Path(__file__).resolve().parents[1] / "assets" / filename


class PipelineWorker(QObject):
    log = Signal(str)
    stage = Signal(str, str, str)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, store: StateStore, project_id: str, start_key: str | None, continue_after: bool) -> None:
        super().__init__()
        self.store = store
        self.project_id = project_id
        self.start_key = start_key
        self.continue_after = continue_after

    def run(self) -> None:
        try:
            project = self.store.get_project(self.project_id)
            engine = PipelineEngine(
                self.store,
                log=self.log.emit,
                progress=lambda key, status, message: self.stage.emit(key, status.value, message),
            )
            result = engine.run(project, self.start_key, continue_after=self.continue_after)
        except Exception as error:
            self.failed.emit(str(error))
        else:
            self.finished.emit(result)


class ImagePreview(QLabel):
    def __init__(self, placeholder: str) -> None:
        super().__init__(placeholder)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(260, 360)
        self.setStyleSheet("background:#ffffff;border:1px solid #d8d8d2;color:#6d6d68;")
        self._path: Path | None = None

    def set_image(self, path: Path | None) -> None:
        self._path = path
        if path is None or not path.is_file():
            self.setPixmap(QPixmap())
            self.setText("Изображение не найдено")
            return
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            self.setPixmap(QPixmap())
            self.setText(path.name)
            return
        self.setText("")
        self.setPixmap(pixmap.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        if self._path:
            self.set_image(self._path)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.store = StateStore()
        self.tools = ToolPaths.defaults()
        self.project = self.store.active_project()
        self.worker_thread: QThread | None = None
        self.worker: PipelineWorker | None = None
        self.stage_items: dict[str, QListWidgetItem] = {}
        self._loading_credits = False
        self.visual_progress_timer = QTimer(self)
        self.visual_progress_timer.setInterval(750)
        self.visual_progress_timer.timeout.connect(self._refresh_visual_progress)
        self.final_export_timer = QTimer(self)
        self.final_export_timer.setInterval(750)
        self.final_export_timer.timeout.connect(self._refresh_final_export_progress)
        self.setWindowTitle("LOOKBOOKBOT")
        self.resize(1500, 940)
        self.setMinimumSize(1180, 760)
        self._build_ui()
        self._apply_style()
        self._load_settings()
        self._load_project()

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(14)

        header = QHBoxLayout()
        header.setSpacing(16)
        self.logo_label = QLabel()
        self.logo_label.setObjectName("BrandLogo")
        logo = QPixmap(str(_asset_path("lbb-logo.png")))
        if not logo.isNull():
            self.logo_label.setPixmap(
                logo.scaled(
                    108,
                    66,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        self.logo_label.setFixedSize(108, 66)
        self.logo_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        title = QLabel("LOOKBOOKBOT")
        title.setObjectName("Title")
        subtitle = QLabel("Управляемая вёрстка лукбука в Adobe InDesign")
        subtitle.setObjectName("Muted")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addWidget(self.logo_label)
        header.addLayout(title_box)
        header.addStretch()
        self.project_label = QLabel("Проект не выбран")
        self.project_label.setObjectName("ProjectBadge")
        header.addWidget(self.project_label)
        outer.addLayout(header)

        setup = QFrame()
        setup.setObjectName("Card")
        setup_layout = QVBoxLayout(setup)
        setup_layout.setContentsMargins(18, 14, 18, 14)
        setup_layout.setSpacing(10)
        self.source_edit = QLineEdit()
        self.source_edit.setPlaceholderText("Папка с PDF, Excel и hires")
        self.source_edit.setMinimumWidth(380)
        browse = QPushButton("Выбрать папку")
        browse.clicked.connect(self._browse_sources)
        self.date_edit = QDateEdit(QDate.currentDate())
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("dd.MM.yyyy")
        self.provider_combo = QComboBox()
        self.provider_combo.addItem("Codex", ProviderKind.CODEX.value)
        self.provider_combo.addItem("Google AI Studio", ProviderKind.GOOGLE.value)
        self.provider_combo.addItem("Ollama", ProviderKind.OLLAMA.value)
        self.provider_combo.addItem("llama.cpp", ProviderKind.LLAMACPP.value)
        self.provider_combo.currentIndexChanged.connect(self._provider_changed)
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.setMinimumWidth(180)
        self.google_key_label = QLabel("Google API key")
        self.google_key_edit = QLineEdit()
        self.google_key_edit.setPlaceholderText("Ключ AI Studio")
        self.google_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.google_key_edit.setMinimumWidth(170)
        self.google_quota = QLabel()
        self.google_quota.setObjectName("Muted")
        test_provider = QPushButton("Проверить модель")
        test_provider.clicked.connect(self._test_provider)
        create = QPushButton("Создать / открыть проект")
        create.setObjectName("Primary")
        create.clicked.connect(self._save_project)
        existing = QPushButton("Открыть готовый проект")
        existing.clicked.connect(self._open_existing_project)
        # Source selection stays wide on its own row. The provider controls
        # and the project actions fit together on the second row.
        source_row = QHBoxLayout()
        source_row.setSpacing(10)
        source_label = QLabel("Исходники")
        source_label.setObjectName("FormLabel")
        source_row.addWidget(source_label)
        source_row.addWidget(self.source_edit, 1)
        source_row.addWidget(browse)
        source_row.addSpacing(14)
        source_row.addWidget(QLabel("Дата"))
        source_row.addWidget(self.date_edit)

        model_row = QHBoxLayout()
        model_row.setSpacing(10)
        provider_label = QLabel("ИИ")
        provider_label.setObjectName("FormLabel")
        model_row.addWidget(provider_label)
        model_row.addWidget(self.provider_combo)
        model_row.addWidget(self.model_combo)
        model_row.addSpacing(14)
        model_row.addWidget(self.google_key_label)
        model_row.addWidget(self.google_key_edit)
        model_row.addWidget(self.google_quota, 1)
        model_row.addWidget(test_provider)
        model_row.addWidget(create)
        model_row.addWidget(existing)

        setup_layout.addLayout(source_row)
        setup_layout.addLayout(model_row)
        outer.addWidget(setup)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        outer.addWidget(splitter, 1)

        sidebar = QFrame()
        sidebar.setObjectName("Card")
        sidebar.setMinimumWidth(320)
        sidebar.setMaximumWidth(400)
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(14, 14, 14, 14)
        stage_heading = QLabel("ЭТАПЫ ВЫПУСКА")
        stage_heading.setObjectName("SidebarTitle")
        side_layout.addWidget(stage_heading)
        self.stage_list = QListWidget()
        self.stage_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.stage_list.itemSelectionChanged.connect(self._stage_selected)
        for stage in STAGES:
            item = QListWidgetItem(f"○  {stage.title}")
            item.setData(Qt.ItemDataRole.UserRole, stage.key)
            item.setToolTip(stage.description)
            item.setSizeHint(QSize(280, 44))
            self.stage_list.addItem(item)
            self.stage_items[stage.key] = item
        side_layout.addWidget(self.stage_list, 1)
        continue_button = QPushButton("ПРОДОЛЖИТЬ С МЕСТА ОСТАНОВКИ")
        continue_button.setObjectName("Continue")
        continue_button.clicked.connect(self._continue_from_checkpoint)
        side_layout.addWidget(continue_button)
        self.approved = QCheckBox("Согласовано — разрешить финальные PDF")
        self.approved.toggled.connect(self._approval_changed)
        side_layout.addWidget(self.approved)
        self.run_one = QCheckBox("Выполнить только выбранный этап")
        side_layout.addWidget(self.run_one)
        self.run_button = QPushButton("▶  ПУСК")
        self.run_button.setObjectName("Run")
        self.run_button.clicked.connect(self._run_pipeline)
        side_layout.addWidget(self.run_button)
        self.visual_progress_label = QLabel()
        self.visual_progress_label.setObjectName("Muted")
        self.visual_progress_label.setWordWrap(True)
        self.visual_progress = QProgressBar()
        self.visual_progress.setRange(0, 1)
        self.visual_progress.setTextVisible(True)
        self.visual_progress.hide()
        self.visual_progress_label.hide()
        side_layout.addWidget(self.visual_progress_label)
        side_layout.addWidget(self.visual_progress)
        self.final_export_label = QLabel()
        self.final_export_label.setObjectName("Muted")
        self.final_export_label.setWordWrap(True)
        self.final_export_progress = QProgressBar()
        self.final_export_progress.setRange(0, 5)
        self.final_export_progress.setTextVisible(True)
        self.final_export_progress.hide()
        self.final_export_label.hide()
        side_layout.addWidget(self.final_export_label)
        side_layout.addWidget(self.final_export_progress)
        splitter.addWidget(sidebar)

        self.tabs = QTabWidget()
        self.overview_tab = self.tabs.addTab(self._build_overview(), "ОБЗОР")
        self.looks_tab = self.tabs.addTab(self._build_looks(), "СПИСОК ЛУКОВ")
        self.credits_tab = self.tabs.addTab(self._build_credits(), "СПИСОК КРЕДИТОВ")
        self.visual_tab = self.tabs.addTab(self._build_visual_audit(), "ВИЗУАЛЬНАЯ ПРОВЕРКА")
        self.log_tab = self.tabs.addTab(self._build_log(), "ЖУРНАЛ")
        splitter.addWidget(self.tabs)
        splitter.setStretchFactor(1, 1)

    def _build_overview(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 18, 18, 18)
        self.stage_title = QLabel("ВЫБЕРИТЕ ПРОЕКТ")
        self.stage_title.setObjectName("SectionTitle")
        self.stage_description = QLabel("Программа сохраняет каждый подтверждённый этап и продолжает с первой незавершённой строки.")
        self.stage_description.setWordWrap(True)
        self.stage_description.setObjectName("Muted")
        self.project_path = QLineEdit()
        self.project_path.setReadOnly(True)
        open_project = QPushButton("Открыть папку проекта")
        open_project.clicked.connect(self._open_project_folder)
        self.source_report = QTextEdit()
        self.source_report.setReadOnly(True)
        self.source_report.setPlaceholderText("После выбора исходников здесь появится проверка комплекта.")
        self.source_report.setMaximumHeight(200)
        layout.addWidget(self.stage_title)
        layout.addWidget(self.stage_description)
        layout.addSpacing(12)
        layout.addWidget(QLabel("Папка выпуска"))
        row = QHBoxLayout()
        row.addWidget(self.project_path, 1)
        row.addWidget(open_project)
        layout.addLayout(row)
        layout.addWidget(QLabel("Проверка комплекта"))
        layout.addWidget(self.source_report)
        layout.addStretch()
        return page

    def _build_looks(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        toolbar = QHBoxLayout()
        hint = QLabel("PDF определяет порядок. Слева всегда полный рост, справа — клоузап.")
        hint.setObjectName("Muted")
        save = QPushButton("Сохранить ручные правки")
        save.clicked.connect(self._save_look_edits)
        swap = QPushButton("Поменять фото в выбранной строке")
        swap.clicked.connect(self._swap_selected_look)
        toolbar.addWidget(hint, 1)
        toolbar.addWidget(swap)
        toolbar.addWidget(save)
        layout.addLayout(toolbar)
        content = QSplitter(Qt.Orientation.Vertical)
        self.looks_table = QTableWidget(0, 7)
        self.looks_table.setHorizontalHeaderLabels(["№", "LOOK", "PDF", "Полный рост — слева", "Клоузап — справа", "Статус", "Примечание"])
        self.looks_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.looks_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.looks_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.looks_table.itemSelectionChanged.connect(self._preview_selected_look)
        content.addWidget(self.looks_table)
        previews = QWidget()
        preview_layout = QHBoxLayout(previews)
        self.left_preview = ImagePreview("Полный рост")
        self.right_preview = ImagePreview("Клоузап")
        preview_layout.addWidget(self.left_preview)
        preview_layout.addWidget(self.right_preview)
        content.addWidget(previews)
        content.setStretchFactor(0, 2)
        content.setStretchFactor(1, 3)
        layout.addWidget(content, 1)
        return page

    def _build_credits(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        toolbar = QHBoxLayout()
        hint = QLabel("Отметьте галочкой только спорные луки: повторно сопоставляться будут лишь они, остальные CONFIRMED останутся замороженными.")
        hint.setObjectName("Muted")
        rematch = QPushButton("↻ Повторно сопоставить отмеченные")
        rematch.clicked.connect(self._run_targeted_credit_rematch)
        save = QPushButton("Сохранить ручные правки")
        save.clicked.connect(self._save_credit_edits)
        toolbar.addWidget(hint, 1)
        toolbar.addWidget(rematch)
        toolbar.addWidget(save)
        layout.addLayout(toolbar)
        content = QSplitter(Qt.Orientation.Horizontal)
        self.credits_table = QTableWidget(0, 7)
        self.credits_table.setHorizontalHeaderLabels(["Повторить", "LOOK", "Лист Excel", "№ карточки", "Статус", "Примечание", "Доказательство"])
        self.credits_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.credits_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.credits_table.itemSelectionChanged.connect(self._preview_selected_credit)
        self.credits_table.itemChanged.connect(self._highlight_credit_duplicates_from_table)
        self.credits_table.itemChanged.connect(self._credit_rematch_checkbox_changed)
        content.addWidget(self.credits_table)
        self.credit_preview = ImagePreview("Трёхпанельная проверка")
        content.addWidget(self.credit_preview)
        content.setStretchFactor(0, 3)
        content.setStretchFactor(1, 2)
        layout.addWidget(content, 1)
        return page

    def _build_visual_audit(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        toolbar = QHBoxLayout()
        self.visual_audit_summary = QLabel("Визуальный аудит ещё не запускался.")
        self.visual_audit_summary.setObjectName("Muted")
        self.refresh_visual_audit = QPushButton("Обновить результаты")
        self.refresh_visual_audit.clicked.connect(self._load_visual_audit)
        toolbar.addWidget(self.visual_audit_summary, 1)
        toolbar.addWidget(self.refresh_visual_audit)
        layout.addLayout(toolbar)
        content = QSplitter(Qt.Orientation.Horizontal)
        self.visual_audit_table = QTableWidget(0, 4)
        self.visual_audit_table.setHorizontalHeaderLabels(["LOOK", "Статус", "Причина", "Доказательство"])
        self.visual_audit_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.visual_audit_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.visual_audit_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.visual_audit_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.visual_audit_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.visual_audit_table.itemSelectionChanged.connect(self._preview_selected_visual_audit)
        content.addWidget(self.visual_audit_table)
        self.visual_audit_preview = ImagePreview("Выберите лук в таблице")
        content.addWidget(self.visual_audit_preview)
        content.setStretchFactor(0, 3)
        content.setStretchFactor(1, 2)
        layout.addWidget(content, 1)
        return page

    def _build_log(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFont(QFont("Cascadia Mono", 9))
        layout.addWidget(self.log_view)
        return page

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background:#f5f5f2; color:#111111;
                font-family:'TSUM Circe', 'Circe', Arial, sans-serif; font-size:14px;
            }
            QFrame#Card, QTabWidget::pane { background:#ffffff; border:1px solid #d8d8d2; border-radius:0; }
            QLabel#Title {
                font-family:'TSUM Circe Bold', 'Circe Bold', Arial, sans-serif; font-size:32px;
                font-weight:700; letter-spacing:0.8px; color:#111111;
            }
            QLabel#SectionTitle {
                font-family:'TSUM Circe Bold', 'Circe Bold', Arial, sans-serif;
                font-size:26px; font-weight:700; letter-spacing:0.4px; color:#111111;
            }
            QLabel#SidebarTitle {
                background:transparent; font-family:'TSUM Circe Bold', 'Circe Bold', Arial, sans-serif;
                font-size:18px; font-weight:700; letter-spacing:0.3px; color:#111111;
            }
            QLabel#Muted { color:#6d6d68; }
            QLabel#BrandLogo { background:transparent; }
            QLabel#FormLabel { background:transparent; font-family:'TSUM Circe Bold', 'Circe Bold', Arial, sans-serif; font-weight:700; }
            QLabel#ProjectBadge {
                background:#111111; color:#ffffff; border:1px solid #111111;
                padding:9px 14px; border-radius:0; font-weight:700; letter-spacing:0.3px;
            }
            QLineEdit, QComboBox, QDateEdit, QTextEdit, QTableWidget, QListWidget {
                background:#ffffff; color:#111111; border:1px solid #cfcfca; border-radius:0; padding:7px;
                selection-background-color:#111111; selection-color:#ffffff;
            }
            QComboBox QAbstractItemView { background:#ffffff; color:#111111; selection-background-color:#111111; }
            QPushButton {
                background:#ffffff; color:#111111; border:1px solid #111111;
                border-radius:0; padding:9px 13px; font-family:'TSUM Circe Bold', 'Circe Bold', Arial, sans-serif; font-weight:700;
            }
            QPushButton:hover { background:#111111; color:#ffffff; }
            QPushButton#Primary { background:#111111; color:#ffffff; border-color:#111111; font-weight:700; }
            QPushButton#Primary:hover { background:#e84b16; border-color:#e84b16; }
            QPushButton#Continue {
                background:#111111; color:#ffffff; border-color:#111111;
                font-family:'TSUM Circe Bold', 'Circe Bold', Arial, sans-serif; font-weight:700; letter-spacing:0.2px;
            }
            QPushButton#Continue:hover { background:#e84b16; border-color:#e84b16; }
            QPushButton#Run {
                background:#e84b16; color:#ffffff; border:1px solid #e84b16;
                padding:13px; font-size:18px; font-weight:800; letter-spacing:0.5px;
            }
            QPushButton#Run:hover { background:#111111; border-color:#111111; }
            QPushButton#Run:disabled { background:#e3e3de; color:#8b8b84; border-color:#e3e3de; }
            QHeaderView::section {
                background:#efefeb; color:#111111; border:none;
                border-right:1px solid #d8d8d2; border-bottom:1px solid #d8d8d2; padding:8px; font-weight:700;
            }
            QTabBar::tab {
                background:#ffffff; color:#6d6d68; padding:11px 18px;
                border:1px solid #d8d8d2; border-bottom:none; border-radius:0; font-weight:600;
            }
            QTabBar::tab:hover { color:#111111; background:#f0f0ec; }
            QTabBar::tab:selected { color:#ffffff; background:#111111; border-color:#111111; }
            QListWidget::item { border-radius:0; margin:1px 0; padding:7px; }
            QListWidget::item:hover { background:#f0f0ec; }
            QListWidget::item:selected { background:#111111; color:#ffffff; }
            QTableWidget::item { padding:5px; }
            QProgressBar { background:#e8e8e3; border:none; border-radius:0; text-align:center; color:#111111; }
            QProgressBar::chunk { background:#e84b16; }
            QCheckBox { background:transparent; spacing:7px; }
            QCheckBox::indicator { width:15px; height:15px; border:1px solid #111111; background:#ffffff; }
            QCheckBox::indicator:checked { background:#111111; }
            QSplitter::handle { background:#f5f5f2; width:8px; height:8px; }
            """
        )

    def _load_settings(self) -> None:
        source = self.store.get_setting("last_source")
        if source:
            self.source_edit.setText(source)
        provider = self.store.get_setting("provider", ProviderKind.CODEX.value)
        index = self.provider_combo.findData(provider)
        if index >= 0:
            self.provider_combo.setCurrentIndex(index)
        self.model_combo.setCurrentText(self.store.get_setting(f"model_{provider}", ""))
        self.google_key_edit.setText(get_google_api_key())
        self._provider_changed()

    def _load_project(self) -> None:
        self.project = self.store.active_project()
        if self.project is None:
            self._refresh_stages()
            return
        self.project_label.setText(self.project.name)
        self.project_path.setText(str(self.project.project_dir))
        self.source_edit.setText(str(self.project.source_dir))
        self.date_edit.setDate(QDate(self.project.show_date.year, self.project.show_date.month, self.project.show_date.day))
        index = self.provider_combo.findData(self.project.provider.value)
        if index >= 0:
            self.provider_combo.setCurrentIndex(index)
        self.model_combo.setCurrentText(self.project.model)
        self.approved.blockSignals(True)
        self.approved.setChecked(self.store.is_approved(self.project.id))
        self.approved.blockSignals(False)
        self._refresh_stages()
        self._refresh_visual_progress()
        self._load_looks()
        self._load_credits()
        self._load_visual_audit()
        self._load_runs()
        self._refresh_google_quota()

    def _browse_sources(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Выберите папку исходников", self.source_edit.text() or str(Path.home()))
        if not selected:
            return
        self.source_edit.setText(selected)
        self._inspect_sources(Path(selected))

    def _inspect_sources(self, source: Path) -> bool:
        report = discover_sources(source, self.tools)
        self.source_report.setPlainText("\n".join(("✓ " if report.bundle else "! ") + line for line in report.messages))
        return report.bundle is not None

    def _save_project(self) -> None:
        source = Path(self.source_edit.text().strip()).expanduser()
        if not self._inspect_sources(source):
            QMessageBox.warning(self, "Исходники не готовы", self.source_report.toPlainText())
            return
        qdate = self.date_edit.date()
        show_date = date(qdate.year(), qdate.month(), qdate.day())
        output_root = infer_output_root(source)
        project_dir = output_root / project_code(show_date)
        provider = ProviderKind(str(self.provider_combo.currentData()))
        model = self.model_combo.currentText().strip()
        self._save_google_key_if_supplied(provider)
        self.project = self.store.save_project(
            name=project_code(show_date), source_dir=source.resolve(), output_root=output_root,
            project_dir=project_dir, show_date=show_date, provider=provider, model=model,
        )
        self.store.set_setting("last_source", str(source.resolve()))
        self.store.set_setting("provider", provider.value)
        self.store.set_setting(f"model_{provider.value}", model)
        self._append_log(f"Проект открыт: {project_dir}")
        self._load_project()

    def _open_existing_project(self) -> None:
        initial = str(self.project.project_dir.parent) if self.project else self.source_edit.text() or str(Path.home())
        selected = QFileDialog.getExistingDirectory(self, "Выберите папку готового проекта", initial)
        if not selected:
            return
        provider = ProviderKind(str(self.provider_combo.currentData()))
        try:
            self.project = open_existing_project(
                self.store, Path(selected), provider=provider, model=self.model_combo.currentText().strip()
            )
        except ExistingProjectError as error:
            QMessageBox.warning(self, "Не удалось открыть проект", str(error))
            return
        self._append_log(f"Восстановлено из evidence: {self.project.project_dir}")
        self._load_project()

    def _provider_changed(self) -> None:
        kind = ProviderKind(str(self.provider_combo.currentData()))
        saved = self.store.get_setting(f"model_{kind.value}", "")
        self.model_combo.clear()
        defaults = {
            ProviderKind.CODEX: ["", "gpt-5.6-sol", "gpt-5.5"],
            ProviderKind.GOOGLE: ["gemini-3.5-flash-lite"],
            ProviderKind.OLLAMA: [saved] if saved else [],
            ProviderKind.LLAMACPP: [saved or "local"],
        }
        self.model_combo.addItems([item for item in defaults[kind] if item or kind == ProviderKind.CODEX])
        self.model_combo.setCurrentText(saved or defaults[kind][0])
        is_google = kind == ProviderKind.GOOGLE
        self.google_key_label.setVisible(is_google)
        self.google_key_edit.setVisible(is_google)
        self.google_quota.setVisible(is_google)
        if is_google:
            self._refresh_google_quota()

    def _save_google_key_if_supplied(self, provider: ProviderKind) -> None:
        if provider == ProviderKind.GOOGLE and self.google_key_edit.text().strip():
            save_google_api_key(self.google_key_edit.text())

    def _refresh_google_quota(self) -> None:
        usage = self.store.google_usage_status()
        tpm = usage["tpm_used"]
        tpm_text = f"{tpm / 1000:.1f}K" if tpm >= 1000 else str(tpm)
        self.google_quota.setText(
            f"RPM {usage['rpm_used']} / {usage['rpm_limit']}   "
            f"TPM {tpm_text} / 250K   RPD {usage['rpd_used']} / {usage['rpd_limit']}"
        )

    def _test_provider(self) -> None:
        kind = ProviderKind(str(self.provider_combo.currentData()))
        self._save_google_key_if_supplied(kind)
        provider = make_provider(
            kind, self.model_combo.currentText().strip(),
            ollama_endpoint=self.store.get_setting("ollama_endpoint", "http://127.0.0.1:11434"),
            llama_endpoint=self.store.get_setting("llama_endpoint", "http://127.0.0.1:8080"),
            google_api_key=get_google_api_key(),
            usage_store=self.store,
        )
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            message = provider.health()
            models = provider.list_models()
        except ProviderError as error:
            QMessageBox.warning(self, "Подключение не готово", str(error))
            return
        finally:
            QApplication.restoreOverrideCursor()
        if models:
            current = self.model_combo.currentText()
            self.model_combo.clear()
            self.model_combo.addItems(models)
            if current:
                self.model_combo.setCurrentText(current)
        QMessageBox.information(self, "Подключение работает", message)
        self._refresh_google_quota()

    def _run_pipeline(self) -> None:
        if self.project is None:
            QMessageBox.information(self, "Сначала создайте проект", "Выберите исходники, дату и нажмите «Создать / открыть проект».")
            return
        self._save_google_key_if_supplied(ProviderKind(str(self.provider_combo.currentData())))
        selected = self.stage_list.selectedItems()
        start_key = str(selected[0].data(Qt.ItemDataRole.UserRole)) if selected else None
        # A deliberately selected stage is an explicit request to rebuild it
        # (for example, after a human correction or to export the review PDF
        # again). Continuing with no selection keeps completed stages intact
        # and starts at the first incomplete one.
        if start_key:
            self.store.reset_from(self.project.id, start_key)
            self._refresh_stages()
        self._start_pipeline(start_key, not self.run_one.isChecked())

    def _start_pipeline(self, start_key: str | None, continue_after: bool) -> None:
        if self.project is None:
            return
        self.run_button.setEnabled(False)
        self.run_button.setText("ВЫПОЛНЯЕТСЯ…")
        self.worker_thread = QThread(self)
        self.worker = PipelineWorker(self.store, self.project.id, start_key, continue_after)
        self.worker.moveToThread(self.worker_thread)
        self.worker_thread.started.connect(self.worker.run)
        self.worker.log.connect(self._append_log)
        self.worker.stage.connect(self._worker_stage)
        self.worker.finished.connect(self._worker_finished)
        self.worker.failed.connect(self._worker_failed)
        self.worker.finished.connect(self.worker_thread.quit)
        self.worker.failed.connect(self.worker_thread.quit)
        self.worker_thread.finished.connect(self.worker.deleteLater)
        self.worker_thread.finished.connect(self.worker_thread.deleteLater)
        self.tabs.setCurrentIndex(self.log_tab)
        self.worker_thread.start()

    def _continue_from_checkpoint(self) -> None:
        self.stage_list.clearSelection()
        self.run_one.setChecked(False)
        self._run_pipeline()

    def _worker_stage(self, key: str, status: str, message: str) -> None:
        self._append_log(f"[{key}] {message}")
        if key == "final" and status == StageStatus.RUNNING.value:
            self._begin_final_export_progress()
        elif key == "final" and status == StageStatus.PASSED.value:
            self._refresh_final_export_progress()
        if key == "visual" and status == StageStatus.RUNNING.value:
            self._begin_visual_progress()
        elif key == "visual" and status == StageStatus.PASSED.value:
            self._refresh_visual_progress()
        self._refresh_stages()

    def _worker_finished(self, result) -> None:
        self._append_log(result.message)
        self._finish_worker()
        if result.stopped_at:
            QMessageBox.warning(self, "Этап требует внимания", result.message)
        else:
            QMessageBox.information(self, "Выполнение завершено", result.message)

    def _worker_failed(self, message: str) -> None:
        self._append_log("ОШИБКА: " + message)
        self._finish_worker()
        QMessageBox.critical(self, "Ошибка выполнения", message)

    def _finish_worker(self) -> None:
        if self.visual_progress_timer.isActive():
            self.visual_progress_timer.stop()
        if self.final_export_timer.isActive():
            self.final_export_timer.stop()
        self._refresh_visual_progress()
        self._refresh_final_export_progress()
        self.run_button.setEnabled(True)
        self.run_button.setText("▶  ПУСК")
        self._load_project()

    def _begin_visual_progress(self) -> None:
        self.visual_progress.setRange(0, 1)
        self.visual_progress.setValue(0)
        self.visual_progress.show()
        self.visual_progress_label.setText("Визуальная проверка: подготавливаются proof-развороты…")
        self.visual_progress_label.show()
        self._refresh_visual_progress()
        self.visual_progress_timer.start()

    def _refresh_visual_progress(self) -> None:
        if self.project is None:
            self.visual_progress.hide()
            self.visual_progress_label.hide()
            return
        rows = self.store.stage_rows(self.project.id)
        status = rows.get("visual", {}).get("status")
        state = read_json(self.project.project_dir / "control" / "lookbook-state.json")
        total = int(state.get("look_count", 0) or 0)
        confirmations = self.project.project_dir / "control" / "visual" / "confirmations"
        complete = len(list(confirmations.glob("LOOK_*.json"))) if confirmations.is_dir() else 0
        if status != StageStatus.RUNNING.value:
            if self.visual_progress_timer.isActive():
                self.visual_progress_timer.stop()
            self.visual_progress.hide()
            self.visual_progress_label.hide()
            return
        self.visual_progress.show()
        self.visual_progress_label.show()
        self.visual_progress.setRange(0, max(total, 1))
        self.visual_progress.setValue(min(complete, total))
        if total and complete >= total:
            self.visual_progress_label.setText(f"Визуальная проверка: {complete} из {total} — фиксируется итоговый PASS…")
        elif total:
            self.visual_progress_label.setText(f"Визуальная проверка: подтверждено {complete} из {total} разворотов")
        else:
            self.visual_progress_label.setText("Визуальная проверка: создаются proof-развороты…")

    def _begin_final_export_progress(self) -> None:
        self.final_export_progress.setRange(0, 5)
        self.final_export_progress.setValue(0)
        self.final_export_progress.show()
        self.final_export_label.setText("Финальные PDF: подготовка экспорта…")
        self.final_export_label.show()
        self._refresh_final_export_progress()
        self.final_export_timer.start()

    def _refresh_final_export_progress(self) -> None:
        if self.project is None:
            return
        manifest = read_json(self.project.project_dir / "control" / "final-deliverables.json")
        outputs = manifest.get("outputs")
        if not isinstance(outputs, list) or not outputs:
            return
        total = len(outputs)
        complete = sum(1 for output in outputs if isinstance(output, dict) and output.get("identity") and output.get("verified_at"))
        current = next((output for output in outputs if isinstance(output, dict) and not output.get("identity")), None)
        self.final_export_progress.setRange(0, total)
        self.final_export_progress.setValue(complete)
        self.final_export_progress.show()
        self.final_export_label.show()
        if str(manifest.get("status", "")).casefold() == "complete" and complete == total:
            self.final_export_label.setText(f"Финальные PDF готовы: {complete} из {total}")
            return
        if isinstance(current, dict):
            filename = Path(str(current.get("path", "PDF"))).name
            self.final_export_label.setText(f"Экспорт PDF {complete + 1} из {total}: {filename}")
        else:
            self.final_export_label.setText(f"Проверка финальных PDF: {complete} из {total}")

    def _refresh_stages(self) -> None:
        rows = self.store.stage_rows(self.project.id) if self.project else {}
        for stage in STAGES:
            status = rows.get(stage.key, {}).get("status", StageStatus.PENDING.value)
            item = self.stage_items[stage.key]
            item.setText(f"{STATUS_ICON.get(status, '○')}  {stage.title}")
            item.setForeground(QColor(STATUS_COLOR.get(status, "#e8edf5")))
            error = rows.get(stage.key, {}).get("error", "")
            item.setToolTip(error or stage.description)

    def _stage_selected(self) -> None:
        selected = self.stage_list.selectedItems()
        if not selected:
            self.stage_title.setText("ПРОДОЛЖЕНИЕ С МЕСТА ОСТАНОВКИ")
            self.stage_description.setText("Пуск начнёт работу с первой незавершённой строки.")
            return
        key = str(selected[0].data(Qt.ItemDataRole.UserRole))
        stage = next(stage for stage in STAGES if stage.key == key)
        self.stage_title.setText(stage.title.upper())
        self.stage_description.setText(stage.description)
        if key == "looks":
            self.tabs.setCurrentIndex(self.looks_tab)
        elif key == "credits_map":
            self.tabs.setCurrentIndex(self.credits_tab)
        elif key == "visual":
            self.tabs.setCurrentIndex(self.visual_tab)
        else:
            self.tabs.setCurrentIndex(self.overview_tab)

    def _load_looks(self) -> None:
        rows = self.store.looks(self.project.id) if self.project else []
        self.looks_table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            values = [row["spread_order"], row["look_id"], row["pdf_spread"], row["left_filename"], row["right_filename"], row["status"], row["note"]]
            for column, value in enumerate(values):
                item = QTableWidgetItem("" if value is None else str(value))
                if column in (0, 1, 2, 5):
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.looks_table.setItem(index, column, item)

    def _save_look_edits(self) -> None:
        if not self.project:
            return
        current = {row["look_id"]: row for row in self.store.looks(self.project.id)}
        changed = 0
        for row in range(self.looks_table.rowCount()):
            look_id = self.looks_table.item(row, 1).text()
            before = current.get(look_id, {})
            left = self.looks_table.item(row, 3).text().strip()
            right = self.looks_table.item(row, 4).text().strip()
            note = self.looks_table.item(row, 6).text().strip()
            if left == before.get("left_filename") and right == before.get("right_filename") and note == before.get("note", ""):
                continue
            self.store.update_look(
                self.project.id, look_id,
                left_filename=left,
                right_filename=right,
                status="manual",
                note=note,
            )
            changed += 1
        if changed:
            self.store.reset_from(self.project.id, "looks")
            self._refresh_stages()
            self._append_log(f"Ручные правки списка луков сохранены: {changed}. Следующий запуск начнётся с их контролируемой проверки.")

    def _swap_selected_look(self) -> None:
        row = self.looks_table.currentRow()
        if row < 0:
            return
        left = self.looks_table.item(row, 3).text()
        right = self.looks_table.item(row, 4).text()
        self.looks_table.item(row, 3).setText(right)
        self.looks_table.item(row, 4).setText(left)
        self._save_look_edits()
        self._preview_selected_look()

    def _preview_selected_look(self) -> None:
        if not self.project or self.looks_table.currentRow() < 0:
            return
        row = self.looks_table.currentRow()
        hires = self.project.project_dir / "control" / "work" / "_mat" / "hires"
        self.left_preview.set_image(hires / self.looks_table.item(row, 3).text())
        self.right_preview.set_image(hires / self.looks_table.item(row, 4).text())

    def _load_credits(self) -> None:
        rows = self.store.credits(self.project.id) if self.project else []
        self._loading_credits = True
        try:
            self.credits_table.setRowCount(len(rows))
            for index, row in enumerate(rows):
                checkbox = QTableWidgetItem()
                checkbox.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsSelectable)
                checkbox.setCheckState(
                    Qt.CheckState.Checked if row.get("needs_rematch") else Qt.CheckState.Unchecked
                )
                checkbox.setToolTip("Отметьте, если этот лук нужно сопоставить с Excel заново. Остальные строки не будут повторно проверяться.")
                self.credits_table.setItem(index, 0, checkbox)
                values = [row["look_id"], row["excel_sheet"], row["excel_look_number"], row["visual_status"], row["note"], row["evidence_file"]]
                for column, value in enumerate(values, start=1):
                    item = QTableWidgetItem(str(value or ""))
                    if column in (1, 4, 6):
                        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    self.credits_table.setItem(index, column, item)
        finally:
            self._loading_credits = False
        self._highlight_credit_duplicates_from_table()
        self._style_credit_statuses(rows)

    def _save_credit_edits(self) -> None:
        if not self.project:
            return
        duplicates = self._credit_table_duplicates()
        self._highlight_credit_duplicates_from_table()
        if duplicates:
            detail = "; ".join(f"{sheet} / {number}: {', '.join(looks)}" for (sheet, number), looks in duplicates.items())
            QMessageBox.warning(self, "Повтор карточки Excel", f"Исправьте красные строки. Одна карточка Excel не может быть назначена двум лукам:\n{detail}")
            return
        edits: list[dict[str, str]] = []
        for row in range(self.credits_table.rowCount()):
            look_id = self.credits_table.item(row, 1).text()
            edits.append({
                "look_id": look_id,
                "excel_sheet": self.credits_table.item(row, 2).text().strip(),
                "excel_look_number": self.credits_table.item(row, 3).text().strip(),
                "note": self.credits_table.item(row, 5).text().strip(),
            })
        try:
            changed = self.store.save_credit_overrides(self.project.id, edits)
        except ValueError as error:
            QMessageBox.warning(self, "Неполная правка", str(error))
            return
        if changed:
            self.store.reset_from(self.project.id, "credits_map")
            self._refresh_stages()
            self._load_credits()
            self._append_log(
                f"Ручные правки кредитов сохранены: {changed}. В списке они отмечены CONFIRMED как решение оператора; "
                "перед вёрсткой программа создаст для них новые proof cards и перепроверит их без пересчёта остальных строк."
            )

    def _credit_rematch_checkbox_changed(self, item: QTableWidgetItem) -> None:
        if self._loading_credits or not self.project or item.column() != 0:
            return
        look_item = self.credits_table.item(item.row(), 1)
        if look_item is None:
            return
        requested = item.checkState() == Qt.CheckState.Checked
        self.store.set_credit_rematch_requested(self.project.id, look_item.text(), requested)
        self._style_credit_statuses(self.store.credits(self.project.id))

    def _run_targeted_credit_rematch(self) -> None:
        if not self.project:
            return
        targets = self.store.requested_credit_rematches(self.project.id)
        if not targets:
            QMessageBox.information(
                self,
                "Нет отмеченных луков",
                "Поставьте галочки рядом с лукaми, которые нужно сопоставить с Excel заново.",
            )
            return
        self.store.reset_from(self.project.id, "credits_map")
        self._refresh_stages()
        self._append_log("Запущена точечная повторная сверка: " + ", ".join(targets))
        # The rematch worker intentionally stops after the credit stage so
        # the operator can review only the corrected proof cards before any
        # later controller gate or InDesign work begins.
        self._start_pipeline("credits_map", continue_after=False)

    def _credit_table_duplicates(self) -> dict[tuple[str, str], list[str]]:
        pairs: dict[tuple[str, str], list[str]] = {}
        for row in range(self.credits_table.rowCount()):
            sheet = self.credits_table.item(row, 2).text().strip()
            number = self.credits_table.item(row, 3).text().strip()
            if not sheet or not number:
                continue
            pairs.setdefault((sheet.casefold(), number), []).append(self.credits_table.item(row, 1).text())
        return {pair: looks for pair, looks in pairs.items() if len(looks) > 1}

    def _highlight_credit_duplicates_from_table(self, *_args) -> None:
        if self._loading_credits:
            return
        duplicates = self._credit_table_duplicates()
        duplicate_looks = {look_id for looks in duplicates.values() for look_id in looks}
        for row in range(self.credits_table.rowCount()):
            look_id = self.credits_table.item(row, 1).text()
            is_duplicate = look_id in duplicate_looks
            for column in (2, 3):
                item = self.credits_table.item(row, column)
                item.setBackground(QColor("#7f1d1d") if is_duplicate else QColor("#0c121d"))
                item.setForeground(QColor("#fecaca") if is_duplicate else QColor("#e8edf5"))
                item.setToolTip("Эта пара «лист Excel + № карточки» уже назначена другому луку." if is_duplicate else "")

    def _style_credit_statuses(self, rows: list[dict]) -> None:
        for index, row in enumerate(rows):
            item = self.credits_table.item(index, 4)
            confirmed = str(row.get("visual_status", "")).upper() == "CONFIRMED"
            item.setForeground(QColor("#34d399") if confirmed else QColor("#fbbf24"))
            if row.get("needs_rematch"):
                item.setForeground(QColor("#fbbf24"))
                item.setToolTip("Отмечено для точечной повторной сверки. До нажатия кнопки карта не меняется.")
            if row.get("manual_override"):
                item.setToolTip("Подтверждено вручную оператором; контроллер создаст новую proof-card перед применением в InDesign.")

    def _preview_selected_credit(self) -> None:
        if not self.project or self.credits_table.currentRow() < 0:
            return
        evidence = self.credits_table.item(self.credits_table.currentRow(), 6).text()
        self.credit_preview.set_image(self.project.project_dir / evidence if evidence else None)

    def _load_visual_audit(self) -> None:
        rows = load_visual_audit(self.project.project_dir) if self.project else []
        self.visual_audit_table.setRowCount(len(rows))
        blocked = 0
        for index, row in enumerate(rows):
            values = [row.look_id, row.status, row.reason, row.evidence_image]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 1:
                    is_clear = row.status == "CLEAR"
                    item.setForeground(QColor("#34d399") if is_clear else QColor("#f87171"))
                    if not is_clear:
                        blocked += 1
                if column == 3:
                    item.setToolTip("Выберите строку, чтобы открыть это доказательство справа.")
                self.visual_audit_table.setItem(index, column, item)
        if not rows:
            self.visual_audit_summary.setText("Controller-evidence визуального аудита ещё не создан.")
            self.visual_audit_preview.set_image(None)
        elif blocked:
            self.visual_audit_summary.setText(f"Требуют внимания: {blocked} из {len(rows)} луков. Выберите строку для просмотра доказательства.")
        else:
            self.visual_audit_summary.setText(f"Clearance-аудит: все {len(rows)} луков CLEAR. Если этап всё ещё не пройден, откройте «Журнал»: не записан PASS visual.")

    def _preview_selected_visual_audit(self) -> None:
        if not self.project or self.visual_audit_table.currentRow() < 0:
            return
        evidence = self.visual_audit_table.item(self.visual_audit_table.currentRow(), 3).text()
        self.visual_audit_preview.set_image(self.project.project_dir / evidence if evidence else None)

    def _load_runs(self) -> None:
        self.log_view.clear()
        if not self.project:
            return
        for run in self.store.recent_runs(self.project.id, 50):
            self.log_view.append(f"[{run['started_at']}] {run['stage_key']} — {run['status']}\n{run['output']}\n")

    def _append_log(self, text: str) -> None:
        self.log_view.append(text)
        scrollbar = self.log_view.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _approval_changed(self, checked: bool) -> None:
        if self.project:
            self.store.set_approved(self.project.id, checked)
            if checked:
                self.store.reset_from(self.project.id, "final")
            self._refresh_stages()

    def _open_project_folder(self) -> None:
        if self.project and self.project.project_dir.exists():
            os.startfile(self.project.project_dir)  # type: ignore[attr-defined]

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self.worker_thread and self.worker_thread.isRunning():
            answer = QMessageBox.question(
                self, "Операция выполняется",
                "Закрытие окна не должно прерывать текущую операцию InDesign. Оставить программу открытой?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if answer == QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        super().closeEvent(event)
