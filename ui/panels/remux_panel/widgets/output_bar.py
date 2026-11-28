"""Barre « Fichier de sortie » : fixe sous le défilement des pages Conteneur et Encodage.

Une seule instance, créée par le panneau Conteneur (propriétaire du chemin et du backend de muxage) et placée
dans la page affichée (:meth:`OutputBar.follow_scroll`) : rien à synchroniser entre les deux pages.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QIcon, QLinearGradient, QPainter, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from core.i18n import translate_text
from ui.design_system import colors as C, font_px, scale
from ui.styles import _card

#: États du champ : normal, vide alors que des sources sont chargées, fichier présent, dossier absent.
STATES = ("normal", "empty", "exists", "missing_dir")
#: Marge horizontale des pages (contenu du défilement), à laquelle la carte s'aligne.
PAGE_MARGIN = 28


def _state_icon(color: str, glyph: str) -> QIcon:
    """Pastille ronde de couleur ``color`` portant ``glyph`` (indicateur de fin de champ)."""
    size = scale(28)
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(color))
    painter.drawEllipse(QRectF(2, 2, size - 4, size - 4))
    font = QFont()
    font.setBold(True)
    font.setPixelSize(int(size * 0.65))
    painter.setFont(font)
    painter.setPen(QColor(C.BG_DEEP))
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, glyph)
    painter.end()
    return QIcon(pixmap)


class OutputBar(QWidget):
    """Chemin de sortie, « Choisir… » et backend de muxage dans une carte alignée sur le contenu de la page."""

    def __init__(self, output_edit: QLineEdit, browse_button: QPushButton, backend_combo: QComboBox,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("outputBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        # Fond de la page : la barre appartient à l'onglet, pas au cadre fixe de la fenêtre.
        self.setStyleSheet(f"QWidget#outputBar{{background:{C.BG_DEEP};}}")
        self._edit = output_edit
        self._required = False
        self._state = "normal"
        self._scroll: QScrollArea | None = None

        self._outer = QVBoxLayout(self)
        self._outer.setSpacing(0)
        card = _card()
        card.setStyleSheet(card.styleSheet() + "QLabel{background:transparent;border:none;}")
        self._outer.addWidget(card)
        row = QHBoxLayout(card)
        row.setContentsMargins(scale(16), scale(9), scale(12), scale(9))
        row.setSpacing(scale(8))

        label = QLabel(translate_text("FICHIER DE SORTIE"))
        label.setStyleSheet(f"color:{C.TEXT_DIM};font-size:{font_px(9)}px;font-weight:700;letter-spacing:1.5px;")
        row.addWidget(label)
        row.addSpacing(scale(6))

        self._base_style = output_edit.styleSheet()
        output_edit.setFixedHeight(scale(30))
        output_edit.setMinimumWidth(scale(160))
        self._state_action = output_edit.addAction(QIcon(), QLineEdit.ActionPosition.TrailingPosition)
        self._state_action.setVisible(False)
        row.addWidget(output_edit, stretch=1)
        browse_button.setFixedHeight(scale(30))
        row.addWidget(browse_button)

        separator = QWidget()
        separator.setFixedSize(1, scale(20))
        separator.setStyleSheet(f"background:{C.BORDER};")
        row.addSpacing(scale(8))
        row.addWidget(separator)
        row.addSpacing(scale(8))
        mux_label = QLabel(translate_text("Muxage"))
        mux_label.setToolTip(translate_text("Backend de muxage"))
        mux_label.setStyleSheet(f"color:{C.TEXT_SEC};font-size:{font_px(11)}px;")
        row.addWidget(mux_label)
        backend_combo.setToolTip(translate_text("Backend de muxage"))
        backend_combo.setFixedHeight(scale(30))
        backend_combo.setMinimumWidth(scale(140))
        row.addWidget(backend_combo)

        # Relecture du disque différée pendant la frappe (aucune sonde par caractère).
        self._state_timer = QTimer(self)
        self._state_timer.setSingleShot(True)
        self._state_timer.setInterval(250)
        self._state_timer.timeout.connect(self.refresh_state)
        output_edit.textChanged.connect(self._state_timer.start)
        self._set_margins(0)

    @property
    def state(self) -> str:
        return self._state

    def follow_scroll(self, scroll: QScrollArea) -> None:
        """Aligne la carte sur le contenu de ``scroll`` (page hôte), barre de défilement visible ou non."""
        if self._scroll is scroll:
            return
        if self._scroll is not None:
            self._scroll.verticalScrollBar().rangeChanged.disconnect(self._on_scroll_range)
        self._scroll = scroll
        scroll.verticalScrollBar().rangeChanged.connect(self._on_scroll_range)
        self._on_scroll_range()

    def _on_scroll_range(self, *_args) -> None:
        bar = self._scroll.verticalScrollBar() if self._scroll is not None else None
        self._set_margins(bar.sizeHint().width() if bar is not None and bar.maximum() > 0 else 0)

    def _set_margins(self, scrollbar_width: int) -> None:
        margin = scale(PAGE_MARGIN)
        self._outer.setContentsMargins(margin, scale(6), margin + scrollbar_width, scale(14))

    def set_required(self, required: bool) -> None:
        """Sources chargées : un chemin vide est alors signalé."""
        if required != self._required:
            self._required = required
            self.refresh_state()

    def refresh_state(self) -> None:
        """Relit l'état de la destination et met à jour l'indicateur du champ."""
        self._state_timer.stop()
        self._apply_state(*self._evaluate())

    def _evaluate(self) -> tuple[str, str]:
        text = self._edit.text().strip()
        if not text:
            return ("empty" if self._required else "normal"), ""
        path = Path(text).expanduser()
        try:
            if path.exists():
                return "exists", translate_text("Fichier existant — son remplacement sera demandé au lancement.")
            if not path.parent.is_dir():
                return "missing_dir", translate_text("Dossier de destination introuvable : {folder}",
                                                     folder=str(path.parent))
        except OSError as exc:
            return "missing_dir", translate_text("Destination illisible : {error}", error=str(exc))
        return "normal", ""

    def _apply_state(self, state: str, message: str) -> None:
        self._state = state
        border = {"empty": C.WARN, "exists": C.WARN, "missing_dir": C.ERROR}.get(state)
        self._edit.setStyleSheet(self._base_style + (f"QLineEdit{{border-color:{border};}}" if border else ""))
        icon = {"exists": (C.WARN, "!"), "missing_dir": (C.ERROR, "×")}.get(state)
        self._state_action.setVisible(icon is not None)
        if icon is not None:
            self._state_action.setIcon(_state_icon(*icon))
        self._state_action.setToolTip(message)
        self._edit.setToolTip(message or self._edit.text().strip())

    def showEvent(self, event) -> None:  # noqa: N802 — API Qt
        super().showEvent(event)
        # Page affichée de nouveau : la destination a pu apparaître (traitement terminé) ou disparaître.
        self.refresh_state()


class ScrollFade(QWidget):
    """Fondu du bas d'un défilement vers le fond de page, quand du contenu reste en dessous.

    Superposé à la zone visible, transparent à la souris : le contenu ne s'arrête pas net au-dessus d'une barre fixe.
    """

    def __init__(self, scroll: QScrollArea, height: int = 22) -> None:
        super().__init__(scroll.viewport())
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._viewport = scroll.viewport()
        self._height = scale(height)
        self._bar = scroll.verticalScrollBar()
        self._viewport.installEventFilter(self)
        self._bar.rangeChanged.connect(self._update)
        self._bar.valueChanged.connect(self._update)
        self._update()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 — API Qt
        if watched is self._viewport and event.type() == QEvent.Type.Resize:
            self._update()
        return False

    def _update(self, *_args) -> None:
        self.setGeometry(0, self._viewport.height() - self._height, self._viewport.width(), self._height)
        self.setVisible(self._bar.value() < self._bar.maximum())
        self.raise_()

    def paintEvent(self, _event) -> None:  # noqa: N802 — API Qt
        gradient = QLinearGradient(0, 0, 0, self.height())
        transparent = QColor(C.BG_DEEP)
        transparent.setAlpha(0)
        gradient.setColorAt(0, transparent)
        gradient.setColorAt(1, QColor(C.BG_DEEP))
        painter = QPainter(self)
        painter.fillRect(self.rect(), gradient)
        painter.end()
