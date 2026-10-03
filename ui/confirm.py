"""
ui/confirm.py — Confirmations des actions qui détruisent un résultat ou un travail en cours.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, cast

from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import QMessageBox, QWidget

from core.i18n import translate_text
from core.workflows.common.validation_override import ValidationOverrideRequest


def confirm_overwrite(parent: QWidget | None, output: Path) -> bool:
    """Vrai si ``output`` n'existe pas ou si l'utilisateur accepte de le remplacer."""
    if not output.exists():
        return True
    reply = QMessageBox.question(
        parent,
        translate_text("Fichier existant"),
        translate_text(
            "{name} existe déjà dans {folder}.\nLe remplacer à la fin du traitement ?",
            name=output.name,
            folder=str(output.parent),
        ),
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    return reply == QMessageBox.StandardButton.Yes


def confirm_close_while_running(parent: QWidget | None, elapsed_s: float) -> bool:
    """Vrai si l'utilisateur accepte d'annuler l'opération en cours pour quitter."""
    reply = QMessageBox.warning(
        parent,
        translate_text("Opération en cours"),
        translate_text(
            "Une opération est en cours depuis {minutes} min.\n"
            "Quitter l'annulera et le fichier partiel sera supprimé.\nQuitter quand même ?",
            minutes=int(max(0.0, elapsed_s) // 60),
        ),
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    return reply == QMessageBox.StandardButton.Yes


class ValidationOverridePrompt(QObject):
    """Le worker attend une décision ; seul le thread Qt affiche la popup."""

    requested = Signal(object)

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.requested.connect(self._show, Qt.ConnectionType.QueuedConnection)

    def request(self, path: Path, message: str, cancelled: Callable[[], bool]) -> bool:
        request = ValidationOverrideRequest(path, message, cancelled)
        self.requested.emit(request)
        return request.wait()

    @Slot(object)
    def _show(self, request: ValidationOverrideRequest) -> None:
        parent = cast(QWidget | None, self.parent())
        if request.cancelled() or getattr(parent, "_closing", False):
            request.resolve(False)
            return
        try:
            dialog = QMessageBox(parent)
            dialog.setIcon(QMessageBox.Icon.Warning)
            dialog.setWindowTitle(translate_text("Contrôle final rejeté"))
            dialog.setText(translate_text(
                    "Le contrôle final a rejeté {name}.\n\n{reason}\n\n"
                    "Poursuivre quand même et conserver le résultat ?\n"
                    "Le fichier peut être incomplet ou ses métadonnées désalignées.",
                    name=request.path.name, reason=request.message,
                ))
            dialog.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            dialog.setDefaultButton(QMessageBox.StandardButton.No)
            timer = QTimer(dialog)
            timer.setInterval(50)
            timer.timeout.connect(lambda: dialog.reject() if request.cancelled() else None)
            timer.start()
            reply = dialog.exec()
            timer.stop()
            dialog.deleteLater()
            request.resolve(reply == QMessageBox.StandardButton.Yes)
        finally:
            # Un échec du slot ne doit pas laisser le worker attendre indéfiniment.
            if not request.resolved:
                request.resolve(False)


__all__ = ["confirm_close_while_running", "confirm_overwrite", "ValidationOverridePrompt"]
