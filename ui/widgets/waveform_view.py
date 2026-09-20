"""Widget de visualisation et de superposition d'enveloppes audio haute précision avec zoom et défilement."""
from __future__ import annotations

import math
import numpy as np
from PySide6.QtCore import QLineF, QPointF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QMouseEvent, QPainter, QPainterPath, QPen, QWheelEvent
from PySide6.QtWidgets import QWidget

from ui.design_system import colors as _C, font_px as _font_px, scale as _scale


def _format_time_tick(ms: float, is_subsecond: bool) -> str:
    total_sec = max(0.0, ms / 1000.0)
    hours = int(total_sec // 3600)
    minutes = int((total_sec % 3600) // 60)
    seconds = int(total_sec % 60)
    millis = int(round(ms % 1000))
    if is_subsecond:
        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"
        return f"{minutes:02d}:{seconds:02d}.{millis:03d}"
    else:
        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        if minutes > 0:
            return f"{minutes:02d}:{seconds:02d}"
        return f"{seconds}s"


def _choose_time_step(dur_ms: float) -> tuple[float, bool]:
    if dur_ms >= 10000:
        return 2000.0, False
    elif dur_ms >= 4000:
        return 1000.0, False
    elif dur_ms >= 1500:
        return 500.0, True
    elif dur_ms >= 600:
        return 200.0, True
    elif dur_ms >= 200:
        return 50.0, True
    elif dur_ms >= 60:
        return 20.0, True
    elif dur_ms >= 20:
        return 5.0, True
    else:
        return 1.0, True


class WaveformView(QWidget):
    """Enveloppes audio superposables avec zoom milliseconde et décalage interactif."""

    zoom_changed = Signal(float, float)
    pan_changed = Signal(float, float, float)
    mode_changed = Signal(str)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        reference_label: str = "Référence (VO)",
        target_label: str = "Cible (VF)",
    ) -> None:
        super().__init__(parent)
        self.setMinimumHeight(_scale(180))
        self.reference_label = reference_label
        self.target_label = target_label
        self.display_mode: str = "split"  # "split" (scindé) ou "overlay" (superposé)

        # Données audio
        self._ref_samples: np.ndarray = np.array([], dtype=np.float32)
        self._tgt_samples: np.ndarray = np.array([], dtype=np.float32)
        self.sample_rate: int = 16000
        self.start_time_ms: float = 0.0
        self.window_duration_ms: float = 20000.0
        self.cut_time_ms: float | None = None
        self.shift_ms: float = 0.0

        # Données historiques de repli
        self.series: tuple[list[float], list[float]] = ([], [])

        # État de zoom et de navigation
        self.zoom_factor: float = 1.0
        self.pan_offset_ms: float = 0.0

        # État de glissement
        self._is_dragging: bool = False
        self._drag_start_x: float = 0.0
        self._drag_start_pan: float = 0.0

        self.loading_text: str = ""
        self.setMouseTracking(True)
        self.setStyleSheet(f"""
            QWidget {{
                background: {_C.BG_DEEP};
                border: 1px solid {_C.BORDER};
                border-radius: {_scale(6)}px;
            }}
        """)

    @property
    def visible_duration_ms(self) -> float:
        return self.window_duration_ms / max(1.0, self.zoom_factor)

    def set_loading(self, text: str) -> None:
        self.loading_text = text
        self.update()

    def set_series(self, series: tuple[list[float], list[float]] | list[list[float]]) -> None:
        """Compatibilité avec HybridStudio ou affichages simplifiés."""
        self.series = tuple(series) if series else ([], [])
        self._ref_samples = np.array([], dtype=np.float32)
        self._tgt_samples = np.array([], dtype=np.float32)
        self.loading_text = ""
        self.update()

    def set_audio_data(
        self,
        ref_samples: np.ndarray | list[float] | None,
        tgt_samples: np.ndarray | list[float] | None,
        sample_rate: int = 16000,
        start_time_ms: float = 0.0,
        cut_time_ms: float | None = None,
    ) -> None:
        """Définit les échantillons 16kHz haute précision pour un zoom au millimètre."""
        self._ref_samples = (
            np.asarray(ref_samples, dtype=np.float32)
            if ref_samples is not None and len(ref_samples) > 0
            else np.array([], dtype=np.float32)
        )
        self._tgt_samples = (
            np.asarray(tgt_samples, dtype=np.float32)
            if tgt_samples is not None and len(tgt_samples) > 0
            else np.array([], dtype=np.float32)
        )
        self.sample_rate = max(1, sample_rate)
        self.start_time_ms = float(start_time_ms)
        self.cut_time_ms = float(cut_time_ms) if cut_time_ms is not None else None

        max_len = max(len(self._ref_samples), len(self._tgt_samples))
        if max_len > 0:
            self.window_duration_ms = float(max_len / self.sample_rate * 1000.0)
        else:
            self.window_duration_ms = 20000.0

        # Normaliser le pan dans les nouvelles limites
        self.set_pan_offset_ms(self.pan_offset_ms)
        self.loading_text = ""
        self.update()

    def set_shift(self, shift_ms: float) -> None:
        self.shift_ms = float(shift_ms)
        self.update()

    def set_display_mode(self, mode: str) -> None:
        mode = "overlay" if mode == "overlay" else "split"
        if mode != self.display_mode:
            self.display_mode = mode
            self.mode_changed.emit(self.display_mode)
            self.update()

    def toggle_display_mode(self) -> str:
        new_mode = "overlay" if self.display_mode == "split" else "split"
        self.set_display_mode(new_mode)
        return self.display_mode

    def set_zoom(self, zoom_factor: float, center_ratio: float = 0.5) -> None:
        new_zoom = max(1.0, min(400.0, float(zoom_factor)))
        if abs(new_zoom - self.zoom_factor) < 1e-4:
            return

        old_visible_ms = self.visible_duration_ms
        new_visible_ms = self.window_duration_ms / new_zoom

        # Ancrer le point sous center_ratio
        t_fixed = self.pan_offset_ms + center_ratio * old_visible_ms
        new_pan = t_fixed - center_ratio * new_visible_ms
        max_pan = max(0.0, self.window_duration_ms - new_visible_ms)
        self.pan_offset_ms = max(0.0, min(max_pan, new_pan))
        self.zoom_factor = new_zoom

        self.zoom_changed.emit(self.zoom_factor, self.pan_offset_ms)
        self.pan_changed.emit(self.pan_offset_ms, self.visible_duration_ms, self.window_duration_ms)
        self.update()

    def zoom_in(self, center_ratio: float = 0.5) -> None:
        self.set_zoom(self.zoom_factor * 1.5, center_ratio)

    def zoom_out(self, center_ratio: float = 0.5) -> None:
        self.set_zoom(self.zoom_factor / 1.5, center_ratio)

    def reset_zoom(self) -> None:
        self.set_zoom(1.0, 0.5)
        self.set_pan_offset_ms(0.0)

    def set_pan_offset_ms(self, offset_ms: float) -> None:
        max_pan = max(0.0, self.window_duration_ms - self.visible_duration_ms)
        new_pan = max(0.0, min(max_pan, float(offset_ms)))
        if abs(new_pan - self.pan_offset_ms) > 1e-3:
            self.pan_offset_ms = new_pan
            self.pan_changed.emit(self.pan_offset_ms, self.visible_duration_ms, self.window_duration_ms)
            self.update()

    def wheelEvent(self, event: QWheelEvent) -> None:
        mods = event.modifiers()
        is_zoom = bool(mods & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier))
        angle_y = event.angleDelta().y()
        angle_x = event.angleDelta().x()

        if is_zoom:
            if angle_y != 0:
                pos_x = event.position().x()
                ratio = max(0.0, min(1.0, pos_x / max(1, self.width())))
                factor = 1.25 if angle_y > 0 else (1.0 / 1.25)
                self.set_zoom(self.zoom_factor * factor, center_ratio=ratio)
                event.accept()
                return
        else:
            # Défilement horizontal
            delta = angle_x if angle_x != 0 else -angle_y
            if delta != 0 and self.zoom_factor > 1.0:
                dt_ms = (delta / 120.0) * (self.visible_duration_ms * 0.12)
                self.set_pan_offset_ms(self.pan_offset_ms + dt_ms)
                event.accept()
                return

        super().wheelEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start_x = event.position().x()
            self._drag_start_pan = self.pan_offset_ms
            self._is_dragging = True
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._is_dragging:
            dx = event.position().x() - self._drag_start_x
            dt_ms = -dx * (self.visible_duration_ms / max(1, self.width()))
            self.set_pan_offset_ms(self._drag_start_pan + dt_ms)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._is_dragging:
            self._is_dragging = False
            self.setCursor(Qt.CursorShape.OpenHandCursor if self.zoom_factor > 1.0 else Qt.CursorShape.ArrowCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _extract_column_peaks(self, raw_samples: np.ndarray, t_start_ms: float, t_end_ms: float, width_px: int) -> np.ndarray:
        """Calcule les crêtes d'amplitude échantillonnées pour chaque colonne de pixels."""
        if len(raw_samples) == 0 or width_px <= 0:
            return np.zeros(max(1, width_px), dtype=np.float32)

        sr = self.sample_rate
        # Indices dans le tampon raw_samples
        idx_start = int((t_start_ms - self.start_time_ms) / 1000.0 * sr)
        idx_end = int((t_end_ms - self.start_time_ms) / 1000.0 * sr)

        total_samples = len(raw_samples)
        # Gestion des débordements avec padding à zéro
        pad_left = max(0, -idx_start)
        pad_right = max(0, idx_end - total_samples)
        valid_start = max(0, min(total_samples, idx_start))
        valid_end = max(0, min(total_samples, idx_end))

        valid_slice = raw_samples[valid_start:valid_end]
        if pad_left > 0 or pad_right > 0:
            parts = []
            if pad_left > 0:
                parts.append(np.zeros(pad_left, dtype=np.float32))
            if len(valid_slice) > 0:
                parts.append(valid_slice)
            if pad_right > 0:
                parts.append(np.zeros(pad_right, dtype=np.float32))
            chunk = np.concatenate(parts) if parts else np.zeros(max(1, idx_end - idx_start), dtype=np.float32)
        else:
            chunk = valid_slice

        n = len(chunk)
        if n == 0:
            return np.zeros(width_px, dtype=np.float32)

        if n >= width_px:
            chunk_size = n // width_px
            usable = chunk_size * width_px
            reshaped = np.abs(chunk[:usable].reshape(width_px, chunk_size))
            return np.max(reshaped, axis=1)
        else:
            # Cas d'un zoom extrême : interpolation pour couvrir tous les pixels
            indices = np.linspace(0, n - 1, width_px).astype(int)
            return np.abs(chunk[indices])

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        painter.fillRect(self.rect(), QColor(_C.BG_DEEP))

        # Message d'attente / chargement
        has_audio = (len(self._ref_samples) > 0 or len(self._tgt_samples) > 0)
        has_series = (self.series and any(self.series))
        if self.loading_text and not has_audio and not has_series:
            painter.setPen(QColor(_C.TEXT_SEC))
            font = QFont()
            font.setPixelSize(_font_px(11))
            painter.setFont(font)
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.loading_text)
            return

        is_overlay = (self.display_mode == "overlay")

        if is_overlay:
            y_ref_center = int(h * 0.50)
            y_tgt_center = int(h * 0.50)
            max_amp = h * 0.36

            # Guide horizontal central
            pen_guide = QPen(QColor(_C.BORDER))
            pen_guide.setStyle(Qt.PenStyle.DashLine)
            pen_guide.setWidthF(_scale(0.8))
            painter.setPen(pen_guide)
            painter.drawLine(0, y_ref_center, w, y_ref_center)
        else:
            y_ref_center = int(h * 0.28)
            y_tgt_center = int(h * 0.72)
            max_amp = h * 0.20

            # Ligne de séparation médiane
            pen_sep = QPen(QColor(_C.BORDER))
            pen_sep.setStyle(Qt.PenStyle.SolidLine)
            pen_sep.setWidthF(_scale(1.0))
            painter.setPen(pen_sep)
            painter.drawLine(0, int(h * 0.50), w, int(h * 0.50))

            # Guides horizontaux pointillés
            pen_guide = QPen(QColor(_C.BORDER))
            pen_guide.setStyle(Qt.PenStyle.DashLine)
            pen_guide.setWidthF(_scale(0.8))
            painter.setPen(pen_guide)
            painter.drawLine(0, y_ref_center, w, y_ref_center)
            painter.drawLine(0, y_tgt_center, w, y_tgt_center)

        # Calcul de la plage temporelle visible
        t_start_ms = self.start_time_ms + self.pan_offset_ms
        t_end_ms = t_start_ms + self.visible_duration_ms

        # Grille verticale et axe temporel
        step_ms, is_subsec = _choose_time_step(self.visible_duration_ms)
        pen_grid = QPen(QColor("#242b38"))
        pen_grid.setStyle(Qt.PenStyle.DotLine)

        font_tick = QFont()
        font_tick.setPixelSize(_font_px(9))
        font_tick.setBold(True)
        painter.setFont(font_tick)

        first_tick = math.floor(t_start_ms / step_ms) * step_ms
        cur_tick = first_tick
        while cur_tick <= t_end_ms + step_ms:
            x = int((cur_tick - t_start_ms) / self.visible_duration_ms * w)
            if 0 <= x <= w:
                painter.setPen(pen_grid)
                painter.drawLine(x, 0, x, h - _scale(16))

                # Graduations et libellé
                painter.setPen(QColor(_C.BORDER_LT))
                painter.drawLine(x, h - _scale(14), x, h)
                painter.setPen(QColor(_C.TEXT_DIM))
                label = _format_time_tick(cur_tick, is_subsec)
                painter.drawText(x + _scale(3), h - _scale(3), label)
            cur_tick += step_ms

        # Marqueur vertical de coupure (si présent dans la plage visible)
        if self.cut_time_ms is not None and t_start_ms <= self.cut_time_ms <= t_end_ms:
            x_cut = int((self.cut_time_ms - t_start_ms) / self.visible_duration_ms * w)
            pen_cut = QPen(QColor("#e5a50a"))
            pen_cut.setStyle(Qt.PenStyle.DashDotLine)
            pen_cut.setWidthF(_scale(1.8))
            painter.setPen(pen_cut)
            painter.drawLine(x_cut, 0, x_cut, h)

            # Badge de coupure
            cut_lbl = f"✂ Coupure ({_format_time_tick(self.cut_time_ms, True)})"
            painter.setPen(QColor("#e5a50a"))
            font_cut = QFont()
            font_cut.setPixelSize(_font_px(9))
            font_cut.setBold(True)
            painter.setFont(font_cut)
            painter.drawText(x_cut + _scale(4), _scale(16), cut_lbl)

        # Repère central vertical (réticule d'alignement)
        pen_reticule = QPen(QColor("#3d4b60"))
        pen_reticule.setStyle(Qt.PenStyle.DashLine)
        pen_reticule.setWidthF(_scale(1.0))
        painter.setPen(pen_reticule)
        painter.drawLine(int(w * 0.5), 0, int(w * 0.5), h)

        # Rendu des formes d'onde
        color_ref = QColor(32, 201, 151, 190) if is_overlay else QColor("#20c997")
        color_tgt = QColor(120, 140, 255, 190) if is_overlay else QColor(_C.ACCENT)
        stroke_w = _scale(1.2) if is_overlay else _scale(1.0)

        if has_audio:
            # 1. Forme d'onde de Référence (VO)
            peaks_ref = self._extract_column_peaks(self._ref_samples, t_start_ms, t_end_ms, w)
            scale_ref = max(float(np.max(peaks_ref)), 1e-4) if len(peaks_ref) > 0 else 1.0

            pen_ref = QPen(color_ref)
            pen_ref.setWidthF(stroke_w)
            painter.setPen(pen_ref)
            lines_ref = []
            for x in range(w):
                amp = (peaks_ref[x] / scale_ref) * max_amp
                if amp > 0.5:
                    lines_ref.append(QLineF(x, y_ref_center - amp, x, y_ref_center + amp))
                else:
                    lines_ref.append(QLineF(x, y_ref_center, x, y_ref_center))
            painter.drawLines(lines_ref)

            # 2. Forme d'onde Cible (VF) avec décalage interactif
            # Le calage de shift_ms décale temporellement la piste cible
            tgt_t_start = t_start_ms - self.shift_ms
            tgt_t_end = t_end_ms - self.shift_ms
            peaks_tgt = self._extract_column_peaks(self._tgt_samples, tgt_t_start, tgt_t_end, w)
            scale_tgt = max(float(np.max(peaks_tgt)), 1e-4) if len(peaks_tgt) > 0 else 1.0

            pen_tgt = QPen(color_tgt)
            pen_tgt.setWidthF(stroke_w)
            painter.setPen(pen_tgt)
            lines_tgt = []
            for x in range(w):
                amp = (peaks_tgt[x] / scale_tgt) * max_amp
                if amp > 0.5:
                    lines_tgt.append(QLineF(x, y_tgt_center - amp, x, y_tgt_center + amp))
                else:
                    lines_tgt.append(QLineF(x, y_tgt_center, x, y_tgt_center))
            painter.drawLines(lines_tgt)

        elif has_series:
            # Rendu de repli à base de listes de points (HybridStudio)
            centers = [y_ref_center, y_tgt_center]
            colors = [color_ref, color_tgt]
            for row, values in enumerate(self.series):
                if not values:
                    continue
                middle = centers[row]
                path = QPainterPath()
                scale_val = max(max(values), 1e-9)
                delta = (self.shift_ms / self.window_duration_ms * w) if row else 0.0

                for i, value in enumerate(values):
                    x = i * w / max(1, len(values) - 1) + delta
                    y = middle - (value / scale_val * max_amp)
                    if i == 0:
                        path.moveTo(x, y)
                    else:
                        path.lineTo(x, y)

                pen = QPen(colors[row % len(colors)])
                pen.setWidthF(_scale(1.5))
                painter.setPen(pen)
                painter.drawPath(path)

        # Légendes d'en-tête
        font_lbl = QFont()
        font_lbl.setPixelSize(_font_px(10))
        font_lbl.setBold(True)
        painter.setFont(font_lbl)

        # Légende Référence
        painter.setPen(QColor("#20c997"))
        painter.drawText(_scale(8), _scale(15), f"● {self.reference_label}")

        # Légende Cible avec décalage
        painter.setPen(QColor(_C.ACCENT))
        shift_text = f" ({self.shift_ms:+.1f} ms)" if self.shift_ms else " (0.0 ms)"
        if is_overlay:
            painter.drawText(_scale(175), _scale(15), f"● {self.target_label}{shift_text}")
        else:
            painter.drawText(_scale(8), int(h * 0.50) + _scale(15), f"● {self.target_label}{shift_text}")

        # Badge de niveau de zoom en haut à droite
        painter.setPen(QColor(_C.TEXT_SEC))
        font_zoom = QFont()
        font_zoom.setPixelSize(_font_px(9))
        painter.setFont(font_zoom)
        zoom_text = f"Zoom : {self.zoom_factor:.1f}x (Fenêtre : {int(self.visible_duration_ms)} ms)"
        painter.drawText(w - _scale(210), _scale(15), zoom_text)
