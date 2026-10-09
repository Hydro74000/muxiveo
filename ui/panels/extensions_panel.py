"""
ui/panels/extensions_panel.py — Page « Extensions » : installation, mise à jour et suppression des extensions.

Extensions compatibles avec la machine en tête ; les incompatibles sont repliées avec leur raison (jamais
masquées sans explication). Les opérations sont confiées aux contrôleurs (ui/plugin_controller.py) par signaux,
avec l'identifiant de l'extension (``mvo-rife``, ``mvo-rife-trt``).
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

from core import plugins
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

# Ordre d'affichage des cartes.
_ORDER = (plugins.RIFE_PLUGIN_ID, plugins.TRT_PLUGIN_ID)


class _ExtensionCard(QFrame):
    """Carte d'une extension : description, état, dernier message, réglages propres, boutons."""

    def __init__(self, plugin_id: str, title: str, description: str) -> None:
        super().__init__()
        self.plugin_id = plugin_id
        frame = _card()
        frame.setParent(self)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(frame)
        self.setStyleSheet("background:transparent;")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(_scale(16), _scale(16), _scale(16), _scale(16))
        layout.setSpacing(_scale(10))
        heading = QLabel(title)
        heading.setStyleSheet(
            f"color:{_C.TEXT_PRI};font-size:{_font_px(13)}px;font-weight:700;background:transparent;border:none;"
        )
        desc = QLabel(description)
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:{_font_px(11)}px;background:transparent;border:none;")
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.status.setStyleSheet(f"color:{_C.TEXT_PRI};font-size:{_font_px(11)}px;background:transparent;border:none;")
        self.message = QLabel("")
        self.message.setWordWrap(True)
        self.message.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:{_font_px(11)}px;background:transparent;border:none;")
        self.message.hide()
        self.extras = QVBoxLayout()
        self.extras.setSpacing(_scale(6))
        buttons = QHBoxLayout()
        buttons.setSpacing(_scale(8))
        buttons.addStretch()
        self.install_btn = _primary_button("Installer…")
        self.update_btn = _primary_button("Mettre à jour")
        self.remove_btn = _secondary_button("Supprimer")
        for btn in (self.install_btn, self.update_btn, self.remove_btn):
            buttons.addWidget(btn)
        for widget in (heading, desc, self.status, self.message):
            layout.addWidget(widget)
        layout.addLayout(self.extras)
        layout.addLayout(buttons)

    def show_message(self, text: str) -> None:
        self.message.setText(text)
        self.message.setVisible(bool(text))

    def sync_buttons(self, state: object, *, offer_install: bool, install_enabled: bool) -> None:
        installed = getattr(state, "installed", None)
        busy = str(getattr(state, "busy", "") or "")
        self.install_btn.setVisible(offer_install and installed is None)
        self.install_btn.setEnabled(not busy and install_enabled)
        self.update_btn.setVisible(bool(getattr(state, "update_available", False)))
        self.update_btn.setEnabled(not busy)
        self.remove_btn.setVisible(installed is not None)
        self.remove_btn.setEnabled(not busy)


def _busy_status(state: object) -> str:
    busy = str(getattr(state, "busy", "") or "")
    progress = int(getattr(state, "progress", -1))
    if busy == "remove":
        return translate_text("Suppression en cours…")
    if busy == "warmup":
        return translate_text("Préparation des moteurs TensorRT pour cette carte…")
    if busy:
        return (translate_text("Téléchargement : {progress} %", progress=progress) if progress >= 0
                else translate_text("Téléchargement…"))
    return ""


def _installed_status(installed: object) -> str:
    size = round(getattr(installed, "size_bytes", 0) / 1e6)
    return translate_text(
        "Version {version} installée ({size} Mo) dans {path}.", version=getattr(installed, "version", "?"),
        size=size, path=str(getattr(installed, "path", "")),
    )


