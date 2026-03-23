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
from PySide6.QtGui import QBrush, QColor, QDragEnterEvent, QDropEvent, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QFileDialog,
    QFrame, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton,
    QScrollArea, QSlider, QSpinBox, QStackedWidget,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from core.config import AppConfig
from core.inspector import AudioTrack, FileInfo, HDRType
from core.workflows.remux import TrackEntry
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
# Popup de sélection source pour piste audio custom
# =============================================================================

class _AudioSourceDialog(QDialog):
    """
    Fenêtre popup pour ajouter une piste audio custom.
    Permet de choisir la piste source, l'encodage et le débit cible.
    """

    def __init__(
        self,
        tracks: list[tuple],   # list[tuple[AudioTrack, str]] — (track, color)
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._tracks = tracks
        self._result_track:  AudioTrack | None = None
        self._result_color:  str = "#ffffff"
        self._setup_ui()

    def _setup_ui(self) -> None:
        self.setWindowTitle("Ajouter une piste audio")
        self.setModal(True)
        self.setMinimumWidth(500)
        self.setStyleSheet(
            f"QDialog{{background:{_C.BG_PANEL};}}"
            f"QLabel{{background:transparent;color:{_C.TEXT_PRI};}}"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)

        # Titre
        title = QLabel("Sélectionner la piste source")
        title.setStyleSheet(
            f"font-size:14px;font-weight:700;color:{_C.TEXT_PRI};"
        )
        layout.addWidget(title)

        sub = QLabel(
            "La piste sera ajoutée en tant qu'encodage supplémentaire de la source choisie."
        )
        sub.setStyleSheet(f"font-size:11px;color:{_C.TEXT_SEC};")
        sub.setWordWrap(True)
        layout.addWidget(sub)

        # Liste des pistes source
        self._track_list = QListWidget()
        self._track_list.setStyleSheet(
            f"QListWidget{{background:{_C.BG_CARD};border:1px solid {_C.BORDER};"
            f"border-radius:6px;color:{_C.TEXT_PRI};font-size:11px;"
            f"font-family:'JetBrains Mono',monospace;}}"
            f"QListWidget::item{{padding:10px 12px;"
            f"border-bottom:1px solid {_C.BORDER};}}"
            f"QListWidget::item:selected{{background:{_C.ACCENT_DIM};}}"
            f"QListWidget::item:hover{{background:{_C.BG_HOVER};}}"
        )
        for track, color in self._tracks:
            ch = track.channels_label
            lang = track.language or "—"
            title_part = f"  {track.title}" if track.title else ""
            text = f"█  #{track.index}  {track.codec.upper()} {ch}  [{lang}]{title_part}"
            item = QListWidgetItem(text)
            item.setForeground(QBrush(QColor(color)))
            item.setData(Qt.ItemDataRole.UserRole, (track, color))
            self._track_list.addItem(item)
        if self._track_list.count():
            self._track_list.setCurrentRow(0)
        n = min(self._track_list.count(), 6)
        self._track_list.setFixedHeight(n * 40 + 4)
        layout.addWidget(self._track_list)

        layout.addWidget(_separator())

        # Encodage + débit
        enc_row = QHBoxLayout()
        enc_row.setSpacing(12)
        enc_lbl = QLabel("Encodage")
        enc_lbl.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:11px;")
        enc_lbl.setFixedWidth(70)
        enc_row.addWidget(enc_lbl)

        self._codec_combo = QComboBox()
        self._codec_combo.setStyleSheet(_combo_style())
        self._codec_combo.setMinimumWidth(200)
        for codec_id, codec_label in AUDIO_CODECS:
            self._codec_combo.addItem(codec_label, codec_id)
        self._codec_combo.currentIndexChanged.connect(self._on_codec_changed)
        enc_row.addWidget(self._codec_combo)
        enc_row.addStretch()
        layout.addLayout(enc_row)

        br_row = QHBoxLayout()
        br_row.setSpacing(12)
        br_lbl = QLabel("Débit cible")
        br_lbl.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:11px;")
        br_lbl.setFixedWidth(70)
        br_row.addWidget(br_lbl)
        self._bitrate_edit = QLineEdit("384")
        self._bitrate_edit.setStyleSheet(_input_style())
        self._bitrate_edit.setFixedWidth(90)
        self._bitrate_edit.setEnabled(False)   # "copy" par défaut
        br_row.addWidget(self._bitrate_edit)
        br_kbps = QLabel("kbps")
        br_kbps.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:11px;")
        br_row.addWidget(br_kbps)
        br_row.addStretch()
        layout.addLayout(br_row)

        layout.addSpacing(4)

        # Boutons
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = _secondary_button("Annuler")
        cancel_btn.clicked.connect(self.reject)
        add_btn = _primary_button("Ajouter la piste")
        add_btn.setFixedWidth(160)
        add_btn.clicked.connect(self._on_accept)
        btn_row.addWidget(cancel_btn)
        btn_row.addSpacing(8)
        btn_row.addWidget(add_btn)
        layout.addLayout(btn_row)

    def _on_codec_changed(self, _idx: int = 0) -> None:
        codec = self._codec_combo.currentData()
        self._bitrate_edit.setEnabled(codec not in ("copy", "flac"))

    def _on_accept(self) -> None:
        item = self._track_list.currentItem()
        if item is None:
            return
        self._result_track, self._result_color = item.data(Qt.ItemDataRole.UserRole)
        self.accept()

    def selected_track(self) -> AudioTrack | None:
        return self._result_track

    def selected_color(self) -> str:
        return self._result_color

    def selected_codec(self) -> str:
        return self._codec_combo.currentData() or "copy"

    def selected_bitrate(self) -> int:
        try:
            return int(self._bitrate_edit.text())
        except ValueError:
            return 384


# =============================================================================
# Tableau des pistes audio
# =============================================================================

class _AudioTable(QTableWidget):
    """
    Tableau listant les pistes audio avec sélecteur codec + débit par ligne.
    Chaque ligne dispose d'un bouton de suppression, désactivé si c'est la
    dernière entrée pour cette piste source.

    Colonnes : src  |  #  |  Format  |  Lang  |  Encodage  |  Débit  |  Del
    """

    COL_SOURCE  = 0
    COL_IDX     = 1
    COL_FORMAT  = 2
    COL_LANG    = 3
    COL_CODEC   = 4
    COL_BITRATE = 5
    COL_DEL     = 6
    HEADERS = ["", "#", "Format", "Lang", "Encodage", "Débit", ""]

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(0, len(self.HEADERS), parent)
        self._row_data: list[dict] = []   # {combo, bitrate, has_atmos, track, color, del_btn}
        self._changed_cb = None
        self._setup_table()

    def set_changed_callback(self, cb) -> None:
        self._changed_cb = cb

    def _setup_table(self) -> None:
        self.setHorizontalHeaderLabels(self.HEADERS)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.verticalHeader().setVisible(False)
        self.setShowGrid(False)
        self.setAlternatingRowColors(False)
        hh = self.horizontalHeader()
        hh.setSectionResizeMode(self.COL_SOURCE,  QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(self.COL_IDX,     QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(self.COL_FORMAT,  QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(self.COL_LANG,    QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(self.COL_CODEC,   QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(self.COL_BITRATE, QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(self.COL_DEL,     QHeaderView.ResizeMode.Fixed)
        self.setColumnWidth(self.COL_SOURCE,  20)
        self.setColumnWidth(self.COL_IDX,     32)
        self.setColumnWidth(self.COL_FORMAT, 110)
        self.setColumnWidth(self.COL_LANG,    48)
        self.setColumnWidth(self.COL_BITRATE, 80)
        self.setColumnWidth(self.COL_DEL,     36)
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

    _MAX_VISIBLE_ROWS = 10
    _ROW_H = 36

    # ------------------------------------------------------------------
    # API publique
    # ------------------------------------------------------------------

    def load_tracks(
        self,
        tracks: list[tuple],   # list[tuple[AudioTrack, str]]
        default_codec: str = "copy",
        default_bitrate: int = 384,
    ) -> None:
        self._row_data = []
        self.setRowCount(0)
        for track, color in tracks:
            self._append_row(track, color, default_codec, default_bitrate)
        self._refresh_delete_buttons()
        self._adjust_height()

    def add_custom_row(
        self, track: AudioTrack, color: str, codec: str = "copy", bitrate: int = 384
    ) -> None:
        self._append_row(track, color, codec, bitrate)
        self._refresh_delete_buttons()
        self._adjust_height()
        if self._changed_cb:
            self._changed_cb()

    def current_audio_settings(self) -> list[AudioTrackSettings]:
        result: list[AudioTrackSettings] = []
        for d in self._row_data:
            codec = d["combo"].currentData() or "copy"
            try:
                bitrate = int(d["bitrate"].text())
            except ValueError:
                bitrate = 384
            result.append(AudioTrackSettings(
                stream_index=d["track"].index,
                codec=codec,
                bitrate_kbps=bitrate,
                extract_truehd_core=d["has_atmos"] and codec != "copy",
            ))
        return result

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _adjust_height(self) -> None:
        n = self.rowCount()
        header_h = self.horizontalHeader().height()
        if n == 0:
            self.setFixedHeight(header_h + 40)
            return
        visible = min(n, self._MAX_VISIBLE_ROWS)
        self.setFixedHeight(visible * self._ROW_H + header_h + 4)

    def _append_row(
        self, track: AudioTrack, color: str, codec: str, bitrate: int
    ) -> None:
        row = self.rowCount()
        self.insertRow(row)
        self.setRowHeight(row, self._ROW_H)

        def _item(text: str) -> QTableWidgetItem:
            it = QTableWidgetItem(text)
            it.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            return it

        src_item = QTableWidgetItem("█")
        src_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        src_item.setForeground(QBrush(QColor(color)))
        src_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setItem(row, self.COL_SOURCE, src_item)

        self.setItem(row, self.COL_IDX,    _item(str(track.index)))
        self.setItem(row, self.COL_FORMAT, _item(f"{track.codec.upper()} {track.channels_label}"))
        self.setItem(row, self.COL_LANG,   _item(track.language or "—"))

        # Sélecteur codec
        combo = QComboBox()
        for codec_id, codec_label in AUDIO_CODECS:
            combo.addItem(codec_label, codec_id)
        sel_idx = next((i for i, (cid, _) in enumerate(AUDIO_CODECS) if cid == codec), 0)
        combo.setCurrentIndex(sel_idx)
        combo.setStyleSheet(_combo_style())
        combo.currentIndexChanged.connect(self._make_codec_handler(combo))
        if self._changed_cb:
            combo.currentIndexChanged.connect(lambda _: self._changed_cb())
        self.setCellWidget(row, self.COL_CODEC, combo)

        # Débit
        bitrate_edit = QLineEdit(str(bitrate))
        bitrate_edit.setStyleSheet(_input_style())
        bitrate_edit.setFixedWidth(72)
        bitrate_edit.setEnabled(codec not in ("copy", "flac"))
        if self._changed_cb:
            bitrate_edit.textChanged.connect(lambda _: self._changed_cb())
        self.setCellWidget(row, self.COL_BITRATE, bitrate_edit)

        # Bouton suppression
        del_btn = QPushButton("✕")
        del_btn.setFixedSize(22, 22)
        del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        del_btn.setStyleSheet(f"""
            QPushButton{{background:transparent;color:{_C.ERROR};
                         border:1px solid {_C.ERROR};border-radius:4px;
                         font-size:10px;font-weight:700;padding:0;}}
            QPushButton:hover{{background:#2a1010;}}
            QPushButton:pressed{{background:#1a0808;}}
            QPushButton:disabled{{color:{_C.TEXT_DIM};border-color:{_C.TEXT_DIM};}}
        """)
        del_btn.clicked.connect(self._make_delete_handler(del_btn))
        del_w = QWidget()
        del_w.setStyleSheet("background:transparent;")
        dl = QHBoxLayout(del_w)
        dl.setContentsMargins(4, 0, 4, 0)
        dl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        dl.addWidget(del_btn)
        self.setCellWidget(row, self.COL_DEL, del_w)

        self._row_data.append({
            "combo":     combo,
            "bitrate":   bitrate_edit,
            "has_atmos": _has_atmos(track),
            "track":     track,
            "color":     color,
            "del_btn":   del_btn,
        })

    def _make_codec_handler(self, combo: QComboBox):
        def _handler(_idx: int = 0) -> None:
            for d in self._row_data:
                if d["combo"] is combo:
                    codec = combo.currentData()
                    d["bitrate"].setEnabled(codec not in ("copy", "flac"))
                    break
        return _handler

    def _make_delete_handler(self, del_btn: QPushButton):
        def _handler() -> None:
            for row, d in enumerate(self._row_data):
                if d["del_btn"] is del_btn:
                    self._delete_row(row)
                    break
        return _handler

    def _delete_row(self, row: int) -> None:
        if not self._can_delete(row):
            return
        self.removeRow(row)
        self._row_data.pop(row)
        self._refresh_delete_buttons()
        self._adjust_height()
        if self._changed_cb:
            self._changed_cb()

    def _can_delete(self, row: int) -> bool:
        track_idx = self._row_data[row]["track"].index
        return sum(1 for d in self._row_data if d["track"].index == track_idx) > 1

    def _refresh_delete_buttons(self) -> None:
        for row, d in enumerate(self._row_data):
            d["del_btn"].setEnabled(self._can_delete(row))


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
        self._workflow  = EncodeWorkflow(
            ffmpeg_bin=config.tool_ffmpeg,
            ram_buffer_enabled=config.ram_buffer_enabled,
            ram_buffer_threshold_pct=config.ram_buffer_threshold_pct,
            parent=self,
        )
        self._profiles  = ProfileManager(config.app_data_dir / "encode_profiles")
        self._executor  = ThreadPoolExecutor(max_workers=1)
        self._file_info: FileInfo | None = None
        self._video_tracks: list[tuple[FileInfo, TrackEntry, str]] = []
        self._audio_tracks_data: list[tuple] = []   # list[tuple[AudioTrack, str]] pour le popup
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
        self._audio_table = _AudioTable()
        self._audio_table.set_changed_callback(self._rebuild_preview)
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
            self._run_btn.setEnabled(False)
            self._video_list.blockSignals(False)
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
        file_info, _track, _color = self._video_tracks[row]
        self._apply_file_info(file_info)

    def _apply_file_info(self, info: FileInfo) -> None:
        """Applique les infos d'un FileInfo sélectionné comme source d'encodage."""
        self._file_info  = info
        self._duration_s = info.duration_s

        if info.primary_video:
            self._prefill_hdr_meta(info.primary_video.raw)

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

    def set_audio_tracks(self, tracks: list[tuple]) -> None:
        """Met à jour les pistes audio depuis les pistes activées dans l'onglet Conteneur."""
        self._audio_tracks_data = tracks
        self._add_audio_btn.setEnabled(bool(tracks))

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
        self._codec_combo.addItem("Copy — remux (sans conversion)", "copy")
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
            copy_subtitles=False,
            duration_s=self._duration_s,
            copy_dv=self._copy_dv_cb.isChecked(),
            copy_hdr10plus=self._copy_hdr10plus_cb.isChecked(),
            dovi_profile=self._dovi_profile_combo.currentData() or "0",
            work_dir=self._config.work_dir,
        )

    def _on_add_audio_track(self) -> None:
        """Ouvre le popup de sélection pour ajouter une piste audio custom."""
        if not self._audio_tracks_data:
            return
        dlg = _AudioSourceDialog(self._audio_tracks_data, parent=self)
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
        )
        self.log_message.emit(
            "INFO",
            f"Piste audio ajoutée : #{track.index} {track.codec.upper()} "
            f"{track.channels_label} → {dlg.selected_codec()}",
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
