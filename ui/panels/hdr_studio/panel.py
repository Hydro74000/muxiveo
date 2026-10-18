"""HDR Studio : inspection HDR d'une piste vidéo (formats, cohérence, courbes de luminance).

Niveau 1 (instantané) : en-têtes déjà lus par l'inspection. Niveau 2 (à la demande) :
RPU Dolby Vision et HDR10+ extraits et analysés, sans décoder l'image ; résultat en cache.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
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
    DoviAnalysis,
    DynamicInspector,
    Finding,
    Hdr10PlusAnalysis,
    InspectionCache,
    InspectionCancelled,
    InspectionReport,
    Severity,
    audit,
    build_snapshot,
)
from core.hdr_inspection.dovi import L9_PRIMARY_NAMES
from core.hdr_inspection.pq import pq12_to_nits
from core.hdr_inspection.snapshot import HdrSnapshot
from core.i18n import translate_text
from core.inspector import FileInfo, FileInspector
from core.runner import TaskSignals, ToolRunner
from core.workdir import create_process_work_dir
from ui.design_system import colors as C, font_px, scale
from ui.panels.hdr_studio.chart import ChartMarker, ChartSeries, LuminanceChart, format_time
from ui.shutdown import Shutdown, defer_close
from ui.styles import _card, _checkbox_style, _combo_style, _input_style, _primary_button, _secondary_button, _section_label

_SEVERITY_COLORS = {Severity.OK: "OK", Severity.INFO: "INFO", Severity.WARN: "WARN", Severity.ERROR: "ERROR"}
_SEVERITY_ICONS = {Severity.OK: "✓", Severity.INFO: "i", Severity.WARN: "!", Severity.ERROR: "✕"}
_L11_CONTENT = {0: "Défaut", 1: "Cinéma", 2: "Jeu", 3: "Sport", 4: "Contenu utilisateur"}


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


def _nits(value: float | None) -> str:
    if value is None:
        return "?"
    return f"{value:.4f}".rstrip("0").rstrip(".") if value < 1 else f"{round(value)}"


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


class _DropLineEdit(QLineEdit):
    dropped = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        for url in event.mimeData().urls():
            if url.isLocalFile():
                self.dropped.emit(Path(url.toLocalFile()))
                break


class HdrStudio(QWidget):
    """Page « HDR Studio » : chargement d'une source, analyse à la demande, onglets de résultats."""

    log_message = Signal(str, str)
    busy_changed = Signal(bool)
    _inspected = Signal(int, object, int, object, str)
    _cache_ready = Signal(int, object)
    _progress = Signal(int, str, int)
    _analyzed = Signal(int, object, str)

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
        self._pending_analyze = False
        self.setAcceptDrops(True)
        self._build_ui()
        self._inspected.connect(self._on_inspected, Qt.ConnectionType.QueuedConnection)
        self._cache_ready.connect(self._on_cache_ready, Qt.ConnectionType.QueuedConnection)
        self._progress.connect(self._on_progress, Qt.ConnectionType.QueuedConnection)
        self._analyzed.connect(self._on_analyzed, Qt.ConnectionType.QueuedConnection)
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
        self._path_edit = _DropLineEdit()
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
        self._luminance_tab = QWidget()
        self._luminance_tab.setStyleSheet("background:transparent;")
        self._build_luminance_tab()
        self._tabs.addTab(self._luminance_tab, translate_text("Luminance"))
        self._dovi_tab = self._scroll_tab(translate_text("Dolby Vision"))
        self._hdr10plus_tab = self._scroll_tab(translate_text("HDR10+"))
        outer.addWidget(self._tabs, 1)

    def _scroll_tab(self, title: str) -> QVBoxLayout:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea{background:transparent;border:none;}")
        body = QWidget()
        body.setStyleSheet("background:transparent;")
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, scale(10), 0, 0)
        layout.setSpacing(scale(10))
        scroll.setWidget(body)
        self._tabs.addTab(scroll, title)
        return layout

    def _build_luminance_tab(self) -> None:
        layout = QVBoxLayout(self._luminance_tab)
        layout.setContentsMargins(0, scale(10), 0, 0)
        layout.setSpacing(scale(8))
        self._legend = QHBoxLayout()
        self._legend.setSpacing(scale(14))
        layout.addLayout(self._legend)
        self._chart = LuminanceChart()
        layout.addWidget(self._chart, 1)
        self._hover_label = _label("", color=C.TEXT_SEC, size=10)
        self._chart.hover_changed.connect(self._hover_label.setText)
        layout.addWidget(self._hover_label)
        self._luminance_hint = _label(translate_text(
            "Axe vertical en nits, échelle PQ. Molette : zoom ; glisser : déplacement ; double-clic : vue entière. "
            "Le L1 Dolby est un maximum robuste (pixels isolés écartés) ; le MaxScl HDR10+ est un maximum strict."),
            color=C.TEXT_DIM, size=10, wrap=True)
        layout.addWidget(self._luminance_hint)

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
            self._set_status(translate_text("Fichier vidéo introuvable ou non pris en charge : {name}", name=path.name),
                             error=True)
            return
        self._stop_running()
        self._token += 1
        token = self._token
        self._info = None
        self._report = None
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
                cached = self._cache.load(path, index) if video else None
                self._inspected.emit(token, info, index, cached, "")
            except Exception as exc:  # inspection impossible : message à l'utilisateur
                self._inspected.emit(token, None, 0, None, str(exc))

        self._executor.submit(task)

    def _on_inspected(self, token: int, info: FileInfo | None, index: int, cached: object, error: str) -> None:
        if token != self._token:
            return
        self._set_busy(False)
        if info is None or not info.video_tracks:
            self._set_status(error or translate_text("Aucune piste vidéo."), error=True)
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

        def task() -> None:
            self._cache_ready.emit(token, self._cache.load(Path(info.path), snapshot.stream_index))

        self._executor.submit(task)

    def _on_cache_ready(self, token: int, cached: object) -> None:
        if token == self._token and self._info is not None:
            self._show_snapshot(cached)

    def _show_snapshot(self, cached: object) -> None:
        assert self._info is not None
        snapshot = build_snapshot(self._info, self._current_stream())
        if isinstance(cached, tuple):
            dovi, hdr10plus = cached
            report = InspectionReport(snapshot, dovi, hdr10plus, audit(snapshot, dovi, hdr10plus),
                                      dynamic=True, from_cache=True)
        else:
            report = InspectionReport(snapshot, findings=audit(snapshot))
        self._render(report)
        self._update_actions()

    # ── Analyse dynamique ───────────────────────────────────────────────
    def analyze(self, *, refresh: bool = False) -> None:
        if self._info is None or self._busy:
            return
        info, stream = self._info, self._current_stream()
        snapshot = build_snapshot(info, stream)
        if snapshot.dovi is None and not snapshot.hdr10plus:
            self._set_status(translate_text("Aucune métadonnée dynamique (Dolby Vision, HDR10+) sur cette piste."))
            return
        self._token += 1
        token = self._token
        # Un événement par analyse : celle qu'on annule s'arrête même si une autre est lancée aussitôt.
        cancel = threading.Event()
        self._cancel = cancel
        signals = TaskSignals()
        self._signals = signals
        runner = ToolRunner(max_workers=1)
        config = self._config
        span = [0.0, 1.0]

        def on_step(label: str, start: float, part: float) -> None:
            span[:] = [start, part]
            self._progress.emit(token, label, int(start * 100))

        def on_pct(pct: int) -> None:
            self._progress.emit(token, "", int((span[0] + span[1] * max(0, min(100, pct)) / 100) * 100))

        def run(cmd: object) -> object:
            return runner._run_cmd(cmd, label="hdr-inspection", progress_pct_cb=on_pct, signals=signals)  # type: ignore[arg-type]

        def task() -> None:
            owned = None
            try:
                owned = create_process_work_dir(config.ensure_work_dir(), process_name="hdr-inspection")
                inspector = DynamicInspector(
                    ffmpeg_bin=str(config.tool_ffmpeg), dovi_tool_bin=str(config.tool_dovi_tool),
                    hdr10plus_bin=str(config.tool_hdr10plus), run_cmd=run, work_dir=owned.path,
                    cache=self._cache, on_step=on_step, cancelled=cancel.is_set)
                self._analyzed.emit(token, inspector.analyze(info, stream, refresh=refresh), "")
            except InspectionCancelled:
                self._analyzed.emit(token, None, "")
            except Exception as exc:  # échec d'outil : affiché, l'instantané reste visible
                self._analyzed.emit(token, None, str(exc) or exc.__class__.__name__)
            finally:
                if owned is not None:
                    try:
                        owned.remove()
                    except OSError:
                        pass  # repris par le nettoyage de démarrage (marqueur conservé)

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
        self._progress_bar.setValue(max(self._progress_bar.value(), pct))

    def _on_analyzed(self, token: int, report: InspectionReport | None, error: str) -> None:
        if token != self._token:
            return
        self._signals = None
        self._set_busy(False)
        if report is None:
            if error:
                self._set_status(translate_text("Analyse impossible : {error}", error=error), error=True)
                self.log_message.emit("ERROR", translate_text("Analyse HDR impossible : {error}", error=error))
            else:
                self._set_status(translate_text("Analyse annulée."))
            return
        self._render(report)
        for message in report.errors:
            self.log_message.emit("WARN", message)
        warnings = sum(1 for f in report.findings if f.severity in (Severity.WARN, Severity.ERROR))
        self.log_message.emit("OK" if not warnings else "WARN", translate_text(
            "Analyse HDR terminée : {count} point(s) à vérifier", count=warnings))
        self._update_actions()

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
        self._progress_bar.setVisible(busy)
        if busy:
            self._set_status(status)
        else:
            self._progress_bar.setRange(0, 100)
            self._progress_bar.setValue(0)
        self._update_actions()
        self.busy_changed.emit(busy)

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
        if report is None:
            self._overview.addWidget(_label(translate_text(
                "Déposez une vidéo, choisissez un fichier, ou faites un clic droit sur une piste vidéo "
                "(Conteneur, Encodage) puis « Inspecter le HDR… »."), color=C.TEXT_DIM, wrap=True))
            self._overview.addStretch()
            self._render_luminance(None)
            return
        self._render_overview(report)
        self._render_dovi(report)
        self._render_hdr10plus(report)
        self._render_luminance(report)
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
        self._overview.addWidget(_section(translate_text("PISTE"), _kv_grid(self._track_rows(snapshot))))
        self._overview.addWidget(_section(translate_text("HDR10 STATIQUE"), _kv_grid(self._static_rows(snapshot))))
        if snapshot.dovi is not None:
            record = snapshot.dovi
            layers = "+".join(name for name, present in (("BL", record.bl), ("EL", record.el), ("RPU", record.rpu)) if present)
            rows = [(translate_text("Profil"), record.label), (translate_text("Niveau"), str(record.level or "?")),
                    (translate_text("Couches"), layers)]
            self._overview.addWidget(_section(translate_text("DOLBY VISION (CONTENEUR)"), _kv_grid(rows)))
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

    @staticmethod
    def _track_rows(snapshot: HdrSnapshot) -> list[tuple[str, str]]:
        frames = "?" if snapshot.frames is None else (
            f"{snapshot.frames}" if snapshot.frames_exact else translate_text("≈ {count} (d'après la durée)", count=snapshot.frames))
        return [
            (translate_text("Codec"), snapshot.codec.upper()),
            (translate_text("Résolution"), f"{snapshot.width or '?'}×{snapshot.height or '?'}"),
            (translate_text("Cadence"), f"{snapshot.fps:.3f} i/s" if snapshot.fps else "?"),
            (translate_text("Images"), frames),
            (translate_text("Profondeur"), f"{snapshot.bit_depth} bits" if snapshot.bit_depth else "?"),
            (translate_text("Transfert"), snapshot.transfer or "?"),
            (translate_text("Primaires"), snapshot.primaries or "?"),
            (translate_text("Matrice"), snapshot.matrix or "?"),
        ]

    @staticmethod
    def _static_rows(snapshot: HdrSnapshot) -> list[tuple[str, str]]:
        mastering = snapshot.mastering
        mdcv = translate_text("absent") if mastering is None else (
            f"{mastering.primaries}, {_nits(mastering.min_nits)} – {_nits(mastering.max_nits)} nits")
        levels = translate_text("absents") if snapshot.max_cll is None else f"{snapshot.max_cll} / {snapshot.max_fall}"
        return [(translate_text("Écran de mastering (MDCV)"), mdcv), (translate_text("MaxCLL / MaxFALL"), levels)]

    def _render_dovi(self, report: InspectionReport) -> None:
        dovi = report.dovi
        if dovi is None:
            text = translate_text("Pas de Dolby Vision sur cette piste.") if report.snapshot.dovi is None else translate_text(
                "Lire les métadonnées dynamiques pour analyser le RPU.")
            self._dovi_tab.addWidget(_label(text, color=C.TEXT_DIM, wrap=True))
            return
        fps = report.snapshot.fps or 24000 / 1001
        lengths = dovi.scene_lengths() / fps
        el = dovi.el_type or translate_text("aucune")
        if dovi.el_type == "FEL" and dovi.frames:
            el += " · " + translate_text("résiduel actif sur {share} % des images",
                                          share=round(100 * dovi.residual_active_frames / dovi.frames, 1))
        display = ", ".join(f"{_nits(lo)} – {hi} nits" for lo, hi in dovi.source_display) or "?"
        rows = [
            (translate_text("Profil (RPU)"), f"P{dovi.profile}" if dovi.profile is not None else "?"),
            (translate_text("Couche EL"), el),
            (translate_text("Version CM"), dovi.cm_version),
            (translate_text("Images"), str(dovi.frames)),
            (translate_text("Plans"), translate_text("{count} (durée min. {min} s, médiane {median} s, max. {max} s)",
                                                     count=dovi.scenes, min=f"{lengths.min():.1f}",
                                                     median=f"{np.median(lengths):.1f}", max=f"{lengths.max():.1f}")
             if lengths.size else "0"),
            (translate_text("Pic L1 (MaxCLL du RPU)"), f"{round(dovi.l1_peak_nits())} nits"),
            (translate_text("Moyenne L1 max. (MaxFALL du RPU)"), f"{dovi.l1_fall_nits():.1f} nits"),
            (translate_text("Écran de mastering (RPU)"), display),
            (translate_text("Trims L2 (écrans cibles)"), ", ".join(f"{t} nits" for t in dovi.l2_targets) or "—"),
        ]
        if dovi.cm_version == "4.0":
            rows.append((translate_text("Trims L8 (index d'écran)"), ", ".join(str(t) for t in dovi.l8_targets) or "—"))
            rows.append((translate_text("Écrans L10"), ", ".join(f"{t} nits" for t in dovi.l10_targets) or "—"))
        if dovi.l9_primaries:
            rows.append((translate_text("Primaires L9"), ", ".join(L9_PRIMARY_NAMES.get(p, str(p)) for p in dovi.l9_primaries)))
        if dovi.l11_content:
            rows.append((translate_text("Type de contenu L11"),
                         ", ".join(translate_text(_L11_CONTENT.get(c, str(c))) for c in dovi.l11_content)))
        self._dovi_tab.addWidget(_section(translate_text("RPU"), _kv_grid(rows)))
        l6_rows = [[f"{s.max_cll} / {s.max_fall}", f"{_nits(s.min_mastering)} – {s.max_mastering}", str(s.frames)]
                   for s in dovi.l6]
        if l6_rows:
            self._dovi_tab.addWidget(_section(translate_text("L6 (HDR10 DU RPU)"), _table(
                [translate_text("MaxCLL / MaxFALL"), translate_text("Mastering (nits)"), translate_text("Images")], l6_rows)))
        width, height = report.snapshot.width or 0, report.snapshot.height or 0
        area_rows = [[format_time(a.first_frame / fps), format_time(a.frames / fps),
                      f"{a.left} / {a.right} / {a.top} / {a.bottom}",
                      f"{max(0, width - a.left - a.right)}×{max(0, height - a.top - a.bottom)}"]
                     for a in dovi.active_areas[:500]]
        self._dovi_tab.addWidget(_section(translate_text("ZONES ACTIVES L5"), _table(
            [translate_text("Début"), translate_text("Durée"), translate_text("Gauche / droite / haut / bas"),
             translate_text("Image active")], area_rows)))

    def _render_hdr10plus(self, report: InspectionReport) -> None:
        hdr10plus = report.hdr10plus
        if hdr10plus is None:
            text = translate_text("Pas de HDR10+ sur cette piste.") if not report.snapshot.hdr10plus else translate_text(
                "Lire les métadonnées dynamiques pour analyser le HDR10+.")
            self._hdr10plus_tab.addWidget(_label(text, color=C.TEXT_DIM, wrap=True))
            return
        targets = ", ".join(f"{t} nits" for t in hdr10plus.targeted_display if t) or translate_text("non renseigné")
        rows = [
            (translate_text("Profil"), hdr10plus.profile + (translate_text(" (courbe de Bézier)") if hdr10plus.profile == "B" else "")),
            (translate_text("Images"), str(hdr10plus.frames)),
            (translate_text("Plans"), str(hdr10plus.scenes)),
            (translate_text("Fenêtres"), ", ".join(str(w) for w in hdr10plus.windows)),
            (translate_text("Écran ciblé (TSDML)"), targets),
            (translate_text("Pic MaxScl"), f"{round(hdr10plus.peak_nits())} nits"),
            *([(translate_text("Pic MaxScl hors saturation"), f"{round(hdr10plus.credible_peak_nits())} nits"),
               (translate_text("Images à 10 000 nits"), str(hdr10plus.saturated().size))]
              if hdr10plus.saturated().size else []),
            (translate_text("AverageRGB max."), f"{hdr10plus.max_average_nits():.1f} nits"),
            (translate_text("Percentile 99,98 % max."), f"{round(float(hdr10plus.p99.max()))} nits" if hdr10plus.frames else "?"),
        ]
        self._hdr10plus_tab.addWidget(_section(translate_text("HDR10+"), _kv_grid(rows)))

    def _render_luminance(self, report: InspectionReport | None) -> None:
        self._clear(self._legend)
        series: list[ChartSeries] = []
        frames = 0
        if report is not None and report.dovi is not None and report.dovi.frames:
            dovi: DoviAnalysis = report.dovi
            series.append(ChartSeries("l1_max", translate_text("L1 max"), C.PURPLE,
                                      pq12_to_nits(dovi.l1_max).astype(np.float32), "max", fill=True))
            series.append(ChartSeries("l1_avg", translate_text("L1 moyenne"), C.ACCENT,
                                      pq12_to_nits(dovi.l1_avg).astype(np.float32), "mean"))
            frames = dovi.frames
        if report is not None and report.hdr10plus is not None and report.hdr10plus.frames:
            hdr10plus: Hdr10PlusAnalysis = report.hdr10plus
            series.append(ChartSeries("maxscl", translate_text("MaxScl HDR10+"), C.WARN, hdr10plus.max_scl, "max",
                                      visible=report.dovi is None, fill=report.dovi is None))
            series.append(ChartSeries("avgrgb", translate_text("AverageRGB HDR10+"), C.TRACK_TAGS, hdr10plus.average_rgb,
                                      "mean", visible=report.dovi is None))
            frames = max(frames, hdr10plus.frames)
        markers: list[ChartMarker] = []
        if report is not None and series:
            snapshot = report.snapshot
            if snapshot.max_cll:
                markers.append(ChartMarker(translate_text("MaxCLL"), float(snapshot.max_cll), C.OK))
            if snapshot.mastering is not None and snapshot.mastering.max_nits:
                markers.append(ChartMarker(translate_text("MDCV"), float(snapshot.mastering.max_nits), C.TEXT_DIM))
        self._chart.set_data(frames=frames, fps=report.snapshot.fps if report is not None else None,
                             series=series, markers=markers,
                             scenes=report.dovi.scene_starts if report is not None and report.dovi is not None else None)
        for entry in series:
            box = QCheckBox(entry.label)
            box.setChecked(entry.visible)
            box.setStyleSheet(_checkbox_style() + f"QCheckBox{{color:{entry.color};}}")
            box.toggled.connect(lambda checked, key=entry.key: self._chart.set_series_visible(key, checked))
            self._legend.addWidget(box)
        self._legend.addStretch()
        self._hover_label.setText("" if series else translate_text(
            "Lire les métadonnées dynamiques pour afficher les courbes (Dolby Vision L1, HDR10+)."))

    # ── Fermeture ───────────────────────────────────────────────────────
    def closeEvent(self, event) -> None:  # noqa: N802
        if not hasattr(self, "_shutdown"):
            self.setEnabled(False)
            self.cancel()
            self._shutdown = Shutdown(executors=(self._executor,))
        if defer_close(self, event, ready=self._shutdown.done.is_set()):
            return
        super().closeEvent(event)
