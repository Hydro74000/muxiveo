"""Carte repliable pour les outils secondaires des studios."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from ui.design_system import colors as C, font_px, scale


class CollapsibleCard(QWidget):
    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("collapsibleCard")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setStyleSheet(f"QWidget#collapsibleCard{{background:{C.BG_PANEL};border:1px solid {C.BORDER};border-radius:8px;}}")
        root = QVBoxLayout(self)
        root.setContentsMargins(scale(12), scale(8), scale(12), scale(8))
        root.setSpacing(scale(8))
        self.header_layout = QHBoxLayout()
        self.toggle = QPushButton("▸  " + title)
        self.toggle.setCheckable(True)
        self.toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggle.setStyleSheet(f"QPushButton{{color:{C.TEXT_SEC};background:transparent;border:none;text-align:left;padding:4px 0;font-size:{font_px(10)}px;font-weight:600;}}")
        self.header_layout.addWidget(self.toggle)
        self.header_layout.addStretch()
        root.addLayout(self.header_layout)
        self.body = QWidget()
        self.body.setStyleSheet("background:transparent;border:none;")
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, scale(6), 0, 0)
        self.body_layout.setSpacing(scale(8))
        root.addWidget(self.body)
        self.body.hide()
        def toggle_body(expanded: bool) -> None:
            self.body.setVisible(expanded)
            self.toggle.setText(("▾  " if expanded else "▸  ") + title)

        self.toggle.toggled.connect(toggle_body)

    def set_expanded(self, expanded: bool) -> None:
        self.toggle.setChecked(expanded)
