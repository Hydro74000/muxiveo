"""Widget de visualisation et de superposition d'enveloppes audio."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from ui.design_system import colors as _C, font_px as _font_px, scale as _scale


class WaveformView(QWidget):
    """Enveloppes audio superposables avec décalage interactif en millisecondes."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        reference_label: str = "Référence (VO)",
        target_label: str = "Cible (VF)",
    ) -> None:
        super().__init__(parent)
        self.setMinimumHeight(_scale(140))
        self.series: tuple[list[float], list[float]] = ([], [])
        self.shift_ms: float = 0.0
        self.duration_ms: float = 20000.0
        self.reference_label = reference_label
        self.target_label = target_label
        self.loading_text: str = ""
        self.setStyleSheet(f"""
            QWidget {{
                background: {_C.BG_DEEP};
                border: 1px solid {_C.BORDER};
                border-radius: {_scale(6)}px;
            }}
        """)

    def set_loading(self, text: str) -> None:
        self.loading_text = text
        self.update()

    def set_series(self, series: tuple[list[float], list[float]] | list[list[float]]) -> None:
        self.series = tuple(series) if series else ([], [])
        self.loading_text = ""
        self.update()

    def set_shift(self, shift_ms: float) -> None:
        self.shift_ms = float(shift_ms)
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        painter.fillRect(self.rect(), QColor(_C.BG_DEEP))

        # Message de chargement ou d'attente
        if self.loading_text and (not self.series or not any(self.series)):
            painter.setPen(QColor(_C.TEXT_SEC))
            font = QFont()
            font.setPixelSize(_font_px(11))
            painter.setFont(font)
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.loading_text)
            return

        # Lignes guides et repères
        pen_grid = QPen(QColor(_C.BORDER))
        pen_grid.setStyle(Qt.PenStyle.DashLine)
        pen_grid.setWidthF(_scale(1.0))
        painter.setPen(pen_grid)
        y_ref_center = int(h * 0.28)
        y_tgt_center = int(h * 0.72)
        painter.drawLine(0, y_ref_center, w, y_ref_center)
        painter.drawLine(0, y_tgt_center, w, y_tgt_center)

        # Repères temporels (0s, 5s, 10s, 15s, 20s)
        time_steps = [0, 5, 10, 15, 20]
        font_tick = QFont()
        font_tick.setPixelSize(_font_px(9))
        font_tick.setBold(True)
        painter.setFont(font_tick)
        painter.setPen(QColor(_C.TEXT_DIM))
        for sec in time_steps:
            x = int(sec * 1000 / self.duration_ms * w)
            painter.drawLine(x, h - _scale(12), x, h)
            if sec > 0 and x < w - 25:
                painter.drawText(x + _scale(3), h - _scale(3), f"{sec}s")

        # Repère de calage t=0
        pen_anchor = QPen(QColor(_C.BORDER_LT))
        pen_anchor.setStyle(Qt.PenStyle.DotLine)
        painter.setPen(pen_anchor)
        painter.drawLine(0, 0, 0, h)

        # Dessin des formes d'onde
        colors = [QColor("#20c997"), QColor(_C.ACCENT)]  # Vert émeraude pour ref, Indigo/Accent pour cible
        centers = [y_ref_center, y_tgt_center]

        for row, values in enumerate(self.series):
            if not values:
                continue
            middle = centers[row]
            path = QPainterPath()
            scale_val = max(max(values), 1e-9)
            delta = (self.shift_ms / self.duration_ms * w) if row else 0.0

            for i, value in enumerate(values):
                x = i * w / max(1, len(values) - 1) + delta
                y = middle - (value / scale_val * h * 0.22)
                if i == 0:
                    path.moveTo(x, y)
                else:
                    path.lineTo(x, y)

            pen = QPen(colors[row % len(colors)])
            pen.setWidthF(_scale(1.6))
            painter.setPen(pen)
            painter.drawPath(path)

        # Légendes d'identification en haut
        font_lbl = QFont()
        font_lbl.setPixelSize(_font_px(10))
        font_lbl.setBold(True)
        painter.setFont(font_lbl)

        # Légende référence
        painter.setPen(colors[0])
        painter.drawText(_scale(8), _scale(14), f"● {self.reference_label}")

        # Légende cible
        painter.setPen(colors[1])
        shift_text = f" ({self.shift_ms:+.1f} ms)" if self.shift_ms else " (0.0 ms)"
        painter.drawText(_scale(8), int(h * 0.52), f"● {self.target_label}{shift_text}")