class ExtensionsPanel(QWidget):
    # Opérations confiées aux contrôleurs : identifiant de l'extension.
    install_requested = Signal(str)
    update_requested = Signal(str)
    remove_requested = Signal(str)
    trt_enabled_toggled = Signal(bool)
    auto_update_toggled = Signal(bool)

    def __init__(self, config: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self._rife_ready = True
        self._trt_state: object | None = None
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
        self._auto_box = QCheckBox("Mettre à jour automatiquement les extensions installées")
        self._auto_box.setStyleSheet(_checkbox_style())
        self._auto_box.setChecked(bool(getattr(self._config, "plugins_auto_update", True)))
        self._auto_box.toggled.connect(self.auto_update_toggled.emit)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addWidget(self._auto_box)
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

        self._rife_card = _ExtensionCard(
            plugins.RIFE_PLUGIN_ID, "Interpolation d'images (MVO-RIFE)",
            "Multiplie la cadence à l'encodage (×2, ×3, ×4, 59,94 / 60 i/s) en générant les images intermédiaires "
            "sur le GPU (Vulkan) : moteur MVO-RIFE, modèles RIFE et préréglages. Mis à jour indépendamment de "
            "Muxiveo (nouveaux modèles, réglages).",
        )
        self._trt_card = _ExtensionCard(
            plugins.TRT_PLUGIN_ID, "Accélération NVIDIA (TensorRT)",
            "Exécute l'interpolation MVO-RIFE sur les Tensor Cores des cartes NVIDIA (NVIDIA TensorRT for RTX) : "
            "traitement nettement plus rapide, images identiques à l'œil. Sans elle, l'interpolation fonctionne "
            "sur le GPU (Vulkan).",
        )
        self._trt_enabled_box = QCheckBox("Utiliser l'accélération TensorRT pour l'interpolation")
        self._trt_enabled_box.setStyleSheet(_checkbox_style())
        self._trt_enabled_box.setChecked(bool(getattr(self._config, "trt_enabled", True)))
        self._trt_enabled_box.toggled.connect(self.trt_enabled_toggled.emit)
        self._trt_card.extras.addWidget(self._trt_enabled_box)
        for card in (self._rife_card, self._trt_card):
            card.install_btn.clicked.connect(lambda _=False, c=card: self.install_requested.emit(c.plugin_id))
            card.update_btn.clicked.connect(lambda _=False, c=card: self.update_requested.emit(c.plugin_id))
            card.remove_btn.clicked.connect(lambda _=False, c=card: self.remove_requested.emit(c.plugin_id))
            self._place(card, compatible=True)
        scroll.setWidget(content)
        root.addWidget(scroll)
        self._sync_sections()

    # ------------------------------------------------------------------ sections

    def _place(self, card: _ExtensionCard, *, compatible: bool) -> None:
        """Range une carte dans la section des extensions disponibles ou des incompatibles (ordre fixe)."""
        target = self._available_box if compatible else self._incompatible_box
        for box in (self._available_box, self._incompatible_box):
            if box is not target and box.indexOf(card) >= 0:
                box.removeWidget(card)
        if target.indexOf(card) >= 0:
            return
        rank = _ORDER.index(card.plugin_id)
        items = (target.itemAt(i) for i in range(target.count()))
        cards = [item.widget() for item in items if item is not None]
        position = sum(1 for w in cards if isinstance(w, _ExtensionCard) and _ORDER.index(w.plugin_id) < rank)
        target.insertWidget(position, card)

    def _sync_sections(self) -> None:
        self._none_label.setVisible(self._available_box.count() == 0)
        self._incompatible_toggle.setVisible(self._incompatible_box.count() > 0)
        self._sync_incompatible()

    def _sync_incompatible(self, _checked: bool = False) -> None:
        count = self._incompatible_box.count()
        self._incompatible_container.setVisible(self._incompatible_toggle.isChecked() and count > 0)
        self._incompatible_toggle.setText(
            ("▾ " if self._incompatible_toggle.isChecked() else "▸ ")
            + translate_text("Non compatibles avec cette machine ({count})", count=count)
        )

    # ------------------------------------------------------------------ états

    def set_rife_state(self, state: object) -> None:
        """Carte Interpolation selon l'état de l'extension (RifeState)."""
        card = self._rife_card
        installed = getattr(state, "installed", None)
        compatible = bool(getattr(state, "compatible", True))
        engine = str(getattr(state, "engine", "") or "")
        self._rife_ready = bool(getattr(state, "ready", False))
        self._place(card, compatible=compatible or installed is not None)
        status = _busy_status(state)
        if not status:
            if not compatible and installed is None:
                status = translate_text(
                    "Non compatible : plate-forme non prise en charge. Requiert Linux ou Windows x86-64, ou macOS "
                    "(Apple Silicon), avec un GPU Vulkan."
                )
            elif installed is None:
                status = (
                    translate_text("Non installée : moteur hors extension utilisé ({path}).", path=engine) if engine
                    else translate_text("Non installée : l'interpolation d'images est indisponible.")
                )
            else:
                status = _installed_status(installed)
                if getattr(state, "update_available", False):
                    status += " " + translate_text("Version {version} disponible.", version=getattr(state, "target", ""))
        card.status.setText(status)
        card.show_message(str(getattr(state, "message", "") or ""))
        card.sync_buttons(state, offer_install=compatible, install_enabled=compatible)
        if self._trt_state is not None:
            self.set_trt_state(self._trt_state)
        self._sync_sections()

    def set_trt_state(self, state: object) -> None:
        """Carte TensorRT selon l'état de l'extension (TrtState) : compatible ou déjà installée → disponible."""
        self._trt_state = state
        card = self._trt_card
        capability = getattr(state, "capability", None)
        device = str(getattr(capability, "device", "") or "")
        installed = getattr(state, "installed", None)
        compatible = bool(getattr(state, "compatible", False))
        probing = capability is None and installed is None      # sonde du GPU pas encore terminée
        available = bool(getattr(state, "visible", False)) or probing
        self._place(card, compatible=available)
        status = _busy_status(state)
        if status:
            pass
        elif probing:
            status = translate_text("Vérification de la compatibilité de cette machine…")
        elif not available:
            reason = str(getattr(capability, "reason", "") or "")
            status = (translate_text("Non compatible : {reason}.", reason=reason) if reason
                      else translate_text("Non compatible avec cette machine."))
            status += " " + translate_text("Requiert une carte NVIDIA Turing (RTX 20xx, GTX 16xx) ou plus récente, "
                                           "sous Linux ou Windows.")
        elif installed is None:
            status = translate_text("Non installée. GPU compatible : {device}.", device=device or "?")
            if not self._rife_ready:
                status += " " + translate_text("Requiert l'extension Interpolation d'images.")
        else:
            ready = bool(getattr(state, "ready", False))
            status = _installed_status(installed) + " " + (
                translate_text("Vérification de la compatibilité de cette machine…") if capability is None
                else translate_text("Active sur {device}.", device=device or "?") if ready
                else translate_text("Inutilisable : {reason}.", reason=str(getattr(capability, "reason", "") or "?"))
            )
            if getattr(state, "update_available", False):
                status += " " + translate_text("Version {version} disponible.", version=getattr(state, "target", ""))
        card.status.setText(status)
        card.show_message(str(getattr(state, "message", "") or ""))
        card.sync_buttons(state, offer_install=available and not probing,
                          install_enabled=compatible and self._rife_ready)
        self._trt_enabled_box.setVisible(installed is not None)
        self._sync_sections()
