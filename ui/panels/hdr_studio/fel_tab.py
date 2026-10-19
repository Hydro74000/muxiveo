"""Onglet FEL du HDR Studio : FEL Analyzer (apport de la couche d'amélioration Dolby Vision)."""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QImage, QMouseEvent, QPixmap
from PySide6.QtWidgets import QComboBox, QGridLayout, QHBoxLayout, QLabel, QSpinBox, QVBoxLayout, QWidget

from core.hdr_inspection import InspectionReport
from core.hdr_inspection.fel_analyzer import CATEGORIES, JND_NOTE, LEVELS, FelReport, Moment, recommendation
from core.i18n import translate_text
from ui.design_system import colors as C, font_px, scale
from ui.panels.hdr_studio.chart import format_time
from ui.styles import _card, _combo_style, _primary_button, _secondary_button, _section_label

#: Échantillonnage : un instant toutes les N secondes.
_DENSITIES = ((60, "Rapide : une image par minute"), (30, "Normal : une image toutes les 30 s"),
              (10, "Fin : une image toutes les 10 s"))
_ENCODERS = (("libx265", "x265 (logiciel)"), ("hevc_nvenc", "NVENC (NVIDIA)"), ("hevc_qsv", "QSV (Intel)"),
             ("hevc_vaapi", "VAAPI"))


def _text(text: str = "", *, color: str | None = None, size: int = 11, bold: bool = False) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    label.setStyleSheet(f"color:{color or C.TEXT_PRI};font-size:{font_px(size)}px;"
                        f"font-weight:{700 if bold else 400};background:transparent;")
    return label


def _pixmap(image: np.ndarray, width: int) -> QPixmap:
    """Vignette RGB 8 bits ramenée à ``width`` pixels (trois par ligne sans défilement horizontal)."""
    height, source_width, _channels = image.shape
    data = np.ascontiguousarray(image)
    qimage = QImage(data.data, source_width, height, 3 * source_width, QImage.Format.Format_RGB888)
    return QPixmap.fromImage(qimage.copy()).scaledToWidth(width, Qt.TransformationMode.SmoothTransformation)


class _ClickableLabel(QLabel):
    clicked = Signal()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (API Qt)
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


def _level_color(level: str) -> str:
    return {LEVELS[2]: C.OK, LEVELS[1]: C.WARN}.get(level, C.TEXT_DIM)


