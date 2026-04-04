"""
ui/panels/remux_panel.py — Panneau de remuxage MKV/MP4 sans réencodage.

Architecture :
    RemuxPanel (QWidget)
    ├── _FileListWidget      — liste de fichiers sources (multi-fichiers, drag-drop)
    │   └── _FileRow         — ligne par fichier : nom, infos, bouton retrait
    ├── _TrackTable          — tableau de pistes multi-sources avec drag-drop et cases à cocher
    ├── Options              — conserver chapitres / pièces jointes
    ├── Section sortie       — chemin du fichier de sortie
    └── Aperçu commande      — QPlainTextEdit read-only, mis à jour en temps réel

Modèle de données :
    SourceFile (dataclass) — représente un fichier source chargé avec ses infos et pistes

Signaux exposés :
    RemuxPanel.log_message(level: str, message: str)
        → connecter à MainWindow.log_requested
    RemuxPanel.video_tracks_changed(list[tuple[FileInfo, TrackEntry, str]])
        → émet toutes les pistes vidéo activées avec leur FileInfo parent et couleur
    RemuxPanel.audio_tracks_changed(list[tuple[AudioTrack, str, Path]])
        → émet toutes les pistes audio activées avec couleur et chemin source
"""

from __future__ import annotations

import colorsys
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace as dc_replace
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QDragEnterEvent, QDropEvent, QFont, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox,
    QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFrame,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMessageBox, QPlainTextEdit, QPushButton, QScrollArea,
    QSizePolicy, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from core.config import AppConfig
from core.inspector import (
    STANDARD_MKV_TAGS,
    AttachmentInfo, AudioTrack, ChapterEntry, FileInfo, FileInspector, InspectionError,
    fmt_timecode_display,
)
from core.lang_tags import Rfc5646LanguageTags
from core.runner import TaskSignals
from core.workflows.remux import (
    RemuxConfig, RemuxWorkflow, SourceInput,
    TrackEntry, tracks_from_file_info,
)
from ui.panels.track_edit_dialog import TrackEditDialog


# Hauteurs fixes pour les éléments de la liste de fichiers
_FILE_ROW_H = 52   # hauteur d'une _FileRow
_FILE_BAR_H = 36   # barre "Ajouter des fichiers"
_FILE_PH_H  = 100  # hauteur du placeholder (sans fichiers)


def _pick_file_color(index: int) -> str:
    """
    Génère une couleur HSL à séparation maximale via l'angle doré (~137.5°).

    Paramètres fixes : saturation 70 %, luminosité 62 % — lisibles sur fond sombre,
    ni trop clair (≠ blanc) ni trop sombre (≠ noir).
    """
    hue = (index * 137.508) % 360
    r, g, b = colorsys.hls_to_rgb(hue / 360, 0.62, 0.70)
    return f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"


# ---------------------------------------------------------------------------
# Helpers timecode (chapitres)
# ---------------------------------------------------------------------------

_TC_RE = re.compile(r"^(\d+):(\d{2}):(\d{2})(?:[.,](\d+))?$")


def _parse_timecode(tc: str) -> float | None:
    """
    Parse HH:MM:SS ou HH:MM:SS.mmm (millisecondes ou plus acceptés) → secondes.
    Retourne None si le format est invalide.
    """
    m = _TC_RE.match(tc.strip())
    if not m:
        return None
    h  = int(m.group(1))
    mn = int(m.group(2))
    sc = int(m.group(3))
    frac_str = m.group(4) or ""
    frac = float("0." + frac_str) if frac_str else 0.0
    return h * 3600 + mn * 60 + sc + frac


def _format_timecode(seconds: float) -> str:
    """Formate un nombre de secondes en HH:MM:SS.mmm (délègue à core.inspector)."""
    return fmt_timecode_display(seconds)


# =============================================================================
# Modèle de données
# =============================================================================

@dataclass
class SourceFile:
    """
    Représente un fichier source chargé dans le panneau Conteneur.

    id     : UUID unique — permet d'identifier le fichier même après suppression
             d'autres fichiers de la liste (les TrackEntry.file_id pointent vers lui).
    path   : chemin absolu du fichier.
    color  : couleur hex assignée à ce fichier (indicateur visuel).
    info   : résultat de l'inspection (None pendant et si erreur).
    tracks : liste complète des pistes du fichier (remplie après inspection).
    """
    id:     str
    path:   Path
    color:  str = ""
    info:   FileInfo | None = None
    tracks: list[TrackEntry] = field(default_factory=list)



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
    TRACK_VIDEO       = "#7ab3f5"   # bleu
    TRACK_AUDIO       = "#ce93d8"   # violet
    TRACK_SUBTITLE    = "#5dcc8a"   # vert
    TRACK_ATTACHMENT  = "#f5c842"   # jaune ambre
    TRACK_TAGS        = "#f5a030"   # orange


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


def _pencil_icon(color: str = _C.TEXT_SEC, size: int = 14) -> QIcon:
    """
    Icône crayon rendue depuis un SVG inline via QSvgRenderer.

    Utilise le tracé Feather Icons (pencil) — un contour simple, sans remplissage,
    adapté à un thème sombre.
    """
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"'
        f' fill="none" stroke="{color}" stroke-width="2.2"'
        ' stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"/>'
        '</svg>'
    )
    renderer = QSvgRenderer(svg.encode())
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    renderer.render(painter)
    painter.end()
    return QIcon(pix)


# =============================================================================
# Ligne de fichier source (_FileRow)
# =============================================================================

