"""Widgets chapitres pour RemuxPanel."""

from __future__ import annotations

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.i18n import apply_translations, translate_text
from core.inspector import ChapterEntry
from ui.panels.remux_panel.models import _format_timecode, _parse_timecode
from ui.panels.remux_panel.theme import _C

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
        apply_translations(self)

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
            self._err_lbl.setText(translate_text("Timecode invalide — format : HH:MM:SS ou HH:MM:SS.mmm"))
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
                    self,
                    translate_text("Timecode invalide"),
                    translate_text("Format attendu : HH:MM:SS ou HH:MM:SS.mmm"),
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

__all__ = ["_AddChapterDialog", "_ChapterPanel"]
