"""Low-code decision profile editor for RemuxPanel."""

from __future__ import annotations

import copy
import re
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QStyle,
    QStyleOptionFrame,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from core.i18n import translate_text
from core.profiles.decision import (
    DecisionProfileManager,
    VIDEO_FLAG_DOLBY_VISION,
    VIDEO_FLAG_HDR,
    VIDEO_FLAG_HDR10PLUS,
    build_video_flags_hex,
    apply_decision_profile,
    remux_config_to_decision_profile,
    validate_decision_profile,
)
from core.workflows.remux_models import RemuxConfig, TrackEntry
from ui.design_system import font_px as _font_px, scale as _scale
from ui.panels.remux_panel.theme import _C


KEYWORD_CATEGORIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Piste", ("type", "source_index", "track_index")),
    ("Langue", ("language", "lang", "lang_name")),
    ("Source", ("title", "source_title")),
    (
        "Audio",
        (
            "codec",
            "codec_name",
            "channels",
            "channel_layout",
            "audio_object",
            "atmos",
            "dtsx",
            "codec_atmos",
            "codec_dtsx",
        ),
    ),
    (
        "Vidéo",
        (
            "resolution",
            "width",
            "height",
            "hdr",
            "video_hdr",
            "video_hdr10",
            "video_hdr10plus",
            "video_dolby_vision",
            "video_hlg",
            "video_sdr",
            "video_flags_hex",
        ),
    ),
    (
        "Flags",
        (
            "flags",
            "flag_enabled",
            "flag_default",
            "flag_forced",
            "flag_hearing_impaired",
            "flag_visual_impaired",
            "flag_original",
            "flag_commentary",
        ),
    ),
)


