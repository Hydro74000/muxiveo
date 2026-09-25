"""Boîte de dialogue Synchro Studio : visualisation de forme d'onde, micro-ajustement et pré-écoute."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import uuid

import numpy as np
from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QRadioButton,
    QScrollBar,
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
    _secondary_button,
    _section_label,
    _stop_icon,
    _table_style,
)
from ui.widgets.waveform_view import WaveformView


class SyncStudioDialog(QDialog):
    """Dialogue complet d'inspection et d'ajustement acoustique pour le Remux Panel."""

    _waveform_loading = Signal(str)
    _segment_audio_ready = Signal(int, object, object, float, object)
    _subtitles_ready = Signal(object, object)
    _auto_sub_sync_ready = Signal(object)
    _auto_sub_sync_failed = Signal(str)
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

        # Gestion des segments et navigation
        self._current_segment_index: int = 0
        self._current_start_s: float = 0.0
        self._current_cut_ms: float | None = None
        self._audio_cache: dict[int, tuple[object, object]] = {}

        # Données de sous-titres
        self._ref_cues: list = []
        self._tgt_cues: list = []
        self._is_sub_sync_running: bool = False

        self._temp_dir = tempfile.TemporaryDirectory(prefix="mediarecode_sync_studio_")
        self._player = None
        self._audio_output = None
        self._is_playing = False
        self._closing = False

        self._waveform_loading.connect(self._on_waveform_loading)
        self._segment_audio_ready.connect(self._on_segment_audio_ready)
        self._subtitles_ready.connect(self._on_subtitles_ready)
        self._auto_sub_sync_ready.connect(self._on_auto_sub_sync_ready)
        self._auto_sub_sync_failed.connect(self._on_auto_sub_sync_failed)
        self._preview_ready.connect(self._on_preview_ready)
        self._preview_error.connect(self._on_preview_error)

        self._init_ui()
        self._select_segment(0)

    def _shift_label_text(self) -> str:
        if len(self.current_calibration.segments) <= 1:
            return translate_text("Décalage au départ :")
        return translate_text("Décalage du segment {idx} :", idx=self._current_segment_index + 1)

    def _init_ui(self) -> None:
        self.setWindowTitle(
            translate_text(
                "Synchro Studio — Piste #{idx} ({codec})",
                idx=self.target_entry.mkv_tid,
                codec=self.target_entry.codec,
            )
        )
        self.resize(_scale(980), _scale(740))
        self.setMinimumWidth(_scale(850))
        self.setMinimumHeight(_scale(650))

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

        # Section Cadence & Hauteur tonale (Pitch)
        if self.current_calibration.cadence_mismatch and getattr(self.current_calibration.cadence_mismatch, "cadence_type", None) not in (None, "none", "NONE"):
            mismatch = self.current_calibration.cadence_mismatch
            pitch = getattr(self.current_calibration, "cadence_pitch_analysis", None)

            cadence_frame = QFrame()
            cadence_frame.setStyleSheet(f"""
                QFrame {{
                    background: {_C.BG_DEEP};
                    border: 1px solid {_C.BORDER};
                    border-radius: {_scale(6)}px;
                }}
            """)
            cad_layout = QVBoxLayout(cadence_frame)
            cad_layout.setContentsMargins(_scale(8), _scale(6), _scale(8), _scale(6))
            cad_layout.setSpacing(_scale(4))

            top_row = QHBoxLayout()
            lbl_cad_title = QLabel(f"<b>{translate_text('Conversion Cadence :')}</b> {mismatch.description}")
            lbl_cad_title.setStyleSheet(f"color: {_C.TEXT_PRI}; font-size: {_font_px(11)}px;")
            top_row.addWidget(lbl_cad_title)
            top_row.addStretch()

            best_method = "atempo"
            if pitch:
                f0_ref = getattr(pitch, "f0_ref_hz", 0.0) or (pitch.get("f0_ref_hz", 0.0) if isinstance(pitch, dict) else 0.0)
                f0_don = getattr(pitch, "f0_donor_hz", 0.0) or (pitch.get("f0_donor_hz", 0.0) if isinstance(pitch, dict) else 0.0)
                shift_semi = getattr(pitch, "pitch_shift_semitones", 0.0) or (pitch.get("pitch_shift_semitones", 0.0) if isinstance(pitch, dict) else 0.0)
                c_ase = getattr(pitch, "spectral_corr_asetrate", 0.0) or (pitch.get("spectral_corr_asetrate", 0.0) if isinstance(pitch, dict) else 0.0)
                c_ate = getattr(pitch, "spectral_corr_atempo", 0.0) or (pitch.get("spectral_corr_atempo", 0.0) if isinstance(pitch, dict) else 0.0)
                best_method = getattr(pitch, "selected_method", "atempo") or (pitch.get("selected_method", "atempo") if isinstance(pitch, dict) else "atempo")

                metric_lbl = QLabel(
                    f"Pitch : {shift_semi:+.2f} demi-ton (F0 : {f0_ref:.0f} Hz vs {f0_don:.0f} Hz) "
                    f"• Corr. spectrale : asetrate {c_ase:.2f} | atempo {c_ate:.2f} "
                    f"• Recommandé : <b>{best_method}</b>"
                )
                metric_lbl.setStyleSheet(f"color: {_C.OK}; font-size: {_font_px(11)}px;")
                top_row.addWidget(metric_lbl)

            cad_layout.addLayout(top_row)

            # Sélecteur de méthode (Radio buttons)
            radio_row = QHBoxLayout()
            radio_row.setSpacing(_scale(12))
            lbl_choose = QLabel(translate_text("Méthode audio :"))
            lbl_choose.setStyleSheet(f"color: {_C.TEXT_SEC}; font-size: {_font_px(11)}px;")
            radio_row.addWidget(lbl_choose)

            self.bg_cadence = QButtonGroup(self)
            current_method = getattr(self.current_calibration, "cadence_audio_method", "auto") or "auto"

            auto_label = translate_text("Auto (détecté : {m})", m=best_method) if pitch else translate_text("Auto (détection pitch)")
            self.rb_auto = QRadioButton(auto_label)
            self.rb_asetrate = QRadioButton(translate_text("asetrate (puriste PAL / pitch naturel)"))
            self.rb_atempo = QRadioButton(translate_text("atempo (tonalité préservée)"))

            for rb in (self.rb_auto, self.rb_asetrate, self.rb_atempo):
                rb.setStyleSheet(f"color: {_C.TEXT_PRI}; font-size: {_font_px(11)}px;")
                self.bg_cadence.addButton(rb)
                radio_row.addWidget(rb)

            if current_method == "asetrate":
                self.rb_asetrate.setChecked(True)
            elif current_method == "atempo":
                self.rb_atempo.setChecked(True)
            else:
                self.rb_auto.setChecked(True)

            self.bg_cadence.buttonClicked.connect(self._on_cadence_method_changed)
            radio_row.addStretch()
            cad_layout.addLayout(radio_row)
            hc_layout.addWidget(cadence_frame)

        vbox.addWidget(header_card)

        # ── Carte 2 : Forme d'onde avec Zoom & Navigation ─────────────────────
        wave_card = _card(self)
        wc_layout = QVBoxLayout(wave_card)
        wc_layout.setContentsMargins(_scale(12), _scale(10), _scale(12), _scale(10))
        wc_layout.setSpacing(_scale(6))

        wave_header = QHBoxLayout()
        wave_header.setSpacing(_scale(8))
        section_title = (
            translate_text("FORME D'ONDE & SOUS-TITRES")
            if (self.target_entry.is_subtitle or (self.reference_entry and self.reference_entry.is_subtitle))
            else translate_text("FORME D'ONDE ACOUSTIQUE")
        )
        wave_header.addWidget(_section_label(section_title))

        # Indicateur de segment actif
        self.seg_indicator = QLabel()
        self.seg_indicator.setStyleSheet(
            f"background: {_C.BG_CARD}; color: {_C.TEXT_PRI}; font-size: {_font_px(11)}px; "
            f"font-weight: 600; padding: {_scale(2)}px {_scale(8)}px; border-radius: {_scale(4)}px; border: 1px solid {_C.BORDER};"
        )
        wave_header.addWidget(self.seg_indicator)

        if self.current_calibration.cuts_count > 0:
            self.btn_prev_seg = _secondary_button("◀", fixed_width=30, padding_h=2)
            self.btn_prev_seg.setToolTip(translate_text("Segment précédent"))
            self.btn_prev_seg.clicked.connect(self._prev_segment)
            wave_header.addWidget(self.btn_prev_seg)

            self.btn_next_seg = _secondary_button("▶", fixed_width=30, padding_h=2)
            self.btn_next_seg.setToolTip(translate_text("Segment suivant"))
            self.btn_next_seg.clicked.connect(self._next_segment)
            wave_header.addWidget(self.btn_next_seg)
        else:
            self.btn_prev_seg = None
            self.btn_next_seg = None

        wave_header.addStretch()

        # Barre d'outils de Zoom
        self.btn_zoom_out = _secondary_button("−", fixed_width=30, padding_h=2)
        self.btn_zoom_out.setToolTip(translate_text("Zoom arrière (Ctrl+Molette bas)"))
        self.btn_zoom_out.clicked.connect(lambda: self.waveform.zoom_out())
        wave_header.addWidget(self.btn_zoom_out)

        self.lbl_zoom = QLabel("1.0x")
        self.lbl_zoom.setStyleSheet(
            f"color: {_C.TEXT_SEC}; font-size: {_font_px(11)}px; font-weight: 600; min-width: {_scale(40)}px; qproperty-alignment: AlignCenter;"
        )
        wave_header.addWidget(self.lbl_zoom)

        self.btn_zoom_in = _secondary_button("+", fixed_width=30, padding_h=2)
        self.btn_zoom_in.setToolTip(translate_text("Zoom avant (Ctrl+Molette haut)"))
        self.btn_zoom_in.clicked.connect(lambda: self.waveform.zoom_in())
        wave_header.addWidget(self.btn_zoom_in)

        self.btn_zoom_reset = _secondary_button(translate_text("Vue 20s"), padding_h=8)
        self.btn_zoom_reset.setToolTip(translate_text("Réinitialiser le zoom"))
        self.btn_zoom_reset.clicked.connect(lambda: self.waveform.reset_zoom())
        wave_header.addWidget(self.btn_zoom_reset)

        # Bascule Vue Scindée / Superposée
        self.btn_mode_switch = _secondary_button(translate_text("Vue : Scindée"), padding_h=8)
        self.btn_mode_switch.setToolTip(translate_text("Passer en vue superposée (même axe)"))
        self.btn_mode_switch.clicked.connect(self._toggle_waveform_mode)
        wave_header.addWidget(self.btn_mode_switch)

        wc_layout.addLayout(wave_header)

        # Widget Waveform
        self.waveform = WaveformView(
            self,
            reference_label=translate_text("Référence"),
            target_label=translate_text("Cible décalée"),
        )
        self.waveform.set_shift(self._current_shift_ms)
        self.waveform.set_loading(translate_text("Chargement des signaux audio..."))
        wc_layout.addWidget(self.waveform)

        # Barre de défilement horizontal synchronisée avec le zoom / pan
        self.zoom_scrollbar = QScrollBar(Qt.Orientation.Horizontal, self)
        self.zoom_scrollbar.setFixedHeight(_scale(12))
        self.zoom_scrollbar.setStyleSheet(f"""
            QScrollBar:horizontal {{
                background: {_C.BG_DEEP};
                height: {_scale(10)}px;
                border-radius: {_scale(4)}px;
                margin: 0px;
            }}
            QScrollBar::handle:horizontal {{
                background: {_C.BORDER_LT};
                min-width: {_scale(20)}px;
                border-radius: {_scale(4)}px;
            }}
            QScrollBar::handle:horizontal:hover {{
                background: {_C.ACCENT};
            }}
        """)
        self.zoom_scrollbar.setVisible(False)
        self.zoom_scrollbar.valueChanged.connect(self._on_scrollbar_value_changed)
        self.waveform.pan_changed.connect(self._on_waveform_pan_changed)
        self.waveform.zoom_changed.connect(self._on_waveform_zoom_changed)
        wc_layout.addWidget(self.zoom_scrollbar)

        # Astuce d'interaction
        hint_lbl = QLabel(
            "💡 " + translate_text("Astuce : Ctrl + Molette pour zoomer • Glisser pour faire défiler la forme d'onde")
        )
        hint_lbl.setStyleSheet(f"color: {_C.TEXT_DIM}; font-size: {_font_px(10)}px;")
        wc_layout.addWidget(hint_lbl)

        vbox.addWidget(wave_card)

        # ── Carte 3 : Micro-ajustement et Pré-écoute ───────────────────────────
        ctrl_card = _card(self)
        cc_layout = QVBoxLayout(ctrl_card)
        cc_layout.setContentsMargins(_scale(12), _scale(10), _scale(12), _scale(10))
        cc_layout.setSpacing(_scale(8))

        ctrl_row = QHBoxLayout()
        ctrl_row.setSpacing(_scale(8))

        self.lbl_shift_title = QLabel(self._shift_label_text())
        self.lbl_shift_title.setStyleSheet(f"color: {_C.TEXT_PRI}; font-size: {_font_px(11)}px; font-weight: 600;")
        ctrl_row.addWidget(self.lbl_shift_title)

        self.spin_shift = QDoubleSpinBox(self)
        self.spin_shift.setStyleSheet(_input_style())
        self.spin_shift.setMinimumWidth(_scale(120))
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
            btn = _secondary_button(f"{sign} ms", min_width=56, padding_h=6)
            btn.clicked.connect(lambda _=None, s=step: self.spin_shift.setValue(self.spin_shift.value() + s))
            ctrl_row.addWidget(btn)

        reset_btn = _secondary_button(translate_text("Réinitialiser"), min_width=75, padding_h=10)
        reset_btn.clicked.connect(self._reset_shifts)
        ctrl_row.addWidget(reset_btn)

        ctrl_row.addStretch()

        if self.target_entry.is_subtitle:
            self.btn_auto_sub_sync = _secondary_button(translate_text("⚡ Synchro sous-titres"), padding_h=10)
            self.btn_auto_sub_sync.setToolTip(translate_text("Lancer l'alignement automatique des sous-titres"))
            self.btn_auto_sub_sync.clicked.connect(self._run_auto_subtitle_sync)
            ctrl_row.addWidget(self.btn_auto_sub_sync)

        # Bouton Pré-écoute
        self.listen_btn = _secondary_button(translate_text("Pré-écoute calée (15s)"), padding_h=12)
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
            self.cuts_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
            self.cuts_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
            self.cuts_table.setMaximumHeight(_scale(120))
            self.cuts_table.cellClicked.connect(self._on_cuts_cell_clicked)
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

    def _select_segment(self, idx: int) -> None:
        total = len(self.current_calibration.segments)
        if idx < 0 or idx >= total:
            return
        self._current_segment_index = idx
        seg = self.current_calibration.segments[idx]

        ts = SyncCalibration.format_timestamp(seg.start_ms)
        pos_label = translate_text("Départ ({ts})", ts=ts) if idx == 0 else translate_text("Coupure à {ts}", ts=ts)
        self.seg_indicator.setText(
            translate_text("Segment {current}/{total} — {pos}", current=idx + 1, total=total, pos=pos_label)
        )
        if self.btn_prev_seg is not None:
            self.btn_prev_seg.setEnabled(idx > 0)
        if self.btn_next_seg is not None:
            self.btn_next_seg.setEnabled(idx < total - 1)

        self.lbl_shift_title.setText(self._shift_label_text())
        self.spin_shift.blockSignals(True)
        self.spin_shift.setValue(seg.shift_ms)
        self.spin_shift.blockSignals(False)

        if self.cuts_table is not None:
            self.cuts_table.blockSignals(True)
            self.cuts_table.selectRow(idx)
            self.cuts_table.blockSignals(False)

        if idx == 0:
            start_s = 0.0
            cut_ms = None
        else:
            start_s = max(0.0, (seg.start_ms - 2000.0) / 1000.0)
            cut_ms = seg.start_ms

        self._current_start_s = start_s
        self._current_cut_ms = cut_ms

        self._load_segment_audio_async(idx, start_s, cut_ms)

    def _prev_segment(self) -> None:
        if self._current_segment_index > 0:
            self._select_segment(self._current_segment_index - 1)

    def _next_segment(self) -> None:
        if self._current_segment_index < len(self.current_calibration.segments) - 1:
            self._select_segment(self._current_segment_index + 1)

    def _on_cuts_cell_clicked(self, row: int, _col: int) -> None:
        self._select_segment(row)

    def _on_scrollbar_value_changed(self, value: int) -> None:
        self.waveform.set_pan_offset_ms(float(value))

    def _on_waveform_zoom_changed(self, zoom_factor: float, _pan_ms: float) -> None:
        self.lbl_zoom.setText(f"{zoom_factor:.1f}x")
        self.zoom_scrollbar.setVisible(zoom_factor > 1.05)

    def _on_waveform_pan_changed(self, pan_ms: float, visible_ms: float, window_ms: float) -> None:
        self.zoom_scrollbar.blockSignals(True)
        max_pan = max(0, int(window_ms - visible_ms))
        self.zoom_scrollbar.setRange(0, max_pan)
        self.zoom_scrollbar.setPageStep(int(visible_ms))
        self.zoom_scrollbar.setValue(int(pan_ms))
        self.zoom_scrollbar.blockSignals(False)

    def _toggle_waveform_mode(self) -> None:
        new_mode = self.waveform.toggle_display_mode()
        if new_mode == "overlay":
            self.btn_mode_switch.setText(translate_text("Vue : Superposée"))
            self.btn_mode_switch.setToolTip(translate_text("Passer en vue scindée (haut/bas)"))
        else:
            self.btn_mode_switch.setText(translate_text("Vue : Scindée"))
            self.btn_mode_switch.setToolTip(translate_text("Passer en vue superposée (même axe)"))

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

        if 0 <= self._current_segment_index < len(segments):
            self.cuts_table.selectRow(self._current_segment_index)

    def _on_shift_spin_changed(self, value: float) -> None:
        self.waveform.set_shift(value)
        old_shift = self.current_calibration.segments[self._current_segment_index].shift_ms
        delta = value - old_shift

        # Propagation du delta vers les segments suivants pour préserver les sauts relatifs
        new_segments = []
        for i, s in enumerate(self.current_calibration.segments):
            if i < self._current_segment_index:
                new_segments.append(s)
            else:
                new_segments.append(SyncSegment(s.start_ms, s.shift_ms + delta))

        self.current_calibration = replace(self.current_calibration, segments=tuple(new_segments))
        self._current_shift_ms = self.current_calibration.segments[0].shift_ms
        self._update_cuts_table()

    def _reset_shifts(self) -> None:
        self.current_calibration = self._initial_calibration
        seg = self.current_calibration.segments[self._current_segment_index]
        self.spin_shift.blockSignals(True)
        self.spin_shift.setValue(seg.shift_ms)
        self.spin_shift.blockSignals(False)
        self.waveform.set_shift(seg.shift_ms)
        self._update_cuts_table()

    def _load_segment_audio_async(self, idx: int, start_s: float, cut_ms: float | None) -> None:
        if idx in self._audio_cache:
            ref_samples, tgt_samples = self._audio_cache[idx]
            self._apply_audio_to_waveform(idx, ref_samples, tgt_samples, start_s, cut_ms)
            return

        is_sub = self.target_entry.is_subtitle
        loading_msg = (
            translate_text("Extraction des données du segment {idx}…", idx=idx + 1)
            if is_sub
            else translate_text("Extraction audio du segment {idx}…", idx=idx + 1)
        )
        self.waveform.set_loading(loading_msg)

        def _worker() -> None:
            try:
                is_tgt_sub = self.target_entry.is_subtitle
                is_ref_sub = self.reference_entry is not None and self.reference_entry.is_subtitle

                # Extraction des sous-titres si applicable
                if (is_tgt_sub or is_ref_sub) and (not self._tgt_cues and not self._ref_cues):
                    from core.workflows.subtitle_sync_scan import SubtitleSyncScanner
                    sub_scanner = SubtitleSyncScanner(self.ffmpeg_bin, self.ffprobe_bin)
                    new_ref_cues = []
                    new_tgt_cues = []
                    if is_ref_sub and self.reference_source_path is not None and self.reference_stream_index is not None:
                        try:
                            new_ref_cues = sub_scanner.extract_cues(self.reference_source_path, self.reference_stream_index)
                        except Exception:
                            new_ref_cues = []
                    if is_tgt_sub:
                        try:
                            new_tgt_cues = sub_scanner.extract_cues(self.target_source_path, self.target_stream_index)
                        except Exception:
                            new_tgt_cues = []
                    self._subtitles_ready.emit(new_ref_cues, new_tgt_cues)

                # Échantillons audio
                scanner = AudioSyncScanner(self.ffmpeg_bin, self.ffprobe_bin)
                ref_samples = np.array([], dtype=np.float32)
                tgt_samples = np.array([], dtype=np.float32)

                # 1. Échantillons de référence
                if self.reference_source_path is not None and self.reference_stream_index is not None and not is_ref_sub:
                    ref_track = AudioSyncTrack(self.reference_source_path, self.reference_stream_index)
                    ref_samples = scanner.samples(ref_track, start_s, 20.0)

                # 2. Échantillons de la cible
                if not is_tgt_sub:
                    tgt_track = AudioSyncTrack(self.target_source_path, self.target_stream_index)
                    tgt_samples = scanner.samples(tgt_track, start_s, 20.0)

                self._segment_audio_ready.emit(idx, ref_samples, tgt_samples, start_s, cut_ms)
            except Exception as exc:
                self._waveform_loading.emit(f"Aperçu non disponible : {exc}")

        threading.Thread(target=_worker, daemon=True).start()

    def _on_subtitles_ready(self, ref_cues, tgt_cues) -> None:
        if getattr(self, "_closing", False):
            return
        if ref_cues:
            self._ref_cues = ref_cues
        if tgt_cues:
            self._tgt_cues = tgt_cues
        self.waveform.set_subtitle_cues(self._ref_cues, self._tgt_cues)

    def _run_auto_subtitle_sync(self) -> None:
        if self._is_sub_sync_running:
            return
        self._is_sub_sync_running = True
        if hasattr(self, "btn_auto_sub_sync"):
            self.btn_auto_sub_sync.setEnabled(False)
        self.waveform.set_loading(translate_text("Synchronisation automatique des sous-titres en cours…"))
        self.listen_status.setText(translate_text("Synchronisation automatique des sous-titres en cours…"))

        def _worker() -> None:
            try:
                from core.workflows.subtitle_sync_scan import SubtitleSyncScanner
                scanner = SubtitleSyncScanner(self.ffmpeg_bin, self.ffprobe_bin)
                is_ref_sub = self.reference_entry is not None and self.reference_entry.is_subtitle
                ref_src = self.reference_source_path or self.target_source_path
                ref_idx = self.reference_stream_index if self.reference_stream_index is not None else 0
                cal = scanner.scan(
                    reference_source=ref_src,
                    reference_stream_index=ref_idx,
                    target_source=self.target_source_path,
                    target_stream_index=self.target_stream_index,
                    is_ref_sub=is_ref_sub,
                    detect_cuts=True,
                )
                self._auto_sub_sync_ready.emit(cal)
            except Exception as exc:
                self._auto_sub_sync_failed.emit(str(exc))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_auto_sub_sync_ready(self, cal: SyncCalibration) -> None:
        if getattr(self, "_closing", False):
            return
        self._is_sub_sync_running = False
        if hasattr(self, "btn_auto_sub_sync"):
            self.btn_auto_sub_sync.setEnabled(True)

        self.current_calibration = cal
        first_shift = cal.segments[0].shift_ms
        self._current_shift_ms = first_shift
        self.spin_shift.blockSignals(True)
        self.spin_shift.setValue(first_shift)
        self.spin_shift.blockSignals(False)
        self.waveform.set_shift(first_shift)

        if cal.cuts_count > 0 and self.cuts_table is not None:
            self.cuts_table.setRowCount(len(cal.segments))
            self._update_cuts_table()

        conf_pct = f"{cal.confidence:.0%}" if cal.confidence is not None else "100%"
        offset_label = f"{first_shift:+.1f}"
        self.listen_status.setText(
            translate_text(
                "Synchronisation automatique terminée ({offset} ms, confiance {conf}).",
                offset=offset_label,
                conf=conf_pct,
            )
        )
        self._select_segment(0)

    def _on_auto_sub_sync_failed(self, err: str) -> None:
        if getattr(self, "_closing", False):
            return
        self._is_sub_sync_running = False
        if hasattr(self, "btn_auto_sub_sync"):
            self.btn_auto_sub_sync.setEnabled(True)
        self.listen_status.setText(f"Échec de l'auto-synchronisation : {err}")
        self.waveform.set_loading("")

    def _on_waveform_loading(self, text: str) -> None:
        if getattr(self, "_closing", False):
            return
        self.waveform.set_loading(text)

    def _on_segment_audio_ready(self, idx: int, ref_samples, tgt_samples, start_s: float, cut_ms: float | None) -> None:
        if getattr(self, "_closing", False):
            return
        self._audio_cache[idx] = (ref_samples, tgt_samples)
        if self._current_segment_index == idx:
            self._apply_audio_to_waveform(idx, ref_samples, tgt_samples, start_s, cut_ms)

    def _apply_audio_to_waveform(self, idx: int, ref_samples, tgt_samples, start_s: float, cut_ms: float | None) -> None:
        if getattr(self, "_closing", False):
            return
        seg = self.current_calibration.segments[idx]
        self.waveform.set_audio_data(
            ref_samples=ref_samples,
            tgt_samples=tgt_samples,
            sample_rate=16000,
            start_time_ms=start_s * 1000.0,
            cut_time_ms=cut_ms,
        )
        self.waveform.set_shift(seg.shift_ms)
        if self._ref_cues or self._tgt_cues:
            self.waveform.set_subtitle_cues(self._ref_cues, self._tgt_cues)

    def _toggle_listen(self) -> None:
        if self._is_playing:
            self._stop_playback()
            return

        self.listen_btn.setEnabled(False)
        self.listen_status.setText(translate_text("Génération de l'extrait audio calé…"))

        is_sub = self.target_entry.is_subtitle

        def _worker() -> None:
            try:
                out_path = Path(self._temp_dir.name) / f"{uuid.uuid4().hex}.wav"
                scanner = AudioSyncScanner(self.ffmpeg_bin, self.ffprobe_bin)

                if is_sub:
                    # Pour les sous-titres, extraire l'audio de référence à écouter
                    audio_source = self.reference_source_path or self.target_source_path
                    audio_stream = self.reference_stream_index if (self.reference_entry and not self.reference_entry.is_subtitle) else 0
                    cmd = [
                        self.ffmpeg_bin,
                        "-nostdin",
                        "-v",
                        "error",
                        "-ss",
                        str(max(0.0, self._current_start_s)),
                        "-i",
                        str(audio_source),
                        "-map",
                        f"0:{audio_stream}?",
                        "-t",
                        "15",
                        "-ac",
                        "2",
                        "-c:a",
                        "pcm_s16le",
                        str(out_path),
                    ]
                else:
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
                        "-ss",
                        str(max(0.0, self._current_start_s)),
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
                self._player.positionChanged.connect(self._on_player_position_changed)

            self._player.setSource(QUrl.fromLocalFile(path))
            self._player.play()
            self._is_playing = True
            self.listen_btn.setText(translate_text("Arrêter"))
            self.listen_btn.setIcon(_stop_icon())
            msg = (
                translate_text("Lecture de la référence (15s)…")
                if self.target_entry.is_subtitle
                else translate_text("Lecture de l'extrait calé (15s)…")
            )
            self.listen_status.setText(msg)
        except Exception:
            try:
                import subprocess
                ffplay = Path(self.ffmpeg_bin).parent / "ffplay"
                if ffplay.exists():
                    # nosemgrep
                    subprocess.Popen([str(ffplay), "-nodisp", "-autoexit", path])  # nosec B603 B607
                    self.listen_status.setText(translate_text("Lecture externe ffplay lancée."))
            except Exception as err:
                self.listen_status.setText(str(err))

    def _on_player_position_changed(self, pos_ms: int) -> None:
        if getattr(self, "_closing", False) or not self._is_playing:
            return
        cur_time_ms = self._current_start_s * 1000.0 + pos_ms
        self.waveform.set_playhead_pos_ms(cur_time_ms)

        if self.target_entry.is_subtitle and self._tgt_cues:
            active = [
                c.text if hasattr(c, "text") else c[2]
                for c in self._tgt_cues
                if (c.start_ms if hasattr(c, "start_ms") else c[0]) + self._current_shift_ms
                <= cur_time_ms
                <= (c.end_ms if hasattr(c, "end_ms") else c[1]) + self._current_shift_ms
            ]
            if active and active[0]:
                self.listen_status.setText(f"💬 {active[0]}")

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
        except Exception:  # nosec B110
            pass

    def _stop_playback(self) -> None:
        if self._player is not None:
            try:
                self._player.stop()
            except Exception:  # nosec B110
                pass
        self._is_playing = False
        self.waveform.set_playhead_pos_ms(None)
        self.listen_btn.setText(translate_text("Pré-écoute calée (15s)"))
        self.listen_btn.setIcon(_play_icon())
        self.listen_status.setText("")

    def _on_cadence_method_changed(self) -> None:
        if hasattr(self, "rb_asetrate") and self.rb_asetrate.isChecked():
            new_method = "asetrate"
        elif hasattr(self, "rb_atempo") and self.rb_atempo.isChecked():
            new_method = "atempo"
        else:
            new_method = "auto"

        self._stop_playback()
        self.current_calibration = replace(
            self.current_calibration,
            cadence_audio_method=new_method,
        )

    def result_calibration(self) -> tuple[SyncCalibration, int]:
        return self.current_calibration, round(self.current_calibration.segments[0].shift_ms)

    def done(self, r: int) -> None:
        self._closing = True
        self._stop_playback()
        super().done(r)

    def closeEvent(self, event) -> None:
        self._closing = True
        self._stop_playback()
        try:
            self._temp_dir.cleanup()
        except Exception:  # nosec B110
            pass
        super().closeEvent(event)
