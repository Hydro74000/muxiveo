"""
ui/tool_admission.py — Exclusion réciproque, dans le fil Qt, entre l'usage des outils et la modification des
extensions.

Un traitement, un aperçu ou une analyse qui utilise les outils (NVEncC, moteur MVO, mvo-fel…) interdit
d'installer, de mettre à jour ou de supprimer une extension ; une modification d'extension en cours (son
préchauffage compris) interdit d'en démarrer un. Une seule modification à la fois. Les mises à jour
automatiques refusées sont mémorisées et relancées dès que tout est libre (fin d'usage ou de modification),
jamais pendant une boîte de dialogue modale (un lancement peut être en train de se décider).
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication


class ToolAdmission(QObject):
    """Admission des modifications d'extensions face aux usages des outils (fil Qt seulement)."""

    changed = Signal()

    _RETRY_MS = 1000

    def __init__(self, usage_probe: Callable[[], bool], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._usage_probe = usage_probe
        self._mutations: set[str] = set()
        self._pending: dict[str, Callable[[], bool]] = {}  # ordre d'arrivée conservé
        self._draining = False
        self._retry = QTimer(self)
        self._retry.setSingleShot(True)
        self._retry.setInterval(self._RETRY_MS)
        self._retry.timeout.connect(self.drain)

    # ------------------------------------------------------------------ état

    def tools_in_use(self) -> bool:
        """Vrai si un traitement, un aperçu ou une analyse utilise les outils."""
        return bool(self._usage_probe())

    def mutation_active(self) -> bool:
        """Vrai si une extension est en cours d'installation, de mise à jour ou de suppression."""
        return bool(self._mutations)

    def pending(self) -> tuple[str, ...]:
        return tuple(self._pending)

    # ------------------------------------------------------------- mutations

    def acquire_mutation(self, plugin_id: str) -> bool:
        """Réserve une modification d'extension ; faux si les outils sont utilisés ou si une autre est en cours."""
        if self._mutations or self.tools_in_use():
            return False
        self._mutations.add(plugin_id)
        self.changed.emit()
        return True

    def release_mutation(self, plugin_id: str) -> None:
        """Fin réelle d'une modification (toute issue) : libère et relance les mises à jour en attente."""
        if plugin_id in self._mutations:
            self._mutations.discard(plugin_id)
            self.changed.emit()
        self.drain()

    def start_mutation(self, plugin_id: str, start: Callable[[], bool]) -> bool:
        """Réserve puis lance ``start`` (vrai s'il a démarré) ; réservation rendue s'il n'a pas démarré."""
        if not self.acquire_mutation(plugin_id):
            return False
        if not start():
            self.release_mutation(plugin_id)
            return False
        return True

    def defer(self, plugin_id: str, start: Callable[[], bool]) -> None:
        """Mise à jour automatique : lancée dès que possible (une seule demande par extension)."""
        self._pending.setdefault(plugin_id, start)
        self.drain()

    # ---------------------------------------------------------------- usages

    def usage_changed(self) -> None:
        """Un usage commence ou se termine : état publié, mises à jour en attente relancées si tout est libre."""
        self.changed.emit()
        self.drain()

    def drain(self) -> None:
        """Lance les mises à jour en attente, une à la fois, quand ni usage, ni modification, ni dialogue."""
        if self._draining:
            return
        self._draining = True
        try:
            while self._pending and not self._mutations and not self.tools_in_use():
                if QApplication.activeModalWidget() is not None:
                    break
                plugin_id = next(iter(self._pending))
                start = self._pending.pop(plugin_id)
                self.start_mutation(plugin_id, start)
        finally:
            self._draining = False
        if self._pending and not self._mutations:
            self._retry.start()  # usage ou dialogue en cours : nouvel essai (fin d'usage = relance immédiate)


__all__ = ["ToolAdmission"]
