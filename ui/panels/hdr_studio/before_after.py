"""Comparateur avant / après du HDR Studio : trait de séparation à glisser, zoom à la molette, déplacement."""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter, QPainterPath, QPen, QPolygonF, QWheelEvent
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from core.hdr_inspection.fel_analyzer import MomentImages, luminance_nits, render_sdr
from core.i18n import translate_text
from ui.design_system import colors as C, font_px, scale
from ui.panels.hdr_studio.chart import format_time
from ui.styles import _combo_style, _secondary_button

#: Zoom maximal (pixels de l'écran par pixel de l'image).
MAX_ZOOM = 16.0
_WHEEL_STEP = 1.25


def to_qimage(rgb: np.ndarray) -> QImage:
    """Image RGB 8 bits (H × W × 3) → QImage indépendante du tableau."""
    data = np.ascontiguousarray(rgb)
    height, width, _channels = data.shape
    return QImage(data.data, width, height, 3 * width, QImage.Format.Format_RGB888).copy()


class BeforeAfterView(QWidget):
    """Deux images de même taille : gauche avant le trait, droite après ; zoom autour du curseur."""

    #: Pixel de l'image sous le curseur (x, y), (-1, -1) hors de l'image.
    hovered = Signal(int, int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._images: tuple[QImage, QImage] | None = None
        self._labels = ("", "")
        self._split = 0.5  # part de la largeur du widget
        self._zoom = 1.0
        self._center = QPointF()  # point de l'image au centre du widget
        self._fitted = True
        self._drag: str | None = None  # "split" | "pan"
        self._last = QPointF()
        self.setMouseTracking(True)
        self.setMinimumSize(scale(320), scale(200))
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    # ── Données ──────────────────────────────────────────────────────────
    def set_images(self, left: QImage, right: QImage, labels: tuple[str, str] = ("", "")) -> None:
        """Images à comparer ; la vue (zoom, position, trait) est conservée si la taille ne change pas."""
        same = self._images is not None and self._images[0].size() == left.size()
        self._images = (left, right)
        self._labels = labels
        if not same:
            self.fit()
        self.update()

    def zoom(self) -> float:
        return self._zoom

    def split(self) -> float:
        return self._split

    def set_split(self, fraction: float) -> None:
        self._split = min(1.0, max(0.0, fraction))
        self.update()

    # ── Vue ──────────────────────────────────────────────────────────────
    def _fit_zoom(self) -> float:
        if self._images is None or self._images[0].isNull():
            return 1.0
        size = self._images[0].size()
        return min(self.width() / max(1, size.width()), self.height() / max(1, size.height()))

    def fit(self) -> None:
        """Image entière dans la vue."""
        self._fitted = True
        self._zoom = self._fit_zoom()
        if self._images is not None:
            size = self._images[0].size()
            self._center = QPointF(size.width() / 2, size.height() / 2)
        self.update()

    def zoom_to(self, zoom: float, anchor: QPointF | None = None) -> None:
        """Zoom borné (image entière ↔ ``MAX_ZOOM``), le point sous ``anchor`` restant fixe."""
        if self._images is None:
            return
        anchor = anchor if anchor is not None else QPointF(self.width() / 2, self.height() / 2)
        minimum = min(1.0, self._fit_zoom())
        zoom = min(MAX_ZOOM, max(minimum, zoom))
        point = self.image_point(anchor)
        self._zoom = zoom
        middle = QPointF(self.width() / 2, self.height() / 2)
        self._center = point - (anchor - middle) / zoom
        self._fitted = zoom <= self._fit_zoom() + 1e-9
        if self._fitted:
            self.fit()
            return
        self._clamp()
        self.update()

    def image_point(self, position: QPointF) -> QPointF:
        """Point de l'image (pixels) sous une position du widget."""
        middle = QPointF(self.width() / 2, self.height() / 2)
        return self._center + (position - middle) / self._zoom

    def _clamp(self) -> None:
        """Garde l'image sous le centre de la vue (pas de fuite hors champ)."""
        if self._images is None:
            return
        size = self._images[0].size()
        self._center = QPointF(min(max(self._center.x(), 0.0), float(size.width())),
                               min(max(self._center.y(), 0.0), float(size.height())))

    def _target(self) -> QRectF:
        assert self._images is not None
        size = self._images[0].size()
        left = self.width() / 2 - self._center.x() * self._zoom
        top = self.height() / 2 - self._center.y() * self._zoom
        return QRectF(left, top, size.width() * self._zoom, size.height() * self._zoom)

    # ── Événements ───────────────────────────────────────────────────────
    def resizeEvent(self, event) -> None:  # noqa: N802 (API Qt)
        if self._fitted:
            self.fit()
        super().resizeEvent(event)

    def _near_split(self, x: float) -> bool:
        return abs(x - self._split * self.width()) <= scale(14)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton or self._images is None:
            return
        position = event.position()
        self._drag = "split" if self._near_split(position.x()) else "pan"
        self._last = position
        if self._drag == "split":
            self.set_split(position.x() / max(1, self.width()))
        else:
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        position = event.position()
        if self._drag == "split":
            self.set_split(position.x() / max(1, self.width()))
        elif self._drag == "pan":
            self._center -= (position - self._last) / self._zoom
            self._last = position
            self._fitted = False
            self._clamp()
            self.update()
        else:
            self.setCursor(Qt.CursorShape.SplitHCursor if self._near_split(position.x())
                           else Qt.CursorShape.OpenHandCursor)
        if self._images is not None:
            point = self.image_point(position)
            size = self._images[0].size()
            inside = 0 <= point.x() < size.width() and 0 <= point.y() < size.height()
            self.hovered.emit(int(point.x()) if inside else -1, int(point.y()) if inside else -1)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._drag = None
        self.setCursor(Qt.CursorShape.SplitHCursor if self._near_split(event.position().x())
                       else Qt.CursorShape.OpenHandCursor)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        """Double-clic : pixels 1:1 à l'endroit visé, ou retour à l'image entière."""
        if self._fitted:
            self.zoom_to(1.0 if self._fit_zoom() < 1.0 else 2.0, event.position())
        else:
            self.fit()

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        steps = event.angleDelta().y() / 120
        if steps:
            self.zoom_to(self._zoom * _WHEEL_STEP ** steps, event.position())

    def leaveEvent(self, event) -> None:  # noqa: N802
        self.hovered.emit(-1, -1)
        super().leaveEvent(event)

    # ── Dessin ───────────────────────────────────────────────────────────
    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(C.BG_DEEP))
        if self._images is None:
            painter.end()
            return
        target = self._target()
        # Lissage en vue réduite, pixels nets au-delà de 2 × (inspection du grain et du détail).
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, self._zoom < 2.0)
        split_x = self._split * self.width()
        for image, clip in ((self._images[0], QRectF(0, 0, split_x, self.height())),
                            (self._images[1], QRectF(split_x, 0, self.width() - split_x, self.height()))):
            visible = clip.intersected(target)
            if visible.isEmpty():
                continue
            source = QRectF((visible.left() - target.left()) / self._zoom, (visible.top() - target.top()) / self._zoom,
                            visible.width() / self._zoom, visible.height() / self._zoom)
            painter.drawImage(visible, image, source)
        self._paint_handle(painter, split_x)
        self._paint_labels(painter, split_x)
        painter.end()

    def _paint_handle(self, painter: QPainter, x: float) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(QColor(255, 255, 255, 230), max(2, scale(2))))
        painter.drawLine(QPointF(x, 0), QPointF(x, self.height()))
        radius = scale(18)
        middle = QPointF(x, self.height() / 2)
        painter.setBrush(QColor(0, 0, 0, 110))
        painter.drawEllipse(middle, radius, radius)
        painter.setBrush(QColor(255, 255, 255, 230))
        painter.setPen(Qt.PenStyle.NoPen)
        arrow = scale(5)
        for direction in (-1, 1):
            tip = middle + QPointF(direction * radius * 0.62, 0)
            base = middle + QPointF(direction * radius * 0.22, 0)
            painter.drawPolygon(QPolygonF([tip, base + QPointF(0, -arrow), base + QPointF(0, arrow)]))

    def _paint_labels(self, painter: QPainter, split_x: float) -> None:
        font = painter.font()
        font.setPixelSize(font_px(10))
        font.setBold(True)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        margin, pad = scale(10), scale(6)
        for index, text in enumerate(self._labels):
            if not text:
                continue
            width = metrics.horizontalAdvance(text) + 2 * pad
            height = metrics.height() + pad
            left = margin if index == 0 else self.width() - margin - width
            # Étiquette masquée quand le trait la recouvre (côté presque vide).
            if (index == 0 and split_x < left + width) or (index == 1 and split_x > left):
                continue
            box = QRectF(left, margin, width, height)
            path = QPainterPath()
            path.addRoundedRect(box, scale(4), scale(4))
            painter.fillPath(path, QColor(0, 0, 0, 150))
            painter.setPen(QColor(255, 255, 255))
            painter.drawText(box, Qt.AlignmentFlag.AlignCenter, text)


