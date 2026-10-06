"""
ui/panels/merge_dovi_panel.py — Panneau d'injection DoVi RPU + HDR10+.

Architecture :
    MergeDoviPanel (QWidget)
    ├── _FilePairSection          — sélecteurs Film 1 / Film 2
    ├── _FrameCountBar            — comparaison frame counts
    ├── _ConfigSection            — profil DoVi, dossiers work/output
    ├── _StepProgressWidget       — barre de progression par étape
    └── _ResultSection            — résumé final + lien fichier de sortie

Signaux exposés :
    MergeDoviPanel.log_message(level: str, message: str)
        → peut être connecté à MainWindow.log_requested
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Protocol

from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFrame,
    QHBoxLayout, QLabel, QLayout, QLineEdit, QPushButton,
    QScrollArea, QVBoxLayout, QWidget,
)

from core.config import AppConfig
from core.matroska.progress import native_mux_progress_label
from core.file_types import build_qt_filter, is_accepted
from core.i18n import apply_translations, translate_text
from core.frame_count import reliable_frame_count
from core.workflows.encode.runtime.frame_count_guard import MetadataAdjustment
from core.workflows.merge_dovi import (
    DoviProfile, FrameCountResult, HdrState,
    MergeDoviWorkflow, StepResult, WorkflowStep, probe_hdr_state,
)
from ui.confirm import confirm_overwrite, ValidationOverridePrompt
from ui.shutdown import defer_close
from ui.desktop import open_external
from ui.design_system import colors as _C, font_px as _font_px, scale as _scale


class _StringSignal(Protocol):
    def emit(self, *args: Any) -> None: ...


# =============================================================================
# Helpers de style
# =============================================================================

def _section_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(f"""
        color: {_C.TEXT_DIM};
        font-size: {_font_px(9)}px;
        font-weight: 700;
        letter-spacing: {_scale(2)}px;
        background: transparent;
    """)
    return lbl


def _card(parent: QWidget | None = None) -> QWidget:
    w = QWidget(parent)
    w.setStyleSheet(f"""
        QWidget {{
            background: {_C.BG_CARD};
            border: 1px solid {_C.BORDER};
            border-radius: {_scale(6)}px;
        }}
    """)
    return w


def _primary_button(text: str) -> QPushButton:
    btn = QPushButton(text)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setFixedHeight(_scale(36))
    btn.setStyleSheet(f"""
        QPushButton {{
            background: {_C.ACCENT};
            color: #ffffff;
            border: none;
            border-radius: {_scale(6)}px;
            font-size: {_font_px(13)}px;
            font-weight: 700;
            padding: 0 {_scale(24)}px;
        }}
        QPushButton:hover {{
            background: #6070f8;
        }}
        QPushButton:pressed {{
            background: {_C.ACCENT_DIM};
        }}
        QPushButton:disabled {{
            background: {_C.BG_ACTIVE};
            color: {_C.TEXT_DIM};
        }}
    """)
    return btn


def _secondary_button(
    text: str,
    fixed_width: int | None = None,
    *,
    min_width: int | None = None,
    padding_h: int | None = None,
) -> QPushButton:
    btn = QPushButton(text)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setFixedHeight(_scale(30))

    is_symbol = len(text.strip()) <= 2
    if padding_h is not None:
        pad_px = _scale(padding_h)
    elif is_symbol:
        pad_px = _scale(2)
    elif fixed_width is not None and fixed_width <= 50:
        pad_px = _scale(4)
    else:
        pad_px = _scale(8)

    if fixed_width:
        btn.setFixedWidth(_scale(fixed_width))
    if min_width:
        btn.setMinimumWidth(_scale(min_width))

    btn.setStyleSheet(f"""
        QPushButton {{
            background: {_C.BG_ACTIVE};
            color: {_C.TEXT_SEC};
            border: 1px solid {_C.BORDER_LT};
            border-radius: {_scale(5)}px;
            font-size: {_font_px(11)}px;
            padding: 0 {pad_px}px;
            text-align: center;
        }}
        QPushButton:hover {{
            background: {_C.BG_HOVER};
            color: {_C.TEXT_PRI};
            border-color: {_C.ACCENT};
        }}
        QPushButton:disabled {{
            color: {_C.TEXT_DIM};
            border-color: {_C.BORDER};
        }}
    """)
    return btn


def _path_input() -> QLineEdit:
    le = QLineEdit()
    le.setReadOnly(True)
    le.setPlaceholderText("Aucun fichier sélectionné")
    le.setStyleSheet(f"""
        QLineEdit {{
            background: {_C.BG_DEEP};
            color: {_C.TEXT_SEC};
            border: 1px solid {_C.BORDER};
            border-radius: {_scale(4)}px;
            padding: {_scale(4)}px {_scale(8)}px;
            font-size: {_font_px(11)}px;
            font-family: 'JetBrains Mono', monospace;
        }}
        QLineEdit:focus {{
            border-color: {_C.ACCENT};
        }}
    """)
    return le


# =============================================================================
# Section sélection de fichiers
# =============================================================================

class _FilePairSection(QWidget):
    """
    Deux sélecteurs de fichier (Film 1 cible + Film 2 source) avec
    support drag-and-drop.

    Signaux :
        film1_changed(path: str)
        film2_changed(path: str)
    """

    film1_changed = Signal(str)
    film2_changed = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._film1: Path | None = None
        self._film2: Path | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(_scale(8))

        layout.addWidget(_section_label("FICHIERS SOURCE"))

        for label, role, attr, signal in [
            ("Film 1 — Cible (vidéo à enrichir)",
             "Ouvrir Film 1 (MKV / HEVC)…", "film1", self.film1_changed),
            ("Film 2 — Source (porteur DoVi / HDR10+)",
             "Ouvrir Film 2 (MKV / HEVC)…", "film2", self.film2_changed),
        ]:
            row = self._make_file_row(label, role, attr, signal)
            layout.addWidget(row)

    def _make_file_row(
        self,
        label: str,
        dialog_title: str,
        attr: str,
        signal: _StringSignal,
    ) -> QWidget:
        card = _card()
        card.setAcceptDrops(True)
        cl  = QVBoxLayout(card)
        cl.setContentsMargins(_scale(14), _scale(10), _scale(14), _scale(10))
        cl.setSpacing(_scale(6))

        lbl = QLabel(label)
        lbl.setStyleSheet(
            f"color: {_C.TEXT_SEC}; font-size: {_font_px(12)}px; background: transparent; border: none;"
        )
        cl.addWidget(lbl)

        row = QWidget()
        row.setStyleSheet("background: transparent; border: none;")
        rl  = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(_scale(8))

        path_le = _path_input()
        rl.addWidget(path_le, stretch=1)

        btn = _secondary_button("Parcourir…", 90)
        rl.addWidget(btn)
        cl.addWidget(row)

        # Badge info (codec, taille)
        badge = QLabel("")
        badge.setStyleSheet(f"""
            color: {_C.TEXT_DIM};
            font-size: {_font_px(10)}px;
            font-family: 'JetBrains Mono', monospace;
            background: transparent;
            border: none;
        """)
        cl.addWidget(badge)

        def on_path_selected(path_str: str) -> None:
            p = Path(path_str)
            setattr(self, f"_{attr}", p)
            path_le.setText(str(p))
            path_le.setStyleSheet(path_le.styleSheet().replace(
                f"color: {_C.TEXT_SEC}", f"color: {_C.TEXT_PRI}"
            ))
            size_mb = p.stat().st_size / (1024 ** 2) if p.is_file() else 0
            badge.setText(f"{p.suffix.upper()[1:]}  ·  {size_mb:.1f} Mo")
            badge.setStyleSheet(f"""
                color: {_C.TEXT_SEC};
                font-size: {_font_px(10)}px;
                font-family: 'JetBrains Mono', monospace;
                background: transparent;
                border: none;
            """)
            signal.emit(path_str)

        def open_dialog() -> None:
            path, _ = QFileDialog.getOpenFileName(
                self,
                translate_text(dialog_title),
                "",
                build_qt_filter(video_only=True),
            )
            if path:
                on_path_selected(path)

        btn.clicked.connect(open_dialog)

        # Drag & drop sur la card
        def dragEnterEvent(event) -> None:
            if event.mimeData().hasUrls():
                url = event.mimeData().urls()[0]
                if is_accepted(url.toLocalFile(), video_only=True):
                    event.acceptProposedAction()
                    card.setStyleSheet(f"""
                        QWidget {{
                            background: {_C.BG_HOVER};
                            border: 1px solid {_C.ACCENT};
                            border-radius: 6px;
                        }}
                    """)
                    return
            event.ignore()

        def dragLeaveEvent(event) -> None:
            card.setStyleSheet(f"""
                QWidget {{
                    background: {_C.BG_CARD};
                    border: 1px solid {_C.BORDER};
                    border-radius: 6px;
                }}
            """)

        def dropEvent(event) -> None:
            dragLeaveEvent(event)
            urls = event.mimeData().urls()
            if urls:
                on_path_selected(urls[0].toLocalFile())

        card.dragEnterEvent = dragEnterEvent  # type: ignore[method-assign]
        card.dragLeaveEvent = dragLeaveEvent  # type: ignore[method-assign]
        card.dropEvent      = dropEvent       # type: ignore[method-assign]

        return card

    @property
    def film1(self) -> Path | None:
        return self._film1

    @property
    def film2(self) -> Path | None:
        return self._film2


# =============================================================================
# Barre de comparaison des frame counts
# =============================================================================

class _FrameCountBar(QWidget):
    """Barre affichant la comparaison des frame counts en temps réel."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self) -> None:
        self.setFixedHeight(36)
        self.setStyleSheet(f"""
            QWidget {{
                background: {_C.BG_PANEL};
                border: 1px solid {_C.BORDER};
                border-radius: 5px;
            }}
        """)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 0, 14, 0)
        layout.setSpacing(16)

        prefix = QLabel("FRAMES")
        prefix.setStyleSheet(f"""
            color: {_C.TEXT_DIM};
            font-size: 9px;
            font-weight: 700;
            letter-spacing: 2px;
            background: transparent;
            border: none;
        """)
        layout.addWidget(prefix)

        self._lbl = QLabel("—")
        self._lbl.setStyleSheet(f"""
            color: {_C.TEXT_DIM};
            font-size: 11px;
            font-family: 'JetBrains Mono', monospace;
            background: transparent;
            border: none;
        """)
        layout.addWidget(self._lbl)
        layout.addStretch()

    def set_result(self, result: FrameCountResult) -> None:
        if result.fc1 is None or result.fc2 is None:
            # Valeur rapide invérifiable (flux brut, statistiques douteuses) :
            # le compte exact est établi au lancement, pas à la sélection.
            known = [f"Film {i} : {fc}" for i, fc in ((1, result.fc1), (2, result.fc2)) if fc is not None]
            self._lbl.setText(
                "  |  ".join(known + [translate_text("comptage exact au lancement")])
            )
            color = _C.TEXT_DIM
        else:
            self._lbl.setText(result.status_text)
            color = (
                _C.OK   if result.diff == 0 else
                _C.WARN if result.warning else
                _C.ERROR
            )
        self._lbl.setStyleSheet(f"""
            color: {color};
            font-size: 11px;
            font-family: 'JetBrains Mono', monospace;
            background: transparent;
            border: none;
        """)

    def reset(self) -> None:
        self._lbl.setText("—")
        self._lbl.setStyleSheet(f"""
            color: {_C.TEXT_DIM};
            font-size: 11px;
            font-family: 'JetBrains Mono', monospace;
            background: transparent;
            border: none;
        """)


