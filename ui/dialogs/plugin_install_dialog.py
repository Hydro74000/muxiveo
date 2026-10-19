"""
ui/dialogs/plugin_install_dialog.py — consentement à l'installation de l'accélération NVIDIA (TensorRT).

Un seul dialogue : ce que fait l'extension (sans chiffres de performance, licence NVIDIA), taille du
téléchargement, emplacement, licence NVIDIA à accepter. L'installation elle-même se fait en arrière-plan
(TrtPluginController).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QCheckBox, QDialog, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from core import plugins
from core.i18n import apply_translations, translate_text
from ui.design_system import colors as _C
from ui.styles import _checkbox_style, _primary_button, _secondary_button

# Ordre de grandeur affiché tant que la taille réelle n'est pas connue (archive Linux ou Windows).
_APPROX_SIZE_MB = 140


class PluginInstallDialog(QDialog):
    """Consentement à l'installation de mvo-rife-trt (taille, emplacement, licence NVIDIA)."""

    _size_ready = Signal(int)

    def __init__(self, device: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(translate_text("Accélération NVIDIA (TensorRT)"))
        self.setModal(True)
        self.setMinimumWidth(520)
        self.setStyleSheet(f"QDialog{{background:{_C.BG_DEEP};}}")
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        title = QLabel(translate_text("Installer l'accélération NVIDIA (TensorRT) ?"))
        title.setStyleSheet(f"color:{_C.TEXT_PRI};font-size:15px;font-weight:700;background:transparent;")
        layout.addWidget(title)

        intro = QLabel(translate_text(
            "L'interpolation MVO-RIFE peut s'exécuter sur les Tensor Cores de votre carte NVIDIA{device} avec "
            "NVIDIA TensorRT for RTX : traitement nettement plus rapide, images identiques à l'œil. Sans cette "
            "extension, l'interpolation continue de fonctionner sur le GPU (Vulkan).",
            device=f" ({device})" if device else "",
        ))
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color:{_C.TEXT_SEC};background:transparent;")
        layout.addWidget(intro)

        self._size_label = QLabel(self._size_text(0))
        self._size_label.setWordWrap(True)
        self._size_label.setStyleSheet(f"color:{_C.TEXT_SEC};background:transparent;")
        layout.addWidget(self._size_label)

        license_label = QLabel(translate_text(
            "L'extension contient NVIDIA TensorRT for RTX, distribué sous <a href=\"{url}\">licence NVIDIA</a>, "
            "pour un usage sur carte NVIDIA.", url=plugins.TRT_LICENSE_URL,
        ))
        license_label.setWordWrap(True)
        license_label.setOpenExternalLinks(True)
        license_label.setTextFormat(Qt.TextFormat.RichText)
        license_label.setStyleSheet(f"color:{_C.TEXT_SEC};background:transparent;")
        layout.addWidget(license_label)

        self._accept_box = QCheckBox(translate_text("J'accepte la licence NVIDIA TensorRT for RTX"))
        self._accept_box.setStyleSheet(_checkbox_style())
        layout.addWidget(self._accept_box)

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = _secondary_button(translate_text("Annuler"))
        cancel.clicked.connect(self.reject)
        self._install = _primary_button(translate_text("Installer"))
        self._install.setEnabled(False)
        self._install.clicked.connect(self.accept)
        self._accept_box.toggled.connect(self._install.setEnabled)
        buttons.addWidget(cancel)
        buttons.addWidget(self._install)
        layout.addLayout(buttons)

        self._size_ready.connect(self._on_size, Qt.ConnectionType.QueuedConnection)
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._executor.submit(self._fetch_size)
        apply_translations(self)

    @staticmethod
    def _size_text(size: int) -> str:
        megabytes = round(size / 1e6) if size > 0 else _APPROX_SIZE_MB
        approx = "" if size > 0 else "≈ "
        return translate_text(
            "Téléchargement : {approx}{size} Mo, installé dans {path}. Mises à jour automatiques ; "
            "suppression à tout moment dans la page Extensions.",
            approx=approx, size=megabytes, path=str(plugins.plugins_root()),
        )

    def _fetch_size(self) -> None:
        try:
            size = plugins.asset_size(plugins.release_asset(plugins.TRT))
        except plugins.PluginError:
            size = 0
        try:
            self._size_ready.emit(size)
        except RuntimeError:
            pass  # dialogue déjà fermé

    def _on_size(self, size: int) -> None:
        self._size_label.setText(self._size_text(size))

    def done(self, result: int) -> None:  # noqa: D401 — QDialog
        self._executor.shutdown(wait=False, cancel_futures=True)
        super().done(result)
