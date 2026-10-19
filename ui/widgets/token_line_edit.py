"""Menus d'insertion partagés entre les profils et les templates de sortie."""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from PySide6.QtWidgets import QLineEdit, QMenu

from core.i18n import translate_text


@dataclass(frozen=True)
class TokenChoice:
    categories: tuple[str, ...]
    label: str
    text: str
    description: str = ""


def add_token_submenu(parent: QMenu, label: str) -> QMenu:
    """Garde les sous-menus vivants pendant toute l'ouverture du menu Qt."""
    submenu = QMenu(translate_text(label), parent)
    submenu.setToolTipsVisible(True)
    parent.addMenu(submenu)
    return submenu


def populate_token_menu(menu: QMenu, choices: Iterable[TokenChoice], insert: Callable[[str], None]) -> None:
    """Conserve le libellé lisible, le texte exact à insérer et son explication."""
    menus: dict[tuple[str, ...], QMenu] = {(): menu}
    menu.setToolTipsVisible(True)
    for choice in choices:
        path: tuple[str, ...] = ()
        for category in choice.categories:
            parent = menus[path]
            path = (*path, category)
            if path not in menus:
                menus[path] = add_token_submenu(parent, category)
        label = translate_text(choice.label)
        action = menus[path].addAction(label if label == choice.text else f"{label}\t{choice.text}")
        action.setData(choice.text)
        action.setToolTip(translate_text(choice.description))
        action.setStatusTip(action.toolTip())
        action.triggered.connect(lambda _checked=False, text=choice.text: insert(text))


class TokenLineEdit(QLineEdit):
    """Garde le menu d'édition natif et laisse chaque entrée proposer ses tokens."""

    def _context_menu(self) -> QMenu:
        menu = self.createStandardContextMenu()
        if not self.isReadOnly():
            menu.addSeparator()
            self._populate_context_menu(menu)
        return menu

    def _populate_context_menu(self, menu: QMenu) -> None:
        pass

    def contextMenuEvent(self, event) -> None:
        menu = self._context_menu()
        try:
            menu.exec(event.globalPos())
        finally:
            menu.deleteLater()
