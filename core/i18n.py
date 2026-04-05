"""
core/i18n.py — Localisation JSON légère pour l'UI et les messages maîtrisés.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from PySide6.QtCore import QLocale, Qt
from PySide6.QtWidgets import (
    QAbstractButton,
    QComboBox,
    QGroupBox,
    QLineEdit,
    QPlainTextEdit,
    QTabWidget,
    QTableWidget,
    QTextEdit,
    QWidget,
)

from core.lang_tags import Rfc5646LanguageTags


_LOCALES_PATH = Path(__file__).parent.parent / "locales.json"
_FALLBACK_LANGUAGE = "eng"
_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
_HEADER_SOURCE_ROLE = int(Qt.ItemDataRole.UserRole) + 913
_COMBO_SOURCE_ROLE = int(Qt.ItemDataRole.UserRole) + 914

_current_language = _FALLBACK_LANGUAGE
_catalog_cache: dict[str, dict[str, str]] | None = None


class _SafeFormatDict(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _catalog() -> dict[str, dict[str, str]]:
    global _catalog_cache
    if _catalog_cache is not None:
        return _catalog_cache
    if not _LOCALES_PATH.exists():
        _catalog_cache = {}
        return _catalog_cache
    data = json.loads(_LOCALES_PATH.read_text(encoding="utf-8"))
    _catalog_cache = {
        str(key): {str(lang).lower(): str(value) for lang, value in values.items()}
        for key, values in data.items()
        if isinstance(values, dict)
    }
    return _catalog_cache


def refresh_catalog() -> None:
    global _catalog_cache
    _catalog_cache = None
    _pattern_templates.cache_clear()
    _compiled_pattern.cache_clear()


def normalize_language(code: str | None) -> str:
    if not code:
        return _FALLBACK_LANGUAGE
    raw = code.strip()
    if not raw:
        return _FALLBACK_LANGUAGE
    if len(raw) == 3:
        ietf = Rfc5646LanguageTags.from_iso639_2(raw)
        if ietf:
            canonical = Rfc5646LanguageTags.to_iso639_2(ietf) or raw.lower()
            return canonical.lower()
    normalized = Rfc5646LanguageTags.from_locale_name(raw)
    return normalized or _FALLBACK_LANGUAGE


def set_current_language(code: str | None) -> str:
    global _current_language
    _current_language = normalize_language(code)
    return _current_language


def current_language() -> str:
    return _current_language


def available_languages() -> list[tuple[str, str]]:
    codes: set[str] = set()
    for values in _catalog().values():
        codes.update(str(code).lower() for code in values)

    normalized_codes: set[str] = set()
    for code in codes:
        normalized = normalize_language(code)
        if len(normalized) == 3 and Rfc5646LanguageTags.from_iso639_2(normalized):
            normalized_codes.add(normalized)

    if not normalized_codes:
        normalized_codes = {"fra", "eng"}

    items: list[tuple[str, str]] = []
    for code in sorted(normalized_codes):
        ietf = Rfc5646LanguageTags.from_iso639_2(code) or code
        locale = QLocale(ietf)
        native = locale.nativeLanguageName().strip()
        if code == "eng":
            native = "English"
        elif not native:
            native = QLocale.languageToString(locale.language()).strip()
        if not native:
            native = Rfc5646LanguageTags.iso639_2_name(code) or code
        name = native[:1].upper() + native[1:] if native else code
        items.append((code, name))

    return sorted(items, key=lambda item: item[1].lower())


def _lookup_template(template: str, language: str) -> str:
    values = _catalog().get(template)
    if not values:
        return template
    return (
        values.get(language)
        or values.get(_FALLBACK_LANGUAGE)
        or values.get("fra")
        or next(iter(values.values()), template)
    )


@lru_cache(maxsize=512)
def _compiled_pattern(template: str) -> re.Pattern[str] | None:
    matches = list(_PLACEHOLDER_RE.finditer(template))
    if not matches:
        return None

    parts: list[str] = []
    cursor = 0
    for match in matches:
        parts.append(re.escape(template[cursor:match.start()]))
        parts.append(f"(?P<{match.group(1)}>.+?)")
        cursor = match.end()
    parts.append(re.escape(template[cursor:]))
    return re.compile("^" + "".join(parts) + "$")


@lru_cache(maxsize=1)
def _pattern_templates() -> tuple[str, ...]:
    return tuple(
        template
        for template in _catalog()
        if _compiled_pattern(template) is not None
    )


def translate_text(text: str, language: str | None = None, **kwargs: Any) -> str:
    if not text:
        return text

    lang = normalize_language(language or _current_language)

    if kwargs:
        template = _lookup_template(text, lang)
        payload = _SafeFormatDict({k: str(v) for k, v in kwargs.items()})
        return template.format_map(payload)

    if text in _catalog():
        return _lookup_template(text, lang)

    for template in _pattern_templates():
        pattern = _compiled_pattern(template)
        if pattern is None:
            continue
        match = pattern.fullmatch(text)
        if match is None:
            continue
        translated = _lookup_template(template, lang)
        payload = _SafeFormatDict(match.groupdict())
        return translated.format_map(payload)

    return text


def _translated_source(widget: QWidget, property_name: str, current_value: str) -> str:
    prop_key = f"_i18n_source_{property_name}"
    source = widget.property(prop_key)
    if source is None:
        widget.setProperty(prop_key, current_value)
        source = current_value
    return str(source)


def _translate_widget_property(
    widget: QWidget,
    getter_name: str,
    setter_name: str,
    property_name: str,
) -> None:
    getter = getattr(widget, getter_name, None)
    setter = getattr(widget, setter_name, None)
    if getter is None or setter is None:
        return
    current = getter()
    if not isinstance(current, str) or not current:
        return
    source = _translated_source(widget, property_name, current)
    setter(translate_text(source))


def _translate_table_headers(table: QTableWidget) -> None:
    for column in range(table.columnCount()):
        item = table.horizontalHeaderItem(column)
        if item is None:
            continue
        source = item.data(_HEADER_SOURCE_ROLE) or item.text()
        if not source:
            continue
        item.setData(_HEADER_SOURCE_ROLE, source)
        item.setText(translate_text(str(source)))


def _translate_combo_items(combo: QComboBox) -> None:
    for index in range(combo.count()):
        source = combo.itemData(index, _COMBO_SOURCE_ROLE) or combo.itemText(index)
        if not source:
            continue
        combo.setItemData(index, source, _COMBO_SOURCE_ROLE)
        combo.setItemText(index, translate_text(str(source)))


def _translate_tab_titles(tabs: QTabWidget) -> None:
    prop_key = "_i18n_tab_sources"
    count = tabs.count()
    sources_prop = tabs.property(prop_key)
    sources = list(sources_prop) if isinstance(sources_prop, list) else []

    if len(sources) < count:
        for index in range(len(sources), count):
            sources.append(tabs.tabText(index))
    elif len(sources) > count:
        sources = sources[:count]

    for index in range(count):
        source = sources[index] or tabs.tabText(index)
        if not source:
            continue
        sources[index] = source
        tabs.setTabText(index, translate_text(str(source)))

    tabs.setProperty(prop_key, sources)


def apply_translations(root: QWidget) -> None:
    widgets = [root]
    widgets.extend(root.findChildren(QWidget))

    for widget in widgets:
        _translate_widget_property(widget, "windowTitle", "setWindowTitle", "window_title")
        _translate_widget_property(widget, "toolTip", "setToolTip", "tool_tip")

        if isinstance(widget, QAbstractButton):
            _translate_widget_property(widget, "text", "setText", "text")

        if isinstance(widget, QGroupBox):
            _translate_widget_property(widget, "title", "setTitle", "title")

        if widget.__class__.__name__ == "QLabel":
            _translate_widget_property(widget, "text", "setText", "text")

        if isinstance(widget, QLineEdit):
            _translate_widget_property(widget, "placeholderText", "setPlaceholderText", "placeholder")

        if isinstance(widget, (QPlainTextEdit, QTextEdit)):
            _translate_widget_property(widget, "placeholderText", "setPlaceholderText", "placeholder")

        if isinstance(widget, QComboBox):
            _translate_combo_items(widget)

        if isinstance(widget, QTabWidget):
            _translate_tab_titles(widget)

        if isinstance(widget, QTableWidget):
            _translate_table_headers(widget)
