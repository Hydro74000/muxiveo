"""
ui/panels/settings_panel.py — Éditeur complet de config.ini.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
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
    QSlider,
    QSizePolicy,
    QSpinBox,
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
    _section_label,
    _separator,
)
from ui.design_system import font_px as _font_px, scale as _scale


def _spin_style() -> str:
    return (
        f"QSpinBox{{background:{_C.BG_CARD};color:{_C.TEXT_PRI};"
        f"border:1px solid {_C.BORDER};border-radius:{_scale(5)}px;"
        f"padding:{_scale(4)}px {_scale(10)}px;font-size:{_font_px(11)}px;}}"
        f"QSpinBox:focus{{border-color:{_C.ACCENT};}}"
        f"QSpinBox::up-button,QSpinBox::down-button{{width:{_scale(18)}px;border:none;background:{_C.BG_HOVER};}}"
    )


def _slider_style() -> str:
    handle = _scale(16)
    groove_h = _scale(6)
    radius = _scale(3)
    margin = _scale(6)
    return (
        f"QSlider::groove:horizontal{{background:{_C.BG_CARD};height:{groove_h}px;"
        f"border:1px solid {_C.BORDER};border-radius:{radius}px;}}"
        f"QSlider::sub-page:horizontal{{background:{_C.ACCENT};border-radius:{radius}px;}}"
        f"QSlider::add-page:horizontal{{background:{_C.BG_ACTIVE};border-radius:{radius}px;}}"
        f"QSlider::handle:horizontal{{background:{_C.TEXT_PRI};width:{handle}px;height:{handle}px;"
        f"margin:-{margin}px 0;border-radius:{_scale(8)}px;border:1px solid {_C.BORDER_LT};}}"
        f"QSlider::handle:horizontal:hover{{border-color:{_C.ACCENT};}}"
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


class SettingsPanel(QWidget):
    settings_saved = Signal()
    # Extension d'accélération NVIDIA (TensorRT), traitée par TrtPluginController.
    trt_install_requested = Signal()
    trt_update_requested = Signal()
    trt_remove_requested = Signal()
    trt_enabled_toggled = Signal(bool)
    trt_auto_update_toggled = Signal(bool)

    def __init__(self, config: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self._field_widgets: dict[tuple[str, str], QWidget] = {}
        self._slider_value_labels: dict[tuple[str, str], QLabel] = {}
        self._status_label: QLabel | None = None
        self._scroll: QScrollArea | None = None
        self._extensions_label: QLabel | None = None
        self._extensions_card: QWidget | None = None
        self._build_ui()
        self._load_from_config()
        self._sync_dependent_field_states()
        apply_translations(self)

    def widget_for(self, section: str, key: str) -> QWidget:
        return self._field_widgets[(section, key)]

    def _build_ui(self) -> None:
        self.setStyleSheet(f"background:{_C.BG_DEEP};")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        scroll = QScrollArea()
        self._scroll = scroll
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
        layout.setContentsMargins(_scale(28), _scale(24), _scale(28), _scale(24))
        layout.setSpacing(_scale(20))
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinAndMaxSize)

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
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addWidget(_separator())

        # Extensions : visibles seulement sur machine compatible (ou si déjà installées).
        self._extensions_label = _section_label("EXTENSIONS")
        self._extensions_card = self._build_extensions_card()
        self._extensions_label.hide()
        self._extensions_card.hide()
        layout.addWidget(self._extensions_label)
        layout.addWidget(self._extensions_card)

        _section_order = {"ui": 0, "audio_encoding": 1, "sync": 2, "metadata": 3, "paths": 4}
        groups = sorted(INI_FIELD_GROUPS, key=lambda group: _section_order.get(group["section"], 4))
        for group in groups:
            layout.addWidget(_section_label(group["title"].upper()))
            layout.addWidget(self._build_group_card(group))

        actions = QHBoxLayout()
        actions.setSpacing(_scale(12))
        reload_btn = _secondary_button("Recharger depuis config.ini")
        reload_btn.clicked.connect(self._on_reload_clicked)
        rerun_setup_btn = _secondary_button("Relancer le setup")
        rerun_setup_btn.clicked.connect(self._on_rerun_setup_clicked)
        save_btn = _primary_button("Sauvegarder toute la configuration")
        save_btn.clicked.connect(self._on_save_clicked)

        self._status_label = QLabel("")
        self._status_label.setWordWrap(True)
        self._status_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._status_label.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:{_font_px(11)}px;background:transparent;")

        actions.addWidget(self._status_label, stretch=1)
        actions.addWidget(reload_btn)
        actions.addWidget(rerun_setup_btn)
        actions.addWidget(save_btn)
        layout.addLayout(actions)

        scroll.setWidget(content)
        root.addWidget(scroll, stretch=1)

    def _build_extensions_card(self) -> QWidget:
        card = _card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(_scale(16), _scale(16), _scale(16), _scale(16))
        layout.setSpacing(_scale(10))
        title = QLabel("Accélération NVIDIA (TensorRT)")
        title.setStyleSheet(f"color:{_C.TEXT_PRI};font-size:{_font_px(13)}px;font-weight:700;background:transparent;")
        desc = QLabel(
            "Exécute l'interpolation MVO-RIFE sur les Tensor Cores des cartes NVIDIA (NVIDIA TensorRT for RTX) : "
            "traitement nettement plus rapide, images identiques à l'œil. Sans elle, l'interpolation fonctionne "
            "sur le GPU (Vulkan)."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:{_font_px(11)}px;background:transparent;")
        self._trt_status = QLabel("")
        self._trt_status.setWordWrap(True)
        self._trt_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._trt_status.setStyleSheet(f"color:{_C.TEXT_PRI};font-size:{_font_px(11)}px;background:transparent;")
        self._trt_message = QLabel("")
        self._trt_message.setWordWrap(True)
        self._trt_message.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:{_font_px(11)}px;background:transparent;")

        self._trt_enabled_box = QCheckBox("Utiliser l'accélération TensorRT pour l'interpolation")
        self._trt_enabled_box.setStyleSheet(_checkbox_style())
        self._trt_enabled_box.setChecked(bool(getattr(self._config, "trt_enabled", True)))
        self._trt_enabled_box.toggled.connect(self.trt_enabled_toggled.emit)
        self._trt_auto_box = QCheckBox("Mises à jour automatiques")
        self._trt_auto_box.setStyleSheet(_checkbox_style())
        self._trt_auto_box.setChecked(bool(getattr(self._config, "plugins_auto_update", True)))
        self._trt_auto_box.toggled.connect(self.trt_auto_update_toggled.emit)

        buttons = QHBoxLayout()
        buttons.setSpacing(_scale(8))
        buttons.addStretch()
        self._trt_install_btn = _primary_button("Installer…")
        self._trt_install_btn.clicked.connect(self.trt_install_requested.emit)
        self._trt_update_btn = _primary_button("Mettre à jour")
        self._trt_update_btn.clicked.connect(self.trt_update_requested.emit)
        self._trt_remove_btn = _secondary_button("Supprimer")
        self._trt_remove_btn.clicked.connect(self.trt_remove_requested.emit)
        for btn in (self._trt_install_btn, self._trt_update_btn, self._trt_remove_btn):
            buttons.addWidget(btn)

        for widget in (title, desc, self._trt_status, self._trt_message, self._trt_enabled_box, self._trt_auto_box):
            layout.addWidget(widget)
        layout.addLayout(buttons)
        return card

    def set_trt_state(self, state: object) -> None:
        """Section Extensions selon l'état de l'extension TensorRT (TrtState)."""
        if self._extensions_card is None or self._extensions_label is None:
            return
        visible = bool(getattr(state, "visible", False))
        self._extensions_label.setVisible(visible)
        self._extensions_card.setVisible(visible)
        if not visible:
            return
        capability = getattr(state, "capability", None)
        device = str(getattr(capability, "device", "") or "")
        installed = getattr(state, "installed", None)
        busy = str(getattr(state, "busy", "") or "")
        progress = int(getattr(state, "progress", -1))
        if busy == "remove":
            status = translate_text("Suppression en cours…")
        elif busy == "warmup":
            status = translate_text("Préparation des moteurs TensorRT pour cette carte…")
        elif busy:
            status = translate_text("Téléchargement : {progress} %", progress=progress) if progress >= 0 else translate_text("Téléchargement…")
        elif installed is None:
            status = translate_text("Non installée. GPU compatible : {device}.", device=device or "?")
        else:
            size = round(installed.size_bytes / 1e6)
            ready = bool(getattr(state, "ready", False))
            status = translate_text(
                "Version {version} installée ({size} Mo) dans {path}.", version=installed.version, size=size,
                path=str(installed.path),
            ) + " " + (
                translate_text("Active sur {device}.", device=device or "?") if ready
                else translate_text("Inutilisable : {reason}.", reason=str(getattr(capability, "reason", "") or "?"))
            )
            if getattr(state, "update_available", False):
                status += " " + translate_text("Mise à jour disponible.")
        self._trt_status.setText(status)
        self._trt_message.setText(str(getattr(state, "message", "") or ""))
        self._trt_message.setVisible(bool(getattr(state, "message", "")))
        self._trt_install_btn.setVisible(installed is None)
        self._trt_install_btn.setEnabled(not busy and bool(getattr(state, "compatible", False)))
        self._trt_update_btn.setVisible(bool(getattr(state, "update_available", False)))
        self._trt_update_btn.setEnabled(not busy)
        self._trt_remove_btn.setVisible(installed is not None)
        self._trt_remove_btn.setEnabled(not busy)
        self._trt_enabled_box.setEnabled(installed is not None)

    def show_extensions(self) -> None:
        """Fait défiler la page jusqu'à la section Extensions."""
        if self._scroll is not None and self._extensions_label is not None and self._extensions_label.isVisible():
            self._scroll.ensureWidgetVisible(self._extensions_label, 0, _scale(24))

    def _build_group_card(self, group: dict[str, Any]) -> QWidget:
        section = group["section"]
        section_title = group["title"]
        card = _card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(_scale(16), _scale(16), _scale(16), _scale(16))
        layout.setSpacing(_scale(14))

        for index, field in enumerate(group["fields"]):
            layout.addWidget(self._build_field_widget(section, field))
            if index < len(group["fields"]) - 1:
                layout.addWidget(_separator())

        layout.addWidget(_separator())
        save_row = QHBoxLayout()
        save_row.setContentsMargins(0, 0, 0, 0)
        save_row.setSpacing(_scale(8))
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
        for group in INI_FIELD_GROUPS:
            section = group["section"]
            for field in group["fields"]:
                widget = self._field_widgets[(section, field["key"])]
                value = getattr(self._config, field["attr"])

                if isinstance(widget, QLineEdit):
                    widget.setText(str(value))
                elif isinstance(widget, QCheckBox):
                    widget.setChecked(bool(value))
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
                    if index >= 0:
                        widget.setCurrentIndex(index)

        self._sync_dependent_field_states()
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
        values = self._collect_values()
        if not self._validate_values(values):
            return
        write_ini_settings(values)
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
        values = self._collect_section_values(section)
        if not self._validate_values(values):
            return
        write_ini_settings(values)
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