class FelTab(QWidget):
    """Résultats du FEL Analyzer ; émet les demandes d'analyse et de test de survie."""

    analysis_requested = Signal(int)
    survival_requested = Signal(str, int)
    #: Comparateur pleine définition d'un moment (instant en secondes).
    moment_requested = Signal(float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setStyleSheet("background:transparent;")
        self._report: InspectionReport | None = None
        self._fel: FelReport | None = None
        self._provisional = False
        self._busy = False
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, scale(10), 0, 0)
        self._layout.setSpacing(scale(10))
        self._layout.addWidget(_text(translate_text(
            "Compare l'image reconstruite avec le FEL à la couche de base passée par les courbes du RPU sans "
            "résiduel (ce qu'affiche une TV Dolby Vision avec la version P8.1 sans FEL), sur des images "
            "réparties dans le film. Niveaux faible / moyen / fort exprimés en seuils de visibilité (JND)."),
            color=C.TEXT_DIM, size=10))
        controls = _card()
        row = QHBoxLayout(controls)
        row.setContentsMargins(scale(14), scale(10), scale(14), scale(10))
        row.setSpacing(scale(8))
        self.density = QComboBox()
        self.density.setStyleSheet(_combo_style())
        for seconds, label in _DENSITIES:
            self.density.addItem(translate_text(label), seconds)
        self.density.setCurrentIndex(1)
        self.analyse_btn = _primary_button(translate_text("Analyser l'apport du FEL"))
        self.analyse_btn.clicked.connect(lambda: self.analysis_requested.emit(int(self.density.currentData())))
        row.addWidget(self.density)
        row.addWidget(self.analyse_btn)
        row.addStretch()
        self._layout.addWidget(controls)
        self._facts = self._section(translate_text("COUCHE D'AMÉLIORATION"))
        self._categories = self._section(translate_text("APPORT PAR CATÉGORIE"))
        survival_controls = QWidget()
        survival_controls.setStyleSheet("background:transparent;")
        survival_row = QHBoxLayout(survival_controls)
        survival_row.setContentsMargins(0, 0, 0, 0)
        survival_row.setSpacing(scale(8))
        self.encoder = QComboBox()
        self.encoder.setStyleSheet(_combo_style())
        for name, label in _ENCODERS:
            self.encoder.addItem(translate_text(label), name)
        self.bitrate = QSpinBox()
        self.bitrate.setRange(2, 200)
        self.bitrate.setValue(35)
        self.bitrate.setSuffix(" Mbit/s")
        self.bitrate.setStyleSheet(
            f"QSpinBox{{background:{C.BG_CARD};color:{C.TEXT_PRI};border:1px solid {C.BORDER};border-radius:5px;"
            f"padding:{scale(3)}px {scale(6)}px;font-size:{font_px(11)}px;}}")
        self.survival_btn = _secondary_button(translate_text("Tester la survie"))
        self.survival_btn.setToolTip(translate_text(
            "Encode une seconde du moment le plus détaillé au débit choisi puis mesure la part de l'apport "
            "du FEL conservée."))
        self.survival_btn.clicked.connect(
            lambda: self.survival_requested.emit(str(self.encoder.currentData()), self.bitrate.value() * 1000))
        for widget in (self.encoder, self.bitrate, self.survival_btn):
            survival_row.addWidget(widget)
        survival_row.addStretch()
        # Contrôles permanents, hors de la zone effacée à chaque rendu.
        self._survival = self._section(translate_text("SURVIE À L'ENCODAGE"), survival_controls)
        self._moments = self._section(translate_text("MOMENTS LES PLUS MARQUÉS"))
        self._layout.addStretch()
        self._render()

    def _section(self, title: str, permanent: QWidget | None = None) -> QVBoxLayout:
        card = _card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(scale(14), scale(10), scale(14), scale(12))
        layout.setSpacing(scale(6))
        layout.addWidget(_section_label(title))
        if permanent is not None:
            layout.addWidget(permanent)
        body = QVBoxLayout()
        body.setSpacing(scale(6))
        layout.addLayout(body)
        self._layout.addWidget(card)
        return body

    @staticmethod
    def _clear(layout: QVBoxLayout | QHBoxLayout | QGridLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            if item is None:
                continue
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
            elif (child := item.layout()) is not None:
                FelTab._clear(child)  # type: ignore[arg-type]

    # ── Données ─────────────────────────────────────────────────────────
    def set_report(self, report: InspectionReport | None) -> None:
        same = (report is not None and self._report is not None and report.snapshot.path == self._report.snapshot.path
                and report.snapshot.stream_index == self._report.snapshot.stream_index)
        self._report = report
        if not same:
            self._fel = None
        self._render()

    def set_fel_report(self, fel: FelReport | None, *, provisional: bool = False) -> None:
        """``provisional`` : résultat d'une passe grossière, l'analyse continue sur la densité choisie."""
        self._fel = fel
        self._provisional = provisional
        self._render()

    def fel_report(self) -> FelReport | None:
        return self._fel

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._sync()

    def available(self) -> bool:
        snapshot = self._report.snapshot if self._report is not None else None
        return snapshot is not None and snapshot.dovi is not None and snapshot.dovi.el

    def _sync(self) -> None:
        dovi = self._report.dovi if self._report is not None else None
        residual = dovi is None or (dovi.el_type == "FEL" and dovi.residual_active_frames > 0)
        self.analyse_btn.setEnabled(self.available() and residual and not self._busy)
        self.density.setEnabled(not self._busy)
        self.survival_btn.setEnabled(self._fel is not None and bool(self._fel.samples) and not self._busy)

    # ── Rendu ───────────────────────────────────────────────────────────
    def _render(self) -> None:
        for layout in (self._facts, self._categories, self._survival, self._moments):
            self._clear(layout)
        self._sync()
        if not self.available():
            self._facts.addWidget(_text(translate_text(
                "Aucune couche d'amélioration (EL) : le FEL Analyzer concerne les sources Dolby Vision profil 7."),
                color=C.TEXT_DIM))
            return
        self._render_facts()
        fel = self._fel
        dovi = self._report.dovi if self._report is not None else None
        if fel is None and dovi is not None and (dovi.el_type != "FEL" or not dovi.residual_active_frames):
            self._categories.addWidget(_text(translate_text(
                "Couche sans résiduel (MEL ou FEL vide) : l'image reconstruite est celle de la lecture P8.1, "
                "le FEL n'apporte rien à l'image."), color=C.TEXT_SEC))
            return
        if fel is None:
            self._categories.addWidget(_text(translate_text(
                "Lancer l'analyse : relevé des couches puis comparaison image par image (extension mvo-fel "
                "0.4.0 ou plus récente)."), color=C.TEXT_DIM))
            return
        verdict, detail = recommendation(fel)
        self._categories.addWidget(_text(verdict, bold=True, size=12))
        if self._provisional:
            self._categories.addWidget(_text(translate_text(
                "Verdict provisoire sur {count} instants : l'analyse continue sur la densité choisie.",
                count=len(fel.samples)), color=C.WARN, size=10))
        self._categories.addWidget(_text(detail, color=C.TEXT_SEC))
        grid = QGridLayout()
        grid.setHorizontalSpacing(scale(18))
        grid.setVerticalSpacing(scale(4))
        for column, title in enumerate((translate_text("Catégorie"), translate_text("Médiane"),
                                        translate_text("p90"), translate_text("Visibilité (p90)"),
                                        translate_text("Niveau"))):
            grid.addWidget(_text(title, color=C.TEXT_SEC, size=10, bold=True), 0, column)
        summaries = {s.key: s for s in fel.summary}
        for row, (key, label, unit, _thresholds) in enumerate(CATEGORIES, start=1):
            summary = summaries.get(key)
            if summary is None:
                continue
            grid.addWidget(_text(translate_text(label)), row, 0)
            grid.addWidget(_text(f"{summary.median:.2f} {translate_text(unit)}"), row, 1)
            grid.addWidget(_text(f"{summary.p90:.2f} {translate_text(unit)}"), row, 2)
            grid.addWidget(_text(translate_text("≈ {value} JND", value=f"{summary.jnd:.1f}")), row, 3)
            grid.addWidget(_text(translate_text(summary.level), color=_level_color(summary.level), bold=True), row, 4)
        overall, row = fel.overall, len(CATEGORIES) + 1
        grid.addWidget(_text(translate_text("Apport global (cumul)"), bold=True), row, 0)
        grid.addWidget(_text(f"{overall.median:.2f} JND"), row, 1)
        grid.addWidget(_text(f"{overall.p90:.2f} JND"), row, 2)
        grid.addWidget(_text(translate_text("≈ {value} JND", value=f"{overall.jnd:.1f}")), row, 3)
        grid.addWidget(_text(translate_text(overall.level), color=_level_color(overall.level), bold=True), row, 4)
        grid.setColumnStretch(5, 1)
        holder = QWidget()
        holder.setStyleSheet("background:transparent;")
        holder.setLayout(grid)
        self._categories.addWidget(holder)
        self._categories.addWidget(_text(translate_text("{count} instants mesurés.", count=len(fel.samples)),
                                         color=C.TEXT_DIM, size=10))
        self._categories.addWidget(_text(translate_text(JND_NOTE), color=C.TEXT_DIM, size=10))
        survival = fel.survival
        if survival is not None:
            self._survival.addWidget(_text(translate_text(
                "{encoder} à {bitrate} Mbit/s : ton conservé à {tone} %, détail à {detail} % "
                "(erreur d'encodage {error} codes RMS pour un apport de {contribution} codes RMS).",
                encoder=survival.encoder, bitrate=round(survival.bitrate_kbps / 1000), tone=round(survival.tone_kept * 100),
                detail=round(survival.detail_kept * 100), error=f"{survival.encode_error:.2f}",
                contribution=f"{survival.detail:.2f}")))
        for moment in fel.moments:
            self._moments.addWidget(self._moment_row(moment))
        if not fel.moments:
            self._moments.addWidget(_text(translate_text("Aucun moment mesuré."), color=C.TEXT_DIM))

    def _render_facts(self) -> None:
        report = self._report
        if report is None:
            return
        dovi = report.dovi
        facts = self._fel.facts if self._fel is not None else None
        rows: list[tuple[str, str]] = []
        if dovi is not None:
            el = dovi.el_type or "?"
            if dovi.el_type == "FEL" and dovi.frames:
                if dovi.residual_active_frames == 0:
                    el += " · " + translate_text("vide (résiduel désactivé partout)")
                else:
                    el += " · " + translate_text("résiduel actif sur {share} % des images",
                                                  share=round(100 * dovi.residual_active_frames / dovi.frames, 1))
            rows.append((translate_text("Type"), el))
        else:
            rows.append((translate_text("Type"), translate_text("lire les métadonnées dynamiques pour le préciser")))
        if facts is not None:
            if facts.el_size is not None:
                rows.append((translate_text("Résolution de l'EL"), f"{facts.el_size[0]}×{facts.el_size[1]}"))
            rows.append((translate_text("Débit de l'EL"), translate_text(
                "{rate} Mbit/s ({share} % du flux vidéo)", rate=f"{facts.bitrate(facts.el_bytes) / 1e6:.1f}",
                share=f"{facts.el_share * 100:.1f}")))
            rows.append((translate_text("Débit de la BL"), f"{facts.bitrate(facts.bl_bytes) / 1e6:.1f} Mbit/s"))
            rows.append((translate_text("RPU"), f"{facts.bitrate(facts.rpu_bytes) / 1e3:.0f} kbit/s"))
        grid = QGridLayout()
        grid.setHorizontalSpacing(scale(16))
        for index, (key, value) in enumerate(rows):
            grid.addWidget(_text(key, color=C.TEXT_SEC), index, 0)
            grid.addWidget(_text(value), index, 1)
        grid.setColumnStretch(1, 1)
        holder = QWidget()
        holder.setStyleSheet("background:transparent;")
        holder.setLayout(grid)
        self._facts.addWidget(holder)

    def _moment_row(self, moment: Moment) -> QWidget:
        row = QWidget()
        row.setStyleSheet("background:transparent;")
        layout = QVBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, scale(6))
        layout.setSpacing(scale(4))
        layout.addWidget(_text(translate_text("{time} · indice {score}", time=format_time(moment.time_s),
                                              score=f"{moment.score:.2f}"), bold=True))
        images = QHBoxLayout()
        images.setSpacing(scale(8))
        for title, image in ((translate_text("Sans FEL (P8.1)"), moment.mapped),
                             (translate_text("Avec FEL"), moment.reconstructed),
                             (translate_text("Écart (16 codes = jaune)"), moment.difference)):
            column = QVBoxLayout()
            picture = _ClickableLabel()
            picture.setPixmap(_pixmap(image, scale(300)))
            picture.setCursor(Qt.CursorShape.PointingHandCursor)
            picture.setToolTip(translate_text("Clic : comparateur avant / après en pleine définition (zoom, trait à "
                                              "glisser)."))
            picture.clicked.connect(lambda time_s=moment.time_s: self.moment_requested.emit(time_s))
            column.addWidget(picture)
            column.addWidget(_text(title, color=C.TEXT_SEC, size=10))
            images.addLayout(column)
        images.addStretch()
        layout.addLayout(images)
        return row
