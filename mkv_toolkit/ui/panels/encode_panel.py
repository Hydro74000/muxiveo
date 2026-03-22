"""
ui/panels/encode_panel.py — Panneau d'encodage vidéo/audio (Phase 6).

Architecture :
    EncodePanel (QWidget)
    ├── _FileZone          — sélection/dépôt du fichier source
    ├── Section vidéo      — codec, qualité, preset, params avancés
    ├── Section HDR        — injection métadonnées statiques / tone-mapping
    ├── _AudioTable        — pistes audio avec sélecteur codec par ligne
    ├── Section profils    — sauvegarde/chargement JSON
    ├── Section sortie     — chemin de sortie
    ├── Aperçu commande    — QPlainTextEdit
    └── Barre d'action     — progress + statut + bouton Lancer

Signaux exposés :
    EncodePanel.log_message(level: str, message: str)
"""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFileDialog,
    QFrame, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
    QLineEdit, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QScrollArea, QSlider, QSpinBox, QStackedWidget,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from core.config import AppConfig
from core.inspector import AudioTrack, FileInfo, HDRType
from core.runner import TaskSignals
from core.workflows.encode import (
    AUDIO_CODECS, HARDWARE_VIDEO_CODECS, SOFTWARE_VIDEO_CODECS,
    TONEMAP_ALGORITHMS, AudioTrackSettings, EncodeConfig,
    EncodeError, EncodePreset, EncodeWorkflow, HardwareEncoderDetector,
    ProfileManager, QualityMode, VideoEncodeSettings, presets_for_codec,
)


# =============================================================================
# Palette (thème sombre cohérent avec le reste de l'app)
# =============================================================================

class _C:
    BG_DEEP    = "#0d0f14"
    BG_PANEL   = "#141720"
    BG_CARD    = "#1a1e2a"
    BG_HOVER   = "#1f2435"
    BG_ACTIVE  = "#232840"
    BORDER     = "#252a3a"
    BORDER_LT  = "#2e3450"
    TEXT_PRI   = "#e8ecf4"
    TEXT_SEC   = "#7a85a0"
    TEXT_DIM   = "#3d4560"
    ACCENT     = "#4f6ef7"
    ACCENT_DIM = "#2a3a8a"
    OK         = "#5dcc8a"
    WARN       = "#f5c842"
    ERROR      = "#f55a5a"
    INFO       = "#7ab3f5"


# =============================================================================
# Helpers UI
# =============================================================================

def _section_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(f"color:{_C.TEXT_DIM};font-size:9px;font-weight:700;"
                      f"letter-spacing:2px;background:transparent;")
    return lbl


def _card(parent: QWidget | None = None) -> QWidget:
    w = QWidget(parent)
    w.setStyleSheet(f"QWidget{{background:{_C.BG_CARD};border:1px solid {_C.BORDER};"
                    f"border-radius:6px;}}")
    return w


def _primary_button(text: str) -> QPushButton:
    btn = QPushButton(text)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setFixedHeight(36)
    btn.setStyleSheet(f"""
        QPushButton{{background:{_C.ACCENT};color:#fff;border:none;border-radius:6px;
                     font-size:12px;font-weight:700;padding:0 20px;}}
        QPushButton:hover{{background:#6070f0;}}
        QPushButton:pressed{{background:#3a52c0;}}
        QPushButton:disabled{{background:{_C.BG_ACTIVE};color:{_C.TEXT_DIM};}}
    """)
    return btn


def _secondary_button(text: str) -> QPushButton:
    btn = QPushButton(text)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setFixedHeight(28)
    btn.setStyleSheet(f"""
        QPushButton{{background:{_C.BG_CARD};color:{_C.TEXT_SEC};
                     border:1px solid {_C.BORDER};border-radius:5px;
                     font-size:11px;font-weight:500;padding:0 12px;}}
        QPushButton:hover{{background:{_C.BG_HOVER};color:{_C.TEXT_PRI};
                           border-color:{_C.BORDER_LT};}}
        QPushButton:pressed{{background:{_C.BG_ACTIVE};}}
    """)
    return btn


def _separator() -> QFrame:
    sep = QFrame()
    sep.setFrameShape(QFrame.Shape.HLine)
    sep.setFixedHeight(1)
    sep.setStyleSheet(f"background:{_C.BORDER};border:none;")
    return sep


def _input_style() -> str:
    return (f"QLineEdit{{background:{_C.BG_CARD};color:{_C.TEXT_PRI};"
            f"border:1px solid {_C.BORDER};border-radius:5px;"
            f"padding:4px 10px;font-size:11px;}}"
            f"QLineEdit:focus{{border-color:{_C.ACCENT};}}"
            f"QLineEdit:disabled{{background:{_C.BG_DEEP};color:{_C.TEXT_DIM};"
            f"border-color:{_C.BORDER};}}")