# =============================================================================
# Section configuration
# =============================================================================

class _ConfigSection(QWidget):
    """Profil DoVi, dossiers de travail et de sortie."""

    def __init__(self, config: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self._work_input: QLineEdit
        self._output_input: QLineEdit
        self._hdr_states: tuple[HdrState, HdrState] | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        layout.addWidget(_section_label("CONFIGURATION"))

        card = _card()
        cl   = QVBoxLayout(card)
        cl.setContentsMargins(14, 12, 14, 12)
        cl.setSpacing(10)

        # Profil DoVi
        row1 = QWidget()
        row1.setStyleSheet("background: transparent; border: none;")
        r1l  = QHBoxLayout(row1)
        r1l.setContentsMargins(0, 0, 0, 0)
        r1l.setSpacing(12)

        lbl_profile = QLabel("Profil Dolby Vision :")
        lbl_profile.setFixedWidth(160)
        lbl_profile.setStyleSheet(f"color: {_C.TEXT_SEC}; font-size: 12px; background: transparent; border: none;")
        r1l.addWidget(lbl_profile)

        self._profile_combo = QComboBox()
        self._profile_combo.addItem(
            "Désactivé  —  n'injecte pas le DV de Film 2 (celui de Film 1 est conservé)",
            DoviProfile.DISABLED,
        )
        self._profile_combo.addItem("Profile 8.1  —  -m 2, standard remux UHD (recommandé)", DoviProfile.P8_1)
        self._profile_combo.addItem("Mode 0  —  rewrite untouched, préserve le profil source",  DoviProfile.P8_0)
        self._profile_combo.setCurrentIndex(1)
        self._profile_combo.setStyleSheet(f"""
            QComboBox {{
                background: {_C.BG_DEEP};
                color: {_C.TEXT_PRI};
                border: 1px solid {_C.BORDER};
                border-radius: 4px;
                padding: 4px 8px;
                font-size: 12px;
            }}
            QComboBox::drop-down {{
                border: none;
                width: 20px;
            }}
            QComboBox QAbstractItemView {{
                background: {_C.BG_PANEL};
                color: {_C.TEXT_PRI};
                border: 1px solid {_C.BORDER_LT};
                selection-background-color: {_C.BG_ACTIVE};
            }}
        """)
        r1l.addWidget(self._profile_combo, stretch=1)
        cl.addWidget(row1)

        # Politique explicite si Film 2 a quelques trames de plus que Film 1 :
        # décochée par défaut (comptages identiques exigés), non mémorisée.
        self._trim_tail_cb = QCheckBox(
            "Écart de 1 à 4 trames : retirer l'excédent de métadonnées en fin de flux"
        )
        self._trim_tail_cb.setToolTip(
            "Si Film 2 a 1 à 4 trames de plus que Film 1, les métadonnées DoVi / HDR10+ "
            "excédentaires sont retirées à la fin, en supposant le début des deux films aligné.\n"
            "Aucune métadonnée n'est jamais inventée : si Film 2 est plus court, l'injection est refusée.\n"
            "Un comptage identique ne prouve pas l'alignement temporel."
        )
        self._trim_tail_cb.setStyleSheet(
            f"color: {_C.TEXT_SEC}; font-size: 12px; background: transparent; border: none;"
        )
        cl.addWidget(self._trim_tail_cb)

        # État HDR des deux films + conséquence du profil choisi
        self._hdr_hint = QLabel()
        self._hdr_hint.setWordWrap(True)
        self._hdr_hint.setVisible(False)
        cl.addWidget(self._hdr_hint)
        self._profile_combo.currentIndexChanged.connect(lambda _: self._update_hdr_hint())

        # Dossier de travail + sortie
        for label, attr, default in [
            ("Dossier de travail :", "_work_input",   str(self._config.work_dir)),
            ("Dossier de sortie :", "_output_input", str(self._config.output_dir)),
        ]:
            row = QWidget()
            row.setStyleSheet("background: transparent; border: none;")
            rl  = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.setSpacing(8)

            lbl = QLabel(label)
            lbl.setFixedWidth(160)
            lbl.setStyleSheet(f"color: {_C.TEXT_SEC}; font-size: 12px; background: transparent; border: none;")
            rl.addWidget(lbl)

            le = QLineEdit(default)
            le.setStyleSheet(f"""
                QLineEdit {{
                    background: {_C.BG_DEEP};
                    color: {_C.TEXT_SEC};
                    border: 1px solid {_C.BORDER};
                    border-radius: 4px;
                    padding: 4px 8px;
                    font-size: 11px;
                    font-family: 'JetBrains Mono', monospace;
                }}
                QLineEdit:focus {{
                    border-color: {_C.ACCENT};
                    color: {_C.TEXT_PRI};
                }}
            """)
            setattr(self, attr, le)
            rl.addWidget(le, stretch=1)

            btn = _secondary_button("…", 32)
            def open_dir(le=le) -> None:
                d = QFileDialog.getExistingDirectory(
                    self,
                    translate_text("Sélectionner un dossier"),
                    le.text(),
                )
                if d:
                    le.setText(d)
            btn.clicked.connect(open_dir)
            rl.addWidget(btn)
            cl.addWidget(row)

        layout.addWidget(card)

    @property
    def dovi_profile(self) -> DoviProfile:
        return self._profile_combo.currentData()

    @property
    def metadata_adjustment(self) -> MetadataAdjustment:
        """Politique choisie explicitement par l'utilisateur pour un écart de trames."""
        return MetadataAdjustment.TRIM_TAIL if self._trim_tail_cb.isChecked() else MetadataAdjustment.EXACT

    def set_hdr_states(self, film1: HdrState | None, film2: HdrState | None) -> None:
        """Mémorise l'état HDR des deux films (None = inconnu) et met à jour l'indication."""
        self._hdr_states = (film1, film2) if film1 is not None and film2 is not None else None
        self._update_hdr_hint()

    def _update_hdr_hint(self) -> None:
        if self._hdr_states is None:
            self._hdr_hint.setVisible(False)
            return
        film1, film2 = self._hdr_states
        lines = [translate_text(
            "Film 1 : {film1}  ·  Film 2 : {film2}", film1=film1.label, film2=film2.label,
        )]
        warn = False
        if film1.has_dovi:
            if film2.has_dovi and self.dovi_profile != DoviProfile.DISABLED:
                warn = True
                lines.append(translate_text(
                    "Le RPU Dolby Vision de Film 1 sera remplacé par celui de Film 2. "
                    "Choisir « Désactivé » pour n'injecter que le HDR10+."
                ))
            else:
                lines.append(translate_text("Le Dolby Vision de Film 1 sera conservé."))
        if film1.has_hdr10plus and film2.has_hdr10plus:
            warn = True
            lines.append(translate_text("Le HDR10+ de Film 1 sera remplacé par celui de Film 2."))
        self._hdr_hint.setText("\n".join(lines))
        self._hdr_hint.setStyleSheet(
            f"color: {_C.WARN if warn else _C.TEXT_SEC}; font-size: 11px; "
            "background: transparent; border: none;"
        )
        self._hdr_hint.setVisible(True)

    @property
    def work_dir(self) -> Path:
        return Path(self._work_input.text())

    @property
    def output_dir(self) -> Path:
        return Path(self._output_input.text())


# =============================================================================
# Indicateur d'étape
# =============================================================================

_STEP_LABELS: dict[WorkflowStep, str] = {
    WorkflowStep.VALIDATION:        "Validation",
    WorkflowStep.DETECT_DOVI:       "Détection profil DV",
    WorkflowStep.FRAME_COUNT:       "Frame count",
    WorkflowStep.STORAGE_CHECK:     "Espace disque",
    WorkflowStep.EXTRACT_PARALLEL:  "Extractions",
    WorkflowStep.SDR_TO_HDR10:      "Conversion SDR → HDR10",
    WorkflowStep.CONVERT_DOVI:      "Conversion P7/P5 → P8.1",
    WorkflowStep.CHECK_METADATA:    "Contrôle métadonnées",
    WorkflowStep.INJECT_DOVI:       "Injection DoVi",
    WorkflowStep.INJECT_HDR10PLUS:  "Injection HDR10+",
    WorkflowStep.INJECT_STATIC_HDR: "Injection HDR10 statique",
    WorkflowStep.VERIFY:            "Vérification",
    WorkflowStep.REMUX:             "Remuxage",
    WorkflowStep.CLEANUP:           "Nettoyage",
}

_STEP_ORDER = list(WorkflowStep)


class _StepIndicator(QWidget):
    """Un indicateur visuel pour une seule étape."""

    def __init__(self, step: WorkflowStep, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._step = step
        self._build_ui()
        self.set_idle()

    def _build_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(10)

        self._icon = QLabel("○")
        self._icon.setFixedWidth(14)
        self._icon.setStyleSheet(f"color: {_C.TEXT_DIM}; font-size: 12px; background: transparent; border: none;")
        layout.addWidget(self._icon)

        self._name = QLabel(_STEP_LABELS[self._step])
        self._name.setFixedWidth(140)
        self._name.setStyleSheet(f"color: {_C.TEXT_DIM}; font-size: 12px; background: transparent; border: none;")
        layout.addWidget(self._name)

        self._detail = QLabel("")
        self._detail.setStyleSheet(f"""
            color: {_C.TEXT_DIM};
            font-size: 11px;
            font-family: 'JetBrains Mono', monospace;
            background: transparent;
            border: none;
        """)
        layout.addWidget(self._detail, stretch=1)

        self._duration = QLabel("")
        self._duration.setStyleSheet(f"color: {_C.TEXT_DIM}; font-size: 10px; background: transparent; border: none;")
        layout.addWidget(self._duration)

    def set_idle(self) -> None:
        self._icon.setText("○")
        self._icon.setStyleSheet(f"color: {_C.TEXT_DIM}; font-size: 12px; background: transparent; border: none;")
        self._name.setStyleSheet(f"color: {_C.TEXT_DIM}; font-size: 12px; background: transparent; border: none;")
        self._detail.setText("")
        self._duration.setText("")

    def set_running(self) -> None:
        self._icon.setText("◉")
        self._icon.setStyleSheet(f"color: {_C.ACCENT}; font-size: 12px; background: transparent; border: none;")
        self._name.setStyleSheet(f"color: {_C.TEXT_PRI}; font-size: 12px; font-weight: 600; background: transparent; border: none;")
        self._detail.setText(translate_text("en cours…"))
        self._detail.setStyleSheet(f"color: {_C.ACCENT}; font-size: 11px; font-family: 'JetBrains Mono', monospace; background: transparent; border: none;")

    def set_progress(self, message: str) -> None:
        self._detail.setText(message[-80:])  # Tronquer les messages longs
        self._detail.setStyleSheet(f"color: {_C.TEXT_SEC}; font-size: 11px; font-family: 'JetBrains Mono', monospace; background: transparent; border: none;")

    def set_ok(self, result: StepResult) -> None:
        self._icon.setText("✓")
        self._icon.setStyleSheet(f"color: {_C.OK}; font-size: 12px; background: transparent; border: none;")
        self._name.setStyleSheet(f"color: {_C.TEXT_PRI}; font-size: 12px; background: transparent; border: none;")
        self._detail.setText(result.message)
        self._detail.setStyleSheet(f"color: {_C.TEXT_SEC}; font-size: 11px; font-family: 'JetBrains Mono', monospace; background: transparent; border: none;")
        self._duration.setText(f"{result.duration:.1f}s")

    def set_error(self, message: str) -> None:
        self._icon.setText("✗")
        self._icon.setStyleSheet(f"color: {_C.ERROR}; font-size: 12px; background: transparent; border: none;")
        self._name.setStyleSheet(f"color: {_C.ERROR}; font-size: 12px; font-weight: 600; background: transparent; border: none;")
        self._detail.setText(message[:80])
        self._detail.setStyleSheet(f"color: {_C.ERROR}; font-size: 11px; font-family: 'JetBrains Mono', monospace; background: transparent; border: none;")


class _StepProgressWidget(QWidget):
    """Affiche une ligne par étape du workflow avec son état courant."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._indicators: dict[WorkflowStep, _StepIndicator] = {}
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(_section_label("PROGRESSION"))

        card = _card()
        cl   = QVBoxLayout(card)
        cl.setContentsMargins(0, 4, 0, 4)
        cl.setSpacing(0)

        for i, step in enumerate(_STEP_ORDER):
            ind = _StepIndicator(step)
            self._indicators[step] = ind

            if i > 0:
                sep = QFrame()
                sep.setFrameShape(QFrame.Shape.HLine)
                sep.setStyleSheet(f"color: {_C.BORDER}; background: {_C.BORDER}; border: none;")
                sep.setFixedHeight(1)
                cl.addWidget(sep)

            cl.addWidget(ind)

        layout.addWidget(card)

    def reset(self) -> None:
        for ind in self._indicators.values():
            ind.set_idle()

    def set_running(self, step: WorkflowStep) -> None:
        self._indicators[step].set_running()

    def set_progress(self, step: WorkflowStep, message: str) -> None:
        self._indicators[step].set_progress(message)

    def set_ok(self, step: WorkflowStep, result: StepResult) -> None:
        self._indicators[step].set_ok(result)

    def set_error(self, step: WorkflowStep, message: str) -> None:
        self._indicators[step].set_error(message)


# =============================================================================
# Section résultat final
# =============================================================================

class _ResultSection(QWidget):
    """Résumé final avec lien vers le fichier de sortie."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._build_ui()
        self.setVisible(False)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        card = _card()
        card.setStyleSheet("""
            QWidget {
                background: #0f2318;
                border: 1px solid #1a4a2e;
                border-radius: 6px;
            }
        """)
        cl = QVBoxLayout(card)
        cl.setContentsMargins(16, 14, 16, 14)
        cl.setSpacing(8)

        header = QHBoxLayout()
        icon = QLabel("✓")
        icon.setStyleSheet(f"color: {_C.OK}; font-size: 20px; background: transparent; border: none;")
        header.addWidget(icon)

        title = QLabel("Workflow terminé avec succès")
        title.setStyleSheet(f"color: {_C.OK}; font-size: 14px; font-weight: 700; background: transparent; border: none;")
        header.addWidget(title)
        header.addStretch()
        cl.addLayout(header)

        self._path_lbl = QLabel("")
        self._path_lbl.setStyleSheet(f"""
            color: {_C.TEXT_SEC};
            font-size: 11px;
            font-family: 'JetBrains Mono', monospace;
            background: transparent;
            border: none;
        """)
        self._path_lbl.setWordWrap(True)
        cl.addWidget(self._path_lbl)

        btns = QHBoxLayout()
        self._open_btn  = _secondary_button("⬜  Ouvrir le dossier")
        self._copy_btn  = _secondary_button("⎘  Copier le chemin")
        btns.addWidget(self._open_btn)
        btns.addWidget(self._copy_btn)
        btns.addStretch()
        cl.addLayout(btns)

        layout.addWidget(card)
        self._output_path: Path | None = None

        self._open_btn.clicked.connect(self._open_folder)
        self._copy_btn.clicked.connect(self._copy_path)

    def show_result(self, output_path: str) -> None:
        self._output_path = Path(output_path)
        self._path_lbl.setText(str(self._output_path))
        self.setVisible(True)

    def hide_result(self) -> None:
        self.setVisible(False)
        self._output_path = None

    def _open_folder(self) -> None:
        if self._output_path:
            from PySide6.QtCore import QUrl
            open_external(QUrl.fromLocalFile(str(self._output_path.parent)))

    def _copy_path(self) -> None:
        if self._output_path:
            QApplication.clipboard().setText(str(self._output_path))


# =============================================================================
# Section résultat erreur
# =============================================================================

class _ErrorSection(QWidget):
    """Affiche l'erreur en cas d'échec du workflow."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._build_ui()
        self.setVisible(False)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        card = _card()
        card.setStyleSheet("""
            QWidget {
                background: #1f0e0e;
                border: 1px solid #3a1515;
                border-radius: 6px;
            }
        """)
        cl = QVBoxLayout(card)
        cl.setContentsMargins(16, 12, 16, 12)
        cl.setSpacing(6)

        header = QHBoxLayout()
        icon = QLabel("✗")
        icon.setStyleSheet(f"color: {_C.ERROR}; font-size: 18px; background: transparent; border: none;")
        header.addWidget(icon)
        title = QLabel("Workflow échoué")
        title.setStyleSheet(f"color: {_C.ERROR}; font-size: 13px; font-weight: 700; background: transparent; border: none;")
        header.addWidget(title)
        header.addStretch()
        cl.addLayout(header)

        self._msg_lbl = QLabel("")
        self._msg_lbl.setWordWrap(True)
        self._msg_lbl.setStyleSheet("""
            color: #f09090;
            font-size: 11px;
            font-family: 'JetBrains Mono', monospace;
            background: transparent;
            border: none;
        """)
        cl.addWidget(self._msg_lbl)
        layout.addWidget(card)

    def show_error(self, message: str) -> None:
        self._msg_lbl.setText(message)
        self.setVisible(True)

    def hide_error(self) -> None:
        self.setVisible(False)


# =============================================================================
# Panneau principal
# =============================================================================

class MergeDoviPanel(QWidget):
    """
    Panneau complet d'injection DoVi RPU + HDR10+.

    Signal :
        log_message(level: str, message: str)
            Peut être connecté à MainWindow.log_requested pour poster
            des messages dans le LogPanel global.
    """

    log_message = Signal(str, str)   # (level, message)
    # Pilote la barre de progression globale (MainWindow) :
    #   state ∈ {"started", "step", "finished", "failed", "cancelled"}
    #   label = libellé court à afficher (étape en cours, message d'état…)
    op_state_changed = Signal(str, str)
    # Pourcentage 0..100 pour la barre globale (revient à 0 à chaque step).
    op_progress_pct = Signal(int)
    op_progress = Signal(str)

    def __init__(self, config: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config    = config
        self._workflow: MergeDoviWorkflow | None = None
        self._running   = False
        self._build_ui()
        apply_translations(self)
        self._init_workflow()

    # ------------------------------------------------------------------
    # Construction UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self.setStyleSheet(f"background: {_C.BG_DEEP};")

        # Scroll area pour le contenu
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet(f"""
            QScrollArea {{ background: {_C.BG_DEEP}; border: none; }}
            QScrollBar:vertical {{
                background: {_C.BG_DEEP}; width: {_scale(6)}px; border: none;
            }}
            QScrollBar::handle:vertical {{
                background: {_C.BORDER_LT}; border-radius: {_scale(3)}px; min-height: {_scale(20)}px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
        """)

        content = QWidget()
        content.setStyleSheet(f"background: {_C.BG_DEEP};")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(_scale(24), _scale(24), _scale(24), _scale(24))
        content_layout.setSpacing(_scale(16))
        content_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        content_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinAndMaxSize)

        # Titre
        title = QLabel("Injection DoVi RPU + HDR10+")
        title.setStyleSheet(f"""
            font-size: {_font_px(22)}px;
            font-weight: 800;
            color: {_C.TEXT_PRI};
            background: transparent;
            letter-spacing: -{_scale(1)}px;
        """)
        content_layout.addWidget(title)

        subtitle = QLabel(
            "Transfère les métadonnées Dolby Vision et/ou HDR10+ "
            "de Film 2 (source) vers Film 1 (cible). Profil « Désactivé » : "
            "HDR10+ seul, le Dolby Vision de Film 1 est conservé."
        )
        subtitle.setStyleSheet(f"color: {_C.TEXT_SEC}; font-size: {_font_px(12)}px; background: transparent;")
        content_layout.addWidget(subtitle)

        # Séparateur
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {_C.BORDER};")
        content_layout.addWidget(sep)

        # Sections
        self._file_section    = _FilePairSection()
        content_layout.addWidget(self._file_section)

        self._framecount_bar  = _FrameCountBar()
        content_layout.addWidget(self._framecount_bar)

        self._config_section  = _ConfigSection(self._config)
        content_layout.addWidget(self._config_section)

        self._step_progress   = _StepProgressWidget()
        content_layout.addWidget(self._step_progress)

        self._result_section  = _ResultSection()
        content_layout.addWidget(self._result_section)

        self._error_section   = _ErrorSection()
        content_layout.addWidget(self._error_section)

        # Boutons d'action
        btn_row = QWidget()
        btn_row.setStyleSheet("background: transparent;")
        bl = QHBoxLayout(btn_row)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(_scale(10))

        self._run_btn    = _primary_button("▶  Lancer le workflow")
        self._cancel_btn = _secondary_button("⏹  Annuler")
        self._cancel_btn.setEnabled(False)

        bl.addWidget(self._run_btn)
        bl.addWidget(self._cancel_btn)
        bl.addStretch()
        content_layout.addWidget(btn_row)

        content_layout.addStretch()

        scroll.setWidget(content)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(scroll)

        # Connexions
        self._run_btn.clicked.connect(self._on_run)
        self._cancel_btn.clicked.connect(self._on_cancel)
        self._file_section.film1_changed.connect(self._on_files_changed)
        self._file_section.film2_changed.connect(self._on_files_changed)

    # ------------------------------------------------------------------
    # Workflow
    # ------------------------------------------------------------------

    def _init_workflow(self) -> None:
        self._validation_prompt = ValidationOverridePrompt(self)
        self._workflow = MergeDoviWorkflow(
            mediainfo_bin    = self._config.tool_mediainfo,
            ffmpeg_bin       = self._config.tool_ffmpeg,
            ffprobe_bin      = self._config.tool_ffprobe,
            dovi_tool_bin    = self._config.tool_dovi_tool,
            hdr10plus_bin    = self._config.tool_hdr10plus,
            max_workers      = 4,
        )
        wf = self._workflow
        wf.set_validation_override(self._validation_prompt.request)
        wf.step_started.connect(self._on_step_started,   Qt.ConnectionType.QueuedConnection)
        wf.step_progress.connect(self._on_step_progress, Qt.ConnectionType.QueuedConnection)
        wf.step_progress_pct.connect(self._on_step_progress_pct, Qt.ConnectionType.QueuedConnection)
        wf.step_finished.connect(self._on_step_finished, Qt.ConnectionType.QueuedConnection)
        wf.workflow_finished.connect(self._on_workflow_finished, Qt.ConnectionType.QueuedConnection)
        wf.workflow_failed.connect(self._on_workflow_failed,     Qt.ConnectionType.QueuedConnection)
        wf.workflow_cancelled.connect(self._on_workflow_cancelled, Qt.ConnectionType.QueuedConnection)

    def _on_run(self) -> None:
        if self._running:
            return
        guard = getattr(self, "_operation_start_guard", None)
        if guard is not None and not guard():
            return
        film1 = self._file_section.film1
        film2 = self._file_section.film2

        if not film1 or not film2:
            self._error_section.show_error(translate_text("Sélectionnez Film 1 et Film 2 avant de lancer."))
            self._result_section.hide_result()
            return

        output_path = MergeDoviWorkflow.output_path_for(film1, self._config_section.output_dir)
        if not confirm_overwrite(self, output_path):
            return

        self._result_section.hide_result()
        self._error_section.hide_error()
        self._step_progress.reset()
        self._running = True
        self._run_btn.setEnabled(False)
        self._cancel_btn.setEnabled(True)

        self.log_message.emit(
            "INFO",
            translate_text(
                "Démarrage workflow DoVi : {film1} ← {film2}",
                film1=film1.name,
                film2=film2.name,
            ),
        )
        self.op_state_changed.emit(
            "started",
            translate_text("Injection DoVi/HDR10+ en cours…"),
        )

        assert self._workflow is not None
        try:
            self._workflow.start(
                film1        = film1,
                film2        = film2,
                work_dir     = self._config_section.work_dir,
                output_dir   = self._config_section.output_dir,
                dovi_profile = self._config_section.dovi_profile,
                metadata_adjustment = self._config_section.metadata_adjustment,
            )
        except OSError as exc:
            # Dossier de travail non créable (droits, disque) : rien n'a démarré.
            self._on_workflow_failed(
                WorkflowStep.VALIDATION,
                translate_text("Dossier de travail inutilisable : {error}", error=str(exc)),
            )

    def closeEvent(self, event) -> None:
        if not getattr(self, "_closing", False):
            self._closing = True
            self.setEnabled(False)
            if self._workflow is not None:
                self._workflow.cancel()
        if defer_close(self, event, ready=not self._running):
            return
        super().closeEvent(event)

    def _on_cancel(self) -> None:
        """
        Demande l'annulation. Le workflow arrête l'étape en cours, nettoie
        puis émet workflow_cancelled — c'est ce signal qui remet l'UI en état
        idle. On ne touche pas l'état ici pour éviter que le bouton Run soit
        réactivé pendant que le thread tourne.
        """
        if self._workflow:
            self._workflow.cancel()
        self._cancel_btn.setEnabled(False)
        self.log_message.emit("WARN", translate_text("Annulation demandée — en attente de fin d'étape…"))

    # ------------------------------------------------------------------
    # Slots workflow (thread Qt principal via QueuedConnection)
    # ------------------------------------------------------------------

    def _on_step_started(self, step: WorkflowStep) -> None:
        self._step_progress.set_running(step)
        step_label = translate_text(_STEP_LABELS[step])
        self.log_message.emit("INFO", translate_text("[{step}] Démarrage…", step=step_label))
        self.op_state_changed.emit("step", step_label)

    def _on_step_progress_pct(self, _step: WorkflowStep, pct: int) -> None:
        # Lignes XX% des outils (dovi_tool / hdr10plus_tool) → barre globale.
        # Seules les étapes dovi_tool/hdr10plus_tool atteignent ce slot ; les
        # autres restent sur la prep jaune indéterminée.
        self.op_progress_pct.emit(pct)

    def _on_step_progress(self, step: WorkflowStep, message: str) -> None:
        native_label = native_mux_progress_label(message)
        if native_label is not None:
            self._step_progress.set_progress(step, native_label)
            self.op_progress.emit(message)
            return
        step_label = translate_text(_STEP_LABELS[step])
        local_message = translate_text(message)
        self._step_progress.set_progress(step, local_message)
        self.log_message.emit(
            "INFO",
            translate_text("[{step}] {message}", step=step_label, message=local_message),
        )

    def _on_step_finished(self, step: WorkflowStep, result: StepResult) -> None:
        step_label = translate_text(_STEP_LABELS[step])
        local_message = translate_text(result.message)
        local_result = StepResult(
            step=result.step,
            success=result.success,
            message=local_message,
            duration=result.duration,
            detail=result.detail,
        )
        self._step_progress.set_ok(step, local_result)
        self.log_message.emit(
            "OK",
            translate_text(
                "[{step}] {message}  ({duration}s)",
                step=step_label,
                message=local_message,
                duration=f"{result.duration:.1f}",
            ),
        )

    def _on_workflow_finished(self, output_path: str) -> None:
        self._result_section.show_result(output_path)
        self._error_section.hide_error()
        self._set_idle_state()
        self.log_message.emit("OK", translate_text("Fichier de sortie : {path}", path=output_path))
        self.op_state_changed.emit("finished", translate_text("Terminé."))

    def _on_workflow_cancelled(self) -> None:
        self._error_section.hide_error()
        self.log_message.emit("WARN", translate_text("Workflow annulé par l'utilisateur."))
        self.op_state_changed.emit("cancelled", translate_text("Annulé."))
        self._set_idle_state()

    def _on_workflow_failed(self, step: WorkflowStep, message: str) -> None:
        step_label = translate_text(_STEP_LABELS[step])
        local_message = translate_text(message)
        self._step_progress.set_error(step, local_message)
        self._error_section.show_error(f"[{step_label}] {local_message}")
        self.log_message.emit(
            "ERROR",
            translate_text(
                "Workflow échoué à l'étape {step} : {message}",
                step=step_label,
                message=local_message,
            ),
        )
        self.op_state_changed.emit("failed", translate_text("Échec."))
        self._set_idle_state()

    # ------------------------------------------------------------------
    # Utilitaires
    # ------------------------------------------------------------------

    def set_operation_start_guard(self, guard) -> None:
        self._operation_start_guard = guard

    def _set_idle_state(self) -> None:
        self._running = False
        self._run_btn.setEnabled(True)
        self._cancel_btn.setEnabled(False)

    def _on_files_changed(self, _: str) -> None:
        """Réinitialise l'affichage dès que les fichiers changent."""
        self._result_section.hide_result()
        self._error_section.hide_error()
        self._step_progress.reset()
        self._framecount_bar.reset()
        self._config_section.set_hdr_states(None, None)

        # Déclenche la lecture des frame counts après un court délai
        # (évite plusieurs appels rapides lors d'un drag-and-drop)
        QTimer.singleShot(300, self._refresh_framecounts)

    def _refresh_framecounts(self) -> None:
        """
        Lit les frame counts et l'état HDR des deux fichiers dans un thread
        secondaire et met à jour l'UI via QTimer.singleShot (thread-safe).
        """
        film1 = self._file_section.film1
        film2 = self._file_section.film2
        if not film1 or not film2:
            return
        if not film1.is_file() or not film2.is_file():
            return

        mediainfo_bin = self._config.tool_mediainfo
        ffprobe_bin = self._config.tool_ffprobe

        def _read() -> tuple[FrameCountResult, HdrState, HdrState]:
            def fc(path: Path) -> int | None:
                # Affichage seulement : jamais de lecture complète du fichier
                # (flux brut de plusieurs dizaines de Go) ; compte exact au lancement.
                return reliable_frame_count(
                    path, mediainfo_bin=mediainfo_bin, ffprobe_bin=ffprobe_bin, full_scan=False,
                )

            fc1  = fc(film1)
            fc2  = fc(film2)
            diff = abs(fc2 - fc1) if fc1 is not None and fc2 is not None else None
            hdr1 = probe_hdr_state(film1, mediainfo_bin)
            hdr2 = probe_hdr_state(film2, mediainfo_bin)
            return FrameCountResult(fc1, fc2, diff), hdr1, hdr2

        def _done(future) -> None:
            try:
                result = future.result()
                # QTimer.singleShot est thread-safe : repasse dans le thread Qt
                QTimer.singleShot(0, lambda r=result: self._apply_probe(*r))
            except Exception:
                pass

        executor = ThreadPoolExecutor(max_workers=1)
        f = executor.submit(_read)
        f.add_done_callback(_done)
        executor.shutdown(wait=False)

    def _apply_probe(self, result: FrameCountResult, hdr1: HdrState, hdr2: HdrState) -> None:
        self._framecount_bar.set_result(result)
        self._config_section.set_hdr_states(hdr1, hdr2)
