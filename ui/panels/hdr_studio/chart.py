"""Courbe de luminance dans le temps : échelle PQ (perceptuelle) ou linéaire, plage dynamique, deux axes.

Axe principal (gauche) : pics, affichage simulé de la TV et repères ; axe secondaire (droite) : moyennes.
La plage de chaque axe suit les valeurs visibles (zoom, déplacement) ; un repère hors plage est signalé
en bord haut au lieu d'écraser la courbe.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPainterPath, QPen, QWheelEvent
from PySide6.QtWidgets import QCheckBox, QComboBox, QHBoxLayout, QSizePolicy, QWidget

from core.hdr_inspection import axes
from core.hdr_inspection.axes import SCALES
from core.hdr_inspection.summary import bucket, format_time
from core.i18n import translate_text
from ui.design_system import colors as C, font_px, scale
from ui.styles import _checkbox_style, _combo_style

_TIME_STEPS = (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600)


@dataclass
class ChartSeries:
    key: str
    label: str
    color: str
    #: Nits par image.
    values: np.ndarray
    #: Agrégation par colonne de pixels : « max » (pics) ou « mean » (moyennes).
    mode: str = "max"
    visible: bool = True
    fill: bool = False


@dataclass(frozen=True)
class ChartMarker:
    label: str
    nits: float
    color: str


class LuminanceChart(QWidget):
    """Séries par image (nits), repères horizontaux, plans, zoom à la molette, glisser pour se déplacer."""

    frame_clicked = Signal(int)
    hover_changed = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setMinimumHeight(scale(260))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._series: list[ChartSeries] = []
        self._markers: list[ChartMarker] = []
        self._scenes: np.ndarray | None = None
        self._frames = 0
        self._fps = 24000 / 1001
        self._view = (0, 0)
        self._hover_x: float | None = None
        self._drag: tuple[float, tuple[int, int]] | None = None
        self._dragged = False
        self._bands: np.ndarray | None = None
        self._band_color = C.ERROR
        self._scale = "pq"
        self._dual = True
        #: Plages (bas, haut) en nits des axes principal et secondaire, recalculées à chaque rendu.
        self._ranges: dict[str, tuple[float, float]] = {"main": (0.1, 1000.0), "second": (0.1, 1000.0)}

    # ── Données ─────────────────────────────────────────────────────────
    def set_data(self, *, frames: int, fps: float | None, series: list[ChartSeries],
                 markers: list[ChartMarker] | None = None, scenes: np.ndarray | None = None,
                 keep_view: bool = False) -> None:
        """Remplace séries et repères ; ``keep_view`` garde le zoom si la durée est inchangée."""
        frames = max(0, int(frames))
        keep = keep_view and frames == self._frames
        self._frames = frames
        self._fps = fps if fps and fps > 0 else 24000 / 1001
        # La visibilité appartient aux séries (cases de la légende de l'appelant).
        self._series = list(series)
        self._markers = list(markers or ())
        self._scenes = scenes
        if not keep:
            self._view = (0, self._frames)
            self._bands = None
        self._drag = None
        self._hover_x = None
        self.update()

    def set_bands(self, mask: np.ndarray | None, color: str | None = None) -> None:
        """Images signalées (ex. écrêtées) : bandes verticales translucides."""
        self._bands = mask if mask is not None and mask.size == self._frames and mask.any() else None
        self._band_color = color or C.ERROR
        self.update()

    def clear(self) -> None:
        self.set_data(frames=0, fps=None, series=[])

    def set_series_visible(self, key: str, visible: bool) -> None:
        for item in self._series:
            if item.key == key:
                item.visible = visible
        self.update()

    def series(self) -> list[ChartSeries]:
        return list(self._series)

    def view(self) -> tuple[int, int]:
        return self._view

    def reset_zoom(self) -> None:
        self._view = (0, self._frames)
        self.update()

    def set_scale(self, mode: str) -> None:
        """``pq`` (PQ dynamique), ``pq_fixed`` (0,1 – 10 000 nits) ou ``linear`` (linéaire dynamique)."""
        self._scale = mode if mode in dict(SCALES) else "pq"
        self.update()

    def set_dual_axes(self, dual: bool) -> None:
        """Moyennes sur un axe secondaire (droite) avec leur propre plage."""
        self._dual = bool(dual)
        self.update()

    def scale_mode(self) -> str:
        return self._scale

    def dual_axes(self) -> bool:
        return self._dual

    def _axis(self, item: ChartSeries) -> str:
        return "second" if self._dual and item.mode == "mean" else "main"

    def axis_range(self, axis: str = "main") -> tuple[float, float]:
        """Plage (nits) de l'axe au dernier rendu."""
        return self._ranges[axis]

    # ── Géométrie ───────────────────────────────────────────────────────
    def _plot_rect(self) -> QRectF:
        right = scale(56) if self._dual else scale(12)
        return QRectF(scale(52), scale(16), max(1, self.width() - scale(52) - right), max(1, self.height() - scale(42)))

    def _y(self, rect: QRectF, nits: np.ndarray | float, axis: str = "main") -> np.ndarray:
        """Ordonnée (pixels) : valeurs hors plage ramenées aux bords."""
        low, high = self._ranges[axis]
        return rect.bottom() - axes.position(nits, low, high, self._scale) * rect.height()

    def _fit_ranges(self, columns: dict[str, list[np.ndarray]]) -> None:
        """Plages d'après les valeurs visibles (colonnes agrégées) ; repères proches inclus (axe principal)."""
        for axis in ("main", "second"):
            parts = columns.get(axis, [])
            if not parts and axis == "second":
                self._ranges[axis] = self._ranges["main"]
                continue
            values = np.concatenate(parts) if parts else np.zeros(0)
            markers = [m.nits for m in self._markers] if axis == "main" else []
            self._ranges[axis] = axes.fit_range(values, self._scale, markers)

    def _frame_at(self, x: float) -> int | None:
        rect = self._plot_rect()
        if not self._frames or not rect.left() <= x <= rect.right():
            return None
        start, end = self._view
        return int(start + (x - rect.left()) / rect.width() * (end - start))

    # ── Dessin ──────────────────────────────────────────────────────────
    def paintEvent(self, _event) -> None:  # noqa: N802 (API Qt)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(C.BG_CARD))
        rect = self._plot_rect()
        font = painter.font()
        font.setPixelSize(font_px(9))
        painter.setFont(font)
        if not self._frames:
            painter.setPen(QColor(C.TEXT_DIM))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, translate_text("Aucune donnée de luminance"))
            return
        start, end = self._view
        columns = max(1, int(rect.width()))
        aggregated: list[tuple[ChartSeries, np.ndarray]] = []
        for item in self._series:
            if item.visible and item.values.size:
                values = bucket(item.values, start, min(end, item.values.size), columns, item.mode)
                if values.size:
                    aggregated.append((item, values))
        by_axis: dict[str, list[np.ndarray]] = {}
        for item, values in aggregated:
            by_axis.setdefault(self._axis(item), []).append(values)
        self._fit_ranges(by_axis)
        self._draw_grid(painter, rect, second="second" in by_axis)
        if self._bands is not None:
            marked = bucket(self._bands.astype(np.float32), start, end, columns, "max")
            band = QColor(self._band_color)
            band.setAlpha(40)
            step = rect.width() / max(1, marked.size)
            for index in np.nonzero(marked > 0)[0]:
                painter.fillRect(QRectF(rect.left() + index * step, rect.top(), max(1.0, step), rect.height()), band)
        for item, values in aggregated:
            span = min(end, item.values.size) - start
            xs = rect.left() + np.arange(values.size) * (span / values.size) / max(1, end - start) * rect.width()
            ys = self._y(rect, np.nan_to_num(values, nan=0.0), self._axis(item))
            finite = np.isfinite(values)
            color = QColor(item.color)
            # Tronçons continus : une image sans valeur (NaN) coupe la courbe.
            breaks = np.flatnonzero(np.diff(finite.astype(np.int8))) + 1
            for segment in np.split(np.arange(values.size), breaks):
                if not segment.size or not finite[segment[0]]:
                    continue
                path = QPainterPath(QPointF(float(xs[segment[0]]), float(ys[segment[0]])))
                for index in segment[1:]:
                    path.lineTo(float(xs[index]), float(ys[index]))
                if item.fill:
                    area = QPainterPath(path)
                    area.lineTo(float(xs[segment[-1]]), rect.bottom())
                    area.lineTo(float(xs[segment[0]]), rect.bottom())
                    area.closeSubpath()
                    fill = QColor(color)
                    fill.setAlpha(32)
                    painter.fillPath(area, fill)
                painter.setPen(QPen(color, 1.2))
                painter.drawPath(path)
        label_h = scale(13)
        last_label: float | None = None
        high = self._ranges["main"][1]
        for marker in sorted(self._markers, key=lambda m: -m.nits):
            above = marker.nits > high
            y = float(self._y(rect, marker.nits))
            painter.setPen(QPen(QColor(marker.color), 1, Qt.PenStyle.DotLine if above else Qt.PenStyle.DashLine))
            if not above:
                painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
            # Étiquettes empilées vers le bas quand deux repères sont proches ; hors plage : en bord haut.
            top = rect.top() + scale(1) if above else y - label_h - scale(1)
            if last_label is not None and top < last_label + label_h:
                top = last_label + label_h
            last_label = top
            painter.drawText(QRectF(rect.left() + scale(4), top, rect.width() - scale(8), label_h),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                             f"{marker.label} {round(marker.nits)}" + (" ↑" if above else ""))
        self._draw_scenes(painter, rect)
        self._draw_hover(painter, rect)

    def _draw_scenes(self, painter: QPainter, rect: QRectF) -> None:
        """Débuts de plans en bas du graphique, seulement s'ils restent lisibles (≥ 4 px d'écart moyen)."""
        if self._scenes is None or not self._scenes.size:
            return
        start, end = self._view
        visible = self._scenes[(self._scenes >= start) & (self._scenes < end)]
        if not visible.size or visible.size > rect.width() / 4:
            return
        painter.setPen(QPen(QColor(C.TEXT_DIM), 1))
        for frame in visible:
            x = rect.left() + (float(frame) - start) / max(1, end - start) * rect.width()
            painter.drawLine(QPointF(x, rect.bottom() - scale(6)), QPointF(x, rect.bottom()))

    def ticks(self, axis: str, height: float) -> list[float]:
        """Graduations de l'axe dans sa plage, espacées d'au moins 18 px."""
        low, high = self._ranges[axis]
        return axes.ticks(low, high, self._scale, scale(18) / max(1.0, height))

    @staticmethod
    def _tick_text(tick: float) -> str:
        return f"{tick:g}"

    def _draw_grid(self, painter: QPainter, rect: QRectF, *, second: bool = False) -> None:
        grid = QColor(C.BORDER)
        for tick in self.ticks("main", rect.height()):
            y = float(self._y(rect, tick))
            painter.setPen(QPen(grid, 1))
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
            painter.setPen(QColor(C.TEXT_DIM))
            painter.drawText(QRectF(0, y - scale(7), rect.left() - scale(6), scale(14)),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, self._tick_text(tick))
        if second:
            # Axe secondaire (moyennes) : graduations à droite, sans grille (lecture sur l'axe).
            painter.setPen(QColor(C.TEXT_SEC))
            for tick in self.ticks("second", rect.height()):
                y = float(self._y(rect, tick, "second"))
                painter.drawLine(QPointF(rect.right(), y), QPointF(rect.right() + scale(4), y))
                painter.drawText(QRectF(rect.right() + scale(6), y - scale(7), scale(52), scale(14)),
                                 Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self._tick_text(tick))
            painter.setPen(QColor(C.TEXT_DIM))
            painter.drawText(QRectF(rect.right() - scale(160), rect.top() - scale(12), scale(214), scale(11)),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                             translate_text("moyennes (nits) →"))
            painter.drawText(QRectF(rect.left(), rect.top() - scale(12), scale(200), scale(11)),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                             translate_text("← pics (nits)"))
        start, end = self._view
        duration = (end - start) / self._fps
        step = next((s for s in _TIME_STEPS if duration / s <= 8), 7200)
        first = int(np.ceil(start / self._fps / step)) * step
        t = first
        while t * self._fps <= end:
            x = rect.left() + (t * self._fps - start) / max(1, end - start) * rect.width()
            painter.setPen(QPen(grid, 1))
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
            painter.setPen(QColor(C.TEXT_DIM))
            painter.drawText(QRectF(x - scale(40), rect.bottom() + scale(4), scale(80), scale(14)),
                             Qt.AlignmentFlag.AlignHCenter, format_time(t))
            t += step
        painter.setPen(QPen(QColor(C.BORDER_LT), 1))
        painter.drawRect(rect)

    def _draw_hover(self, painter: QPainter, rect: QRectF) -> None:
        if self._hover_x is None:
            return
        painter.setPen(QPen(QColor(C.TEXT_SEC), 1, Qt.PenStyle.DotLine))
        painter.drawLine(QPointF(self._hover_x, rect.top()), QPointF(self._hover_x, rect.bottom()))

    # ── Interaction ─────────────────────────────────────────────────────
    def hover_text(self, frame: int) -> str:
        parts = [f"{format_time(frame / self._fps)} · {translate_text('image')} {frame}"]
        for item in self._series:
            if item.visible and 0 <= frame < item.values.size and np.isfinite(item.values[frame]):
                parts.append(f"{item.label} {round(float(item.values[frame]))}")
        return "   ".join(parts)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._drag is not None and event.buttons() & Qt.MouseButton.LeftButton:
            origin, (start, end) = self._drag
            width = end - start
            shift = int((origin - event.position().x()) / self._plot_rect().width() * width)
            new_start = min(max(0, start + shift), self._frames - width)
            self._dragged = self._dragged or shift != 0
            self._view = (new_start, new_start + width)
        frame = self._frame_at(event.position().x())
        self._hover_x = event.position().x() if frame is not None else None
        self.hover_changed.emit(self.hover_text(frame) if frame is not None else "")
        self.update()

    def leaveEvent(self, _event) -> None:  # noqa: N802
        self._hover_x = None
        self.hover_changed.emit("")
        self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag = (event.position().x(), self._view)
            self._dragged = False

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton or self._drag is None:
            return
        self._drag = None
        frame = self._frame_at(event.position().x())
        if frame is not None and not self._dragged:
            self.frame_clicked.emit(frame)

    def mouseDoubleClickEvent(self, _event: QMouseEvent) -> None:  # noqa: N802
        self.reset_zoom()

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        frame = self._frame_at(event.position().x())
        if frame is None or not self._frames:
            return
        start, end = self._view
        factor = 0.8 if event.angleDelta().y() > 0 else 1.25
        width = int(min(self._frames, max(48, (end - start) * factor)))
        ratio = (frame - start) / max(1, end - start)
        new_start = int(min(max(0, frame - ratio * width), self._frames - width))
        self._view = (new_start, new_start + width)
        self.update()
        event.accept()


def chart_controls(chart: LuminanceChart) -> QWidget:
    """Choix de l'échelle et des deux axes d'un graphique (à placer près de sa légende)."""
    holder = QWidget()
    holder.setStyleSheet("background:transparent;")
    row = QHBoxLayout(holder)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(scale(8))
    combo = QComboBox()
    combo.setStyleSheet(_combo_style())
    for key, label in SCALES:
        combo.addItem(translate_text(label), key)
    combo.setCurrentIndex(max(0, combo.findData(chart.scale_mode())))
    combo.currentIndexChanged.connect(lambda _i: chart.set_scale(str(combo.currentData())))
    dual = QCheckBox(translate_text("Moyennes sur un axe à droite"))
    dual.setStyleSheet(_checkbox_style())
    dual.setChecked(chart.dual_axes())
    dual.toggled.connect(chart.set_dual_axes)
    row.addWidget(combo)
    row.addWidget(dual)
    row.addStretch()
    holder.scale_combo = combo  # type: ignore[attr-defined]
    holder.dual_box = dual  # type: ignore[attr-defined]
    return holder
