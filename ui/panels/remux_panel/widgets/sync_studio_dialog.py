"""Boîte de dialogue Synchro Studio : visualisation de forme d'onde, micro-ajustement et pré-écoute."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import uuid

import numpy as np
from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.i18n import translate_text
from core.subprocess_utils import subprocess_text_kwargs
from core.workflows.audio_sync import AudioSyncTrack
from core.workflows.audio_sync_scan import AudioSyncScanner
from core.workflows.physical_sync import audio_filter
from core.workflows.remux_models import TrackEntry
from core.workflows.sync_calibration import SyncCalibration, SyncSegment
from ui.design_system import colors as _C, font_px as _font_px, scale as _scale
from ui.panels.remux_panel.theme import (
    _card,
    _input_style,
    _play_icon,
    _primary_button,
    _scissors_icon,
    _secondary_button,
    _section_label,
    _stop_icon,
    _table_style,
    _waveform_icon,
)
from ui.widgets.waveform_view import WaveformView


class SyncStudioDialog(QDialog):
    """Dialogue complet d'inspection et d'ajustement acoustique pour le Remux Panel."""

    _waveform_ready = Signal(object)
    _waveform_loading = Signal(str)
    _preview_ready = Signal(str)
    _preview_error = Signal(str)

    def __init__(
        self,
        target_entry: TrackEntry,
        target_source_path: Path,
        target_stream_index: int,
        reference_entry: TrackEntry | None,
        reference_source_path: Path | None,
        reference_stream_index: int | None,
        calibration: SyncCalibration | dict | None = None,
        ffmpeg_bin: str = "ffmpeg",
        ffprobe_bin: str = "ffprobe",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.target_entry = target_entry
        self.target_source_path = Path(target_source_path)
        self.target_stream_index = target_stream_index
        self.reference_entry = reference_entry
        self.reference_source_path = Path(reference_source_path) if reference_source_path else None
        self.reference_stream_index = reference_stream_index
        self.ffmpeg_bin = str(ffmpeg_bin)
        self.ffprobe_bin = str(ffprobe_bin)

        # Résolution de la calibration initiale
        if isinstance(calibration, SyncCalibration):
            self._initial_calibration = calibration
        elif isinstance(calibration, dict):
            try:
                self._initial_calibration = SyncCalibration.from_dict(calibration)
            except Exception:
                self._initial_calibration = SyncCalibration.linear(target_entry.time_shift_ms or 0)
        else:
            self._initial_calibration = SyncCalibration.linear(target_entry.time_shift_ms or 0)

        self.current_calibration = self._initial_calibration
        self._initial_shift_ms = float(self._initial_calibration.segments[0].shift_ms)
        self._current_shift_ms = self._initial_shift_ms

        self._temp_dir = tempfile.TemporaryDirectory(prefix="mediarecode_sync_studio_")
        self._player = None
        self._audio_output = None
        self._is_playing = False

        self._waveform_ready.connect(self._on_waveform_data_ready)
        self._waveform_loading.connect(self._on_waveform_loading)
        self._preview_ready.connect(self._on_preview_ready)
        self._preview_error.connect(self._on_preview_error)

        self._init_ui()
        self._load_waveform_async()

    def _init_ui(self) -> None:
        self.setWindowTitle(
            translate_text(
                "Synchro Studio — Piste #{idx} ({codec})",
                idx=self.target_entry.mkv_tid,
                codec=self.target_entry.codec,
            )
        )
        self.setMinimumWidth(_scale(640))
        self.setMinimumHeight(_scale(520))

        vbox = QVBoxLayout(self)
        vbox.setContentsMargins(_scale(14), _scale(14), _scale(14), _scale(14))
        vbox.setSpacing(_scale(10))

        # ── Carte 1 : En-tête métadonnées ──────────────────────────────────────
        header_card = _card(self)
        hc_layout = QVBoxLayout(header_card)
        hc_layout.setContentsMargins(_scale(12), _scale(10), _scale(12), _scale(10))
        hc_layout.setSpacing(_scale(6))

        title_row = QHBoxLayout()
        title_lbl = QLabel(
            translate_text(
                "Piste #{idx} ({type} {codec}) — {lang}",
                idx=self.target_entry.mkv_tid,
                type=self.target_entry.track_type,
                codec=self.target_entry.codec,
                lang=self.target_entry.language or "und",
            )
        )
        title_lbl.setStyleSheet(f"font-weight: 700; font-size: {_font_px(13)}px; color: {_C.TEXT_PRI};")
        title_row.addWidget(title_lbl)
        title_row.addStretch()

        if self.current_calibration.confidence:
            conf_badge = QLabel(f"Confiance : {self.current_calibration.confidence:.0%}")
            conf_badge.setStyleSheet(f"color: {_C.OK}; font-size: {_font_px(11)}px; font-weight: 600;")
            title_row.addWidget(conf_badge)
        hc_layout.addLayout(title_row)

        ref_info = (
            f"{self.reference_source_path.name} (piste #{self.reference_stream_index})"
            if self.reference_source_path is not None
            else "Source principale"
        )
        tgt_info = f"{self.target_source_path.name} (piste #{self.target_stream_index})"

        info_lbl = QLabel(
            f"<b>{translate_text('Référence :')}</b> <span style='color: #20c997;'>{ref_info}</span><br>"
            f"<b>{translate_text('Cible :')}</b> <span style='color: {_C.ACCENT};'>{tgt_info}</span>"
        )
        info_lbl.setStyleSheet(f"color: {_C.TEXT_SEC}; font-size: {_font_px(11)}px;")
        hc_layout.addWidget(info_lbl)
        vbox.addWidget(header_card)

        # ── Carte 2 : Forme d'onde ─────────────────────────────────────────────
        wave_card = _card(self)
        wc_layout = QVBoxLayout(wave_card)
        wc_layout.setContentsMargins(_scale(12), _scale(10), _scale(12), _scale(10))
        wc_layout.setSpacing(_scale(6))

        wave_header = QHBoxLayout()
        wave_header.addWidget(_section_label(translate_text("FORME D'ONDE ACOUSTIQUE SUPERPOSÉE (20s)")))
        wave_header.addStretch()
        wc_layout.addLayout(wave_header)

        self.waveform = WaveformView(
            self,
            reference_label=translate_text("Référence"),
            target_label=translate_text("Cible décalée"),
        )
        self.waveform.set_shift(self._current_shift_ms)
        self.waveform.set_loading(translate_text("Chargement des signaux audio..."))
        wc_layout.addWidget(self.waveform)
        vbox.addWidget(wave_card)

        # ── Carte 3 : Micro-ajustement et Pré-écoute ───────────────────────────
        ctrl_card = _card(self)
        cc_layout = QVBoxLayout(ctrl_card)
        cc_layout.setContentsMargins(_scale(12), _scale(10), _scale(12), _scale(10))
        cc_layout.setSpacing(_scale(8))

        ctrl_row = QHBoxLayout()
        ctrl_row.setSpacing(_scale(8))

        adj_label = QLabel(translate_text("Décalage au départ :"))
        adj_label.setStyleSheet(f"color: {_C.TEXT_PRI}; font-size: {_font_px(11)}px; font-weight: 600;")
        ctrl_row.addWidget(adj_label)

        self.spin_shift = QDoubleSpinBox(self)
        self.spin_shift.setStyleSheet(_input_style())
        self.spin_shift.setRange(-60000.0, 60000.0)
        self.spin_shift.setDecimals(1)
        self.spin_shift.setSingleStep(10.0)
        self.spin_shift.setSuffix(" ms")
        self.spin_shift.setValue(self._current_shift_ms)
        self.spin_shift.valueChanged.connect(self._on_shift_spin_changed)
        ctrl_row.addWidget(self.spin_shift)

        # Boutons pas rapide
        for step in (-10.0, -1.0, 1.0, 10.0):
            sign = f"{step:+.0f}"
            btn = _secondary_button(f"{sign} ms", fixed_width=48)
            btn.clicked.connect(lambda _=None, s=step: self.spin_shift.setValue(self.spin_shift.value() + s))
            ctrl_row.addWidget(btn)

        reset_btn = _secondary_button(translate_text("Réinitialiser"), fixed_width=80)
        reset_btn.clicked.connect(lambda: self.spin_shift.setValue(self._initial_shift_ms))
        ctrl_row.addWidget(reset_btn)

        ctrl_row.addStretch()

        # Bouton Pré-écoute
        self.listen_btn = _secondary_button(translate_text("Pré-écoute calée (15s)"))
        self.listen_btn.setIcon(_play_icon())
        self.listen_btn.clicked.connect(self._toggle_listen)
        ctrl_row.addWidget(self.listen_btn)

        cc_layout.addLayout(ctrl_row)

        self.listen_status = QLabel("")
        self.listen_status.setStyleSheet(f"color: {_C.TEXT_SEC}; font-size: {_font_px(10)}px;")
        cc_layout.addWidget(self.listen_status)
        vbox.addWidget(ctrl_card)

        # ── Carte 4 : Coupures / Multi-segments (si applicable) ────────────────
        if self.current_calibration.cuts_count > 0:
            cuts_card = _card(self)
            cuts_layout = QVBoxLayout(cuts_card)
            cuts_layout.setContentsMargins(_scale(12), _scale(8), _scale(12), _scale(8))
            cuts_layout.setSpacing(_scale(6))

            cuts_header = QLabel(
                translate_text(
                    "Coupures et variations de rythme détectées ({count}) :",
                    count=self.current_calibration.cuts_count,
                )
            )
            cuts_header.setStyleSheet(f"font-weight: 700; font-size: {_font_px(11)}px; color: #e5a50a;")
            cuts_layout.addWidget(cuts_header)

            segments = self.current_calibration.segments
            self.cuts_table = QTableWidget(len(segments), 4, self)
            self.cuts_table.setStyleSheet(_table_style())
            self.cuts_table.setHorizontalHeaderLabels([
                translate_text("Segment"),
                translate_text("Position"),
                translate_text("Décalage"),
                translate_text("Saut relatif"),
            ])
            self.cuts_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
            self.cuts_table.verticalHeader().setVisible(False)
            self.cuts_table.setShowGrid(True)
            self.cuts_table.setAlternatingRowColors(True)
            self.cuts_table.setMaximumHeight(_scale(120))
            self._update_cuts_table()
            cuts_layout.addWidget(self.cuts_table)
            vbox.addWidget(cuts_card)
        else:
            self.cuts_table = None

        # ── Boutons de dialogue ────────────────────────────────────────────────
        btn_bar = QHBoxLayout()
        btn_bar.addStretch()

        cancel_btn = _secondary_button(translate_text("Annuler"))
        cancel_btn.clicked.connect(self.reject)
        btn_bar.addWidget(cancel_btn)

        apply_btn = _primary_button(translate_text("Appliquer la synchronisation"))
        apply_btn.clicked.connect(self.accept)
        btn_bar.addWidget(apply_btn)

        vbox.addLayout(btn_bar)

    def _update_cuts_table(self) -> None:
        if self.cuts_table is None:
            return
        segments = self.current_calibration.segments
        prev_shift = 0.0
        for i, s in enumerate(segments):
            ts = SyncCalibration.format_timestamp(s.start_ms)
            pos_label = (
                translate_text("Départ ({ts})", ts=ts)
                if i == 0
                else translate_text("Coupure à {ts}", ts=ts)
            )
            delta_label = "-" if i == 0 else f"{s.shift_ms - prev_shift:+.1f} ms"

            item_seg = QTableWidgetItem(f"Segment {i + 1}")
            item_pos = QTableWidgetItem(pos_label)
            item_shift = QTableWidgetItem(f"{s.shift_ms:+.1f} ms")
            item_delta = QTableWidgetItem(delta_label)

            for item in (item_seg, item_pos, item_shift, item_delta):
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)

            self.cuts_table.setItem(i, 0, item_seg)
            self.cuts_table.setItem(i, 1, item_pos)
            self.cuts_table.setItem(i, 2, item_shift)
            self.cuts_table.setItem(i, 3, item_delta)
            prev_shift = s.shift_ms

    def _on_shift_spin_changed(self, value: float) -> None:
        self._current_shift_ms = value
        self.waveform.set_shift(value)
        delta = value - self._initial_shift_ms
        self.current_calibration = replace(
            self._initial_calibration,
            segments=tuple(
                SyncSegment(s.start_ms, s.shift_ms + delta)
                for s in self._initial_calibration.segments
            ),
        )
        self._update_cuts_table()

    def _load_waveform_async(self) -> None:
        def _worker() -> None:
            scanner = AudioSyncScanner(self.ffmpeg_bin, self.ffprobe_bin)
            series = []
            try:
                # 1. Échantillons de référence
                if self.reference_source_path is not None and self.reference_stream_index is not None:
                    ref_track = AudioSyncTrack(self.reference_source_path, self.reference_stream_index)
                    ref_samples = scanner.samples(ref_track, 0, 20)
                    size = max(1, len(ref_samples) // 1000)
                    series.append(
                        np.max(
                            np.abs(ref_samples[: len(ref_samples) // size * size].reshape(-1, size)),
                            axis=1,
                        ).tolist()
                    )
                else:
                    series.append([])

                # 2. Échantillons de la cible
                tgt_track = AudioSyncTrack(self.target_source_path, self.target_stream_index)
                tgt_samples = scanner.samples(tgt_track, 0, 20)
                size = max(1, len(tgt_samples) // 1000)
                series.append(
                    np.max(
                        np.abs(tgt_samples[: len(tgt_samples) // size * size].reshape(-1, size)),
                        axis=1,
                    ).tolist()
                )
                self._waveform_ready.emit(series)
            except Exception as exc:
                self._waveform_loading.emit(f"Aperçu audio non disponible : {exc}")

        threading.Thread(target=_worker, daemon=True).start()

    def _on_waveform_loading(self, text: str) -> None:
        if getattr(self, "_closing", False):
            return
        self.waveform.set_loading(text)

    def _on_waveform_data_ready(self, series: list[list[float]]) -> None:
        if getattr(self, "_closing", False):
            return
        self.waveform.set_series(series)

    def _toggle_listen(self) -> None:
        if self._is_playing:
            self._stop_playback()
            return

        self.listen_btn.setEnabled(False)
        self.listen_status.setText(translate_text("Génération de l'extrait audio calé…"))

        def _worker() -> None:
            try:
                out_path = Path(self._temp_dir.name) / f"{uuid.uuid4().hex}.wav"
                scanner = AudioSyncScanner(self.ffmpeg_bin, self.ffprobe_bin)
                # Utiliser audio_filter avec la calibration courante
                graph = audio_filter(self.current_calibration, crossfade_ms=80).replace(
                    "[0:a:0]", f"[0:{self.target_stream_index}]"
                )
                cmd = [
                    self.ffmpeg_bin,
                    "-nostdin",
                    "-v",
                    "error",
                    "-i",
                    str(self.target_source_path),
                    "-filter_complex",
                    graph,
                    "-map",
                    "[out]",
                    "-t",
                    "15",
                    "-ac",
                    "2",
                    "-c:a",
                    "pcm_s16le",
                    str(out_path),
                ]
                res = scanner._run(cmd, timeout=30, **subprocess_text_kwargs())
                if res.returncode != 0:
                    raise RuntimeError(res.stderr)
                self._preview_ready.emit(str(out_path))
            except Exception as exc:
                self._preview_error.emit(str(exc))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_preview_ready(self, path: str) -> None:
        if getattr(self, "_closing", False):
            return
        self.listen_btn.setEnabled(True)
        try:
            from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
            if self._player is None:
                self._player = QMediaPlayer(self)
                self._audio_output = QAudioOutput(self)
                self._player.setAudioOutput(self._audio_output)
                self._player.mediaStatusChanged.connect(self._on_media_status_changed)

            self._player.setSource(QUrl.fromLocalFile(path))
            self._player.play()
            self._is_playing = True
            self.listen_btn.setText(translate_text("Arrêter"))
            self.listen_btn.setIcon(_stop_icon())
            self.listen_status.setText(translate_text("Lecture de l'extrait calé (15s)…"))
        except Exception:
            # Repli ffplay
            try:
                import subprocess
                ffplay = Path(self.ffmpeg_bin).parent / "ffplay"
                if ffplay.exists():
                    subprocess.Popen([str(ffplay), "-nodisp", "-autoexit", path])
                    self.listen_status.setText(translate_text("Lecture externe ffplay lancée."))
            except Exception as err:
                self.listen_status.setText(str(err))

    def _on_preview_error(self, message: str) -> None:
        if getattr(self, "_closing", False):
            return
        self.listen_btn.setEnabled(True)
        self.listen_status.setText(translate_text("Erreur lors de la pré-écoute : {err}", err=message))

    def _on_media_status_changed(self, status: object) -> None:
        try:
            from PySide6.QtMultimedia import QMediaPlayer
            if status == QMediaPlayer.MediaStatus.EndOfMedia:
                self._stop_playback()
        except Exception:
            pass

    def _stop_playback(self) -> None:
        if self._player is not None:
            try:
                self._player.stop()
            except Exception:
                pass
        self._is_playing = False
        self.listen_btn.setText(translate_text("Pré-écoute calée (15s)"))
        self.listen_btn.setIcon(_play_icon())
        self.listen_status.setText("")

    def result_calibration(self) -> tuple[SyncCalibration, int]:
        return self.current_calibration, round(self._current_shift_ms)

    def done(self, r: int) -> None:
        self._closing = True
        self._stop_playback()
        super().done(r)

    def closeEvent(self, event) -> None:
        self._closing = True
        self._stop_playback()
        try:
            self._temp_dir.cleanup()
        except Exception:
            pass
        super().closeEvent(event)
