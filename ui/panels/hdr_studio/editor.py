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
from core.hdr_inspection.patch import Hdr10PlusEdit, PatchPlan, RpuEdit, StaticEdit
from core.i18n import translate_text
from ui.design_system import colors as C, font_px, scale
from ui.styles import _card, _checkbox_style, _combo_style, _input_style, _primary_button, _secondary_button, _section_label

_SPIN_STYLE = (
    "QSpinBox, QDoubleSpinBox{{background:{bg};color:{fg};border:1px solid {border};border-radius:5px;"
    "padding:{pad}px {padh}px;font-size:{size}px;}}"
)
_L11_LABELS = ("Défaut", "Cinéma", "Jeu", "Sport", "Contenu utilisateur")


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
    """Choix des corrections et de la sortie ; ``write_requested`` porte le plan complet."""

    write_requested = Signal(object)
    luminance_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setStyleSheet("background:transparent;")
        self._report: InspectionReport | None = None
        self._static_values = (0, 0)
        self._busy = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, scale(10), 0, 0)
        layout.setSpacing(scale(10))
        layout.addWidget(_label(translate_text(
            "Les corrections réécrivent le fichier sans réencoder l'image : flux vidéo extrait, métadonnées "
            "corrigées par dovi_tool et hdr10plus_tool, puis replacé dans une copie du MKV (autres pistes, "
            "chapitres et pièces jointes conservés). La source n'est jamais modifiée."), color=C.TEXT_DIM, size=10))
        layout.addWidget(self._build_static())
        self._dovi_card = self._build_dovi()
        layout.addWidget(self._dovi_card)
        self._hdr10plus_card = self._build_hdr10plus()
        layout.addWidget(self._hdr10plus_card)
        layout.addWidget(self._build_output())
        layout.addStretch()
        for box in self.findChildren(QCheckBox):
            box.toggled.connect(self._sync)
        self._sync()

    # ── Cartes ──────────────────────────────────────────────────────────
    def _card(self, title: str) -> tuple[QWidget, QVBoxLayout]:
        card = _card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(scale(14), scale(10), scale(14), scale(12))
        layout.setSpacing(scale(6))
        layout.addWidget(_section_label(title))
        return card, layout

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
        self.h10_target_box = _check(translate_text("Écran ciblé (TSDML)"))
        self.h10_target = _int_spin(10000, " nits")
        self.h10_bezier = _check(translate_text("Retirer la courbe de Bézier (profil A)"))
        self.h10_cap_box = _check(translate_text("Plafonner MaxScl, AverageRGB et percentiles"))
        self.h10_cap = _int_spin(10000, " nits")
        self.h10_cap.setMinimum(100)
        layout.addWidget(self.h10_remove)
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
        self.write_btn = _primary_button(translate_text("Écrire le fichier corrigé"))
        self.write_btn.clicked.connect(self._write)
        actions = QHBoxLayout()
        actions.addWidget(self.write_btn)
        actions.addStretch()
        layout.addLayout(actions)
        return card

    # ── Données ─────────────────────────────────────────────────────────
    def set_report(self, report: InspectionReport | None) -> None:
        """Préremplit avec les valeurs actuelles du fichier (RPU et HDR10+ si lus)."""
        previous = self._report.snapshot.path if self._report is not None else None
        self._report = report
        if report is None:
            self._sync()
            return
        snapshot = report.snapshot
        if snapshot.path != previous:
            path = Path(snapshot.path)
            self.output.setText(str(path.with_name(f"{path.stem}.hdrfix{path.suffix}")))
            for box in self.findChildren(QCheckBox):
                box.setChecked(False)
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
        self._dovi_card.setVisible(snapshot.dovi is not None)
        self._hdr10plus_card.setVisible(snapshot.hdr10plus)
        self._dovi_hint.setText(translate_text(
            "Lire les métadonnées dynamiques pour préremplir les valeurs actuelles du RPU."))
        self._dovi_hint.setVisible(dovi is None)
        self.cmv4_box.setEnabled(dovi is not None and dovi.cm_version == "4.0")
        self._sync()

    def set_static_values(self, max_cll: int, max_fall: int) -> None:
        self._static_values = (max_cll, max_fall)
        self._sync()

    def enable_static(self) -> None:
        self.static_box.setChecked(True)

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
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
        static = StaticEdit(*self._static_values) if self.static_box.isChecked() else None
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
        ) if report.snapshot.hdr10plus else None
        return PatchPlan(Path(report.snapshot.path), report.snapshot.stream_index, Path(self.output.text().strip()),
                         static=static, rpu=rpu, hdr10plus=hdr10plus)

    def _changes(self) -> list[str]:
        changes: list[str] = []
        if self.static_box.isChecked():
            changes.append(translate_text("MaxCLL/MaxFALL {cll}/{fall}", cll=self._static_values[0],
                                          fall=self._static_values[1]))
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
        self.h10_target.setEnabled(not removing and self.h10_target_box.isChecked() and not self.h10_bezier.isChecked())
        self.h10_cap.setEnabled(not removing and self.h10_cap_box.isChecked())
        groups: tuple[tuple[QCheckBox, tuple[QWidget, ...]], ...] = (
            (self.l6_box, (self.l6_max_mastering, self.l6_min_mastering, self.l6_cll, self.l6_fall)),
            (self.l5_box, tuple(self.l5)), (self.l11_box, (self.l11_content, self.l11_whitepoint, self.l11_reference)),
            (self.source_box, (self.source_min, self.source_max)))
        for box, members in groups:
            for member in members:
                member.setEnabled(box.isChecked())
        changes = self._changes()
        output = self.output.text().strip()
        source = self._report.snapshot.path if self._report is not None else ""
        if not changes:
            summary = translate_text("Aucune modification cochée.")
        elif not output:
            summary = translate_text("Choisir le fichier de sortie.")
        elif Path(output) == Path(source):
            summary = translate_text("La sortie doit être un autre fichier que la source.")
        else:
            summary = translate_text("Modifications : {changes}.", changes=", ".join(changes))
        self._summary.setText(summary)
        self.write_btn.setEnabled(bool(changes and output and Path(output) != Path(source))
                                  and self._report is not None and not self._busy)

    def _write(self) -> None:
        plan = self.plan()
        if plan is not None and not plan.is_empty():
            self.write_requested.emit(plan)


__all__ = ["EditionTab"]
