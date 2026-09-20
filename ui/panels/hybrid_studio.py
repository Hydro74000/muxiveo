"""Studio d'hybridation : appariement par lot, calibration acoustique et synchronisation physique."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import uuid

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.i18n import translate_text
from core.workflows.audio_sync import AudioSyncTrack
from core.workflows.audio_sync_scan import AudioSyncScanner
from core.workflows.sync_calibration import SyncSegment
from ui.design_system import colors as _C, font_px as _font_px, scale as _scale
from ui.styles import _checkbox_style, _groupbox_checkable_style


# =============================================================================
# Helpers de style Design System
# =============================================================================

def _section_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(f"""
        color: {_C.TEXT_DIM};
        font-size: {_font_px(9)}px;
        font-weight: 700;
        letter-spacing: {_scale(2)}px;
        background: transparent;
    """)
    return lbl


def _card(parent: QWidget | None = None) -> QWidget:
    w = QWidget(parent)
    w.setStyleSheet(f"""
        QWidget {{
            background: {_C.BG_CARD};
            border: 1px solid {_C.BORDER};
            border-radius: {_scale(6)}px;
        }}
    """)
    return w


def _primary_button(text: str) -> QPushButton:
    btn = QPushButton(text)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setFixedHeight(_scale(32))
    btn.setStyleSheet(f"""
        QPushButton {{
            background: {_C.ACCENT};
            color: #ffffff;
            border: none;
            border-radius: {_scale(5)}px;
            font-size: {_font_px(11)}px;
            font-weight: 700;
            padding: 0 {_scale(16)}px;
        }}
        QPushButton:hover {{
            background: {_C.ACCENT_HOVER if hasattr(_C, 'ACCENT_HOVER') else '#6070f8'};
        }}
        QPushButton:pressed {{
            background: {_C.ACCENT_DIM};
        }}
        QPushButton:disabled {{
            background: {_C.BG_ACTIVE};
            color: {_C.TEXT_DIM};
        }}
    """)
    return btn


def _secondary_button(
    text: str,
    fixed_width: int | None = None,
    *,
    min_width: int | None = None,
    padding_h: int | None = None,
) -> QPushButton:
    btn = QPushButton(text)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setFixedHeight(_scale(30))

    is_symbol = len(text.strip()) <= 2
    if padding_h is not None:
        pad_px = _scale(padding_h)
    elif is_symbol:
        pad_px = _scale(2)
    elif fixed_width is not None and fixed_width <= 50:
        pad_px = _scale(4)
    else:
        pad_px = _scale(8)

    if fixed_width:
        btn.setFixedWidth(_scale(fixed_width))
    if min_width:
        btn.setMinimumWidth(_scale(min_width))

    btn.setStyleSheet(f"""
        QPushButton {{
            background: {_C.BG_ACTIVE};
            color: {_C.TEXT_SEC};
            border: 1px solid {_C.BORDER_LT};
            border-radius: {_scale(5)}px;
            font-size: {_font_px(11)}px;
            padding: 0 {pad_px}px;
            text-align: center;
        }}
        QPushButton:hover {{
            background: {_C.BG_HOVER};
            color: {_C.TEXT_PRI};
            border-color: {_C.ACCENT};
        }}
        QPushButton:disabled {{
            color: {_C.TEXT_DIM};
            border-color: {_C.BORDER};
        }}
    """)
    return btn


def _input_style() -> str:
    return f"""
        QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
            background: {_C.BG_CARD};
            color: {_C.TEXT_PRI};
            border: 1px solid {_C.BORDER};
            border-radius: {_scale(5)}px;
            padding: {_scale(4)}px {_scale(8)}px;
            font-size: {_font_px(11)}px;
            min-height: {_scale(22)}px;
        }}
        QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
            border-color: {_C.ACCENT};
        }}
        QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {{
            color: {_C.TEXT_DIM};
            background: {_C.BG_CARD};
        }}
    """


def _table_style() -> str:
    return f"""
        QTableWidget {{
            background: {_C.BG_CARD};
            color: {_C.TEXT_PRI};
            border: 1px solid {_C.BORDER};
            border-radius: {_scale(5)}px;
            gridline-color: {_C.BORDER};
            font-size: {_font_px(11)}px;
            selection-background-color: {_C.BG_ACTIVE};
            selection-color: {_C.TEXT_PRI};
        }}
        QHeaderView::section {{
            background: {_C.BG_CARD};
            color: {_C.TEXT_SEC};
            border: none;
            border-bottom: 1px solid {_C.BORDER};
            padding: {_scale(4)}px {_scale(8)}px;
            font-size: {_font_px(10)}px;
            font-weight: 700;
        }}
    """


# =============================================================================
# Widgets spécialisés
# =============================================================================

class DirectoryEdit(QLineEdit):
    def __init__(self, placeholder: str = ""):
        super().__init__()
        self.setAcceptDrops(True)
        if placeholder:
            self.setPlaceholderText(placeholder)
        self.setStyleSheet(_input_style())

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if len(urls) == 1:
            local = urls[0].toLocalFile()
            p = Path(local)
            if p.is_dir():
                self.setText(str(p))
                event.acceptProposedAction()
            elif p.is_file():
                self.setText(str(p.parent))
                event.acceptProposedAction()


from ui.widgets.waveform_view import WaveformView


class ProfileSelector(QWidget):
    """Sélecteur de profil décisionnel ou de job exact pour l'hybridation par lot."""

    def __init__(self, profiles_dir: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.profiles_dir = Path(profiles_dir)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(_scale(6))

        self.combo = QComboBox()
        self.combo.setStyleSheet(_input_style())
        layout.addWidget(self.combo, stretch=1)

        self.browse_btn = _secondary_button("…", fixed_width=32)
        self.browse_btn.setToolTip(translate_text("Parcourir un fichier profil ou exact job JSON"))
        self.browse_btn.clicked.connect(self._browse)
        layout.addWidget(self.browse_btn)

        self._custom_path: str = ""
        self.reload_profiles()

    def reload_profiles(self) -> None:
        current_data = self.combo.currentData() or self._custom_path
        self.combo.clear()
        self.combo.addItem(translate_text("[Aucun profil - Règles par défaut]"), "")
        try:
            from core.profiles.decision import DecisionProfileManager
            mgr = DecisionProfileManager(self.profiles_dir / "decision")
            for p in mgr.load_all():
                name = str(p.get("name", "")).strip()
                if name:
                    self.combo.addItem(f"Profil : {name}", name)
        except Exception:
            pass

        if self._custom_path:
            label = f"Fichier : {Path(self._custom_path).name}"
            self.combo.addItem(label, self._custom_path)

        if current_data:
            self.setText(str(current_data))

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            translate_text("Choisir un profil ou exact job JSON"),
            str(self.profiles_dir),
            "JSON (*.json);;Tous les fichiers (*)",
        )
        if path:
            self._custom_path = path
            self.reload_profiles()
            self.setText(path)

    def text(self) -> str:
        data = self.combo.currentData()
        if data:
            return str(data)
        txt = self.combo.currentText().strip()
        if txt.startswith("["):
            return ""
        return txt

    def setText(self, text: str) -> None:
        target = str(text or "").strip()
        if not target:
            self.combo.setCurrentIndex(0)
            return
        for i in range(self.combo.count()):
            data = self.combo.itemData(i)
            if data == target or self.combo.itemText(i) == target or f"Profil : {target}" == self.combo.itemText(i):
                self.combo.setCurrentIndex(i)
                return
        self._custom_path = target
        self.combo.addItem(f"Fichier : {Path(target).name}", target)
        self.combo.setCurrentIndex(self.combo.count() - 1)

    def setEnabled(self, enabled: bool) -> None:
        super().setEnabled(enabled)
        self.combo.setEnabled(enabled)
        self.browse_btn.setEnabled(enabled)



