"""Précision de la reconstruction FEL : Lent (recommandé), Standard ou Rapide."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import QComboBox, QWidget

from core.fel.devices import precision_choice
from core.i18n import translate_text

# (valeur, libellé, infobulle) ; textes traduits à l'affichage.
PRECISIONS = (
    ("exact", "Lent (recommandé)",
     "Résultat certifié : chaque couleur est l’arrondi exact démontré du calcul, identique au bit près à "
     "la référence CPU, y compris aux couleurs extrêmes. Sur GPU, exige un GPU calculant en double "
     "précision (NVIDIA, AMD, Intel jusqu’à la 11e génération) ; en Auto, un GPU dédié, sinon le CPU. "
     "Environ 2 fois plus lent que le mode Standard sur GPU NVIDIA récent, 3 fois sur GPU intégré ; sans "
     "effet sur la durée quand l’encodeur est plus lent que lui (x265, NVEncC en qualité élevée)."),
    ("standard", "Standard (rapide, moins précis)",
     "Hybride automatique avec légère dérive couleur possible aux extrêmes de 1/64e de code couleur. "
     "Non visible à l’œil nu. Exige un GPU calculant en double précision (NVIDIA, AMD, Intel jusqu’à "
     "la 11e génération) ; sinon la reconstruction bascule automatiquement sur le CPU, plus lent mais exact. "
     "Sur GPU intégré, le gain face au CPU peut être faible."),
    ("fast", "Rapide (non recommandé)",
     "Calcul en simple précision, compatible avec tous les GPU. Écart d’au plus 1/64e de code couleur sur "
     "les films mesurés ; jusqu’à 2 codes couleur possibles sur des couleurs saturées très lumineuses "
     "(tests synthétiques). Sur CPU, le calcul reste exact."),
)


class FelPrecisionCombo(QComboBox):
    def __init__(self, warning_color: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        for value, label, tip in PRECISIONS:
            self.addItem(translate_text(label), value)
            self.setItemData(self.count()-1, translate_text(tip), Qt.ItemDataRole.ToolTipRole)
        # Mode déconseillé signalé dans la liste.
        self.setItemData(self.count()-1, QBrush(QColor(warning_color)), Qt.ItemDataRole.ForegroundRole)
        self.currentIndexChanged.connect(self._sync_tooltip)
        self.set_choice("exact")

    def set_choice(self, choice: object) -> None:
        was_blocked = self.blockSignals(True)
        try:
            self.setCurrentIndex(max(0, self.findData(precision_choice(choice))))
        finally:
            self.blockSignals(was_blocked)
        self._sync_tooltip()

    def choice(self) -> str:
        return precision_choice(self.currentData())

    def _sync_tooltip(self, *_args: object) -> None:
        self.setToolTip(str(self.itemData(self.currentIndex(), Qt.ItemDataRole.ToolTipRole) or ""))