class _FileRow(QWidget):
    """
    Ligne représentant un fichier source dans _FileListWidget.

    Affiche : icône · nom du fichier · infos (après inspection) · bouton retrait.
    """

    remove_clicked = Signal(str)   # file_id

    def __init__(self, file_id: str, path: Path, color: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._file_id = file_id
        self.setFixedHeight(_FILE_ROW_H)
        self._build_ui(path, color)

    def _build_ui(self, path: Path, color: str) -> None:
        self.setStyleSheet(f"""
            _FileRow {{
                background: {_C.BG_CARD};
                border-bottom: 1px solid {_C.BORDER};
            }}
        """)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 8, 8)
        lay.setSpacing(10)

        color_square = QLabel()
        color_square.setFixedSize(12, 12)
        color_square.setStyleSheet(
            f"background: {color}; border-radius: 3px; border: none;"
        )
        lay.addWidget(color_square)

        text_col = QVBoxLayout()
        text_col.setSpacing(2)

        self._name_lbl = QLabel(path.name)
        self._name_lbl.setStyleSheet(f"""
            color: {_C.TEXT_PRI};
            font-size: 12px;
            font-weight: 600;
            background: transparent;
            border: none;
        """)

        self._info_lbl = QLabel("Inspection en cours…")
        self._info_lbl.setStyleSheet(f"""
            color: {_C.TEXT_DIM};
            font-size: 10px;
            font-family: 'JetBrains Mono', monospace;
            background: transparent;
            border: none;
        """)

        text_col.addWidget(self._name_lbl)
        text_col.addWidget(self._info_lbl)
        lay.addLayout(text_col, stretch=1)

        remove_btn = QPushButton("✕")
        remove_btn.setFixedSize(22, 22)
        remove_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        remove_btn.setToolTip("Retirer ce fichier")
        remove_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {_C.TEXT_DIM};
                border: 1px solid {_C.BORDER};
                border-radius: 4px;
                font-size: 10px;
                font-weight: 700;
            }}
            QPushButton:hover {{
                color: {_C.ERROR};
                border-color: {_C.ERROR};
                background: #1f0e0e;
            }}
            QPushButton:pressed {{ background: #2a0f0f; }}
        """)
        remove_btn.clicked.connect(lambda: self.remove_clicked.emit(self._file_id))
        lay.addWidget(remove_btn)

    def set_info(self, info: FileInfo) -> None:
        """Met à jour la ligne d'informations après inspection réussie."""
        parts = [info.size_human, info.duration_human, info.format]
        if info.primary_video:
            parts.append(info.primary_video.resolution)
            if info.primary_video.hdr_type.label() != "SDR":
                parts.append(info.primary_video.hdr_type.label())
        self._info_lbl.setText("   ·   ".join(p for p in parts if p and p != "?"))
        self._info_lbl.setStyleSheet(f"""
            color: {_C.TEXT_SEC};
            font-size: 10px;
            font-family: 'JetBrains Mono', monospace;
            background: transparent;
            border: none;
        """)

    def set_error(self, message: str) -> None:
        """Affiche une erreur d'inspection."""
        self._info_lbl.setText(f"Erreur : {message}")
        self._info_lbl.setStyleSheet(f"""
            color: {_C.ERROR};
            font-size: 10px;
            background: transparent;
            border: none;
        """)


# =============================================================================
# Zone de liste de fichiers sources (_FileListWidget)
# =============================================================================

_ACCEPTED_EXT = {".mkv", ".mp4", ".m4v", ".mov"}


class _FileListWidget(QFrame):
    """
    Widget de sélection multi-fichiers.

    Permet d'ajouter des fichiers :
      - par bouton "Ajouter des fichiers…" (dialogue multi-sélection)
      - par glisser-déposer (un ou plusieurs fichiers simultanément)

    Permet de retirer chaque fichier individuellement via le bouton ✕.

    Signaux :
        add_requested(list[str])     — chemins des fichiers à charger
        remove_requested(str)        — file_id du fichier à retirer
    """

    add_requested    = Signal(list)   # list[str]
    remove_requested = Signal(str)    # file_id

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self._rows: dict[str, _FileRow] = {}   # file_id → _FileRow
        self._build_ui()

    def _build_ui(self) -> None:
        self.setStyleSheet(f"""
            QFrame {{
                background: {_C.BG_CARD};
                border: 1px dashed {_C.BORDER_LT};
                border-radius: 8px;
            }}
        """)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Zone de défilement des lignes de fichiers
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        self._scroll.setVisible(False)   # Caché tant qu'aucun fichier

        self._rows_container = QWidget()
        self._rows_container.setStyleSheet("background: transparent;")
        self._rows_layout = QVBoxLayout(self._rows_container)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(0)
        self._rows_layout.addStretch()

        self._scroll.setWidget(self._rows_container)
        root.addWidget(self._scroll, stretch=1)

        # Placeholder (affiché quand aucun fichier)
        self._placeholder = QWidget()
        self._placeholder.setStyleSheet("background: transparent;")
        ph_lay = QVBoxLayout(self._placeholder)
        ph_lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ph_lay.setSpacing(6)

        ph_icon = QLabel("⊞")
        ph_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ph_icon.setStyleSheet(f"font-size: 28px; color: {_C.TEXT_DIM}; background: transparent; border: none;")
        ph_lay.addWidget(ph_icon)

        ph_text = QLabel("Déposer des fichiers MKV / MP4 ici")
        ph_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ph_text.setStyleSheet(f"color: {_C.TEXT_SEC}; font-size: 12px; font-weight: 500; background: transparent; border: none;")
        ph_lay.addWidget(ph_text)

        ph_sub = QLabel("ou cliquer sur « Ajouter des fichiers »")
        ph_sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ph_sub.setStyleSheet(f"color: {_C.TEXT_DIM}; font-size: 10px; background: transparent; border: none;")
        ph_lay.addWidget(ph_sub)

        root.addWidget(self._placeholder, stretch=1)

        # Barre de bas : bouton Ajouter
        add_bar = QWidget()
        add_bar.setStyleSheet(f"""
            QWidget {{
                background: {_C.BG_DEEP};
                border-top: 1px solid {_C.BORDER};
                border-bottom-left-radius: 8px;
                border-bottom-right-radius: 8px;
            }}
        """)
        add_bar.setFixedHeight(36)
        add_bar_lay = QHBoxLayout(add_bar)
        add_bar_lay.setContentsMargins(12, 0, 12, 0)
        add_bar_lay.setSpacing(0)

        add_btn = QPushButton("+ Ajouter des fichiers…")
        add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {_C.ACCENT};
                border: none;
                font-size: 11px;
                font-weight: 600;
            }}
            QPushButton:hover {{ color: #8090ff; }}
        """)
        add_btn.clicked.connect(self._browse)
        add_bar_lay.addWidget(add_btn)
        add_bar_lay.addStretch()

        root.addWidget(add_bar)
        self._update_visibility()

    # ------------------------------------------------------------------
    # API publique
    # ------------------------------------------------------------------

    def add_file(self, sf: SourceFile) -> None:
        """Ajoute une ligne de fichier dans l'état 'inspection en cours'."""
        row = _FileRow(sf.id, sf.path, sf.color)
        row.remove_clicked.connect(self.remove_requested)
        self._rows[sf.id] = row

        # Insérer avant le stretch en fin de layout
        count = self._rows_layout.count()
        self._rows_layout.insertWidget(count - 1, row)

        self._update_visibility()

    def update_file(self, sf: SourceFile) -> None:
        """Met à jour la ligne après inspection réussie."""
        row = self._rows.get(sf.id)
        if row and sf.info:
            row.set_info(sf.info)

    def set_file_error(self, file_id: str, message: str) -> None:
        """Affiche une erreur sur la ligne correspondante."""
        row = self._rows.get(file_id)
        if row:
            row.set_error(message)

    def remove_file(self, file_id: str) -> None:
        """Retire visuellement la ligne."""
        row = self._rows.pop(file_id, None)
        if row:
            self._rows_layout.removeWidget(row)
            row.deleteLater()
        self._update_visibility()

    def file_count(self) -> int:
        return len(self._rows)

    # ------------------------------------------------------------------
    # Interne
    # ------------------------------------------------------------------

    def _update_visibility(self) -> None:
        has_files = bool(self._rows)
        self._scroll.setVisible(has_files)
        self._placeholder.setVisible(not has_files)
        n = len(self._rows)
        h = (_FILE_ROW_H * n + _FILE_BAR_H) if has_files else (_FILE_PH_H + _FILE_BAR_H)
        self.setFixedHeight(h)

    def _browse(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Sélectionner des fichiers vidéo",
            "",
            "Fichiers vidéo (*.mkv *.mp4 *.m4v *.mov);;Tous les fichiers (*)",
        )
        if paths:
            self.add_requested.emit(paths)

    # ------------------------------------------------------------------
    # Drag & drop
    # ------------------------------------------------------------------

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if Path(url.toLocalFile()).suffix.lower() in _ACCEPTED_EXT:
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        paths = []
        for url in event.mimeData().urls():
            p = Path(url.toLocalFile())
            if p.suffix.lower() in _ACCEPTED_EXT and p.is_file():
                paths.append(str(p))
        if paths:
            self.add_requested.emit(paths)
            event.acceptProposedAction()
        else:
            event.ignore()



# =============================================================================
# Tableau de pistes multi-sources (_TrackTable)
# =============================================================================

class _TrackTable(QTableWidget):
    """
    Tableau de pistes multi-sources avec :
      - colonne Source (fichier d'origine, lecture seule)
      - cases à cocher pour l'inclusion/exclusion
      - drag-and-drop pour réordonner les lignes (entre sources possibles)
      - colonnes Langue et Titre éditables inline
      - signal order_changed émis après chaque réordonnancement

    Les données de chaque ligne sont stockées dans le UserRole de la
    colonne COL_CHECK. La méthode current_tracks() synchronise UI → TrackEntry.
    """

    order_changed = Signal()

    _TYPE_ORDER: dict[str, int] = {"video": 0, "audio": 1, "subtitle": 2}
    _MAX_VISIBLE_ROWS = 15
    _ROW_H_DEFAULT    = 28

    # Indices de colonnes
    COL_SOURCE = 0
    COL_CHECK  = 1
    COL_TYPE   = 2
    COL_CODEC  = 3
    COL_LANG   = 4
    COL_TITLE  = 5
    COL_INFO   = 6
    COL_EDIT   = 7

    _HEADERS = ["", "", "Type", "Codec", "Langue", "Titre", "Info", ""]

    _FLAG_RO = (
        Qt.ItemFlag.ItemIsEnabled
        | Qt.ItemFlag.ItemIsSelectable
        | Qt.ItemFlag.ItemIsDragEnabled
    )
    _FLAG_RW = _FLAG_RO | Qt.ItemFlag.ItemIsEditable

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(0, len(self._HEADERS), parent)
        self._filter_selected = False
        self._prev_lang: dict[int, str] = {}
        self._setup_ui()
        self._adjust_height()
        self.itemChanged.connect(self._on_item_changed)

    def _setup_ui(self) -> None:
        self.setHorizontalHeaderLabels(self._HEADERS)

        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDragDropOverwriteMode(False)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)

        self.setShowGrid(False)
        self.setAlternatingRowColors(True)
        self.verticalHeader().setVisible(False)
        self.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.SelectedClicked
        )

        hh = self.horizontalHeader()
        hh.setSectionResizeMode(self.COL_SOURCE, QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(self.COL_CHECK,  QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(self.COL_TYPE,   QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(self.COL_CODEC,  QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(self.COL_LANG,   QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(self.COL_INFO,   QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(self.COL_TITLE,  QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(self.COL_EDIT,   QHeaderView.ResizeMode.Fixed)
        self.setColumnWidth(self.COL_SOURCE, 20)
        self.setColumnWidth(self.COL_CHECK,  32)
        self.setColumnWidth(self.COL_TYPE,   48)
        self.setColumnWidth(self.COL_LANG,   70)
        self.setColumnWidth(self.COL_EDIT,   30)

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

    def append_tracks(self, source_color: str, tracks: list[TrackEntry]) -> None:
        """
        Insère les pistes dans le tableau en respectant l'ordre V → A → S.

        Chaque piste est insérée après les pistes du même type déjà présentes
        (et avant celles des types suivants), regroupant ainsi les pistes par type
        puis par fichier source au sein de chaque type.
        """
        self.blockSignals(True)
        for entry in tracks:
            order = {"video": 0, "audio": 1, "subtitle": 2}.get(entry.track_type, 2)
            pos = self._find_insert_position(order)
            self.insertRow(pos)
            self._fill_row(pos, entry, source_color)
        self.blockSignals(False)
        self._adjust_height()

    @staticmethod
    def _row_type_order(data) -> int:
        """Retourne l'ordre de tri d'une donnée de ligne (TrackEntry)."""
        if isinstance(data, TrackEntry):
            return {"video": 0, "audio": 1, "subtitle": 2}.get(data.track_type, 2)
        return 3

    def _find_insert_position(self, order: int) -> int:
        """Retourne l'index où insérer une ligne de l'ordre donné (après toutes les lignes du même ordre)."""
        for row in range(self.rowCount()):
            item = self.item(row, self.COL_CHECK)
            if item is None:
                continue
            data = item.data(Qt.ItemDataRole.UserRole)
            if data is None:
                continue
            if self._row_type_order(data) > order:
                return row
        return self.rowCount()

    def _adjust_height(self) -> None:
        """Ajuste la hauteur du tableau au nombre de lignes (max 15 lignes visibles)."""
        n = self.rowCount()
        row_h = self.rowHeight(0) if n > 0 else self._ROW_H_DEFAULT
        header_h = self.horizontalHeader().height()
        visible = min(n, self._MAX_VISIBLE_ROWS)
        h = visible * row_h + header_h + 4 if n > 0 else 80 + header_h
        self.setFixedHeight(h)

    def remove_tracks_by_file_id(self, file_id: str) -> None:
        """Retire toutes les lignes (pistes, attachements, tags) du fichier identifié."""
        self.blockSignals(True)
        row = self.rowCount() - 1
        while row >= 0:
            item = self.item(row, self.COL_CHECK)
            if item is not None:
                data = item.data(Qt.ItemDataRole.UserRole)
                if data is not None and getattr(data, "file_id", None) == file_id:
                    self.removeRow(row)
            row -= 1
        self.blockSignals(False)
        self._rebuild_prev_lang()
        self._adjust_height()

    def clear_all(self) -> None:
        """Vide complètement le tableau."""
        self.setRowCount(0)
        self._prev_lang.clear()
        self._adjust_height()

    def _rebuild_prev_lang(self) -> None:
        """Reconstruit _prev_lang depuis les valeurs actuelles du tableau (après un décalage de lignes)."""
        self._prev_lang.clear()
        for row in range(self.rowCount()):
            lang_item = self.item(row, self.COL_LANG)
            if lang_item is not None:
                self._prev_lang[row] = lang_item.text()

    def _fill_row(self, row: int, entry: TrackEntry, source_color: str) -> None:
        """Remplit une ligne depuis un TrackEntry."""

        # Col 0 — carré coloré représentant le fichier source
        src_item = QTableWidgetItem("█")
        src_item.setFlags(self._FLAG_RO & ~Qt.ItemFlag.ItemIsDragEnabled)
        src_item.setForeground(QColor(source_color))
        src_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        src_item.setFont(QFont("Arial", 11))
        src_item.setData(Qt.ItemDataRole.UserRole, source_color)
        self.setItem(row, self.COL_SOURCE, src_item)

        # Col 1 — case à cocher + stockage de l'entrée
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

        # Col 2 — type (lettre colorée)
        type_item = QTableWidgetItem(entry.type_label)
        type_item.setFlags(self._FLAG_RO)
        type_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        match entry.track_type:
            case "video":    type_item.setForeground(QColor(_C.TRACK_VIDEO))
            case "audio":    type_item.setForeground(QColor(_C.TRACK_AUDIO))
            case "subtitle": type_item.setForeground(QColor(_C.TRACK_SUBTITLE))
        self.setItem(row, self.COL_TYPE, type_item)

        # Col 3 — codec
        codec_item = QTableWidgetItem(entry.codec)
        codec_item.setFlags(self._FLAG_RO)
        self.setItem(row, self.COL_CODEC, codec_item)

        # Col 4 — langue (éditable)
        lang_item = QTableWidgetItem(entry.language)
        lang_item.setFlags(self._FLAG_RW)
        self._prev_lang[row] = entry.language
        self.setItem(row, self.COL_LANG, lang_item)

        # Col 5 — infos techniques + flags actifs
        info_item = QTableWidgetItem(entry.full_info_label)
        info_item.setFlags(self._FLAG_RO)
        info_item.setForeground(QColor(_C.TEXT_SEC))
        self.setItem(row, self.COL_INFO, info_item)

        # Col 6 — titre (éditable)
        title_item = QTableWidgetItem(entry.title)
        title_item.setFlags(self._FLAG_RW)
        self.setItem(row, self.COL_TITLE, title_item)

        # Col 7 — bouton édition (icône crayon SVG)
        edit_btn = QPushButton()
        from PySide6.QtCore import QSize
        edit_btn.setIcon(_pencil_icon(_C.TEXT_SEC, 13))
        edit_btn.setIconSize(QSize(13, 13))
        edit_btn.setFixedSize(22, 22)
        edit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        edit_btn.setToolTip("Éditer les métadonnées de cette piste")
        edit_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                border: 1px solid {_C.BORDER};
                border-radius: 4px;
                padding: 0;
            }}
            QPushButton:hover {{
                border-color: {_C.ACCENT};
                background: {_C.ACCENT_DIM};
            }}
            QPushButton:pressed {{
                background: {_C.BG_ACTIVE};
            }}
        """)
        edit_btn.clicked.connect(lambda _=None, e=entry: self._open_edit_dialog(e))
        self.setCellWidget(row, self.COL_EDIT, edit_btn)

    # ------------------------------------------------------------------
    # Lecture de l'état courant
    # ------------------------------------------------------------------

    def current_tracks(self) -> list[TrackEntry]:
        """
        Retourne les TrackEntry dans l'ordre courant du tableau, en
        synchronisant l'état UI (enabled, language, title) vers les objets.
        Seules les lignes de type TrackEntry sont retournées.
        """
        tracks: list[TrackEntry] = []
        for row in range(self.rowCount()):
            item0 = self.item(row, self.COL_CHECK)
            if item0 is None:
                continue
            entry = item0.data(Qt.ItemDataRole.UserRole)
            if not isinstance(entry, TrackEntry):
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

    def _open_edit_dialog(self, entry: "TrackEntry") -> None:
        """Ouvre le dialogue d'édition pour la piste donnée et synchronise le tableau."""
        dlg = TrackEditDialog(entry, parent=self)
        if dlg.exec() == TrackEditDialog.DialogCode.Accepted:
            row = self._find_row_for_entry(entry)
            if row is not None:
                # Block signals during all updates so that:
                # - _on_item_changed (lang check) does not fire mid-update
                # - _on_table_changed / current_tracks() cannot read a stale title
                self.blockSignals(True)
                lang_item = self.item(row, self.COL_LANG)
                if lang_item:
                    lang_item.setText(entry.language)
                title_item = self.item(row, self.COL_TITLE)
                if title_item:
                    title_item.setText(entry.title)
                info_item = self.item(row, self.COL_INFO)
                if info_item:
                    info_item.setText(entry.full_info_label)
                self.blockSignals(False)
                # Emit once to validate lang and trigger preview/signal refresh
                if lang_item is not None:
                    self.itemChanged.emit(lang_item)

    def update_audio_meta(self, file_id: str, mkv_tid: int, lang: str, title: str) -> None:
        """Met à jour lang/titre d'une piste audio sans émettre de signal (sync depuis EncodePanel)."""
        self.blockSignals(True)
        try:
            for row in range(self.rowCount()):
                item0 = self.item(row, self.COL_CHECK)
                if item0 is None:
                    continue
                entry = item0.data(Qt.ItemDataRole.UserRole)
                if not isinstance(entry, TrackEntry):
                    continue
                if entry.file_id == file_id and entry.mkv_tid == mkv_tid:
                    lang_item = self.item(row, self.COL_LANG)
                    if lang_item:
                        lang_item.setText(lang)
                        self._prev_lang[row] = lang
                    title_item = self.item(row, self.COL_TITLE)
                    if title_item:
                        title_item.setText(title)
                    entry.language = lang
                    entry.title = title
                    break
        finally:
            self.blockSignals(False)

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != self.COL_LANG:
            return
        if not Rfc5646LanguageTags.validate_item(item, self._prev_lang):
            prev = self._prev_lang.get(item.row(), "")
            self.blockSignals(True)
            item.setText(prev)
            self.blockSignals(False)
            QTimer.singleShot(0, lambda: QMessageBox.warning(
                self, "Erreur", "Erreur : code langue non reconnu"
            ))

    def _find_row_for_entry(self, entry: "TrackEntry") -> int | None:
        """Retourne l'index de ligne dont le COL_CHECK stocke l'entrée donnée."""
        for row in range(self.rowCount()):
            item = self.item(row, self.COL_CHECK)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) is entry:
                return row
        return None

    def set_all_enabled(self, enabled: bool) -> None:
        """Active ou désactive toutes les pistes d'un coup."""
        self.blockSignals(True)
        state = Qt.CheckState.Checked if enabled else Qt.CheckState.Unchecked
        for row in range(self.rowCount()):
            item = self.item(row, self.COL_CHECK)
            if item:
                item.setCheckState(state)
        self.blockSignals(False)

    def set_filter_selected(self, enabled: bool) -> None:
        """Active/désactive le filtre 'sélectionnées seulement'."""
        self._filter_selected = enabled
        self.refresh_filter()

    def refresh_filter(self) -> None:
        """Applique (ou retire) le filtre sur les lignes décochées."""
        for row in range(self.rowCount()):
            item = self.item(row, self.COL_CHECK)
            if item is None:
                self.setRowHidden(row, False)
                continue
            hidden = (
                self._filter_selected
                and item.checkState() != Qt.CheckState.Checked
            )
            self.setRowHidden(row, hidden)

    # ------------------------------------------------------------------
    # Drag-drop : déplacement de lignes entières
    # ------------------------------------------------------------------

    def dropEvent(self, event: QDropEvent) -> None:
        if event.source() is not self:
            event.ignore()
            return

        src_rows = sorted(set(idx.row() for idx in self.selectedIndexes()))
        if not src_rows:
            event.ignore()
            return

        all_entries = self.current_tracks()
        drop_row = self._drop_target_row(event)

        moving    = [all_entries[r] for r in src_rows]
        remaining = [e for i, e in enumerate(all_entries) if i not in src_rows]

        adjusted = drop_row
        for r in src_rows:
            if r < drop_row:
                adjusted -= 1
        adjusted = max(0, min(adjusted, len(remaining)))

        for i, entry in enumerate(moving):
            remaining.insert(adjusted + i, entry)

        # Capture les couleurs de source AVANT de vider le tableau
        color_by_file_id: dict[str, str] = {}
        for r in range(self.rowCount()):
            item_chk = self.item(r, self.COL_CHECK)
            item_src = self.item(r, self.COL_SOURCE)
            if item_chk and item_src:
                e = item_chk.data(Qt.ItemDataRole.UserRole)
                if isinstance(e, TrackEntry):
                    color_by_file_id[e.file_id] = item_src.data(Qt.ItemDataRole.UserRole) or _C.BORDER

        # Reconstruit le tableau
        self.blockSignals(True)
        self.setRowCount(0)
        for entry in remaining:
            row = self.rowCount()
            self.insertRow(row)
            src_color = color_by_file_id.get(entry.file_id, _C.BORDER)
            self._fill_row(row, entry, src_color)
        self.blockSignals(False)

        self.selectRow(adjusted)
        event.setDropAction(Qt.DropAction.IgnoreAction)
        event.accept()
        self._adjust_height()
        self.order_changed.emit()

    def _drop_target_row(self, event: QDropEvent) -> int:
        index = self.indexAt(event.position().toPoint())
        if not index.isValid():
            return self.rowCount()
        row = index.row()
        rect = self.visualRect(index)
        if event.position().toPoint().y() > rect.top() + rect.height() // 2:
            return row + 1
        return row


# =============================================================================
# Panneau Pièces jointes (_AttachmentItemWidget / _AttachmentPanel)
# =============================================================================
# Dialogue d'édition des balises MKV
# =============================================================================

class _TagEditDialog(QDialog):
    """
    Dialogue d'édition des balises MKV globales d'un fichier source.

    Affiche les balises existantes (tag name figé + value éditable) et permet
    d'en ajouter via un bouton « + Ajouter ».
    """

    def __init__(self, tags: dict[str, str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Éditer les balises")
        self.setMinimumWidth(580)
        self.setMinimumHeight(320)
        self.setStyleSheet(f"""
            QDialog {{ background: {_C.BG_DEEP}; color: {_C.TEXT_PRI}; }}
            QLabel  {{ color: {_C.TEXT_PRI}; background: transparent; border: none; font-size: 11px; }}
        """)
        # Liste mutable (name_widget, value_edit, row_widget)
        self._rows: list[tuple[QComboBox | QLabel, QLineEdit, QWidget]] = []
        self._build_ui(tags)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _build_ui(self, tags: dict[str, str]) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(8)

        # Scroll pour les lignes de tags
        self._rows_widget = QWidget()
        self._rows_widget.setStyleSheet("background: transparent;")
        self._rows_layout = QVBoxLayout(self._rows_widget)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(4)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet(
            f"QScrollArea {{ background: {_C.BG_CARD}; border: 1px solid {_C.BORDER}; border-radius: 4px; }}"
        )
        scroll.setWidget(self._rows_widget)
        root.addWidget(scroll, stretch=1)

        # Tags existants
        for name, value in tags.items():
            self._add_existing_row(name, value)

        # Bouton Ajouter
        add_btn = QPushButton("+ Ajouter un tag")
        add_btn.setFixedHeight(26)
        add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {_C.ACCENT};
                border: 1px solid {_C.ACCENT_DIM};
                border-radius: 4px;
                font-size: 10px;
                font-weight: 600;
                padding: 0 12px;
            }}
            QPushButton:hover {{ background: {_C.ACCENT_DIM}; color: #fff; }}
        """)
        add_btn.clicked.connect(self._add_new_row)
        root.addWidget(add_btn)

        # OK / Annuler
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        btns.setStyleSheet(f"""
            QPushButton {{
                background: {_C.BG_PANEL};
                color: {_C.TEXT_PRI};
                border: 1px solid {_C.BORDER_LT};
                border-radius: 4px;
                font-size: 11px;
                padding: 4px 16px;
            }}
            QPushButton:hover {{ background: {_C.BG_CARD}; }}
        """)
        root.addWidget(btns)

    def _row_stylesheet(self) -> str:
        return (
            f"QLineEdit {{ background: {_C.BG_DEEP}; color: {_C.TEXT_PRI}; "
            f"border: 1px solid {_C.BORDER}; border-radius: 3px; font-size: 11px; padding: 2px 6px; }}"
            f"QLineEdit:focus {{ border-color: {_C.ACCENT}; }}"
        )

    def _add_existing_row(self, name: str, value: str) -> None:
        """Ajoute une ligne avec tag name en label fixe + value éditable."""
        row = QWidget()
        row.setStyleSheet("background: transparent;")
        h = QHBoxLayout(row)
        h.setContentsMargins(4, 2, 4, 2)
        h.setSpacing(8)

        name_lbl = QLabel(name)
        name_lbl.setFixedWidth(160)
        name_lbl.setStyleSheet(
            f"color: {_C.TEXT_DIM}; font-size: 10px; font-weight: 600; "
            "background: transparent; border: none;"
        )
        h.addWidget(name_lbl)

        val_edit = QLineEdit(value)
        val_edit.setStyleSheet(self._row_stylesheet())
        h.addWidget(val_edit, stretch=1)

        rm_btn = self._make_remove_btn(row, name_lbl, val_edit)
        h.addWidget(rm_btn)

        self._rows_layout.addWidget(row)
        self._rows.append((name_lbl, val_edit, row))  # type: ignore[arg-type]

    def _add_new_row(self) -> None:
        """Ajoute une ligne avec combobox (noms standards) + value éditable."""
        row = QWidget()
        row.setStyleSheet("background: transparent;")
        h = QHBoxLayout(row)
        h.setContentsMargins(4, 2, 4, 2)
        h.setSpacing(8)

        name_combo = QComboBox()
        name_combo.setEditable(True)
        name_combo.setFixedWidth(160)
        sorted_tags = sorted(STANDARD_MKV_TAGS - {"TITLE"})
        name_combo.addItems(sorted_tags)
        name_combo.setCurrentText("")
        name_combo.setStyleSheet(f"""
            QComboBox {{
                background: {_C.BG_DEEP};
                color: {_C.TEXT_PRI};
                border: 1px solid {_C.BORDER};
                border-radius: 3px;
                font-size: 11px;
                padding: 2px 6px;
            }}
            QComboBox:focus {{ border-color: {_C.ACCENT}; }}
            QComboBox QAbstractItemView {{
                background: {_C.BG_PANEL};
                color: {_C.TEXT_PRI};
                selection-background-color: {_C.ACCENT_DIM};
            }}
        """)
        h.addWidget(name_combo)

        val_edit = QLineEdit()
        val_edit.setPlaceholderText("valeur…")
        val_edit.setStyleSheet(self._row_stylesheet())
        h.addWidget(val_edit, stretch=1)

        rm_btn = self._make_remove_btn(row, name_combo, val_edit)
        h.addWidget(rm_btn)

        self._rows_layout.addWidget(row)
        self._rows.append((name_combo, val_edit, row))

    def _make_remove_btn(
        self, row: QWidget, name_w: QWidget, val_edit: QLineEdit
    ) -> QPushButton:
        btn = QPushButton("✕")
        btn.setFixedSize(20, 20)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {_C.TEXT_DIM};
                border: 1px solid {_C.BORDER}; border-radius: 3px;
                font-size: 9px; font-weight: 700;
            }}
            QPushButton:hover {{ color: {_C.ERROR}; border-color: {_C.ERROR}; background: #1f0e0e; }}
        """)
        btn.clicked.connect(lambda: self._remove_row(row, name_w, val_edit))
        return btn

    def _remove_row(self, row: QWidget, name_w: QWidget, val_edit: QLineEdit) -> None:
        self._rows = [(n, v, r) for n, v, r in self._rows if r is not row]
        self._rows_layout.removeWidget(row)
        row.deleteLater()

    # ------------------------------------------------------------------
    # Résultat
    # ------------------------------------------------------------------

    def result_tags(self) -> dict[str, str]:
        """Retourne le dict tag_name → valeur après confirmation."""
        result: dict[str, str] = {}
        for name_w, val_edit, _row in self._rows:
            if isinstance(name_w, QComboBox):
                name = name_w.currentText().strip().upper()
            elif isinstance(name_w, QLabel):
                name = name_w.text().strip().upper()
            else:
                continue
            value = val_edit.text().strip()
            if name and value:
                result[name] = value
        return result


# =============================================================================

class _AttachmentItemWidget(QWidget):
    """
    Ligne dans le panneau des pièces jointes.

    Trois variantes :
    - Attachement source (file_id, att, source_color)    → case + nom, sans ✕
    - Balises source    (file_id, is_tag=True, tag_count) → case + "X balises", sans ✕
    - Ajout manuel      (is_manual=True, manual_path)     → case + nom + ✕
    """

    remove_clicked = Signal(object)   # self
    changed        = Signal()

    def __init__(
        self,
        file_id:      str,
        source_color: str               = "",
        att:          AttachmentInfo | None = None,
        tags:         dict[str, str]    | None = None,   # balises MKV globales
        is_tag:       bool              = False,
        is_manual:    bool              = False,
        manual_path:  Path | None       = None,
        parent:       QWidget | None    = None,
    ) -> None:
        super().__init__(parent)
        self.file_id      = file_id
        self.att          = att
        self.is_tag       = is_tag
        self.is_manual    = is_manual
        self.manual_path  = manual_path
        self._orig_tags:   dict[str, str] = tags or {}
        self.setFixedHeight(28)
        self._build_ui(source_color)

    @property
    def tag_count(self) -> int:
        return len(self._orig_tags)

    @property
    def tags(self) -> dict[str, str]:
        return self._orig_tags

    @property
    def enabled(self) -> bool:
        return self._cb.isChecked()

    def _build_ui(self, source_color: str) -> None:
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 8, 0)
        lay.setSpacing(6)

        # Carré coloré source
        if source_color and not self.is_manual:
            sq = QLabel("█")
            sq.setFixedWidth(14)
            sq.setAlignment(Qt.AlignmentFlag.AlignCenter)
            sq.setStyleSheet(
                f"color: {source_color}; background: transparent; border: none; font-size: 11px;"
            )
            lay.addWidget(sq)
        else:
            sp = QWidget()
            sp.setFixedWidth(14)
            lay.addWidget(sp)

        # Case à cocher
        self._cb = QCheckBox()
        self._cb.setChecked(True)
        self._cb.setStyleSheet(f"""
            QCheckBox::indicator {{
                width: 13px;
                height: 13px;
                border-radius: 3px;
                border: 1px solid {_C.BORDER_LT};
                background: {_C.BG_DEEP};
            }}
            QCheckBox::indicator:checked {{
                background: {_C.ACCENT};
                border-color: {_C.ACCENT};
            }}
        """)
        self._cb.stateChanged.connect(self.changed)
        lay.addWidget(self._cb)

        # Libellé
        if self.is_tag:
            n = self.tag_count
            names = ", ".join(self._orig_tags.keys())
            prefix = f"{n} Tag{'s' if n > 1 else ''}"
            text   = f"{prefix} : {names}" if names else prefix
            color  = _C.TRACK_TAGS
        elif self.is_manual:
            text  = self.manual_path.name if self.manual_path else ""
            color = _C.TEXT_PRI
        else:
            text  = self.att.filename if self.att else ""
            color = _C.TRACK_ATTACHMENT

        lbl = QLabel(text)
        if self.is_manual and self.manual_path:
            lbl.setToolTip(str(self.manual_path))
        lbl.setStyleSheet(
            f"color: {color}; background: transparent; border: none; font-size: 11px;"
        )
        lay.addWidget(lbl, stretch=1)

        # Bouton ✕ (uniquement pour les ajouts manuels)
        if self.is_manual:
            rm_btn = QPushButton("✕")
            rm_btn.setFixedSize(18, 18)
            rm_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            rm_btn.setToolTip("Retirer cet attachement")
            rm_btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent;
                    color: {_C.TEXT_DIM};
                    border: 1px solid {_C.BORDER};
                    border-radius: 3px;
                    font-size: 9px;
                    font-weight: 700;
                }}
                QPushButton:hover {{
                    color: {_C.ERROR};
                    border-color: {_C.ERROR};
                    background: #1f0e0e;
                }}
            """)
            rm_btn.clicked.connect(lambda: self.remove_clicked.emit(self))
            lay.addWidget(rm_btn)
        elif not self.is_tag:
            sp2 = QWidget()
            sp2.setFixedWidth(18)
            lay.addWidget(sp2)


class _AttachmentPanel(QFrame):
    """
    Panneau dédié aux pièces jointes et balises MKV.

    Affiche :
    - Par fichier source : une ligne par attachement individuel (cochée par défaut)
    - Par fichier source : une ligne "X balises" (décochée par défaut)
    - Attachements manuels ajoutés via « Ajouter… » (cochés, avec bouton ✕)

    Signal :
        changed()  — émis à chaque modification de sélection ou ajout/retrait
    """

    changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._items: list[_AttachmentItemWidget] = []
        self._panel_tag_overrides: dict[str, str] | None = None  # None = utiliser tags source
        self._build_ui()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self.setStyleSheet(f"""
            QFrame {{
                background: {_C.BG_CARD};
                border: 1px solid {_C.BORDER};
                border-radius: 6px;
            }}
        """)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # En-tête
        header = QWidget()
        header.setStyleSheet(f"""
            QWidget {{
                background: {_C.BG_PANEL};
                border-bottom: 1px solid {_C.BORDER};
                border-top-left-radius: 6px;
                border-top-right-radius: 6px;
            }}
        """)
        header.setFixedHeight(32)
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(12, 0, 8, 0)
        h_lay.setSpacing(8)

        title_lbl = QLabel("PIÈCES JOINTES  &  BALISES")
        title_lbl.setStyleSheet(f"""
            color: {_C.TEXT_DIM};
            font-size: 9px;
            font-weight: 700;
            letter-spacing: 2px;
            background: transparent;
            border: none;
        """)
        h_lay.addWidget(title_lbl)
        h_lay.addStretch()

        self._edit_tags_btn = QPushButton("Éditer les tags")
        self._edit_tags_btn.setFixedHeight(22)
        self._edit_tags_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._edit_tags_btn.setEnabled(False)
        self._edit_tags_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {_C.TRACK_TAGS};
                border: 1px solid {_C.TRACK_TAGS};
                border-radius: 4px;
                font-size: 10px;
                font-weight: 600;
                padding: 0 10px;
            }}
            QPushButton:hover {{
                background: rgba(245,160,48,0.15);
            }}
            QPushButton:disabled {{
                color: {_C.TEXT_DIM};
                border-color: {_C.BORDER};
            }}
        """)
        self._edit_tags_btn.clicked.connect(self._open_global_tag_dialog)
        h_lay.addWidget(self._edit_tags_btn)

        add_btn = QPushButton("+ Ajouter…")
        add_btn.setFixedHeight(22)
        add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {_C.ACCENT};
                border: 1px solid {_C.ACCENT_DIM};
                border-radius: 4px;
                font-size: 10px;
                font-weight: 600;
                padding: 0 10px;
            }}
            QPushButton:hover {{
                background: {_C.ACCENT_DIM};
                color: #ffffff;
            }}
        """)
        add_btn.clicked.connect(self._browse_add)
        h_lay.addWidget(add_btn)
        root.addWidget(header)

        # Placeholder
        self._placeholder = QLabel("Aucune pièce jointe ni balise dans les fichiers sources")
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder.setContentsMargins(0, 16, 0, 16)
        self._placeholder.setStyleSheet(
            f"color: {_C.TEXT_DIM}; font-size: 11px; background: transparent; border: none;"
        )
        root.addWidget(self._placeholder)

        # Conteneur des items
        self._items_widget = QWidget()
        self._items_widget.setStyleSheet("background: transparent;")
        self._items_layout = QVBoxLayout(self._items_widget)
        self._items_layout.setContentsMargins(0, 4, 0, 4)
        self._items_layout.setSpacing(0)
        root.addWidget(self._items_widget)

        self._update_state()

    # ------------------------------------------------------------------
    # API publique
    # ------------------------------------------------------------------

    def add_source_attachments(
        self, file_id: str, source_color: str, attachments: list[AttachmentInfo]
    ) -> None:
        """Ajoute les attachements d'un fichier source."""
        for att in attachments:
            self._add_item(_AttachmentItemWidget(
                file_id=file_id, source_color=source_color, att=att,
            ))

    def add_source_tags(self, file_id: str, source_color: str, tags: dict[str, str]) -> None:
        """Ajoute la ligne de balises d'un fichier source (si tags non vide)."""
        if tags:
            self._add_item(_AttachmentItemWidget(
                file_id=file_id, source_color=source_color,
                tags=tags, is_tag=True,
            ))

    def remove_by_file_id(self, file_id: str) -> None:
        """Retire tous les items (attachements + balises) d'un fichier source."""
        to_remove = [i for i in self._items if i.file_id == file_id]
        for item in to_remove:
            self._items.remove(item)
            self._items_layout.removeWidget(item)
            item.deleteLater()
        if to_remove:
            # Réinitialise les tags édités car la fusion peut avoir changé
            self._panel_tag_overrides = None
            self._update_state()
            self.changed.emit()

    def clear_all(self) -> None:
        """Vide complètement le panneau, y compris les ajouts manuels."""
        for item in self._items[:]:
            self._items_layout.removeWidget(item)
            item.deleteLater()
        self._items.clear()
        self._panel_tag_overrides = None
        self._update_state()

    def get_global_tag_overrides(self) -> "dict[str, str] | None":
        """
        Retourne les balises à appliquer globalement sur le fichier de sortie.

        - Si l'utilisateur a édité via le dialogue global → retourne les tags édités.
        - Sinon → fusionne les tags sources de toutes les lignes cochées
          (priorité au premier fichier importé pour les clés en commun).
        - Retourne None si aucune ligne de balises n'est cochée.
        """
        if self._panel_tag_overrides is not None:
            return self._panel_tag_overrides
        # Merge first-wins depuis les items de tags activés
        merged: dict[str, str] | None = None
        for item in self._items:
            if item.is_tag and item.enabled:
                if merged is None:
                    merged = {}
                for k, v in item.tags.items():
                    merged.setdefault(k, v)   # premier fichier garde la priorité
        return merged

    def get_extras_per_file(self) -> dict:
        """
        Retourne les sélections par fichier source.

        Retourne : dict[file_id, {
            "selected_attachments": list[AttachmentInfo],
            "has_tags": bool,   — True si la ligne de balises de ce fichier est cochée
        }]
        """
        result: dict = {}
        for item in self._items:
            if item.is_manual:
                continue
            entry = result.setdefault(
                item.file_id, {"selected_attachments": [], "has_tags": False}
            )
            if item.is_tag:
                if item.enabled:
                    entry["has_tags"] = True
            elif item.att is not None and item.enabled:
                entry["selected_attachments"].append(item.att)
        return result

    def get_extra_attachments(self) -> list[Path]:
        """Retourne les pièces jointes manuelles cochées."""
        return [
            item.manual_path
            for item in self._items
            if item.is_manual and item.enabled and item.manual_path is not None
        ]

    # ------------------------------------------------------------------
    # Interne
    # ------------------------------------------------------------------

    def _add_item(self, item: _AttachmentItemWidget) -> None:
        self._items.append(item)
        self._items_layout.addWidget(item)
        item.changed.connect(self.changed)
        item.remove_clicked.connect(self._on_remove_item)
        self._update_state()

    def _on_remove_item(self, item: _AttachmentItemWidget) -> None:
        self._items.remove(item)
        self._items_layout.removeWidget(item)
        item.deleteLater()
        self._update_state()
        self.changed.emit()

    def _merged_source_tags(self) -> dict[str, str]:
        """Fusionne les tags de toutes les lignes de balises (premier fichier prioritaire)."""
        merged: dict[str, str] = {}
        for item in self._items:
            if item.is_tag and item.enabled:
                for k, v in item.tags.items():
                    merged.setdefault(k, v)
        return merged

    def _open_global_tag_dialog(self) -> None:
        """Ouvre le dialogue d'édition global des balises (toutes sources fusionnées)."""
        current = self._panel_tag_overrides if self._panel_tag_overrides is not None \
            else self._merged_source_tags()
        dlg = _TagEditDialog(current, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._panel_tag_overrides = dlg.result_tags()
            # Met à jour le libellé du bouton pour indiquer qu'il y a des modifications
            n = len(self._panel_tag_overrides)
            label = f"Tags édités ({n})" if n else "Tags supprimés"
            self._edit_tags_btn.setText(label)
            self.changed.emit()

    def _update_state(self) -> None:
        has = bool(self._items)
        has_tags = any(i.is_tag for i in self._items)
        self._placeholder.setVisible(not has)
        self._items_widget.setVisible(has)
        self._edit_tags_btn.setEnabled(has_tags)
        if not has_tags:
            self._panel_tag_overrides = None
            self._edit_tags_btn.setText("Éditer les tags")

    def _browse_add(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Ajouter des pièces jointes", "",
            "Tous les fichiers (*)",
        )
        for path_str in paths:
            path = Path(path_str)
            self._add_item(_AttachmentItemWidget(
                file_id="", is_manual=True, manual_path=path,
            ))
        if paths:
            self.changed.emit()


# =============================================================================
# Dialogue d'ajout de chapitre (_AddChapterDialog)
# =============================================================================

class _AddChapterDialog(QDialog):
    """
    Dialogue minimaliste pour saisir un nouveau chapitre.

    Champs :
        Timecode  — HH:MM:SS ou HH:MM:SS.mmm
        Nom       — chaîne libre (peut être vide)
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Ajouter un chapitre")
        self.setMinimumWidth(380)
        self.setStyleSheet(f"""
            QDialog {{ background: {_C.BG_DEEP}; color: {_C.TEXT_PRI}; }}
            QLabel  {{ color: {_C.TEXT_SEC}; background: transparent; border: none;
                       font-size: 11px; }}
        """)
        self._tc_edit:   QLineEdit | None = None
        self._name_edit: QLineEdit | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 14)
        root.setSpacing(10)

        field_style = f"""
            QLineEdit {{
                background: {_C.BG_CARD};
                color: {_C.TEXT_PRI};
                border: 1px solid {_C.BORDER};
                border-radius: 4px;
                font-size: 12px;
                font-family: 'JetBrains Mono', monospace;
                padding: 5px 8px;
            }}
            QLineEdit:focus {{ border-color: {_C.ACCENT}; }}
        """

        # --- Timecode ---
        root.addWidget(QLabel("Timecode (HH:MM:SS ou HH:MM:SS.mmm)"))
        self._tc_edit = QLineEdit()
        self._tc_edit.setPlaceholderText("00:00:00.000")
        self._tc_edit.setStyleSheet(field_style)
        root.addWidget(self._tc_edit)

        # --- Nom ---
        root.addWidget(QLabel("Nom du chapitre"))
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("Chapitre…")
        self._name_edit.setStyleSheet(field_style)
        root.addWidget(self._name_edit)

        self._err_lbl = QLabel("")
        self._err_lbl.setStyleSheet(
            f"color: {_C.ERROR}; background: transparent; border: none; font-size: 10px;"
        )
        self._err_lbl.setVisible(False)
        root.addWidget(self._err_lbl)

        # --- Boutons ---
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        ok_btn = btns.button(QDialogButtonBox.StandardButton.Ok)
        ok_btn.setText("Ajouter")
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)
        btns.setStyleSheet(f"""
            QPushButton {{
                background: {_C.BG_PANEL};
                color: {_C.TEXT_PRI};
                border: 1px solid {_C.BORDER_LT};
                border-radius: 4px;
                font-size: 11px;
                padding: 4px 16px;
            }}
            QPushButton:hover {{ background: {_C.BG_CARD}; }}
        """)
        root.addWidget(btns)

    def _on_accept(self) -> None:
        tc_text = self._tc_edit.text().strip() if self._tc_edit else ""
        if _parse_timecode(tc_text) is None:
            self._err_lbl.setText("Timecode invalide — format : HH:MM:SS ou HH:MM:SS.mmm")
            self._err_lbl.setVisible(True)
            return
        self._err_lbl.setVisible(False)
        self.accept()

    def get_chapter(self) -> ChapterEntry | None:
        """Retourne le ChapterEntry saisi, ou None si annulé."""
        if self.result() != QDialog.DialogCode.Accepted:
            return None
        tc_text = self._tc_edit.text().strip() if self._tc_edit else ""
        tc_s    = _parse_timecode(tc_text)
        if tc_s is None:
            return None
        name = (self._name_edit.text().strip() if self._name_edit else "") or ""
        return ChapterEntry(timecode_s=tc_s, name=name)


# =============================================================================
# Panneau de chapitres (_ChapterPanel)
# =============================================================================

class _ChapterPanel(QFrame):
    """
    Panneau d'édition des chapitres.

    Contient :
    - Un en-tête avec titre « CHAPITRES » et bouton « + Ajouter un chapitre »
    - Une case à cocher « Conserver les chapitres »
    - Un tableau éditable (timecode + nom + suppression)

    Signal :
        changed()  — émis à chaque modification (ajout, édition, suppression)
    """

    changed = Signal()

    COL_TC   = 0
    COL_NAME = 1
    COL_DEL  = 2

    _ROW_H = 28

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._chapters: list[ChapterEntry] = []
        self._modified: bool = False
        self._prev_tc: dict[int, str] = {}   # row → dernière valeur timecode valide
        self._build_ui()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self.setStyleSheet(f"""
            QFrame {{
                background: {_C.BG_CARD};
                border: 1px solid {_C.BORDER};
                border-radius: 6px;
            }}
        """)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # En-tête
        header = QWidget()
        header.setStyleSheet(f"""
            QWidget {{
                background: {_C.BG_PANEL};
                border-bottom: 1px solid {_C.BORDER};
                border-top-left-radius: 6px;
                border-top-right-radius: 6px;
            }}
        """)
        header.setFixedHeight(32)
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(12, 0, 8, 0)
        h_lay.setSpacing(8)

        title_lbl = QLabel("CHAPITRES")
        title_lbl.setStyleSheet(f"""
            color: {_C.TEXT_DIM};
            font-size: 9px;
            font-weight: 700;
            letter-spacing: 2px;
            background: transparent;
            border: none;
        """)
        h_lay.addWidget(title_lbl)
        h_lay.addStretch()

        self._add_btn = QPushButton("+ Ajouter un chapitre")
        self._add_btn.setFixedHeight(22)
        self._add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._add_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {_C.ACCENT};
                border: 1px solid {_C.ACCENT_DIM};
                border-radius: 4px;
                font-size: 10px;
                font-weight: 600;
                padding: 0 10px;
            }}
            QPushButton:hover {{ background: {_C.ACCENT_DIM}; color: #ffffff; }}
            QPushButton:disabled {{
                color: {_C.TEXT_DIM};
                border-color: {_C.BORDER};
            }}
        """)
        self._add_btn.clicked.connect(self._on_add_clicked)
        h_lay.addWidget(self._add_btn)
        root.addWidget(header)

        # Case à cocher "Conserver les chapitres"
        cb_row = QWidget()
        cb_row.setStyleSheet(f"""
            QWidget {{
                background: {_C.BG_CARD};
                border-bottom: 1px solid {_C.BORDER};
            }}
        """)
        cb_row.setFixedHeight(36)
        cb_lay = QHBoxLayout(cb_row)
        cb_lay.setContentsMargins(12, 0, 12, 0)

        self._keep_cb = QCheckBox("Conserver les chapitres")
        self._keep_cb.setChecked(True)
        self._keep_cb.setStyleSheet(f"""
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
        """)
        self._keep_cb.stateChanged.connect(self._on_keep_changed)
        cb_lay.addWidget(self._keep_cb)
        cb_lay.addStretch()
        root.addWidget(cb_row)

        # Tableau des chapitres
        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(["Timecode", "Nom du chapitre", ""])
        self._table.setShowGrid(False)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.SelectedClicked
        )
        # Pas de drag-drop : l'ordre est imposé par les timecodes
        self._table.setDragDropMode(QAbstractItemView.DragDropMode.NoDragDrop)

        hh = self._table.horizontalHeader()
        from PySide6.QtWidgets import QHeaderView
        hh.setSectionResizeMode(self.COL_TC,   QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(self.COL_NAME, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(self.COL_DEL,  QHeaderView.ResizeMode.Fixed)
        self._table.setColumnWidth(self.COL_TC,  110)
        self._table.setColumnWidth(self.COL_DEL,  30)

        mono = QFont("JetBrains Mono", 10)
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self._table.setFont(mono)

        self._table.setStyleSheet(f"""
            QTableWidget {{
                background: {_C.BG_CARD};
                alternate-background-color: {_C.BG_DEEP};
                color: {_C.TEXT_PRI};
                border: none;
                border-bottom-left-radius: 6px;
                border-bottom-right-radius: 6px;
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
        """)
        self._table.itemChanged.connect(self._on_item_changed)
        root.addWidget(self._table)

        self._placeholder = QLabel("Aucun chapitre dans les fichiers sources")
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder.setContentsMargins(0, 14, 0, 14)
        self._placeholder.setStyleSheet(
            f"color: {_C.TEXT_DIM}; font-size: 11px; background: transparent; border: none;"
        )
        root.addWidget(self._placeholder)

        self._refresh_ui_state()

    # ------------------------------------------------------------------
    # API publique
    # ------------------------------------------------------------------

    def reset_chapters(self, entries: list[ChapterEntry]) -> None:
        """
        Réinitialise la liste depuis les fichiers sources (efface le flag modifié).
        Appelé par RemuxPanel quand les sources changent.
        """
        self._chapters = sorted(entries, key=lambda e: e.timecode_s)
        self._modified = False
        self._rebuild_table()

    def clear_all(self) -> None:
        """Restaure l'état initial du panneau quand il n'y a plus de source."""
        self._keep_cb.blockSignals(True)
        self._keep_cb.setChecked(True)
        self._keep_cb.blockSignals(False)
        self.reset_chapters([])

    def is_modified(self) -> bool:
        return self._modified

    def keep_chapters(self) -> bool:
        return self._keep_cb.isChecked()

    def get_chapters(self) -> list[ChapterEntry]:
        """Retourne les chapitres courants, triés par timecode."""
        return sorted(self._chapters, key=lambda e: e.timecode_s)

    # ------------------------------------------------------------------
    # Interne — table
    # ------------------------------------------------------------------

    def _rebuild_table(self) -> None:
        """Reconstruit entièrement le tableau depuis self._chapters."""
        self._table.blockSignals(True)
        self._table.setRowCount(0)
        self._prev_tc.clear()
        for entry in self._chapters:
            self._append_row(entry)
        self._table.blockSignals(False)
        self._adjust_height()
        self._refresh_ui_state()

    def _append_row(self, entry: ChapterEntry) -> None:
        """Insère une ligne pour entry à la fin du tableau (sans réordonner)."""
        row = self._table.rowCount()
        self._table.insertRow(row)

        _FLAG_RW = (
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsEditable
        )
        _FLAG_RO = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

        tc_str = _format_timecode(entry.timecode_s)
        tc_item = QTableWidgetItem(tc_str)
        tc_item.setFlags(_FLAG_RW)
        self._table.setItem(row, self.COL_TC, tc_item)
        self._prev_tc[row] = tc_str

        name_item = QTableWidgetItem(entry.name)
        name_item.setFlags(_FLAG_RW)
        self._table.setItem(row, self.COL_NAME, name_item)

        # Bouton suppression
        del_btn = QPushButton("✕")
        del_btn.setFixedSize(22, 22)
        del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        del_btn.setToolTip("Supprimer ce chapitre")
        del_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {_C.TEXT_DIM};
                border: 1px solid {_C.BORDER};
                border-radius: 4px;
                font-size: 10px;
                font-weight: 700;
            }}
            QPushButton:hover {{
                color: {_C.ERROR};
                border-color: {_C.ERROR};
                background: #1f0e0e;
            }}
        """)
        del_btn.clicked.connect(lambda _=None, r=row: self._delete_row(r))
        self._table.setCellWidget(row, self.COL_DEL, del_btn)
        self._table.setRowHeight(row, self._ROW_H)

    def _delete_row(self, row: int) -> None:
        """Supprime le chapitre à la ligne row (en tenant compte du décalage après rebuild)."""
        # Cherche le chapitre par timecode (la ligne bouton pointe sur row au moment de la création,
        # mais après un rebuild les indices sont stables car on reconstruit de zéro).
        if row < 0 or row >= len(self._chapters):
            return
        del self._chapters[row]
        self._modified = True
        self._rebuild_table()
        self.changed.emit()

    def _adjust_height(self) -> None:
        n = self._table.rowCount()
        if n == 0:
            self._table.setFixedHeight(0)
        else:
            header_h = self._table.horizontalHeader().height()
            max_vis = 10
            vis = min(n, max_vis)
            self._table.setFixedHeight(vis * self._ROW_H + header_h + 4)

    def _refresh_ui_state(self) -> None:
        """Met à jour l'état actif/inactif des contrôles."""
        keep = self._keep_cb.isChecked()
        self._table.setEnabled(keep)
        self._add_btn.setEnabled(keep)
        has_rows = self._table.rowCount() > 0
        self._table.setVisible(keep and has_rows)
        self._placeholder.setVisible(keep and not has_rows)

    # ------------------------------------------------------------------
    # Interne — slots
    # ------------------------------------------------------------------

    def _on_keep_changed(self) -> None:
        self._refresh_ui_state()
        self.changed.emit()

    def _on_add_clicked(self) -> None:
        dlg = _AddChapterDialog(parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            entry = dlg.get_chapter()
            if entry is not None:
                self._chapters.append(entry)
                self._chapters.sort(key=lambda e: e.timecode_s)
                self._modified = True
                self._rebuild_table()
                self.changed.emit()

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        row = item.row()
        col = item.column()

        if col == self.COL_TC:
            tc_text = item.text().strip()
            tc_s    = _parse_timecode(tc_text)
            if tc_s is None:
                # Invalide : revert
                prev = self._prev_tc.get(row, "00:00:00.000")
                self._table.blockSignals(True)
                item.setText(prev)
                self._table.blockSignals(False)
                QTimer.singleShot(0, lambda: QMessageBox.warning(
                    self, "Timecode invalide",
                    "Format attendu : HH:MM:SS ou HH:MM:SS.mmm"
                ))
                return
            # Valide : met à jour l'entrée + retrie
            self._prev_tc[row] = _format_timecode(tc_s)
            if row < len(self._chapters):
                self._chapters[row].timecode_s = tc_s
                self._chapters.sort(key=lambda e: e.timecode_s)
                self._modified = True
                self._rebuild_table()
                self.changed.emit()

        elif col == self.COL_NAME:
            if row < len(self._chapters):
                self._chapters[row].name = item.text()
                self._modified = True
                self.changed.emit()


# =============================================================================
# Panneau principal
# =============================================================================

class RemuxPanel(QWidget):
    """
    Panneau de remuxage MKV/MP4 — support multi-sources.

    Signaux :
        log_message(level: str, message: str)
        video_tracks_changed(list)  — pistes vidéo activées (FileInfo, TrackEntry, couleur)
        audio_tracks_changed(list)  — pistes audio activées (AudioTrack, couleur, Path source)
        ready_changed(bool)         — True quand au moins un fichier est inspecté
    """

    log_message = Signal(str, str)

    # Signaux internes thread-safe (résultat d'inspection)
    _inspection_done  = Signal(str, object)   # (file_id, FileInfo)
    _inspection_error = Signal(str, str)      # (file_id, error_message)

    video_tracks_changed = Signal(object)   # list[tuple[FileInfo, TrackEntry, str]]
    audio_tracks_changed = Signal(object)   # list[tuple[AudioTrack, str, Path]]
    ready_changed        = Signal(bool)     # True quand des fichiers sont prêts

    def __init__(self, config: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config   = config
        self._workflow = RemuxWorkflow(
            mkvmerge_bin=config.tool_mkvmerge,
            mkvpropedit_bin=config.tool_mkvpropedit,
        )
        self._executor = ThreadPoolExecutor(max_workers=2)

        # Liste ordonnée des SourceFile chargés
        self._source_files: list[SourceFile] = []
        # Mapping file_id → nom court (pour logs)
        self._source_names:  dict[str, str] = {}
        # Mapping file_id → couleur hex (pour colonne source du tableau)
        self._source_colors: dict[str, str] = {}
        # Compteur monotone pour l'index de couleur (jamais décrémenté)
        self._color_index: int = 0

        self._workflow.log_message.connect(
            self.log_message, Qt.ConnectionType.QueuedConnection
        )
        self._inspection_done.connect(
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
        subtitle = QLabel("Remuxage, fusion et sélection de pistes — sans réencodage")
        subtitle.setStyleSheet(f"color: {_C.TEXT_SEC}; font-size: 12px; background: transparent;")
        content_layout.addWidget(title)
        content_layout.addWidget(subtitle)
        content_layout.addWidget(_separator())

        # --- Fichiers sources ---
        content_layout.addWidget(_section_label("FICHIERS SOURCES"))
        self._file_list = _FileListWidget()
        self._file_list.add_requested.connect(self._on_add_files)
        self._file_list.remove_requested.connect(self._on_remove_file)
        content_layout.addWidget(self._file_list)

        content_layout.addWidget(_separator())

        # --- Pistes ---
        track_header = QHBoxLayout()
        track_header.setSpacing(8)
        track_header.addWidget(_section_label("PISTES"))
        track_header.addStretch()

        btn_all  = _secondary_button("Tout activer")
        btn_none = _secondary_button("Tout désactiver")
        btn_all.clicked.connect(lambda: self._set_all_tracks(True))
        btn_none.clicked.connect(lambda: self._set_all_tracks(False))
        track_header.addWidget(btn_all)
        track_header.addWidget(btn_none)

        self._filter_btn = QPushButton("Sélectionnées seulement")
        self._filter_btn.setCheckable(True)
        self._filter_btn.setFixedHeight(28)
        self._filter_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._filter_btn.setStyleSheet(f"""
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
            QPushButton:checked {{
                background: {_C.ACCENT_DIM};
                color: {_C.ACCENT};
                border-color: {_C.ACCENT};
            }}
        """)
        self._filter_btn.toggled.connect(
            lambda checked: self._track_table.set_filter_selected(checked)
        )
        track_header.addWidget(self._filter_btn)
        content_layout.addLayout(track_header)

        hint = QLabel("Glisser-déposer les lignes pour réordonner · Double-clic pour éditer Langue / Titre")
        hint.setStyleSheet(f"color: {_C.TEXT_DIM}; font-size: 10px; background: transparent;")
        content_layout.addWidget(hint)

        self._track_table = _TrackTable()
        self._track_table.itemChanged.connect(self._on_table_changed)
        self._track_table.order_changed.connect(self._rebuild_preview)
        content_layout.addWidget(self._track_table)

        content_layout.addWidget(_separator())

        # --- Titre du fichier ---
        title_card = _card()
        title_card_layout = QVBoxLayout(title_card)
        title_card_layout.setContentsMargins(16, 10, 16, 10)
        title_card_layout.setSpacing(6)
        title_card_layout.addWidget(_section_label("TITRE DU FICHIER"))

        self._file_title_edit = QLineEdit()
        self._file_title_edit.setPlaceholderText("Titre du conteneur MKV (balise Title)")
        self._file_title_edit.setStyleSheet(self._input_style())
        self._file_title_edit.textChanged.connect(self._rebuild_preview)
        title_card_layout.addWidget(self._file_title_edit)

        content_layout.addWidget(title_card)

        # --- Pièces jointes & Balises ---
        self._attachment_panel = _AttachmentPanel()
        self._attachment_panel.changed.connect(self._rebuild_preview)
        content_layout.addWidget(self._attachment_panel)

        content_layout.addWidget(_separator())

        # --- Chapitres ---
        self._chapter_panel = _ChapterPanel()
        self._chapter_panel.changed.connect(self._on_chapters_changed)
        content_layout.addWidget(self._chapter_panel)

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
            "Ajoutez au moins un fichier source et définissez le chemin de sortie…"
        )
        content_layout.addWidget(self._cmd_preview)

        content_layout.addStretch()
        scroll.setWidget(content)
        root.addWidget(scroll, stretch=1)

    # ------------------------------------------------------------------
    # Ajout de fichiers
    # ------------------------------------------------------------------

    def _on_add_files(self, paths: list[str]) -> None:
        """Reçoit la liste de chemins à ajouter, crée les SourceFile et lance l'inspection."""
        for path_str in paths:
            path = Path(path_str)
            # Évite les doublons
            if any(sf.path == path for sf in self._source_files):
                self.log_message.emit("WARN", f"{path.name} est déjà dans la liste.")
                continue

            color = _pick_file_color(self._color_index)
            self._color_index += 1
            sf = SourceFile(id=str(uuid.uuid4()), path=path, color=color)
            self._source_files.append(sf)

            name = path.name
            short = name[:18] + "…" if len(name) > 20 else name
            self._source_names[sf.id]  = short
            self._source_colors[sf.id] = color

            self._file_list.add_file(sf)
            self.log_message.emit("INFO", f"Inspection de {path.name}…")
            self._executor.submit(self._inspect_file, sf.id, path)

    def _inspect_file(self, file_id: str, path: Path) -> None:
        """Thread worker : inspecte un fichier et émet un signal thread-safe."""
        try:
            inspector = FileInspector(
                ffprobe_bin=self._config.tool_ffprobe,
                mediainfo_bin=self._config.tool_mediainfo,
                mkvmerge_bin=self._config.tool_mkvmerge,
            )
            info = inspector.inspect(path)
            self._inspection_done.emit(file_id, info)
        except InspectionError as exc:
            self.log_message.emit("ERROR", str(exc))
            self._inspection_error.emit(file_id, "Erreur d'inspection.")
        except Exception as exc:
            self.log_message.emit("ERROR", f"Erreur inattendue : {exc}")
            self._inspection_error.emit(file_id, "Erreur d'inspection.")

    def _apply_inspection(self, file_id: str, info: FileInfo) -> None:
        """Reçu dans le thread Qt après une inspection réussie."""
        sf = self._find_source(file_id)
        if sf is None:
            return  # fichier retiré pendant l'inspection

        sf.info   = info
        sf.tracks = tracks_from_file_info(info, file_id=file_id)

        self._file_list.update_file(sf)

        source_color = self._source_colors.get(file_id, _C.BORDER)
        self._track_table.append_tracks(source_color, sf.tracks)
        self._attachment_panel.add_source_attachments(file_id, source_color, info.attachments)
        self._attachment_panel.add_source_tags(file_id, source_color, info.global_tags)

        att_str  = f"  {len(info.attachments)}PJ" if info.attachments else ""
        tag_str  = f"  {info.tag_count}Tags" if info.tag_count else ""
        chap_str = f"  {info.chapters.count}Chap" if info.chapters else ""
        self.log_message.emit(
            "OK",
            f"{info.path.name} chargé — "
            f"{len(info.video_tracks)}V  {len(info.audio_tracks)}A  "
            f"{len(info.subtitle_tracks)}S{att_str}{tag_str}{chap_str}",
        )

        # Met à jour les chapitres (règle d'or appliquée)
        self._update_chapters_from_sources()

        # Chemin de sortie + titre par défaut (premier fichier uniquement)
        if self._source_files[0].id == file_id and not self._output_edit.text().strip():
            default_out = self._config.output_dir / f"{info.path.stem}-MRecode.mkv"
            self._output_edit.setText(str(default_out))
            if not self._file_title_edit.text().strip():
                self._file_title_edit.setText(info.title)

        self.ready_changed.emit(self._has_ready_files())
        self._rebuild_preview()
        self._emit_signals()

    def _on_inspection_error(self, file_id: str, message: str) -> None:
        """Reçu dans le thread Qt après une erreur d'inspection."""
        self._file_list.set_file_error(file_id, message)

    # ------------------------------------------------------------------
    # Retrait de fichier
    # ------------------------------------------------------------------

    def _on_remove_file(self, file_id: str) -> None:
        """Retire un fichier source et toutes ses pistes du tableau."""
        sf = self._find_source(file_id)
        if sf is None:
            return

        self._source_files.remove(sf)
        self._source_names.pop(file_id, None)
        self._source_colors.pop(file_id, None)
        self._file_list.remove_file(file_id)
        self._track_table.remove_tracks_by_file_id(file_id)
        self._attachment_panel.remove_by_file_id(file_id)

        if self._source_files:
            self._update_chapters_from_sources()
        else:
            self._reset_empty_state()

        self.ready_changed.emit(self._has_ready_files())
        self._rebuild_preview()
        self._emit_signals()

        self.log_message.emit("INFO", f"{sf.path.name} retiré de la liste.")

    # ------------------------------------------------------------------
    # Helpers de recherche
    # ------------------------------------------------------------------

    def _find_source(self, file_id: str) -> SourceFile | None:
        return next((sf for sf in self._source_files if sf.id == file_id), None)

    def _has_ready_files(self) -> bool:
        """Retourne True si au moins un fichier a été inspecté avec succès."""
        return any(sf.info is not None for sf in self._source_files)

    # ------------------------------------------------------------------
    # Aperçu de la commande
    # ------------------------------------------------------------------

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
        self._track_table.refresh_filter()
        self._rebuild_preview()
        self._emit_audio_tracks()

    # ------------------------------------------------------------------
    # Signaux vers EncodePanel
    # ------------------------------------------------------------------

    def _emit_signals(self) -> None:
        """Émet video_tracks_changed et audio_tracks_changed."""
        self._emit_video_tracks()
        self._emit_audio_tracks()

    def _emit_video_tracks(self) -> None:
        """Émet video_tracks_changed — toutes pistes vidéo activées avec FileInfo et couleur."""
        track_entries = self._track_table.current_tracks()
        file_info_by_id = {
            sf.id: sf.info
            for sf in self._source_files
            if sf.info is not None
        }

        video_tuples: list[tuple] = []
        for entry in track_entries:
            if entry.track_type != "video" or not entry.enabled:
                continue
            file_info = file_info_by_id.get(entry.file_id)
            if file_info is None:
                continue
            color = self._source_colors.get(entry.file_id, _C.BORDER)
            video_tuples.append((file_info, entry, color))

        self.video_tracks_changed.emit(video_tuples)

    def _emit_audio_tracks(self) -> None:
        """Émet audio_tracks_changed — tuples (AudioTrack, couleur, chemin_source)."""
        track_entries = self._track_table.current_tracks()
        audio_lookup: dict[tuple[str, int], tuple[AudioTrack, str, Path]] = {}
        for sf in self._source_files:
            if sf.info is None:
                continue
            color = self._source_colors.get(sf.id, _C.BORDER)
            for audio_track in sf.info.audio_tracks:
                audio_lookup[(sf.id, audio_track.index)] = (audio_track, color, sf.info.path)

        audio_tuples: list[tuple] = []
        for entry in track_entries:
            if entry.track_type != "audio" or not entry.enabled:
                continue
            audio_data = audio_lookup.get((entry.file_id, entry.mkv_tid))
            if audio_data is None:
                continue
            audio_track, color, source_path = audio_data
            audio_tuples.append((
                dc_replace(audio_track, language=entry.language, title=entry.title),
                color,
                source_path,
            ))

        self.audio_tracks_changed.emit(audio_tuples)

    # ------------------------------------------------------------------
    # Construction de la configuration
    # ------------------------------------------------------------------

    def _current_config(self) -> RemuxConfig | None:
        """Construit un RemuxConfig depuis l'état courant de l'interface."""
        if not self._has_ready_files():
            return None

        output_str = self._output_edit.text().strip()
        if not output_str:
            return None

        all_tracks = self._track_table.current_tracks()
        if not all_tracks:
            return None

        # Construit le mapping file_id → file_index (basé sur _source_files)
        id_to_index = {sf.id: i for i, sf in enumerate(self._source_files)}

        extras = self._attachment_panel.get_extras_per_file()
        merged_tag_overrides = self._attachment_panel.get_global_tag_overrides()

        # SourceInput par fichier : toutes les pistes du fichier
        sources: list[SourceInput] = []
        for i, sf in enumerate(self._source_files):
            if sf.info is None:
                continue
            src_tracks = [t for t in all_tracks if t.file_id == sf.id]
            if not src_tracks:
                src_tracks = sf.tracks
            file_extras = extras.get(sf.id, {})
            has_tags = file_extras.get("has_tags", False)
            sources.append(SourceInput(
                path=sf.path,
                file_index=i,
                tracks=src_tracks,
                selected_attachments=file_extras.get("selected_attachments", []),
                attachment_count=len(sf.info.attachments) if sf.info else 0,
                copy_tags=has_tags,
            ))

        if not sources:
            return None

        # Ordre global des pistes activées (dans l'ordre du tableau)
        track_order = [
            (id_to_index[t.file_id], t.mkv_tid)
            for t in all_tracks
            if t.enabled and t.file_id in id_to_index
        ]

        keep_ch     = self._chapter_panel.keep_chapters()
        ch_overrides = (
            self._chapter_panel.get_chapters()
            if keep_ch and self._chapter_panel.is_modified()
            else None
        )

        return RemuxConfig(
            sources=sources,
            output=Path(output_str),
            track_order=track_order,
            keep_chapters=keep_ch,
            chapter_overrides=ch_overrides,
            extra_attachments=self._attachment_panel.get_extra_attachments(),
            work_dir=self._config.work_dir,
            file_title=self._file_title_edit.text().strip(),
            tag_overrides=merged_tag_overrides,
        )

    # ------------------------------------------------------------------
    # API publique — exécution (déléguée à MainWindow)
    # ------------------------------------------------------------------

    def collect_config(self) -> "RemuxConfig | None":
        """Retourne la configuration de remuxage courante, ou None si incomplète."""
        return self._current_config()

    def update_audio_track_meta(self, stream_index: int, source_path, lang: str, title: str) -> None:
        """Met à jour lang/titre d'une piste audio depuis l'EncodePanel (sync bidirectionnelle)."""
        file_id = next(
            (sf.id for sf in self._source_files if sf.info and sf.info.path == source_path),
            None,
        )
        if file_id is None:
            return
        self._track_table.update_audio_meta(file_id, stream_index, lang, title)
        self._rebuild_preview()

    def current_output_path(self) -> "Path | None":
        """Retourne le chemin de sortie courant saisi dans ce panneau, ou None si vide."""
        text = self._output_edit.text().strip()
        return Path(text) if text else None

    def current_file_title(self) -> str:
        """Retourne le titre de fichier courant saisi dans les options."""
        return self._file_title_edit.text().strip()

    def current_extra_attachments(self) -> list:
        """Retourne les pièces jointes manuelles cochées (list[Path])."""
        return self._attachment_panel.get_extra_attachments()

    def current_tag_overrides(self) -> "dict[str, str] | None":
        """
        Retourne les balises MKV à appliquer sur la sortie, ou None si aucune
        ligne de balises n'est cochée. Utilisé par EncodePanel via provider.
        """
        return self._attachment_panel.get_global_tag_overrides()

    def current_chapter_overrides(self) -> "list | None":
        """
        Retourne les chapitres personnalisés, ou None si non modifiés ou non conservés.
        Utilisé par EncodePanel via provider.
        """
        keep_ch = self._chapter_panel.keep_chapters()
        if keep_ch and self._chapter_panel.is_modified():
            return self._chapter_panel.get_chapters()
        return None

    # ------------------------------------------------------------------
    # Chapitres — helpers internes
    # ------------------------------------------------------------------

    def _on_chapters_changed(self) -> None:
        """Slot : réagit à toute modification dans le panneau chapitres."""
        self._rebuild_preview()
        # Pas d'émission de video/audio signals — les chapitres n'y participent pas.

    def _update_chapters_from_sources(self) -> None:
        """Recalcule les chapitres de base depuis les fichiers sources (règle d'or)."""
        base = self._resolve_base_chapters()
        self._chapter_panel.reset_chapters(base)

    def _reset_empty_state(self) -> None:
        """Restaure l'état initial du panneau quand toutes les sources ont été retirées."""
        self._color_index = 0
        self._track_table.clear_all()
        self._attachment_panel.clear_all()
        self._chapter_panel.clear_all()
        self._filter_btn.setChecked(False)
        self._file_title_edit.clear()
        self._output_edit.clear()

    def _resolve_base_chapters(self) -> "list[ChapterEntry]":
        """
        Règle d'or des chapitres :
            Retourne les chapitres du premier fichier source qui en possède.
            Si aucun fichier n'a de chapitres, retourne [].
        """
        for sf in self._source_files:
            if sf.info and sf.info.chapters and sf.info.chapters.entries:
                return list(sf.info.chapters.entries)
        return []

    def is_ready(self) -> bool:
        """True si au moins un fichier source est inspecté et prêt."""
        return self._has_ready_files()

    def run_operation(self, config: "RemuxConfig") -> "TaskSignals":
        """Lance le remuxage et retourne les signaux de progression."""
        return self._workflow.run(config)

    def validate_config(self, config: "RemuxConfig") -> list[str]:
        """Retourne la liste des erreurs de validation (vide = OK)."""
        return self._workflow.validate(config)

    # ------------------------------------------------------------------
    # Helpers UI
    # ------------------------------------------------------------------

    def _set_all_tracks(self, enabled: bool) -> None:
        self._track_table.set_all_enabled(enabled)
        self._track_table.refresh_filter()
        self._rebuild_preview()

    def _browse_output(self) -> None:
        default = self._output_edit.text() or str(self._config.output_dir)
        path, _ = QFileDialog.getSaveFileName(
            self, "Fichier de sortie", default,
            "Matroska (*.mkv);;Tous les fichiers (*)",
        )
        if path:
            self._output_edit.setText(path)

    def _copy_command(self) -> None:
        from PySide6.QtWidgets import QApplication
        text = self._cmd_preview.toPlainText()
        if text:
            QApplication.clipboard().setText(text)

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