def _combo_style() -> str:
    return (f"QComboBox{{background:{_C.BG_CARD};color:{_C.TEXT_PRI};"
            f"border:1px solid {_C.BORDER};border-radius:5px;"
            f"padding:3px 8px;font-size:11px;}}"
            f"QComboBox:focus{{border-color:{_C.ACCENT};}}"
            f"QComboBox QAbstractItemView{{background:{_C.BG_CARD};"
            f"color:{_C.TEXT_PRI};selection-background-color:{_C.ACCENT_DIM};}}")


def _checkbox_style() -> str:
    return (f"QCheckBox{{color:{_C.TEXT_SEC};font-size:12px;background:transparent;}}"
            f"QCheckBox::indicator{{width:14px;height:14px;"
            f"border:1px solid {_C.BORDER_LT};border-radius:3px;"
            f"background:{_C.BG_CARD};}}"
            f"QCheckBox::indicator:checked{{background:{_C.ACCENT};"
            f"border-color:{_C.ACCENT};}}")


def _has_atmos(track: AudioTrack) -> bool:
    """Détecte si une piste TrueHD contient une couche Atmos/JOC."""
    if track.codec.lower() != "truehd":
        return False
    profile = track.raw.get("profile", "").lower()
    title   = (track.title or "").lower()
    return "atmos" in profile or "atmos" in title or "joc" in profile


# =============================================================================
# Zone de dépôt du fichier source
# =============================================================================

class _FileZone(QFrame):
    file_selected = Signal(str)
    _ACCEPTED = {".mkv", ".mp4", ".m4v", ".mov", ".ts", ".m2ts"}

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self._build_ui()

    def _build_ui(self) -> None:
        self.setStyleSheet(f"QFrame{{background:{_C.BG_CARD};"
                           f"border:1px dashed {_C.BORDER_LT};border-radius:8px;}}")
        self.setMinimumHeight(72)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(12)

        self._icon = QLabel("⊞")
        self._icon.setStyleSheet(f"font-size:24px;color:{_C.TEXT_DIM};"
                                 f"background:transparent;border:none;")
        layout.addWidget(self._icon)

        text_col = QVBoxLayout()
        text_col.setSpacing(3)
        self._main_lbl = QLabel("Déposer un fichier vidéo ici")
        self._main_lbl.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:12px;"
                                     f"font-weight:500;background:transparent;border:none;")
        self._info_lbl = QLabel("")
        self._info_lbl.setStyleSheet(f"color:{_C.TEXT_DIM};font-size:10px;"
                                     f"font-family:'JetBrains Mono',monospace;"
                                     f"background:transparent;border:none;")
        text_col.addWidget(self._main_lbl)
        text_col.addWidget(self._info_lbl)
        layout.addLayout(text_col, stretch=1)

        btn = _secondary_button("Parcourir…")
        btn.clicked.connect(self._browse)
        layout.addWidget(btn)

    def set_file_info(self, info: FileInfo) -> None:
        self._main_lbl.setText(info.path.name)
        self._main_lbl.setStyleSheet(f"color:{_C.TEXT_PRI};font-size:12px;"
                                     f"font-weight:600;background:transparent;border:none;")
        parts = [info.size_human, info.duration_human, info.format]
        if info.primary_video:
            parts.append(info.primary_video.resolution)
            if info.hdr_type.label() != "SDR":
                parts.append(info.hdr_type.label())
        self._info_lbl.setText("   ".join(p for p in parts if p != "?"))
        self.setStyleSheet(f"QFrame{{background:{_C.BG_CARD};"
                           f"border:1px solid {_C.BORDER_LT};border-radius:8px;}}")

    def reset(self) -> None:
        self._main_lbl.setText("Déposer un fichier vidéo ici")
        self._main_lbl.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:12px;"
                                     f"font-weight:500;background:transparent;border:none;")
        self._info_lbl.setText("")
        self.setStyleSheet(f"QFrame{{background:{_C.BG_CARD};"
                           f"border:1px dashed {_C.BORDER_LT};border-radius:8px;}}")

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if Path(url.toLocalFile()).suffix.lower() in self._ACCEPTED:
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        for url in event.mimeData().urls():
            path = Path(url.toLocalFile())
            if path.suffix.lower() in self._ACCEPTED and path.is_file():
                self.file_selected.emit(str(path))
                event.acceptProposedAction()
                return
        event.ignore()

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Sélectionner un fichier vidéo", "",
            "Vidéos (*.mkv *.mp4 *.m4v *.mov *.ts *.m2ts);;Tous (*)",
        )
        if path:
            self.file_selected.emit(path)


# =============================================================================
# Tableau des pistes audio
# =============================================================================

