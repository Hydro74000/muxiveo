"""
ui/panels/settings_panel.py — Éditeur complet de config.ini, une section par onglet.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
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
    QPushButton,
    QScrollArea,
    QSlider,
    QSizePolicy,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from core.config import AppConfig, AUDIO_BITRATE_STEPS, INI_FIELD_GROUPS, write_ini_settings
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
    _separator,
)
from ui.design_system import font_px as _font_px, scale as _scale
from ui.styles import _slider_style


def _spin_style() -> str:
    return (
        f"QSpinBox{{background:{_C.BG_CARD};color:{_C.TEXT_PRI};"
        f"border:1px solid {_C.BORDER};border-radius:{_scale(5)}px;"
        f"padding:{_scale(4)}px {_scale(10)}px;font-size:{_font_px(11)}px;}}"
        f"QSpinBox:focus{{border-color:{_C.ACCENT};}}"
        f"QSpinBox::up-button,QSpinBox::down-button{{width:{_scale(18)}px;border:none;background:{_C.BG_HOVER};}}"
    )


def _snap_slider_value(value: int, minimum: int, maximum: int, step: int) -> int:
    clamped = max(minimum, min(maximum, value))
    snapped = minimum + round((clamped - minimum) / step) * step
    return max(minimum, min(maximum, snapped))


# Case à cocher maîtresse → champ activé seulement quand elle est cochée.
_DEPENDENT_FIELDS: dict[tuple[str, str], tuple[str, str]] = {
    ("ui", "enable_file_logging"): ("ui", "file_logging_level"),
    ("paths", "release_group_tag_enabled"): ("paths", "release_group_tag"),
}

# Ordre des onglets ; sections absentes : après, dans l'ordre de INI_FIELD_GROUPS.
_SECTION_ORDER = {"ui": 0, "audio_encoding": 1, "sync": 2, "metadata": 3}


def _dirty_icon() -> QIcon:
    """Pastille des onglets modifiés non enregistrés."""
    size = _scale(8)
    pixmap = QPixmap(size * 2, size * 2)
    pixmap.setDevicePixelRatio(2.0)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(_C.WARN))
    painter.drawEllipse(0, 0, size, size)
    painter.end()
    return QIcon(pixmap)


class SettingsPanel(QWidget):
    """Réglages de config.ini, une section par onglet.

    Chaque onglet s'enregistre à part ; quitter un onglet (ou la page) avec des
    modifications non enregistrées propose de les enregistrer ou de les ignorer.
    """

    settings_saved = Signal()

    def __init__(self, config: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self._field_widgets: dict[tuple[str, str], QWidget] = {}
        self._slider_value_labels: dict[tuple[str, str], QLabel] = {}
        self._status_label: QLabel | None = None
        self._save_btn: QPushButton | None = None
        self._groups = sorted(INI_FIELD_GROUPS, key=lambda group: _SECTION_ORDER.get(group["section"], len(_SECTION_ORDER)))
        self._group_by_section = {group["section"]: group for group in self._groups}
        self._tab_sections = [group["section"] for group in self._groups]
        # Valeurs enregistrées par section : référence des modifications en cours.
        self._baseline: dict[str, dict[str, str]] = {}
        # Valeur enregistrée qu'un champ ne sait pas afficher (curseur à paliers, option
        # inconnue, borne) : conservée telle quelle tant que l'utilisateur n'y touche pas.
        self._raw_values: dict[tuple[str, str], str] = {}
        self._applying = False
        self._resolving = False
        self._leaving = False
        self._current_tab = 0
        self._dirty_icon = _dirty_icon()
        self._build_ui()
        self._load_from_config()
        apply_translations(self)
        self._tabs.currentChanged.connect(self._on_tab_changed)

    def widget_for(self, section: str, key: str) -> QWidget:
        return self._field_widgets[(section, key)]

    def _build_ui(self) -> None:
        self.setStyleSheet(f"background:{_C.BG_DEEP};")
        root = QVBoxLayout(self)
        root.setContentsMargins(_scale(28), _scale(24), _scale(28), _scale(20))
        root.setSpacing(_scale(12))

        title = QLabel("Réglages")
        title.setStyleSheet(
            f"font-size:{_font_px(20)}px;font-weight:800;color:{_C.TEXT_PRI};"
            f"background:transparent;letter-spacing:-{_scale(1)}px;"
        )
        subtitle = QLabel(
            "Modifiez toutes les valeurs persistées dans config.ini. "
            "Les changements de langue sont appliqués aux textes maîtrisés "
            "par l'application ; un redémarrage reste conseillé pour repartir sur un état propre."
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:{_font_px(12)}px;background:transparent;")
        root.addWidget(title)
        root.addWidget(subtitle)

        self._tabs = QTabWidget()
        self._tabs.setDocumentMode(True)
        self._tabs.setUsesScrollButtons(True)
        self._tabs.setElideMode(Qt.TextElideMode.ElideNone)
        self._tabs.setIconSize(QSize(_scale(8), _scale(8)))
        self._tabs.tabBar().setDrawBase(False)
        self._tabs.setStyleSheet(
            f"QTabWidget::pane{{border:none;background:transparent;}}"
            f"QTabBar::tab{{background:transparent;color:{_C.TEXT_SEC};padding:{_scale(6)}px {_scale(12)}px;"
            f"border:none;border-bottom:2px solid transparent;font-size:{_font_px(11)}px;}}"
            f"QTabBar::tab:selected{{color:{_C.TEXT_PRI};border-bottom:2px solid {_C.ACCENT};}}"
            f"QTabBar::tab:hover{{color:{_C.TEXT_PRI};}}"
        )
        for group in self._groups:
            self._tabs.addTab(self._build_tab_page(group), group["title"])
        root.addWidget(self._tabs, stretch=1)

        actions = QHBoxLayout()
        actions.setSpacing(_scale(12))
        reload_btn = _secondary_button("Recharger depuis config.ini")
        reload_btn.clicked.connect(self._on_reload_clicked)
        rerun_setup_btn = _secondary_button("Relancer le setup")
        rerun_setup_btn.clicked.connect(self._on_rerun_setup_clicked)
        self._save_btn = _primary_button("Enregistrer")
        self._save_btn.setEnabled(False)
        self._save_btn.clicked.connect(self._on_save_clicked)

        self._status_label = QLabel("")
        # Messages déjà traduits à l'affichage : la traduction automatique réafficherait le premier.
        self._status_label.setProperty("_i18n_skip", True)
        self._status_label.setWordWrap(True)
        self._status_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._status_label.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:{_font_px(11)}px;background:transparent;")

        actions.addWidget(self._status_label, stretch=1)
        actions.addWidget(reload_btn)
        actions.addWidget(rerun_setup_btn)
        actions.addWidget(self._save_btn)
        root.addLayout(actions)

        for key, widget in self._field_widgets.items():
            self._watch_field(key, widget)

    def _build_tab_page(self, group: dict[str, Any]) -> QScrollArea:
        """Page d'un onglet : les champs de la section dans une carte, défilante."""
        section = group["section"]
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet(
            f"QScrollArea{{background:{_C.BG_DEEP};border:none;}}"
            f"QScrollBar:vertical{{background:{_C.BG_DEEP};width:{_scale(6)}px;border:none;}}"
            f"QScrollBar::handle:vertical{{background:{_C.BORDER_LT};border-radius:{_scale(3)}px;min-height:{_scale(24)}px;}}"
            f"QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{{height:0;}}"
        )

        content = QWidget()
        content.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        content.setStyleSheet(f"background:{_C.BG_DEEP};")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, _scale(12), _scale(8), _scale(4))
        layout.setSpacing(0)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinAndMaxSize)

        card = _card()
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(_scale(16), _scale(16), _scale(16), _scale(16))
        card_layout.setSpacing(_scale(14))
        for index, field in enumerate(group["fields"]):
            card_layout.addWidget(self._build_field_widget(section, field))
            if index < len(group["fields"]) - 1:
                card_layout.addWidget(_separator())
        layout.addWidget(card)

        scroll.setWidget(content)
        return scroll

    def _build_field_widget(self, section: str, field: dict[str, Any]) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(_scale(8))

        kind = field["kind"]
        tooltip = str(field.get("tooltip") or "").strip()
        if kind == "bool":
            checkbox = QCheckBox(field["label"])
            checkbox.setObjectName(f"{section}.{field['key']}")
            checkbox.setStyleSheet(_checkbox_style())
            if tooltip:
                checkbox.setToolTip(tooltip)
            if (section, field["key"]) in _DEPENDENT_FIELDS:
                checkbox.toggled.connect(self._sync_dependent_field_states)
            layout.addWidget(checkbox)
            self._field_widgets[(section, field["key"])] = checkbox
        else:
            label = QLabel(field["label"])
            label.setStyleSheet(
                f"color:{_C.TEXT_PRI};font-size:{_font_px(12)}px;font-weight:600;background:transparent;"
            )
            if tooltip:
                label.setToolTip(tooltip)
            layout.addWidget(label)

            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(_scale(8))
            widget = self._make_editor(section, field)
            if tooltip:
                widget.setToolTip(tooltip)
            row.addWidget(widget, stretch=1)
            if kind in {"directory", "tool"}:
                browse_btn = _secondary_button("Parcourir…")
                browse_btn.clicked.connect(lambda _=False, s=section, f=field: self._browse_for_field(s, f))
                if tooltip:
                    browse_btn.setToolTip(tooltip)
                row.addWidget(browse_btn)
            layout.addLayout(row)

        desc = QLabel(field["description"])
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:{_font_px(11)}px;background:transparent;")
        if tooltip:
            desc.setToolTip(tooltip)
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
            if field["key"] == "ui_scale_percent":
                slider_wrap = QWidget()
                slider_layout = QHBoxLayout(slider_wrap)
                slider_layout.setContentsMargins(0, 0, 0, 0)
                slider_layout.setSpacing(_scale(10))

                slider = QSlider(Qt.Orientation.Horizontal)
                slider.setObjectName(f"{section}.{field['key']}")
                slider_min = int(field.get("min", 0))
                slider_max = int(field.get("max", 1_000_000))
                slider_step = 10
                slider.setRange(slider_min, slider_max)
                slider.setSingleStep(slider_step)
                slider.setPageStep(slider_step)
                slider.setTickPosition(QSlider.TickPosition.TicksBelow)
                slider.setTickInterval(slider_step)
                slider.setStyleSheet(_slider_style())
                slider_layout.addWidget(slider, stretch=1)

                value_label = QLabel("")
                value_label.setProperty("_i18n_skip", True)
                value_label.setMinimumWidth(_scale(52))
                value_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                value_label.setStyleSheet(
                    f"color:{_C.TEXT_SEC};font-size:{_font_px(11)}px;font-family:'JetBrains Mono',monospace;background:transparent;"
                )
                def _sync_slider(value: int, *, s=slider, lbl=value_label, minimum=slider_min, maximum=slider_max, step=slider_step) -> None:
                    snapped = _snap_slider_value(value, minimum, maximum, step)
                    if snapped != value:
                        s.blockSignals(True)
                        s.setValue(snapped)
                        s.blockSignals(False)
                    lbl.setText(f"{snapped}%")

                slider.valueChanged.connect(_sync_slider)
                slider_layout.addWidget(value_label)

                self._field_widgets[key] = slider
                self._slider_value_labels[key] = value_label
                return slider_wrap

            spin = QSpinBox()
            spin.setObjectName(f"{section}.{field['key']}")
            spin.setRange(int(field.get("min", 0)), int(field.get("max", 1_000_000)))
            if "single_step" in field:
                spin.setSingleStep(int(field["single_step"]))
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

        if kind == "stepped_slider":
            steps: list[int] = list(field.get("steps", AUDIO_BITRATE_STEPS))
            slider_wrap = QWidget()
            slider_layout = QHBoxLayout(slider_wrap)
            slider_layout.setContentsMargins(0, 0, 0, 0)
            slider_layout.setSpacing(_scale(10))

            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setObjectName(f"{section}.{field['key']}")
            slider.setRange(0, len(steps) - 1)
            slider.setSingleStep(1)
            slider.setPageStep(1)
            slider.setTickPosition(QSlider.TickPosition.TicksBelow)
            slider.setTickInterval(1)
            slider.setStyleSheet(_slider_style())
            slider.setProperty("steps", steps)
            slider_layout.addWidget(slider, stretch=1)

            value_label = QLabel("")
            value_label.setProperty("_i18n_skip", True)
            value_label.setMinimumWidth(_scale(70))
            value_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            value_label.setStyleSheet(
                f"color:{_C.TEXT_SEC};font-size:{_font_px(11)}px;font-family:'JetBrains Mono',monospace;background:transparent;"
            )

            steps_tuple = tuple(steps)

            def _sync_stepped(idx: int, *, s=slider, lbl=value_label, st=steps_tuple) -> None:
                clamped = max(0, min(idx, len(st) - 1))
                if clamped != idx:
                    s.blockSignals(True)
                    s.setValue(clamped)
                    s.blockSignals(False)
                lbl.setText(f"{st[clamped]} kbps")

            slider.valueChanged.connect(_sync_stepped)
            slider_layout.addWidget(value_label)

            self._field_widgets[key] = slider
            self._slider_value_labels[key] = value_label
            return slider_wrap

        raise ValueError(f"Unsupported settings field kind: {kind}")

    def _field_value(self, section: str, field: dict[str, Any]) -> str:
        """Valeur du champ : celle affichée, ou la valeur enregistrée que le champ ne sait pas afficher."""
        raw = self._raw_values.get((section, field["key"]))
        return raw if raw is not None else self._widget_value(section, field)

    def _widget_value(self, section: str, field: dict[str, Any]) -> str:
        widget = self._field_widgets[(section, field["key"])]
        if isinstance(widget, QLineEdit):
            return widget.text().strip()
        if isinstance(widget, QCheckBox):
            return "true" if widget.isChecked() else "false"
        if isinstance(widget, QSlider):
            steps = widget.property("steps")
            if steps:
                idx = max(0, min(widget.value(), len(steps) - 1))
                return str(steps[idx])
            return str(widget.value())
        if isinstance(widget, QSpinBox):
            return str(widget.value())
        if isinstance(widget, QComboBox):
            data = widget.currentData()
            return str(data if data is not None else widget.currentText())
        raise TypeError(f"Unsupported widget type: {type(widget)!r}")

    def _sync_dependent_field_states(self) -> None:
        """Active chaque champ dépendant selon l'état de sa case à cocher maîtresse."""
        for master_key, dependent_key in _DEPENDENT_FIELDS.items():
            checkbox = self._field_widgets.get(master_key)
            dependent = self._field_widgets.get(dependent_key)
            if isinstance(checkbox, QCheckBox) and dependent is not None:
                dependent.setEnabled(checkbox.isChecked())

    def _load_from_config(self) -> None:
        for group in self._groups:
            section = group["section"]
            for field in group["fields"]:
                self._load_field(section, field)
            self._baseline[section] = self._section_values(section)

        self._sync_dependent_field_states()
        self._refresh_dirty_state()
        if self._status_label is not None:
            self._status_label.clear()

    def _load_field(self, section: str, field: dict[str, Any]) -> None:
        self._apply_value(section, field, getattr(self._config, field["attr"]))

    def _apply_value(self, section: str, field: dict[str, Any], value: Any) -> None:
        """Affiche ``value`` (valeur de la config ou chaîne relevée par ``_field_value``), sans perte."""
        key = (section, field["key"])
        self._applying = True
        try:
            self._show_value(section, field, value)
        finally:
            self._applying = False
        raw = "true" if value is True else "false" if value is False else str(value).strip()
        if self._widget_value(section, field) == raw:
            self._raw_values.pop(key, None)
        else:
            self._raw_values[key] = raw
        # Libellé toujours réécrit : la position peut rester la même pour une autre valeur.
        label = self._slider_value_labels.get(key)
        widget = self._field_widgets[key]
        if label is not None and isinstance(widget, QSlider):
            shown = self._field_value(section, field)
            label.setText(f"{shown} kbps" if widget.property("steps") else f"{shown}%")

    def _show_value(self, section: str, field: dict[str, Any], value: Any) -> None:
        widget = self._field_widgets[(section, field["key"])]

        if isinstance(widget, QLineEdit):
            widget.setText(str(value))
        elif isinstance(widget, QCheckBox):
            widget.setChecked(value if isinstance(value, bool) else str(value).strip().lower() == "true")
        elif isinstance(widget, QSlider):
            steps = widget.property("steps")
            if steps:
                try:
                    idx = steps.index(int(value))
                except ValueError:
                    idx = min(range(len(steps)), key=lambda i: abs(steps[i] - int(value)))
                widget.setValue(idx)
            else:
                widget.setValue(int(value))
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
            # Option inconnue : aucune sélection plutôt qu'une option qui n'est pas la valeur.
            widget.setCurrentIndex(index)

    def refresh_field(self, section: str, key: str) -> None:
        """Recharge un seul champ depuis la config (réglage modifié hors du panneau), sans toucher aux autres saisies."""
        group = self._group_by_section.get(section)
        if group is None:
            return
        for field in group["fields"]:
            if field["key"] == key:
                self._load_field(section, field)
                if section in self._baseline:
                    self._baseline[section][key] = self._field_value(section, field)
                self._sync_dependent_field_states()
                self._refresh_dirty_state()
                return

    # ------------------------------------------------------------------
    # Modifications en cours
    # ------------------------------------------------------------------

    def _watch_field(self, key: tuple[str, str], widget: QWidget) -> None:
        def edited(*_args: object) -> None:
            self._on_field_edited(key)

        if isinstance(widget, QLineEdit):
            widget.textChanged.connect(edited)
        elif isinstance(widget, QCheckBox):
            widget.toggled.connect(edited)
        elif isinstance(widget, (QSlider, QSpinBox)):
            widget.valueChanged.connect(edited)
        elif isinstance(widget, QComboBox):
            widget.currentIndexChanged.connect(edited)

    def _on_field_edited(self, key: tuple[str, str]) -> None:
        if not self._applying:
            # Champ modifié par l'utilisateur : sa valeur affichée devient la valeur.
            self._raw_values.pop(key, None)
        self._refresh_dirty_state()

    def _section_values(self, section: str) -> dict[str, str]:
        group = self._group_by_section.get(section)
        if group is None:
            return {}
        return {field["key"]: self._field_value(section, field) for field in group["fields"]}

    def _section_dirty(self, section: str) -> bool:
        baseline = self._baseline.get(section)
        return baseline is not None and self._section_values(section) != baseline

    def dirty_sections(self) -> list[str]:
        """Sections dont les valeurs affichées diffèrent des valeurs enregistrées."""
        return [section for section in self._tab_sections if self._section_dirty(section)]

    def has_unsaved_changes(self) -> bool:
        return bool(self.dirty_sections())

    def _refresh_dirty_state(self) -> None:
        tabs = getattr(self, "_tabs", None)
        if tabs is None:
            return
        dirty = set(self.dirty_sections())
        for index, section in enumerate(self._tab_sections):
            tabs.setTabIcon(index, self._dirty_icon if section in dirty else QIcon())
        if self._save_btn is not None:
            self._save_btn.setEnabled(bool(dirty))

    def _on_tab_changed(self, index: int) -> None:
        previous = self._current_tab
        if index == previous:
            return
        allowed = False
        try:
            allowed = self._resolve_pending([self._tab_sections[previous]])
        finally:
            if allowed:
                self._current_tab = index
            else:
                # Annulé, enregistrement refusé ou en échec : on reste sur l'onglet modifié.
                self._tabs.blockSignals(True)
                self._tabs.setCurrentIndex(previous)
                self._tabs.blockSignals(False)

    def confirm_leave(self) -> bool:
        """Vrai si la page peut être quittée : rien à enregistrer, enregistré ou ignoré."""
        previous, self._leaving = self._leaving, True
        try:
            return self._resolve_pending(self._tab_sections)
        finally:
            self._leaving = previous

    def is_leaving(self) -> bool:
        """Vrai pendant la question posée avant une autre action (navigation, fermeture, mise à jour, setup)."""
        return self._leaving

    def _resolve_pending(self, sections: list[str]) -> bool:
        dirty = [section for section in sections if self._section_dirty(section)]
        if not dirty:
            return True
        if self._resolving:
            # Question déjà posée (onglet, navigation ou fermeture) : toute autre demande attend.
            return False
        self._resolving = True
        try:
            choice = self._ask_pending_changes(dirty)
            if choice == "save":
                return self._save_sections(dirty)
            if choice == "discard":
                self._discard_sections(dirty)
                return True
            return False
        finally:
            self._resolving = False

    def _ask_pending_changes(self, sections: list[str]) -> str:
        """Popup Enregistrer / Ignorer / Annuler ; renvoie ``save``, ``discard`` ou ``cancel``."""
        titles = ", ".join(translate_text(self._group_by_section[section]["title"]) for section in sections)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(translate_text("Modifications non enregistrées"))
        box.setText(
            translate_text("L'onglet « {tabs} » contient des modifications non enregistrées.", tabs=titles)
            if len(sections) == 1 else
            translate_text("Les onglets « {tabs} » contiennent des modifications non enregistrées.", tabs=titles)
        )
        box.setInformativeText(translate_text("Enregistrer les modifications ou les ignorer ?"))
        save_btn = box.addButton(translate_text("Enregistrer"), QMessageBox.ButtonRole.AcceptRole)
        discard_btn = box.addButton(translate_text("Ignorer"), QMessageBox.ButtonRole.DestructiveRole)
        cancel_btn = box.addButton(translate_text("Annuler"), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(save_btn)
        box.setEscapeButton(cancel_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is save_btn:
            return "save"
        if clicked is discard_btn:
            return "discard"
        return "cancel"

    def _discard_sections(self, sections: list[str]) -> None:
        for section in sections:
            baseline = self._baseline.get(section, {})
            for field in self._group_by_section[section]["fields"]:
                if field["key"] in baseline:
                    self._apply_value(section, field, baseline[field["key"]])
            self._baseline[section] = self._section_values(section)
        self._sync_dependent_field_states()
        self._refresh_dirty_state()
        if self._status_label is not None:
            self._status_label.setText(translate_text("Modifications ignorées."))

    # ------------------------------------------------------------------
    # Enregistrement
    # ------------------------------------------------------------------

    def _validate_values(self, values: dict[str, dict[str, str]]) -> bool:
        ui_values = values.get("ui", {})
        verbose_enabled = str(ui_values.get("enable_file_logging", "false")).strip().lower() == "true"
        verbose_dir = str(ui_values.get("verbose_log_dir", "")).strip()
        if verbose_enabled and not verbose_dir:
            message = translate_text(
                "Le dossier des logs fichier doit être renseigné quand l'option est activée."
            )
            if self._status_label is not None:
                self._status_label.setText(message)
            QMessageBox.warning(
                self,
                translate_text("Réglage invalide"),
                message,
            )
            return False
        return True

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
        self._save_sections(self.dirty_sections())

    def _save_sections(self, sections: list[str]) -> bool:
        """Écrit les sections dans config.ini ; faux si la validation refuse ou si l'écriture échoue."""
        if not sections:
            return True
        values = {section: self._section_values(section) for section in sections}
        if not self._validate_values(values):
            return False
        try:
            write_ini_settings(values)
        except OSError as exc:
            message = translate_text("Impossible d'enregistrer les paramètres : {error}", error=str(exc))
            if self._status_label is not None:
                self._status_label.setText(message)
            QMessageBox.warning(self, translate_text("Erreur"), message)
            return False
        self._config.reload()
        self._config.save()
        # Valeurs relues depuis la config : normalisations visibles, référence exacte.
        for section in sections:
            for field in self._group_by_section[section]["fields"]:
                self._load_field(section, field)
            self._baseline[section] = self._section_values(section)
        self._sync_dependent_field_states()
        self._refresh_dirty_state()
        self.settings_saved.emit()
        if self._status_label is not None:
            # Après l'application des réglages : message dans la langue choisie.
            titles = ", ".join(translate_text(self._group_by_section[section]["title"]) for section in sections)
            self._status_label.setText(
                translate_text("Section « {title} » enregistrée dans config.ini.", title=titles)
                if len(sections) == 1 else
                translate_text("Sections « {titles} » enregistrées dans config.ini.", titles=titles)
            )
        return True

    def _on_rerun_setup_clicked(self) -> None:
        # Le setup réécrit config.ini et peut redémarrer l'application : saisies tranchées avant.
        if not self.confirm_leave():
            return
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

        self._config.reload()
        self._load_from_config()
        self.settings_saved.emit()
        if self._status_label is not None:
            self._status_label.setText(
                translate_text(
                    "Setup relancé avec succès. Un redémarrage de l'application est recommandé."
                )
            )

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
