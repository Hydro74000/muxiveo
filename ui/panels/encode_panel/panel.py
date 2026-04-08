"""
ui/panels/encode_panel/panel.py — Main EncodePanel widget.

Public:
    EncodePanel
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog,
    QFrame, QHBoxLayout, QInputDialog, QLabel,
    QLineEdit, QListWidget, QListWidgetItem,
    QPlainTextEdit, QPushButton,
    QScrollArea, QSlider, QSpinBox, QStackedWidget,
    QVBoxLayout, QWidget,
)

from core.config import AppConfig
from core.inspector import FileInfo, HDRType
from core.i18n import apply_translations, translate_text
from core.lang_tags import Rfc5646LanguageTags
from core.workflows.remux import TrackEntry
from core.runner import TaskSignals
from core.workflows.encode import (
    AUDIO_CODECS, HARDWARE_VIDEO_CODECS, SOFTWARE_VIDEO_CODECS,
    TONEMAP_ALGORITHMS, AudioTrackSettings, EncodeConfig,
    EncodePreset, EncodeWorkflow, HardwareEncoderDetector,
    ProfileManager, QualityMode, TrackMetaEdit, VideoEncodeSettings, presets_for_codec,
)
from ui.panels.encode_panel.theme import (
    _C, _card, _checkbox_style, _combo_style,
    _input_style, _primary_button, _secondary_button,
    _section_label, _separator,
)
from ui.panels.encode_panel.widgets import _AudioSourceDialog, _AudioTable


class EncodePanel(QWidget):
    """
    Panneau d'encodage vidéo/audio.

    Signaux :
        log_message(level: str, message: str)
        ready_changed(bool) — True quand une source vidéo est sélectionnée
    """

    log_message              = Signal(str, str)
    ready_changed            = Signal(bool)     # émis quand la source vidéo change
    audio_track_meta_changed = Signal(int, object, str, str)  # (stream_index, source_path, lang, title)
    _hw_detected             = Signal(object, object, object)   # (hw: set[str], sw: set[str], hw_ffmpeg: str)

    def __init__(
        self,
        config: AppConfig,
        parent: QWidget | None = None,
        *,
        writing_application: str = "",
    ) -> None:
        super().__init__(parent)
        self._config    = config
        self._workflow  = EncodeWorkflow(
            ffmpeg_bin=config.tool_ffmpeg,
            dovi_tool_bin=config.tool_dovi_tool,
            hdr10plus_bin=config.tool_hdr10plus,
            mkvmerge_bin=config.tool_mkvmerge,
            mkvextract_bin=config.tool_mkvextract,
            mkvpropedit_bin=config.tool_mkvpropedit,
            ram_buffer_enabled=config.ram_buffer_enabled,
            ram_buffer_threshold_pct=config.ram_buffer_threshold_pct,
            parent=self,
            writing_application=writing_application,
        )
        self._profiles  = ProfileManager(config.app_data_dir / "encode_profiles")
        self._executor  = ThreadPoolExecutor(max_workers=1)
        self._file_info: FileInfo | None = None
        self._video_tracks: list[tuple[FileInfo, TrackEntry, str]] = []
        self._audio_tracks_data: list[tuple] = []   # list[tuple[AudioTrack, str, Path]] pour le popup
        self._duration_s: float | None = None
        self._hw_encoders: set[str] = set()
        # Callable fourni par MainWindow pour récupérer le chemin de sortie depuis RemuxPanel.
        self._output_provider: Callable[[], Path | None] = lambda: None
        # Callable fourni par MainWindow pour récupérer le titre de fichier depuis RemuxPanel.
        self._file_title_provider: Callable[[], str] = lambda: ""
        # Callable fourni par MainWindow pour récupérer les pièces jointes manuelles depuis RemuxPanel.
        self._extra_attachments_provider: Callable[[], list] = lambda: []
        # Callable fourni par MainWindow pour récupérer les tag_overrides depuis RemuxPanel.
        self._tag_overrides_provider: Callable[[], "dict | None"] = lambda: None
        # Callable fourni par MainWindow pour récupérer les chapter_overrides depuis RemuxPanel.
        self._chapters_provider: Callable[[], "list | None"] = lambda: None

        self._sw_encoders: set[str] = {codec_id for codec_id, _ in SOFTWARE_VIDEO_CODECS}
        self._workflow.log_message.connect(self.log_message, Qt.ConnectionType.QueuedConnection)
        self._hw_detected.connect(self._on_hw_detected, Qt.ConnectionType.QueuedConnection)

        self._build_ui()
        apply_translations(self)
        self._executor.submit(self._detect_hw_encoders)

    # ------------------------------------------------------------------
    # Construction de l'interface
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self.setStyleSheet(f"background:{_C.BG_DEEP};")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet(f"QScrollArea{{background:{_C.BG_DEEP};border:none;}}"
                             f"QScrollBar:vertical{{background:{_C.BG_DEEP};width:6px;border:none;}}"
                             f"QScrollBar::handle:vertical{{background:{_C.BORDER_LT};"
                             f"border-radius:3px;min-height:24px;}}"
                             f"QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{{height:0;}}")

        content = QWidget()
        content.setStyleSheet(f"background:{_C.BG_DEEP};")
        cl = QVBoxLayout(content)
        cl.setContentsMargins(28, 24, 28, 24)
        cl.setSpacing(20)

        # --- Titre ---
        title = QLabel("Encodage Vidéo / Audio")
        title.setStyleSheet(f"font-size:20px;font-weight:800;color:{_C.TEXT_PRI};"
                            f"background:transparent;letter-spacing:-0.3px;")
        subtitle = QLabel("x265 · x264 · SVT-AV1 · NVENC/AMF/QSV — HDR10 · Tone mapping · Audio multicanal")
        subtitle.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:12px;background:transparent;")
        cl.addWidget(title)
        cl.addWidget(subtitle)
        cl.addWidget(_separator())

        # --- Piste vidéo source ---
        cl.addWidget(_section_label("PISTE VIDÉO SOURCE"))
        cl.addWidget(self._build_video_source_card())
        cl.addWidget(_separator())

        # --- Encodage vidéo ---
        cl.addWidget(_section_label("ENCODAGE VIDÉO"))
        cl.addWidget(self._build_video_card())
        cl.addWidget(_separator())

        # --- HDR ---
        cl.addWidget(_section_label("HDR"))
        cl.addWidget(self._build_hdr_card())
        cl.addWidget(_separator())

        # --- Pistes audio ---
        cl.addWidget(_section_label("PISTES AUDIO"))
        self._audio_table = _AudioTable(self._config)
        self._audio_table.set_changed_callback(self._rebuild_preview)
        self._audio_table.track_meta_changed.connect(self.audio_track_meta_changed)
        cl.addWidget(self._audio_table)

        add_track_row = QHBoxLayout()
        add_track_row.setSpacing(0)
        self._add_audio_btn = _secondary_button("＋  Ajouter une piste…")
        self._add_audio_btn.setEnabled(False)
        self._add_audio_btn.clicked.connect(self._on_add_audio_track)
        add_track_row.addWidget(self._add_audio_btn)
        add_track_row.addStretch()
        cl.addLayout(add_track_row)

        cl.addWidget(_separator())

        # --- Profils ---
        cl.addWidget(_section_label("PROFILS"))
        cl.addWidget(self._build_profiles_card())
        cl.addWidget(_separator())

        # --- Aperçu commande ---
        cmd_row = QHBoxLayout()
        cmd_row.addWidget(_section_label("APERÇU COMMANDE"))
        cmd_row.addStretch()
        copy_btn = _secondary_button("Copier")
        copy_btn.clicked.connect(self._copy_command)
        cmd_row.addWidget(copy_btn)
        cl.addLayout(cmd_row)

        self._cmd_preview = QPlainTextEdit()
        self._cmd_preview.setReadOnly(True)
        self._cmd_preview.setFixedHeight(140)
        mono = QFont("JetBrains Mono", 9)
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self._cmd_preview.setFont(mono)
        self._cmd_preview.setStyleSheet(
            f"QPlainTextEdit{{background:{_C.BG_DEEP};color:{_C.TEXT_SEC};"
            f"border:1px solid {_C.BORDER};border-radius:6px;padding:8px 12px;}}"
        )
        self._cmd_preview.setPlaceholderText(
            "Sélectionnez un fichier source et configurez l'encodage…"
        )
        cl.addWidget(self._cmd_preview)
        cl.addStretch()

        scroll.setWidget(content)
        root.addWidget(scroll, stretch=1)

    def _build_video_source_card(self) -> QWidget:
        """Sélecteur de piste vidéo alimenté par l'onglet Conteneur."""
        card = _card()
        cl = QVBoxLayout(card)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)

        self._video_list = QListWidget()
        self._video_list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self._video_list.setStyleSheet(
            f"QListWidget{{background:{_C.BG_CARD};border:none;border-radius:6px;"
            f"color:{_C.TEXT_PRI};font-size:11px;font-family:'JetBrains Mono',monospace;}}"
            f"QListWidget::item{{padding:8px 12px;border-bottom:1px solid {_C.BORDER};}}"
            f"QListWidget::item:selected{{background:{_C.ACCENT_DIM};}}"
            f"QListWidget::item:hover{{background:{_C.BG_HOVER};}}"
        )
        self._video_list.currentRowChanged.connect(self._on_video_row_changed)
        cl.addWidget(self._video_list)

        self._video_placeholder = QLabel(
            "Aucune piste vidéo — sélectionnez des fichiers dans l'onglet Conteneur"
        )
        self._video_placeholder.setStyleSheet(
            f"color:{_C.TEXT_DIM};font-size:11px;padding:14px;"
            f"background:transparent;"
        )
        self._video_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cl.addWidget(self._video_placeholder)

        self._video_list.setVisible(False)
        self._video_placeholder.setVisible(True)

        # --- Langue de la piste vidéo de sortie ---
        lang_row = QHBoxLayout()
        lang_row.setContentsMargins(12, 8, 12, 8)
        lang_row.setSpacing(8)
        lang_lbl = QLabel("Langue")
        lang_lbl.setStyleSheet(
            f"color:{_C.TEXT_SEC};font-size:11px;background:transparent;min-width:52px;"
        )
        self._video_lang_edit = QLineEdit()
        self._video_lang_edit.setPlaceholderText("ex : fr, en, und…")
        self._video_lang_edit.setFixedWidth(110)
        self._video_lang_edit.setStyleSheet(_input_style())
        self._video_lang_edit.setToolTip(
            "Balise langue RFC 5646 appliquée à la piste vidéo du fichier encodé"
        )
        self._video_lang_edit.textChanged.connect(self._on_video_lang_changed)
        lang_row.addWidget(lang_lbl)
        lang_row.addWidget(self._video_lang_edit)
        lang_row.addStretch()
        cl.addLayout(lang_row)

        return card

    # ------------------------------------------------------------------
    # API publique — appelée par MainWindow depuis RemuxPanel
    # ------------------------------------------------------------------

    def set_video_tracks(self, tracks: list[tuple]) -> None:
        """Met à jour la liste des pistes vidéo depuis l'onglet Conteneur."""
        self._video_tracks = tracks
        self._video_list.blockSignals(True)
        self._video_list.clear()

        if not tracks:
            self._video_list.setVisible(False)
            self._video_placeholder.setVisible(True)
            self._file_info = None
            self.ready_changed.emit(False)
            self._video_list.blockSignals(False)
            self._video_lang_edit.blockSignals(True)
            self._video_lang_edit.clear()
            self._video_lang_edit.blockSignals(False)
            self._rebuild_preview()
            return

        self._video_placeholder.setVisible(False)
        self._video_list.setVisible(True)

        for file_info, track, color in tracks:
            hdr = file_info.hdr_type.label()
            hdr_part = f"  {hdr}" if hdr not in ("SDR", "?") else ""
            text = f"█  {file_info.path.name}    {track.codec.upper()}  {track.display_info}{hdr_part}"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, (file_info, track))
            item.setForeground(QBrush(QColor(color)))
            self._video_list.addItem(item)

        self._video_list.blockSignals(False)
        self._adjust_video_list_height()
        self._video_list.setCurrentRow(0)   # triggers _on_video_row_changed

    def _adjust_video_list_height(self) -> None:
        """Ajuste la hauteur de la liste vidéo pour afficher exactement n lignes."""
        n = self._video_list.count()
        if n == 0:
            return
        row_h = self._video_list.sizeHintForRow(0)
        self._video_list.setFixedHeight(n * row_h + 2)

    def _on_video_row_changed(self, row: int) -> None:
        if row < 0 or row >= len(self._video_tracks):
            return
        file_info, track, _color = self._video_tracks[row]
        self._video_lang_edit.blockSignals(True)
        self._video_lang_edit.setText(track.language or "")
        self._video_lang_edit.blockSignals(False)
        self._apply_file_info(file_info)

    def _apply_file_info(self, info: FileInfo) -> None:
        """Applique les infos d'un FileInfo sélectionné comme source d'encodage."""
        self._file_info  = info
        self._duration_s = info.duration_s

        if info.primary_video:
            self._prefill_hdr_meta(info.primary_video.raw)

        pass  # Fichier de sortie géré par RemuxPanel

        self.ready_changed.emit(True)
        self._update_passthrough_controls(auto_check=True)
        self.log_message.emit(
            "OK",
            f"{info.path.name} — "
            f"{len(info.video_tracks)}V  {len(info.audio_tracks)}A  "
            f"{len(info.subtitle_tracks)}S  {info.hdr_type.label()}",
        )
        self._rebuild_preview()

    def set_audio_tracks(self, tracks: list[tuple]) -> None:
        """Met à jour les pistes audio depuis les pistes activées dans l'onglet Conteneur.
        tracks : list[tuple[AudioTrack, str, Path]] — (piste, couleur, chemin_source)
        """
        self._audio_tracks_data = tracks
        self._add_audio_btn.setEnabled(bool(tracks))

        default_codec   = "copy"
        default_bitrate = None
        profile_name = self._profile_combo.currentText()
        if profile_name:
            for p in self._profiles.load_all():
                if p.name == profile_name:
                    default_codec   = p.default_audio_codec
                    default_bitrate = p.default_audio_bitrate_kbps
                    break

        self._audio_table.load_tracks(tracks, default_codec, default_bitrate)
        self._rebuild_preview()

    # ------------------------------------------------------------------
    # Carte encodage vidéo
    # ------------------------------------------------------------------

    def _build_video_card(self) -> QWidget:
        card = _card()
        cl = QVBoxLayout(card)
        cl.setContentsMargins(16, 14, 16, 14)
        cl.setSpacing(12)

        # Ligne codec (toujours visible)
        r1 = QHBoxLayout()
        r1.setSpacing(12)
        codec_lbl = QLabel("Codec")
        codec_lbl.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:11px;background:transparent;")
        r1.addWidget(codec_lbl)
        self._codec_combo = QComboBox()
        self._codec_combo.setStyleSheet(_combo_style())
        self._codec_combo.setMinimumWidth(220)
        self._populate_codec_combo()
        self._codec_combo.currentIndexChanged.connect(self._on_codec_changed)
        r1.addWidget(self._codec_combo)
        r1.addStretch()
        cl.addLayout(r1)

        # Contrôles d'encodage (masqués quand codec = copy)
        self._video_encode_controls = QWidget()
        self._video_encode_controls.setStyleSheet("background:transparent;")
        enc_cl = QVBoxLayout(self._video_encode_controls)
        enc_cl.setContentsMargins(0, 0, 0, 0)
        enc_cl.setSpacing(12)

        rp = QHBoxLayout()
        rp.setSpacing(12)
        preset_lbl = QLabel("Preset")
        preset_lbl.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:11px;background:transparent;")
        rp.addWidget(preset_lbl)
        self._preset_combo = QComboBox()
        self._preset_combo.setStyleSheet(_combo_style())
        self._preset_combo.setMinimumWidth(120)
        rp.addWidget(self._preset_combo)
        rp.addStretch()
        enc_cl.addLayout(rp)

        # Ligne mode qualité
        r2 = QHBoxLayout()
        r2.setSpacing(12)
        mode_lbl = QLabel("Mode")
        mode_lbl.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:11px;background:transparent;")
        self._mode_combo = QComboBox()
        self._mode_combo.setStyleSheet(_combo_style())
        for mode in QualityMode:
            self._mode_combo.addItem(mode.label(), mode)
        self._mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        r2.addWidget(mode_lbl)
        r2.addWidget(self._mode_combo)
        r2.addSpacing(16)

        # Valeur qualité (stack : CRF slider+spin / bitrate edit / size edit)
        self._quality_stack = QStackedWidget()

        # Page 0 : CRF
        crf_w = QWidget()
        crf_w.setStyleSheet("background:transparent;")
        crf_l = QHBoxLayout(crf_w)
        crf_l.setContentsMargins(0, 0, 0, 0)
        crf_l.setSpacing(8)
        self._crf_slider = QSlider(Qt.Orientation.Horizontal)
        self._crf_slider.setRange(0, 51)
        self._crf_slider.setValue(18)
        self._crf_slider.setFixedWidth(160)
        self._crf_slider.setStyleSheet(
            f"QSlider::groove:horizontal{{height:4px;background:{_C.BG_ACTIVE};"
            f"border-radius:2px;}}"
            f"QSlider::handle:horizontal{{width:14px;height:14px;margin:-5px 0;"
            f"background:{_C.ACCENT};border-radius:7px;}}"
            f"QSlider::sub-page:horizontal{{background:{_C.ACCENT};border-radius:2px;}}"
        )
        self._crf_spin = QSpinBox()
        self._crf_spin.setRange(0, 51)
        self._crf_spin.setValue(18)
        self._crf_spin.setFixedWidth(52)
        self._crf_spin.setStyleSheet(
            f"QSpinBox{{background:{_C.BG_CARD};color:{_C.TEXT_PRI};"
            f"border:1px solid {_C.BORDER};border-radius:4px;padding:2px 4px;}}"
        )
        self._crf_slider.valueChanged.connect(self._crf_spin.setValue)
        self._crf_spin.valueChanged.connect(self._crf_slider.setValue)
        self._crf_slider.valueChanged.connect(lambda _: self._rebuild_preview())
        crf_l.addWidget(self._crf_slider)
        crf_l.addWidget(self._crf_spin)
        self._quality_stack.addWidget(crf_w)

        # Page 1 : Bitrate
        br_w = QWidget()
        br_w.setStyleSheet("background:transparent;")
        br_l = QHBoxLayout(br_w)
        br_l.setContentsMargins(0, 0, 0, 0)
        br_l.setSpacing(6)
        self._bitrate_edit = QLineEdit("5000")
        self._bitrate_edit.setStyleSheet(_input_style())
        self._bitrate_edit.setFixedWidth(100)
        self._bitrate_edit.textChanged.connect(lambda _: self._rebuild_preview())
        br_lbl = QLabel("kbps")
        br_lbl.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:11px;background:transparent;")
        br_l.addWidget(self._bitrate_edit)
        br_l.addWidget(br_lbl)
        self._quality_stack.addWidget(br_w)

        # Page 2 : Taille cible
        sz_w = QWidget()
        sz_w.setStyleSheet("background:transparent;")
        sz_l = QHBoxLayout(sz_w)
        sz_l.setContentsMargins(0, 0, 0, 0)
        sz_l.setSpacing(6)
        self._size_edit = QLineEdit("4000")
        self._size_edit.setStyleSheet(_input_style())
        self._size_edit.setFixedWidth(100)
        self._size_edit.textChanged.connect(lambda _: self._rebuild_preview())
        sz_lbl = QLabel("Mo")
        sz_lbl.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:11px;background:transparent;")
        sz_l.addWidget(self._size_edit)
        sz_l.addWidget(sz_lbl)
        self._quality_stack.addWidget(sz_w)

        r2.addWidget(self._quality_stack)
        r2.addStretch()
        enc_cl.addLayout(r2)

        # Params avancés
        adv_lbl = QLabel("Params avancés  (x265-params / svtav1-params)")
        adv_lbl.setStyleSheet(f"color:{_C.TEXT_DIM};font-size:10px;background:transparent;")
        enc_cl.addWidget(adv_lbl)
        self._extra_params = QLineEdit()
        self._extra_params.setPlaceholderText("ex. no-open-gop=1:hdr10=1:hdr10-opt=1")
        self._extra_params.setStyleSheet(_input_style())
        self._extra_params.textChanged.connect(lambda _: self._rebuild_preview())
        enc_cl.addWidget(self._extra_params)

        cl.addWidget(self._video_encode_controls)

        self._on_codec_changed()   # initialise preset combo + visibility
        return card

    def _build_hdr_card(self) -> QWidget:
        card = _card()
        cl = QVBoxLayout(card)
        cl.setContentsMargins(16, 14, 16, 14)
        cl.setSpacing(10)

        # 1. Injection métadonnées HDR10 statiques
        self._inject_hdr_cb = QCheckBox("Injecter les métadonnées HDR10 statiques (ST 2086 / MaxCLL)")
        self._inject_hdr_cb.setStyleSheet(_checkbox_style())
        self._inject_hdr_cb.stateChanged.connect(self._on_hdr_toggle)
        cl.addWidget(self._inject_hdr_cb)

        self._hdr_meta_widget = QWidget()
        self._hdr_meta_widget.setStyleSheet("background:transparent;")
        hm_l = QVBoxLayout(self._hdr_meta_widget)
        hm_l.setContentsMargins(20, 4, 0, 4)
        hm_l.setSpacing(6)

        r_md = QHBoxLayout()
        r_md.setSpacing(8)
        md_lbl = QLabel("Master Display")
        md_lbl.setFixedWidth(110)
        md_lbl.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:11px;background:transparent;")
        self._master_display = QLineEdit()
        self._master_display.setPlaceholderText(
            "G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(40000000,50)"
        )
        self._master_display.setStyleSheet(_input_style())
        self._master_display.textChanged.connect(lambda _: self._rebuild_preview())
        r_md.addWidget(md_lbl)
        r_md.addWidget(self._master_display)
        hm_l.addLayout(r_md)

        r_cll = QHBoxLayout()
        r_cll.setSpacing(8)
        cll_lbl = QLabel("MaxCLL / MaxFALL")
        cll_lbl.setFixedWidth(110)
        cll_lbl.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:11px;background:transparent;")
        self._max_cll = QLineEdit()
        self._max_cll.setPlaceholderText("1000,400")
        self._max_cll.setFixedWidth(160)
        self._max_cll.setStyleSheet(_input_style())
        self._max_cll.textChanged.connect(lambda _: self._rebuild_preview())
        r_cll.addWidget(cll_lbl)
        r_cll.addWidget(self._max_cll)
        r_cll.addStretch()
        hm_l.addLayout(r_cll)

        self._hdr_meta_widget.setVisible(False)
        cl.addWidget(self._hdr_meta_widget)

        cl.addWidget(_separator())

        # 2. Passthrough Dolby Vision RPU
        self._copy_dv_cb = QCheckBox("Copier le RPU Dolby Vision depuis la source")
        self._copy_dv_cb.setStyleSheet(_checkbox_style())
        self._copy_dv_cb.setEnabled(False)
        self._copy_dv_cb.stateChanged.connect(self._on_dv_toggle)
        cl.addWidget(self._copy_dv_cb)

        self._dovi_profile_widget = QWidget()
        self._dovi_profile_widget.setStyleSheet("background:transparent;")
        dp_l = QHBoxLayout(self._dovi_profile_widget)
        dp_l.setContentsMargins(20, 0, 0, 0)
        dp_l.setSpacing(8)
        dp_lbl = QLabel("Profil dovi_tool")
        dp_lbl.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:11px;background:transparent;")
        self._dovi_profile_combo = QComboBox()
        self._dovi_profile_combo.setStyleSheet(_combo_style())
        self._dovi_profile_combo.addItem("P8.1 — conserver (par défaut)", "0")
        self._dovi_profile_combo.addItem("P8.1 — normaliser / supprimer FEL·MEL", "2")
        self._dovi_profile_combo.currentIndexChanged.connect(lambda _: self._rebuild_preview())
        dp_l.addWidget(dp_lbl)
        dp_l.addWidget(self._dovi_profile_combo)
        dp_l.addStretch()
        self._dovi_profile_widget.setVisible(False)
        cl.addWidget(self._dovi_profile_widget)

        # 3. Passthrough HDR10+ SEI
        self._copy_hdr10plus_cb = QCheckBox("Copier les métadonnées HDR10+ depuis la source")
        self._copy_hdr10plus_cb.setStyleSheet(_checkbox_style())
        self._copy_hdr10plus_cb.setEnabled(False)
        self._copy_hdr10plus_cb.stateChanged.connect(lambda _: self._rebuild_preview())
        cl.addWidget(self._copy_hdr10plus_cb)

        cl.addWidget(_separator())

        # 4. Tone mapping HDR→SDR
        self._tonemap_cb = QCheckBox("Tone-mapping HDR → SDR  (zscale + tonemap)")
        self._tonemap_cb.setStyleSheet(_checkbox_style())
        self._tonemap_cb.stateChanged.connect(self._on_tonemap_toggle)
        cl.addWidget(self._tonemap_cb)

        self._tonemap_algo_widget = QWidget()
        self._tonemap_algo_widget.setStyleSheet("background:transparent;")
        ta_l = QHBoxLayout(self._tonemap_algo_widget)
        ta_l.setContentsMargins(20, 0, 0, 0)
        ta_l.setSpacing(8)
        algo_lbl = QLabel("Algorithme")
        algo_lbl.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:11px;background:transparent;")
        self._tonemap_algo = QComboBox()
        self._tonemap_algo.setStyleSheet(_combo_style())
        for algo in TONEMAP_ALGORITHMS:
            self._tonemap_algo.addItem(algo, algo)
        self._tonemap_algo.currentIndexChanged.connect(lambda _: self._rebuild_preview())
        ta_l.addWidget(algo_lbl)
        ta_l.addWidget(self._tonemap_algo)
        ta_l.addStretch()
        self._tonemap_algo_widget.setVisible(False)
        cl.addWidget(self._tonemap_algo_widget)

        return card

    def _build_profiles_card(self) -> QWidget:
        card = _card()
        cl = QHBoxLayout(card)
        cl.setContentsMargins(16, 12, 16, 12)
        cl.setSpacing(10)

        self._profile_combo = QComboBox()
        self._profile_combo.setStyleSheet(_combo_style())
        self._profile_combo.setMinimumWidth(180)
        self._refresh_profiles()
        cl.addWidget(self._profile_combo)

        load_btn = _secondary_button("Charger")
        load_btn.clicked.connect(self._load_profile)
        del_btn = _secondary_button("Supprimer")
        del_btn.clicked.connect(self._delete_profile)
        cl.addWidget(load_btn)
        cl.addWidget(del_btn)
        cl.addStretch()

        self._profile_name = QLineEdit()
        self._profile_name.setPlaceholderText("Nom du profil…")
        self._profile_name.setStyleSheet(_input_style())
        self._profile_name.setFixedWidth(160)
        save_btn = _secondary_button("Enregistrer")
        save_btn.clicked.connect(self._save_profile)
        cl.addWidget(self._profile_name)
        cl.addWidget(save_btn)

        return card

    def _prefill_hdr_meta(self, raw: dict) -> None:
        """Extrait master_display et max_cll depuis le side_data_list ffprobe."""
        def _rat(v) -> float:
            """Parse un rationnel ffprobe '35400/50000' ou un float direct."""
            s = str(v)
            if "/" in s:
                a, b = s.split("/", 1)
                return float(a) / float(b)
            return float(s)

        for sd in raw.get("side_data_list", []):
            if sd.get("side_data_type") == "Mastering display metadata":
                try:
                    rx = _rat(sd.get("red_x", 0));         ry = _rat(sd.get("red_y", 0))
                    gx = _rat(sd.get("green_x", 0));        gy = _rat(sd.get("green_y", 0))
                    bx = _rat(sd.get("blue_x", 0));         by = _rat(sd.get("blue_y", 0))
                    wx = _rat(sd.get("white_point_x", 0));  wy = _rat(sd.get("white_point_y", 0))
                    lmax = _rat(sd.get("max_luminance", 0))
                    lmin = _rat(sd.get("min_luminance", 0))
                    # Chromaticité : unités x265 (×50000) ; luminance : 0,0001 cd/m² (×10000)
                    c = lambda f: int(round(f * 50000))
                    l = lambda f: int(round(f * 10000))
                    md = (f"G({c(gx)},{c(gy)})"
                          f"B({c(bx)},{c(by)})"
                          f"R({c(rx)},{c(ry)})"
                          f"WP({c(wx)},{c(wy)})"
                          f"L({l(lmax)},{l(lmin)})")
                    self._master_display.setText(md)
                except Exception:
                    pass
            elif sd.get("side_data_type") == "Content light level metadata":
                try:
                    maxcll  = int(sd.get("max_content", 0))
                    maxfall = int(sd.get("max_average", 0))
                    self._max_cll.setText(f"{maxcll},{maxfall}")
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Détection encodeurs matériels
    # ------------------------------------------------------------------

    def _detect_hw_encoders(self) -> None:
        detector = HardwareEncoderDetector()
        ffmpeg = self._config.tool_ffmpeg
        hw, hw_ffmpeg = detector.detect(ffmpeg)
        sw = detector.detect_software(ffmpeg)
        # hw_ffmpeg peut être le ffmpeg système si le ffmpeg embarqué manque de HW codecs.
        # On l'envoie avec le signal pour que le workflow l'utilise lors de l'encodage HW.
        self._hw_detected.emit(hw, sw, hw_ffmpeg)

    def _on_hw_detected(self, hw: set[str], sw: set[str], hw_ffmpeg: str) -> None:
        self._hw_encoders = hw
        # Ne met à jour les codecs SW que si la détection a retourné au moins un résultat.
        # Un set vide signifie une erreur de détection (ffmpeg absent), pas "aucun codec".
        if sw:
            self._sw_encoders = sw
        # Si le ffmpeg HW est différent du ffmpeg embarqué (AppImage : système vs bundled),
        # mettre à jour le workflow pour que l'encodage HW utilise le bon binaire.
        if hw and hw_ffmpeg != self._config.tool_ffmpeg:
            self._workflow.set_ffmpeg(hw_ffmpeg)
        current = self._codec_combo.currentData()
        self._populate_codec_combo()
        # Restaure la sélection précédente si toujours disponible
        for i in range(self._codec_combo.count()):
            if self._codec_combo.itemData(i) == current:
                self._codec_combo.setCurrentIndex(i)
                break
        all_detected = hw | sw
        if all_detected:
            self.log_message.emit(
                "OK",
                translate_text(
                    "Encodeurs détectés : {items}",
                    items=", ".join(sorted(all_detected)),
                ),
            )

    def _populate_codec_combo(self) -> None:
        self._codec_combo.blockSignals(True)
        self._codec_combo.clear()
        self._codec_combo.addItem("Copy — remux (sans conversion)", "copy")
        for codec_id, label in SOFTWARE_VIDEO_CODECS:
            if codec_id in self._sw_encoders:
                self._codec_combo.addItem(label, codec_id)
        for codec_id, label in HARDWARE_VIDEO_CODECS:
            if codec_id in self._hw_encoders:
                self._codec_combo.addItem(f"⚡ {label}", codec_id)
        self._codec_combo.blockSignals(False)

    # ------------------------------------------------------------------
    # Changements UI → rebuild preview
    # ------------------------------------------------------------------

    def _on_codec_changed(self, _idx: int = 0) -> None:
        codec = self._codec_combo.currentData() or "libx265"
        if hasattr(self, "_video_encode_controls"):
            self._video_encode_controls.setVisible(codec != "copy")
        presets = presets_for_codec(codec)
        self._preset_combo.blockSignals(True)
        self._preset_combo.clear()
        for p in presets:
            self._preset_combo.addItem(p, p)
        # Sélectionne "slow" par défaut pour x265/x264, "6" pour SVT-AV1
        default = "slow" if codec not in ("libsvtav1",) else "6"
        idx = next((i for i in range(self._preset_combo.count())
                    if self._preset_combo.itemData(i) == default), 0)
        self._preset_combo.setCurrentIndex(idx)
        self._preset_combo.setEnabled(bool(presets))
        self._preset_combo.blockSignals(False)
        self._update_passthrough_controls()
        self._rebuild_preview()

    def _on_dv_toggle(self, _state: int) -> None:
        self._dovi_profile_widget.setVisible(self._copy_dv_cb.isChecked())
        self._rebuild_preview()

    def _update_passthrough_controls(self, *, auto_check: bool = False) -> None:
        """Active/désactive les contrôles DV/HDR10+ selon la source et le codec."""
        if not hasattr(self, "_copy_dv_cb"):
            return   # appelé pendant l'init avant que le card HDR soit construit
        if self._file_info is None:
            self._copy_dv_cb.setEnabled(False)
            self._copy_hdr10plus_cb.setEnabled(False)
            return

        codec = self._codec_combo.currentData() or "libx265"
        is_hevc = codec in ("libx265", "hevc_nvenc", "hevc_amf", "hevc_qsv", "copy")
        hdr = self._file_info.hdr_type

        has_dv       = hdr in (HDRType.DOLBY_VISION, HDRType.DOLBY_VISION_HDR10PLUS)
        has_hdr10plus = hdr in (HDRType.HDR10PLUS, HDRType.DOLBY_VISION_HDR10PLUS)

        dv_ok       = has_dv and is_hevc
        hdr10plus_ok = has_hdr10plus and is_hevc

        self._copy_dv_cb.setEnabled(dv_ok)
        self._copy_hdr10plus_cb.setEnabled(hdr10plus_ok)

        if auto_check:
            self._copy_dv_cb.setChecked(dv_ok)
            self._copy_hdr10plus_cb.setChecked(hdr10plus_ok)
            has_static_hdr = bool(self._master_display.text().strip())
            self._inject_hdr_cb.setChecked(has_static_hdr)

        if not dv_ok:
            self._copy_dv_cb.setChecked(False)
        if not hdr10plus_ok:
            self._copy_hdr10plus_cb.setChecked(False)

    def _on_mode_changed(self, _idx: int = 0) -> None:
        mode = self._mode_combo.currentData()
        page = {QualityMode.CRF: 0, QualityMode.BITRATE: 1, QualityMode.SIZE: 2}.get(mode, 0)
        self._quality_stack.setCurrentIndex(page)
        self._rebuild_preview()

    def _on_hdr_toggle(self, _state: int) -> None:
        visible = self._inject_hdr_cb.isChecked()
        self._hdr_meta_widget.setVisible(visible)
        if visible:
            self._tonemap_cb.setChecked(False)
        self._rebuild_preview()

    def _on_tonemap_toggle(self, _state: int) -> None:
        visible = self._tonemap_cb.isChecked()
        self._tonemap_algo_widget.setVisible(visible)
        if visible:
            self._inject_hdr_cb.setChecked(False)
        self._rebuild_preview()

    def _on_video_lang_changed(self, text: str) -> None:
        """Corrige silencieusement la casse du code langue vidéo, puis rafraîchit l'aperçu."""
        canonical = Rfc5646LanguageTags.normalize(text.strip())
        if canonical is not None and canonical != text.strip():
            self._video_lang_edit.blockSignals(True)
            self._video_lang_edit.setText(canonical)
            self._video_lang_edit.blockSignals(False)
        self._rebuild_preview()

    # ------------------------------------------------------------------
    # Aperçu commande
    # ------------------------------------------------------------------

    def _rebuild_preview(self) -> None:
        if not hasattr(self, "_cmd_preview"):
            return   # appelé pendant l'init avant que le widget existe
        config = self._current_config()
        if config is None:
            self._cmd_preview.setPlainText("")
            return
        try:
            text = self._workflow.preview_command(config)
            self._cmd_preview.setPlainText(text)
        except Exception:
            self._cmd_preview.setPlainText(translate_text("(erreur de construction de la commande)"))

    # ------------------------------------------------------------------
    # Profils
    # ------------------------------------------------------------------

    def _refresh_profiles(self) -> None:
        self._profile_combo.clear()
        for name in self._profiles.names():
            self._profile_combo.addItem(name, name)

    def _load_profile(self) -> None:
        name = self._profile_combo.currentText()
        if not name:
            return
        presets = {p.name: p for p in self._profiles.load_all()}
        if name not in presets:
            return
        preset = presets[name]
        vs = preset.to_video_settings()
        # Codec
        for i in range(self._codec_combo.count()):
            if self._codec_combo.itemData(i) == vs.codec:
                self._codec_combo.setCurrentIndex(i)
                break
        # Mode qualité
        for i in range(self._mode_combo.count()):
            if self._mode_combo.itemData(i) == QualityMode(preset.quality_mode):
                self._mode_combo.setCurrentIndex(i)
                break
        self._crf_slider.setValue(vs.crf)
        self._bitrate_edit.setText(str(vs.bitrate_kbps))
        self._size_edit.setText(str(vs.target_size_mb))
        self._extra_params.setText(vs.extra_params)
        self._inject_hdr_cb.setChecked(vs.inject_hdr_meta)
        self._master_display.setText(vs.master_display)
        self._max_cll.setText(vs.max_cll)
        self._tonemap_cb.setChecked(vs.tonemap_to_sdr)
        idx_algo = next((i for i in range(self._tonemap_algo.count())
                         if self._tonemap_algo.itemData(i) == vs.tonemap_algorithm), 0)
        self._tonemap_algo.setCurrentIndex(idx_algo)
        self._rebuild_preview()
        self.log_message.emit("OK", translate_text("Profil chargé : {name}", name=name))

    def _save_profile(self) -> None:
        name = self._profile_name.text().strip()
        if not name:
            name, ok = QInputDialog.getText(
                self,
                translate_text("Enregistrer le profil"),
                translate_text("Nom du profil :"),
            )
            if not ok or not name.strip():
                return
            name = name.strip()
        vs = self._current_video_settings()
        preset = EncodePreset(
            name=name,
            codec=vs.codec,
            quality_mode=vs.quality_mode.value,
            crf=vs.crf,
            bitrate_kbps=vs.bitrate_kbps,
            target_size_mb=vs.target_size_mb,
            preset=vs.preset,
            extra_params=vs.extra_params,
            inject_hdr_meta=vs.inject_hdr_meta,
            master_display=vs.master_display,
            max_cll=vs.max_cll,
            tonemap_to_sdr=vs.tonemap_to_sdr,
            tonemap_algorithm=vs.tonemap_algorithm,
        )
        self._profiles.save(preset)
        self._refresh_profiles()
        self._profile_name.clear()
        self.log_message.emit("OK", translate_text("Profil enregistré : {name}", name=name))

    def _delete_profile(self) -> None:
        name = self._profile_combo.currentText()
        if name:
            self._profiles.delete(name)
            self._refresh_profiles()
            self.log_message.emit("INFO", translate_text("Profil supprimé : {name}", name=name))

    # ------------------------------------------------------------------
    # API publique — exécution (déléguée à MainWindow)
    # ------------------------------------------------------------------

    def collect_config(self) -> "EncodeConfig | None":
        """Retourne la configuration d'encodage courante, ou None si incomplète."""
        return self._current_config()

    def get_duration_s(self) -> "float | None":
        """Durée de la source sélectionnée (pour le calcul de progression dans MainWindow)."""
        return self._duration_s

    def run_operation(self, config: "EncodeConfig") -> "TaskSignals":
        """Lance l'encodage et retourne les signaux de progression."""
        return self._workflow.run(config)

    def validate_config(self, config: "EncodeConfig") -> list[str]:
        """Retourne la liste des erreurs de validation (vide = OK)."""
        return self._workflow.validate(config)

    def is_pure_copy(self, config: "EncodeConfig") -> bool:
        """True si tout est en copie et qu'aucune injection HDR n'est demandée."""
        v = config.video
        return (
            v.codec == "copy"
            and all(a.codec == "copy" for a in config.audio_tracks)
            and not config.copy_dv
            and not config.copy_hdr10plus
            and not v.inject_hdr_meta
            and not v.tonemap_to_sdr
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _current_video_settings(self) -> VideoEncodeSettings:
        codec = self._codec_combo.currentData() or "libx265"
        mode  = self._mode_combo.currentData() or QualityMode.CRF
        preset = self._preset_combo.currentData() or "slow"
        try:
            bitrate = int(self._bitrate_edit.text())
        except ValueError:
            bitrate = 5000
        try:
            size = int(self._size_edit.text())
        except ValueError:
            size = 4000
        return VideoEncodeSettings(
            codec=codec,
            quality_mode=mode,
            crf=self._crf_slider.value(),
            bitrate_kbps=bitrate,
            target_size_mb=size,
            preset=preset,
            extra_params=self._extra_params.text().strip(),
            inject_hdr_meta=self._inject_hdr_cb.isChecked(),
            master_display=self._master_display.text().strip(),
            max_cll=self._max_cll.text().strip(),
            tonemap_to_sdr=self._tonemap_cb.isChecked(),
            tonemap_algorithm=self._tonemap_algo.currentData() or "hable",
        )

    def set_output_provider(self, provider: Callable[[], "Path | None"]) -> None:
        """
        Fournit un callable qui retourne le chemin de sortie courant (depuis RemuxPanel).
        Appelé par MainWindow après création des panneaux.
        """
        self._output_provider = provider

    def set_file_title_provider(self, provider: Callable[[], str]) -> None:
        """
        Fournit un callable qui retourne le titre de fichier courant (depuis RemuxPanel).
        Appelé par MainWindow après création des panneaux.
        """
        self._file_title_provider = provider

    def set_extra_attachments_provider(self, provider: Callable[[], list]) -> None:
        """
        Fournit un callable qui retourne les pièces jointes manuelles (depuis RemuxPanel).
        Appelé par MainWindow après création des panneaux.
        """
        self._extra_attachments_provider = provider

    def set_tag_overrides_provider(self, provider: "Callable[[], dict | None]") -> None:
        """
        Fournit un callable qui retourne les balises MKV éditées (depuis RemuxPanel).
        Appelé par MainWindow après création des panneaux.
        """
        self._tag_overrides_provider = provider

    def set_chapters_provider(self, provider: "Callable[[], list | None]") -> None:
        """
        Fournit un callable qui retourne les chapter_overrides (depuis RemuxPanel).
        Appelé par MainWindow après création des panneaux.
        """
        self._chapters_provider = provider

    def _current_config(self) -> EncodeConfig | None:
        if self._file_info is None:
            return None
        output = self._output_provider()
        if output is None:
            return None
        video_lang = self._video_lang_edit.text().strip()
        track_meta_edits = [TrackMetaEdit(track_order=1, language=video_lang)] if video_lang else []
        return EncodeConfig(
            source=self._file_info.path,
            output=output,
            video=self._current_video_settings(),
            audio_tracks=self._audio_table.current_audio_settings(),
            copy_subtitles=True,
            duration_s=self._duration_s,
            copy_dv=self._copy_dv_cb.isChecked(),
            copy_hdr10plus=self._copy_hdr10plus_cb.isChecked(),
            dovi_profile=self._dovi_profile_combo.currentData() or "0",
            work_dir=self._config.work_dir,
            file_title=self._file_title_provider(),
            extra_attachments=self._extra_attachments_provider(),
            track_meta_edits=track_meta_edits,
            tag_overrides=self._tag_overrides_provider(),
            chapter_overrides=self._chapters_provider(),
        )

    def _on_add_audio_track(self) -> None:
        """Ouvre le popup de sélection pour ajouter une piste audio custom."""
        if not self._audio_tracks_data:
            return
        dlg = _AudioSourceDialog(self._audio_tracks_data, config=self._config, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        track = dlg.selected_track()
        if track is None:
            return
        self._audio_table.add_custom_row(
            track,
            dlg.selected_color(),
            dlg.selected_codec(),
            dlg.selected_bitrate(),
            dlg.selected_source_path(),
        )
        self.log_message.emit(
            "INFO",
            translate_text(
                "Piste audio ajoutée : #{index} {codec} {channels} → {target}",
                index=track.index,
                codec=track.codec.upper(),
                channels=track.channels_label,
                target=dlg.selected_codec(),
            ),
        )

    def refresh_runtime_settings(self) -> None:
        self._audio_table.refresh_runtime_settings()
        self._rebuild_preview()

    def _copy_command(self) -> None:
        from PySide6.QtWidgets import QApplication
        text = self._cmd_preview.toPlainText()
        if text:
            QApplication.clipboard().setText(text)