# =============================================================================
# Panneau Studio Hybridation
# =============================================================================

class HybridStudio(QWidget):
    prepared = Signal(object, object)
    failed = Signal(str)
    waveform_ready = Signal(object)
    preview_ready = Signal(str)
    log_message = Signal(str, str)

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.setWindowTitle(translate_text("Studio Hybridation"))
        self.executor = ThreadPoolExecutor(max_workers=1)
        self._executor = self.executor
        self.jobs, self.pairs = [], []
        self.index = 0
        self.stopped = False
        self.cancel_event = threading.Event()
        self.signals = None
        self.busy = False
        self.aux_running = False
        self.preview_temp = tempfile.TemporaryDirectory(prefix="Muxiveo_listen_")
        self._build_ui()

    def _build_ui(self) -> None:
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(_scale(16), _scale(16), _scale(16), _scale(16))
        outer_layout.setSpacing(_scale(12))

        # En-tête de section
        header_row = QHBoxLayout()
        title_lbl = QLabel(translate_text("Studio Hybridation"))
        title_lbl.setStyleSheet(f"""
            color: {_C.TEXT_PRI};
            font-size: {_font_px(15)}px;
            font-weight: 700;
            background: transparent;
        """)
        header_row.addWidget(title_lbl)
        header_row.addStretch()
        outer_layout.addLayout(header_row)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")

        container = QWidget()
        container.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(_scale(12))

        # ── Carte 1 : Dossiers sources et profils ──────────────────────────────
        card_dirs = _card(self)
        cd_layout = QVBoxLayout(card_dirs)
        cd_layout.setContentsMargins(_scale(12), _scale(10), _scale(12), _scale(10))
        cd_layout.setSpacing(_scale(8))
        cd_layout.addWidget(_section_label(translate_text("DOSSIERS SOURCES ET SORTIE")))

        form = QFormLayout()
        form.setSpacing(_scale(8))
        self.reference = DirectoryEdit("/chemin/vers/episodes_reference")
        self.donor = DirectoryEdit("/chemin/vers/episodes_donneur")
        self.output = DirectoryEdit("/chemin/vers/dossier_sortie")

        for label, edit in (("Référence", self.reference), ("Donneur", self.donor), ("Sortie", self.output)):
            row = QHBoxLayout()
            row.setSpacing(_scale(6))
            row.addWidget(edit, stretch=1)
            browse = _secondary_button("…", fixed_width=32)
            browse.clicked.connect(lambda checked=False, field=edit: self.browse(field))
            row.addWidget(browse)
            lbl = QLabel(translate_text(label))
            lbl.setStyleSheet(f"color: {_C.TEXT_SEC}; font-size: {_font_px(11)}px;")
            form.addRow(lbl, row)

        self.profile = ProfileSelector(self.config.profiles_dir, self)
        lbl_prof = QLabel(translate_text("Profil décisionnel"))
        lbl_prof.setStyleSheet(f"color: {_C.TEXT_SEC}; font-size: {_font_px(11)}px;")
        form.addRow(lbl_prof, self.profile)

        cd_layout.addLayout(form)

        self.detect_cuts = QCheckBox(
            translate_text("Détecter les coupures et variations de cadences (cuts multi-segments)")
        )
        self.detect_cuts.setChecked(True)
        self.detect_cuts.setStyleSheet(_checkbox_style())
        cd_layout.addWidget(self.detect_cuts)

        layout.addWidget(card_dirs)

        # ── Carte 2 : Options d'hybridation (dépliables) ───────────────────────
        advanced = QGroupBox(translate_text("Options d'hybridation avancées"))
        advanced.setStyleSheet(_groupbox_checkable_style())
        settings = QFormLayout(advanced)
        settings.setSpacing(_scale(6))
        self.controls = {}
        for option, label, default in (
            ("auto-forced-subs", "Détecter les sous-titres forcés", False),
            ("auto-sdh", "Détecter les sous-titres SDH", False),
            ("no-clean-nfo", "Conserver le chemin complet dans le NFO", False),
            ("no-cover", "Sans jaquette TMDB", False),
        ):
            control = QCheckBox(translate_text(label))
            control.setChecked(default)
            control.setStyleSheet(_checkbox_style())
            settings.addRow(control)
            self.controls[option] = control

        for option, label, value, maximum in (
            ("forced-threshold", "Seuil forcés", 50, 10000),
            ("crossfade-ms", "Fondu aux raccords (ms)", 80, 1000),
            ("drift-threshold-ms", "Seuil de dérive (ms)", 25, 10000),
        ):
            control = QSpinBox()
            control.setStyleSheet(_input_style())
            control.setRange(0 if option == "crossfade-ms" else 1, maximum)
            control.setValue(value)
            lbl_spin = QLabel(translate_text(label))
            lbl_spin.setStyleSheet(f"color: {_C.TEXT_SEC}; font-size: {_font_px(11)}px;")
            settings.addRow(lbl_spin, control)
            self.controls[option] = control

        for option, label in (
            ("auto-tmdb", "ID TMDB"),
            ("tag", "Groupe de release"),
            ("output-template", "Template de sortie"),
            ("calibration", "Fichier de calibration"),
        ):
            control = QLineEdit("MVO" if option == "tag" else "")
            control.setStyleSheet(_input_style())
            lbl_txt = QLabel(translate_text(label))
            lbl_txt.setStyleSheet(f"color: {_C.TEXT_SEC}; font-size: {_font_px(11)}px;")
            settings.addRow(lbl_txt, control)
            self.controls[option] = control

        self.mode = QComboBox()
        self.mode.setStyleSheet(_input_style())
        self.mode.addItem(translate_text("Synchronisation physique"), "physical")
        self.mode.addItem(translate_text("Décalage conteneur"), "container")
        lbl_mode = QLabel(translate_text("Mode de synchro"))
        lbl_mode.setStyleSheet(f"color: {_C.TEXT_SEC}; font-size: {_font_px(11)}px;")
        settings.addRow(lbl_mode, self.mode)

        self.mirror = QCheckBox(translate_text("Recaler les sous-titres"))
        self.mirror.setChecked(True)
        self.mirror.setStyleSheet(_checkbox_style())
        settings.addRow(self.mirror)

        advanced.setCheckable(True)
        advanced.setChecked(False)
        advanced.toggled.connect(lambda checked: [
            settings.itemAt(i).widget().setVisible(checked)
            for i in range(settings.count()) if settings.itemAt(i).widget()
        ])
        advanced.toggled.emit(False)
        layout.addWidget(advanced)

        # ── Carte 3 : Tableau d'appariement ──────────────────────────────────
        card_table = _card(self)
        ct_layout = QVBoxLayout(card_table)
        ct_layout.setContentsMargins(_scale(12), _scale(10), _scale(12), _scale(10))
        ct_layout.setSpacing(_scale(8))
        ct_layout.addWidget(_section_label(translate_text("ÉPISODES APPARIÉS ET ÉTATS")))

        self.table = QTableWidget(0, 3)
        self.table.setStyleSheet(_table_style())
        self.table.setHorizontalHeaderLabels([
            translate_text("Référence"),
            translate_text("Donneur"),
            translate_text("État"),
        ])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setMinimumHeight(_scale(150))
        self.table.itemSelectionChanged.connect(self.select_waveform)
        ct_layout.addWidget(self.table)
        layout.addWidget(card_table)

        # ── Carte 4 : Forme d'onde et micro-ajustement ─────────────────────────
        card_wave = _card(self)
        cw_layout = QVBoxLayout(card_wave)
        cw_layout.setContentsMargins(_scale(12), _scale(10), _scale(12), _scale(10))
        cw_layout.setSpacing(_scale(8))

        wave_header = QHBoxLayout()
        wave_header.addWidget(_section_label(translate_text("ANALYSE ACOUSTIQUE ET CALIBRATION")))
        wave_header.addStretch()

        adj_label = QLabel(translate_text("Ajustement manuel :"))
        adj_label.setStyleSheet(f"color: {_C.TEXT_SEC}; font-size: {_font_px(11)}px;")
        wave_header.addWidget(adj_label)

        self.adjust = QDoubleSpinBox()
        self.adjust.setStyleSheet(_input_style())
        self.adjust.setRange(-60000, 60000)
        self.adjust.setDecimals(1)
        self.adjust.setSuffix(" ms")
        self.adjust.valueChanged.connect(self.adjust_offset)
        wave_header.addWidget(self.adjust)
        cw_layout.addLayout(wave_header)

        self.waveform = WaveformView(self)
        cw_layout.addWidget(self.waveform)
        layout.addWidget(card_wave)

        scroll.setWidget(container)
        outer_layout.addWidget(scroll, stretch=1)

        # ── Barre d'action inférieure ─────────────────────────────────────────
        bar = QHBoxLayout()
        bar.setSpacing(_scale(8))

        self.scan_button = _secondary_button(translate_text("Analyser la saison"))
        self.scan_button.clicked.connect(self.scan)
        bar.addWidget(self.scan_button)

        self.listen_button = _secondary_button(translate_text("Pré-écoute"))
        self.listen_button.clicked.connect(self.listen)
        bar.addWidget(self.listen_button)

        self.export_button = _secondary_button(translate_text("Sauvegarder les workflows…"))
        self.export_button.clicked.connect(self.export_jobs)
        bar.addWidget(self.export_button)

        self.cancel_button = _secondary_button(translate_text("Annuler"))
        self.cancel_button.clicked.connect(self.cancel)
        bar.addWidget(self.cancel_button)

        bar.addStretch()

        self.run_button = _primary_button(translate_text("Lancer l'hybridation"))
        self.run_button.setEnabled(False)
        self.run_button.clicked.connect(self.run)
        bar.addWidget(self.run_button)

        outer_layout.addLayout(bar)

        self.status = QLabel()
        self.status.setStyleSheet(f"color: {_C.TEXT_SEC}; font-size: {_font_px(11)}px;")
        outer_layout.addWidget(self.status)

        self.prepared.connect(self.on_prepared)
        self.failed.connect(self.on_failed)
        self.waveform_ready.connect(self.on_waveform)
        self.preview_ready.connect(self.play_preview)

    def browse(self, field: QLineEdit):
        path = QFileDialog.getExistingDirectory(self, translate_text("Choisir un dossier"), field.text())
        if path:
            field.setText(path)

    def set_busy(self, busy: bool):
        self.busy = busy
        self.scan_button.setEnabled(not busy)
        self.run_button.setEnabled(not busy and bool(self.jobs) and len(self.jobs) == len(self.pairs))
        for field in (
            self.reference,
            self.donor,
            self.output,
            self.profile,
            self.detect_cuts,
            self.mode,
            self.mirror,
            *self.controls.values(),
        ):
            field.setEnabled(not busy)

    def scan(self):
        from cli.hybrid import pairs_from_args
        from cli.parser import build_parser

        arguments = [
            "hybrid",
            "--ref-dir", self.reference.text(),
            "--donor-dir", self.donor.text(),
            "--output-dir", self.output.text(),
            "--dry-run",
        ]
        if self.profile.text().strip():
            arguments += ["--profile", self.profile.text().strip()]
        if self.detect_cuts.isChecked():
            arguments += ["--detect-cuts"]
        for option, control in self.controls.items():
            if isinstance(control, QCheckBox):
                if control.isChecked():
                    arguments.append("--" + option)
            elif isinstance(control, QSpinBox):
                arguments += ["--" + option, str(control.value())]
            elif control.text().strip():
                if option == "auto-tmdb" and not control.text().strip().isdigit():
                    self.on_failed(translate_text("ID TMDB invalide."))
                    return
                arguments += ["--" + option, control.text().strip()]
        arguments += [
            "--sync-mode", self.mode.currentData(),
            "--sync-subtitles", "mirror" if self.mirror.isChecked() else "none",
        ]
        try:
            if not self.output.text().strip():
                raise ValueError(translate_text("Choisir un dossier de sortie."))
            self.args = build_parser().parse_args(arguments)
            self.cancel_event.clear()
            self.args.cancel_event = self.cancel_event
            self.pairs = pairs_from_args(self.args)
        except Exception as exc:
            self.on_failed(str(exc))
            return
        self.jobs, self.index, self.stopped = [], 0, False
        self.table.setRowCount(len(self.pairs))
        for row, pair in enumerate(self.pairs):
            for column, text in enumerate((pair.reference.name, pair.donor.name, translate_text("En attente"))):
                self.table.setItem(row, column, QTableWidgetItem(text))
        self.set_busy(True)
        self.prepare_next()

    def prepare_next(self):
        if self.stopped or self.index >= len(self.pairs):
            self.set_busy(False)
            return
        pair = self.pairs[self.index]
        self.table.item(self.index, 2).setText(translate_text("Analyse en cours…"))

        def task():
            from cli.hybrid import prepare_pair
            from cli.logging import Logger
            try:
                config, calibration = prepare_pair(pair, self.args, self.config, Logger())
                self.prepared.emit(config, calibration)
            except Exception as exc:
                self.failed.emit(str(exc))

        self.executor.submit(task)

    def on_prepared(self, config, calibration):
        if self.stopped:
            self.set_busy(False)
            return
        self.jobs.append((config, calibration))
        item = self.table.item(self.index, 2)
        item.setText(f"{calibration.segments[0].shift_ms:+.1f} ms · {len(calibration.segments)} seg")
        color = QColor(_C.OK) if len(calibration.segments) == 1 else QColor(_C.WARN)
        item.setForeground(color)
        self.index += 1
        self.prepare_next()

    def on_failed(self, message: str):
        self.status.setText(translate_text("Hybridation impossible : {err}", err=message))
        if self.index < self.table.rowCount():
            item = self.table.item(self.index, 2)
            if item is not None:
                item.setForeground(QColor(_C.ERROR))
                item.setText(translate_text("Échec"))
        self.listen_button.setEnabled(True)
        self.aux_running = False
        self.set_busy(False)

    def select_waveform(self):
        row = self.table.currentRow()
        if self.busy or not 0 <= row < len(self.jobs):
            return
        config, calibration = self.jobs[row]
        self.adjust.blockSignals(True)
        self.adjust.setValue(calibration.segments[0].shift_ms)
        self.adjust.blockSignals(False)
        self.waveform.shift_ms = calibration.segments[0].shift_ms

        def task():
            import numpy as np
            scanner = AudioSyncScanner(self.config.tool_ffmpeg, self.config.tool_ffprobe)
            try:
                series = []
                for source in config.sources[:2]:
                    track = next(t for t in source.tracks if t.track_type == "audio" and t.enabled)
                    samples = scanner.samples(AudioSyncTrack(source.path, track.mkv_tid), 0, 20)
                    size = max(1, len(samples) // 1000)
                    series.append(np.max(np.abs(samples[:len(samples) // size * size].reshape(-1, size)), axis=1).tolist())
                self.waveform_ready.emit((row, series))
            except Exception as exc:
                self.failed.emit(str(exc))

        self.aux_running = True
        self.executor.submit(task)

    def on_waveform(self, result):
        self.aux_running = False
        row, series = result
        if row == self.table.currentRow():
            self.waveform.series = series
            self.waveform.update()

    def adjust_offset(self, value: float):
        row = self.table.currentRow()
        if self.busy or not 0 <= row < len(self.jobs):
            return
        config, calibration = self.jobs[row]
        delta = value - calibration.segments[0].shift_ms
        calibration = replace(
            calibration,
            segments=tuple(SyncSegment(s.start_ms, s.shift_ms + delta) for s in calibration.segments),
        )
        if config.sync_mode == "physical":
            config.sync_calibrations = {"1": calibration.to_dict()}
        else:
            for track in config.sources[1].tracks:
                if track.track_type == "audio" or (track.track_type == "subtitle" and config.sync_subtitles == "mirror"):
                    track.time_shift_ms = round(value)
        self.jobs[row] = config, calibration
        self.waveform.shift_ms = value
        self.waveform.update()

    def listen(self):
        row = self.table.currentRow()
        if self.busy or not 0 <= row < len(self.jobs):
            return

        has_qt_media = False
        try:
            from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
            has_qt_media = True
        except ImportError:
            has_qt_media = False

        if has_qt_media and not hasattr(self, "player"):
            try:
                self.player = QMediaPlayer(self)
                self.audio = QAudioOutput(self)
                self.player.setAudioOutput(self.audio)
            except Exception:
                has_qt_media = False

        config, calibration = self.jobs[row]
        self.listen_button.setEnabled(False)

        def task():
            from core.subprocess_utils import subprocess_text_kwargs
            from core.workflows.physical_sync import audio_filter

            source = config.sources[1]
            track = next(t for t in source.tracks if t.track_type == "audio" and t.enabled)
            path = Path(self.preview_temp.name) / (uuid.uuid4().hex + ".wav")
            graph = audio_filter(calibration, config.crossfade_ms).replace("[0:a:0]", f"[0:{track.mkv_tid}]")
            try:
                scan = AudioSyncScanner(self.config.tool_ffmpeg, self.config.tool_ffprobe, cancel_event=self.cancel_event)
                result = scan._run(
                    [
                        str(self.config.tool_ffmpeg), "-nostdin", "-v", "error", "-i", str(source.path),
                        "-filter_complex", graph, "-map", "[out]", "-t", "20", "-ac", "2",
                        "-c:a", "pcm_s16le", str(path),
                    ],
                    timeout=120,
                    **subprocess_text_kwargs(),
                )
                if result.returncode:
                    raise ValueError(result.stderr)
                self.preview_ready.emit(str(path))
            except Exception as exc:
                self.failed.emit(str(exc))

        self.aux_running = True
        self.executor.submit(task)

    def play_preview(self, path: str):
        self.aux_running = False
        self.listen_button.setEnabled(True)
        if hasattr(self, "player") and self.player is not None:
            self.player.setSource(QUrl.fromLocalFile(path))
            self.player.play()
        else:
            try:
                import subprocess
                ffplay = Path(str(self.config.tool_ffmpeg)).parent / "ffplay"
                if ffplay.exists():
                    subprocess.Popen([str(ffplay), "-nodisp", "-autoexit", path])
                else:
                    self.status.setText(f"Aperçu audio : {path}")
            except Exception:
                pass

    def export_jobs(self):
        if self.busy or not self.jobs:
            return
        directory = QFileDialog.getExistingDirectory(self, translate_text("Sauvegarder les workflows…"))
        if not directory:
            return
        from core.profiles.selectors import remux_config_to_exact_job
        from core.workflows.workflow_store import save_workflow

        try:
            for config, calibration in self.jobs:
                save_workflow(Path(directory) / (config.output.stem + ".exact-job.json"), remux_config_to_exact_job(config))
        except Exception as exc:
            self.on_failed(str(exc))

    def run(self):
        if self.busy or not self.jobs:
            return
        self.index, self.stopped = 0, False
        self.set_busy(True)
        self.run_next()

    def run_next(self):
        from core.workflows.remux import RemuxWorkflow

        if self.stopped or self.index >= len(self.jobs):
            self.set_busy(False)
            return
        config, _ = self.jobs[self.index]
        if config.output.exists():
            self.on_failed(translate_text("La sortie existe déjà : {path}", path=str(config.output)))
            return
        config.output.parent.mkdir(parents=True, exist_ok=True)
        self.workflow = RemuxWorkflow(
            ffmpeg_bin=str(self.config.tool_ffmpeg),
            ffprobe_bin=str(self.config.tool_ffprobe),
            mediainfo_bin=str(self.config.tool_mediainfo),
        )
        try:
            self.signals = self.workflow.run(replace(config, allow_missing_output_dir=False))
            self.signals.finished.connect(self.on_finished)
            self.signals.failed.connect(lambda message, exc: self.on_failed(message))
            self.signals.cancelled.connect(lambda: self.set_busy(False))
            self.signals.progress.connect(self.status.setText)
        except Exception as exc:
            self.on_failed(str(exc))

    def on_finished(self, message: str):
        item = self.table.item(self.index, 2)
        if item is not None:
            item.setText(translate_text("Terminé"))
            item.setForeground(QColor(_C.OK))
        self.index += 1
        self.run_next()

    def cancel(self):
        self.stopped = True
        self.cancel_event.set()
        if self.signals is not None:
            self.signals.cancel()

    def closeEvent(self, event):
        if self.busy or self.aux_running:
            self.cancel()
            event.ignore()
            return
        if hasattr(self, "player") and self.player is not None:
            try:
                self.player.stop()
                self.player.setSource(QUrl())
            except Exception:
                pass
        self.executor.shutdown(wait=False, cancel_futures=True)
        try:
            self.preview_temp.cleanup()
        except Exception:
            pass
        super().closeEvent(event)