class _AudioTable(QTableWidget):
    """
    Tableau listant les pistes audio détectées avec sélecteur codec + options par ligne.

    Colonnes : #  |  Format  |  Lang  |  Encodage  |  Débit  |  Options
    """

    COL_IDX     = 0
    COL_FORMAT  = 1
    COL_LANG    = 2
    COL_CODEC   = 3
    COL_BITRATE = 4
    COL_OPTIONS = 5
    COL_WARN    = 6

    HEADERS = ["#", "Format", "Lang", "Encodage", "Débit", "Options", "⚠"]

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(0, len(self.HEADERS), parent)
        self._tracks: list[AudioTrack] = []
        self._row_widgets: list[dict] = []   # {combo, bitrate, core_cb}
        self._setup_table()

    def _setup_table(self) -> None:
        self.setHorizontalHeaderLabels(self.HEADERS)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.verticalHeader().setVisible(False)
        self.setShowGrid(False)
        self.setAlternatingRowColors(False)
        hh = self.horizontalHeader()
        hh.setSectionResizeMode(self.COL_IDX,     QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(self.COL_FORMAT,  QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(self.COL_LANG,    QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(self.COL_CODEC,   QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(self.COL_BITRATE, QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(self.COL_OPTIONS, QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(self.COL_WARN,    QHeaderView.ResizeMode.Fixed)
        self.setColumnWidth(self.COL_IDX,     32)
        self.setColumnWidth(self.COL_FORMAT, 110)
        self.setColumnWidth(self.COL_LANG,    48)
        self.setColumnWidth(self.COL_BITRATE, 80)
        self.setColumnWidth(self.COL_OPTIONS,110)
        self.setColumnWidth(self.COL_WARN,    24)
        self.setStyleSheet(f"""
            QTableWidget{{background:{_C.BG_CARD};color:{_C.TEXT_PRI};
                          border:1px solid {_C.BORDER};border-radius:6px;
                          gridline-color:transparent;font-size:11px;}}
            QHeaderView::section{{background:{_C.BG_ACTIVE};color:{_C.TEXT_DIM};
                                   border:none;padding:4px 8px;font-size:9px;
                                   font-weight:700;letter-spacing:1px;}}
            QTableWidget::item{{padding:4px 8px;border:none;}}
            QTableWidget::item:selected{{background:{_C.ACCENT_DIM};}}
        """)

    def load_tracks(
        self,
        tracks: list[AudioTrack],
        default_codec: str = "copy",
        default_bitrate: int = 384,
    ) -> None:
        self._tracks = tracks
        self._row_widgets = []
        self.setRowCount(0)
        for track in tracks:
            self._append_row(track, default_codec, default_bitrate)

    def _append_row(self, track: AudioTrack, default_codec: str, default_bitrate: int) -> None:
        row = self.rowCount()
        self.insertRow(row)
        self.setRowHeight(row, 36)

        def _item(text: str) -> QTableWidgetItem:
            it = QTableWidgetItem(text)
            it.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            return it

        self.setItem(row, self.COL_IDX,    _item(str(track.index)))
        self.setItem(row, self.COL_FORMAT, _item(f"{track.codec.upper()} {track.channels_label}"))
        self.setItem(row, self.COL_LANG,   _item(track.language or "—"))

        # Sélecteur codec
        combo = QComboBox()
        for codec_id, codec_label in AUDIO_CODECS:
            combo.addItem(codec_label, codec_id)
        idx = next((i for i, (cid, _) in enumerate(AUDIO_CODECS) if cid == default_codec), 0)
        combo.setCurrentIndex(idx)
        combo.setStyleSheet(_combo_style())
        combo.currentIndexChanged.connect(lambda _, r=row: self._on_codec_changed(r))
        self.setCellWidget(row, self.COL_CODEC, combo)

        # Débit
        bitrate_edit = QLineEdit(str(default_bitrate))
        bitrate_edit.setStyleSheet(_input_style())
        bitrate_edit.setFixedWidth(72)
        bitrate_edit.setEnabled(default_codec not in ("copy", "flac"))
        self.setCellWidget(row, self.COL_BITRATE, bitrate_edit)

        # Options : extraction core TrueHD
        core_cb = QCheckBox("Core TrueHD")
        core_cb.setChecked(False)
        core_cb.setEnabled(track.codec.lower() == "truehd")
        core_cb.setStyleSheet(_checkbox_style())
        core_cb.setVisible(track.codec.lower() == "truehd")
        self.setCellWidget(row, self.COL_OPTIONS, core_cb)

        # Avertissement JOC/Atmos
        warn_lbl = QLabel()
        has_atmos = _has_atmos(track)
        if has_atmos:
            warn_lbl.setText("!")
            warn_lbl.setToolTip(
                "TrueHD + Atmos (JOC) détecté.\n"
                "FFmpeg ne peut pas encoder EAC-3 JOC.\n"
                "Utilisez 'Copie' ou extrayez le core TrueHD."
            )
            warn_lbl.setStyleSheet(
                f"color:{_C.WARN};font-weight:700;font-size:13px;"
                f"background:transparent;"
            )
        self.setCellWidget(row, self.COL_WARN, warn_lbl)

        self._row_widgets.append({
            "combo":     combo,
            "bitrate":   bitrate_edit,
            "core_cb":   core_cb,
            "has_atmos": has_atmos,
        })

    def _on_codec_changed(self, row: int) -> None:
        if row >= len(self._row_widgets):
            return
        w = self._row_widgets[row]
        codec = w["combo"].currentData()
        w["bitrate"].setEnabled(codec not in ("copy", "flac"))

    def current_audio_settings(self) -> list[AudioTrackSettings]:
        result: list[AudioTrackSettings] = []
        for i, (track, w) in enumerate(zip(self._tracks, self._row_widgets)):
            codec = w["combo"].currentData() or "copy"
            try:
                bitrate = int(w["bitrate"].text())
            except ValueError:
                bitrate = 384
            result.append(AudioTrackSettings(
                stream_index=track.index,
                codec=codec,
                bitrate_kbps=bitrate,
                extract_truehd_core=w["core_cb"].isChecked(),
            ))
        return result

    def has_unhandled_atmos(self) -> list[int]:
        """
        Retourne les indices de rangées où Atmos est détecté ET l'encodage
        cible est EAC-3 sans extraction core — combinaison que FFmpeg ne supporte pas.
        """
        risky = []
        for i, w in enumerate(self._row_widgets):
            if w["has_atmos"] and w["combo"].currentData() == "eac3" and not w["core_cb"].isChecked():
                risky.append(i)
        return risky


# =============================================================================
# Panneau principal
# =============================================================================

_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")
_FPS_RE  = re.compile(r"\bfps=\s*([\d.]+)")


def _fmt_eta(seconds: float) -> str:
    """Formate une durée en 'Xm Xs' ou 'Xs'. Retourne '—' si indéterminé."""
    if seconds <= 0 or seconds != seconds:   # négatif ou NaN/inf
        return "—"
    s = int(seconds)
    m, s = divmod(s, 60)
    return f"{m}m {s:02d}s" if m else f"{s}s"


class EncodePanel(QWidget):
    """
    Panneau d'encodage vidéo/audio — Phase 6.

    Signaux :
        log_message(level: str, message: str)
    """

    log_message  = Signal(str, str)
    _hw_detected = Signal(object)   # set[str]

    def __init__(self, config: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config    = config
        self._workflow  = EncodeWorkflow(ffmpeg_bin=config.tool_ffmpeg, parent=self)
        self._profiles  = ProfileManager(config.app_data_dir / "encode_profiles")
        self._executor  = ThreadPoolExecutor(max_workers=1)
        self._file_info: FileInfo | None = None
        self._running   = False
        self._duration_s: float | None = None
        self._hw_encoders: set[str] = set()
        self._signals: TaskSignals | None = None

        self._workflow.log_message.connect(self.log_message, Qt.ConnectionType.QueuedConnection)
        self._hw_detected.connect(self._on_hw_detected, Qt.ConnectionType.QueuedConnection)

        self._build_ui()
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

        # --- Fichier source ---
        cl.addWidget(_section_label("FICHIER SOURCE"))
        cl.addWidget(self._build_source_card())
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
        self._atmos_warn = QLabel(
            "⚠  TrueHD + Atmos (JOC) détecté — FFmpeg ne peut pas encoder EAC-3 JOC. "
            "Utilisez Copie ou activez l'extraction du core TrueHD."
        )
        self._atmos_warn.setWordWrap(True)
        self._atmos_warn.setStyleSheet(f"color:{_C.WARN};font-size:11px;"
                                       f"background:transparent;padding:4px 0;")
        self._atmos_warn.setVisible(False)
        cl.addWidget(self._atmos_warn)
        self._audio_table = _AudioTable()
        self._audio_table.setMinimumHeight(120)
        cl.addWidget(self._audio_table)
        cl.addWidget(_separator())

        # --- Profils ---
        cl.addWidget(_section_label("PROFILS"))
        cl.addWidget(self._build_profiles_card())
        cl.addWidget(_separator())

        # --- Fichier de sortie ---
        cl.addWidget(_section_label("FICHIER DE SORTIE"))
        out_row = QHBoxLayout()
        out_row.setSpacing(8)
        self._output_edit = QLineEdit()
        self._output_edit.setPlaceholderText("/chemin/vers/sortie.mkv")
        self._output_edit.setStyleSheet(_input_style())
        self._output_edit.textChanged.connect(self._rebuild_preview)
        out_row.addWidget(self._output_edit, stretch=1)
        browse_out = _secondary_button("Choisir…")
        browse_out.clicked.connect(self._browse_output)
        out_row.addWidget(browse_out)
        cl.addLayout(out_row)
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

        # --- Barre d'action ---
        btn_bar = QWidget()
        btn_bar.setStyleSheet(f"QWidget{{background:{_C.BG_PANEL};"
                              f"border-top:1px solid {_C.BORDER};}}")
        bbl = QHBoxLayout(btn_bar)
        bbl.setContentsMargins(28, 12, 28, 12)
        bbl.setSpacing(12)

        # Conteneur vertical : barre fine + légende (pct · fps · ETA)
        self._progress_widget = QWidget()
        self._progress_widget.setStyleSheet("background:transparent;")
        _pvl = QVBoxLayout(self._progress_widget)
        _pvl.setContentsMargins(0, 4, 0, 4)
        _pvl.setSpacing(4)

        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.setFixedHeight(6)
        self._progress_bar.setTextVisible(False)
        self._progress_bar.setStyleSheet(
            f"QProgressBar{{background:{_C.BG_ACTIVE};border:none;border-radius:3px;}}"
            f"QProgressBar::chunk{{background:{_C.ACCENT};border-radius:3px;}}"
        )
        _pvl.addWidget(self._progress_bar)

        self._progress_lbl = QLabel("")
        self._progress_lbl.setStyleSheet(
            f"color:{_C.TEXT_DIM};font-size:10px;"
            f"font-family:'JetBrains Mono',monospace;background:transparent;"
        )
        _pvl.addWidget(self._progress_lbl)

        self._progress_widget.setVisible(False)
        bbl.addWidget(self._progress_widget, stretch=1)

        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:11px;background:transparent;")
        bbl.addWidget(self._status_lbl)
        bbl.addSpacing(4)

        self._run_btn = _primary_button("▶  Lancer l'encodage")
        self._run_btn.setFixedWidth(200)
        self._run_btn.setEnabled(False)
        self._run_btn.clicked.connect(self._on_run)
        bbl.addWidget(self._run_btn)

        self._cancel_btn = QPushButton("✕  Annuler")
        self._cancel_btn.setFixedWidth(110)
        self._cancel_btn.setFixedHeight(36)
        self._cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._cancel_btn.setStyleSheet(f"""
            QPushButton{{background:{_C.BG_CARD};color:{_C.WARN};
                         border:1px solid {_C.WARN};border-radius:6px;
                         font-size:12px;font-weight:600;padding:0 14px;}}
            QPushButton:hover{{background:#2a2010;border-color:#f0b030;color:#f0b030;}}
            QPushButton:pressed{{background:#1a1608;}}
        """)
        self._cancel_btn.setVisible(False)
        self._cancel_btn.clicked.connect(self._on_cancel)
        bbl.addWidget(self._cancel_btn)

        root.addWidget(btn_bar)

    def _build_source_card(self) -> QWidget:
        """Carte read-only affichant les infos du fichier sélectionné dans l'onglet Conteneur."""
        card = _card()
        cl = QHBoxLayout(card)
        cl.setContentsMargins(16, 12, 16, 12)
        cl.setSpacing(12)

        self._src_icon = QLabel("⊞")
        self._src_icon.setStyleSheet(f"font-size:22px;color:{_C.TEXT_DIM};"
                                     f"background:transparent;border:none;")
        cl.addWidget(self._src_icon)

        info_col = QVBoxLayout()
        info_col.setSpacing(3)

        self._src_placeholder = QLabel(
            "Aucun fichier — sélectionnez un fichier dans l'onglet Conteneur"
        )
        self._src_placeholder.setStyleSheet(f"color:{_C.TEXT_DIM};font-size:11px;"
                                            f"background:transparent;border:none;")
        info_col.addWidget(self._src_placeholder)

        self._src_name = QLabel("")
        self._src_name.setStyleSheet(f"color:{_C.TEXT_PRI};font-size:12px;font-weight:600;"
                                     f"background:transparent;border:none;")
        self._src_name.setVisible(False)
        info_col.addWidget(self._src_name)

        self._src_meta = QLabel("")
        self._src_meta.setStyleSheet(f"color:{_C.TEXT_DIM};font-size:10px;"
                                     f"font-family:'JetBrains Mono',monospace;"
                                     f"background:transparent;border:none;")
        self._src_meta.setVisible(False)
        info_col.addWidget(self._src_meta)

        self._src_hdr = QLabel("")
        self._src_hdr.setStyleSheet(f"color:{_C.INFO};font-size:10px;font-weight:600;"
                                    f"background:transparent;border:none;")
        self._src_hdr.setVisible(False)
        info_col.addWidget(self._src_hdr)

        self._src_audio_count = QLabel("")
        self._src_audio_count.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:10px;"
                                            f"background:transparent;border:none;")
        self._src_audio_count.setVisible(False)
        info_col.addWidget(self._src_audio_count)

        cl.addLayout(info_col, stretch=1)
        return card

    # ------------------------------------------------------------------
    # API publique — appelée par MainWindow depuis RemuxPanel
    # ------------------------------------------------------------------

    def set_file_info(self, info: FileInfo) -> None:
        """Reçoit les infos du fichier sélectionné dans l'onglet Conteneur."""
        self._file_info  = info
        self._duration_s = info.duration_s

        # Carte source
        self._src_placeholder.setVisible(False)
        self._src_icon.setStyleSheet(f"font-size:22px;color:{_C.ACCENT};"
                                     f"background:transparent;border:none;")
        self._src_name.setText(info.path.name)
        self._src_name.setVisible(True)

        parts: list[str] = [info.size_human, info.duration_human, info.format]
        if info.primary_video:
            parts.append(info.primary_video.resolution)
        self._src_meta.setText("   ".join(p for p in parts if p != "?"))
        self._src_meta.setVisible(True)

        hdr_label = info.hdr_type.label()
        if hdr_label not in ("SDR", "?"):
            self._src_hdr.setText(hdr_label)
            self._src_hdr.setVisible(True)
        else:
            self._src_hdr.setVisible(False)

        # Pré-remplir master_display et max_cll
        if info.primary_video:
            self._prefill_hdr_meta(info.primary_video.raw)

        # Chemin de sortie par défaut
        default_out = self._config.output_dir / f"{info.path.stem}_encode.mkv"
        self._output_edit.setText(str(default_out))

        self._run_btn.setEnabled(True)
        self._set_status("")
        self._update_passthrough_controls(auto_check=True)
        self.log_message.emit(
            "OK",
            f"{info.path.name} — "
            f"{len(info.video_tracks)}V  {len(info.audio_tracks)}A  "
            f"{len(info.subtitle_tracks)}S  {info.hdr_type.label()}",
        )
        self._rebuild_preview()

    def set_audio_tracks(self, tracks: list[AudioTrack]) -> None:
        """Met à jour les pistes audio depuis les pistes activées dans l'onglet Conteneur."""
        default_codec   = "copy"
        default_bitrate = 384
        profile_name = self._profile_combo.currentText()
        if profile_name:
            for p in self._profiles.load_all():
                if p.name == profile_name:
                    default_codec   = p.default_audio_codec
                    default_bitrate = p.default_audio_bitrate_kbps
                    break

        self._audio_table.load_tracks(tracks, default_codec, default_bitrate)
        self._update_atmos_warning()

        n = len(tracks)
        label = f"{n} piste{'s' if n != 1 else ''} audio sélectionnée{'s' if n != 1 else ''}"
        self._src_audio_count.setText(label)
        self._src_audio_count.setVisible(True)

        self._rebuild_preview()

    # ------------------------------------------------------------------
    # Carte encodage vidéo
    # ------------------------------------------------------------------

    def _build_video_card(self) -> QWidget:
        card = _card()
        cl = QVBoxLayout(card)
        cl.setContentsMargins(16, 14, 16, 14)
        cl.setSpacing(12)

        # Ligne codec
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

        preset_lbl = QLabel("Preset")
        preset_lbl.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:11px;background:transparent;")
        r1.addWidget(preset_lbl)
        self._preset_combo = QComboBox()
        self._preset_combo.setStyleSheet(_combo_style())
        self._preset_combo.setMinimumWidth(120)
        r1.addWidget(self._preset_combo)
        cl.addLayout(r1)

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
        cl.addLayout(r2)

        # Params avancés
        adv_lbl = QLabel("Params avancés  (x265-params / svtav1-params)")
        adv_lbl.setStyleSheet(f"color:{_C.TEXT_DIM};font-size:10px;background:transparent;")
        cl.addWidget(adv_lbl)
        self._extra_params = QLineEdit()
        self._extra_params.setPlaceholderText("ex. no-open-gop=1:hdr10=1:hdr10-opt=1")
        self._extra_params.setStyleSheet(_input_style())
        self._extra_params.textChanged.connect(lambda _: self._rebuild_preview())
        cl.addWidget(self._extra_params)

        self._on_codec_changed()   # initialise preset combo
        return card

    def _build_hdr_card(self) -> QWidget:
        card = _card()
        cl = QVBoxLayout(card)
        cl.setContentsMargins(16, 14, 16, 14)
        cl.setSpacing(10)

        # Injection métadonnées HDR10 statiques
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

        # Tone mapping HDR→SDR
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

        # Sous-titres
        self._subs_cb = QCheckBox("Copier les sous-titres")
        self._subs_cb.setChecked(True)
        self._subs_cb.setStyleSheet(_checkbox_style())
        self._subs_cb.stateChanged.connect(lambda _: self._rebuild_preview())
        cl.addWidget(self._subs_cb)

        cl.addWidget(_separator())

        # Passthrough Dolby Vision RPU
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

        # Passthrough HDR10+ SEI
        self._copy_hdr10plus_cb = QCheckBox("Copier les métadonnées HDR10+ depuis la source")
        self._copy_hdr10plus_cb.setStyleSheet(_checkbox_style())
        self._copy_hdr10plus_cb.setEnabled(False)
        self._copy_hdr10plus_cb.stateChanged.connect(lambda _: self._rebuild_preview())
        cl.addWidget(self._copy_hdr10plus_cb)

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
        for sd in raw.get("side_data_list", []):
            if sd.get("side_data_type") == "Mastering display metadata":
                try:
                    rx = sd.get("red_x", 0); ry = sd.get("red_y", 0)
                    gx = sd.get("green_x", 0); gy = sd.get("green_y", 0)
                    bx = sd.get("blue_x", 0); by = sd.get("blue_y", 0)
                    wx = sd.get("white_point_x", 0); wy = sd.get("white_point_y", 0)
                    lmax = sd.get("max_luminance", 0); lmin = sd.get("min_luminance", 0)
                    def p(v) -> int:
                        f = float(v)
                        return int(f * 50000) if f < 1 else int(f)
                    md = (f"G({p(gx)},{p(gy)})"
                          f"B({p(bx)},{p(by)})"
                          f"R({p(rx)},{p(ry)})"
                          f"WP({p(wx)},{p(wy)})"
                          f"L({p(lmax)},{p(lmin)})")
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
        detected = HardwareEncoderDetector().detect(self._config.tool_ffmpeg)
        self._hw_detected.emit(detected)

    def _on_hw_detected(self, detected: set[str]) -> None:
        self._hw_encoders = detected
        current = self._codec_combo.currentData()
        self._populate_codec_combo()
        # Restaure la sélection précédente si toujours disponible
        for i in range(self._codec_combo.count()):
            if self._codec_combo.itemData(i) == current:
                self._codec_combo.setCurrentIndex(i)
                break
        if detected:
            self.log_message.emit(
                "OK", f"Encodeurs matériels détectés : {', '.join(sorted(detected))}"
            )

    def _populate_codec_combo(self) -> None:
        self._codec_combo.blockSignals(True)
        self._codec_combo.clear()
        for codec_id, label in SOFTWARE_VIDEO_CODECS:
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
        is_hevc = codec in ("libx265", "hevc_nvenc", "hevc_amf", "hevc_qsv")
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

    def _update_atmos_warning(self) -> None:
        risky = self._audio_table.has_unhandled_atmos()
        self._atmos_warn.setVisible(bool(risky))

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
            self._cmd_preview.setPlainText("(erreur de construction de la commande)")

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
        self.log_message.emit("OK", f"Profil chargé : {name}")

    def _save_profile(self) -> None:
        name = self._profile_name.text().strip()
        if not name:
            name, ok = QInputDialog.getText(self, "Enregistrer le profil", "Nom du profil :")
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
        self.log_message.emit("OK", f"Profil enregistré : {name}")

    def _delete_profile(self) -> None:
        name = self._profile_combo.currentText()
        if name:
            self._profiles.delete(name)
            self._refresh_profiles()
            self.log_message.emit("INFO", f"Profil supprimé : {name}")

    # ------------------------------------------------------------------
    # Exécution
    # ------------------------------------------------------------------

    def _on_run(self) -> None:
        if self._running:
            return
        config = self._current_config()
        if config is None:
            self.log_message.emit("WARN", "Configuration incomplète.")
            return

        # Avertissement Atmos non géré (non bloquant — log WARN)
        risky = self._audio_table.has_unhandled_atmos()
        for row in risky:
            self.log_message.emit(
                "WARN",
                f"Piste audio ligne {row + 1} : TrueHD Atmos → EAC-3 "
                "non supporté par FFmpeg. La piste sera encodée sans la couche JOC.",
            )

        errors = self._workflow.validate(config)
        if errors:
            for e in errors:
                self.log_message.emit("ERROR", e)
            return

        self._running = True
        self._op_start = time.monotonic()
        self._run_btn.setEnabled(False)
        self._cancel_btn.setVisible(True)
        self._progress_bar.setValue(0)
        self._progress_lbl.setText("")
        self._progress_widget.setVisible(True)
        self._set_status("Encodage en cours…")
        self.log_message.emit("INFO", f"Démarrage → {config.output.name}")

        try:
            signals = self._workflow.run(config)
        except EncodeError as exc:
            self.log_message.emit("ERROR", str(exc))
            self._on_run_finished(success=False)
            return

        self._signals = signals
        signals.progress.connect(self._on_progress, Qt.ConnectionType.QueuedConnection)
        signals.finished.connect(
            lambda _: self._on_run_finished(success=True),
            Qt.ConnectionType.QueuedConnection,
        )
        signals.failed.connect(
            lambda msg, _exc: self._on_run_finished(success=False, error=msg),
            Qt.ConnectionType.QueuedConnection,
        )
        signals.cancelled.connect(
            self._on_run_cancelled,
            Qt.ConnectionType.QueuedConnection,
        )

    # Patterns de lignes ffmpeg à ignorer silencieusement (bibliothèques compilées
    # mais non disponibles à l'exécution, e.g. libvmaf sans modèles installés).
    _NOISE_RE = re.compile(r"libvmaf\s+ERROR|could not read model from path")

    def _on_progress(self, line: str) -> None:
        """Parse les stats ffmpeg (frame=… fps=… time=…) et met à jour la barre + légende."""
        if self._NOISE_RE.search(line):
            return
        m = _TIME_RE.search(line)
        if m:
            elapsed_video = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
            dur = self._duration_s
            if dur and dur > 0:
                pct = min(99, int(elapsed_video / dur * 100))
                self._progress_bar.setValue(pct)

                # FPS d'encodage (throughput, pas le fps du fichier source)
                fps_m = _FPS_RE.search(line)
                fps_str = f"{float(fps_m.group(1)):.1f} fps" if fps_m else ""

                # ETA : temps restant basé sur le ratio vidéo encodée / temps réel
                elapsed_wall = time.monotonic() - self._op_start
                if elapsed_wall > 0 and elapsed_video > 0:
                    speed = elapsed_video / elapsed_wall          # s_video / s_réel
                    eta_s = (dur - elapsed_video) / speed
                    eta_str = f"ETA {_fmt_eta(eta_s)}"
                else:
                    eta_str = ""

                parts = [f"{pct}%", fps_str, eta_str]
                self._progress_lbl.setText("  ·  ".join(p for p in parts if p))
            return
        self.log_message.emit("INFO", line)

    def _on_cancel(self) -> None:
        reply = QMessageBox.question(
            self,
            "Confirmer l'annulation",
            "Annuler l'encodage en cours ?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes and self._signals is not None:
            self._signals.cancel()

    def _on_run_cancelled(self) -> None:
        self._running = False
        self._signals = None
        self._run_btn.setEnabled(True)
        self._cancel_btn.setVisible(False)
        self._progress_widget.setVisible(False)
        self._progress_lbl.setText("")
        self._set_status("Annulé.")
        self.log_message.emit("WARN", "Encodage annulé.")

    def _on_run_finished(self, success: bool, error: str = "") -> None:
        self._running = False
        self._signals = None
        self._run_btn.setEnabled(True)
        self._cancel_btn.setVisible(False)
        if success:
            config = self._current_config()
            out = config.output if config else None
            self._progress_bar.setValue(100)
            self._progress_lbl.setText("100%  ·  terminé")
            self._set_status("Terminé.")
            self.log_message.emit("OK", f"Encodage terminé → {out}")
        else:
            self._progress_widget.setVisible(False)
            self._progress_lbl.setText("")
            self._set_status("Échec.")
            if error:
                self.log_message.emit("ERROR", error)

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

    def _current_config(self) -> EncodeConfig | None:
        if self._file_info is None:
            return None
        output_str = self._output_edit.text().strip()
        if not output_str:
            return None
        return EncodeConfig(
            source=self._file_info.path,
            output=Path(output_str),
            video=self._current_video_settings(),
            audio_tracks=self._audio_table.current_audio_settings(),
            copy_subtitles=self._subs_cb.isChecked(),
            duration_s=self._duration_s,
            copy_dv=self._copy_dv_cb.isChecked(),
            copy_hdr10plus=self._copy_hdr10plus_cb.isChecked(),
            dovi_profile=self._dovi_profile_combo.currentData() or "0",
            work_dir=self._config.work_dir,
        )

    def _browse_output(self) -> None:
        default = self._output_edit.text() or str(self._config.output_dir)
        path, _ = QFileDialog.getSaveFileName(
            self, "Fichier de sortie", default,
            "Matroska (*.mkv);;MP4 (*.mp4);;Tous (*)",
        )
        if path:
            self._output_edit.setText(path)

    def _copy_command(self) -> None:
        from PySide6.QtWidgets import QApplication
        text = self._cmd_preview.toPlainText()
        if text:
            QApplication.clipboard().setText(text)
            self._set_status("Commande copiée.")

    def _set_status(self, text: str) -> None:
        self._status_lbl.setText(text)
