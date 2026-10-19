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
            engine = FelEngine.installed()
            devices = engine.devices()
            # Double précision connue seulement si le plugin l'annonce (≥ 0.2.0).
            capabilities = getattr(engine, "capabilities", None)
            known = isinstance(capabilities, dict) and "float64" in (capabilities.get("device_features") or [])
        except FelError:
            devices, known = (), False
        blocked = self.blockSignals(True)
        try:
            self.clear()
            self.addItem(translate_text("Auto"), "auto")
            for device in devices:
                note = f" ({translate_text('sans double précision : CPU en Lent et Standard')})" if known and not device.float64 else ""
                self.addItem(f"GPU {device.index} — {device.name}{note}", device.uuid)
            self.addItem(translate_text("CPU"), "cpu")
            self.set_choice(selected)
        finally:
            self.blockSignals(blocked)

    def showPopup(self) -> None:
        self.refresh_devices()
        super().showPopup()
