"""
tests/test_i18n.py — Régressions i18n des contrôles Qt.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from PySide6.QtWidgets import QComboBox, QTabWidget, QWidget

from core.i18n import apply_translations, set_current_language


_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _locale_catalog() -> dict[str, dict[str, str]]:
    return json.loads((_PROJECT_ROOT / "locales.json").read_text(encoding="utf-8"))


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def _constant_string(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _is_translate_text_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    return _call_name(node) == "translate_text"


def _looks_user_facing(text: str) -> bool:
    if not text or text.strip() != text or len(text) <= 1:
        return False
    if text.startswith(("_", "Q", "color:", "#", "font", "background", "border", "padding", "margin")):
        return False
    return any(ch.isspace() for ch in text) or any(ch in text for ch in "éèêàùçÉÀôîû…?:«»")


def _assert_catalog_entries(strings: list[tuple[Path, int, str]]) -> None:
    catalog = _locale_catalog()
    missing = [
        f"{path.relative_to(_PROJECT_ROOT)}:{line}: {text!r}"
        for path, line, text in strings
        if text not in catalog or "fra" not in catalog[text] or "eng" not in catalog[text]
    ]
    assert missing == []


def test_i18n_catalog_covers_translate_text_literals():
    paths = [
        *(_PROJECT_ROOT / "core").rglob("*.py"),
        *(_PROJECT_ROOT / "ui").rglob("*.py"),
        *(_PROJECT_ROOT / "workers").rglob("*.py"),
        *(_PROJECT_ROOT / "cli").rglob("*.py"),
        _PROJECT_ROOT / "main.py",
        _PROJECT_ROOT / "launcher.py",
        _PROJECT_ROOT / "mediamanager.py",
    ]
    strings: list[tuple[Path, int, str]] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _call_name(node) != "translate_text" or not node.args:
                continue
            text = _constant_string(node.args[0])
            if text is not None:
                strings.append((path, node.lineno, text))

    _assert_catalog_entries(strings)


def test_i18n_catalog_covers_known_qt_literal_sources():
    qt_constructors = {
        "QAction",
        "QCheckBox",
        "QGroupBox",
        "QLabel",
        "QPushButton",
        "QRadioButton",
        "QToolButton",
    }
    methods = {
        "addItem",
        "addTab",
        "setHeaderLabels",
        "setHorizontalHeaderLabels",
        "setPlaceholderText",
        "setStatusTip",
        "setText",
        "setTitle",
        "setToolTip",
        "setWindowTitle",
    }
    strings: list[tuple[Path, int, str]] = []
    for path in [*(_PROJECT_ROOT / "ui").rglob("*.py"), _PROJECT_ROOT / "main.py"]:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            if isinstance(node.func, ast.Name) and name in qt_constructors and node.args:
                text = _constant_string(node.args[0])
                if text and _looks_user_facing(text) and not _is_translate_text_call(node.args[0]):
                    strings.append((path, node.lineno, text))
            if name in methods and node.args:
                values = node.args[0].elts if isinstance(node.args[0], (ast.List, ast.Tuple)) else [node.args[0]]
                for value in values:
                    text = _constant_string(value)
                    if text and _looks_user_facing(text) and not _is_translate_text_call(value):
                        strings.append((path, node.lineno, text))

    _assert_catalog_entries(strings)


def test_i18n_catalog_covers_settings_and_advanced_param_specs():
    strings: list[tuple[Path, int, str]] = []

    config_path = _PROJECT_ROOT / "core" / "config.py"
    config_tree = ast.parse(config_path.read_text(encoding="utf-8"), filename=str(config_path))
    for node in ast.walk(config_tree):
        if not isinstance(node, ast.Dict):
            continue
        for key_node, value_node in zip(node.keys, node.values):
            if (
                isinstance(key_node, ast.Constant)
                and key_node.value in {"title", "label", "description"}
                and isinstance(value_node, ast.Constant)
                and isinstance(value_node.value, str)
            ):
                strings.append((config_path, value_node.lineno, value_node.value))

    specs_path = _PROJECT_ROOT / "ui" / "dialogs" / "extra_params_dialog.py"
    specs_tree = ast.parse(specs_path.read_text(encoding="utf-8"), filename=str(specs_path))
    for node in ast.walk(specs_tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if (
                keyword.arg in {"label", "tooltip", "title"}
                and isinstance(keyword.value, ast.Constant)
                and isinstance(keyword.value.value, str)
            ):
                strings.append((specs_path, keyword.value.lineno, keyword.value.value))

    _assert_catalog_entries(strings)


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


def test_apply_translations_escapes_tabwidget_ampersands(qt_app):
    root = QWidget()
    tabs = QTabWidget(root)
    tabs.addTab(QWidget(), "Sources & Audio")

    apply_translations(root)

    assert tabs.tabText(0) == "Sources && Audio"
