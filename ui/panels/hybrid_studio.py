"""Studio de saison : appariement, calibration, écoute et exécution séquentielle."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import uuid

from PySide6.QtCore import Signal, QUrl
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import (QCheckBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget, QComboBox, QSpinBox, QGroupBox)

from core.i18n import translate_text
from core.workflows.audio_sync import AudioSyncTrack
from core.workflows.audio_sync_scan import AudioSyncScanner
from core.workflows.sync_calibration import SyncSegment


class DirectoryEdit(QLineEdit):
    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if len(urls) == 1 and Path(urls[0].toLocalFile()).is_dir():
            self.setText(urls[0].toLocalFile())
            event.acceptProposedAction()


class WaveformView(QWidget):
    """Enveloppes audio superposables ; le décalage est exprimé en ms."""
    def __init__(self):
        super().__init__()
        self.setMinimumHeight(150)
        self.series = ([], [])
        self.shift_ms = 0
        self.duration_ms = 20000

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.palette().base())
        for row, values in enumerate(self.series):
            if not len(values):
                continue
            middle = self.height() * (0.25 + row * 0.5)
            path = QPainterPath()
            scale = max(max(values), 1e-9)
            delta = self.shift_ms / self.duration_ms * self.width() if row else 0
            for i, value in enumerate(values):
                x = i * self.width() / max(1, len(values) - 1) + delta
                y = middle - value / scale * self.height() * 0.22
                path.moveTo(x, y) if i == 0 else path.lineTo(x, y)
            painter.setPen(QColor("#36b37e" if row == 0 else "#e5a642"))
            painter.drawPath(path)


class HybridStudio(QDialog):
    prepared = Signal(object, object)
    failed = Signal(str)
    waveform_ready = Signal(object)
    preview_ready = Signal(str)

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.setWindowTitle(translate_text("Studio Hybridation"))
        self.resize(900, 650)
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.jobs, self.pairs = [], []
        self.index = 0
        self.stopped = False
        self.cancel_event = threading.Event()
        self.signals = None
        self.busy = False
        self.aux_running = False
        self.preview_temp = tempfile.TemporaryDirectory(prefix="Muxiveo_listen_")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.reference, self.donor, self.output = DirectoryEdit(), DirectoryEdit(), DirectoryEdit()
        for label, edit in (("Référence", self.reference), ("Donneur", self.donor), ("Sortie", self.output)):
            row = QHBoxLayout()
            row.addWidget(edit)
            browse = QPushButton("…")
            browse.clicked.connect(lambda checked=False, field=edit: self.browse(field))
            row.addWidget(browse)
            form.addRow(translate_text(label), row)
        self.profile = QLineEdit()
        form.addRow(translate_text("Profil décisionnel"), self.profile)
        self.detect_cuts = QCheckBox(translate_text("Détecter les coupures"))
        self.detect_cuts.setChecked(True)
        form.addRow(self.detect_cuts)
        layout.addLayout(form)
        advanced = QGroupBox(translate_text("Options d'hybridation"))
        settings = QFormLayout(advanced)
        self.controls = {}
        for option, label, default in (
            ("auto-forced-subs", "Détecter les sous-titres forcés", False),
            ("auto-sdh", "Détecter les sous-titres SDH", False),
            ("no-clean-nfo", "Conserver le chemin complet dans le NFO", False),
            ("no-cover", "Sans jaquette TMDB", False),
        ):
            control = QCheckBox(translate_text(label))
            control.setChecked(default)
            settings.addRow(control)
            self.controls[option] = control
        for option, label, value, maximum in (
            ("forced-threshold", "Seuil forcés", 50, 10000),
            ("crossfade-ms", "Fondu aux raccords (ms)", 80, 1000),
            ("drift-threshold-ms", "Seuil de dérive (ms)", 25, 10000),
        ):
            control = QSpinBox()
            control.setRange(0 if option == "crossfade-ms" else 1, maximum)
            control.setValue(value)
            settings.addRow(translate_text(label), control)
            self.controls[option] = control
        for option, label in (("auto-tmdb", "ID TMDB"), ("tag", "Groupe de release"),
                              ("output-template", "Template de sortie"), ("calibration", "Fichier de calibration")):
            control = QLineEdit("MVO" if option == "tag" else "")
            settings.addRow(translate_text(label), control)
            self.controls[option] = control
        self.mode = QComboBox()
        self.mode.addItem(translate_text("Synchronisation physique"), "physical")
        self.mode.addItem(translate_text("Décalage conteneur"), "container")
        settings.addRow(self.mode)
        self.mirror = QCheckBox(translate_text("Recaler les sous-titres"))
        self.mirror.setChecked(True)
        settings.addRow(self.mirror)
        advanced.setCheckable(True)
        advanced.setChecked(False)
        advanced.toggled.connect(lambda checked: [settings.itemAt(i).widget().setVisible(checked)
            for i in range(settings.count()) if settings.itemAt(i).widget()])
        advanced.toggled.emit(False)
        layout.addWidget(advanced)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels([translate_text("Référence"), translate_text("Donneur"), translate_text("État")])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self.select_waveform)
        layout.addWidget(self.table)
        self.waveform = WaveformView()
        layout.addWidget(self.waveform)
        self.adjust = QDoubleSpinBox()
        self.adjust.setRange(-60000, 60000)
        self.adjust.setDecimals(1)
        self.adjust.setSuffix(" ms")
        self.adjust.valueChanged.connect(self.adjust_offset)
        layout.addWidget(self.adjust)
        row = QHBoxLayout()
        self.scan_button = QPushButton(translate_text("Analyser la saison"))
        self.run_button = QPushButton(translate_text("Lancer l'hybridation"))
        self.listen_button = QPushButton(translate_text("Pré-écoute"))
        self.cancel_button = QPushButton(translate_text("Annuler"))
        self.export_button = QPushButton(translate_text("Sauvegarder les workflows…"))
        self.export_button.clicked.connect(self.export_jobs)
        row.addWidget(self.export_button)
        self.run_button.setEnabled(False)
        for button in (self.scan_button, self.run_button, self.listen_button, self.cancel_button):
            row.addWidget(button)
        layout.addLayout(row)
        self.status = QLabel()
        layout.addWidget(self.status)
        self.scan_button.clicked.connect(self.scan)
        self.run_button.clicked.connect(self.run)
        self.listen_button.clicked.connect(self.listen)
        self.cancel_button.clicked.connect(self.cancel)
        self.prepared.connect(self.on_prepared)
        self.failed.connect(self.on_failed)
        self.waveform_ready.connect(self.on_waveform)
        self.preview_ready.connect(self.play_preview)

    def browse(self, field):
        path = QFileDialog.getExistingDirectory(self, translate_text("Choisir un dossier"), field.text())
        if path:
            field.setText(path)

    def set_busy(self, busy):
        self.busy = busy
        self.scan_button.setEnabled(not busy)
        self.run_button.setEnabled(not busy and bool(self.jobs) and len(self.jobs) == len(self.pairs))
        for field in (self.reference, self.donor, self.output, self.profile, self.detect_cuts, self.mode, self.mirror, *self.controls.values()):
            field.setEnabled(not busy)

    def scan(self):
        from cli.parser import build_parser
        from cli.hybrid import pairs_from_args
        arguments = ["hybrid", "--ref-dir", self.reference.text(), "--donor-dir", self.donor.text(),
                     "--output-dir", self.output.text(), "--dry-run"]
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
        arguments += ["--sync-mode", self.mode.currentData(), "--sync-subtitles", "mirror" if self.mirror.isChecked() else "none"]
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
        item.setText(f"{calibration.segments[0].shift_ms:+.1f} ms · {len(calibration.segments)}")
        item.setForeground(QColor("#36b37e" if len(calibration.segments) == 1 else "#e5a642"))
        self.index += 1
        self.prepare_next()

    def on_failed(self, message):
        self.status.setText(translate_text("Hybridation impossible : {err}", err=message))
        if self.index < self.table.rowCount():
            self.table.item(self.index, 2).setForeground(QColor("#de350b"))
            self.table.item(self.index, 2).setText(translate_text("Échec"))
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

    def adjust_offset(self, value):
        row = self.table.currentRow()
        if self.busy or not 0 <= row < len(self.jobs):
            return
        config, calibration = self.jobs[row]
        delta = value - calibration.segments[0].shift_ms
        calibration = replace(calibration, segments=tuple(SyncSegment(s.start_ms, s.shift_ms + delta) for s in calibration.segments))
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
        # Qt Multimedia conserve les handles Windows jusqu'à stop()/setSource().
        row = self.table.currentRow()
        if self.busy or not 0 <= row < len(self.jobs):
            return
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
        if not hasattr(self, "player"):
            self.player, self.audio = QMediaPlayer(self), QAudioOutput(self)
            self.player.setAudioOutput(self.audio)
        config, calibration = self.jobs[row]
        self.listen_button.setEnabled(False)
        def task():
            from core.workflows.physical_sync import audio_filter
            from core.subprocess_utils import subprocess_text_kwargs
            source = config.sources[1]
            track = next(t for t in source.tracks if t.track_type == "audio" and t.enabled)
            path = Path(self.preview_temp.name) / (uuid.uuid4().hex + ".wav")
            graph = audio_filter(calibration, config.crossfade_ms).replace("[0:a:0]", f"[0:{track.mkv_tid}]")
            try:
                scan = AudioSyncScanner(self.config.tool_ffmpeg, self.config.tool_ffprobe, cancel_event=self.cancel_event)
                result = scan._run([str(self.config.tool_ffmpeg), "-nostdin", "-v", "error", "-i", str(source.path),
                    "-filter_complex", graph, "-map", "[out]", "-t", "20", "-ac", "2", "-c:a", "pcm_s16le", str(path)],
                    timeout=120, **subprocess_text_kwargs())
                if result.returncode:
                    raise ValueError(result.stderr)
                self.preview_ready.emit(str(path))
            except Exception as exc:
                self.failed.emit(str(exc))
        self.aux_running = True
        self.executor.submit(task)

    def play_preview(self, path):
        self.aux_running = False
        self.listen_button.setEnabled(True)
        self.player.setSource(QUrl.fromLocalFile(path))
        self.player.play()

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
        self.workflow = RemuxWorkflow(ffmpeg_bin=str(self.config.tool_ffmpeg), ffprobe_bin=str(self.config.tool_ffprobe),
                                      mediainfo_bin=str(self.config.tool_mediainfo))
        try:
            self.signals = self.workflow.run(replace(config, allow_missing_output_dir=False))
            self.signals.finished.connect(self.on_finished)
            self.signals.failed.connect(lambda message, exc: self.on_failed(message))
            self.signals.cancelled.connect(lambda: self.set_busy(False))
            self.signals.progress.connect(self.status.setText)
        except Exception as exc:
            self.on_failed(str(exc))

    def on_finished(self, message):
        self.table.item(self.index, 2).setText(translate_text("Terminé"))
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
        if hasattr(self, "player"):
            self.player.stop()
            self.player.setSource(QUrl())
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.preview_temp.cleanup()
        super().closeEvent(event)
