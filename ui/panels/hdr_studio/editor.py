"""Onglet Édition du HDR Studio : corrections du HDR10 statique, du RPU et du HDR10+ sans réencodage."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from core.hdr_inspection import InspectionReport
from core.hdr_inspection.measure import ImageMeasurement
from core.hdr_inspection.patch import Hdr10PlusEdit, MasteringEdit, PatchPlan, RpuEdit, StaticEdit, suggested_mastering
from core.hdr_inspection.realign import Realignment, scene_cuts
from core.hdr_inspection.snapshot import PRIMARY_SETS
from core.i18n import translate_text
from ui.design_system import colors as C, font_px, scale
from ui.styles import _card, _checkbox_style, _combo_style, _input_style, _secondary_button, _section_label

_SPIN_STYLE = (
    "QSpinBox, QDoubleSpinBox{{background:{bg};color:{fg};border:1px solid {border};border-radius:5px;"
    "padding:{pad}px {padh}px;font-size:{size}px;}}"
)
_L11_LABELS = ("Défaut", "Cinéma", "Jeu", "Sport", "Contenu utilisateur")
_MASTERING_SOURCES = {"l6": "L6 du RPU", "source_display": "écran de mastering du RPU",
                      "standard": "écran usuel couvrant le pic du contenu"}


def _style(widget: QWidget) -> QWidget:
    widget.setStyleSheet(_SPIN_STYLE.format(bg=C.BG_CARD, fg=C.TEXT_PRI, border=C.BORDER, pad=scale(3),
                                            padh=scale(6), size=font_px(11)))
    widget.setFixedWidth(scale(110))
    return widget


def _int_spin(maximum: int, suffix: str = "") -> QSpinBox:
    spin = QSpinBox()
    spin.setRange(0, maximum)
    spin.setSuffix(suffix)
    _style(spin)
    return spin


def _float_spin(maximum: float, decimals: int, suffix: str = "") -> QDoubleSpinBox:
    spin = QDoubleSpinBox()
    spin.setRange(0.0, maximum)
    spin.setDecimals(decimals)
    spin.setSingleStep(10 ** -decimals)
    spin.setSuffix(suffix)
    _style(spin)
    return spin


def _label(text: str, *, color: str | None = None, size: int = 11) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet(f"color:{color or C.TEXT_SEC};font-size:{font_px(size)}px;background:transparent;")
    return label


def _check(text: str) -> QCheckBox:
    box = QCheckBox(text)
    box.setStyleSheet(_checkbox_style())
    return box


def _row(*items: QWidget | str) -> QHBoxLayout:
    row = QHBoxLayout()
    row.setSpacing(scale(8))
    row.setContentsMargins(scale(22), 0, 0, 0)
    for item in items:
        row.addWidget(_label(translate_text(item)) if isinstance(item, str) else item)
    row.addStretch()
    return row


class EditionTab(QWidget):
    """Choix des corrections et de la sortie ; le plan prêt (``readiness``) est écrit depuis la barre d'exécution."""

    #: Préparation modifiée (cases, valeurs, sortie) : l'état de la barre d'exécution est recalculé.
    changed = Signal()
    luminance_requested = Signal()
    #: Luminance max. du MDCV choisi (nits), None s'il n'est pas modifié : aperçu de l'onglet Luminance.
    mastering_changed = Signal(object)
    #: MaxCLL, MaxFALL proposés par le réalignement : valeurs de l'onglet Luminance.
    light_levels_requested = Signal(int, int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setStyleSheet("background:transparent;")
        self._report: InspectionReport | None = None
        self._static_values = (0, 0)
        self._busy = False
        self._ready_plan: PatchPlan | None = None
        self._reason = ""
        self._syncing = False
        self._suggested: tuple[MasteringEdit, str] | None = None
        self._prefilled: tuple[str, int, bool] | None = None
        self._emitted_mastering: float | None = None
        self._measurement: ImageMeasurement | None = None
        self._realignment: Realignment | None = None
        self._realign_boxes: dict[str, QCheckBox] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, scale(10), 0, 0)
        layout.setSpacing(scale(10))
        layout.addWidget(_label(translate_text(
            "Les corrections réécrivent le fichier sans réencoder l'image : flux vidéo extrait, métadonnées "
            "corrigées par dovi_tool et hdr10plus_tool, puis replacé dans une copie du MKV (autres pistes, "
            "chapitres et pièces jointes conservés). La source n'est jamais modifiée."), color=C.TEXT_DIM, size=10))
        self._realign_card = self._build_realign()
        layout.addWidget(self._realign_card)
        layout.addWidget(self._build_static())
        self._dovi_card = self._build_dovi()
        layout.addWidget(self._dovi_card)
        self._hdr10plus_card = self._build_hdr10plus()
        layout.addWidget(self._hdr10plus_card)
        layout.addWidget(self._build_output())
        layout.addStretch()
        for box in self.findChildren(QCheckBox):
            box.toggled.connect(self._sync)
        # Écran de mastering : MDCV, L6 et écran du RPU tenus identiques si demandé.
        for origin, spins in (("mdcv", (self.mdcv_min, self.mdcv_max)), ("l6", (self.l6_min_mastering, self.l6_max_mastering)),
                              ("source", (self.source_min, self.source_max))):
            for spin in spins:
                spin.valueChanged.connect(lambda _v, origin=origin: self._mastering_edited(origin))
        self._sync()

    # ── Cartes ──────────────────────────────────────────────────────────
    def _card(self, title: str) -> tuple[QWidget, QVBoxLayout]:
        card = _card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(scale(14), scale(10), scale(14), scale(12))
        layout.setSpacing(scale(6))
        layout.addWidget(_section_label(title))
        return card, layout

    def _build_realign(self) -> QWidget:
        card, layout = self._card(translate_text("RÉALIGNEMENT D'APRÈS LA MESURE"))
        layout.addWidget(_label(translate_text(
            "Écarts entre les métadonnées et l'image mesurée qui ont un effet visible (écrêtage ou assombrissement "
            "simulés sur l'écran choisi dans l'onglet Luminance). Les propositions cochées sont reportées dans les "
            "corrections ci-dessous ; l'écriture se lance ensuite depuis la barre d'exécution."), color=C.TEXT_DIM,
            size=10))
        self._realign_rows = QVBoxLayout()
        self._realign_rows.setSpacing(scale(6))
        layout.addLayout(self._realign_rows)
        self.realign_btn = _secondary_button(translate_text("Reporter les propositions cochées"))
        self.realign_btn.clicked.connect(self.apply_realignment)
        actions = QHBoxLayout()
        actions.addWidget(self.realign_btn)
        actions.addStretch()
        layout.addLayout(actions)
        card.setVisible(False)
        return card

    def set_realignment(self, realignment: Realignment | None) -> None:
        """Propositions de réalignement (mesure de l'image) ; carte masquée sans écart visible."""
        self._realignment = realignment
        while self._realign_rows.count():
            item = self._realign_rows.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._realign_boxes = {}
        proposals = realignment.proposals if realignment is not None else ()
        for proposal in proposals:
            row = QWidget()
            row.setStyleSheet("background:transparent;")
            column = QVBoxLayout(row)
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(scale(2))
            text = f"{proposal.title} : {proposal.current} → {proposal.proposed}"
            if proposal.applicable:
                box = _check(text)
                box.setChecked(True)
                self._realign_boxes[proposal.key] = box
                column.addWidget(box)
            else:
                column.addWidget(_label("⚠ " + text, color=C.WARN))
            impact = _label(proposal.impact, color=C.TEXT_DIM, size=10)
            impact.setContentsMargins(scale(22), 0, 0, 0)
            column.addWidget(impact)
            self._realign_rows.addWidget(row)
        self.realign_btn.setVisible(bool(self._realign_boxes))
        self._realign_card.setVisible(bool(proposals))

    def apply_realignment(self) -> None:
        """Reporte les propositions cochées dans les corrections (cases cochées, valeurs remplies)."""
        realignment = self._realignment
        if realignment is None:
            return
        chosen = {key for key, box in self._realign_boxes.items() if box.isChecked()}
        if "static" in chosen and realignment.light_levels is not None:
            self.light_levels_requested.emit(*realignment.light_levels)
            self.static_box.setChecked(True)
        if "mdcv" in chosen and realignment.mastering is not None:
            edit = realignment.mastering
            self._set_mdcv(edit.primaries, edit.min_nits, edit.max_nits)
            self.mdcv_box.setChecked(True)
            self._mastering_edited("mdcv")
        if "l6" in chosen and realignment.l6 is not None:
            max_mastering, min_mastering, cll, fall = realignment.l6
            self.l6_max_mastering.setValue(int(max_mastering))
            self.l6_min_mastering.setValue(float(min_mastering))
            self.l6_cll.setValue(int(cll))
            self.l6_fall.setValue(int(fall))
            self.l6_box.setChecked(True)
        if "hdr10plus" in chosen and realignment.hdr10plus_scenes is not None:
            self.h10_remove.setChecked(False)
            self.h10_recompute.setChecked(True)
        self._sync()

    def _build_static(self) -> QWidget:
        card, layout = self._card(translate_text("HDR10 STATIQUE (SEI ET CONTENEUR)"))
        self.static_box = _check(translate_text("Écrire MaxCLL/MaxFALL"))
        layout.addWidget(self.static_box)
        self._static_label = _label("")
        self._static_label.setWordWrap(False)
        go = _secondary_button(translate_text("Modifier dans Luminance"))
        go.clicked.connect(self.luminance_requested.emit)
        row = _row(self._static_label, go)
        layout.addLayout(row)
        self.mdcv_box = _check(translate_text("Écrire l'écran de mastering (MDCV)"))
        layout.addWidget(self.mdcv_box)
        self.mdcv_primaries = QComboBox()
        self.mdcv_primaries.setStyleSheet(_combo_style())
        for name in PRIMARY_SETS:
            self.mdcv_primaries.addItem(name, name)
        self.mdcv_primaries.currentIndexChanged.connect(lambda _i: self._sync())
        self.mdcv_min = _float_spin(50.0, 4, " nits")
        self.mdcv_max = _int_spin(10000, " nits")
        self.mdcv_max.setMinimum(1)
        self.suggest_mdcv = _secondary_button(translate_text("Valeurs proposées"))
        self.suggest_mdcv.clicked.connect(self.apply_suggested_mastering)
        layout.addLayout(_row(self.mdcv_primaries, "min.", self.mdcv_min, "max.", self.mdcv_max, self.suggest_mdcv))
        self._mdcv_hint = _label("", color=C.TEXT_DIM, size=10)
        self._mdcv_hint.setContentsMargins(scale(22), 0, 0, 0)
        layout.addWidget(self._mdcv_hint)
        self.mastering_sync = _check(translate_text(
            "Garder identiques le MDCV, le mastering du L6 et l'écran de mastering du RPU"))
        self.mastering_sync.setChecked(True)
        layout.addWidget(self.mastering_sync)
        return card

    def _build_dovi(self) -> QWidget:
        card, layout = self._card(translate_text("DOLBY VISION (RPU)"))
        self._dovi_hint = _label("", color=C.TEXT_DIM, size=10)
        layout.addWidget(self._dovi_hint)
        self.l6_box = _check(translate_text("L6 : HDR10 porté par le RPU"))
        self.l6_max_mastering = _int_spin(10000, " nits")
        self.l6_min_mastering = _float_spin(1.0, 4, " nits")
        self.l6_cll = _int_spin(10000, " nits")
        self.l6_fall = _int_spin(10000, " nits")
        copy = _secondary_button(translate_text("Reprendre MaxCLL/MaxFALL choisis"))
        copy.clicked.connect(self._copy_static_to_l6)
        layout.addWidget(self.l6_box)
        layout.addLayout(_row("Mastering max.", self.l6_max_mastering, "min.", self.l6_min_mastering))
        layout.addLayout(_row("MaxCLL", self.l6_cll, "MaxFALL", self.l6_fall, copy))
        self.l5_box = _check(translate_text("L5 : zone active de toutes les images"))
        self.l5 = [_int_spin(4096, " px") for _ in range(4)]
        layout.addWidget(self.l5_box)
        layout.addLayout(_row("Gauche", self.l5[0], "Droite", self.l5[1], "Haut", self.l5[2], "Bas", self.l5[3]))
        self.l11_box = _check(translate_text("L11 : type de contenu"))
        self.l11_content = QComboBox()
        self.l11_content.setStyleSheet(_combo_style())
        for index, label in enumerate(_L11_LABELS):
            self.l11_content.addItem(translate_text(label), index)
        self.l11_whitepoint = _int_spin(15)
        self.l11_reference = _check(translate_text("Mode de référence"))
        layout.addWidget(self.l11_box)
        layout.addLayout(_row(self.l11_content, "Point blanc", self.l11_whitepoint, self.l11_reference))
        self.source_box = _check(translate_text("Écran de mastering du RPU"))
        self.source_min = _float_spin(1.0, 4, " nits")
        self.source_max = _int_spin(10000, " nits")
        layout.addWidget(self.source_box)
        layout.addLayout(_row("min.", self.source_min, "max.", self.source_max))
        self.cmv4_box = _check(translate_text("Retirer les métadonnées CM v4.0 (RPU en CM v2.9)"))
        layout.addWidget(self.cmv4_box)
        return card

    def _build_hdr10plus(self) -> QWidget:
        card, layout = self._card(translate_text("HDR10+"))
        self.h10_remove = _check(translate_text("Retirer le HDR10+"))
        self.h10_recompute = _check(translate_text("Recalculer le HDR10+ depuis la mesure de l'image (profil A)"))
        self.h10_recompute.setToolTip(translate_text(
            "Statistiques de chaque image mesurée, regroupées par plan (règles du HDR10+ recalculé du FEL) : "
            "MaxScl, percentiles et AverageRGB décrivent l'image réelle."))
        self.h10_target_box = _check(translate_text("Écran ciblé (TSDML)"))
        self.h10_target = _int_spin(10000, " nits")
        self.h10_bezier = _check(translate_text("Retirer la courbe de Bézier (profil A)"))
        self.h10_cap_box = _check(translate_text("Plafonner MaxScl, AverageRGB et percentiles"))
        self.h10_cap = _int_spin(10000, " nits")
        self.h10_cap.setMinimum(100)
        layout.addWidget(self.h10_remove)
        layout.addWidget(self.h10_recompute)
        layout.addWidget(self.h10_target_box)
        layout.addLayout(_row(self.h10_target))
        layout.addWidget(self.h10_bezier)
        layout.addWidget(self.h10_cap_box)
        layout.addLayout(_row(self.h10_cap))
        return card

    def _build_output(self) -> QWidget:
        card, layout = self._card(translate_text("SORTIE"))
        row = QHBoxLayout()
        row.setSpacing(scale(6))
        self.output = QLineEdit()
        self.output.setStyleSheet(_input_style())
        self.output.textChanged.connect(lambda _t: self._sync())
        browse = _secondary_button("…", fixed_width=32)
        browse.setToolTip(translate_text("Choisir le fichier de sortie"))
        browse.clicked.connect(self._browse)
        row.addWidget(self.output, 1)
        row.addWidget(browse)
        layout.addLayout(row)
        self._summary = _label("", color=C.TEXT_PRI)
        layout.addWidget(self._summary)
        layout.addWidget(_label(translate_text(
            "Lancement : bouton « Écrire le fichier corrigé » de la barre d'exécution, sous le panneau."),
            color=C.TEXT_DIM, size=10))
        return card

    # ── Données ─────────────────────────────────────────────────────────
    def set_report(self, report: InspectionReport | None, measurement: ImageMeasurement | None = None) -> None:
        """Préremplit avec les valeurs actuelles du fichier (RPU et HDR10+ si lus)."""
        previous = self._report.snapshot.path if self._report is not None else None
        self._report = report
        self._measurement = measurement
        if report is None:
            self._suggested = None
            self._prefilled = None
            self._sync()
            return
        snapshot = report.snapshot
        self._suggested = suggested_mastering(report, measurement)
        # Valeurs du fichier reprises seulement pour une autre piste ou à l'arrivée du RPU / HDR10+ :
        # un nouveau rendu (mesure, cache) ne remplace pas les valeurs saisies.
        key = (snapshot.path, snapshot.stream_index, report.dynamic)
        prefill = key != self._prefilled
        self._prefilled = key
        self._syncing = True  # valeurs du fichier : aucune propagation
        try:
            if prefill and snapshot.path != previous:
                path = Path(snapshot.path)
                self.output.setText(str(path.with_name(f"{path.stem}.hdrfix{path.suffix}")))
                for box in self.findChildren(QCheckBox):
                    box.setChecked(box is self.mastering_sync)
            if prefill:
                dovi = report.dovi
                mastering = snapshot.mastering
                if dovi is not None and dovi.l6:
                    l6 = dovi.l6[0]
                    values = (l6.max_mastering, l6.min_mastering, l6.max_cll, l6.max_fall)
                else:
                    values = (int(mastering.max_nits or 1000) if mastering else 1000,
                              float(mastering.min_nits or 0.0001) if mastering else 0.0001,
                              snapshot.max_cll or 0, snapshot.max_fall or 0)
                self.l6_max_mastering.setValue(int(values[0]))
                self.l6_min_mastering.setValue(float(values[1]))
                self.l6_cll.setValue(int(values[2]))
                self.l6_fall.setValue(int(values[3]))
                if dovi is not None and dovi.active_areas:
                    area = dovi.active_areas[0]
                    for spin, value in zip(self.l5, (area.left, area.right, area.top, area.bottom)):
                        spin.setValue(value)
                if dovi is not None and dovi.l11_content:
                    self.l11_content.setCurrentIndex(max(0, min(4, dovi.l11_content[0])))
                if dovi is not None and dovi.source_display:
                    low, high = dovi.source_display[0]
                    self.source_min.setValue(low)
                    self.source_max.setValue(high)
                hdr10plus = report.hdr10plus
                targets = [t for t in hdr10plus.targeted_display if t] if hdr10plus is not None else []
                self.h10_target.setValue(targets[0] if targets else 1000)
                if hdr10plus is not None and hdr10plus.frames:
                    self.h10_cap.setValue(max(100, int(round(hdr10plus.credible_peak_nits() or 1000))))
                if (mastering is not None and mastering.primaries in PRIMARY_SETS and mastering.max_nits
                        and mastering.min_nits is not None):
                    self._set_mdcv(mastering.primaries, float(mastering.min_nits or 0), float(mastering.max_nits or 0))
                else:
                    edit = self._suggested[0]
                    self._set_mdcv(edit.primaries, edit.min_nits, edit.max_nits)
        finally:
            self._syncing = False
        self._dovi_card.setVisible(snapshot.dovi is not None)
        self._hdr10plus_card.setVisible(snapshot.hdr10plus)
        self._dovi_hint.setText(translate_text(
            "Lire les métadonnées dynamiques pour préremplir les valeurs actuelles du RPU."))
        self._dovi_hint.setVisible(report.dovi is None)
        self.cmv4_box.setEnabled(report.dovi is not None and report.dovi.cm_version == "4.0")
        self._sync()

    def set_static_values(self, max_cll: int, max_fall: int) -> None:
        self._static_values = (max_cll, max_fall)
        self._sync()

    def enable_static(self) -> None:
        self.static_box.setChecked(True)

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._sync()

    def _set_mdcv(self, primaries: str, min_nits: float, max_nits: float) -> None:
        self.mdcv_primaries.setCurrentIndex(max(0, self.mdcv_primaries.findData(primaries)))
        self.mdcv_min.setValue(min_nits)
        self.mdcv_max.setValue(int(round(max_nits)))

    def apply_suggested_mastering(self) -> None:
        """Écran de mastering proposé (L6, écran du RPU ou standard selon le pic) écrit dans le MDCV."""
        if self._suggested is None:
            return
        edit = self._suggested[0]
        self._set_mdcv(edit.primaries, edit.min_nits, edit.max_nits)
        self.mdcv_box.setChecked(True)
        self._mastering_edited("mdcv")

    def _mastering_groups(self) -> dict[str, tuple[QCheckBox, QDoubleSpinBox, QSpinBox]]:
        groups = {"mdcv": (self.mdcv_box, self.mdcv_min, self.mdcv_max)}
        if self._report is not None and self._report.snapshot.dovi is not None:
            groups["l6"] = (self.l6_box, self.l6_min_mastering, self.l6_max_mastering)
            groups["source"] = (self.source_box, self.source_min, self.source_max)
        return groups

    def _mastering_edited(self, origin: str) -> None:
        """Valeur de mastering modifiée : recopiée dans les autres groupes (et leur case) si synchronisés."""
        groups = self._mastering_groups()
        if self._syncing or origin not in groups or not self.mastering_sync.isChecked():
            self._sync()
            return
        box, low, high = groups[origin]
        self._syncing = True
        try:
            for name, (other_box, other_low, other_high) in groups.items():
                if name == origin:
                    continue
                other_low.setValue(low.value())
                other_high.setValue(high.value())
                if box.isChecked():
                    other_box.setChecked(True)
        finally:
            self._syncing = False
        self._sync()

    def _copy_static_to_l6(self) -> None:
        self.l6_cll.setValue(self._static_values[0])
        self.l6_fall.setValue(self._static_values[1])
        self.l6_box.setChecked(True)

    def _browse(self) -> None:
        start = self.output.text().strip() or (self._report.snapshot.path if self._report else str(Path.home()))
        path, _ = QFileDialog.getSaveFileName(self, translate_text("Fichier corrigé"), start,
                                              translate_text("Matroska (*.mkv);;HEVC (*.hevc *.h265)"))
        if path:
            self.output.setText(path)

    def plan(self) -> PatchPlan | None:
        """Plan des corrections cochées ; None sans source."""
        report = self._report
        if report is None:
            return None
        levels = self._static_values if self.static_box.isChecked() else (None, None)
        mastering = self._mastering_edit() if self.mdcv_box.isChecked() else None
        static = (StaticEdit(max_cll=levels[0], max_fall=levels[1], mastering=mastering)
                  if levels[0] is not None or mastering is not None else None)
        rpu = RpuEdit(
            l6=(self.l6_max_mastering.value(), self.l6_min_mastering.value(), self.l6_cll.value(), self.l6_fall.value())
            if self.l6_box.isChecked() else None,
            active_area=tuple(spin.value() for spin in self.l5) if self.l5_box.isChecked() else None,  # type: ignore[arg-type]
            l11=(int(self.l11_content.currentData()), self.l11_whitepoint.value(), self.l11_reference.isChecked())
            if self.l11_box.isChecked() else None,
            source_display=(self.source_min.value(), float(self.source_max.value())) if self.source_box.isChecked() else None,
            remove_cmv4=self.cmv4_box.isChecked() and self.cmv4_box.isEnabled(),
        ) if report.snapshot.dovi is not None else None
        hdr10plus = Hdr10PlusEdit(
            remove=self.h10_remove.isChecked(),
            targeted_display=self.h10_target.value() if self.h10_target_box.isChecked() else None,
            drop_bezier=self.h10_bezier.isChecked(),
            cap_nits=float(self.h10_cap.value()) if self.h10_cap_box.isChecked() else None,
            recompute=self._recompute_source() if self.h10_recompute.isChecked() and self.h10_recompute.isEnabled()
            else None,
        ) if report.snapshot.hdr10plus else None
        return PatchPlan(Path(report.snapshot.path), report.snapshot.stream_index, Path(self.output.text().strip()),
                         static=static, rpu=rpu, hdr10plus=hdr10plus)

    def _recompute_source(self) -> tuple[object, object] | None:
        """Mesure et débuts de plans du HDR10+ recalculé (plans du HDR10+ actuel si cohérents)."""
        measurement = self._measurement
        if measurement is None:
            return None
        realignment = self._realignment
        if realignment is not None and realignment.hdr10plus_scenes is not None:
            return measurement, realignment.hdr10plus_scenes
        hdr10plus = self._report.hdr10plus if self._report is not None else None
        if hdr10plus is not None and hdr10plus.scenes >= 2 and hdr10plus.frames == measurement.frames:
            return measurement, hdr10plus.scene_starts
        return measurement, scene_cuts(measurement)

    def _mastering_edit(self) -> MasteringEdit:
        return MasteringEdit(str(self.mdcv_primaries.currentData()), self.mdcv_min.value(), float(self.mdcv_max.value()))

    def _changes(self) -> list[str]:
        changes: list[str] = []
        if self.static_box.isChecked():
            changes.append(translate_text("MaxCLL/MaxFALL {cll}/{fall}", cll=self._static_values[0],
                                          fall=self._static_values[1]))
        if self.mdcv_box.isChecked():
            changes.append(translate_text("MDCV {primaries} {min}–{max} nits", primaries=self.mdcv_primaries.currentText(),
                                          min=f"{self.mdcv_min.value():g}", max=self.mdcv_max.value()))
        snapshot = self._report.snapshot if self._report is not None else None
        if snapshot is not None and snapshot.dovi is not None:
            rpu_labels: tuple[tuple[QCheckBox, str], ...] = (
                (self.l6_box, "L6"), (self.l5_box, "L5"), (self.l11_box, "L11"),
                (self.source_box, translate_text("écran de mastering du RPU")), (self.cmv4_box, "CM v4.0"))
            changes += [label for box, label in rpu_labels if box.isChecked() and box.isEnabled()]
        if snapshot is not None and snapshot.hdr10plus:
            if self.h10_remove.isChecked():
                changes.append(translate_text("HDR10+ retiré"))
            else:
                hdr10plus_labels: tuple[tuple[QCheckBox, str], ...] = (
                    (self.h10_recompute, translate_text("HDR10+ recalculé")),
                    (self.h10_target_box, "TSDML"), (self.h10_bezier, translate_text("profil A")),
                    (self.h10_cap_box, translate_text("plafond HDR10+")))
                changes += [label for box, label in hdr10plus_labels if box.isChecked()]
        return changes

    def _sync(self, *_args: object) -> None:
        self._static_label.setText(translate_text("Valeurs choisies dans l'onglet Luminance : {cll} / {fall} nits",
                                                  cll=self._static_values[0], fall=self._static_values[1]))
        removing = self.h10_remove.isChecked()
        for widget in (self.h10_target_box, self.h10_target, self.h10_bezier, self.h10_cap_box, self.h10_cap):
            widget.setEnabled(not removing)
        # HDR10+ recalculé : mesure complète exigée (chaque image ; la mesure rapide n'en voit que les clés).
        complete = self._measurement is not None and not self._measurement.is_estimate
        self.h10_recompute.setEnabled(not removing and complete)
        if self.h10_recompute.isChecked() and self.h10_recompute.isEnabled():
            self.h10_bezier.setEnabled(False)  # profil A d'office
        self.h10_target.setEnabled(not removing and self.h10_target_box.isChecked() and not self.h10_bezier.isChecked())
        self.h10_cap.setEnabled(not removing and self.h10_cap_box.isChecked())
        dovi = self._report is not None and self._report.snapshot.dovi is not None
        self.mastering_sync.setVisible(dovi)
        self._update_mdcv_hint()
        groups: tuple[tuple[QCheckBox, tuple[QWidget, ...]], ...] = (
            (self.mdcv_box, (self.mdcv_primaries, self.mdcv_min, self.mdcv_max)),
            (self.l6_box, (self.l6_max_mastering, self.l6_min_mastering, self.l6_cll, self.l6_fall)),
            (self.l5_box, tuple(self.l5)), (self.l11_box, (self.l11_content, self.l11_whitepoint, self.l11_reference)),
            (self.source_box, (self.source_min, self.source_max)))
        for box, members in groups:
            for member in members:
                member.setEnabled(box.isChecked())
        chosen = float(self.mdcv_max.value()) if self.mdcv_box.isChecked() and self._report is not None else None
        if chosen != self._emitted_mastering:
            self._emitted_mastering = chosen
            self.mastering_changed.emit(chosen)
        changes = self._changes()
        invalid = ""
        if self.mdcv_box.isChecked():
            try:
                self._mastering_edit()
            except ValueError as exc:
                invalid = str(exc)
        output = self.output.text().strip()
        source = self._report.snapshot.path if self._report is not None else ""
        if invalid:
            summary = invalid
        elif not changes:
            summary = translate_text("Aucune modification cochée.")
        elif not output:
            summary = translate_text("Choisir le fichier de sortie.")
        elif Path(output) == Path(source):
            summary = translate_text("La sortie doit être un autre fichier que la source.")
        else:
            summary = translate_text("Modifications : {changes}.", changes=", ".join(changes))
        self._summary.setText(summary)
        ready = bool(changes and not invalid and output and Path(output) != Path(source)) and self._report is not None
        self._ready_plan = None
        if ready:
            try:
                plan = self.plan()
            except ValueError as exc:
                summary = str(exc)
                self._summary.setText(summary)
            else:
                self._ready_plan = plan if plan is not None and not plan.is_empty() else None
        self._reason = "" if self._ready_plan is not None else summary
        self.changed.emit()

    def readiness(self) -> tuple[PatchPlan | None, str]:
        """Plan prêt à écrire, sinon None et la raison affichée à l'utilisateur."""
        return self._ready_plan, self._reason

    def _update_mdcv_hint(self) -> None:
        report = self._report
        if report is None:
            self._mdcv_hint.setText("")
            return
        mastering = report.snapshot.mastering
        current = translate_text("absent") if mastering is None else translate_text(
            "{primaries}, {min} – {max} nits", primaries=mastering.primaries,
            min=f"{mastering.min_nits:g}" if mastering.min_nits is not None else "?",
            max=f"{mastering.max_nits:g}" if mastering.max_nits is not None else "?")
        text = translate_text("Actuel : {current}.", current=current)
        if self._suggested is not None:
            edit, source = self._suggested
            text += " " + translate_text("Proposé : {primaries}, {min} – {max} nits d'après {source}.",
                                         primaries=edit.primaries, min=f"{edit.min_nits:g}", max=f"{edit.max_nits:g}",
                                         source=translate_text(_MASTERING_SOURCES[source]))
        self._mdcv_hint.setText(text)


__all__ = ["EditionTab"]
