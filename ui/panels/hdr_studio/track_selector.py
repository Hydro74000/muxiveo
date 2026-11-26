"""Choix de la piste vidéo d'un fichier du HDR Studio (A ou B) : bouton compact à menu.

Affiché seulement quand le fichier compte plusieurs pistes vidéo (première piste par défaut, comme en CLI) : un fichier
à piste unique n'ajoute rien à l'interface. Interface réduite à celle d'un ``QComboBox`` utilisée par le panneau
(``addItem``, ``clear``, ``count``, ``currentData``, ``setCurrentIndex``, ``currentIndexChanged``).
"""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import QMenu, QToolButton, QWidget

from core.i18n import translate_text
from ui.design_system import colors as C, font_px, scale


class VideoTrackSelector(QToolButton):
    """Bouton « Piste vidéo #N ▾ » : menu des pistes vidéo (index ffprobe, codec, définition, HDR)."""

    currentIndexChanged = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._items: list[tuple[str, Any]] = []
        self._current = -1
        self._menu = QMenu(self)
        self._group = QActionGroup(self)
        self._group.setExclusive(True)
        self.setMenu(self._menu)
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(scale(28))
        self.setStyleSheet(
            f"QToolButton{{background:{C.BG_CARD};color:{C.TEXT_PRI};border:1px solid {C.BORDER};"
            f"border-radius:5px;padding:0 {scale(18)}px 0 {scale(8)}px;font-size:{font_px(11)}px;}}"
            f"QToolButton:hover{{background:{C.BG_HOVER};border-color:{C.BORDER_LT};}}"
            f"QToolButton:disabled{{background:{C.BG_DEEP};color:{C.TEXT_DIM};}}"
            f"QToolButton::menu-indicator{{subcontrol-position:right center;right:{scale(6)}px;}}")
        self._menu.setStyleSheet(
            f"QMenu{{background:{C.BG_PANEL};color:{C.TEXT_PRI};border:1px solid {C.BORDER_LT};"
            f"border-radius:6px;padding:{scale(4)}px;font-size:{font_px(11)}px;}}"
            f"QMenu::item{{padding:{scale(5)}px {scale(14)}px {scale(5)}px {scale(22)}px;border-radius:4px;}}"
            f"QMenu::item:selected{{background:{C.BG_ACTIVE};}}")
        self._refresh()

    # ── Interface QComboBox utilisée par le panneau ──────────────────────
    def count(self) -> int:
        return len(self._items)

    def currentData(self) -> Any:  # noqa: N802 — même nom que QComboBox
        return self._items[self._current][1] if 0 <= self._current < len(self._items) else None

    def currentIndex(self) -> int:  # noqa: N802
        return self._current

    def clear(self) -> None:
        for action in self._group.actions():
            self._group.removeAction(action)
        self._menu.clear()
        self._items = []
        self._set_current(-1)

    def addItem(self, label: str, data: Any = None) -> None:  # noqa: N802
        index = len(self._items)
        self._items.append((label, data))
        action = QAction(label, self._menu)
        action.setCheckable(True)
        action.triggered.connect(lambda _checked=False, i=index: self.setCurrentIndex(i))
        self._group.addAction(action)
        self._menu.addAction(action)
        if self._current < 0:
            self._set_current(0)
        else:
            self._refresh()

    def setCurrentIndex(self, index: int) -> None:  # noqa: N802
        if index == self._current or not -1 <= index < len(self._items):
            return
        self._set_current(index)

    # ── Interne ──────────────────────────────────────────────────────────
    def _set_current(self, index: int) -> None:
        changed = index != self._current
        self._current = index
        self._refresh()
        if changed and not self.signalsBlocked():
            self.currentIndexChanged.emit(index)

    def _refresh(self) -> None:
        actions = self._group.actions()
        for position, action in enumerate(actions):
            action.setChecked(position == self._current)
        data = self.currentData()
        self.setText(translate_text("Piste vidéo #{index}", index=data) if data is not None else "")
        self.setToolTip(self._items[self._current][0] if 0 <= self._current < len(self._items) else "")
        # Un fichier à piste vidéo unique : rien à choisir, rien d'affiché (jamais montré sans parent : fenêtre).
        visible = len(self._items) > 1
        if not visible or self.parentWidget() is not None:
            self.setVisible(visible)


__all__ = ["VideoTrackSelector"]