class MomentCompareDialog(QDialog):
    """Moment du FEL Analyzer en pleine définition : sans FEL (P8.1) | avec FEL ou carte de l'écart."""

    def __init__(self, images: MomentImages, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._images = images
        self.setWindowTitle(translate_text("FEL — moment {time}", time=format_time(images.time_s)))
        self.setStyleSheet(f"QDialog{{background:{C.BG_PANEL};}}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(scale(10), scale(10), scale(10), scale(10))
        layout.setSpacing(scale(8))
        bar = QHBoxLayout()
        bar.setSpacing(scale(8))
        bar.addWidget(self._label(translate_text("Exposition")))
        self.exposure = QDoubleSpinBox()
        self.exposure.setRange(-3.0, 6.0)
        self.exposure.setSingleStep(0.5)
        self.exposure.setDecimals(1)
        self.exposure.setSuffix(" EV")
        self.exposure.setToolTip(translate_text(
            "Éclaircit ou assombrit l'aperçu (les deux côtés) pour examiner ombres ou hautes lumières."))
        self.exposure.setStyleSheet(f"QDoubleSpinBox{{background:{C.BG_CARD};color:{C.TEXT_PRI};"
                                    f"border:1px solid {C.BORDER};border-radius:5px;padding:{scale(3)}px "
                                    f"{scale(6)}px;font-size:{font_px(11)}px;}}")
        bar.addWidget(self.exposure)
        self.right = QComboBox()
        self.right.addItem(translate_text("Avec FEL"), "reconstructed")
        self.right.addItem(translate_text("Carte de l'écart (16 codes = jaune)"), "difference")
        self.right.setStyleSheet(_combo_style())
        bar.addWidget(self.right)
        fit = _secondary_button(translate_text("Image entière"))
        actual = _secondary_button(translate_text("100 %"))
        bar.addWidget(fit)
        bar.addWidget(actual)
        bar.addStretch()
        bar.addWidget(self._label(translate_text("Molette : zoom · glisser : déplacer · trait : comparer · "
                                                 "double-clic : 100 % / image entière"), dim=True))
        layout.addLayout(bar)
        self.view = BeforeAfterView()
        layout.addWidget(self.view, 1)
        self.readout = self._label("", dim=True)
        layout.addWidget(self.readout)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(60)
        self._timer.timeout.connect(self._render)
        self.exposure.valueChanged.connect(lambda _value: self._timer.start())
        self.right.currentIndexChanged.connect(lambda _index: self._render())
        fit.clicked.connect(self.view.fit)
        actual.clicked.connect(lambda: self.view.zoom_to(1.0))
        self.view.hovered.connect(self._show_pixel)
        self._render()

    @staticmethod
    def _label(text: str, *, dim: bool = False) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet(f"color:{C.TEXT_DIM if dim else C.TEXT_SEC};font-size:{font_px(10 if dim else 11)}px;"
                            "background:transparent;")
        return label

    def _render(self) -> None:
        exposure = float(self.exposure.value())
        left = to_qimage(render_sdr(self._images.mapped, exposure))
        if self.right.currentData() == "difference":
            right, title = to_qimage(self._images.difference), translate_text("Écart")
        else:
            right, title = to_qimage(render_sdr(self._images.reconstructed, exposure)), translate_text("Avec FEL")
        self.view.set_images(left, right, (translate_text("Sans FEL (P8.1)"), title))

    def _show_pixel(self, x: int, y: int) -> None:
        if x < 0 or y < 0:
            self.readout.setText("")
            return
        before = luminance_nits(self._images.mapped[y, x])
        after = luminance_nits(self._images.reconstructed[y, x])
        change = f" ({(after / before - 1) * 100:+.1f} %)" if before > 0.01 else ""
        self.readout.setText(translate_text("Pixel {x}, {y} : sans FEL {before} nits · avec FEL {after} nits{change}",
                                            x=x, y=y, before=f"{before:.2f}", after=f"{after:.2f}", change=change))
