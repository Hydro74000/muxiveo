"""Onglet Comparer du HDR Studio : piste courante (A) face à une autre piste ou un autre fichier (B)."""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from core.file_types import build_qt_filter
from core.hdr_inspection.compare import MAX_OFFSET, SOURCE_LABELS, Comparison, Side, compare
from core.hdr_inspection.inspector import InspectionReport
from core.hdr_inspection.summary import format_time
from core.i18n import translate_text
from ui.design_system import colors as C, font_px, scale
from ui.panels.hdr_studio.chart import ChartSeries, LuminanceChart
from ui.styles import _card, _checkbox_style, _combo_style, _input_style, _secondary_button, _section_label

_ALIGNMENT = {"dovi": "plans du RPU", "hdr10plus": "plans HDR10+", "measure": "plans mesurés",
              "curve": "courbes de luminance", "manual": "décalage saisi", "none": "aucun repère commun"}


def _text(text: str = "", *, color: str | None = None, size: int = 11, bold: bool = False) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    label.setStyleSheet(f"color:{color or C.TEXT_PRI};font-size:{font_px(size)}px;"
                        f"font-weight:{700 if bold else 400};background:transparent;")
    return label


class DropLineEdit(QLineEdit):
    """Champ de chemin acceptant le dépôt d'un fichier."""

    dropped = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        for url in event.mimeData().urls():
            if url.isLocalFile():
                self.dropped.emit(Path(url.toLocalFile()))
                break


def shifted(values: np.ndarray, offset: int, frames: int) -> np.ndarray:
    """Courbe de B placée sur la chronologie de A : ``out[i] = values[i − offset]`` (NaN hors recouvrement)."""
    out = np.full(frames, np.nan, dtype=np.float32)
    start, end = max(0, offset), min(frames, values.size + offset)
    if end > start:
        out[start:end] = values[start - offset:end - offset]
    return out