class KeywordLineEdit(QLineEdit):
    """Line edit that displays ``{keyword}`` tokens as badges when not editing."""

    _TOKEN_RE = re.compile(r"\{([^{}]+)\}")

    def __init__(self, *, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.textChanged.connect(lambda _text: self.update())

    def contextMenuEvent(self, event) -> None:  # type: ignore[override]
        menu = self.createStandardContextMenu()
        menu.addSeparator()
        keyword_menu = menu.addMenu(translate_text("Insérer keyword"))
        self._populate_keyword_menu(keyword_menu)
        menu.exec(event.globalPos())

    def _populate_keyword_menu(self, menu: QMenu) -> None:
        for category, keywords in KEYWORD_CATEGORIES:
            submenu = menu.addMenu(translate_text(category))
            for keyword in keywords:
                action = submenu.addAction("{" + keyword + "}")
                action.triggered.connect(lambda _checked=False, k=keyword: self.insert("{" + k + "}"))

    def paintEvent(self, event) -> None:  # type: ignore[override]
        text = self.text()
        if self.hasFocus() or "{" not in text or "}" not in text:
            super().paintEvent(event)
            return
        segments = self._segments(text)
        if not any(is_keyword for is_keyword, _value in segments):
            super().paintEvent(event)
            return

        option = QStyleOptionFrame()
        self.initStyleOption(option)
        painter = QPainter(self)
        self.style().drawPrimitive(QStyle.PrimitiveElement.PE_PanelLineEdit, option, painter, self)

        rect = self.contentsRect().adjusted(_scale(7), _scale(3), -_scale(7), -_scale(3))
        x = rect.left()
        center_y = rect.center().y()
        gap = _scale(5)
        metrics = painter.fontMetrics()
        painter.setClipRect(rect)
        for is_keyword, value in segments:
            if not value:
                continue
            if is_keyword:
                label = value
                padding_x = _scale(7)
                badge_w = metrics.horizontalAdvance(label) + padding_x * 2
                badge_h = max(_scale(18), metrics.height() + _scale(4))
                badge_rect = rect.__class__(x, center_y - badge_h // 2, badge_w, badge_h)
                painter.setPen(QPen(QColor(_C.ACCENT), 1))
                painter.setBrush(QColor(_C.ACCENT_DIM))
                painter.drawRoundedRect(badge_rect, _scale(6), _scale(6))
                painter.setPen(QColor(_C.TEXT_PRI))
                painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, label)
                x += badge_w + gap
            else:
                label = value
                text_w = metrics.horizontalAdvance(label)
                painter.setPen(QColor(_C.TEXT_SEC))
                painter.drawText(x, center_y + metrics.ascent() // 2 - metrics.descent(), label)
                x += text_w + gap
            if x > rect.right():
                break
        painter.end()

    @classmethod
    def _segments(cls, text: str) -> list[tuple[bool, str]]:
        segments: list[tuple[bool, str]] = []
        cursor = 0
        for match in cls._TOKEN_RE.finditer(text):
            if match.start() > cursor:
                plain = text[cursor:match.start()].strip()
                if plain:
                    segments.append((False, plain))
            segments.append((True, match.group(1).strip()))
            cursor = match.end()
        if cursor < len(text):
            plain = text[cursor:].strip()
            if plain:
                segments.append((False, plain))
        return segments


def blank_decision_profile() -> dict[str, Any]:
    return {
        "version": 1,
        "kind": "decision-profile",
        "name": "Nouveau profil",
        "description": "",
        "tags": [],
        "groups": [
            {"id": "video", "label": "Video", "enabled": True, "priority": 300},
            {"id": "audio", "label": "Audio", "enabled": True, "priority": 200},
            {"id": "subtitle", "label": "Sous-titres", "enabled": True, "priority": 100},
            {"id": "order", "label": "Ordre", "enabled": True, "priority": 10},
        ],
        "variables": {"codec_names": {}},
        "rules": [],
    }


class DecisionProfileEditorDialog(QDialog):
    def __init__(
        self,
        *,
        manager: DecisionProfileManager,
        current_config: RemuxConfig | None = None,
        current_tracks: list[TrackEntry] | None = None,
        source_index_by_file_id: dict[str, int] | None = None,
        profile: dict[str, Any] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._manager = manager
        self._current_config = current_config
        self._current_tracks = current_tracks or []
        self._source_index_by_file_id = source_index_by_file_id or {}
        self._profile = copy.deepcopy(profile) if profile else blank_decision_profile()
        self._selected_rule_index = -1

        self.setWindowTitle(translate_text("Éditeur de profil"))
        self.setModal(True)
        self.resize(_scale(1180), _scale(720))
        self.setStyleSheet(f"""
            QDialog {{ background: {_C.BG_DEEP}; color: {_C.TEXT_PRI}; }}
            QLabel, QCheckBox {{ color: {_C.TEXT_SEC}; background: transparent; }}
            QLineEdit, QComboBox, QSpinBox, QPlainTextEdit, QListWidget {{
                background: {_C.BG_CARD};
                color: {_C.TEXT_PRI};
                border: 1px solid {_C.BORDER};
                border-radius: {_scale(5)}px;
                padding: {_scale(5)}px {_scale(7)}px;
                font-size: {_font_px(11)}px;
            }}
            QListWidget::item:selected {{ background: {_C.ACCENT_DIM}; }}
            QGroupBox {{
                color: {_C.TEXT_DIM};
                border: 1px solid {_C.BORDER};
                border-radius: {_scale(6)}px;
                margin-top: {_scale(10)}px;
                padding-top: {_scale(8)}px;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: {_scale(8)}px;
                padding: 0 {_scale(4)}px;
                background: {_C.BG_DEEP};
            }}
            QPushButton {{
                background: {_C.BG_CARD};
                color: {_C.TEXT_SEC};
                border: 1px solid {_C.BORDER};
                border-radius: {_scale(5)}px;
                padding: {_scale(5)}px {_scale(9)}px;
            }}
            QPushButton:hover {{ color: {_C.TEXT_PRI}; border-color: {_C.ACCENT}; }}
        """)
        self._build_ui()
        self._load_profile_to_ui()
        self._refresh_rule_list()
        self._refresh_preview()

    def profile(self) -> dict[str, Any]:
        self._store_current_rule()
        self._profile["name"] = self._name_edit.text().strip() or "Nouveau profil"
        self._profile["description"] = self._description_edit.text().strip()
        self._profile["tags"] = [item.strip() for item in self._tags_edit.text().split(",") if item.strip()]
        variables = copy.deepcopy(self._profile.get("variables", {}))
        if not isinstance(variables, dict):
            variables = {}
        codec_names = self._parse_codec_aliases(self._codec_aliases_edit.toPlainText())
        if codec_names:
            variables["codec_names"] = codec_names
        else:
            variables.pop("codec_names", None)
        if variables:
            self._profile["variables"] = variables
        else:
            self._profile.pop("variables", None)
        return copy.deepcopy(self._profile)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(_scale(14), _scale(14), _scale(14), _scale(10))
        root.setSpacing(_scale(10))

        header = QGridLayout()
        header.addWidget(QLabel(translate_text("Nom")), 0, 0)
        self._name_edit = QLineEdit()
        header.addWidget(self._name_edit, 0, 1)
        header.addWidget(QLabel(translate_text("Tags")), 0, 2)
        self._tags_edit = QLineEdit()
        self._tags_edit.setPlaceholderText("film, vf, vo")
        header.addWidget(self._tags_edit, 0, 3)
        header.addWidget(QLabel(translate_text("Description")), 1, 0)
        self._description_edit = QLineEdit()
        header.addWidget(self._description_edit, 1, 1, 1, 3)
        root.addLayout(header)

        mode_row = QHBoxLayout()
        blank_btn = QPushButton(translate_text("Profil vide"))
        capture_btn = QPushButton(translate_text("Capturer l'état courant"))
        self._profile_selector = QComboBox()
        self._profile_selector.setMinimumWidth(_scale(190))
        load_profile_btn = QPushButton(translate_text("Charger"))
        delete_profile_btn = QPushButton(translate_text("Supprimer profil"))
        blank_btn.clicked.connect(self._use_blank_profile)
        capture_btn.clicked.connect(self._capture_current_config)
        load_profile_btn.clicked.connect(self._load_selected_profile)
        delete_profile_btn.clicked.connect(self._delete_selected_profile)
        mode_row.addWidget(blank_btn)
        mode_row.addWidget(capture_btn)
        mode_row.addWidget(QLabel(translate_text("Profil existant")))
        mode_row.addWidget(self._profile_selector)
        mode_row.addWidget(load_profile_btn)
        mode_row.addWidget(delete_profile_btn)
        mode_row.addStretch()
        root.addLayout(mode_row)
        self._refresh_profile_selector(select_name=str(self._profile.get("name") or ""))

        variables_box = QGroupBox(translate_text("Variables"))
        variables_layout = QVBoxLayout(variables_box)
        variables_layout.addWidget(QLabel(translate_text("Aliases codecs")))
        self._codec_aliases_edit = QPlainTextEdit()
        self._codec_aliases_edit.setMaximumHeight(_scale(82))
        self._codec_aliases_edit.setPlaceholderText(translate_text("EAC3=DDP\nAC3=Dolby Digital\nTRUEHD=Dolby TrueHD"))
        self._codec_aliases_edit.textChanged.connect(self._refresh_preview)
        variables_layout.addWidget(self._codec_aliases_edit)
        root.addWidget(variables_box)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_left_panel())
        splitter.addWidget(self._build_center_panel())
        splitter.addWidget(self._build_preview_panel())
        splitter.setSizes([260, 500, 420])
        root.addWidget(splitter, stretch=1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save_profile)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _build_left_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel(translate_text("Règles")))
        self._rule_list = QListWidget()
        self._rule_list.currentRowChanged.connect(self._select_rule)
        layout.addWidget(self._rule_list, stretch=1)

        add_group = QGroupBox(translate_text("Modèles"))
        add_layout = QVBoxLayout(add_group)
        for label, kind in (
            (translate_text("Sélection vidéo"), "video"),
            (translate_text("Garder langue"), "language"),
            (translate_text("Exclure commentaire"), "no_commentary"),
            (translate_text("Renommer par pattern"), "rename"),
            (translate_text("Appliquer flags"), "flags"),
            (translate_text("Ordre"), "order"),
            (translate_text("Variante audio"), "variant"),
            (translate_text("Tagger piste"), "tag"),
        ):
            btn = QPushButton(label)
            btn.clicked.connect(lambda _=None, k=kind: self._add_template_rule(k))
            add_layout.addWidget(btn)
        layout.addWidget(add_group)

        row = QHBoxLayout()
        delete_btn = QPushButton(translate_text("Supprimer"))
        up_btn = QPushButton("↑")
        down_btn = QPushButton("↓")
        delete_btn.clicked.connect(self._delete_current_rule)
        up_btn.clicked.connect(lambda: self._move_current_rule(-1))
        down_btn.clicked.connect(lambda: self._move_current_rule(1))
        row.addWidget(delete_btn)
        row.addWidget(up_btn)
        row.addWidget(down_btn)
        layout.addLayout(row)
        return panel

    def _build_center_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(_scale(8), 0, _scale(8), 0)
        layout.addWidget(QLabel(translate_text("Critères et actions")))

        rule_box = QGroupBox(translate_text("Règle"))
        grid = QGridLayout(rule_box)
        self._rule_enabled = QCheckBox(translate_text("Activée"))
        self._rule_label = QLineEdit()
        self._rule_group = QComboBox()
        for group_id in ("video", "audio", "subtitle", "order"):
            self._rule_group.addItem(group_id, group_id)
        self._rule_priority = QSpinBox()
        self._rule_priority.setRange(-100000, 100000)
        self._rule_scope = QComboBox()
        for scope in ("best", "all", "first"):
            self._rule_scope.addItem(scope, scope)
        self._rule_write_mode = QComboBox()
        self._rule_write_mode.addItem(translate_text("Priorité"), "priority")
        self._rule_write_mode.addItem(translate_text("Remplacer"), "override")
        self._rule_write_mode.addItem(translate_text("Compléter"), "add")
        grid.addWidget(self._rule_enabled, 0, 0)
        grid.addWidget(QLabel(translate_text("Nom")), 1, 0)
        grid.addWidget(self._rule_label, 1, 1, 1, 3)
        grid.addWidget(QLabel(translate_text("Groupe")), 2, 0)
        grid.addWidget(self._rule_group, 2, 1)
        grid.addWidget(QLabel(translate_text("Priorité")), 2, 2)
        grid.addWidget(self._rule_priority, 2, 3)
        grid.addWidget(QLabel(translate_text("Portée")), 3, 0)
        grid.addWidget(self._rule_scope, 3, 1)
        grid.addWidget(QLabel(translate_text("Écriture")), 3, 2)
        grid.addWidget(self._rule_write_mode, 3, 3)
        layout.addWidget(rule_box)

        match_box = QGroupBox(translate_text("Critères simples"))
        match_grid = QGridLayout(match_box)
        self._match_type = QComboBox()
        self._match_type.addItem("—", "")
        for track_type in ("video", "audio", "subtitle"):
            self._match_type.addItem(track_type, track_type)
        self._match_language = KeywordLineEdit()
        self._match_codec = KeywordLineEdit()
        self._match_codec_required = QCheckBox(translate_text("Obligatoire"))
        self._match_title = KeywordLineEdit()
        self._match_flags = KeywordLineEdit()
        self._match_flags.setPlaceholderText("forced, commentary, default")
        self._match_width = QSpinBox()
        self._match_width.setRange(0, 20000)
        self._match_width.setSpecialValueText("—")
        self._match_height = QSpinBox()
        self._match_height.setRange(0, 20000)
        self._match_height.setSpecialValueText("—")
        self._video_hdr = QCheckBox(translate_text("HDR"))
        self._video_hdr10plus = QCheckBox(translate_text("HDR10+"))
        self._video_dolby_vision = QCheckBox(translate_text("Dolby Vision"))
        self._match_keywords = KeywordLineEdit()
        self._match_keywords.setPlaceholderText(translate_text("{flag_visual_impaired}, {codec_atmos}"))
        self._match_preferred_keywords = KeywordLineEdit()
        self._match_preferred_keywords.setPlaceholderText(translate_text("{atmos}, {dtsx}"))
        match_grid.addWidget(QLabel("Type"), 0, 0)
        match_grid.addWidget(self._match_type, 0, 1)
        match_grid.addWidget(QLabel(translate_text("Langue")), 1, 0)
        match_grid.addWidget(self._match_language, 1, 1)
        codec_row = QHBoxLayout()
        codec_row.addWidget(self._match_codec, stretch=1)
        codec_row.addWidget(self._match_codec_required)
        match_grid.addWidget(QLabel("Codec"), 2, 0)
        match_grid.addLayout(codec_row, 2, 1)
        match_grid.addWidget(QLabel(translate_text("Titre contient")), 3, 0)
        match_grid.addWidget(self._match_title, 3, 1)
        match_grid.addWidget(QLabel("Flags"), 4, 0)
        match_grid.addWidget(self._match_flags, 4, 1)
        video_size_row = QHBoxLayout()
        video_size_row.addWidget(self._match_width)
        video_size_row.addWidget(QLabel("x"))
        video_size_row.addWidget(self._match_height)
        match_grid.addWidget(QLabel(translate_text("Vidéo")), 5, 0)
        match_grid.addLayout(video_size_row, 5, 1)
        video_flags_row = QHBoxLayout()
        video_flags_row.addWidget(self._video_hdr)
        video_flags_row.addWidget(self._video_hdr10plus)
        video_flags_row.addWidget(self._video_dolby_vision)
        video_flags_row.addStretch()
        match_grid.addWidget(QLabel("HDR"), 6, 0)
        match_grid.addLayout(video_flags_row, 6, 1)
        match_grid.addWidget(QLabel(translate_text("Keywords critères")), 7, 0)
        match_grid.addWidget(self._match_keywords, 7, 1)
        match_grid.addWidget(QLabel(translate_text("Keywords préférés")), 8, 0)
        match_grid.addWidget(self._match_preferred_keywords, 8, 1)
        layout.addWidget(match_box)

        action_box = QGroupBox(translate_text("Actions"))
        action_grid = QGridLayout(action_box)
        self._action_enabled = QComboBox()
        self._action_enabled.addItem("—", None)
        self._action_enabled.addItem(translate_text("Activer"), True)
        self._action_enabled.addItem(translate_text("Désactiver"), False)
        self._action_language = KeywordLineEdit()
        self._title_pattern = KeywordLineEdit()
        self._title_pattern.setPlaceholderText("{lang_name} {codec} {channels} {audio_object}")
        self._action_tag = KeywordLineEdit()
        self._action_tag.setPlaceholderText("vf_main, forced_sub")
        action_grid.addWidget(QLabel(translate_text("Sélection")), 0, 0)
        action_grid.addWidget(self._action_enabled, 0, 1)
        action_grid.addWidget(QLabel(translate_text("Langue")), 1, 0)
        action_grid.addWidget(self._action_language, 1, 1)
        action_grid.addWidget(QLabel(translate_text("Titre pattern")), 2, 0)
        action_grid.addWidget(self._title_pattern, 2, 1)
        action_grid.addWidget(QLabel(translate_text("Tags piste")), 3, 0)
        action_grid.addWidget(self._action_tag, 3, 1)
        layout.addWidget(action_box)

        tokens_box = QGroupBox(translate_text("Keywords"))
        tokens_layout = QVBoxLayout(tokens_box)
        self._keyword_button = QPushButton(translate_text("Insérer keyword"))
        self._keyword_button.setMenu(self._build_keyword_menu())
        tokens_layout.addWidget(self._keyword_button)
        layout.addWidget(tokens_box)
        layout.addStretch()

        for widget in (
            self._rule_enabled,
            self._rule_label,
            self._rule_group,
            self._rule_priority,
            self._rule_scope,
            self._rule_write_mode,
            self._match_type,
            self._match_language,
            self._match_codec,
            self._match_codec_required,
            self._match_title,
            self._match_flags,
            self._match_width,
            self._match_height,
            self._video_hdr,
            self._video_hdr10plus,
            self._video_dolby_vision,
            self._match_keywords,
            self._match_preferred_keywords,
            self._action_enabled,
            self._action_language,
            self._title_pattern,
            self._action_tag,
        ):
            signal = getattr(widget, "textChanged", None) or getattr(widget, "currentIndexChanged", None) or getattr(widget, "valueChanged", None) or getattr(widget, "stateChanged", None)
            if signal is not None:
                signal.connect(self._editor_changed)
        return panel

    def _build_preview_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        row.addWidget(QLabel(translate_text("Preview")))
        refresh = QPushButton(translate_text("Rafraîchir"))
        refresh.clicked.connect(self._refresh_preview)
        row.addWidget(refresh)
        layout.addLayout(row)
        self._preview = QPlainTextEdit()
        self._preview.setReadOnly(True)
        layout.addWidget(self._preview, stretch=1)
        return panel

    def _load_profile_to_ui(self) -> None:
        self._name_edit.setText(str(self._profile.get("name") or ""))
        self._description_edit.setText(str(self._profile.get("description") or ""))
        self._tags_edit.setText(", ".join(str(tag) for tag in self._profile.get("tags", []) if str(tag).strip()))
        variables = self._profile.get("variables", {})
        codec_names = variables.get("codec_names", {}) if isinstance(variables, dict) else {}
        if hasattr(self, "_codec_aliases_edit"):
            self._codec_aliases_edit.setPlainText(self._format_codec_aliases(codec_names))

    @staticmethod
    def _parse_codec_aliases(text: str) -> dict[str, str]:
        aliases: dict[str, str] = {}
        for raw_line in str(text or "").replace(";", "\n").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip().upper()
            value = value.strip()
            if key and value:
                aliases[key] = value
        return aliases

    @staticmethod
    def _format_codec_aliases(codec_names: Any) -> str:
        if not isinstance(codec_names, dict):
            return ""
        lines = []
        for key in sorted(codec_names):
            value = str(codec_names.get(key) or "").strip()
            if str(key).strip() and value:
                lines.append(f"{str(key).strip().upper()}={value}")
        return "\n".join(lines)

    def _refresh_profile_selector(self, *, select_name: str = "") -> None:
        if not hasattr(self, "_profile_selector"):
            return
        selected = str(select_name or self._profile_selector.currentData() or "")
        self._profile_selector.blockSignals(True)
        self._profile_selector.clear()
        self._profile_selector.addItem("—", "")
        for name in self._manager.names():
            self._profile_selector.addItem(name, name)
        index = self._profile_selector.findData(selected)
        self._profile_selector.setCurrentIndex(index if index >= 0 else 0)
        self._profile_selector.blockSignals(False)

    def _refresh_rule_list(self) -> None:
        self._rule_list.blockSignals(True)
        self._rule_list.clear()
        for rule in self._rules():
            item = QListWidgetItem(str(rule.get("label") or rule.get("id") or "rule"))
            item.setData(Qt.ItemDataRole.UserRole, rule.get("id"))
            self._rule_list.addItem(item)
        self._rule_list.blockSignals(False)
        if self._rules() and self._selected_rule_index < 0:
            self._rule_list.setCurrentRow(0)

    def _rules(self) -> list[dict[str, Any]]:
        rules = self._profile.setdefault("rules", [])
        return rules if isinstance(rules, list) else []

    def _select_rule(self, row: int) -> None:
        self._store_current_rule()
        self._selected_rule_index = row
        rule = self._rules()[row] if 0 <= row < len(self._rules()) else None
        self._load_rule(rule)

    def _load_rule(self, rule: dict[str, Any] | None) -> None:
        enabled = rule is not None
        for widget in (
            self._rule_enabled,
            self._rule_label,
            self._rule_group,
            self._rule_priority,
            self._rule_scope,
            self._rule_write_mode,
            self._match_type,
            self._match_language,
            self._match_codec,
            self._match_codec_required,
            self._match_title,
            self._match_flags,
            self._match_width,
            self._match_height,
            self._video_hdr,
            self._video_hdr10plus,
            self._video_dolby_vision,
            self._match_keywords,
            self._match_preferred_keywords,
            self._action_enabled,
            self._action_language,
            self._title_pattern,
            self._action_tag,
        ):
            widget.blockSignals(True)
            widget.setEnabled(enabled)
        if rule is None:
            self._rule_label.clear()
            self._set_combo_data(self._rule_group, "audio")
            self._rule_priority.setValue(0)
            self._set_combo_data(self._rule_scope, "best")
            self._set_combo_data(self._rule_write_mode, "priority")
            self._match_language.clear()
            self._match_codec.clear()
            self._match_codec_required.setChecked(False)
            self._match_title.clear()
            self._match_flags.clear()
            self._match_width.setValue(0)
            self._match_height.setValue(0)
            self._video_hdr.setChecked(False)
            self._video_hdr10plus.setChecked(False)
            self._video_dolby_vision.setChecked(False)
            self._match_keywords.clear()
            self._match_preferred_keywords.clear()
            self._action_language.clear()
            self._title_pattern.clear()
            self._action_tag.clear()
        else:
            self._rule_enabled.setChecked(bool(rule.get("enabled", True)))
            self._rule_label.setText(str(rule.get("label") or ""))
            self._set_combo_data(self._rule_group, str(rule.get("group_id") or "audio"))
            self._rule_priority.setValue(int(rule.get("priority") or 0))
            self._set_combo_data(self._rule_scope, str(rule.get("scope") or "best"))
            self._set_combo_data(self._rule_write_mode, str(rule.get("write_mode") or "priority"))
            flat = self._flatten_simple_match(rule.get("match", {}))
            self._set_combo_data(self._match_type, str(flat.get("type") or ""))
            self._match_language.setText(str(flat.get("language") or ""))
            self._match_codec.setText(str(flat.get("codec") or ""))
            self._match_codec_required.setChecked(bool(flat.get("codec_required")))
            self._match_title.setText(str(flat.get("title_contains") or ""))
            self._match_flags.setText(", ".join(flat.get("flags", [])))
            self._match_width.setValue(int(flat.get("width") or 0))
            self._match_height.setValue(int(flat.get("height") or 0))
            self._video_hdr.setChecked(bool(flat.get("video_hdr")))
            self._video_hdr10plus.setChecked(bool(flat.get("video_hdr10plus")))
            self._video_dolby_vision.setChecked(bool(flat.get("video_dolby_vision")))
            self._match_keywords.setText(", ".join(flat.get("keywords", [])))
            self._match_preferred_keywords.setText(", ".join(flat.get("preferred_keywords", [])))
            actions = rule.get("actions", []) if isinstance(rule.get("actions"), list) else []
            enabled_action = next((action for action in actions if action.get("type") == "set_enabled"), None)
            self._set_combo_data(self._action_enabled, enabled_action.get("value") if isinstance(enabled_action, dict) else None)
            lang_action = next((action for action in actions if action.get("type") == "set_language"), None)
            self._action_language.setText(str(lang_action.get("value") or "") if isinstance(lang_action, dict) else "")
            title_action = next((action for action in actions if action.get("type") == "set_title"), None)
            self._title_pattern.setText(str(title_action.get("pattern") or title_action.get("value") or "") if isinstance(title_action, dict) else "")
            tag_action = next((action for action in actions if action.get("type") == "add_track_tags"), None)
            self._action_tag.setText(", ".join(tag_action.get("value", [])) if isinstance(tag_action, dict) and isinstance(tag_action.get("value"), list) else "")
        for widget in (
            self._rule_enabled,
            self._rule_label,
            self._rule_group,
            self._rule_priority,
            self._rule_scope,
            self._rule_write_mode,
            self._match_type,
            self._match_language,
            self._match_codec,
            self._match_codec_required,
            self._match_title,
            self._match_flags,
            self._match_width,
            self._match_height,
            self._video_hdr,
            self._video_hdr10plus,
            self._video_dolby_vision,
            self._match_keywords,
            self._match_preferred_keywords,
            self._action_enabled,
            self._action_language,
            self._title_pattern,
            self._action_tag,
        ):
            widget.blockSignals(False)

    @staticmethod
    def _set_combo_data(combo: QComboBox, value: Any) -> None:
        for index in range(combo.count()):
            if combo.itemData(index) == value:
                combo.setCurrentIndex(index)
                return
        combo.setCurrentIndex(0)

    def _store_current_rule(self) -> None:
        if self._selected_rule_index < 0 or self._selected_rule_index >= len(self._rules()):
            return
        rule = self._rules()[self._selected_rule_index]
        rule["label"] = self._rule_label.text().strip() or str(rule.get("id") or "rule")
        rule["enabled"] = self._rule_enabled.isChecked()
        rule["group_id"] = str(self._rule_group.currentData() or "")
        rule["priority"] = int(self._rule_priority.value())
        rule["scope"] = str(self._rule_scope.currentData() or "best")
        rule["write_mode"] = str(self._rule_write_mode.currentData() or "priority")
        rule["match"] = self._simple_match_payload()
        rule["actions"] = self._simple_actions_payload(rule)
        self._refresh_rule_list_item(self._selected_rule_index)

    def _refresh_rule_list_item(self, index: int) -> None:
        item = self._rule_list.item(index)
        if item is None or index >= len(self._rules()):
            return
        rule = self._rules()[index]
        item.setText(str(rule.get("label") or rule.get("id") or "rule"))

    def _simple_match_payload(self) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        track_type = str(self._match_type.currentData() or "")
        if track_type:
            items.append({"field": "type", "op": "is", "value": track_type, "required": True})
        language = self._match_language.text().strip()
        if language:
            keyword_conditions = self._keyword_conditions_if_field_is_tokens(language)
            if keyword_conditions:
                items.extend(keyword_conditions)
            else:
                items.append({"field": "language", "op": "is", "value": language, "required": True})
        codec = self._match_codec.text().strip()
        if codec:
            codec_required = self._match_codec_required.isChecked()
            keyword_conditions = self._keyword_conditions_if_field_is_tokens(codec, required=codec_required)
            if keyword_conditions:
                items.extend(keyword_conditions)
            else:
                items.append({"field": "codec", "op": "is", "value": codec, "required": codec_required})
        title = self._match_title.text().strip()
        if title:
            keyword_conditions = self._keyword_conditions_if_field_is_tokens(title)
            if keyword_conditions:
                items.extend(keyword_conditions)
            else:
                items.append({"field": "source_title", "op": "contains", "value": title, "required": False})
        for flag in [value.strip() for value in self._match_flags.text().split(",") if value.strip()]:
            normalized = flag.removeprefix("{").removesuffix("}").removeprefix("flag_")
            items.append({"field": f"flag_{normalized}", "op": "is", "value": True, "required": False})
        width = int(self._match_width.value())
        height = int(self._match_height.value())
        if width > 0:
            items.append(
                {
                    "field": "width",
                    "op": "is",
                    "value": width,
                    "required": False,
                }
            )
        if height > 0:
            items.append(
                {
                    "field": "height",
                    "op": "is",
                    "value": height,
                    "required": False,
                }
            )
        if self._video_hdr.isChecked() or self._video_hdr10plus.isChecked() or self._video_dolby_vision.isChecked():
            items.append(
                {
                    "field": "video_flags_hex",
                    "op": "is",
                    "value": build_video_flags_hex(
                        width=width,
                        height=height,
                        hdr=self._video_hdr.isChecked(),
                        hdr10plus=self._video_hdr10plus.isChecked(),
                        dolby_vision=self._video_dolby_vision.isChecked(),
                    ),
                    "required": False,
                }
            )
        items.extend(self._keyword_match_conditions(self._match_keywords.text(), required=True))
        items.extend(self._keyword_match_conditions(self._match_preferred_keywords.text(), required=False))
        return {"all": items} if items else {"all": []}

    def _build_keyword_menu(self) -> QMenu:
        menu = QMenu(self)
        for category, keywords in KEYWORD_CATEGORIES:
            submenu = menu.addMenu(translate_text(category))
            for keyword in keywords:
                action = submenu.addAction("{" + keyword + "}")
                action.triggered.connect(lambda _checked=False, k=keyword: self._insert_keyword_token(k))
        return menu

    def _keyword_conditions_if_field_is_tokens(self, text: str, *, required: bool = True) -> list[dict[str, Any]]:
        tokens = self._decision_keyword_tokens(text)
        if not tokens:
            return []
        residue = re.sub(r"\{[^{}]+\}", "", str(text or ""))
        residue = residue.replace(",", "").strip()
        if residue:
            return []
        conditions = self._keyword_match_conditions(text, required=required)
        return conditions if len(conditions) == len(tokens) else []

    def _keyword_match_conditions(self, text: str, *, required: bool) -> list[dict[str, Any]]:
        conditions: list[dict[str, Any]] = []
        for token in self._decision_keyword_tokens(text):
            field = self._keyword_to_match_field(token)
            if not field:
                continue
            conditions.append({"field": field, "op": "is", "value": True, "required": required})
        return conditions

    @staticmethod
    def _decision_keyword_tokens(text: str) -> list[str]:
        tokens = [match.strip() for match in re.findall(r"\{([^{}]+)\}", str(text or ""))]
        if tokens:
            return tokens
        return [part.strip().strip("{}") for part in str(text or "").split(",") if part.strip()]

    @staticmethod
    def _keyword_to_match_field(keyword: str) -> str:
        key = str(keyword or "").strip().strip("{}")
        aliases = {
            "hdr": "video_hdr",
            "dolby_vision": "video_dolby_vision",
            "dovi": "video_dolby_vision",
            "hdr10plus": "video_hdr10plus",
            "atmos": "codec_atmos",
            "dtsx": "codec_dtsx",
        }
        key = aliases.get(key, key)
        if key.startswith("flag_"):
            return key
        if key in {
            "codec_atmos",
            "codec_dtsx",
            "video_hdr",
            "video_hdr10",
            "video_hdr10plus",
            "video_dolby_vision",
            "video_hlg",
            "video_sdr",
        }:
            return key
        return ""

    def _simple_actions_payload(self, existing_rule: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        managed = {"set_enabled", "set_language", "set_title", "add_track_tags"}
        existing_actions = existing_rule.get("actions", []) if isinstance(existing_rule, dict) else []
        actions: list[dict[str, Any]] = [
            copy.deepcopy(action)
            for action in existing_actions
            if isinstance(action, dict) and str(action.get("type") or "") not in managed
        ]
        enabled_value = self._action_enabled.currentData()
        if enabled_value is not None:
            actions.append({"type": "set_enabled", "value": bool(enabled_value)})
        language = self._action_language.text().strip()
        if language:
            actions.append({"type": "set_language", "value": language})
        pattern = self._title_pattern.text().strip()
        if pattern:
            action = {"type": "set_title"}
            if "{" in pattern and "}" in pattern:
                action["pattern"] = pattern
            else:
                action["value"] = pattern
            actions.append(action)
        tags = [value.strip() for value in self._action_tag.text().split(",") if value.strip()]
        if tags:
            actions.append({"type": "add_track_tags", "value": tags})
        return actions

    def _flatten_simple_match(self, match: Any) -> dict[str, Any]:
        flat: dict[str, Any] = {"flags": [], "keywords": [], "preferred_keywords": [], "codec_required": False}
        items = match.get("all", []) if isinstance(match, dict) else []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            field = str(item.get("field") or "")
            value = item.get("value")
            if field == "type":
                flat["type"] = value
            elif field == "language":
                flat["language"] = value
            elif field == "codec":
                flat["codec"] = value
                flat["codec_required"] = bool(item.get("required", False))
            elif field == "resolution" and isinstance(value, dict):
                flat["width"] = int(value.get("width") or 0)
                flat["height"] = int(value.get("height") or 0)
            elif field == "width":
                flat["width"] = int(value or 0)
            elif field == "height":
                flat["height"] = int(value or 0)
            elif field == "video_flags_hex":
                flags = self._parse_video_flags(value)
                flat["video_hdr"] = bool(flags & VIDEO_FLAG_HDR)
                flat["video_hdr10plus"] = bool(flags & VIDEO_FLAG_HDR10PLUS)
                flat["video_dolby_vision"] = bool(flags & VIDEO_FLAG_DOLBY_VISION)
            elif field in {"title", "source_title"}:
                flat["title_contains"] = value
            elif field.startswith("flag_") and value is True:
                if bool(item.get("required", True)):
                    flat["keywords"].append("{" + field + "}")
                else:
                    flat["preferred_keywords"].append("{" + field + "}")
            elif field in {
                "codec_atmos",
                "codec_dtsx",
                "video_hdr",
                "video_hdr10",
                "video_hdr10plus",
                "video_dolby_vision",
                "video_hlg",
                "video_sdr",
            } and value is True:
                target = "keywords" if bool(item.get("required", True)) else "preferred_keywords"
                flat[target].append("{" + field + "}")
        return flat

    @staticmethod
    def _parse_video_flags(value: Any) -> int:
        try:
            text = str(value or "").strip()
            return int(text, 16 if text.lower().startswith("0x") else 10) if text else 0
        except ValueError:
            return 0

    def _editor_changed(self, *_args) -> None:
        self._store_current_rule()
        self._refresh_preview()

    def _insert_keyword_token(self, keyword: str) -> None:
        target = self.focusWidget()
        if not isinstance(target, QLineEdit) or target not in (
            self._match_language,
            self._match_codec,
            self._match_title,
            self._match_flags,
            self._match_keywords,
            self._action_language,
            self._title_pattern,
            self._action_tag,
        ):
            target = self._title_pattern
        target.insert("{" + keyword + "}")
        target.setFocus()

    def _add_template_rule(self, kind: str) -> None:
        self._store_current_rule()
        index = len(self._rules()) + 1
        rule = self._template_rule(kind, index)
        self._rules().append(rule)
        self._refresh_rule_list()
        self._rule_list.setCurrentRow(len(self._rules()) - 1)
        self._refresh_preview()

    def _template_rule(self, kind: str, index: int) -> dict[str, Any]:
        base = {
            "id": f"{kind}_{index}",
            "label": kind.replace("_", " ").title(),
            "group_id": "audio",
            "tags": [kind],
            "enabled": True,
            "priority": 1000 - index,
            "scope": "best",
            "match": {"all": []},
            "actions": [],
        }
        if kind == "video":
            base.update(group_id="video", label=translate_text("Sélection vidéo"), match={"all": [{"field": "type", "op": "is", "value": "video", "required": True}]}, actions=[{"type": "set_enabled", "value": True}])
        elif kind == "language":
            base.update(label=translate_text("Garder langue"), match={"all": [{"field": "language", "op": "is", "value": "fr-FR", "required": True}]}, actions=[{"type": "set_enabled", "value": True}])
        elif kind == "no_commentary":
            base.update(label=translate_text("Exclure commentaire"), match={"all": [{"field": "source_title", "op": "contains", "value": "comment", "required": False}, {"field": "flag_commentary", "op": "is", "value": True, "required": False}]}, actions=[{"type": "set_enabled", "value": False}])
        elif kind == "rename":
            base.update(label=translate_text("Renommer par pattern"), scope="all", actions=[{"type": "set_title", "pattern": "{lang_name} {codec} {channels} {audio_object}"}])
        elif kind == "flags":
            base.update(label=translate_text("Appliquer flags"), actions=[{"type": "set_flags", "value": {"default": True}}])
        elif kind == "order":
            base.update(group_id="order", label=translate_text("Ordre"), actions=[{"type": "set_order_priority", "value": 100}])
        elif kind == "variant":
            base.update(label=translate_text("Variante audio"), match={"all": [{"field": "type", "op": "is", "value": "audio", "required": True}]}, actions=[{"type": "create_audio_variant", "codec": "ac3", "bitrate_kbps": 640, "title_pattern": "{lang_name} AC3 {channels}"}])
        elif kind == "tag":
            base.update(label=translate_text("Tagger piste"), actions=[{"type": "add_track_tags", "value": ["tag"]}])
        return base

    def _delete_current_rule(self) -> None:
        row = self._rule_list.currentRow()
        if row < 0 or row >= len(self._rules()):
            return
        self._rules().pop(row)
        self._selected_rule_index = -1
        self._refresh_rule_list()
        self._refresh_preview()

    def _move_current_rule(self, delta: int) -> None:
        row = self._rule_list.currentRow()
        new_row = row + delta
        if row < 0 or new_row < 0 or row >= len(self._rules()) or new_row >= len(self._rules()):
            return
        self._store_current_rule()
        rules = self._rules()
        rules[row], rules[new_row] = rules[new_row], rules[row]
        rules[new_row]["priority"] = int(rules[new_row].get("priority") or 0) + (-delta)
        self._selected_rule_index = new_row
        self._refresh_rule_list()
        self._rule_list.setCurrentRow(new_row)
        self._refresh_preview()

    def _use_blank_profile(self) -> None:
        self._profile = blank_decision_profile()
        self._selected_rule_index = -1
        self._load_profile_to_ui()
        self._refresh_profile_selector(select_name="")
        self._refresh_rule_list()
        self._refresh_preview()

    def _load_selected_profile(self) -> None:
        name = str(self._profile_selector.currentData() or "").strip()
        if not name:
            return
        profile = self._manager.load(name)
        if not profile:
            QMessageBox.warning(
                self,
                translate_text("Profil introuvable"),
                translate_text("Impossible de charger le profil sélectionné."),
            )
            self._refresh_profile_selector(select_name="")
            return
        self._profile = copy.deepcopy(profile)
        self._selected_rule_index = -1
        self._load_profile_to_ui()
        self._refresh_profile_selector(select_name=name)
        self._refresh_rule_list()
        self._refresh_preview()

    def _delete_selected_profile(self) -> None:
        name = str(self._profile_selector.currentData() or "").strip()
        if not name:
            return
        reply = QMessageBox.question(
            self,
            translate_text("Supprimer profil"),
            translate_text("Supprimer le profil « {name} » ?", name=name),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._manager.delete(name)
        if str(self._profile.get("name") or "") == name:
            self._profile = blank_decision_profile()
            self._selected_rule_index = -1
            self._load_profile_to_ui()
            self._refresh_rule_list()
        self._refresh_profile_selector(select_name="")
        self._refresh_preview()

    def _capture_current_config(self) -> None:
        if self._current_config is None:
            QMessageBox.information(self, translate_text("Profil"), translate_text("Aucune configuration à capturer."))
            return
        name = self._name_edit.text().strip() or "Profil capturé"
        self._profile = remux_config_to_decision_profile(self._current_config, name=name)
        self._selected_rule_index = -1
        self._load_profile_to_ui()
        self._refresh_rule_list()
        self._refresh_preview()

    def _refresh_preview(self) -> None:
        try:
            profile = self.profile()
            validate_decision_profile(profile)
        except Exception as exc:
            self._preview.setPlainText(translate_text("Profil invalide : {err}", err=str(exc)))
            return
        if not self._current_tracks:
            self._preview.setPlainText(translate_text("Aucune source chargée pour la preview."))
            return
        before = {track.entry_id: copy.deepcopy(track) for track in self._current_tracks}
        tracks = copy.deepcopy(self._current_tracks)
        result = apply_decision_profile(profile, tracks, source_index_by_file_id=self._source_index_by_file_id)
        lines = [
            translate_text("Règles appliquées : {count}", count=int(result.report.get("applied_rules", 0) or 0)),
            translate_text("Conflits : {count}", count=len(result.report.get("conflicts", []))),
            translate_text("Écritures résolues : {count}", count=len(result.report.get("resolved_writes", []))),
            translate_text("Écritures ignorées : {count}", count=len(result.report.get("skipped_writes", []))),
            translate_text("Ambiguïtés : {count}", count=len(result.report.get("ambiguous_matches", []))),
            "",
        ]
        for track in result.tracks:
            old = before.get(track.entry_id)
            prefix = f"{track.type_label}{track.mkv_tid} {track.codec}"
            if old is None:
                lines.append(f"+ {prefix} {track.language} {track.title}")
                continue
            changes = []
            if old.enabled != track.enabled:
                changes.append(f"enabled {old.enabled}->{track.enabled}")
            if old.language != track.language:
                changes.append(f"lang {old.language}->{track.language}")
            if old.title != track.title:
                changes.append(f"title '{old.title}' -> '{track.title}'")
            flag_changes = self._flag_change_labels(old, track)
            if flag_changes:
                changes.append("flags " + ", ".join(flag_changes))
            if changes:
                lines.append(f"* {prefix}: " + "; ".join(changes))
        tags = result.report.get("track_tags", {})
        if tags:
            lines.extend(["", translate_text("Tags temporaires :")])
            for entry_id, values in tags.items():
                lines.append(f"- {entry_id[:8]}: {', '.join(values)}")
        self._preview.setPlainText("\n".join(lines).strip())

    @staticmethod
    def _flag_change_labels(old: TrackEntry, new: TrackEntry) -> list[str]:
        labels: list[str] = []
        for name in (
            "enabled",
            "default",
            "forced",
            "hearing_impaired",
            "visual_impaired",
            "original",
            "commentary",
        ):
            attr = "flag_enabled" if name == "enabled" else f"flag_{name}"
            if bool(getattr(old, attr, False)) != bool(getattr(new, attr, False)):
                labels.append(f"{name} {bool(getattr(old, attr, False))}->{bool(getattr(new, attr, False))}")
        return labels

    def _save_profile(self) -> None:
        try:
            profile = self.profile()
            path = self._manager.save(profile)
        except Exception as exc:
            QMessageBox.warning(self, translate_text("Sauvegarde impossible"), str(exc))
            return
        self._profile = profile
        self._refresh_profile_selector(select_name=str(profile.get("name") or ""))
        QMessageBox.information(self, translate_text("Profil sauvegardé"), str(path))
        self.accept()
