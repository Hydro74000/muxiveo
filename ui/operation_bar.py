"""Barre d'exécution commune et description légère des opérations configurées."""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QMenu, QProgressBar, QPushButton, QSizePolicy,
    QVBoxLayout, QWidget, QWidgetAction,
)

from core.i18n import translate_text
from ui.design_system import colors as C, font_px, scale
from ui.styles import workflow_icon


@dataclass(frozen=True)
class OperationState:
    key: str
    page: int
    label: str
    summary: str
    ready: bool
    reason: str = ""


class OperationBar(QWidget):
    launch_requested = Signal(str)
    cancel_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._operations: list[OperationState] = []
        self._active: str | None = None
        self._busy = False
        self.setObjectName("operationBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setMinimumHeight(scale(78))
        self.setStyleSheet(f"""
            QWidget#operationBar {{background:{C.BG_PANEL};border-top:1px solid {C.BORDER_LT};}}
            QLabel {{background:transparent;border:none;}}
        """)
        row = QHBoxLayout(self)
        row.setContentsMargins(scale(24), scale(12), scale(24), scale(12))
        row.setSpacing(scale(12))
        info = QVBoxLayout()
        info.setSpacing(scale(5))
        self.heading = QLabel()
        self.heading.setStyleSheet(f"color:{C.TEXT_SEC};font-size:{font_px(9)}px;font-weight:700;letter-spacing:1.5px;")
        self.status_label = QLabel()
        self.status_label.setMinimumWidth(0)
        self.status_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.status_label.setStyleSheet(f"color:{C.TEXT_SEC};font-size:{font_px(11)}px;")
        info.addWidget(self.heading)
        info.addWidget(self.status_label)
        self.progress_widget = QWidget()
        progress = QVBoxLayout(self.progress_widget)
        progress.setContentsMargins(0, 0, 0, 0)
        progress.setSpacing(scale(6))
        self.progress_label = QLabel()
        self.progress_label.setMinimumWidth(0)
        self.progress_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.progress_label.setStyleSheet(f"color:{C.TEXT_SEC};font-size:{font_px(10)}px;")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFixedHeight(scale(4))
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setStyleSheet(f"QProgressBar{{background:{C.BG_CARD};border:none;border-radius:2px;}}QProgressBar::chunk{{background:{C.ACCENT};border-radius:2px;}}")
        progress.addWidget(self.progress_label)
        progress.addWidget(self.progress_bar)
        self.progress_widget.hide()
        info.addWidget(self.progress_widget)
        row.addLayout(info, 1)
        split = QHBoxLayout()
        split.setSpacing(0)
        self.run_button = QPushButton()
        self.run_button.setIcon(workflow_icon("play", "#ffffff"))
        self.run_button.setMinimumWidth(scale(215))
        self.run_button.setFixedHeight(scale(42))
        self.run_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.run_button.clicked.connect(self._run)
        self.arrow_button = QPushButton()
        self.arrow_button.setIcon(workflow_icon("down", "#ffffff", 14))
        self.arrow_button.setFixedSize(scale(38), scale(42))
        self.arrow_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.arrow_button.setAccessibleName(translate_text("Autres opérations prêtes"))
        self.arrow_button.setToolTip(translate_text("Autres opérations prêtes"))
        self.arrow_button.clicked.connect(self.open_menu)
        for button in (self.run_button, self.arrow_button):
            button.setStyleSheet(f"""
                QPushButton{{background:{C.ACCENT};color:white;border:none;font-size:{font_px(12)}px;font-weight:700;padding:0 {scale(16)}px;border-radius:6px;}}
                QPushButton:hover{{background:#6070f8;}}
                QPushButton:disabled{{background:{C.BG_CARD};color:{C.TEXT_SEC};}}
                QPushButton[hasMenu="true"]{{border-top-right-radius:0;border-bottom-right-radius:0;}}
            """)
        split.addWidget(self.run_button)
        split.addWidget(self.arrow_button)
        self.arrow_button.setStyleSheet(self.arrow_button.styleSheet() + "QPushButton{border-top-left-radius:0;border-bottom-left-radius:0;border-left:1px solid rgba(255,255,255,65);}")
        row.addLayout(split)
        self.cancel_button = QPushButton(translate_text("Annuler l’opération"))
        self.cancel_button.setFixedHeight(scale(42))
        self.cancel_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_button.setStyleSheet(f"QPushButton{{background:{C.BG_CARD};color:{C.WARN};border:1px solid {C.WARN};border-radius:6px;padding:0 {scale(16)}px;font-size:{font_px(11)}px;}}")
        self.cancel_button.clicked.connect(self.cancel_requested)
        self.cancel_button.hide()
        row.addWidget(self.cancel_button)
        self.menu = QMenu(self)
        self._menu_rows: dict[QWidgetAction, QWidget] = {}
        self.menu.hovered.connect(self._highlight_menu_row)
        self.menu.setStyleSheet(f"""
            QMenu{{background:{C.BG_PANEL};border:1px solid {C.BORDER_LT};border-radius:8px;padding:6px;}}
            QMenu::item:selected{{background:{C.BG_ACTIVE};}}
            QMenu::section{{color:{C.TEXT_SEC};padding:10px;font-size:{font_px(9)}px;}}
        """)

    def set_operations(self, operations: list[OperationState], active: str | None, running_label: str = "") -> None:
        self._operations, self._active, self._busy = operations, active, bool(running_label)
        current = next((op for op in operations if op.key == active), None)
        ready = [op for op in operations if op.ready]
        self.run_button.setEnabled(not self._busy and (current.ready if current else bool(ready)))
        self.run_button.setText(running_label or (current.label if current else translate_text("Choisir une opération")))
        self.run_button.setToolTip(current.summary if current else translate_text("Choisir une opération prête"))
        self.arrow_button.setVisible(not self._busy and active is not None and any(op.key != active for op in ready))
        has_menu = not self.arrow_button.isHidden()
        if self.run_button.property("hasMenu") != has_menu:
            self.run_button.setProperty("hasMenu", has_menu)
            self.run_button.style().unpolish(self.run_button)
            self.run_button.style().polish(self.run_button)
        self.cancel_button.setVisible(self._busy)
        self.heading.setText(translate_text({"media": "REMUX / ENCODAGE", "hybrid": "HYBRID STUDIO", "merge_dovi": "MERGE DOVI / HDR10+"}.get(active or "", "OPÉRATIONS CONFIGURÉES")))
        if not self._busy:
            self.status_label.setVisible(True)
            self.status_label.setText((current.summary if current.ready else current.reason) if current else translate_text("{count} opérations prêtes", count=len(ready)))
            self.status_label.setToolTip(self.status_label.text())
        else:
            self.menu.close()
            self.status_label.hide()

    def _run(self) -> None:
        if self._busy:
            return
        if self._active is None:
            self.open_menu()
        else:
            self.launch_requested.emit(self._active)

    def open_menu(self) -> None:
        if self._busy:
            return
        self._menu_rows.clear()
        self.menu.clear()
        title = QWidgetAction(self.menu)
        title.setEnabled(False)
        heading = QLabel(translate_text("LANCER UNE OPÉRATION"))
        heading.setStyleSheet(f"color:{C.TEXT_SEC};background:{C.BG_PANEL};border:none;padding:10px;font-size:{font_px(9)}px;letter-spacing:1px;")
        title.setDefaultWidget(heading)
        self.menu.addAction(title)
        for op in sorted((op for op in self._operations if op.ready), key=lambda op: op.key != self._active):
            action = QWidgetAction(self.menu)
            action.setText(op.label)
            widget = QWidget()
            widget.setObjectName("operationMenuRow")
            widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
            widget.setStyleSheet(f"QWidget#operationMenuRow{{background:{C.BG_ACTIVE if op.key == self._active else C.BG_PANEL};border-radius:6px;}}")
            widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            widget.setMinimumWidth(scale(330))
            layout = QHBoxLayout(widget)
            layout.setContentsMargins(scale(12), scale(13), scale(12), scale(13))
            symbol = QLabel()
            symbol.setStyleSheet("background:transparent;border:none;")
            symbol.setPixmap(workflow_icon(op.key).pixmap(scale(22), scale(22)))
            layout.addWidget(symbol)
            texts = QVBoxLayout()
            label = QLabel(op.label)
            label.setStyleSheet(f"color:{C.TEXT_PRI};font-size:{font_px(12)}px;font-weight:600;background:transparent;")
            detail = QLabel(op.summary)
            detail.setMaximumWidth(scale(390))
            detail.setWordWrap(True)
            detail.setStyleSheet(f"color:{C.TEXT_SEC};font-size:{font_px(10)}px;background:transparent;")
            texts.addWidget(label)
            texts.addWidget(detail)
            layout.addLayout(texts, 1)
            badge = QLabel(translate_text("Prêt"))
            badge.setStyleSheet(f"color:{C.OK};font-size:{font_px(9)}px;background:transparent;")
            layout.addWidget(badge)
            action.setDefaultWidget(widget)
            self._menu_rows[action] = widget
            action.triggered.connect(lambda _checked=False, key=op.key: self.launch_requested.emit(key))
            self.menu.addAction(action)
        self.menu.ensurePolished()
        self.menu.popup(self.run_button.mapToGlobal(QPoint(0, -self.menu.sizeHint().height() - scale(10))))

    def _highlight_menu_row(self, action: QAction) -> None:
        for item, widget in self._menu_rows.items():
            widget.setStyleSheet(f"QWidget#operationMenuRow{{background:{C.BG_ACTIVE if item == action else C.BG_PANEL};border-radius:6px;}}")
