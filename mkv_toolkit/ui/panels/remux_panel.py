"""
ui/panels/remux_panel.py — Panneau de remuxage MKV/MP4 sans réencodage.

Architecture :
    RemuxPanel (QWidget)
    ├── _FileZone            — sélection/drop du fichier source + résumé
    ├── _TrackTable          — tableau de pistes avec drag-drop et cases à cocher
    ├── Options              — conserver chapitres / pièces jointes
    ├── Section sortie       — chemin du fichier de sortie
    └── Aperçu commande      — QPlainTextEdit read-only, mis à jour en temps réel

Signaux exposés :
    RemuxPanel.log_message(level: str, message: str)
        → connecter à MainWindow.log_requested
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QDragEnterEvent, QDropEvent, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QFileDialog, QFrame,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
    QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from core.config import AppConfig
from core.inspector import FileInfo, FileInspector, InspectionError
from core.runner import TaskSignals
from core.workflows.remux import (
    RemuxConfig, RemuxError, RemuxWorkflow,
    TrackEntry, tracks_from_file_info,
)


def _fmt_eta(seconds: float) -> str:
    """Formate une durée en 'Xm Xs' ou 'Xs'. Retourne '—' si indéterminé."""
    if seconds <= 0 or seconds != seconds:
        return "—"
    s = int(seconds)
    m, s = divmod(s, 60)
    return f"{m}m {s:02d}s" if m else f"{s}s"


# =============================================================================
# Palette de couleurs (cohérente avec le thème sombre de l'application)
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

    # Couleurs de types de piste
    TRACK_VIDEO    = "#7ab3f5"   # bleu
    TRACK_AUDIO    = "#ce93d8"   # violet
    TRACK_SUBTITLE = "#5dcc8a"   # vert


# =============================================================================
# Helpers de style
# =============================================================================

def _section_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(f"""
        color: {_C.TEXT_DIM};
        font-size: 9px;
        font-weight: 700;
        letter-spacing: 2px;
        background: transparent;
    """)
    return lbl


def _card(parent: QWidget | None = None) -> QWidget:
    w = QWidget(parent)
    w.setStyleSheet(f"""
        QWidget {{
            background: {_C.BG_CARD};
            border: 1px solid {_C.BORDER};
            border-radius: 6px;
        }}
    """)
    return w


def _primary_button(text: str) -> QPushButton:
    btn = QPushButton(text)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setFixedHeight(36)
    btn.setStyleSheet(f"""
        QPushButton {{
            background: {_C.ACCENT};
            color: #ffffff;
            border: none;
            border-radius: 6px;
            font-size: 12px;
            font-weight: 700;
            padding: 0 20px;
        }}
        QPushButton:hover  {{ background: #6070f0; }}
        QPushButton:pressed {{ background: #3a52c0; }}
        QPushButton:disabled {{
            background: {_C.BG_ACTIVE};
            color: {_C.TEXT_DIM};
        }}
    """)
    return btn


def _secondary_button(text: str) -> QPushButton:
    btn = QPushButton(text)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setFixedHeight(28)
    btn.setStyleSheet(f"""
        QPushButton {{
            background: {_C.BG_CARD};
            color: {_C.TEXT_SEC};
            border: 1px solid {_C.BORDER};
            border-radius: 5px;
            font-size: 11px;
            font-weight: 500;
            padding: 0 12px;
        }}
        QPushButton:hover {{
            background: {_C.BG_HOVER};
            color: {_C.TEXT_PRI};
            border-color: {_C.BORDER_LT};
        }}
        QPushButton:pressed {{ background: {_C.BG_ACTIVE}; }}
    """)
    return btn


def _separator() -> QFrame:
    sep = QFrame()
    sep.setFrameShape(QFrame.Shape.HLine)
    sep.setFixedHeight(1)
    sep.setStyleSheet(f"background: {_C.BORDER}; border: none;")
    return sep


# =============================================================================
# Zone de dépôt de fichier source
# =============================================================================

class _FileZone(QFrame):
    """
    Zone de sélection et dépôt du fichier source.

    Signal :
        file_selected(path: str)
    """

    file_selected = Signal(str)

    _ACCEPTED = {".mkv", ".mp4", ".m4v", ".mov"}

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self._build_ui()

    def _build_ui(self) -> None:
        self.setStyleSheet(f"""
            QFrame {{
                background: {_C.BG_CARD};
                border: 1px dashed {_C.BORDER_LT};
                border-radius: 8px;
            }}
        """)
        self.setMinimumHeight(72)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(12)

        self._icon = QLabel("⊞")
        self._icon.setStyleSheet(f"""
            font-size: 24px;
            color: {_C.TEXT_DIM};
            background: transparent;
            border: none;
        """)
        layout.addWidget(self._icon)

        text_col = QVBoxLayout()
        text_col.setSpacing(3)

        self._main_lbl = QLabel("Déposer un fichier MKV / MP4 ici")
        self._main_lbl.setStyleSheet(f"""
            color: {_C.TEXT_SEC};
            font-size: 12px;
            font-weight: 500;
            background: transparent;
            border: none;
        """)
        self._info_lbl = QLabel("")
        self._info_lbl.setStyleSheet(f"""
            color: {_C.TEXT_DIM};
            font-size: 10px;
            font-family: 'JetBrains Mono', monospace;
            background: transparent;
            border: none;
        """)
        text_col.addWidget(self._main_lbl)
        text_col.addWidget(self._info_lbl)
        layout.addLayout(text_col, stretch=1)

        browse_btn = _secondary_button("Parcourir…")
        browse_btn.clicked.connect(self._browse)
        layout.addWidget(browse_btn)

    # ------------------------------------------------------------------
    # API publique
    # ------------------------------------------------------------------

    def set_file_info(self, info: FileInfo) -> None:
        """Met à jour l'affichage avec les informations du fichier chargé."""
        self._main_lbl.setText(info.path.name)
        self._main_lbl.setStyleSheet(f"""
            color: {_C.TEXT_PRI};
            font-size: 12px;
            font-weight: 600;
            background: transparent;
            border: none;
        """)
        parts = [info.size_human, info.duration_human, info.format]
        if info.primary_video:
            parts.append(info.primary_video.resolution)
        self._info_lbl.setText("   ".join(p for p in parts if p != "?"))
        self.setStyleSheet(f"""
            QFrame {{
                background: {_C.BG_CARD};
                border: 1px solid {_C.BORDER_LT};
                border-radius: 8px;
            }}
        """)

    def reset(self) -> None:
        self._main_lbl.setText("Déposer un fichier MKV / MP4 ici")
        self._main_lbl.setStyleSheet(f"""
            color: {_C.TEXT_SEC};
            font-size: 12px;
            font-weight: 500;
            background: transparent;
            border: none;
        """)
        self._info_lbl.setText("")
        self.setStyleSheet(f"""
            QFrame {{
                background: {_C.BG_CARD};
                border: 1px dashed {_C.BORDER_LT};
                border-radius: 8px;
            }}
        """)

    # ------------------------------------------------------------------
    # Drag & drop
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Parcourir
    # ------------------------------------------------------------------

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Sélectionner un fichier vidéo",
            "",
            "Fichiers vidéo (*.mkv *.mp4 *.m4v *.mov);;Tous les fichiers (*)",
        )
        if path:
            self.file_selected.emit(path)


# =============================================================================
# Tableau de pistes avec drag-drop de lignes
# =============================================================================

class _TrackTable(QTableWidget):
    """
    Tableau de pistes avec :
      - cases à cocher pour l'inclusion/exclusion
      - drag-and-drop pour réordonner les lignes
      - colonnes Langue et Titre éditables inline
      - signal order_changed émis après chaque réordonnancement

    Les données de chaque ligne sont stockées dans le UserRole de la
    colonne 0. La méthode current_tracks() synchronise l'état UI → TrackEntry
    avant de retourner la liste dans l'ordre courant.
    """

    order_changed = Signal()

    # Indices de colonnes
    COL_CHECK  = 0
    COL_TYPE   = 1
    COL_CODEC  = 2
    COL_INFO   = 3
    COL_LANG   = 4
    COL_TITLE  = 5

    _HEADERS = ["", "Type", "Codec", "Info", "Langue", "Titre"]

    # Flags des cellules non-éditables (les cellules de type/codec/info)
    _FLAG_RO = (
        Qt.ItemFlag.ItemIsEnabled
        | Qt.ItemFlag.ItemIsSelectable
        | Qt.ItemFlag.ItemIsDragEnabled
    )
    # Flags des cellules éditables (Langue, Titre)
    _FLAG_RW = _FLAG_RO | Qt.ItemFlag.ItemIsEditable

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(0, len(self._HEADERS), parent)
        self._setup_ui()

    def _setup_ui(self) -> None:
        self.setHorizontalHeaderLabels(self._HEADERS)

        # Drag-drop de lignes entières
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDragDropOverwriteMode(False)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)

        # Apparence
        self.setShowGrid(False)
        self.setAlternatingRowColors(True)
        self.verticalHeader().setVisible(False)
        self.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.SelectedClicked
        )

        # Redimensionnement des colonnes
        hh = self.horizontalHeader()
        hh.setSectionResizeMode(self.COL_CHECK, QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(self.COL_TYPE,  QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(self.COL_CODEC, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(self.COL_INFO,  QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(self.COL_LANG,  QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(self.COL_TITLE, QHeaderView.ResizeMode.Stretch)
        self.setColumnWidth(self.COL_CHECK, 32)
        self.setColumnWidth(self.COL_TYPE,  48)
        self.setColumnWidth(self.COL_LANG,  70)

        mono = QFont("JetBrains Mono", 10)
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self.setFont(mono)

        self.setStyleSheet(f"""
            QTableWidget {{
                background: {_C.BG_CARD};
                alternate-background-color: {_C.BG_DEEP};
                color: {_C.TEXT_PRI};
                border: 1px solid {_C.BORDER};
                border-radius: 6px;
                gridline-color: transparent;
            }}
            QTableWidget::item {{
                padding: 4px 6px;
                border: none;
            }}
            QTableWidget::item:selected {{
                background: {_C.ACCENT_DIM};
                color: {_C.TEXT_PRI};
            }}
            QHeaderView::section {{
                background: {_C.BG_PANEL};
                color: {_C.TEXT_DIM};
                font-size: 9px;
                font-weight: 700;
                letter-spacing: 1px;
                border: none;
                border-bottom: 1px solid {_C.BORDER};
                padding: 4px 6px;
            }}
            QScrollBar:vertical {{
                background: {_C.BG_DEEP};
                width: 6px;
                border: none;
            }}
            QScrollBar::handle:vertical {{
                background: {_C.BORDER_LT};
                border-radius: 3px;
                min-height: 20px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
        """)

    # ------------------------------------------------------------------
    # Remplissage
    # ------------------------------------------------------------------

    def load_tracks(self, tracks: list[TrackEntry]) -> None:
        """Charge la liste de pistes. Efface d'abord le contenu existant."""
        self.blockSignals(True)
        self.setRowCount(0)
        for entry in tracks:
            row = self.rowCount()
            self.insertRow(row)
            self._fill_row(row, entry)
        self.blockSignals(False)

    def _fill_row(self, row: int, entry: TrackEntry) -> None:
        """Remplit une ligne depuis un TrackEntry."""

        # Col 0 — case à cocher + stockage de l'entrée
        chk = QTableWidgetItem()
        chk.setData(Qt.ItemDataRole.UserRole, entry)
        chk.setFlags(
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsUserCheckable
            | Qt.ItemFlag.ItemIsDragEnabled
        )
        chk.setCheckState(
            Qt.CheckState.Checked if entry.enabled else Qt.CheckState.Unchecked
        )
        chk.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setItem(row, self.COL_CHECK, chk)

        # Col 1 — type (lettre colorée)
        type_item = QTableWidgetItem(entry.type_label)
        type_item.setFlags(self._FLAG_RO)
        type_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        match entry.track_type:
            case "video":    type_item.setForeground(QColor(_C.TRACK_VIDEO))
            case "audio":    type_item.setForeground(QColor(_C.TRACK_AUDIO))
            case "subtitle": type_item.setForeground(QColor(_C.TRACK_SUBTITLE))
        self.setItem(row, self.COL_TYPE, type_item)

        # Col 2 — codec
        codec_item = QTableWidgetItem(entry.codec)
        codec_item.setFlags(self._FLAG_RO)
        self.setItem(row, self.COL_CODEC, codec_item)

        # Col 3 — infos techniques
        info_item = QTableWidgetItem(entry.display_info)
        info_item.setFlags(self._FLAG_RO)
        info_item.setForeground(QColor(_C.TEXT_SEC))
        self.setItem(row, self.COL_INFO, info_item)

        # Col 4 — langue (éditable)
        lang_item = QTableWidgetItem(entry.language)
        lang_item.setFlags(self._FLAG_RW)
        self.setItem(row, self.COL_LANG, lang_item)

        # Col 5 — titre (éditable)
        title_item = QTableWidgetItem(entry.title)
        title_item.setFlags(self._FLAG_RW)
        self.setItem(row, self.COL_TITLE, title_item)

    # ------------------------------------------------------------------
    # Lecture de l'état courant
    # ------------------------------------------------------------------

    def current_tracks(self) -> list[TrackEntry]:
        """
        Retourne les TrackEntry dans l'ordre courant du tableau, en
        synchronisant l'état UI (enabled, language, title) vers les objets.
        """
        tracks: list[TrackEntry] = []
        for row in range(self.rowCount()):
            item0 = self.item(row, self.COL_CHECK)
            if item0 is None:
                continue
            entry: TrackEntry = item0.data(Qt.ItemDataRole.UserRole)
            if entry is None:
                continue

            entry.enabled = item0.checkState() == Qt.CheckState.Checked

            lang_item = self.item(row, self.COL_LANG)
            if lang_item:
                entry.language = lang_item.text().strip()

            title_item = self.item(row, self.COL_TITLE)
            if title_item:
                entry.title = title_item.text().strip()

            tracks.append(entry)
        return tracks

    def set_all_enabled(self, enabled: bool) -> None:
        """Active ou désactive toutes les pistes d'un coup.

        Le recalcul de l'aperçu est à la charge de l'appelant.
        """
        self.blockSignals(True)
        state = Qt.CheckState.Checked if enabled else Qt.CheckState.Unchecked
        for row in range(self.rowCount()):
            item = self.item(row, self.COL_CHECK)
            if item:
                item.setCheckState(state)
        self.blockSignals(False)

    # ------------------------------------------------------------------
    # Drag-drop : déplacement de lignes entières
    # ------------------------------------------------------------------

    def dropEvent(self, event: QDropEvent) -> None:
        if event.source() is not self:
            event.ignore()
            return

        # Lecture des lignes sélectionnées avant modification
        src_rows = sorted(set(idx.row() for idx in self.selectedIndexes()))
        if not src_rows:
            event.ignore()
            return

        # Synchronise l'état UI → TrackEntry avant extraction
        all_entries = self.current_tracks()

        # Ligne cible
        drop_row = self._drop_target_row(event)

        # Réordonne la liste en mémoire
        moving = [all_entries[r] for r in src_rows]
        remaining = [e for i, e in enumerate(all_entries) if i not in src_rows]

        # Ajuste drop_row après suppression des lignes sources
        adjusted = drop_row
        for r in src_rows:
            if r < drop_row:
                adjusted -= 1
        adjusted = max(0, min(adjusted, len(remaining)))

        for i, entry in enumerate(moving):
            remaining.insert(adjusted + i, entry)

        # Reconstruit le tableau
        self.blockSignals(True)
        self.setRowCount(0)
        for entry in remaining:
            row = self.rowCount()
            self.insertRow(row)
            self._fill_row(row, entry)
        self.blockSignals(False)

        # Sélectionne les lignes déplacées
        self.selectRow(adjusted)
        event.accept()
        self.order_changed.emit()

    def _drop_target_row(self, event: QDropEvent) -> int:
        """Calcule l'indice d'insertion cible."""
        index = self.indexAt(event.position().toPoint())
        if not index.isValid():
            return self.rowCount()
        row = index.row()
        rect = self.visualRect(index)
        if event.position().toPoint().y() > rect.top() + rect.height() // 2:
            return row + 1
        return row


# =============================================================================
# Panneau principal
# =============================================================================

class RemuxPanel(QWidget):
    """
    Panneau de remuxage MKV/MP4.

    Signaux :
        log_message(level: str, message: str)
    """

    log_message = Signal(str, str)
    _inspection_result = Signal(object)   # FileInfo — signal interne thread-safe
    _inspection_error  = Signal(str)      # message d'erreur — signal interne thread-safe

    file_info_changed    = Signal(object)   # FileInfo — émis après chaque inspection réussie
    audio_tracks_changed = Signal(object)   # list[AudioTrack] — pistes audio activées dans le tableau

    def __init__(self, config: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config    = config
        self._workflow  = RemuxWorkflow(mkvmerge_bin=config.tool_mkvmerge)
        self._executor  = ThreadPoolExecutor(max_workers=1)
        self._file_info: FileInfo | None = None
        self._running   = False
        self._signals: TaskSignals | None = None

        self._workflow.log_message.connect(
            self.log_message, Qt.ConnectionType.QueuedConnection
        )
        self._inspection_result.connect(
            self._apply_inspection, Qt.ConnectionType.QueuedConnection
        )
        self._inspection_error.connect(
            self._on_inspection_error, Qt.ConnectionType.QueuedConnection
        )

        self._build_ui()

    # ------------------------------------------------------------------
    # Construction de l'interface
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self.setStyleSheet(f"background: {_C.BG_DEEP};")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Zone de défilement
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet(f"""
            QScrollArea {{ background: {_C.BG_DEEP}; border: none; }}
            QScrollBar:vertical {{
                background: {_C.BG_DEEP};
                width: 6px;
                border: none;
            }}
            QScrollBar::handle:vertical {{
                background: {_C.BORDER_LT};
                border-radius: 3px;
                min-height: 24px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
        """)

        content = QWidget()
        content.setStyleSheet(f"background: {_C.BG_DEEP};")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(28, 24, 28, 24)
        content_layout.setSpacing(20)

        # --- Titre de page ---
        title = QLabel("Manipulation Conteneur")
        title.setStyleSheet(f"""
            font-size: 20px;
            font-weight: 800;
            color: {_C.TEXT_PRI};
            background: transparent;
            letter-spacing: -0.3px;
        """)
        subtitle = QLabel("Remuxage, sélection et réordonnancement de pistes — sans réencodage")
        subtitle.setStyleSheet(f"color: {_C.TEXT_SEC}; font-size: 12px; background: transparent;")
        content_layout.addWidget(title)
        content_layout.addWidget(subtitle)
        content_layout.addWidget(_separator())

        # --- Fichier source ---
        content_layout.addWidget(_section_label("FICHIER SOURCE"))
        self._file_zone = _FileZone()
        self._file_zone.file_selected.connect(self._on_file_selected)
        content_layout.addWidget(self._file_zone)

        content_layout.addWidget(_separator())

        # --- Pistes ---
        track_header = QHBoxLayout()
        track_header.setSpacing(8)
        track_header.addWidget(_section_label("PISTES"))
        track_header.addStretch()

        btn_all = _secondary_button("Tout activer")
        btn_none = _secondary_button("Tout désactiver")
        btn_all.clicked.connect(lambda: self._set_all_tracks(True))
        btn_none.clicked.connect(lambda: self._set_all_tracks(False))
        track_header.addWidget(btn_all)
        track_header.addWidget(btn_none)

        content_layout.addLayout(track_header)

        hint = QLabel("Glisser-déposer les lignes pour réordonner · Double-clic pour éditer Langue / Titre")
        hint.setStyleSheet(f"color: {_C.TEXT_DIM}; font-size: 10px; background: transparent;")
        content_layout.addWidget(hint)

        self._track_table = _TrackTable()
        self._track_table.setMinimumHeight(180)
        self._track_table.itemChanged.connect(self._on_table_changed)
        self._track_table.order_changed.connect(self._rebuild_preview)
        content_layout.addWidget(self._track_table)

        content_layout.addWidget(_separator())

        # --- Options ---
        content_layout.addWidget(_section_label("OPTIONS"))
        opts_card = _card()
        opts_layout = QVBoxLayout(opts_card)
        opts_layout.setContentsMargins(16, 12, 16, 12)
        opts_layout.setSpacing(8)

        self._chapters_cb = QCheckBox("Conserver les chapitres")
        self._chapters_cb.setChecked(True)
        self._chapters_cb.setStyleSheet(self._checkbox_style())
        self._chapters_cb.stateChanged.connect(self._rebuild_preview)

        self._attach_cb = QCheckBox("Conserver les pièces jointes (cover.jpg, …)")
        self._attach_cb.setChecked(True)
        self._attach_cb.setStyleSheet(self._checkbox_style())
        self._attach_cb.stateChanged.connect(self._rebuild_preview)

        opts_layout.addWidget(self._chapters_cb)
        opts_layout.addWidget(self._attach_cb)
        content_layout.addWidget(opts_card)

        content_layout.addWidget(_separator())

        # --- Sortie ---
        content_layout.addWidget(_section_label("FICHIER DE SORTIE"))
        out_row = QHBoxLayout()
        out_row.setSpacing(8)

        self._output_edit = QLineEdit()
        self._output_edit.setPlaceholderText("/chemin/vers/sortie.mkv")
        self._output_edit.setStyleSheet(self._input_style())
        self._output_edit.textChanged.connect(self._rebuild_preview)
        out_row.addWidget(self._output_edit, stretch=1)

        browse_out = _secondary_button("Choisir…")
        browse_out.clicked.connect(self._browse_output)
        out_row.addWidget(browse_out)

        content_layout.addLayout(out_row)
        content_layout.addWidget(_separator())

        # --- Aperçu de la commande ---
        cmd_header = QHBoxLayout()
        cmd_header.addWidget(_section_label("APERÇU COMMANDE"))
        cmd_header.addStretch()
        copy_btn = _secondary_button("Copier")
        copy_btn.clicked.connect(self._copy_command)
        cmd_header.addWidget(copy_btn)
        content_layout.addLayout(cmd_header)

        self._cmd_preview = QPlainTextEdit()
        self._cmd_preview.setReadOnly(True)
        self._cmd_preview.setFixedHeight(120)
        mono = QFont("JetBrains Mono", 9)
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self._cmd_preview.setFont(mono)
        self._cmd_preview.setStyleSheet(f"""
            QPlainTextEdit {{
                background: {_C.BG_DEEP};
                color: {_C.TEXT_SEC};
                border: 1px solid {_C.BORDER};
                border-radius: 6px;
                padding: 8px 12px;
            }}
        """)
        self._cmd_preview.setPlaceholderText(
            "Sélectionnez un fichier source et définissez le chemin de sortie…"
        )
        content_layout.addWidget(self._cmd_preview)

        content_layout.addStretch()
        scroll.setWidget(content)
        root.addWidget(scroll, stretch=1)

        # --- Bouton Lancer ---
        btn_bar = QWidget()
        btn_bar.setStyleSheet(f"""
            QWidget {{
                background: {_C.BG_PANEL};
                border-top: 1px solid {_C.BORDER};
            }}
        """)
        btn_bar_layout = QHBoxLayout(btn_bar)
        btn_bar_layout.setContentsMargins(28, 12, 28, 12)
        btn_bar_layout.setSpacing(12)

        # Conteneur vertical : barre fine + légende (pct · ETA)
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
        self._progress_bar.setStyleSheet(f"""
            QProgressBar {{
                background: {_C.BG_ACTIVE};
                border: none;
                border-radius: 3px;
            }}
            QProgressBar::chunk {{
                background: {_C.ACCENT};
                border-radius: 3px;
            }}
        """)
        _pvl.addWidget(self._progress_bar)

        self._progress_lbl = QLabel("")
        self._progress_lbl.setStyleSheet(
            f"color:{_C.TEXT_DIM};font-size:10px;"
            f"font-family:'JetBrains Mono',monospace;background:transparent;"
        )
        _pvl.addWidget(self._progress_lbl)

        self._progress_widget.setVisible(False)
        btn_bar_layout.addWidget(self._progress_widget, stretch=1)

        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet(f"""
            color: {_C.TEXT_SEC};
            font-size: 11px;
            background: transparent;
        """)
        btn_bar_layout.addWidget(self._status_lbl)
        btn_bar_layout.addSpacing(4)

        self._run_btn = _primary_button("▶  Lancer le remuxage")
        self._run_btn.setFixedWidth(200)
        self._run_btn.setEnabled(False)
        self._run_btn.clicked.connect(self._on_run)
        btn_bar_layout.addWidget(self._run_btn)

        self._cancel_btn = QPushButton("✕  Annuler")
        self._cancel_btn.setFixedWidth(110)
        self._cancel_btn.setFixedHeight(36)
        self._cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._cancel_btn.setStyleSheet(f"""
            QPushButton {{
                background: {_C.BG_CARD}; color: {_C.WARN};
                border: 1px solid {_C.WARN}; border-radius: 6px;
                font-size: 12px; font-weight: 600; padding: 0 14px;
            }}
            QPushButton:hover {{
                background: #2a2010; border-color: #f0b030; color: #f0b030;
            }}
            QPushButton:pressed {{ background: #1a1608; }}
        """)
        self._cancel_btn.setVisible(False)
        self._cancel_btn.clicked.connect(self._on_cancel)
        btn_bar_layout.addWidget(self._cancel_btn)

        root.addWidget(btn_bar)

    # ------------------------------------------------------------------
    # Chargement du fichier source
    # ------------------------------------------------------------------

    def _on_file_selected(self, path_str: str) -> None:
        path = Path(path_str)
        self.log_message.emit("INFO", f"Inspection de {path.name}…")
        self._set_status("Inspection en cours…")
        self._run_btn.setEnabled(False)
        self._executor.submit(self._inspect_file, path)

    def _inspect_file(self, path: Path) -> None:
        """Exécuté dans le thread executor. Ne touche jamais l'UI directement."""
        try:
            inspector = FileInspector(
                ffprobe_bin=self._config.tool_ffprobe,
                mediainfo_bin=self._config.tool_mediainfo,
            )
            info = inspector.inspect(path)
            # Repasse dans le thread Qt via signal (thread-safe)
            self._inspection_result.emit(info)
        except InspectionError as exc:
            self.log_message.emit("ERROR", str(exc))
            self._inspection_error.emit("Erreur d'inspection.")
        except Exception as exc:
            self.log_message.emit("ERROR", f"Erreur inattendue lors de l'inspection : {exc}")
            self._inspection_error.emit("Erreur d'inspection.")

    def _apply_inspection(self, info: FileInfo) -> None:
        """Applique le résultat d'inspection — s'exécute dans le thread Qt."""
        self._file_info = info
        self._file_zone.set_file_info(info)

        tracks = tracks_from_file_info(info)
        self._track_table.load_tracks(tracks)

        # Chemin de sortie par défaut
        default_out = self._config.output_dir / f"{info.path.stem}_remux.mkv"
        self._output_edit.setText(str(default_out))

        self._run_btn.setEnabled(True)
        self._set_status("")
        self.log_message.emit(
            "OK",
            f"{info.path.name} chargé — "
            f"{len(info.video_tracks)}V  {len(info.audio_tracks)}A  "
            f"{len(info.subtitle_tracks)}S",
        )
        self._rebuild_preview()
        self.file_info_changed.emit(info)
        self._emit_audio_tracks()

    # ------------------------------------------------------------------
    # Aperçu de la commande
    # ------------------------------------------------------------------

    def _emit_audio_tracks(self) -> None:
        """Émet audio_tracks_changed avec les pistes audio actuellement activées."""
        if self._file_info is None:
            return
        track_entries = self._track_table.current_tracks()
        enabled_tids = {
            t.mkv_tid for t in track_entries
            if t.track_type == "audio" and t.enabled
        }
        audio = [a for a in self._file_info.audio_tracks if a.index in enabled_tids]
        self.audio_tracks_changed.emit(audio)

    def _rebuild_preview(self) -> None:
        config = self._current_config()
        if config is None:
            self._cmd_preview.setPlainText("")
            return
        try:
            text = self._workflow.preview_command(config)
            self._cmd_preview.setPlainText(text)
        except Exception:
            self._cmd_preview.setPlainText("(erreur de construction de la commande)")

    def _on_table_changed(self, _item: QTableWidgetItem | None = None) -> None:
        self._rebuild_preview()
        self._emit_audio_tracks()

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
        self._set_status("Remuxage en cours…")
        self.log_message.emit("INFO", f"Démarrage → {config.output.name}")

        try:
            signals = self._workflow.run(config)
        except RemuxError as exc:
            self.log_message.emit("ERROR", str(exc))
            self._on_run_finished(success=False)
            return

        self._signals = signals
        signals.progress.connect(
            self._on_progress,
            Qt.ConnectionType.QueuedConnection,
        )
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

    def _on_cancel(self) -> None:
        reply = QMessageBox.question(
            self,
            "Confirmer l'annulation",
            "Annuler le remuxage en cours ?",
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
        self.log_message.emit("WARN", "Remuxage annulé.")

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
            self.log_message.emit("OK", f"Remuxage terminé → {out}")
        else:
            self._progress_widget.setVisible(False)
            self._progress_lbl.setText("")
            self._set_status("Échec.")
            if error:
                self.log_message.emit("ERROR", error)

    def _on_progress(self, line: str) -> None:
        """Parse les lignes stdout de mkvmerge et met à jour la barre + légende."""
        if "Progress:" in line:
            try:
                pct = int(line.split("%")[0].split()[-1])
                self._progress_bar.setValue(pct)

                elapsed_wall = time.monotonic() - self._op_start
                if pct > 0 and elapsed_wall > 0:
                    eta_s = elapsed_wall * (100 - pct) / pct
                    eta_str = f"ETA {_fmt_eta(eta_s)}"
                else:
                    eta_str = ""

                parts = [f"{pct}%", eta_str]
                self._progress_lbl.setText("  ·  ".join(p for p in parts if p))
            except (ValueError, IndexError):
                pass
        else:
            self.log_message.emit("INFO", line)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _current_config(self) -> RemuxConfig | None:
        """Construit un RemuxConfig depuis l'état courant de l'interface."""
        if self._file_info is None:
            return None
        output_str = self._output_edit.text().strip()
        if not output_str:
            return None
        tracks = self._track_table.current_tracks()
        if not tracks:
            return None
        return RemuxConfig(
            source=self._file_info.path,
            output=Path(output_str),
            tracks=tracks,
            keep_chapters=self._chapters_cb.isChecked(),
            keep_attachments=self._attach_cb.isChecked(),
            work_dir=self._config.work_dir,
        )

    def _set_all_tracks(self, enabled: bool) -> None:
        self._track_table.set_all_enabled(enabled)
        self._rebuild_preview()

    def _browse_output(self) -> None:
        default = self._output_edit.text() or str(self._config.output_dir)
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Fichier de sortie",
            default,
            "Matroska (*.mkv);;Tous les fichiers (*)",
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

    def _on_inspection_error(self, message: str) -> None:
        """Reçu dans le thread Qt depuis le signal _inspection_error."""
        self._set_status(message)

    # ------------------------------------------------------------------
    # Styles réutilisables
    # ------------------------------------------------------------------

    @staticmethod
    def _checkbox_style() -> str:
        return f"""
            QCheckBox {{
                color: {_C.TEXT_SEC};
                font-size: 12px;
                spacing: 8px;
                background: transparent;
            }}
            QCheckBox::indicator {{
                width: 14px;
                height: 14px;
                border-radius: 3px;
                border: 1px solid {_C.BORDER_LT};
                background: {_C.BG_DEEP};
            }}
            QCheckBox::indicator:checked {{
                background: {_C.ACCENT};
                border-color: {_C.ACCENT};
            }}
            QCheckBox:hover {{ color: {_C.TEXT_PRI}; }}
        """

    @staticmethod
    def _input_style() -> str:
        return f"""
            QLineEdit {{
                background: {_C.BG_CARD};
                color: {_C.TEXT_PRI};
                border: 1px solid {_C.BORDER};
                border-radius: 5px;
                font-size: 12px;
                font-family: 'JetBrains Mono', monospace;
                padding: 6px 10px;
            }}
            QLineEdit:focus {{
                border-color: {_C.ACCENT};
            }}
            QLineEdit::placeholder {{
                color: {_C.TEXT_DIM};
            }}
        """

    # ------------------------------------------------------------------
    # Nettoyage
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._executor.shutdown(wait=False)
        super().closeEvent(event)
