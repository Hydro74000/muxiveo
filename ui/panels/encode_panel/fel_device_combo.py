"""Liste matérielle FEL actualisée à chaque ouverture, choix conservé par UUID."""
from __future__ import annotations

from PySide6.QtWidgets import QComboBox, QWidget

from core.fel.devices import device_choice
from core.fel.engine import FelEngine, FelError
from core.i18n import translate_text


class FelDeviceCombo(QComboBox):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.set_choice("auto")

    def set_choice(self, choice: str) -> None:
        selected = device_choice(choice)
        was_blocked = self.blockSignals(True)
        try:
            if not self.count():
                self.addItem(translate_text("Auto"), "auto")
                self.addItem(translate_text("CPU"), "cpu")
            index = self.findData(selected)
            if index < 0:
                self.addItem(translate_text("GPU indisponible ({id})", id=selected[:8]), selected)
                index = self.count()-1
            self.setCurrentIndex(index)
        finally:
            self.blockSignals(was_blocked)

    def refresh_devices(self) -> None:
        selected = str(self.currentData() or "auto")
        try:
            devices = FelEngine.installed().devices()
        except FelError:
            devices = ()
        blocked = self.blockSignals(True)
        try:
            self.clear()
            self.addItem(translate_text("Auto"), "auto")
            for device in devices:
                self.addItem(f"GPU {device.index} — {device.name}", device.uuid)
            self.addItem(translate_text("CPU"), "cpu")
            self.set_choice(selected)
        finally:
            self.blockSignals(blocked)

    def showPopup(self) -> None:
        self.refresh_devices()
        super().showPopup()
