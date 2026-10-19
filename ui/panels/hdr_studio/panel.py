"""HDR Studio : inspection HDR d'une piste vidéo (formats, cohérence, courbes de luminance).

Niveau 1 (instantané) : en-têtes déjà lus par l'inspection. Niveau 2 (à la demande) :
RPU Dolby Vision et HDR10+ extraits et analysés, sans décoder l'image ; résultat en cache.
"""
from __future__ import annotations

import dataclasses
import subprocess
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from core.file_types import is_accepted
from core.hdr_inspection import (
    DynamicInspector,
    Finding,
    InspectionCache,
    InspectionCancelled,
    InspectionReport,
    Severity,
    audit,
    build_snapshot,
)
from core.hdr_inspection import summary
from core.hdr_inspection.compare import Side
from core.hdr_inspection.realign import Realignment, realign
from core.hdr_inspection.fel_analyzer import CaptureLayer
from core.hdr_inspection.measure import ImageMeasurement, read_measurement
from core.hdr_inspection.patch import MetadataPatcher, PatchCancelled, PatchPlan
from core.hdr_inspection.report import write_report
from core.hdr_inspection.snapshot import HdrSnapshot
from core.i18n import translate_text
from core.inspector import FileInfo, FileInspector
from core.runner import TaskSignals, ToolRunner
from core.workdir import create_process_work_dir
from ui.design_system import colors as C, font_px, scale
from ui.operation_bar import OperationState
from ui.panels.hdr_studio.chart import format_time
from ui.panels.hdr_studio.compare_tab import CompareTab, DropLineEdit
from ui.panels.hdr_studio.editor import EditionTab
from ui.panels.hdr_studio.fel_tab import FelTab
from ui.panels.hdr_studio.luminance import LuminanceTab
from ui.shutdown import Shutdown, defer_close
from ui.styles import _card, _combo_style, _input_style, _primary_button, _secondary_button, _section_label

_SEVERITY_COLORS = {Severity.OK: "OK", Severity.INFO: "INFO", Severity.WARN: "WARN", Severity.ERROR: "ERROR"}
_SEVERITY_ICONS = {Severity.OK: "✓", Severity.INFO: "i", Severity.WARN: "!", Severity.ERROR: "✕"}


def _label(text: str, *, color: str | None = None, size: int = 11, bold: bool = False, wrap: bool = False) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(wrap)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    label.setStyleSheet(f"color:{color or C.TEXT_PRI};font-size:{font_px(size)}px;"
                        f"font-weight:{700 if bold else 400};background:transparent;")
    return label


def _kv_grid(rows: list[tuple[str, str]]) -> QWidget:
    widget = QWidget()
    widget.setStyleSheet("background:transparent;")
    grid = QGridLayout(widget)
    grid.setContentsMargins(0, 0, 0, 0)
    grid.setHorizontalSpacing(scale(16))
    grid.setVerticalSpacing(scale(5))
    for row, (key, value) in enumerate(rows):
        grid.addWidget(_label(key, color=C.TEXT_SEC), row, 0, Qt.AlignmentFlag.AlignTop)
        grid.addWidget(_label(value, wrap=True), row, 1)
    grid.setColumnStretch(1, 1)
    return widget


def _section(title: str, body: QWidget) -> QWidget:
    card = _card()
    layout = QVBoxLayout(card)
    layout.setContentsMargins(scale(14), scale(10), scale(14), scale(12))
    layout.setSpacing(scale(8))
    layout.addWidget(_section_label(title))
    layout.addWidget(body)
    return card


def _table(headers: list[str], rows: list[list[str]]) -> QTableWidget:
    table = QTableWidget(len(rows), len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
    table.horizontalHeader().setStretchLastSection(True)
    table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    table.setStyleSheet(
        f"QTableWidget{{background:{C.BG_CARD};color:{C.TEXT_PRI};border:1px solid {C.BORDER};"
        f"gridline-color:{C.BORDER};font-size:{font_px(11)}px;}}"
        f"QHeaderView::section{{background:{C.BG_CARD};color:{C.TEXT_SEC};border:none;"
        f"border-bottom:1px solid {C.BORDER};padding:{scale(4)}px {scale(8)}px;font-size:{font_px(10)}px;font-weight:700;}}")
    for r, values in enumerate(rows):
        for c, value in enumerate(values):
            table.setItem(r, c, QTableWidgetItem(value))
    table.resizeColumnsToContents()
    visible = min(len(rows), 8)
    table.setFixedHeight(table.horizontalHeader().height() + sum(table.rowHeight(r) for r in range(visible)) + scale(4))
    return table


def _badge(text: str, color: str) -> QLabel:
    badge = QLabel(text)
    badge.setStyleSheet(f"color:{color};border:1px solid {color};border-radius:{scale(4)}px;"
                        f"padding:{scale(2)}px {scale(8)}px;font-size:{font_px(10)}px;font-weight:700;background:transparent;")
    return badge


def _finding_row(finding: Finding) -> QWidget:
    row = QWidget()
    row.setStyleSheet("background:transparent;")
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, scale(2), 0, scale(2))
    layout.setSpacing(scale(10))
    color = getattr(C, _SEVERITY_COLORS[finding.severity])
    icon = QLabel(_SEVERITY_ICONS[finding.severity])
    icon.setFixedSize(scale(18), scale(18))
    icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
    icon.setStyleSheet(f"color:{color};border:1px solid {color};border-radius:{scale(9)}px;"
                       f"font-size:{font_px(10)}px;font-weight:700;background:transparent;")
    layout.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
    text = QVBoxLayout()
    text.setSpacing(scale(1))
    text.addWidget(_label(finding.title, wrap=True))
    if finding.detail:
        text.addWidget(_label(finding.detail, color=C.TEXT_SEC, size=10, wrap=True))
    layout.addLayout(text, 1)
    return row


