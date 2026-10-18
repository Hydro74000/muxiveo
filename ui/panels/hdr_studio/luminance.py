"""Onglet Luminance du HDR Studio : courbes, MaxCLL/MaxFALL modifiables et aperçu du tone mapping."""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QCheckBox, QComboBox, QGridLayout, QHBoxLayout, QLabel, QSpinBox, QVBoxLayout, QWidget

from core.hdr_inspection import InspectionReport
from core.hdr_inspection.audit import Severity
from core.hdr_inspection.measure import ImageMeasurement
from core.hdr_inspection.pq import pq12_to_nits
from core.hdr_inspection.tonemap import (
    FrameLevels,
    ToneMapRisk,
    TvModel,
    analyse,
    frame_levels,
    suggested_light_levels,
)
from core.i18n import translate_text
from ui.design_system import colors as C, font_px, scale
from ui.panels.hdr_studio.chart import ChartMarker, ChartSeries, LuminanceChart, chart_controls
from ui.styles import _card, _checkbox_style, _combo_style, _primary_button, _secondary_button, _section_label

_SOURCE_LABELS = {
    "measure": "mesure de l'image (exacte)",
    "hdr10plus": "MaxScl HDR10+ (valeurs saturées écartées)",
    "l1": "L1 Dolby Vision : pic robuste, écrêtage sous-estimé ; mesurer l'image pour un résultat exact",
}
_MODELS = (
    (TvModel.MAXCLL, "TV suivant la MaxCLL (BT.2390)"),
    (TvModel.MDCV, "TV suivant le MDCV seul"),
    (TvModel.DYNAMIC, "TV à analyse dynamique (référence)"),
)


def _spin(maximum: int, suffix: str = "") -> QSpinBox:
    spin = QSpinBox()
    spin.setRange(0, maximum)
    spin.setSuffix(suffix)
    spin.setFixedWidth(scale(110))
    spin.setStyleSheet(
        f"QSpinBox{{background:{C.BG_CARD};color:{C.TEXT_PRI};border:1px solid {C.BORDER};border-radius:5px;"
        f"padding:{scale(3)}px {scale(6)}px;font-size:{font_px(11)}px;}}QSpinBox:focus{{border-color:{C.ACCENT};}}")
    return spin


def _text(text: str = "", *, color: str | None = None, size: int = 11) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    label.setStyleSheet(f"color:{color or C.TEXT_PRI};font-size:{font_px(size)}px;background:transparent;")
    return label


def _percent(value: float) -> str:
    return f"{value * 100:.3f}".rstrip("0").rstrip(".") if value < 0.01 else f"{value * 100:.1f}"


def describe_risk(risk: ToneMapRisk) -> tuple[str, str, str]:
    """Écrêtage, assombrissement, compression : trois phrases du bilan."""
    frames = max(1, risk.frames)
    if risk.clipped_frames:
        clip = translate_text("Écrêtage : {time} % du temps ({scenes} plan(s)), jusqu'à +{ev} EV au-delà de {level} nits",
                              time=_percent(risk.clipped_frames / frames), scenes=risk.clipped_scenes,
                              ev=f"{risk.worst_clip_ev:.1f}", level=round(risk.clip_level))
        if risk.clipped_pixels is not None:
            clip += translate_text(" ; {pixels} % des pixels du film", pixels=_percent(risk.clipped_pixels))
    else:
        clip = translate_text("Aucun écrêtage")
    if risk.darkened_frames:
        dark = translate_text("Hautes lumières assombries de {median} EV (médiane, p90 {p90} EV) sur {time} % du temps",
                              median=f"{risk.darkening_median_ev:.2f}", p90=f"{risk.darkening_p90_ev:.2f}",
                              time=_percent(risk.darkened_frames / frames))
    else:
        dark = translate_text("Pas d'assombrissement notable des hautes lumières")
    if risk.model is TvModel.DYNAMIC:
        knee = translate_text("Courbe adaptée à chaque image")
    elif risk.knee is None:
        knee = translate_text("Aucune compression : écran au moins aussi lumineux que le pic annoncé")
    else:
        knee = translate_text("Compression des hautes lumières à partir de {knee} nits (pic supposé {peak} nits)",
                              knee=round(risk.knee), peak=round(risk.source_peak or 0))
    return clip, dark, knee


