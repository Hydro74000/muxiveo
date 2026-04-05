"""
tests/test_i18n.py — Régressions i18n des contrôles Qt.
"""

from __future__ import annotations

from PySide6.QtWidgets import QComboBox, QTabWidget, QWidget

from core.i18n import apply_translations, set_current_language


def test_apply_translations_translates_combobox_items_and_keeps_source(qt_app):
    root = QWidget()
    combo = QComboBox(root)
    combo.addItem("Parcourir…", "browse")
    combo.addItem("Réglages", "settings")

    set_current_language("eng")
    apply_translations(root)
    assert combo.itemText(0) == "Browse..."
    assert combo.itemText(1) == "Settings"

    set_current_language("fra")
    apply_translations(root)
    assert combo.itemText(0) == "Parcourir…"
    assert combo.itemText(1) == "Réglages"


def test_apply_translations_translates_tabwidget_titles_and_keeps_source(qt_app):
    root = QWidget()
    tabs = QTabWidget(root)
    tabs.addTab(QWidget(), "Réglages")
    tabs.addTab(QWidget(), "ENCODAGE")

    set_current_language("eng")
    apply_translations(root)
    assert tabs.tabText(0) == "Settings"
    assert tabs.tabText(1) == "ENCODING"

    set_current_language("fra")
    apply_translations(root)
    assert tabs.tabText(0) == "Réglages"
    assert tabs.tabText(1) == "ENCODAGE"

