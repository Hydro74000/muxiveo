"""
ui/panels/encode_panel/widgets.py — Reusable sub-widgets for the encode panel.

Public:
    _has_atmos       — detects Atmos layer in TrueHD track
    _FileZone        — drag-drop file source selector
    _AudioSourceDialog — popup for adding custom audio track
    _AudioTable      — editable audio tracks table
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QFileDialog,
    QFrame, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QListWidget, QListWidgetItem,
    QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from core.inspector import AudioTrack, FileInfo
from core.workflows.encode.models import AUDIO_CODECS, AudioTrackSettings
from ui.panels.encode_panel.theme import (
    _C, _combo_style, _input_style, _primary_button, _secondary_button, _separator,
)


# =============================================================================
# Helpers
# =============================================================================

def _has_atmos(track: AudioTrack) -> bool:
    """True si la piste est TrueHD avec couche Atmos (utilisé pour extract_truehd_core)."""
    return track.codec.lower() == "truehd" and track.atmos_flag


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

    tracks : list[tuple[AudioTrack, str]] ou list[tuple[AudioTrack, str, Path]]
    """

    def __init__(
        self,
        tracks: list[tuple],   # (track, color) ou (track, color, source_path)
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._tracks = tracks
        self._result_track:       AudioTrack | None = None
        self._result_color:       str = "#ffffff"
        self._result_source_path = None   # Path | None
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
        for entry in self._tracks:
            track, color = entry[0], entry[1]
            source_path = entry[2] if len(entry) > 2 else None
            ch = track.channels_label
            lang = track.language or "—"
            title_part = f"  {track.title}" if track.title else ""
            if track.atmos_flag:
                fmt_tag = "  Atmos"
            elif track.dtsx_flag:
                fmt_tag = "  DTS:X"
            else:
                fmt_tag = ""
            text = f"█  #{track.index}  {track.codec.upper()} {ch}{fmt_tag}  [{lang}]{title_part}"
            item = QListWidgetItem(text)
            item.setForeground(QBrush(QColor(color)))
            item.setData(Qt.ItemDataRole.UserRole, (track, color, source_path))
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
        data = item.data(Qt.ItemDataRole.UserRole)
        self._result_track       = data[0]
        self._result_color       = data[1]
        self._result_source_path = data[2] if len(data) > 2 else None
        self.accept()

    def selected_track(self) -> AudioTrack | None:
        return self._result_track

    def selected_color(self) -> str:
        return self._result_color

    def selected_source_path(self):   # -> Path | None
        return self._result_source_path

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

    Colonnes : src  |  #  |  Format  |  Nom  |  Lang  |  Encodage  |  Débit  |  Del
    """

    # Émis quand l'utilisateur modifie lang ou titre : (stream_index, source_path, lang, title)
    track_meta_changed = Signal(int, object, str, str)

    COL_SOURCE  = 0
    COL_IDX     = 1
    COL_FORMAT  = 2
    COL_TITLE   = 3
    COL_LANG    = 4
    COL_CODEC   = 5
    COL_BITRATE = 6
    COL_DEL     = 7
    HEADERS = ["", "#", "Format", "Nom", "Lang", "Encodage", "Débit", ""]

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(0, len(self.HEADERS), parent)
        self._row_data: list[dict] = []   # {combo, bitrate, has_atmos, track, color, source_path, del_btn}
        self._changed_cb = None
        self._setup_table()
        self.itemChanged.connect(self._on_item_changed)

    def set_changed_callback(self, cb) -> None:
        self._changed_cb = cb

    def _setup_table(self) -> None:
        self.setHorizontalHeaderLabels(self.HEADERS)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked)
        self.verticalHeader().setVisible(False)
        self.setShowGrid(False)
        self.setAlternatingRowColors(False)
        hh = self.horizontalHeader()
        hh.setSectionResizeMode(self.COL_SOURCE,  QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(self.COL_IDX,     QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(self.COL_FORMAT,  QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(self.COL_TITLE,   QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(self.COL_LANG,    QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(self.COL_CODEC,   QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(self.COL_BITRATE, QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(self.COL_DEL,     QHeaderView.ResizeMode.Fixed)
        self.setColumnWidth(self.COL_SOURCE,  20)
        self.setColumnWidth(self.COL_IDX,     32)
        self.setColumnWidth(self.COL_FORMAT, 130)
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
        tracks: list[tuple],   # list[tuple[AudioTrack, str]] ou [AudioTrack, str, Path]
        default_codec: str = "copy",
        default_bitrate: int = 384,
    ) -> None:
        self._row_data = []
        self.setRowCount(0)
        for entry in tracks:
            track, color = entry[0], entry[1]
            source_path = entry[2] if len(entry) > 2 else None
            self._append_row(track, color, default_codec, default_bitrate, source_path)
        self._refresh_delete_buttons()
        self._adjust_height()

    def add_custom_row(
        self, track: AudioTrack, color: str, codec: str = "copy", bitrate: int = 384,
        source_path=None,   # Path | None
    ) -> None:
        self._append_row(track, color, codec, bitrate, source_path)
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
                source_path=d.get("source_path"),
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
        self, track: AudioTrack, color: str, codec: str, bitrate: int,
        source_path=None,   # Path | None
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

        fmt_parts = [f"{track.codec.upper()} {track.channels_label}"]
        if track.atmos_flag:
            fmt_parts.append("Atmos")
        elif track.dtsx_flag:
            fmt_parts.append("DTS:X")

        def _item_rw(text: str) -> QTableWidgetItem:
            it = QTableWidgetItem(text)
            it.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEditable)
            return it

        self.setItem(row, self.COL_IDX,    _item(str(track.index)))
        self.setItem(row, self.COL_FORMAT, _item("  ".join(fmt_parts)))
        self.setItem(row, self.COL_TITLE,  _item_rw(track.title or ""))
        self.setItem(row, self.COL_LANG,   _item_rw(track.language or ""))

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
            "combo":       combo,
            "bitrate":     bitrate_edit,
            "has_atmos":   _has_atmos(track),
            "track":       track,
            "color":       color,
            "source_path": source_path,
            "del_btn":     del_btn,
        })

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        row = item.row()
        col = item.column()
        if col not in (self.COL_TITLE, self.COL_LANG):
            return
        if row >= len(self._row_data):
            return
        d = self._row_data[row]
        lang_item  = self.item(row, self.COL_LANG)
        title_item = self.item(row, self.COL_TITLE)
        lang  = lang_item.text()  if lang_item  else ""
        title = title_item.text() if title_item else ""
        self.track_meta_changed.emit(d["track"].index, d.get("source_path"), lang, title)
        if self._changed_cb:
            self._changed_cb()

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
