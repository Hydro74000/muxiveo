"""
ui/panels/track_edit_dialog.py — Boîte de dialogue d'édition des métadonnées d'une piste.

Utilisée par RemuxPanel (_TrackTable) pour modifier en place un TrackEntry.

Usage :
    from ui.panels.track_edit_dialog import TrackEditDialog

    dlg = TrackEditDialog(entry, parent=self)
    if dlg.exec() == QDialog.DialogCode.Accepted:
        ...  # entry a été mis à jour en place

Champs proposés selon le type de piste :
    video    → Langue uniquement
    audio    → Nom, Langue, Flags (sans Forcé)
    subtitle → Nom, Langue, Flags complets (avec Forcé, sans Malvoyant)
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFrame, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit,
    QVBoxLayout, QWidget,
)

from core.i18n import apply_translations
from core.lang_tags import Rfc5646LanguageTags
from core.workflows.remux import TrackEntry


# =============================================================================
# Palette (copie locale — même valeurs que dans remux_panel._C)
# =============================================================================

class _C:
    BG_DEEP    = "#0d0f14"
    BG_PANEL   = "#141720"
    BG_CARD    = "#1a1e2a"
    BG_HOVER   = "#1f2435"
    BG_ACTIVE  = "#232840"

    BORDER     = "#252a3a"
    BORDER_LT  = "#2e3450"

    TEXT_PRI   = "#e8ecf4"
    TEXT_SEC   = "#7a85a0"
    TEXT_DIM   = "#3d4560"

    ACCENT     = "#4f6ef7"
    ACCENT_DIM = "#2a3a8a"

    OK         = "#5dcc8a"
    WARN       = "#f5c842"
    ERROR      = "#f55a5a"
    INFO       = "#7ab3f5"


def _separator() -> QFrame:
    sep = QFrame()
    sep.setFrameShape(QFrame.Shape.HLine)
    sep.setFixedHeight(1)
    sep.setStyleSheet(f"background: {_C.BORDER}; border: none;")
    return sep


# =============================================================================
# Boîte de dialogue
# =============================================================================

class TrackEditDialog(QDialog):
    """
    Fenêtre modale d'édition des métadonnées d'une piste.

    Les champs proposés varient selon le type de piste :
        video    → Langue uniquement
        audio    → Nom, Langue, Flags (sans Forcé)
        subtitle → Nom, Langue, Flags complets (avec Forcé, sans Malvoyant)

    Usage :
        dlg = TrackEditDialog(entry, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            ...  # entry a été mis à jour en place
    """

    def __init__(self, entry: TrackEntry, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._entry = entry
        tt = entry.track_type

        title = (
            "Langue de la piste"
            if tt == "video"
            else "Éditer la piste"
        )
        self.setWindowTitle(f"{title} — {entry.type_long}  ·  {entry.codec}")
        self.setModal(True)
        self.setMinimumWidth(440)
        self.setStyleSheet(f"""
            QDialog {{
                background: {_C.BG_PANEL};
            }}
            QLabel {{
                color: {_C.TEXT_PRI};
                background: transparent;
            }}
            QLineEdit {{
                background: {_C.BG_DEEP};
                color: {_C.TEXT_PRI};
                border: 1px solid {_C.BORDER_LT};
                border-radius: 4px;
                padding: 4px 8px;
                font-size: 12px;
            }}
            QLineEdit:focus {{
                border-color: {_C.ACCENT};
            }}
            QComboBox {{
                background: {_C.BG_DEEP};
                color: {_C.TEXT_PRI};
                border: 1px solid {_C.BORDER_LT};
                border-radius: 4px;
                padding: 4px 8px;
                font-size: 12px;
            }}
            QComboBox::drop-down {{
                border: none;
                padding-right: 6px;
            }}
            QComboBox QAbstractItemView {{
                background: {_C.BG_CARD};
                color: {_C.TEXT_PRI};
                selection-background-color: {_C.ACCENT_DIM};
                border: 1px solid {_C.BORDER_LT};
            }}
            QGroupBox {{
                color: {_C.TEXT_DIM};
                font-size: 9px;
                font-weight: 700;
                letter-spacing: 2px;
                border: 1px solid {_C.BORDER};
                border-radius: 6px;
                margin-top: 10px;
                padding-top: 8px;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                subcontrol-position: top left;
                left: 10px;
                padding: 0 4px;
                background: {_C.BG_PANEL};
            }}
            QCheckBox {{
                color: {_C.TEXT_PRI};
                font-size: 12px;
                spacing: 8px;
            }}
            QCheckBox::indicator {{
                width: 14px;
                height: 14px;
                border-radius: 3px;
                border: 1px solid {_C.BORDER_LT};
                background: {_C.BG_DEEP};
            }}
            QCheckBox::indicator:checked {{
                background: {_C.ACCENT};
                border-color: {_C.ACCENT};
            }}
            QDialogButtonBox QPushButton {{
                background: {_C.BG_CARD};
                color: {_C.TEXT_SEC};
                border: 1px solid {_C.BORDER};
                border-radius: 5px;
                font-size: 11px;
                font-weight: 600;
                padding: 5px 18px;
                min-width: 70px;
            }}
            QDialogButtonBox QPushButton:hover {{
                background: {_C.BG_HOVER};
                color: {_C.TEXT_PRI};
                border-color: {_C.BORDER_LT};
            }}
            QDialogButtonBox QPushButton[text="OK"] {{
                background: {_C.ACCENT};
                color: #ffffff;
                border: none;
            }}
            QDialogButtonBox QPushButton[text="OK"]:hover {{
                background: #6070f0;
            }}
        """)
        self._build_ui()
        self._configure_for_type(tt)
        self._populate(entry)
        apply_translations(self)

    # ------------------------------------------------------------------
    # Construction de l'UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(14)
        root.setContentsMargins(20, 20, 20, 20)

        # --- En-tête : type + codec ---
        header_lbl = QLabel(f"{self._entry.type_long}  ·  {self._entry.codec}")
        header_lbl.setStyleSheet(f"""
            color: {_C.TEXT_SEC};
            font-size: 11px;
            font-family: 'JetBrains Mono', monospace;
            background: transparent;
        """)
        root.addWidget(header_lbl)
        root.addWidget(_separator())

        # --- Section Nom (cachée pour vidéo) ---
        self._name_widget = QWidget()
        name_lay = QVBoxLayout(self._name_widget)
        name_lay.setContentsMargins(0, 0, 0, 0)
        name_lay.setSpacing(4)

        name_lbl = QLabel("NOM DE LA PISTE")
        name_lbl.setStyleSheet(
            f"color: {_C.TEXT_DIM}; font-size: 9px; font-weight: 700; "
            f"letter-spacing: 2px; background: transparent;"
        )
        name_lay.addWidget(name_lbl)

        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("Titre de la piste (facultatif)")
        name_lay.addWidget(self._name_edit)
        root.addWidget(self._name_widget)

        # --- Section Langue (toujours visible) ---
        lang_lbl = QLabel("LANGUE  (RFC 5646)")
        lang_lbl.setStyleSheet(
            f"color: {_C.TEXT_DIM}; font-size: 9px; font-weight: 700; "
            f"letter-spacing: 2px; background: transparent;"
        )
        root.addWidget(lang_lbl)

        lang_row = QHBoxLayout()
        lang_row.setSpacing(8)

        self._lang_edit = QLineEdit()
        self._lang_edit.setPlaceholderText("ex : fr, en, ja, und…")
        self._lang_edit.setMaximumWidth(120)
        lang_row.addWidget(self._lang_edit)

        self._lang_combo = QComboBox()
        self._lang_combo.setMaxVisibleItems(20)
        self._lang_combo.addItem("— sélectionner —", None)
        for code, name in Rfc5646LanguageTags.items():
            self._lang_combo.addItem(f"{code}  —  {name}", code)
        lang_row.addWidget(self._lang_combo, stretch=1)

        root.addLayout(lang_row)

        self._lang_warn = QLabel("⚠  Balise non reconnue — vérifiez la valeur RFC 5646")
        self._lang_warn.setStyleSheet(
            f"color: {_C.WARN}; font-size: 10px; background: transparent;"
        )
        self._lang_warn.setVisible(False)
        root.addWidget(self._lang_warn)

        # --- Section Flags (cachée pour vidéo) ---
        self._flags_group = QGroupBox("FLAGS")
        flags_lay = QVBoxLayout(self._flags_group)
        flags_lay.setSpacing(6)
        flags_lay.setContentsMargins(12, 8, 12, 10)

        self._cb_enabled    = QCheckBox("Piste activée")
        self._cb_default    = QCheckBox("Piste par défaut")
        self._cb_forced     = QCheckBox("Forcé  (forced track)")
        self._cb_hearing    = QCheckBox("Malentendant  (hearing impaired)")
        self._cb_visual     = QCheckBox("Malvoyant  (visual impaired)")
        self._cb_original   = QCheckBox("Langue d'origine  (original language)")
        self._cb_commentary = QCheckBox("Commentaires  (commentary)")

        for cb in (
            self._cb_enabled, self._cb_default, self._cb_forced,
            self._cb_hearing, self._cb_visual,
            self._cb_original, self._cb_commentary,
        ):
            flags_lay.addWidget(cb)

        root.addWidget(self._flags_group)

        # --- Boutons OK / Annuler ---
        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)
        root.addWidget(btn_box)

        # Connexions
        self._lang_combo.currentIndexChanged.connect(self._on_combo_changed)
        self._lang_edit.textChanged.connect(self._on_lang_text_changed)

    def _configure_for_type(self, track_type: str) -> None:
        """
        Adapte la visibilité des sections selon le type de piste.

        video    → Langue uniquement (Nom et Flags masqués)
        audio    → Nom + Langue + Flags sans Forcé (Malvoyant visible)
        subtitle → Nom + Langue + Flags avec Forcé, sans Malvoyant
        """
        is_video    = track_type == "video"
        is_subtitle = track_type == "subtitle"

        self._name_widget.setVisible(not is_video)
        self._flags_group.setVisible(not is_video)

        # Forcé uniquement pour les sous-titres
        self._cb_forced.setVisible(is_subtitle)
        # Malvoyant uniquement pour l'audio (pas les sous-titres)
        self._cb_visual.setVisible(not is_subtitle)

    # ------------------------------------------------------------------
    # Population / synchronisation
    # ------------------------------------------------------------------

    def _populate(self, entry: TrackEntry) -> None:
        self._name_edit.setText(entry.title)
        self._lang_edit.setText(entry.language)
        self._cb_enabled.setChecked(entry.flag_enabled)
        self._cb_default.setChecked(entry.flag_default)
        self._cb_forced.setChecked(entry.flag_forced)
        self._cb_hearing.setChecked(entry.flag_hearing_impaired)
        self._cb_visual.setChecked(entry.flag_visual_impaired)
        self._cb_original.setChecked(entry.flag_original)
        self._cb_commentary.setChecked(entry.flag_commentary)
        self._sync_combo_to_lang(entry.language)

    def _sync_combo_to_lang(self, lang: str) -> None:
        """Sélectionne dans la combobox la valeur correspondant au code langue."""
        for i in range(self._lang_combo.count()):
            if self._lang_combo.itemData(i) == lang:
                self._lang_combo.blockSignals(True)
                self._lang_combo.setCurrentIndex(i)
                self._lang_combo.blockSignals(False)
                return
        self._lang_combo.blockSignals(True)
        self._lang_combo.setCurrentIndex(0)
        self._lang_combo.blockSignals(False)

    def _on_combo_changed(self, idx: int) -> None:
        code = self._lang_combo.itemData(idx)
        if code is not None:
            self._lang_edit.blockSignals(True)
            self._lang_edit.setText(code)
            self._lang_edit.blockSignals(False)
            self._update_lang_validation(code)

    def _on_lang_text_changed(self, text: str) -> None:
        self._sync_combo_to_lang(text.strip())
        self._update_lang_validation(text.strip())

    def _update_lang_validation(self, tag: str) -> None:
        valid = Rfc5646LanguageTags.is_valid(tag)
        self._lang_warn.setVisible(bool(tag) and not valid)
        border = _C.BORDER_LT if valid else _C.WARN
        self._lang_edit.setStyleSheet(f"""
            QLineEdit {{
                background: {_C.BG_DEEP};
                color: {_C.TEXT_PRI};
                border: 1px solid {border};
                border-radius: 4px;
                padding: 4px 8px;
                font-size: 12px;
            }}
        """)

    # ------------------------------------------------------------------
    # Acceptation
    # ------------------------------------------------------------------

    def accept(self) -> None:
        e = self._entry
        tt = e.track_type
        # Langue : toujours appliquée
        e.language = self._lang_edit.text().strip()
        # Nom : audio et subtitle uniquement
        if tt != "video":
            e.title = self._name_edit.text().strip()
        # Flags : audio et subtitle uniquement
        if tt != "video":
            e.flag_enabled            = self._cb_enabled.isChecked()
            e.flag_default            = self._cb_default.isChecked()
            e.flag_hearing_impaired   = self._cb_hearing.isChecked()
            e.flag_original           = self._cb_original.isChecked()
            e.flag_commentary         = self._cb_commentary.isChecked()
            if tt == "subtitle":
                e.flag_forced         = self._cb_forced.isChecked()
            if tt == "audio":
                e.flag_visual_impaired = self._cb_visual.isChecked()
        super().accept()
