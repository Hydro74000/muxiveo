"""Widgets de sélection de fichiers pour RemuxPanel."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from core.i18n import translate_text
from core.inspector import FileInfo
from ui.panels.remux_panel.models import _FILE_BAR_H, _FILE_PH_H, _FILE_ROW_H, SourceFile
from ui.panels.remux_panel.theme import _C


class _FileRow(QWidget):
    remove_clicked = Signal(str)

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
        self._info_lbl.setText(translate_text("Erreur : {message}", message=message))
        self._info_lbl.setStyleSheet(f"""
            color: {_C.ERROR};
            font-size: 10px;
            background: transparent;
            border: none;
        """)


_ACCEPTED_EXT = {".mkv", ".mp4", ".m4v", ".mov", ".srt"}


class _FileListWidget(QFrame):
    add_requested = Signal(list)
    remove_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self._rows: dict[str, _FileRow] = {}
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

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        self._scroll.setVisible(False)

        self._rows_container = QWidget()
        self._rows_container.setStyleSheet("background: transparent;")
        self._rows_layout = QVBoxLayout(self._rows_container)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(0)
        self._rows_layout.addStretch()

        self._scroll.setWidget(self._rows_container)
        root.addWidget(self._scroll, stretch=1)

        self._placeholder = QWidget()
        self._placeholder.setStyleSheet("background: transparent;")
        ph_lay = QVBoxLayout(self._placeholder)
        ph_lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ph_lay.setSpacing(6)

        ph_icon = QLabel("⊞")
        ph_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ph_icon.setStyleSheet(f"font-size: 28px; color: {_C.TEXT_DIM}; background: transparent; border: none;")
        ph_lay.addWidget(ph_icon)

        ph_text = QLabel("Déposer des fichiers vidéo / sous-titres ici")
        ph_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ph_text.setStyleSheet(f"color: {_C.TEXT_SEC}; font-size: 12px; font-weight: 500; background: transparent; border: none;")
        ph_lay.addWidget(ph_text)

        ph_sub = QLabel("ou cliquer sur « Ajouter des fichiers »")
        ph_sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ph_sub.setStyleSheet(f"color: {_C.TEXT_DIM}; font-size: 10px; background: transparent; border: none;")
        ph_lay.addWidget(ph_sub)

        root.addWidget(self._placeholder, stretch=1)

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

    def add_file(self, sf: SourceFile) -> None:
        row = _FileRow(sf.id, sf.path, sf.color)
        row.remove_clicked.connect(self.remove_requested)
        self._rows[sf.id] = row

        count = self._rows_layout.count()
        self._rows_layout.insertWidget(count - 1, row)
        self._update_visibility()

    def update_file(self, sf: SourceFile) -> None:
        row = self._rows.get(sf.id)
        if row and sf.info:
            row.set_info(sf.info)

    def set_file_error(self, file_id: str, message: str) -> None:
        row = self._rows.get(file_id)
        if row:
            row.set_error(message)

    def remove_file(self, file_id: str) -> None:
        row = self._rows.pop(file_id, None)
        if row:
            self._rows_layout.removeWidget(row)
            row.deleteLater()
        self._update_visibility()

    def file_count(self) -> int:
        return len(self._rows)

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
            translate_text("Sélectionner des fichiers source"),
            "",
            translate_text("Fichiers source (*.mkv *.mp4 *.m4v *.mov *.srt);;Tous les fichiers (*)"),
        )
        if paths:
            self.add_requested.emit(paths)

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


__all__ = ["_FileListWidget"]