class CompareTab(QWidget):
    """Choix de B, alignement (automatique ou saisi), écarts de métadonnées et courbes superposées."""

    source_requested = Signal(object)
    track_requested = Signal(int)
    analyze_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setStyleSheet("background:transparent;")
        self._a: Side | None = None
        self._b: Side | None = None
        self._comparison: Comparison | None = None
        self._busy = False
        self._updating = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, scale(10), 0, 0)
        layout.setSpacing(scale(10))
        layout.addWidget(_text(translate_text(
            "Compare la piste inspectée (A) à une autre (B) : autre édition, encodage, source d'un remux. "
            "Les deux pistes sont alignées sur leurs plans (RPU, HDR10+, mesure) ou leurs courbes ; "
            "le décalage reste modifiable."), color=C.TEXT_DIM, size=10))
        layout.addWidget(self._build_source())
        layout.addWidget(self._build_alignment())
        self._fields_card, self._fields = self._section(translate_text("MÉTADONNÉES"))
        layout.addWidget(self._fields_card)
        curves_card, curves = self._section(translate_text("COURBES"))
        self._legend = QHBoxLayout()
        self._legend.setSpacing(scale(12))
        curves.addLayout(self._legend)
        self.chart = LuminanceChart()
        self.chart.setMinimumHeight(scale(240))
        curves.addWidget(self.chart)
        self._deltas = QVBoxLayout()
        self._deltas.setSpacing(scale(4))
        curves.addLayout(self._deltas)
        layout.addWidget(curves_card)
        layout.addStretch()
        self._render()

    def _section(self, title: str) -> tuple[QWidget, QVBoxLayout]:
        card = _card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(scale(14), scale(10), scale(14), scale(12))
        layout.setSpacing(scale(6))
        layout.addWidget(_section_label(title))
        body = QVBoxLayout()
        body.setSpacing(scale(6))
        layout.addLayout(body)
        return card, body

    def _build_source(self) -> QWidget:
        card, layout = self._section(translate_text("PISTE B"))
        row = QHBoxLayout()
        row.setSpacing(scale(6))
        self.path = DropLineEdit()
        self.path.setPlaceholderText(translate_text("Déposer la vidéo à comparer ou choisir un fichier…"))
        self.path.setStyleSheet(_input_style())
        self.path.returnPressed.connect(lambda: self.source_requested.emit(Path(self.path.text().strip())))
        self.path.dropped.connect(self.source_requested.emit)
        browse = _secondary_button("…", fixed_width=32)
        browse.setToolTip(translate_text("Choisir un fichier"))
        browse.clicked.connect(self._browse)
        self.track = QComboBox()
        self.track.setStyleSheet(_combo_style())
        self.track.setMinimumWidth(scale(220))
        self.track.currentIndexChanged.connect(self._track_changed)
        row.addWidget(self.path, 1)
        row.addWidget(browse)
        row.addWidget(self.track)
        layout.addLayout(row)
        actions = QHBoxLayout()
        actions.setSpacing(scale(8))
        self.analyze_btn = _secondary_button(translate_text("Lire les métadonnées dynamiques de B"))
        self.analyze_btn.clicked.connect(self.analyze_requested.emit)
        actions.addWidget(self.analyze_btn)
        self._status = _text("", color=C.TEXT_SEC)
        actions.addWidget(self._status, 1)
        layout.addLayout(actions)
        return card

    def _build_alignment(self) -> QWidget:
        card, layout = self._section(translate_text("ALIGNEMENT"))
        row = QHBoxLayout()
        row.setSpacing(scale(8))
        row.addWidget(_text(translate_text("Décalage de B"), color=C.TEXT_SEC))
        self.offset = QSpinBox()
        self.offset.setRange(-MAX_OFFSET * 4, MAX_OFFSET * 4)
        self.offset.setSuffix(translate_text(" images"))
        self.offset.setStyleSheet(
            f"QSpinBox{{background:{C.BG_CARD};color:{C.TEXT_PRI};border:1px solid {C.BORDER};border-radius:5px;"
            f"padding:{scale(3)}px {scale(6)}px;font-size:{font_px(11)}px;}}")
        self.offset.setFixedWidth(scale(140))
        self.offset.valueChanged.connect(self._offset_edited)
        self.auto_btn = _secondary_button(translate_text("Automatique"))
        self.auto_btn.clicked.connect(lambda: self._recompute(None))
        row.addWidget(self.offset)
        row.addWidget(self.auto_btn)
        self._alignment = _text("", color=C.TEXT_SEC)
        row.addWidget(self._alignment, 1)
        layout.addLayout(row)
        return card

    def _browse(self) -> None:
        start = self.path.text().strip() or (self._a.report.snapshot.path if self._a else str(Path.home()))
        path, _ = QFileDialog.getOpenFileName(self, translate_text("Vidéo à comparer"), start,
                                              build_qt_filter(video_only=True))
        if path:
            self.source_requested.emit(Path(path))

    def _track_changed(self, _index: int) -> None:
        data = self.track.currentData()
        if not self._updating and data is not None:
            self.track_requested.emit(int(data))

    # ── Données ─────────────────────────────────────────────────────────
    def set_tracks(self, path: Path, tracks: list[tuple[int, str]], current: int) -> None:
        """Pistes vidéo de B (index ffprobe, libellé) ; ``current`` sélectionnée sans nouvelle demande."""
        self._updating = True
        try:
            self.path.setText(str(path))
            self.track.clear()
            for index, label in tracks:
                self.track.addItem(label, index)
                if index == current:
                    self.track.setCurrentIndex(self.track.count() - 1)
        finally:
            self._updating = False

    def set_side_a(self, side: Side | None) -> None:
        same = (side is not None and self._a is not None and side.report.snapshot.path == self._a.report.snapshot.path
                and side.report.snapshot.stream_index == self._a.report.snapshot.stream_index)
        self._a = side
        self._recompute(self._comparison.offset if same and self._comparison is not None else None)

    def set_side_b(self, side: Side | None) -> None:
        self._b = side
        self._recompute(None)

    def set_status(self, text: str, *, error: bool = False) -> None:
        self._status.setText(text)
        self._status.setStyleSheet(f"color:{C.ERROR if error else C.TEXT_SEC};font-size:{font_px(11)}px;"
                                   "background:transparent;")

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._sync()

    def side_b(self) -> Side | None:
        return self._b

    def comparison(self) -> Comparison | None:
        return self._comparison

    def other_report(self) -> InspectionReport | None:
        return self._b.report if self._b is not None and self._comparison is not None else None

    def _offset_edited(self, value: int) -> None:
        if not self._updating:
            self._recompute(value)

    def _recompute(self, offset: int | None) -> None:
        a, b = self._a, self._b
        self._comparison = compare(a, b, offset) if a is not None and b is not None else None
        if self._comparison is not None:
            self._updating = True
            try:
                self.offset.setValue(self._comparison.offset)
            finally:
                self._updating = False
        self._render()

    def _sync(self) -> None:
        b = self._b
        snapshot = b.report.snapshot if b is not None else None
        dynamic = snapshot is not None and (snapshot.dovi is not None or snapshot.hdr10plus)
        self.analyze_btn.setEnabled(bool(dynamic) and not self._busy and b is not None and not b.report.dynamic)
        self.offset.setEnabled(self._comparison is not None)
        self.auto_btn.setEnabled(self._comparison is not None)

    # ── Rendu ───────────────────────────────────────────────────────────
    @staticmethod
    def _clear(layout: QVBoxLayout | QHBoxLayout | QGridLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def _render(self) -> None:
        for layout in (self._fields, self._deltas, self._legend):
            self._clear(layout)
        self._sync()
        comparison = self._comparison
        if comparison is None:
            self._alignment.setText("")
            self.chart.clear()
            self._fields.addWidget(_text(translate_text(
                "Choisir la piste B : une vidéo déposée ici, ou une autre piste du même fichier."), color=C.TEXT_DIM))
            return
        fps = self._a.fps if self._a is not None else 24000 / 1001
        basis = translate_text(_ALIGNMENT.get(comparison.alignment, comparison.alignment))
        score = (translate_text("{share} % des plans appariés", share=round(comparison.alignment_score * 100))
                 if comparison.alignment in ("dovi", "hdr10plus", "measure")
                 else translate_text("corrélation {value}", value=f"{comparison.alignment_score:.2f}")
                 if comparison.alignment == "curve" else "")
        self._alignment.setText(translate_text("{seconds} s, d'après : {basis}", seconds=f"{comparison.offset / fps:+.3f}",
                                               basis=basis) + (f" ({score})" if score else ""))
        self._render_fields(comparison)
        self._render_curves(comparison, fps)

    def _render_fields(self, comparison: Comparison) -> None:
        holder = QWidget()
        holder.setStyleSheet("background:transparent;")
        grid = QGridLayout(holder)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(scale(16))
        grid.setVerticalSpacing(scale(3))
        for column, title in enumerate((translate_text("Champ"), "A", "B")):
            grid.addWidget(_text(title, color=C.TEXT_SEC, size=10, bold=True), 0, column)
        for row, field in enumerate(comparison.fields, start=1):
            color = None if field.same else C.WARN
            grid.addWidget(_text(field.label, color=C.TEXT_SEC), row, 0)
            grid.addWidget(_text(field.a, color=color), row, 1)
            grid.addWidget(_text(field.b, color=color), row, 2)
        grid.setColumnMinimumWidth(0, scale(180))
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(2, 1)
        self._fields.addWidget(holder)
        differences = sum(1 for field in comparison.fields if not field.same)
        self._fields.addWidget(_text(translate_text("{count} différence(s).", count=differences) if differences
                                     else translate_text("Métadonnées identiques."),
                                     color=C.WARN if differences else C.OK, size=10))

    def _render_curves(self, comparison: Comparison, fps: float) -> None:
        series: list[ChartSeries] = []
        frames = comparison.peak_a.values.size if comparison.peak_a is not None else 0
        pairs = ((comparison.peak_a, comparison.peak_b, "max", translate_text("Pic"), C.TRACK_VIDEO, C.WARN),
                 (comparison.average_a, comparison.average_b, "mean", translate_text("Moyenne"), C.OK, C.TRACK_TAGS))
        for first, second, mode, label, color_a, color_b in pairs:
            if first is None or second is None:
                continue
            frames = max(frames, first.values.size)
            series.append(ChartSeries(f"a_{mode}", f"{label} A ({translate_text(SOURCE_LABELS[first.source])})", color_a,
                                      first.values.astype(np.float32), mode, fill=mode == "max"))
            series.append(ChartSeries(f"b_{mode}", f"{label} B ({translate_text(SOURCE_LABELS[second.source])})", color_b,
                                      shifted(second.values, comparison.offset, first.values.size), mode))
        self.chart.set_data(frames=frames, fps=fps, series=series, keep_view=True)
        for entry in series:
            box = QCheckBox(entry.label)
            box.setChecked(entry.visible)
            box.setStyleSheet(_checkbox_style() + f"QCheckBox{{color:{entry.color};}}")
            box.toggled.connect(lambda checked, key=entry.key: self.chart.set_series_visible(key, checked))
            self._legend.addWidget(box)
        self._legend.addStretch()
        names = {"peak": translate_text("Pic"), "average": translate_text("Moyenne")}
        for delta in comparison.curves:
            worst = ", ".join(format_time(t) for t in delta.worst) or "—"
            self._deltas.addWidget(_text(translate_text(
                "{kind} : B {ratio} en médiane, écart PQ médian {median} codes (99 % : {p99}), corrélation {corr} ; "
                "écarts les plus forts : {worst}.", kind=names.get(delta.kind, delta.kind),
                ratio=f"{(delta.median_ratio - 1) * 100:+.1f} %", median=f"{delta.median_delta:.1f}",
                p99=f"{delta.p99_delta:.1f}", corr=f"{delta.correlation:.3f}", worst=worst), size=10))
            if delta.source_a != delta.source_b:
                self._deltas.addWidget(_text(translate_text(
                    "Sources différentes ({a} face à {b}) : écart indicatif.",
                    a=translate_text(SOURCE_LABELS[delta.source_a]), b=translate_text(SOURCE_LABELS[delta.source_b])),
                    color=C.WARN, size=10))


__all__ = ["CompareTab", "DropLineEdit", "shifted"]