class LuminanceTab(QWidget):
    """Courbes et simulation ; émet les demandes de mesure, d'application et de correction."""

    measure_requested = Signal()
    apply_to_encode_requested = Signal(int, int)
    prepare_patch_requested = Signal(int, int)
    display_peak_changed = Signal(int)
    values_changed = Signal(int, int)

    def __init__(self, display_peak: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._mdcv_choice: float | None = None
        self.setStyleSheet("background:transparent;")
        self._report: InspectionReport | None = None
        self._measurement: ImageMeasurement | None = None
        self._levels: FrameLevels | None = None
        self._original: tuple[int | None, int | None] = (None, None)
        self._suggested: tuple[int, int | None, str] | None = None
        self._series: list[ChartSeries] = []
        self._frames = 0
        self._keep_view = False
        self._recompute = QTimer(self)
        self._recompute.setSingleShot(True)
        self._recompute.setInterval(120)
        self._recompute.timeout.connect(self._update_preview)
        self._build(display_peak)

    # ── Construction ────────────────────────────────────────────────────
    def _build(self, display_peak: int) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, scale(10), 0, 0)
        layout.setSpacing(scale(8))
        self._legend = QHBoxLayout()
        self._legend.setSpacing(scale(14))
        layout.addLayout(self._legend)
        self.chart = LuminanceChart()
        self.chart_controls = chart_controls(self.chart)
        layout.addWidget(self.chart_controls)
        layout.addWidget(self.chart, 1)
        self._hover = _text(color=C.TEXT_SEC, size=10)
        self.chart.hover_changed.connect(self._hover.setText)
        layout.addWidget(self._hover)
        layout.addWidget(_text(translate_text(
            "Axes en nits, plage ajustée aux valeurs visibles (échelle PQ par défaut) ; pics et affichage TV à gauche, "
            "moyennes à droite ; un repère hors plage est signalé en haut (↑). Molette : zoom ; glisser : déplacement ; "
            "double-clic : vue entière. Le L1 Dolby est un maximum robuste (pixels isolés écartés) ; le MaxScl HDR10+ "
            "est un maximum strict."),
            color=C.TEXT_DIM, size=10))

        card = _card()
        grid = QVBoxLayout(card)
        grid.setContentsMargins(scale(14), scale(10), scale(14), scale(12))
        grid.setSpacing(scale(8))
        grid.addWidget(_section_label(translate_text("MÉTADONNÉES STATIQUES ET TONE MAPPING HDR10")))
        controls = QGridLayout()
        controls.setHorizontalSpacing(scale(10))
        controls.setVerticalSpacing(scale(6))
        self.max_cll = _spin(10000, " nits")
        self.max_fall = _spin(10000, " nits")
        self.display_peak = _spin(10000, " nits")
        self.display_peak.setMinimum(100)
        self.display_peak.setValue(display_peak)
        self.model = QComboBox()
        self.model.setStyleSheet(_combo_style())
        for model, label in _MODELS:
            self.model.addItem(translate_text(label), model.value)
        for column, (label, widget) in enumerate((("MaxCLL", self.max_cll), ("MaxFALL", self.max_fall))):
            controls.addWidget(_text(translate_text(label), color=C.TEXT_SEC), 0, 2 * column)
            controls.addWidget(widget, 0, 2 * column + 1)
        controls.addWidget(_text(translate_text("Pic de l'écran"), color=C.TEXT_SEC), 1, 0)
        controls.addWidget(self.display_peak, 1, 1)
        controls.addWidget(_text(translate_text("Téléviseur"), color=C.TEXT_SEC), 1, 2)
        controls.addWidget(self.model, 1, 3)
        controls.setColumnStretch(4, 1)
        grid.addLayout(controls)
        buttons = QHBoxLayout()
        buttons.setSpacing(scale(8))
        self.reset_btn = _secondary_button(translate_text("Valeurs du fichier"))
        self.reset_btn.clicked.connect(self.reset_values)
        self.suggest_btn = _secondary_button(translate_text("Valeurs proposées"))
        self.suggest_btn.clicked.connect(self.apply_suggestion)
        self.measure_btn = _primary_button(translate_text("Mesurer l'image"))
        self.measure_btn.setToolTip(translate_text(
            "Mesure exacte de chaque image sur GPU (extension mvo-fel 0.4.0 ou plus récente) : "
            "MaxCLL/MaxFALL réels, histogrammes, simulation exacte."))
        self.measure_btn.clicked.connect(self.measure_requested.emit)
        self.encode_btn = _secondary_button(translate_text("Appliquer au panneau Encodage"))
        self.encode_btn.clicked.connect(lambda: self.apply_to_encode_requested.emit(*self.values()))
        self.patch_btn = _secondary_button(translate_text("Préparer la correction du fichier…"))
        self.patch_btn.clicked.connect(lambda: self.prepare_patch_requested.emit(*self.values()))
        for button in (self.reset_btn, self.suggest_btn, self.measure_btn, self.encode_btn, self.patch_btn):
            buttons.addWidget(button)
        buttons.addStretch()
        grid.addLayout(buttons)
        self._risk_lines = [_text() for _ in range(3)]
        for line in self._risk_lines:
            grid.addWidget(line)
        self._original_line = _text(color=C.TEXT_SEC, size=10)
        self._source_line = _text(color=C.TEXT_DIM, size=10)
        self._note_line = _text(color=C.TEXT_DIM, size=10)
        for line in (self._original_line, self._source_line, self._note_line):
            grid.addWidget(line)
        layout.addWidget(card)
        for spin in (self.max_cll, self.max_fall):
            spin.valueChanged.connect(self._values_edited)
        self.display_peak.valueChanged.connect(self._display_peak_edited)
        self.model.currentIndexChanged.connect(lambda _i: self._recompute.start())

    # ── État ────────────────────────────────────────────────────────────
    def _display_peak_edited(self, value: int) -> None:
        self.display_peak_changed.emit(value)
        self._recompute.start()

    def values(self) -> tuple[int, int]:
        return self.max_cll.value(), self.max_fall.value()

    def _values_edited(self, _value: int) -> None:
        self.values_changed.emit(*self.values())
        self._recompute.start()

    def set_values(self, max_cll: int, max_fall: int) -> None:
        for spin, value in ((self.max_cll, max_cll), (self.max_fall, max_fall)):
            blocked = spin.blockSignals(True)
            spin.setValue(int(value))
            spin.blockSignals(blocked)
        self.values_changed.emit(*self.values())
        self._recompute.start()

    def reset_values(self) -> None:
        cll, fall = self._original
        self.set_values(cll or 0, fall or 0)

    def apply_suggestion(self) -> None:
        if self._suggested is None:
            return
        cll, fall, _source = self._suggested
        self.set_values(cll, fall if fall is not None else self.max_fall.value())

    def set_busy(self, busy: bool) -> None:
        self.measure_btn.setEnabled(not busy and self._report is not None)

    def set_report(self, report: InspectionReport | None, measurement: ImageMeasurement | None) -> None:
        """Nouveau rapport : séries, valeurs d'origine (fichier) et proposition."""
        same_file = (report is not None and self._report is not None
                     and report.snapshot.path == self._report.snapshot.path
                     and report.snapshot.stream_index == self._report.snapshot.stream_index)
        self._report, self._measurement = report, measurement
        self._levels = frame_levels(report, measurement)
        self._suggested = suggested_light_levels(report, measurement)
        snapshot = report.snapshot if report is not None else None
        self._original = (snapshot.max_cll, snapshot.max_fall) if snapshot is not None else (None, None)
        if not same_file:
            self.set_values(self._original[0] or 0, self._original[1] or 0)
        self.suggest_btn.setEnabled(self._suggested is not None)
        if self._suggested is not None:
            cll, fall, source = self._suggested
            self.suggest_btn.setToolTip(translate_text("{cll} / {fall} d'après : {source}", cll=cll,
                                                       fall=fall if fall is not None else "—",
                                                       source=translate_text(_SOURCE_LABELS[source])))
        enabled = report is not None and snapshot is not None and snapshot.is_pq
        for widget in (self.max_cll, self.max_fall, self.reset_btn, self.encode_btn, self.patch_btn):
            widget.setEnabled(bool(enabled))
        self.measure_btn.setEnabled(report is not None)
        self.measure_btn.setText(translate_text("Mesurer à nouveau") if measurement is not None
                                 else translate_text("Mesurer l'image"))
        self._build_series(keep_view=same_file)
        self._update_preview()

    # ── Courbes ─────────────────────────────────────────────────────────
    def _build_series(self, *, keep_view: bool) -> None:
        while self._legend.count():
            item = self._legend.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        report, measurement = self._report, self._measurement
        series: list[ChartSeries] = []
        frames = 0
        if measurement is not None and measurement.frames:
            series.append(ChartSeries("peak", translate_text("Pic mesuré"), C.TRACK_VIDEO,
                                      measurement.peaks().astype(np.float32), "max", fill=True))
            series.append(ChartSeries("average", translate_text("Moyenne mesurée"), C.OK,
                                      measurement.average, "mean"))
            frames = measurement.frames
        dovi = report.dovi if report is not None else None
        if dovi is not None and dovi.frames:
            series.append(ChartSeries("l1_max", translate_text("L1 max"), C.PURPLE,
                                      pq12_to_nits(dovi.l1_max).astype(np.float32), "max",
                                      visible=measurement is None, fill=measurement is None))
            series.append(ChartSeries("l1_avg", translate_text("L1 moyenne"), C.ACCENT,
                                      pq12_to_nits(dovi.l1_avg).astype(np.float32), "mean",
                                      visible=measurement is None))
            frames = max(frames, dovi.frames)
        hdr10plus = report.hdr10plus if report is not None else None
        if hdr10plus is not None and hdr10plus.frames:
            alone = measurement is None and dovi is None
            series.append(ChartSeries("maxscl", translate_text("MaxScl HDR10+"), C.WARN, hdr10plus.max_scl, "max",
                                      visible=alone, fill=alone))
            series.append(ChartSeries("avgrgb", translate_text("AverageRGB HDR10+"), C.TRACK_TAGS,
                                      hdr10plus.average_rgb, "mean", visible=alone))
            frames = max(frames, hdr10plus.frames)
        if self._levels is not None:
            series.append(ChartSeries("displayed", translate_text("Affiché par la TV"), C.ERROR,
                                      np.full(self._levels.frames, np.nan, dtype=np.float32), "max"))
        self._series = series
        self._frames = frames
        self._keep_view = keep_view
        for entry in series:
            box = QCheckBox(entry.label)
            box.setChecked(entry.visible)
            box.setStyleSheet(_checkbox_style() + f"QCheckBox{{color:{entry.color};}}")
            box.toggled.connect(lambda checked, key=entry.key: self.chart.set_series_visible(key, checked))
            self._legend.addWidget(box)
        self._legend.addStretch()
        self._hover.setText("" if series else translate_text(
            "Lire les métadonnées dynamiques ou mesurer l'image pour afficher les courbes."))

    def _markers(self) -> list[ChartMarker]:
        report = self._report
        if report is None:
            return []
        snapshot = report.snapshot
        markers: list[ChartMarker] = []
        cll, _fall = self.values()
        if snapshot.max_cll:
            markers.append(ChartMarker(translate_text("MaxCLL du fichier"), float(snapshot.max_cll), C.OK))
        if cll and cll != snapshot.max_cll:
            markers.append(ChartMarker(translate_text("MaxCLL choisie"), float(cll), C.ACCENT))
        markers.append(ChartMarker(translate_text("Écran"), float(self.display_peak.value()), C.TEXT_SEC))
        if snapshot.mastering is not None and snapshot.mastering.max_nits:
            markers.append(ChartMarker(translate_text("MDCV"), float(snapshot.mastering.max_nits), C.TEXT_DIM))
        if self._mdcv_choice is not None and self._mdcv_choice != self._file_mdcv():
            markers.append(ChartMarker(translate_text("MDCV choisi"), self._mdcv_choice, C.PURPLE))
        return markers

    def set_mastering_choice(self, max_nits: float | None) -> None:
        """MDCV choisi dans l'onglet Édition : modèle de TV « suit le MDCV » et bilan mis à jour."""
        if max_nits != self._mdcv_choice:
            self._mdcv_choice = max_nits
            self._update_preview()

    def _file_mdcv(self) -> float | None:
        mastering = self._report.snapshot.mastering if self._report is not None else None
        return mastering.max_nits if mastering is not None else None

    def _risk(self, max_cll: int | None, *, original: bool = False) -> ToneMapRisk | None:
        if self._levels is None or self._report is None:
            return None
        mdcv = self._file_mdcv() if original or self._mdcv_choice is None else self._mdcv_choice
        return analyse(self._levels, model=TvModel(self.model.currentData()), max_cll=max_cll, mdcv_max=mdcv,
                       display_peak=float(self.display_peak.value()))

    def current_risk(self) -> ToneMapRisk | None:
        return self._risk(self.values()[0])

    def _update_preview(self) -> None:
        """Aperçu dynamique : courbe affichée, images écrêtées, bilan face aux valeurs du fichier."""
        risk = self.current_risk()
        for entry in self._series:
            if entry.key == "displayed" and risk is not None:
                entry.values = risk.displayed_peak
        self.chart.set_data(frames=self._frames,
                            fps=self._report.snapshot.fps if self._report is not None else None,
                            series=self._series, markers=self._markers(),
                            scenes=self._levels.scene_starts if self._levels is not None else None,
                            keep_view=self._keep_view)
        # Valeurs modifiées ensuite : même fichier, zoom conservé.
        self._keep_view = True
        if risk is None or self._levels is None:
            for line in self._risk_lines:
                line.setText("")
            self._original_line.setText("")
            self._source_line.setText("")
            self._note_line.setText("")
            self.chart.set_bands(None)
            return
        peak = self._levels.peak
        self.chart.set_bands(np.isfinite(peak) & (peak > risk.clip_level))
        severity = risk.severity()
        color = {Severity.ERROR: C.ERROR, Severity.WARN: C.WARN}.get(severity, C.OK)
        for line, text in zip(self._risk_lines, describe_risk(risk)):
            line.setText(text)
        self._risk_lines[0].setStyleSheet(f"color:{color};font-size:{font_px(11)}px;font-weight:700;background:transparent;")
        original_cll, original_fall = self._original
        mdcv_changed = self._mdcv_choice is not None and self._mdcv_choice != self._file_mdcv()
        if (original_cll, original_fall) != self.values() or mdcv_changed:
            original = self._risk(original_cll, original=True)
            if original is not None:
                clip, dark, _knee = describe_risk(original)
                self._original_line.setText(translate_text(
                    "Valeurs du fichier ({cll}/{fall}) : {clip} ; {dark}.",
                    cll=original_cll if original_cll is not None else "—",
                    fall=original_fall if original_fall is not None else "—", clip=clip, dark=dark.lower()))
        else:
            self._original_line.setText(translate_text("Valeurs du fichier affichées."))
        self._source_line.setText(translate_text("Niveaux : {source}.", source=translate_text(
            _SOURCE_LABELS[self._levels.source])))
        snapshot = self._report.snapshot if self._report is not None else None
        notes = [translate_text("Modèle de référence (BT.2390), pas le comportement exact d'un téléviseur donné. "
                                "La MaxFALL est rarement utilisée par les téléviseurs.")]
        if snapshot is not None and (snapshot.dovi is not None or snapshot.hdr10plus):
            notes.append(translate_text("En lecture Dolby Vision ou HDR10+, la TV suit les métadonnées dynamiques : "
                                        "MaxCLL/MaxFALL ne servent qu'en lecture HDR10."))
        self._note_line.setText(" ".join(notes))
