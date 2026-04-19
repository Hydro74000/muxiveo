"""
ui/panels/settings_panel.py — Éditeur complet de config.ini.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QLayout,
    QMessageBox,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from core.config import AppConfig, INI_FIELD_GROUPS, write_ini_settings
from core.i18n import apply_translations, available_languages, translate_text
from core.lang_tags import Rfc5646LanguageTags
from ui.panels.encode_panel.theme import (
    _C,
    _card,
    _checkbox_style,
    _combo_style,
    _input_style,
    _primary_button,
    _secondary_button,
    _section_label,
    _separator,
)


def _spin_style() -> str:
    return (
        f"QSpinBox{{background:{_C.BG_CARD};color:{_C.TEXT_PRI};"
        f"border:1px solid {_C.BORDER};border-radius:5px;padding:4px 10px;font-size:11px;}}"
        f"QSpinBox:focus{{border-color:{_C.ACCENT};}}"
        f"QSpinBox::up-button,QSpinBox::down-button{{width:18px;border:none;background:{_C.BG_HOVER};}}"
    )


class SettingsPanel(QWidget):
    settings_saved = Signal()

    def __init__(self, config: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self._field_widgets: dict[tuple[str, str], QWidget] = {}
        self._status_label: QLabel | None = None
        self._build_ui()
        self._load_from_config()
        apply_translations(self)

    def widget_for(self, section: str, key: str) -> QWidget:
        return self._field_widgets[(section, key)]

    def _build_ui(self) -> None:
        self.setStyleSheet(f"background:{_C.BG_DEEP};")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet(
            f"QScrollArea{{background:{_C.BG_DEEP};border:none;}}"
            f"QScrollBar:vertical{{background:{_C.BG_DEEP};width:6px;border:none;}}"
            f"QScrollBar::handle:vertical{{background:{_C.BORDER_LT};border-radius:3px;min-height:24px;}}"
            f"QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{{height:0;}}"
        )

        content = QWidget()
        content.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        content.setStyleSheet(f"background:{_C.BG_DEEP};")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(20)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinAndMaxSize)

        title = QLabel("Réglages")
        title.setStyleSheet(
            f"font-size:20px;font-weight:800;color:{_C.TEXT_PRI};"
            f"background:transparent;letter-spacing:-0.3px;"
        )
        subtitle = QLabel(
            "Modifiez toutes les valeurs persistées dans config.ini. "
            "Les changements de langue sont appliqués aux textes maîtrisés "
            "par l'application ; un redémarrage reste conseillé pour repartir sur un état propre."
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:12px;background:transparent;")
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addWidget(_separator())

        _section_order = {"ui": 0, "metadata": 1, "paths": 2}
        groups = sorted(INI_FIELD_GROUPS, key=lambda group: _section_order.get(group["section"], 3))
        for group in groups:
            layout.addWidget(_section_label(group["title"].upper()))
            layout.addWidget(self._build_group_card(group))

        actions = QHBoxLayout()
        actions.setSpacing(12)
        reload_btn = _secondary_button("Recharger depuis config.ini")
        reload_btn.clicked.connect(self._on_reload_clicked)
        rerun_setup_btn = _secondary_button("Relancer le setup")
        rerun_setup_btn.clicked.connect(self._on_rerun_setup_clicked)
        save_btn = _primary_button("Sauvegarder toute la configuration")
        save_btn.clicked.connect(self._on_save_clicked)

        self._status_label = QLabel("")
        self._status_label.setWordWrap(True)
        self._status_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._status_label.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:11px;background:transparent;")

        actions.addWidget(self._status_label, stretch=1)
        actions.addWidget(reload_btn)
        actions.addWidget(rerun_setup_btn)
        actions.addWidget(save_btn)
        layout.addLayout(actions)

        scroll.setWidget(content)
        root.addWidget(scroll, stretch=1)

    def _build_group_card(self, group: dict[str, Any]) -> QWidget:
        section = group["section"]
        section_title = group["title"]
        card = _card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(14)

        for index, field in enumerate(group["fields"]):
            layout.addWidget(self._build_field_widget(section, field))
            if index < len(group["fields"]) - 1:
                layout.addWidget(_separator())

        layout.addWidget(_separator())
        save_row = QHBoxLayout()
        save_row.setContentsMargins(0, 0, 0, 0)
        save_row.setSpacing(8)
        save_row.addStretch()
        save_btn = _secondary_button("Enregistrer")
        save_btn.clicked.connect(
            lambda _=False, sec=section, title=section_title: self._on_save_section(sec, title)
        )
        save_row.addWidget(save_btn)
        layout.addLayout(save_row)

        return card

    def _build_field_widget(self, section: str, field: dict[str, Any]) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        kind = field["kind"]
        if kind == "bool":
            checkbox = QCheckBox(field["label"])
            checkbox.setObjectName(f"{section}.{field['key']}")
            checkbox.setStyleSheet(_checkbox_style())
            layout.addWidget(checkbox)
            self._field_widgets[(section, field["key"])] = checkbox
        else:
            label = QLabel(field["label"])
            label.setStyleSheet(f"color:{_C.TEXT_PRI};font-size:12px;font-weight:600;background:transparent;")
            layout.addWidget(label)

            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(8)
            widget = self._make_editor(section, field)
            row.addWidget(widget, stretch=1)
            if kind in {"directory", "tool"}:
                browse_btn = _secondary_button("Parcourir…")
                browse_btn.clicked.connect(lambda _=False, s=section, f=field: self._browse_for_field(s, f))
                row.addWidget(browse_btn)
            layout.addLayout(row)

        desc = QLabel(field["description"])
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:11px;background:transparent;")
        layout.addWidget(desc)
        return container

    def _make_editor(self, section: str, field: dict[str, Any]) -> QWidget:
        kind = field["kind"]
        key = (section, field["key"])

        if kind in {"directory", "tool", "text"}:
            edit = QLineEdit()
            edit.setObjectName(f"{section}.{field['key']}")
            edit.setStyleSheet(_input_style())
            self._field_widgets[key] = edit
            return edit

        if kind == "int":
            spin = QSpinBox()
            spin.setObjectName(f"{section}.{field['key']}")
            spin.setRange(0, 1_000_000)
            spin.setStyleSheet(_spin_style())
            self._field_widgets[key] = spin
            return spin

        if kind == "choice":
            combo = QComboBox()
            combo.setObjectName(f"{section}.{field['key']}")
            combo.setStyleSheet(_combo_style())
            for value, label in field.get("options", ()): 
                combo.addItem(label, value)
            self._field_widgets[key] = combo
            return combo

        if kind == "language":
            combo = QComboBox()
            combo.setObjectName(f"{section}.{field['key']}")
            combo.setStyleSheet(_combo_style())
            for code, name in available_languages():
                combo.addItem(name, code)
            self._field_widgets[key] = combo
            return combo

        raise ValueError(f"Unsupported settings field kind: {kind}")

    def _field_value(self, section: str, field: dict[str, Any]) -> str:
        widget = self._field_widgets[(section, field["key"])]
        if isinstance(widget, QLineEdit):
            return widget.text().strip()
        if isinstance(widget, QCheckBox):
            return "true" if widget.isChecked() else "false"
        if isinstance(widget, QSpinBox):
            return str(widget.value())
        if isinstance(widget, QComboBox):
            data = widget.currentData()
            return str(data if data is not None else widget.currentText())
        raise TypeError(f"Unsupported widget type: {type(widget)!r}")

    def _load_from_config(self) -> None:
        for group in INI_FIELD_GROUPS:
            section = group["section"]
            for field in group["fields"]:
                widget = self._field_widgets[(section, field["key"])]
                value = getattr(self._config, field["attr"])

                if isinstance(widget, QLineEdit):
                    widget.setText(str(value))
                elif isinstance(widget, QCheckBox):
                    widget.setChecked(bool(value))
                elif isinstance(widget, QSpinBox):
                    widget.setValue(int(value))
                elif isinstance(widget, QComboBox):
                    lookup = str(value)
                    index = widget.findData(lookup)
                    if index < 0 and field["kind"] == "language":
                        ietf = Rfc5646LanguageTags.from_iso639_2(lookup)
                        canonical = Rfc5646LanguageTags.to_iso639_2(ietf) if ietf else None
                        if canonical:
                            index = widget.findData(canonical)
                    if index >= 0:
                        widget.setCurrentIndex(index)

        if self._status_label is not None:
            self._status_label.clear()

    def _collect_values(self) -> dict[str, dict[str, str]]:
        values: dict[str, dict[str, str]] = {}
        for group in INI_FIELD_GROUPS:
            section = group["section"]
            values[section] = {}
            for field in group["fields"]:
                values[section][field["key"]] = self._field_value(section, field)
        return values

    def _collect_section_values(self, section: str) -> dict[str, dict[str, str]]:
        for group in INI_FIELD_GROUPS:
            if group["section"] != section:
                continue
            return {
                section: {
                    field["key"]: self._field_value(section, field)
                    for field in group["fields"]
                }
            }
        return {section: {}}

    def _browse_for_field(self, section: str, field: dict[str, Any]) -> None:
        widget = self._field_widgets[(section, field["key"])]
        if not isinstance(widget, QLineEdit):
            return

        current = widget.text().strip() or str(Path.home())
        if field["kind"] == "directory":
            selected = QFileDialog.getExistingDirectory(self, field["label"], current)
            if selected:
                widget.setText(selected)
            return

        selected, _ = QFileDialog.getOpenFileName(self, field["label"], current)
        if selected:
            widget.setText(selected)

    def _on_reload_clicked(self) -> None:
        self._config.reload()
        self._load_from_config()
        if self._status_label is not None:
            self._status_label.setText(translate_text("Configuration rechargée depuis config.ini."))

    def _on_save_clicked(self) -> None:
        write_ini_settings(self._collect_values())
        self._config.reload()
        self._config.save()
        if self._status_label is not None:
            self._status_label.setText(
                translate_text(
                    "Configuration enregistrée dans config.ini. Certains changements peuvent nécessiter de rouvrir les panneaux ou l'application."
                )
            )
        self.settings_saved.emit()

    def _on_save_section(self, section: str, section_title: str) -> None:
        write_ini_settings(self._collect_section_values(section))
        self._config.reload()
        self._config.save()
        if self._status_label is not None:
            self._status_label.setText(
                translate_text(
                    "Section « {title} » enregistrée dans config.ini.",
                    title=section_title,
                )
            )
        self.settings_saved.emit()

    def _on_rerun_setup_clicked(self) -> None:
        try:
            self._config.rerun_setup()
        except Exception as exc:
            if self._status_label is not None:
                self._status_label.setText(
                    translate_text("Erreur pendant la relance du setup : {exc}", exc=exc)
                )
            QMessageBox.warning(
                self,
                translate_text("Erreur"),
                translate_text("Impossible de relancer le setup : {exc}", exc=exc),
            )
            return

        if self._status_label is not None:
            self._status_label.setText(
                translate_text(
                    "Setup relancé avec succès. Un redémarrage de l'application est recommandé."
                )
            )
        self.settings_saved.emit()

        reply = QMessageBox.question(
            self,
            translate_text("Redémarrage recommandé"),
            translate_text("Le setup est terminé. Redémarrer l'application maintenant ?"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply == QMessageBox.StandardButton.Yes:
            restarted = self._config.restart_application()
            if not restarted:
                QMessageBox.warning(
                    self,
                    translate_text("Erreur"),
                    translate_text("Impossible de redémarrer automatiquement l'application."),
                )