class HdrStudio(QWidget):
    """Page « HDR Studio » : chargement d'une source, analyse à la demande, onglets de résultats."""

    log_message = Signal(str, str)
    busy_changed = Signal(bool)
    # Barre d'exécution globale (écriture du fichier corrigé) : mêmes signaux que les autres studios.
    op_state_changed = Signal(str, str)
    op_progress_pct = Signal(int)
    ready_changed = Signal(bool)
    #: Sortie détaillée des outils (outil, ligne) : journal verbose (niveau debug).
    tool_output = Signal(str, str)
    #: Chemin, index ffprobe de la piste, MaxCLL, MaxFALL.
    apply_to_encode_requested = Signal(object, int, int, int)
    _inspected = Signal(int, object, int, object, str)
    _cache_ready = Signal(int, object)
    _progress = Signal(int, str, int)
    _analyzed = Signal(int, object, str)
    _measured = Signal(int, object, str)
    _patched = Signal(int, object, str)
    _fel_done = Signal(int, object, str)
    _moment_ready = Signal(int, object, str)
    _compare_inspected = Signal(int, object, int, object, str)
    _compare_analyzed = Signal(int, object, str)

    def __init__(self, config, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hdr-studio")
        self._cache = InspectionCache(Path(config.app_data_dir) / "hdr_inspection")
        self._token = 0
        self._info: FileInfo | None = None
        self._report: InspectionReport | None = None
        self._busy = False
        self._cancel = threading.Event()
        self._signals: TaskSignals | None = None
        self._moment_dialog: QWidget | None = None
        self._pending_analyze = False
        self._measurement: ImageMeasurement | None = None
        self._last_output: Path | None = None
        # Piste B de l'onglet Comparer.
        self._compare_info: FileInfo | None = None
        self._compare_token = 0
        # Réalignement sur la mesure, mémorisé (rapport, mesure, pic de l'écran).
        self._realign_key: tuple | None = None
        self._realign_value: Realignment | None = None
        self._patch_token: int | None = None
        self._last_step = ""
        # Moteur de mesure (plugin mvo-fel), remplaçable par les tests.
        from core.fel.engine import FelEngine
        self._engine_factory = FelEngine.installed
        self.setAcceptDrops(True)
        self._build_ui()
        self._inspected.connect(self._on_inspected, Qt.ConnectionType.QueuedConnection)
        self._cache_ready.connect(self._on_cache_ready, Qt.ConnectionType.QueuedConnection)
        self._progress.connect(self._on_progress, Qt.ConnectionType.QueuedConnection)
        self._analyzed.connect(self._on_analyzed, Qt.ConnectionType.QueuedConnection)
        self._measured.connect(self._on_measured, Qt.ConnectionType.QueuedConnection)
        self._patched.connect(self._on_patched, Qt.ConnectionType.QueuedConnection)
        self._fel_done.connect(self._on_fel_done, Qt.ConnectionType.QueuedConnection)
        self._moment_ready.connect(self._on_moment_ready, Qt.ConnectionType.QueuedConnection)
        self._compare_inspected.connect(self._on_compare_inspected, Qt.ConnectionType.QueuedConnection)
        self._compare_analyzed.connect(self._on_compare_analyzed, Qt.ConnectionType.QueuedConnection)
        self._render(None)

    # ── Construction ────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(scale(28), scale(24), scale(28), scale(20))
        outer.setSpacing(scale(12))
        title = QLabel(translate_text("HDR Studio"))
        title.setStyleSheet(f"color:{C.TEXT_PRI};font-size:{font_px(22)}px;font-weight:700;background:transparent;")
        outer.addWidget(title)
        subtitle = QLabel(translate_text(
            "Inspecter le HDR d'une vidéo : formats, cohérence des métadonnées, courbes de luminance."))
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet(f"color:{C.TEXT_SEC};font-size:{font_px(11)}px;background:transparent;")
        outer.addWidget(subtitle)

        source = _card()
        source_layout = QVBoxLayout(source)
        source_layout.setContentsMargins(scale(12), scale(10), scale(12), scale(10))
        source_layout.setSpacing(scale(8))
        source_layout.addWidget(_section_label(translate_text("SOURCE")))
        row = QHBoxLayout()
        row.setSpacing(scale(6))
        self._path_edit = DropLineEdit()
        self._path_edit.setPlaceholderText(translate_text("Déposer une vidéo ici ou choisir un fichier…"))
        self._path_edit.setStyleSheet(_input_style())
        self._path_edit.returnPressed.connect(lambda: self.load_source(Path(self._path_edit.text().strip())))
        self._path_edit.dropped.connect(self.load_source)
        row.addWidget(self._path_edit, 1)
        browse = _secondary_button("…", fixed_width=32)
        browse.setToolTip(translate_text("Choisir un fichier"))
        browse.clicked.connect(self._browse)
        row.addWidget(browse)
        self._track_combo = QComboBox()
        self._track_combo.setStyleSheet(_combo_style())
        self._track_combo.setMinimumWidth(scale(220))
        self._track_combo.currentIndexChanged.connect(self._on_track_changed)
        row.addWidget(self._track_combo)
        source_layout.addLayout(row)
        actions = QHBoxLayout()
        actions.setSpacing(scale(8))
        self._analyze_btn = _primary_button(translate_text("Lire les métadonnées dynamiques"))
        self._analyze_btn.setToolTip(translate_text(
            "Extrait et analyse le RPU Dolby Vision et le HDR10+ : lecture complète du fichier, sans décodage."))
        self._analyze_btn.clicked.connect(lambda: self.analyze())
        actions.addWidget(self._analyze_btn)
        self._refresh_btn = _secondary_button(translate_text("Relire le fichier"))
        self._refresh_btn.setToolTip(translate_text("Ignore le résultat en cache et relit le fichier."))
        self._refresh_btn.clicked.connect(lambda: self.analyze(refresh=True))
        actions.addWidget(self._refresh_btn)
        self._cancel_btn = _secondary_button(translate_text("Annuler"))
        self._cancel_btn.clicked.connect(self.cancel)
        actions.addWidget(self._cancel_btn)
        self._open_output_btn = _secondary_button(translate_text("Inspecter le fichier corrigé"))
        self._open_output_btn.clicked.connect(self._open_output)
        self._open_output_btn.setVisible(False)
        actions.addWidget(self._open_output_btn)
        self._export_btn = _secondary_button(translate_text("Exporter le rapport…"))
        self._export_btn.setToolTip(translate_text(
            "Page HTML autonome ou JSON : contrôles, métadonnées, courbes, FEL Analyzer et comparaison."))
        self._export_btn.clicked.connect(lambda: self.export_report())
        actions.addWidget(self._export_btn)
        self._status = _label("", color=C.TEXT_SEC)
        actions.addWidget(self._status, 1)
        source_layout.addLayout(actions)
        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setTextVisible(False)
        self._progress_bar.setFixedHeight(scale(4))
        self._progress_bar.setStyleSheet(
            f"QProgressBar{{background:{C.BG_ACTIVE};border:none;border-radius:2px;}}"
            f"QProgressBar::chunk{{background:{C.ACCENT};border-radius:2px;}}")
        source_layout.addWidget(self._progress_bar)
        outer.addWidget(source)

        self._tabs = QTabWidget()
        self._tabs.setDocumentMode(True)
        self._tabs.setStyleSheet(
            f"QTabWidget::pane{{border:none;background:transparent;}}"
            f"QTabBar::tab{{background:transparent;color:{C.TEXT_SEC};padding:{scale(6)}px {scale(14)}px;"
            f"border:none;border-bottom:2px solid transparent;font-size:{font_px(11)}px;}}"
            f"QTabBar::tab:selected{{color:{C.TEXT_PRI};border-bottom:2px solid {C.ACCENT};}}"
            f"QTabBar::tab:hover{{color:{C.TEXT_PRI};}}")
        self._overview = self._scroll_tab(translate_text("Vue d'ensemble"))
        self._luminance = LuminanceTab(int(getattr(self._config, "display_peak_nits", 1000) or 1000))
        self._luminance.measure_requested.connect(lambda fast: self.measure(fast=fast))
        self._luminance.display_peak_changed.connect(self._display_peak_changed)
        self._luminance.apply_to_encode_requested.connect(self._apply_to_encode)
        self._luminance.prepare_patch_requested.connect(self._prepare_patch)
        # Défilement propre à l'onglet : un journal agrandi ne comprime jamais la courbe ni les réglages.
        self._luminance_scroll = self._scroll_area(self._luminance)
        self._tabs.addTab(self._luminance_scroll, translate_text("Luminance"))
        self._dovi_tab = self._scroll_tab(translate_text("Dolby Vision"))
        self._hdr10plus_tab = self._scroll_tab(translate_text("HDR10+"))
        self._fel_tab = FelTab()
        self._fel_tab.analysis_requested.connect(self.analyse_fel)
        self._fel_tab.survival_requested.connect(self.test_fel_survival)
        self._fel_tab.moment_requested.connect(self.open_fel_moment)
        self._fel_scroll = self._scroll_area(self._fel_tab)
        self._tabs.addTab(self._fel_scroll, translate_text("FEL"))
        self._compare_tab = CompareTab()
        self._compare_tab.source_requested.connect(self.load_compare)
        self._compare_tab.track_requested.connect(self.compare_track_changed)
        self._compare_tab.analyze_requested.connect(self.analyze_compare)
        self._compare_scroll = self._scroll_area(self._compare_tab)
        self._tabs.addTab(self._compare_scroll, translate_text("Comparer"))
        self._editor = EditionTab()
        self._luminance.values_changed.connect(self._editor.set_static_values)
        self._editor.luminance_requested.connect(self.show_luminance_tab)
        self._editor.changed.connect(lambda: self.ready_changed.emit(self.operation_state().ready))
        self._editor.mastering_changed.connect(self._luminance.set_mastering_choice)
        self._editor.light_levels_requested.connect(self._luminance.set_values)
        self._tabs.addTab(self._scroll_area(self._editor), translate_text("Édition"))
        outer.addWidget(self._tabs, 1)

    @staticmethod
    def _scroll_area(widget: QWidget) -> QScrollArea:
        """Onglet dans sa propre zone de défilement (contenu jamais comprimé sous sa taille minimale)."""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea{background:transparent;border:none;}")
        scroll.setWidget(widget)
        return scroll

    def _realignment(self, report: InspectionReport | None) -> Realignment | None:
        """Réalignement sur la mesure (mémorisé par rapport, mesure et pic de l'écran)."""
        measurement = self._measurement
        if report is None or measurement is None:
            return None
        peak = float(getattr(self._config, "display_peak_nits", 1000) or 1000)
        key = (id(report), id(measurement), peak)
        if self._realign_key != key:
            self._realign_key, self._realign_value = key, realign(report, measurement, peak)
        return self._realign_value

    def show_luminance_tab(self) -> None:
        self._tabs.setCurrentWidget(self._luminance_scroll)

    def _scroll_tab(self, title: str) -> QVBoxLayout:
        body = QWidget()
        body.setStyleSheet("background:transparent;")
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, scale(10), 0, 0)
        layout.setSpacing(scale(10))
        self._tabs.addTab(self._scroll_area(body), title)
        return layout

    # ── Source ──────────────────────────────────────────────────────────
    def _browse(self) -> None:
        start = self._path_edit.text().strip() or str(Path.home())
        path, _ = QFileDialog.getOpenFileName(self, translate_text("Choisir une vidéo"), start)
        if path:
            self.load_source(Path(path))

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        for url in event.mimeData().urls():
            if url.isLocalFile():
                self.load_source(Path(url.toLocalFile()))
                break

    def load_source(self, path: Path, stream_index: int | None = None, *, analyze: bool = False) -> None:
        """Charge ``path`` (piste ``stream_index`` si donnée) ; ``analyze`` : lance le niveau 2 ensuite."""
        path = Path(path)
        if not path.is_file() or not is_accepted(path):
            self._notify("ERROR", translate_text("Fichier vidéo introuvable ou non pris en charge : {name}",
                                                 name=path.name))
            return
        self._stop_running()
        if self._moment_dialog is not None:
            self._moment_dialog.close()  # moment de l'ancienne source
        self._token += 1
        token = self._token
        self._info = None
        self._report = None
        self._measurement = None
        self._pending_analyze = analyze
        self._path_edit.setText(str(path))
        self._track_combo.blockSignals(True)
        self._track_combo.clear()
        self._track_combo.blockSignals(False)
        self._render(None)
        self._set_busy(True, translate_text("Inspection de {name}…", name=path.name))
        self._progress_bar.setRange(0, 0)
        ffprobe, mediainfo = str(self._config.tool_ffprobe), str(self._config.tool_mediainfo)

        def task() -> None:
            try:
                info = FileInspector(ffprobe, mediainfo).inspect(path)
                video = info.video_tracks
                index = next((t.index for t in video if t.index == stream_index), video[0].index if video else 0)
                cached = self._cached(path, index) if video else None
                self._inspected.emit(token, info, index, cached, "")
            except Exception as exc:  # inspection impossible : message à l'utilisateur
                self._inspected.emit(token, None, 0, None, str(exc))

        self._executor.submit(task)

    def _on_inspected(self, token: int, info: FileInfo | None, index: int, cached: object, error: str) -> None:
        if token != self._token:
            return
        self._set_busy(False)
        if info is None or not info.video_tracks:
            self._notify("ERROR", error or translate_text("Aucune piste vidéo."))
            return
        self._info = info
        self._track_combo.blockSignals(True)
        for track in info.video_tracks:
            self._track_combo.addItem(
                f"#{track.index} · {track.codec.upper()} {track.resolution} · {track.hdr_label}", track.index)
            if track.index == index:
                self._track_combo.setCurrentIndex(self._track_combo.count() - 1)
        self._track_combo.blockSignals(False)
        self._show_snapshot(cached)
        self.log_message.emit("INFO", translate_text("Inspection HDR : {name}", name=info.path.name))
        if self._pending_analyze and not (self._report is not None and self._report.dynamic):
            self.analyze()
        self._pending_analyze = False

    def _current_stream(self) -> int | None:
        data = self._track_combo.currentData()
        return int(data) if data is not None else None

    def _on_track_changed(self, _index: int) -> None:
        if self._info is None:
            return
        self._stop_running()
        self._token += 1
        token, info, stream = self._token, self._info, self._current_stream()
        snapshot = build_snapshot(info, stream)
        self._render(InspectionReport(snapshot, findings=audit(snapshot)))

        self._measurement = None

        def task() -> None:
            self._cache_ready.emit(token, self._cached(Path(info.path), snapshot.stream_index))

        self._executor.submit(task)

    def _on_cache_ready(self, token: int, cached: object) -> None:
        if token == self._token and self._info is not None:
            self._show_snapshot(cached)

    def _cached(self, path: Path, stream_index: int) -> dict:
        """Résultats en cache (thread de travail) : métadonnées dynamiques et mesure de l'image."""
        return {"dynamic": self._cache.load(path, stream_index),
                "measure": self._cache.load_measurement(path, stream_index)}

    def _show_snapshot(self, cached: object) -> None:
        assert self._info is not None
        snapshot = build_snapshot(self._info, self._current_stream())
        dynamic = cached.get("dynamic") if isinstance(cached, dict) else None
        if isinstance(cached, dict) and isinstance(cached.get("measure"), ImageMeasurement):
            self._measurement = cached["measure"]
        if isinstance(dynamic, tuple):
            dovi, hdr10plus = dynamic
            report = InspectionReport(snapshot, dovi, hdr10plus, audit(snapshot, dovi, hdr10plus, self._measurement),
                                      dynamic=True, from_cache=True)
        else:
            report = InspectionReport(snapshot, findings=audit(snapshot, measurement=self._measurement))
        self._render(report)
        self._update_actions()

    # ── Analyse dynamique ───────────────────────────────────────────────
    def _dynamic_task(self, info: FileInfo, stream: int | None, refresh: bool, token: int,
                      cancel: threading.Event, signals: TaskSignals) -> InspectionReport | None:
        """Niveau 2 dans le thread de travail (progression sur ``token``) ; None si annulé."""
        runner = ToolRunner(max_workers=1)
        span = [0.0, 1.0]

        def on_step(label: str, start: float, part: float) -> None:
            span[:] = [start, part]
            self._progress.emit(token, label, int(start * 100))

        def on_pct(pct: int) -> None:
            self._progress.emit(token, "", int((span[0] + span[1] * max(0, min(100, pct)) / 100) * 100))

        def run(cmd: object) -> object:
            return runner._run_cmd(cmd, label="hdr-inspection", progress_cb=self._tool_line(cmd),  # type: ignore[arg-type]
                                   progress_pct_cb=on_pct, signals=signals)

        owned = None
        try:
            owned = create_process_work_dir(self._config.ensure_work_dir(), process_name="hdr-inspection")
            inspector = DynamicInspector(
                ffmpeg_bin=str(self._config.tool_ffmpeg), dovi_tool_bin=str(self._config.tool_dovi_tool),
                hdr10plus_bin=str(self._config.tool_hdr10plus), run_cmd=run, work_dir=owned.path,
                cache=self._cache, on_step=on_step, cancelled=cancel.is_set)
            return inspector.analyze(info, stream, refresh=refresh)
        except InspectionCancelled:
            return None
        finally:
            if owned is not None:
                try:
                    owned.remove()
                except OSError:
                    pass  # repris par le nettoyage de démarrage (marqueur conservé)

    def analyze(self, *, refresh: bool = False) -> None:
        if self._info is None or self._busy:
            return
        info, stream = self._info, self._current_stream()
        snapshot = build_snapshot(info, stream)
        if snapshot.dovi is None and not snapshot.hdr10plus:
            self._notify("INFO", translate_text("Aucune métadonnée dynamique (Dolby Vision, HDR10+) sur cette piste."))
            return
        self._token += 1
        token = self._token
        # Un événement par analyse : celle qu'on annule s'arrête même si une autre est lancée aussitôt.
        cancel = threading.Event()
        self._cancel = cancel
        signals = TaskSignals()
        self._signals = signals

        def task() -> None:
            try:
                self._analyzed.emit(token, self._dynamic_task(info, stream, refresh, token, cancel, signals), "")
            except Exception as exc:  # échec d'outil : affiché, l'instantané reste visible
                self._analyzed.emit(token, None, str(exc) or exc.__class__.__name__)

        self._set_busy(True, translate_text("Analyse des métadonnées dynamiques…"))
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self.log_message.emit("INFO", translate_text("Lecture des métadonnées dynamiques : {name}", name=info.path.name))
        self._executor.submit(task)

    def _on_progress(self, token: int, label: str, pct: int) -> None:
        if token != self._token:
            return
        if label:
            self._status.setText(label)
            if label != self._last_step:
                self._last_step = label
                self.log_message.emit("INFO", label)
                if token == self._patch_token:
                    self.op_state_changed.emit("step", label)
        value = max(self._progress_bar.value(), pct)
        self._progress_bar.setValue(value)
        if token == self._patch_token:
            self.op_progress_pct.emit(value)

    def _tool_line(self, cmd: object) -> Callable[[str], None]:
        """Lignes d'un outil (commande comprise) vers le journal verbose, étiquetées par l'outil."""
        name = Path(str(cmd[0])).name if isinstance(cmd, list) and cmd else "hdr-studio"
        return lambda line: self.tool_output.emit(name, line)

    def _notify(self, level: str, text: str) -> None:
        """Message visible dans la ligne d'état et dans le journal d'exécution, au niveau donné."""
        if not text:
            return
        self._set_status(text, error=level == "ERROR")
        self.log_message.emit(level, text)

    def _on_analyzed(self, token: int, report: InspectionReport | None, error: str) -> None:
        if token != self._token:
            return
        self._signals = None
        self._set_busy(False)
        if report is not None and self._measurement is not None:
            report = dataclasses.replace(report, findings=audit(report.snapshot, report.dovi, report.hdr10plus,
                                                                 self._measurement))
        if report is None:
            if error:
                self._notify("ERROR", translate_text("Analyse HDR impossible : {error}", error=error))
            else:
                self._notify("WARN", translate_text("Analyse annulée."))
            return
        self._render(report)
        for message in report.errors:
            self.log_message.emit("WARN", message)
        warnings = sum(1 for f in report.findings if f.severity in (Severity.WARN, Severity.ERROR))
        self.log_message.emit("OK" if not warnings else "WARN", translate_text(
            "Analyse HDR terminée : {count} point(s) à vérifier", count=warnings))
        self._update_actions()

    # ── Mesure de l'image ───────────────────────────────────────────────
    def measure(self, fast: bool = False) -> None:
        """Niveau 3 : mesure de l'image (BL lue comme HDR10) par le plugin mvo-fel.

        ``fast`` : images clés seulement (plugin ≥ 0.5.0), estimation en moins d'une minute pour un film.
        """
        if self._info is None or self._busy:
            return
        info, stream = self._info, self._current_stream()
        snapshot = build_snapshot(info, stream)
        if snapshot.codec.lower() != "hevc" or not snapshot.is_pq:
            self._notify("WARN", translate_text("Mesure réservée aux pistes HEVC PQ (HDR10, Dolby Vision, HDR10+)."))
            return
        if snapshot.dovi is not None and snapshot.dovi.profile == 5:
            self._notify("WARN", translate_text("Profil 5 : aucune couche HDR10 à mesurer."))
            return
        try:
            engine = self._engine_factory()
        except Exception as exc:  # extension absente ou incompatible : message à l'utilisateur
            self._notify("ERROR", translate_text("Mesure impossible : {error}", error=str(exc)))
            return
        from core.fel.engine import measure_sampling_supported, measure_supported
        if not measure_supported(engine, "base"):
            self._notify("ERROR", translate_text(
                "Mesure de l'image : extension mvo-fel 0.4.0 ou plus récente requise (page Extensions)."))
            return
        if fast and not measure_sampling_supported(engine, "keyframes"):
            self._notify("ERROR", translate_text(
                "Mesure rapide : extension mvo-fel 0.5.0 ou plus récente requise (page Extensions)."))
            return
        if fast and Path(info.path).suffix.lower() in (".hevc", ".h265", ".265", ".x265"):
            self._notify("WARN", translate_text("Mesure rapide : flux HEVC brut sans horodatages, mesure complète "
                                                "nécessaire."))
            return
        self._token += 1
        token = self._token
        cancel = threading.Event()
        self._cancel = cancel
        self._signals = None
        config = self._config
        fps = snapshot.fps or 24000 / 1001
        frames = snapshot.frames or (int(round(snapshot.duration_s * fps)) if snapshot.duration_s else None)
        total = max(1, frames or 1)

        def task() -> None:
            owned = None
            try:
                owned = create_process_work_dir(config.ensure_work_dir(), process_name="hdr-mesure")
                statistics = owned.path / "mesure.jsonl"
                # Mesure rapide : la progression reçoit la position dans le flux (images).
                backend = engine.measure(
                    Path(info.path), snapshot.stream_index, statistics, layer="base",
                    threads=max(2, int(getattr(config, "ffmpeg_threads", 8) or 8)),
                    progress=lambda frames: self._progress.emit(token, "", min(99, int(100 * frames / total))),
                    cancelled=cancel, sampling="keyframes" if fast else "all")
                measurement = read_measurement(statistics, "base", total_frames=frames, fps=fps,
                                               cancelled=cancel.is_set)
                self._cache.store_measurement(Path(info.path), snapshot.stream_index, measurement)
                self.log_message.emit("INFO", translate_text("Mesure de l'image : {backend}", backend=backend))
                self._measured.emit(token, measurement, "")
            except Exception as exc:
                self._measured.emit(token, None, "" if cancel.is_set() else (str(exc) or exc.__class__.__name__))
            finally:
                if owned is not None:
                    try:
                        owned.remove()
                    except OSError:
                        pass

        self._set_busy(True, translate_text("Mesure rapide (images clés)…") if fast
                       else translate_text("Mesure de chaque image…"))
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self.log_message.emit("INFO", translate_text("Mesure de l'image : {name}", name=info.path.name))
        self._executor.submit(task)

    def _on_measured(self, token: int, measurement: ImageMeasurement | None, error: str) -> None:
        if token != self._token:
            return
        self._set_busy(False)
        if measurement is None:
            self._notify("ERROR" if error else "WARN",
                         translate_text("Mesure de l'image impossible : {error}", error=error) if error
                         else translate_text("Mesure annulée."))
            return
        self._measurement = measurement
        report = self._report
        if report is not None:
            report = dataclasses.replace(report, findings=audit(report.snapshot, report.dovi, report.hdr10plus,
                                                                 measurement))
        self._render(report)
        if measurement.is_estimate:
            self._set_status(translate_text("Mesure rapide : MaxCLL ≈ {cll}, MaxFALL ≥ {fall} nits (estimation).",
                                            cll=measurement.max_cll(), fall=measurement.max_fall()))
            self.log_message.emit("OK", translate_text(
                "Mesure rapide terminée : {sampled} images clés sur {frames}, MaxCLL ≈ {cll}, MaxFALL ≥ {fall} "
                "(estimation ; mesure complète pour des valeurs exactes).", sampled=measurement.sampled,
                frames=measurement.frames, cll=measurement.max_cll(), fall=measurement.max_fall()))
        else:
            self._set_status(translate_text("Image mesurée : MaxCLL {cll}, MaxFALL {fall} nits.",
                                            cll=measurement.max_cll(), fall=measurement.max_fall()))
            self.log_message.emit("OK", translate_text(
                "Mesure terminée : {frames} images, MaxCLL/MaxFALL {cll}/{fall}.", frames=measurement.frames,
                cll=measurement.max_cll(), fall=measurement.max_fall()))
        realignment = self._realignment(report)
        for proposal in getattr(realignment, "proposals", ()):
            self.log_message.emit("WARN" if proposal.applicable else "INFO", translate_text(
                "Réalignement proposé (onglet Édition) — {title} : {current} → {proposed} ({impact})",
                title=proposal.title, current=proposal.current, proposed=proposal.proposed, impact=proposal.impact))
        self.show_luminance_tab()

    # ── FEL Analyzer ────────────────────────────────────────────────────
    def show_fel_tab(self) -> None:
        self._tabs.setCurrentWidget(self._fel_scroll)

    def _fel_engine(self) -> object | None:
        """Moteur mvo-fel capable de produire les couches comparées ; message à l'utilisateur sinon."""
        try:
            engine = self._engine_factory()
        except Exception as exc:  # extension absente ou incompatible
            self._notify("ERROR", translate_text("FEL Analyzer impossible : {error}", error=str(exc)))
            return None
        from core.fel.engine import layers_supported
        if not (layers_supported(engine, "mapped") and layers_supported(engine, "reconstructed")):
            self._notify("ERROR", translate_text(
                "FEL Analyzer : extension mvo-fel 0.4.0 ou plus récente requise (page Extensions)."))
            return None
        return engine

    def _fel_capture(self, engine: object, snapshot: HdrSnapshot, cancel: threading.Event,
                     device: str = "cpu") -> CaptureLayer:
        from fractions import Fraction

        from core.hdr_inspection.fel_analyzer import plugin_capture

        rate = Fraction(snapshot.fps or 24000 / 1001).limit_denominator(1001)
        return plugin_capture(engine, Path(snapshot.path), snapshot.stream_index,
                              ffmpeg_bin=str(self._config.tool_ffmpeg), width=snapshot.width or 3840,
                              height=snapshot.height or 2160, frame_rate=f"{rate.numerator}/{rate.denominator}",
                              threads=max(2, int(getattr(self._config, "ffmpeg_threads", 8) or 8)), cancelled=cancel,
                              device=device)

    def analyse_fel(self, interval_s: int) -> None:
        """Relevé des couches puis comparaison image reconstruite / couche mapped aux instants échantillonnés."""
        if self._info is None or self._busy or self._report is None:
            return
        snapshot, report = self._report.snapshot, self._report
        dovi = report.dovi
        if dovi is None:
            self._notify("WARN", translate_text(
                "Lire d'abord les métadonnées dynamiques : le type de couche (FEL, MEL) vient du RPU."))
            return
        from core.hdr_inspection.fel_analyzer import nothing_to_measure
        kind = nothing_to_measure(dovi)
        if kind is not None:
            self._notify("INFO", translate_text(
                "{kind} : aucun résiduel, l'image est identique à la lecture P8.1 ; rien à mesurer.", kind=kind))
            return
        engine = self._fel_engine()
        if engine is None:
            return
        self._token += 1
        token = self._token
        cancel = threading.Event()
        self._cancel = cancel
        self._signals = None
        duration = snapshot.duration_s or (snapshot.frames or 0) / (snapshot.fps or 24)
        area = self._fel_area()

        def task() -> None:
            from core.hdr_inspection.fel_analyzer import analyse, sample_times
            from core.hdr_inspection.fel_layer import scan_layers

            try:
                facts = None
                if Path(snapshot.path).suffix.lower() == ".mkv":
                    self._progress.emit(token, translate_text("Relevé des couches BL / EL…"), 0)
                    facts = scan_layers(Path(snapshot.path), snapshot.stream_index, cancelled=cancel.is_set,
                                        progress=lambda f: self._progress.emit(token, "", int(f * 20)))
                self._progress.emit(token, translate_text("Comparaison avec et sans FEL…"), 20)
                result = analyse(self._fel_capture(engine, snapshot, cancel), sample_times(duration, interval_s),
                                 area=area, facts=facts, cancelled=cancel.is_set,
                                 progress=lambda f: self._progress.emit(token, "", 20 + int(f * 80)))
                self._fel_done.emit(token, result, "")
            except Exception as exc:
                self._fel_done.emit(token, None, "" if cancel.is_set() else (str(exc) or exc.__class__.__name__))

        self._set_busy(True, translate_text("Analyse de l'apport du FEL…"))
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self.log_message.emit("INFO", translate_text("FEL Analyzer : {name}", name=Path(snapshot.path).name))
        self._executor.submit(task)

    def _fel_area(self) -> Callable[[float], tuple[int, int, int, int] | None]:
        """Zone active L5 (gauche, droite, haut, bas) en vigueur à un instant, None sans L5."""
        from core.hdr_inspection.fel_analyzer import area_at

        report = self._report
        return area_at(report.dovi if report is not None else None,
                       (report.snapshot.fps if report is not None else None) or 24)

    def test_fel_survival(self, encoder: str, bitrate_kbps: int) -> None:
        """Encode une seconde du moment le plus détaillé et mesure la part de l'apport conservée."""
        fel = self._fel_tab.fel_report()
        if fel is None or not fel.samples or self._busy or self._report is None:
            return
        snapshot = self._report.snapshot
        engine = self._fel_engine()
        if engine is None:
            return
        self._token += 1
        token = self._token
        cancel = threading.Event()
        self._cancel = cancel
        # Rafale d'une seconde : le GPU amortit son initialisation (CPU pour les instants isolés).
        capture = self._fel_capture(engine, snapshot, cancel, device="auto")
        area = self._fel_area()

        def task() -> None:
            from core.hdr_inspection.fel_analyzer import run_survival

            try:
                result = run_survival(capture, fel, ffmpeg_bin=str(self._config.tool_ffmpeg), encoder=encoder,
                                      bitrate_kbps=bitrate_kbps, fps=snapshot.fps or 24000 / 1001, area=area,
                                      duration_s=snapshot.duration_s,
                                      progress=lambda text, value: self._progress.emit(token, text, value))
                self._fel_done.emit(token, dataclasses.replace(fel, survival=result), "")
            except subprocess.CalledProcessError as exc:
                detail = (exc.stderr or b"").decode("utf-8", errors="replace").strip()[-300:]
                self._fel_done.emit(token, None, translate_text("Encodeur {encoder} indisponible : {detail}",
                                                                encoder=encoder, detail=detail or exc))
            except Exception as exc:
                self._fel_done.emit(token, None, "" if cancel.is_set() else (str(exc) or exc.__class__.__name__))

        self._set_busy(True, translate_text("Test de survie à l'encodage…"))
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._executor.submit(task)

    def _on_fel_done(self, token: int, fel: object, error: str) -> None:
        if token != self._token:
            return
        self._set_busy(False)
        from core.hdr_inspection.fel_analyzer import FelReport, recommendation
        if not isinstance(fel, FelReport):
            self._notify("ERROR" if error else "WARN", translate_text("FEL Analyzer : {error}", error=error) if error
                         else translate_text("Analyse annulée."))
            return
        self._fel_tab.set_fel_report(fel)
        verdict, detail = recommendation(fel)
        self._set_status(verdict)
        self.log_message.emit("OK", f"{verdict} — {detail}")
        self.show_fel_tab()

    def open_fel_moment(self, time_s: float) -> None:
        """Capture pleine définition des couches sans / avec FEL à un instant, puis comparateur avant / après."""
        if self._busy or self._report is None:
            return
        snapshot = self._report.snapshot
        engine = self._fel_engine()
        if engine is None:
            return
        self._token += 1
        token = self._token
        cancel = threading.Event()
        self._cancel = cancel
        capture = self._fel_capture(engine, snapshot, cancel)
        area = self._fel_area()

        def task() -> None:
            from core.hdr_inspection.fel_analyzer import active_rows, moment_images

            try:
                reconstructed = capture("reconstructed", time_s, 1)[0]
                self._progress.emit(token, "", 50)
                mapped = capture("mapped", time_s, 1)[0]
                images = moment_images(reconstructed, mapped, active_rows(mapped, area(time_s)), time_s)
                self._moment_ready.emit(token, images, "")
            except Exception as exc:
                self._moment_ready.emit(token, None, "" if cancel.is_set() else (str(exc) or exc.__class__.__name__))

        self._set_busy(True, translate_text("Capture du moment {time} en pleine définition…",
                                            time=format_time(time_s)))
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._executor.submit(task)

    def _on_moment_ready(self, token: int, images: object, error: str) -> None:
        if token != self._token:
            return
        self._set_busy(False)
        from core.hdr_inspection.fel_analyzer import MomentImages
        if not isinstance(images, MomentImages):
            self._notify("ERROR" if error else "WARN", translate_text("FEL Analyzer : {error}", error=error) if error
                         else translate_text("Analyse annulée."))
            return
        from ui.panels.hdr_studio.before_after import MomentCompareDialog
        if self._moment_dialog is not None:
            self._moment_dialog.close()
        dialog = MomentCompareDialog(images, self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.destroyed.connect(lambda _obj=None, d=dialog: self._forget_moment_dialog(d))
        screen = self.screen().availableGeometry() if self.screen() is not None else None
        if screen is not None:
            dialog.resize(int(screen.width() * 0.85), int(screen.height() * 0.85))
        self._moment_dialog = dialog
        dialog.show()

    def _forget_moment_dialog(self, dialog: object) -> None:
        if self._moment_dialog is dialog:
            self._moment_dialog = None

    # ── Comparaison (piste B) ───────────────────────────────────────────
    def show_compare_tab(self) -> None:
        self._tabs.setCurrentWidget(self._compare_scroll)

    def load_compare(self, path: Path, stream_index: int | None = None) -> None:
        """Piste B : inspection rapide, puis métadonnées dynamiques et mesure reprises du cache."""
        path = Path(path)
        if not path.is_file() or not is_accepted(path):
            self._compare_notify("ERROR", translate_text("Fichier vidéo introuvable ou non pris en charge : {name}",
                                                         name=path.name))
            return
        if self._busy:
            self._compare_notify("WARN", translate_text("Opération en cours : réessayer ensuite."))
            return
        self._compare_token += 1
        token = self._compare_token
        self._compare_tab.set_status(translate_text("Inspection de {name}…", name=path.name))
        ffprobe, mediainfo = str(self._config.tool_ffprobe), str(self._config.tool_mediainfo)

        def task() -> None:
            try:
                info = FileInspector(ffprobe, mediainfo).inspect(path)
                video = info.video_tracks
                index = next((t.index for t in video if t.index == stream_index), video[0].index if video else 0)
                self._compare_inspected.emit(token, info, index, self._cached(path, index) if video else None, "")
            except Exception as exc:  # inspection impossible : message à l'utilisateur
                self._compare_inspected.emit(token, None, 0, None, str(exc))

        self._executor.submit(task)

    def compare_track_changed(self, stream_index: int) -> None:
        info = self._compare_info
        if info is None:
            return
        self._compare_token += 1
        token = self._compare_token

        def task() -> None:
            self._compare_inspected.emit(token, info, stream_index, self._cached(Path(info.path), stream_index), "")

        self._executor.submit(task)

    def _on_compare_inspected(self, token: int, info: FileInfo | None, index: int, cached: object, error: str) -> None:
        if token != self._compare_token:
            return
        if info is None or not info.video_tracks:
            self._compare_notify("ERROR", error or translate_text("Aucune piste vidéo."))
            return
        self._compare_info = info
        self._compare_tab.set_tracks(Path(info.path), [
            (track.index, f"#{track.index} · {track.codec.upper()} {track.resolution} · {track.hdr_label}")
            for track in info.video_tracks], index)
        snapshot = build_snapshot(info, index)
        dynamic = cached.get("dynamic") if isinstance(cached, dict) else None
        measurement = cached.get("measure") if isinstance(cached, dict) else None
        measurement = measurement if isinstance(measurement, ImageMeasurement) else None
        if isinstance(dynamic, tuple):
            dovi, hdr10plus = dynamic
            report = InspectionReport(snapshot, dovi, hdr10plus, audit(snapshot, dovi, hdr10plus, measurement),
                                      dynamic=True, from_cache=True)
        else:
            report = InspectionReport(snapshot, findings=audit(snapshot, measurement=measurement))
        self._compare_tab.set_side_b(Side(report, measurement))
        needs_dynamic = not report.dynamic and (snapshot.dovi is not None or snapshot.hdr10plus)
        self._compare_tab.set_status(translate_text(
            "B : en-têtes seulement ; lire les métadonnées dynamiques de B pour comparer les courbes.")
            if needs_dynamic else translate_text("B : {name}", name=info.path.name))

    def analyze_compare(self) -> None:
        """Niveau 2 de la piste B (même progression et annulation que la piste A)."""
        info = self._compare_info
        side = self._compare_tab.side_b()
        if info is None or side is None or self._busy:
            return
        stream = side.report.snapshot.stream_index
        self._token += 1
        token = self._token
        cancel = threading.Event()
        self._cancel = cancel
        signals = TaskSignals()
        self._signals = signals

        def task() -> None:
            try:
                self._compare_analyzed.emit(token, self._dynamic_task(info, stream, False, token, cancel, signals), "")
            except Exception as exc:
                self._compare_analyzed.emit(token, None, str(exc) or exc.__class__.__name__)

        self._set_busy(True, translate_text("Analyse des métadonnées dynamiques de B…"))
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self.log_message.emit("INFO", translate_text("Lecture des métadonnées dynamiques : {name}", name=info.path.name))
        self._executor.submit(task)

    def _on_compare_analyzed(self, token: int, report: InspectionReport | None, error: str) -> None:
        if token != self._token:
            return
        self._signals = None
        self._set_busy(False)
        if report is None:
            self._compare_notify("ERROR" if error else "WARN",
                                 translate_text("Analyse HDR impossible : {error}", error=error) if error
                                 else translate_text("Analyse annulée."))
            return
        side = self._compare_tab.side_b()
        measurement = side.measurement if side is not None else None
        if measurement is not None:
            report = dataclasses.replace(report, findings=audit(report.snapshot, report.dovi, report.hdr10plus,
                                                                 measurement))
        self._compare_tab.set_side_b(Side(report, measurement))
        self._compare_tab.set_status(translate_text("B : {name}", name=Path(report.snapshot.path).name))
        for message in report.errors:
            self.log_message.emit("WARN", message)

    def _compare_notify(self, level: str, text: str) -> None:
        """Message de la piste B : ligne d'état de l'onglet Comparer et journal d'exécution."""
        self._compare_tab.set_status(text, error=level == "ERROR")
        self.log_message.emit(level, text)

    # ── Rapport ─────────────────────────────────────────────────────────
    def export_report(self, path: Path | None = None) -> Path | None:
        """Rapport HTML (autonome) ou JSON de la piste inspectée, FEL Analyzer et comparaison compris."""
        report = self._report
        if report is None:
            return None
        if path is None:
            source = Path(report.snapshot.path)
            chosen, selected = QFileDialog.getSaveFileName(
                self, translate_text("Exporter le rapport HDR"), str(source.with_name(f"{source.stem}.hdr.html")),
                translate_text("Page HTML (*.html);;JSON (*.json)"))
            if not chosen:
                return None
            path = Path(chosen)
            if path.suffix.lower() not in (".html", ".htm", ".json"):
                path = path.with_name(path.name + (".json" if "json" in selected.lower() else ".html"))
        try:
            written = write_report(path, report, measurement=self._measurement, fel=self._fel_tab.fel_report(),
                                   comparison=self._compare_tab.comparison(), other=self._compare_tab.other_report())
        except (OSError, ValueError) as exc:
            self._notify("ERROR", translate_text("Export du rapport impossible : {error}", error=str(exc)))
            return None
        self._set_status(translate_text("Rapport exporté : {name}", name=written.name))
        self.log_message.emit("OK", translate_text("Rapport HDR exporté : {path}", path=str(written)))
        return written

    # ── Correction du fichier ───────────────────────────────────────────
    def _prepare_patch(self, max_cll: int, max_fall: int) -> None:
        self._editor.set_static_values(max_cll, max_fall)
        self._editor.enable_static()
        self._tabs.setCurrentIndex(self._tabs.count() - 1)

    def _display_peak_changed(self, value: int) -> None:
        # Enregistré avec la configuration (fermeture de l'application ou page Paramètres).
        self._config.display_peak_nits = int(value)
        self._editor.set_realignment(self._realignment(self._report))

    def _apply_to_encode(self, max_cll: int, max_fall: int) -> None:
        report = self._report
        if report is not None:
            self.apply_to_encode_requested.emit(Path(report.snapshot.path), report.snapshot.stream_index,
                                                max_cll, max_fall)

    def operation_state(self) -> OperationState:
        """Opération « Écrire le fichier corrigé » de la barre d'exécution."""
        plan, reason = self._editor.readiness()
        summary = (f"{plan.output.name} ← {plan.source.name}" if plan is not None
                   else translate_text("Correction des métadonnées HDR sans réencodage"))
        if plan is not None and self._busy:
            reason = translate_text("HDR Studio occupé : attendre la fin de l'analyse en cours.")
        return OperationState("hdr", 7, translate_text("Écrire le fichier corrigé"), summary,
                              plan is not None and not self._busy,
                              reason or translate_text("Choisissez les corrections dans l'onglet Édition."))

    def run(self) -> None:
        """Lancement depuis la barre d'exécution."""
        plan, reason = self._editor.readiness()
        if plan is None:
            self._notify("WARN", reason)
            return
        self.write_patch(plan)

    def write_patch(self, plan: PatchPlan) -> None:
        """Écrit le fichier corrigé (thread de travail, annulable)."""
        if self._busy:
            return
        self._token += 1
        token = self._token
        cancel = threading.Event()
        self._cancel = cancel
        signals = TaskSignals()
        self._signals = signals
        runner = ToolRunner(max_workers=1)
        config = self._config
        span = [0.0, 1.0]
        last_pct = [-1]

        def on_step(label: str, start: float, part: float) -> None:
            span[:] = [start, part]
            last_pct[0] = -1
            self._progress.emit(token, label, int(start * 100))

        def on_pct(pct: int) -> None:
            pct = max(0, min(100, pct))
            if pct < last_pct[0]:
                # Second balayage d'un outil (inject-rpu, hdr10plus_tool inject) : fenêtre suivante.
                span[0] += span[1]
            last_pct[0] = pct
            self._progress.emit(token, "", int((span[0] + span[1] * pct / 100) * 100))

        def run(cmd: list[str]) -> object:
            return runner._run_cmd(cmd, label="hdr-patch", progress_cb=self._tool_line(cmd), progress_pct_cb=on_pct,
                                   signals=signals)

        def task() -> None:
            owned = None
            try:
                owned = create_process_work_dir(config.ensure_work_dir(), process_name="hdr-correction")
                patcher = MetadataPatcher(
                    ffmpeg_bin=str(config.tool_ffmpeg), dovi_tool_bin=str(config.tool_dovi_tool),
                    hdr10plus_bin=str(config.tool_hdr10plus), run_cmd=run, work_dir=owned.path,
                    on_step=on_step, cancelled=cancel.is_set,
                    progress=lambda fraction: self._progress.emit(token, "", int(fraction * 100)))
                self._patched.emit(token, patcher.run(plan), "")
            except PatchCancelled:
                self._patched.emit(token, None, "")
            except Exception as exc:
                self._patched.emit(token, None, "" if cancel.is_set() else (str(exc) or exc.__class__.__name__))
            finally:
                if owned is not None:
                    try:
                        owned.remove()
                    except OSError:
                        pass

        self._patch_token = token
        self._set_busy(True, translate_text("Correction du fichier…"))
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self.log_message.emit("INFO", translate_text("Correction des métadonnées : {source} → {output}",
                                                     source=plan.source.name, output=plan.output.name))
        self.op_state_changed.emit("started", translate_text("Correction de {name}…", name=plan.source.name))
        self._executor.submit(task)

    def _on_patched(self, token: int, output: Path | None, error: str) -> None:
        if token != self._token:
            return
        self._signals = None
        self._patch_token = None
        self._set_busy(False)
        if output is None:
            message = (translate_text("Correction impossible : {error}", error=error) if error
                       else translate_text("Correction annulée."))
            self._set_status(message, error=bool(error))
            # Journal : écrit par la barre d'exécution (état terminal).
            self.op_state_changed.emit("failed" if error else "cancelled", message)
            return
        self._last_output = Path(output)
        self._open_output_btn.setVisible(True)
        self._set_status(translate_text("Fichier corrigé écrit : {name}", name=Path(output).name))
        self.op_state_changed.emit("finished", translate_text("Fichier corrigé écrit : {path}", path=str(output)))

    def _open_output(self) -> None:
        if self._last_output is not None:
            self._open_output_btn.setVisible(False)
            self.load_source(self._last_output)

    def cancel(self) -> None:
        self._cancel.set()
        if self._signals is not None:
            self._signals.cancel()

    def _stop_running(self) -> None:
        if self._busy:
            self.cancel()
            self._set_busy(False)

    def is_busy(self) -> bool:
        return self._busy

    # ── État ────────────────────────────────────────────────────────────
    def _set_busy(self, busy: bool, status: str = "") -> None:
        self._busy = busy
        self._last_step = ""
        self._progress_bar.setVisible(busy)
        if busy:
            self._set_status(status)
        else:
            self._progress_bar.setRange(0, 100)
            self._progress_bar.setValue(0)
        self._update_actions()
        self.busy_changed.emit(busy)
        self.ready_changed.emit(self.operation_state().ready)

    def _set_status(self, text: str, *, error: bool = False) -> None:
        self._status.setText(text)
        self._status.setStyleSheet(f"color:{C.ERROR if error else C.TEXT_SEC};font-size:{font_px(11)}px;background:transparent;")

    def _update_actions(self) -> None:
        report = self._report
        dynamic_available = report is not None and (report.snapshot.dovi is not None or report.snapshot.hdr10plus)
        self._analyze_btn.setVisible(not (report is not None and report.dynamic))
        self._analyze_btn.setEnabled(bool(dynamic_available) and not self._busy)
        self._refresh_btn.setVisible(report is not None and report.dynamic)
        self._refresh_btn.setEnabled(not self._busy)
        self._cancel_btn.setVisible(self._busy)
        self._track_combo.setEnabled(not self._busy and self._track_combo.count() > 1)
        self._luminance.set_busy(self._busy)
        self._fel_tab.set_busy(self._busy)
        self._editor.set_busy(self._busy)
        self._compare_tab.set_busy(self._busy)
        self._export_btn.setEnabled(report is not None and not self._busy)
        if report is not None and not self._busy:
            if report.from_cache:
                self._set_status(translate_text("Métadonnées dynamiques : résultat en cache."))
            elif report.dynamic:
                self._set_status(translate_text("Métadonnées dynamiques lues."))
            elif not dynamic_available:
                self._set_status(translate_text("Aucune métadonnée dynamique sur cette piste."))
            else:
                self._set_status("")

    # ── Rendu ───────────────────────────────────────────────────────────
    @staticmethod
    def _clear(layout: QVBoxLayout | QHBoxLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                # Détaché tout de suite : sinon visible jusqu'au traitement du deleteLater.
                widget.setParent(None)
                widget.deleteLater()

    def report(self) -> InspectionReport | None:
        return self._report

    def _render(self, report: InspectionReport | None) -> None:
        self._report = report
        for layout in (self._overview, self._dovi_tab, self._hdr10plus_tab):
            self._clear(layout)
        self._luminance.set_report(report, self._measurement)
        self._fel_tab.set_report(report)
        self._compare_tab.set_side_a(Side(report, self._measurement) if report is not None else None)
        self._editor.set_report(report, self._measurement)
        self._editor.set_realignment(self._realignment(report))
        if report is None:
            self._overview.addWidget(_label(translate_text(
                "Déposez une vidéo, choisissez un fichier, ou faites un clic droit sur une piste vidéo "
                "(Conteneur, Encodage) puis « Inspecter le HDR… »."), color=C.TEXT_DIM, wrap=True))
            self._overview.addStretch()
            return
        self._render_overview(report)
        self._render_dovi(report)
        self._render_hdr10plus(report)
        for layout in (self._overview, self._dovi_tab, self._hdr10plus_tab):
            layout.addStretch()

    def _render_overview(self, report: InspectionReport) -> None:
        snapshot = report.snapshot
        badges = QHBoxLayout()
        badges.setSpacing(scale(6))
        palette = {"Dolby Vision": C.PURPLE, "HDR10+": C.WARN, "HDR10": C.INFO, "PQ": C.INFO, "HLG": C.OK}
        for name in snapshot.formats:
            badges.addWidget(_badge(name, palette.get(name, C.TEXT_SEC)))
        badges.addStretch()
        holder = QWidget()
        holder.setStyleSheet("background:transparent;")
        holder.setLayout(badges)
        self._overview.addWidget(holder)
        self._overview.addWidget(_section(translate_text("PISTE"), _kv_grid(summary.track_rows(snapshot))))
        self._overview.addWidget(_section(translate_text("HDR10 STATIQUE"),
                                          _kv_grid(summary.static_rows(snapshot, self._measurement))))
        if snapshot.dovi is not None:
            self._overview.addWidget(_section(translate_text("DOLBY VISION (CONTENEUR)"),
                                              _kv_grid(summary.container_dovi_rows(snapshot.dovi))))
        checks = QWidget()
        checks.setStyleSheet("background:transparent;")
        checks_layout = QVBoxLayout(checks)
        checks_layout.setContentsMargins(0, 0, 0, 0)
        checks_layout.setSpacing(scale(4))
        for finding in report.findings:
            checks_layout.addWidget(_finding_row(finding))
        for message in report.errors:
            checks_layout.addWidget(_finding_row(Finding(Severity.ERROR, "format", message)))
        if not report.dynamic and (snapshot.dovi is not None or snapshot.hdr10plus):
            checks_layout.addWidget(_label(translate_text(
                "Contrôles des en-têtes seulement. « Lire les métadonnées dynamiques » vérifie aussi le RPU, "
                "le HDR10+ et leur cohérence."), color=C.TEXT_DIM, size=10, wrap=True))
        if not report.findings and not report.errors:
            checks_layout.addWidget(_label(translate_text("Aucune remarque."), color=C.TEXT_SEC))
        self._overview.addWidget(_section(translate_text("CONTRÔLES"), checks))

    def _render_dovi(self, report: InspectionReport) -> None:
        dovi = report.dovi
        if dovi is None:
            text = translate_text("Pas de Dolby Vision sur cette piste.") if report.snapshot.dovi is None else translate_text(
                "Lire les métadonnées dynamiques pour analyser le RPU.")
            self._dovi_tab.addWidget(_label(text, color=C.TEXT_DIM, wrap=True))
            return
        rows = summary.dovi_rows(dovi, report.snapshot.fps or 24000 / 1001)
        self._dovi_tab.addWidget(_section(translate_text("RPU"), _kv_grid(rows)))
        headers, l6_rows = summary.l6_table(dovi)
        if l6_rows:
            self._dovi_tab.addWidget(_section(translate_text("L6 (HDR10 DU RPU)"), _table(headers, l6_rows)))
        self._dovi_tab.addWidget(_section(translate_text("ZONES ACTIVES L5"), _table(*summary.active_area_table(report))))

    def _render_hdr10plus(self, report: InspectionReport) -> None:
        hdr10plus = report.hdr10plus
        if hdr10plus is None:
            text = translate_text("Pas de HDR10+ sur cette piste.") if not report.snapshot.hdr10plus else translate_text(
                "Lire les métadonnées dynamiques pour analyser le HDR10+.")
            self._hdr10plus_tab.addWidget(_label(text, color=C.TEXT_DIM, wrap=True))
            return
        rows = summary.hdr10plus_rows(hdr10plus)
        self._hdr10plus_tab.addWidget(_section(translate_text("HDR10+"), _kv_grid(rows)))

    # ── Fermeture ───────────────────────────────────────────────────────
    def closeEvent(self, event) -> None:  # noqa: N802
        if not hasattr(self, "_shutdown"):
            self.setEnabled(False)
            self.cancel()
            self._shutdown = Shutdown(executors=(self._executor,))
        if defer_close(self, event, ready=self._shutdown.done.is_set()):
            return
        super().closeEvent(event)
