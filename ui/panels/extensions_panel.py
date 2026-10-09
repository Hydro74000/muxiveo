"""
ui/panels/extensions_panel.py — Page « Extensions » : installation, mise à jour et suppression des extensions.

Extensions compatibles avec la machine en tête ; les incompatibles sont repliées avec leur raison (jamais
masquées sans explication). Les opérations sont confiées aux contrôleurs (ex. TrtPluginController) par signaux.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from core.config import AppConfig
from core.i18n import apply_translations, translate_text
from ui.design_system import font_px as _font_px, scale as _scale
from ui.panels.encode_panel.theme import (
    _C,
    _card,
    _checkbox_style,
    _primary_button,
    _secondary_button,
    _section_label,
    _separator,
)


class ExtensionsPanel(QWidget):
    # Extension d'accélération NVIDIA (TensorRT), traitée par TrtPluginController.
    trt_install_requested = Signal()
    trt_update_requested = Signal()
    trt_remove_requested = Signal()
    trt_enabled_toggled = Signal(bool)
    trt_auto_update_toggled = Signal(bool)

    def __init__(self, config: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self._build_ui()
        apply_translations(self)

    # ------------------------------------------------------------------ construction

    def _build_ui(self) -> None:
        self.setStyleSheet(f"background:{_C.BG_DEEP};")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet(f"QScrollArea{{background:{_C.BG_DEEP};border:none;}}")
        content = QWidget()
        content.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        content.setStyleSheet(f"background:{_C.BG_DEEP};")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(_scale(28), _scale(24), _scale(28), _scale(24))
        layout.setSpacing(_scale(20))
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinAndMaxSize)

        title = QLabel("Extensions")
        title.setStyleSheet(
            f"font-size:{_font_px(20)}px;font-weight:800;color:{_C.TEXT_PRI};"
            f"background:transparent;letter-spacing:-{_scale(1)}px;"
        )
        subtitle = QLabel(
            "Composants facultatifs téléchargés à la demande, vérifiés (SHA-256) puis installés dans votre "
            "dossier utilisateur. Muxiveo fonctionne sans eux ; seules les extensions compatibles avec cette "
            "machine peuvent être installées."
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:{_font_px(12)}px;background:transparent;border:none;")
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addWidget(_separator())

        self._available_label = _section_label("DISPONIBLES POUR CETTE MACHINE")
        self._available_box = QVBoxLayout()
        self._available_box.setSpacing(_scale(12))
        self._none_label = QLabel("Aucune extension compatible avec cette machine pour l'instant.")
        self._none_label.setWordWrap(True)
        self._none_label.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:{_font_px(11)}px;background:transparent;border:none;")
        layout.addWidget(self._available_label)
        layout.addLayout(self._available_box)
        layout.addWidget(self._none_label)

        self._incompatible_toggle = QPushButton()
        self._incompatible_toggle.setCheckable(True)
        self._incompatible_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._incompatible_toggle.setStyleSheet(
            f"QPushButton{{color:{_C.TEXT_SEC};font-size:{_font_px(11)}px;font-weight:700;background:transparent;"
            f"border:none;text-align:left;padding:0;}}QPushButton:hover{{color:{_C.TEXT_PRI};}}"
        )
        self._incompatible_toggle.toggled.connect(self._sync_incompatible)
        self._incompatible_box = QVBoxLayout()
        self._incompatible_box.setSpacing(_scale(12))
        self._incompatible_container = QWidget()
        self._incompatible_container.setStyleSheet("background:transparent;")
        self._incompatible_container.setLayout(self._incompatible_box)
        layout.addWidget(self._incompatible_toggle)
        layout.addWidget(self._incompatible_container)

        self._trt_card = self._build_trt_card()
        self._place(self._trt_card, compatible=False)
        scroll.setWidget(content)
        root.addWidget(scroll)
        self._sync_sections()

    def _build_trt_card(self) -> QWidget:
        card = _card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(_scale(16), _scale(16), _scale(16), _scale(16))
        layout.setSpacing(_scale(10))
        title = QLabel("Accélération NVIDIA (TensorRT)")
        title.setStyleSheet(f"color:{_C.TEXT_PRI};font-size:{_font_px(13)}px;font-weight:700;background:transparent;border:none;")
        desc = QLabel(
            "Exécute l'interpolation MVO-RIFE sur les Tensor Cores des cartes NVIDIA (NVIDIA TensorRT for RTX) : "
            "traitement nettement plus rapide, images identiques à l'œil. Sans elle, l'interpolation fonctionne "
            "sur le GPU (Vulkan)."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:{_font_px(11)}px;background:transparent;border:none;")
        self._trt_status = QLabel("")
        self._trt_status.setWordWrap(True)
        self._trt_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._trt_status.setStyleSheet(f"color:{_C.TEXT_PRI};font-size:{_font_px(11)}px;background:transparent;border:none;")
        self._trt_message = QLabel("")
        self._trt_message.setWordWrap(True)
        self._trt_message.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:{_font_px(11)}px;background:transparent;border:none;")

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

    # ------------------------------------------------------------------ sections

    def _place(self, card: QWidget, *, compatible: bool) -> None:
        """Range une carte dans la section des extensions disponibles ou des incompatibles."""
        target = self._available_box if compatible else self._incompatible_box
        for box in (self._available_box, self._incompatible_box):
            if box is not target and box.indexOf(card) >= 0:
                box.removeWidget(card)
        if target.indexOf(card) < 0:
            target.addWidget(card)

    def _sync_sections(self) -> None:
        available = self._available_box.count()
        incompatible = self._incompatible_box.count()
        self._none_label.setVisible(available == 0)
        self._incompatible_toggle.setVisible(incompatible > 0)
        self._incompatible_toggle.setText(
            ("▾ " if self._incompatible_toggle.isChecked() else "▸ ")
            + translate_text("Non compatibles avec cette machine ({count})", count=incompatible)
        )
        self._sync_incompatible()

    def _sync_incompatible(self, _checked: bool = False) -> None:
        self._incompatible_container.setVisible(self._incompatible_toggle.isChecked() and self._incompatible_box.count() > 0)
        if self._incompatible_box.count():
            self._incompatible_toggle.setText(
                ("▾ " if self._incompatible_toggle.isChecked() else "▸ ")
                + translate_text("Non compatibles avec cette machine ({count})", count=self._incompatible_box.count())
            )

    # ------------------------------------------------------------------ état TensorRT

    def set_trt_state(self, state: object) -> None:
        """Carte TensorRT selon l'état de l'extension (TrtState) : compatible ou déjà installée → disponible."""
        capability = getattr(state, "capability", None)
        device = str(getattr(capability, "device", "") or "")
        installed = getattr(state, "installed", None)
        busy = str(getattr(state, "busy", "") or "")
        progress = int(getattr(state, "progress", -1))
        compatible = bool(getattr(state, "compatible", False))
        probing = capability is None and installed is None      # sonde du GPU pas encore terminée
        available = bool(getattr(state, "visible", False)) or probing
        self._place(self._trt_card, compatible=available)
        if probing:
            status = translate_text("Vérification de la compatibilité de cette machine…")
        elif not available:
            reason = str(getattr(capability, "reason", "") or "")
            status = (translate_text("Non compatible : {reason}.", reason=reason) if reason
                      else translate_text("Non compatible avec cette machine."))
            status += " " + translate_text("Requiert une carte NVIDIA Turing (RTX 20xx, GTX 16xx) ou plus récente, "
                                           "sous Linux ou Windows.")
        elif busy == "remove":
            status = translate_text("Suppression en cours…")
        elif busy == "warmup":
            status = translate_text("Préparation des moteurs TensorRT pour cette carte…")
        elif busy:
            status = (translate_text("Téléchargement : {progress} %", progress=progress) if progress >= 0
                      else translate_text("Téléchargement…"))
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
        message = str(getattr(state, "message", "") or "")
        self._trt_message.setText(message)
        self._trt_message.setVisible(bool(message))
        self._trt_install_btn.setVisible(available and not probing and installed is None)
        self._trt_install_btn.setEnabled(not busy and compatible)
        self._trt_update_btn.setVisible(bool(getattr(state, "update_available", False)))
        self._trt_update_btn.setEnabled(not busy)
        self._trt_remove_btn.setVisible(installed is not None)
        self._trt_remove_btn.setEnabled(not busy)
        self._trt_enabled_box.setVisible(installed is not None)
        self._trt_auto_box.setVisible(available and not probing)
        self._sync_sections()
