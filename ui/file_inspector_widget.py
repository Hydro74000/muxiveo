"""
ui/file_inspector_widget.py — Widget Qt d'inspection de fichiers vidéo.

Architecture :
    FileInspectorWidget (QWidget)
    ├── Drop zone / sélection fichier  (FileDropZone)
    ├── Résumé fichier                 (FileSummaryBar)
    └── QTabWidget
        ├── Onglet Vidéo               (TrackTableView)
        ├── Onglet Audio               (TrackTableView)
        ├── Onglet Sous-titres         (TrackTableView)
        └── Onglet Chapitres           (ChapterView)

Signaux exposés :
    FileInspectorWidget.inspection_started(path: str)
    FileInspectorWidget.inspection_finished(info: FileInfo)
    FileInspectorWidget.inspection_failed(error: str)

Usage :
    widget = FileInspectorWidget(config)
    widget.inspection_finished.connect(on_done)
    widget.inspect_file(Path("/films/movie.mkv"))
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    Qt, Signal, QAbstractTableModel, QModelIndex, QPersistentModelIndex, QObject,
)
from PySide6.QtGui import QColor, QDragEnterEvent, QDropEvent, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QFileDialog, QFrame,
    QHBoxLayout, QHeaderView, QLabel,
    QPushButton, QTabWidget, QTableView,
    QVBoxLayout, QWidget,
)

from core.config import AppConfig
from core.file_types import build_qt_filter, is_accepted
from core.i18n import apply_translations, translate_text
from core.inspector import (
    AudioTrack, ChapterInfo, FileInfo, FileInspector,
    HDRType, InspectionError, SubtitleTrack, VideoTrack,
)
from ui.design_system import colors as _C, font_px as _font_px, scale as _scale


# =============================================================================
# Modèle de tableau générique
# =============================================================================

class _TrackTableModel(QAbstractTableModel):
    """
    Modèle Qt pour afficher une liste de pistes dans un QTableView.

    Chaque piste est un dict[str, str] — colonne → valeur.
    """

    def __init__(
        self,
        headers: list[str],
        rows: list[list[str]],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._headers = headers
        self._rows    = rows

    # --- Interface QAbstractTableModel ---

    def rowCount(
        self,
        parent: QModelIndex | QPersistentModelIndex = QModelIndex(),
    ) -> int:
        return len(self._rows)

    def columnCount(
        self,
        parent: QModelIndex | QPersistentModelIndex = QModelIndex(),
    ) -> int:
        return len(self._headers)

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if not index.isValid():
            return None

        match role:
            case Qt.ItemDataRole.DisplayRole:
                try:
                    return self._rows[index.row()][index.column()]
                except IndexError:
                    return ""

            case Qt.ItemDataRole.TextAlignmentRole:
                return Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft

            case Qt.ItemDataRole.ForegroundRole:
                # Coloration spéciale pour certaines valeurs
                try:
                    val = self._rows[index.row()][index.column()]
                except IndexError:
                    return None
                if val in ("—", "?"):
                    return QColor(_C.TEXT_DIM)
                return QColor(_C.TEXT_PRI)

            case _:
                return None

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if role == Qt.ItemDataRole.DisplayRole:
            if orientation == Qt.Orientation.Horizontal:
                try:
                    return translate_text(self._headers[section])
                except IndexError:
                    return ""
        if role == Qt.ItemDataRole.ForegroundRole:
            return QColor(_C.TEXT_SEC)
        return None

    def update_data(self, rows: list[list[str]]) -> None:
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()


# =============================================================================
# QTableView stylée
# =============================================================================

def _make_table_view(model: _TrackTableModel) -> QTableView:
    """Crée un QTableView avec le style cohérent au thème sombre."""
    view = QTableView()
    view.setModel(model)
    view.setShowGrid(False)
    view.setAlternatingRowColors(True)
    view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    view.verticalHeader().setVisible(False)
    view.horizontalHeader().setStretchLastSection(True)
    view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    view.setFrameShape(QFrame.Shape.NoFrame)
    view.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    mono = QFont("JetBrains Mono", _font_px(10))
    mono.setStyleHint(QFont.StyleHint.Monospace)
    view.setFont(mono)

    view.setStyleSheet(f"""
        QTableView {{
            background: {_C.BG_DEEP};
            alternate-background-color: {_C.BG_PANEL};
            color: {_C.TEXT_PRI};
            border: none;
            gridline-color: {_C.BORDER};
            selection-background-color: {_C.BG_ACTIVE};
            selection-color: {_C.TEXT_PRI};
        }}
        QHeaderView::section {{
            background: {_C.BG_PANEL};
            color: {_C.TEXT_SEC};
            border: none;
            border-bottom: 1px solid {_C.BORDER};
            padding: {_scale(6)}px {_scale(10)}px;
            font-size: {_font_px(10)}px;
            font-weight: 600;
            letter-spacing: {_scale(1)}px;
        }}
        QScrollBar:vertical {{
            background: {_C.BG_DEEP};
            width: {_scale(6)}px;
            border: none;
        }}
        QScrollBar::handle:vertical {{
            background: {_C.BORDER_LT};
            border-radius: {_scale(3)}px;
            min-height: {_scale(20)}px;
        }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
        QScrollBar:horizontal {{
            background: {_C.BG_DEEP};
            height: {_scale(6)}px;
            border: none;
        }}
        QScrollBar::handle:horizontal {{
            background: {_C.BORDER_LT};
            border-radius: {_scale(3)}px;
        }}
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
    """)
    return view


# =============================================================================
# Convertisseurs pistes → lignes tableau
# =============================================================================

_VIDEO_HEADERS = [
    "#", "Codec", "Résolution", "FPS", "Bit depth",
    "Couleur", "Transfer", "HDR", "Langue", "Titre",
]

_AUDIO_HEADERS = [
    "#", "Codec", "Canaux", "Sample rate", "Bitrate",
    "Langue", "Titre",
]

_SUB_HEADERS = [
    "#", "Codec", "Langue", "Titre", "Défaut", "Forcé",
]

_CHAPTER_HEADERS = ["#", "Timecode", "Titre"]


def _video_rows(tracks: list[VideoTrack]) -> list[list[str]]:
    rows = []
    for t in tracks:
        fps = _fmt_fps(t.frame_rate)
        rows.append([
            str(t.index),
            t.codec.upper(),
            t.resolution,
            fps,
            f"{t.bit_depth} bit" if t.bit_depth else "—",
            t.color_space or "—",
            t.color_transfer or "—",
            t.hdr_label,
            t.language or "—",
            t.title or "—",
        ])
    return rows


def _audio_rows(tracks: list[AudioTrack]) -> list[list[str]]:
    rows = []
    for t in tracks:
        br = f"{t.bit_rate // 1000} kb/s" if t.bit_rate else "—"
        sr = f"{t.sample_rate // 1000} kHz"  if t.sample_rate else "—"
        rows.append([
            str(t.index),
            t.codec.upper(),
            t.channels_label,
            sr,
            br,
            t.language or "—",
            t.title or "—",
        ])
    return rows


def _subtitle_rows(tracks: list[SubtitleTrack]) -> list[list[str]]:
    rows = []
    for t in tracks:
        rows.append([
            str(t.index),
            t.codec,
            t.language or "—",
            t.title or "—",
            translate_text("Oui") if t.default else "—",
            translate_text("Oui") if t.forced  else "—",
        ])
    return rows


def _chapter_rows(info: ChapterInfo | None) -> list[list[str]]:
    if info is None:
        return []
    from core.inspector import fmt_timecode_display
    return [
        [
            str(i + 1),
            fmt_timecode_display(e.timecode_s),
            e.name or translate_text("Chapitre {index}", index=i + 1),
        ]
        for i, e in enumerate(info.entries)
    ]


def _fmt_fps(raw: str | None) -> str:
    """Convertit '24000/1001' en '23.976', '25/1' en '25.000'."""
    if not raw:
        return "—"
    if "/" in raw:
        try:
            num, den = raw.split("/")
            val = int(num) / int(den)
            return f"{val:.3f}"
        except (ValueError, ZeroDivisionError):
            return raw
    return raw


# =============================================================================
# Zone de drop fichier
# =============================================================================

class _FileDropZone(QWidget):
    """
    Zone de drop / bouton d'ouverture de fichier.

    Signal :
        file_selected(path: str)
    """
    file_selected = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self._build_ui()

    def _build_ui(self) -> None:
        self.setFixedHeight(_scale(96))
        self.setStyleSheet(f"""
            QWidget {{
                background: {_C.BG_CARD};
                border: 1px dashed {_C.BORDER_LT};
                border-radius: {_scale(8)}px;
            }}
        """)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(_scale(24), 0, _scale(24), 0)
        layout.setSpacing(_scale(16))

        icon = QLabel("⬇")
        icon.setStyleSheet(
            f"font-size: {_font_px(22)}px; color: {_C.TEXT_DIM}; border: none; background: transparent;"
        )
        layout.addWidget(icon)

        text = QLabel("Déposer un fichier MKV / MP4 ici")
        text.setStyleSheet(
            f"color: {_C.TEXT_SEC}; font-size: {_font_px(13)}px; border: none; background: transparent;"
        )
        layout.addWidget(text)

        layout.addStretch()

        btn = QPushButton("Parcourir…")
        btn.setFixedHeight(_scale(30))
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setStyleSheet(f"""
            QPushButton {{
                background: {_C.BG_ACTIVE};
                color: {_C.TEXT_SEC};
                border: 1px solid {_C.BORDER_LT};
                border-radius: {_scale(5)}px;
                font-size: {_font_px(12)}px;
                padding: 0 {_scale(14)}px;
            }}
            QPushButton:hover {{
                background: {_C.ACCENT_DIM};
                color: {_C.TEXT_PRI};
                border-color: {_C.ACCENT};
            }}
        """)
        btn.clicked.connect(self._open_dialog)
        layout.addWidget(btn)

    def _open_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            translate_text("Ouvrir un fichier vidéo"),
            "",
            build_qt_filter(video_only=True),
        )
        if path:
            self.file_selected.emit(path)

    # --- Drag & Drop ---

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            if urls and is_accepted(urls[0].toLocalFile(), video_only=True):
                event.acceptProposedAction()
                self.setStyleSheet(f"""
                    QWidget {{
                        background: {_C.BG_HOVER};
                border: 1px dashed {_C.ACCENT};
                        border-radius: {_scale(8)}px;
                    }}
                """)
                return
        event.ignore()

    def dragLeaveEvent(self, event) -> None:  # type: ignore[override]
        self.setStyleSheet(f"""
            QWidget {{
                background: {_C.BG_CARD};
                border: 1px dashed {_C.BORDER_LT};
                border-radius: {_scale(8)}px;
            }}
        """)

    def dropEvent(self, event: QDropEvent) -> None:
        self.dragLeaveEvent(event)
        urls = event.mimeData().urls()
        if urls:
            self.file_selected.emit(urls[0].toLocalFile())


# =============================================================================
# Barre de résumé
# =============================================================================

class _FileSummaryBar(QWidget):
    """Barre horizontale affichant les métadonnées clés du fichier."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(_scale(44))
        self.setStyleSheet(f"""
            QWidget {{
                background: {_C.BG_PANEL};
                border: 1px solid {_C.BORDER};
                border-radius: {_scale(6)}px;
            }}
        """)
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(_scale(16), 0, _scale(16), 0)
        self._layout.setSpacing(_scale(24))
        self._cells: dict[str, QLabel] = {}
        self._build_cells()

    def _build_cells(self) -> None:
        keys = ["Fichier", "Taille", "Durée", "Format", "HDR", "Frames"]
        for key in keys:
            pair = QWidget()
            pair.setStyleSheet("background: transparent; border: none;")
            pl = QHBoxLayout(pair)
            pl.setContentsMargins(0, 0, 0, 0)
            pl.setSpacing(_scale(6))

            lbl_key = QLabel(key)
            lbl_key.setStyleSheet(f"""
                color: {_C.TEXT_DIM};
                font-size: {_font_px(10)}px;
                font-weight: 700;
                letter-spacing: {_scale(1)}px;
                background: transparent;
                border: none;
            """)
            pl.addWidget(lbl_key)

            lbl_val = QLabel("—")
            lbl_val.setStyleSheet(f"""
                color: {_C.TEXT_SEC};
                font-size: {_font_px(11)}px;
                background: transparent;
                border: none;
            """)
            self._cells[key] = lbl_val
            pl.addWidget(lbl_val)

            self._layout.addWidget(pair)

        self._layout.addStretch()

    def update_from(self, info: FileInfo) -> None:
        self._cells["Fichier"].setText(info.path.name)
        self._cells["Taille"].setText(info.size_human)
        self._cells["Durée"].setText(info.duration_human)
        self._cells["Format"].setText(info.format.split(",")[0].upper())
        primary = info.primary_video
        hdr_display = primary.hdr_label if primary is not None else info.hdr_type.label()
        self._cells["HDR"].setText(hdr_display)
        self._cells["Frames"].setText(str(info.frame_count) if info.frame_count else "—")

        # Coloriser le badge HDR
        hdr_color = {
            HDRType.NONE:                   _C.TEXT_DIM,
            HDRType.HLG:                    "#7ed957",
            HDRType.HDR10:                  _C.INFO,
            HDRType.HDR10PLUS:              "#4fc3f7",
            HDRType.DOLBY_VISION:           "#ce93d8",
            HDRType.DOLBY_VISION_HDR10PLUS: _C.ACCENT,
        }.get(info.hdr_type, _C.TEXT_SEC)
        self._cells["HDR"].setStyleSheet(
            f"color: {hdr_color}; font-size: {_font_px(11)}px; font-weight: 600;"
            f"background: transparent; border: none;"
        )

    def reset(self) -> None:
        for lbl in self._cells.values():
            lbl.setText("—")
            lbl.setStyleSheet(
                f"color: {_C.TEXT_SEC}; font-size: {_font_px(11)}px; background: transparent; border: none;"
            )


# =============================================================================
# Vue chapitres
# =============================================================================

class _ChapterView(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._model = _TrackTableModel(_CHAPTER_HEADERS, [])
        self._view  = _make_table_view(self._model)
        layout.addWidget(self._view)

        self._empty = QLabel("Aucun chapitre détecté")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setStyleSheet(f"color: {_C.TEXT_DIM}; font-size: {_font_px(12)}px;")
        layout.addWidget(self._empty)

    def update_data(self, info: ChapterInfo | None) -> None:
        rows = _chapter_rows(info)
        self._model.update_data(rows)
        self._view.setVisible(bool(rows))
        self._empty.setVisible(not rows)


# =============================================================================
# Widget principal
# =============================================================================

class FileInspectorWidget(QWidget):
    """
    Widget d'inspection de fichiers vidéo.

    Signaux :
        inspection_started(path: str)
        inspection_finished(info: FileInfo)
        inspection_failed(error: str)
    """

    inspection_started  = Signal(str)
    inspection_finished = Signal(object)   # FileInfo
    inspection_failed   = Signal(str)

    def __init__(
        self,
        config: AppConfig,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._config    = config
        self._inspector = FileInspector(
            ffprobe_bin   = config.tool_ffprobe,
            mediainfo_bin = config.tool_mediainfo,
        )
        self._executor  = ThreadPoolExecutor(max_workers=1)
        self._current_info: FileInfo | None = None

        self._build_ui()
        self._connect_signals()
        apply_translations(self)

    # ------------------------------------------------------------------
    # Construction UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self.setStyleSheet(f"background: {_C.BG_DEEP};")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Partie haute : drop zone + résumé
        top = QWidget()
        top.setStyleSheet(f"background: {_C.BG_DEEP};")
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(_scale(16), _scale(16), _scale(16), _scale(12))
        top_layout.setSpacing(_scale(10))

        self._drop_zone = _FileDropZone()
        top_layout.addWidget(self._drop_zone)

        self._summary = _FileSummaryBar()
        top_layout.addWidget(self._summary)

        # Label de statut (chargement / erreur)
        self._status = QLabel("")
        self._status.setStyleSheet(
            f"color: {_C.TEXT_DIM}; font-size: {_font_px(11)}px; padding: 0 {_scale(4)}px;"
        )
        self._status.setVisible(False)
        top_layout.addWidget(self._status)

        root.addWidget(top)

        # Séparateur
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {_C.BORDER}; background: {_C.BORDER};")
        sep.setFixedHeight(_scale(1))
        root.addWidget(sep)

        # Onglets pistes
        self._tabs = QTabWidget()
        self._tabs.setStyleSheet(f"""
            QTabWidget::pane {{
                border: none;
                background: {_C.BG_DEEP};
            }}
            QTabBar::tab {{
                background: {_C.BG_PANEL};
                color: {_C.TEXT_SEC};
                border: none;
                border-bottom: 2px solid transparent;
                padding: {_scale(8)}px {_scale(20)}px;
                font-size: {_font_px(12)}px;
            }}
            QTabBar::tab:selected {{
                color: {_C.TEXT_PRI};
                border-bottom: 2px solid {_C.ACCENT};
                background: {_C.BG_DEEP};
            }}
            QTabBar::tab:hover:!selected {{
                background: {_C.BG_HOVER};
                color: {_C.TEXT_PRI};
            }}
        """)

        # Onglet Vidéo
        self._video_model = _TrackTableModel(_VIDEO_HEADERS, [])
        self._video_view  = _make_table_view(self._video_model)
        self._tabs.addTab(self._video_view, "▣  Vidéo")

        # Onglet Audio
        self._audio_model = _TrackTableModel(_AUDIO_HEADERS, [])
        self._audio_view  = _make_table_view(self._audio_model)
        self._tabs.addTab(self._audio_view, "♫  Audio")

        # Onglet Sous-titres
        self._sub_model = _TrackTableModel(_SUB_HEADERS, [])
        self._sub_view  = _make_table_view(self._sub_model)
        self._tabs.addTab(self._sub_view, "⬛  Sous-titres")

        # Onglet Chapitres
        self._chapter_view = _ChapterView()
        self._tabs.addTab(self._chapter_view, "◉  Chapitres")

        root.addWidget(self._tabs, stretch=1)

    def _connect_signals(self) -> None:
        self._drop_zone.file_selected.connect(self._on_file_selected)
        # Auto-connexion des slots de résultat — thread-safe via QueuedConnection
        self.inspection_finished.connect(
            self.on_inspection_finished, Qt.ConnectionType.QueuedConnection
        )
        self.inspection_failed.connect(
            self.on_inspection_failed, Qt.ConnectionType.QueuedConnection
        )

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------

    def inspect_file(self, path: Path) -> None:
        """Lance l'inspection du fichier dans un thread secondaire."""
        self._set_status(translate_text("Analyse de {name}…", name=path.name), _C.TEXT_SEC)
        self._summary.reset()
        self.inspection_started.emit(str(path))

        def _run() -> None:
            try:
                info = self._inspector.inspect(path)
                # Retour dans le thread Qt via signal
                self._deliver_result(info)
            except InspectionError as exc:
                self._deliver_error(str(exc))
            except Exception as exc:
                self._deliver_error(translate_text("Erreur inattendue : {exc}", exc=exc))

        self._executor.submit(_run)

    def _deliver_result(self, info: FileInfo) -> None:
        """Appelé depuis le thread secondaire — utilise un signal pour revenir dans Qt."""
        self.inspection_finished.emit(info)

    def _deliver_error(self, msg: str) -> None:
        self.inspection_failed.emit(msg)

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _on_file_selected(self, path_str: str) -> None:
        self.inspect_file(Path(path_str))

    def on_inspection_finished(self, info: FileInfo) -> None:
        """
        Met à jour tous les tableaux depuis le résultat d'inspection.

        À connecter à inspection_finished depuis la fenêtre parente :
            widget.inspection_finished.connect(widget.on_inspection_finished)
        """
        self._current_info = info
        self._summary.update_from(info)

        self._video_model.update_data(_video_rows(info.video_tracks))
        self._audio_model.update_data(_audio_rows(info.audio_tracks))
        self._sub_model.update_data(_subtitle_rows(info.subtitle_tracks))
        self._chapter_view.update_data(info.chapters)

        # Mise à jour des titres d'onglets avec le comptage
        self._tabs.setTabText(0, translate_text("▣  Vidéo ({count})", count=len(info.video_tracks)))
        self._tabs.setTabText(1, translate_text("♫  Audio ({count})", count=len(info.audio_tracks)))
        self._tabs.setTabText(2, translate_text("⬛  Sous-titres ({count})", count=len(info.subtitle_tracks)))
        ch = info.chapters.count if info.chapters else 0
        self._tabs.setTabText(3, translate_text("◉  Chapitres ({count})", count=ch))

        self._set_status(
            translate_text(
                "✓ {name} — {video}V {audio}A {subtitle}S",
                name=info.path.name,
                video=len(info.video_tracks),
                audio=len(info.audio_tracks),
                subtitle=len(info.subtitle_tracks),
            ),
            _C.OK,
        )

    def on_inspection_failed(self, error: str) -> None:
        """À connecter à inspection_failed."""
        self._set_status(translate_text("✗ {error}", error=error), _C.ERROR)

    # ------------------------------------------------------------------
    # Utilitaires
    # ------------------------------------------------------------------

    def _set_status(self, msg: str, color: str) -> None:
        self._status.setText(msg)
        self._status.setStyleSheet(
            f"color: {color}; font-size: {_font_px(11)}px; padding: 0 {_scale(4)}px;"
        )
        self._status.setVisible(True)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        """Arrête proprement le ThreadPoolExecutor à la fermeture du widget."""
        self._executor.shutdown(wait=True)
        super().closeEvent(event)

    def current_info(self) -> FileInfo | None:
        """Retourne le dernier FileInfo inspecté, ou None."""
        return self._current_info
