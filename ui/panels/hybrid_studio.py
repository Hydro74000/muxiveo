"""Studio d'hybridation : appariement par lot, calibration acoustique et synchronisation physique."""
from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
from dataclasses import replace
from io import StringIO
from pathlib import Path
import tempfile
import threading

from typing import Any, Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.i18n import translate_text
from core.workflows.sync_calibration import SyncCalibration
from core.workflows.remux_models import SourceInput, TrackEntry, tracks_from_file_info
from ui.widgets.content_table import ContentTableWidget
from ui.widgets.output_template_edit import OutputTemplateLineEdit, OutputTemplatePreview
from ui.widgets.surface import CollapsibleCard
from ui.panels.tmdb_search_modal import TmdbSearchModal
from core.workflows.hybrid_matrix import (
    HybridMatrix,
    HybridRecipe,
    MatchingMode,
    MatrixEpisode,
    MatrixSource,
    SourceRole,
    _language_selected,
    default_matrix_output_name,
    matrix_output_context,
    parse_episode_key,
    prepare_matrix_episode,
    witness_track_choices,
)
from core.workflows.preparation_cache import PreparationCache
from ui.panels.remux_panel.models import _TRACK_INFO_DISABLED_LABEL_ROLE
from ui.panels.remux_panel.widgets.track_table import _TrackInfoDelegate
from ui.shutdown import Shutdown, defer_close
from ui.operation_bar import OperationState
from ui.design_system import colors as _C, font_px as _font_px, scale as _scale
from ui.styles import _bolt_icon, _checkbox_style, _groupbox_checkable_style


# =============================================================================
# Helpers de style Design System
# =============================================================================

def _section_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(f"""
        color: {_C.TEXT_SEC};
        font-size: {_font_px(9)}px;
        font-weight: 700;
        letter-spacing: {_scale(2)}px;
        background: transparent;
    """)
    return lbl


def _card(parent: QWidget | None = None) -> QWidget:
    from ui.styles import _card as surface_card
    return surface_card(parent)


def _primary_button(text: str) -> QPushButton:
    btn = QPushButton(text)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setFixedHeight(_scale(32))
    btn.setStyleSheet(f"""
        QPushButton {{
            background: {_C.ACCENT};
            color: {_C.ON_ACCENT};
            border: none;
            border-radius: {_scale(5)}px;
            font-size: {_font_px(11)}px;
            font-weight: 700;
            padding: 0 {_scale(16)}px;
        }}
        QPushButton:hover {{
            background: {_C.ACCENT_HOVER};
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


def _input_style() -> str:
    return f"""
        QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
            background: {_C.BG_CARD};
            color: {_C.TEXT_PRI};
            border: 1px solid {_C.BORDER};
            border-radius: {_scale(5)}px;
            padding: {_scale(4)}px {_scale(8)}px;
            font-size: {_font_px(11)}px;
            min-height: {_scale(22)}px;
        }}
        QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
            border-color: {_C.ACCENT};
        }}
        QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {{
            color: {_C.TEXT_DIM};
            background: {_C.BG_CARD};
        }}
    """


def _table_style() -> str:
    return f"""
        QTableWidget {{
            background: {_C.BG_CARD};
            color: {_C.TEXT_PRI};
            border: 1px solid {_C.BORDER};
            border-radius: {_scale(5)}px;
            gridline-color: {_C.BORDER};
            font-size: {_font_px(11)}px;
            selection-background-color: {_C.BG_ACTIVE};
            selection-color: {_C.TEXT_PRI};
        }}
        QHeaderView::section {{
            background: {_C.BG_CARD};
            color: {_C.TEXT_SEC};
            border: none;
            border-bottom: 1px solid {_C.BORDER};
            padding: {_scale(4)}px {_scale(8)}px;
            font-size: {_font_px(10)}px;
            font-weight: 700;
        }}
    """


# =============================================================================
# Widgets spécialisés
# =============================================================================

class DirectoryEdit(QLineEdit):
    def __init__(self, placeholder: str = ""):
        super().__init__()
        self.setAcceptDrops(True)
        if placeholder:
            self.setPlaceholderText(placeholder)
        self.setStyleSheet(_input_style())

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if len(urls) == 1:
            local = urls[0].toLocalFile()
            p = Path(local)
            if p.is_dir() or p.is_file():
                self.setText(str(p))
                event.acceptProposedAction()



class ProfileSelector(QWidget):
    """Sélecteur de profil décisionnel ou de job exact pour l'hybridation par lot."""

    def __init__(self, profiles_dir: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.profiles_dir = Path(profiles_dir)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(_scale(6))

        self.combo = QComboBox()
        self.combo.setStyleSheet(_input_style())
        layout.addWidget(self.combo, stretch=1)

        self.browse_btn = _secondary_button("…", fixed_width=32)
        self.browse_btn.setToolTip(translate_text("Parcourir un fichier profil ou exact job JSON"))
        self.browse_btn.clicked.connect(self._browse)
        layout.addWidget(self.browse_btn)

        self._custom_path: str = ""
        self.reload_profiles()

    def reload_profiles(self) -> None:
        current_data = self.combo.currentData() or self._custom_path
        self.combo.clear()
        self.combo.addItem(translate_text("[Aucun profil - Règles par défaut]"), "")
        try:
            from core.profiles.decision import DecisionProfileManager
            mgr = DecisionProfileManager(self.profiles_dir / "decision")
            for p in mgr.load_all():
                name = str(p.get("name", "")).strip()
                if name:
                    self.combo.addItem(translate_text("Profil : {name}", name=name), name)
        except Exception:  # nosec B110
            pass

        if self._custom_path:
            label = translate_text("Fichier : {name}", name=Path(self._custom_path).name)
            self.combo.addItem(label, self._custom_path)

        if current_data:
            self.setText(str(current_data))

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            translate_text("Choisir un profil ou exact job JSON"),
            str(self.profiles_dir),
            "JSON (*.json);;Tous les fichiers (*)",
        )
        if path:
            self._custom_path = path
            self.reload_profiles()
            self.setText(path)

    def text(self) -> str:
        data = self.combo.currentData()
        if data:
            return str(data)
        txt = self.combo.currentText().strip()
        if txt.startswith("["):
            return ""
        return txt

    def setText(self, text: str) -> None:
        target = str(text or "").strip()
        if not target:
            self.combo.setCurrentIndex(0)
            return
        for i in range(self.combo.count()):
            data = self.combo.itemData(i)
            if data == target or self.combo.itemText(i) == target or translate_text("Profil : {name}", name=target) == self.combo.itemText(i):
                self.combo.setCurrentIndex(i)
                return
        self._custom_path = target
        self.combo.addItem(translate_text("Fichier : {name}", name=Path(target).name), target)
        self.combo.setCurrentIndex(self.combo.count() - 1)

    def setEnabled(self, enabled: bool) -> None:
        super().setEnabled(enabled)
        self.combo.setEnabled(enabled)
        self.browse_btn.setEnabled(enabled)


class DonorSelectCombo(QComboBox):
    """Menu déroulant interactif pour assigner ou réassigner un fichier donneur pour un élément."""

    def __init__(
        self,
        row: int,
        ep: MatrixEpisode,
        all_candidates: list[tuple[MatrixSource, Path]],
        on_changed: Callable[[int, MatrixEpisode], None],
        matrix: HybridMatrix,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.row = row
        self.ep = ep
        self.all_candidates = all_candidates
        self.on_changed = on_changed
        self.matrix = matrix
        self.setStyleSheet(_input_style())
        self._populating = False
        self._populate()
        self.currentIndexChanged.connect(self._handle_selection)

    def _populate(self) -> None:
        self._populating = True
        self.clear()

        # 1. Donneur(s) actuellement assigné(s)
        if self.ep.donor_files:
            for d_src, d_path in self.ep.donor_files:
                self.addItem(f"✓ {d_path.name}  [{d_src.label}]", (d_src, d_path))
            self.setCurrentIndex(0)
        else:
            self.addItem(translate_text("[Aucun donneur]"), "__none__")
            self.setCurrentIndex(0)

        # 2. Option pour retirer / ignorer le donneur
        if self.ep.donor_files:
            self.addItem(translate_text("[Aucun donneur / Ignorer]"), "__none__")

        # 3. Séparateur / Autres candidats disponibles
        current_paths = {p.resolve() for _, p in self.ep.donor_files}
        other_candidates = [(src, p) for src, p in self.all_candidates if p.resolve() not in current_paths]

        if other_candidates:
            self.insertSeparator(self.count())
            for c_src, c_path in other_candidates:
                self.addItem(f"{c_path.name}  [{c_src.label}]", (c_src, c_path))

        # 4. Parcourir un fichier externe
        self.insertSeparator(self.count())
        self.addItem(translate_text("[+ Parcourir un fichier…]"), "__browse__")
        self._populating = False

    def _handle_selection(self, index: int) -> None:
        if self._populating or index < 0:
            return

        data = self.itemData(index)
        if data == "__browse__":
            path, _ = QFileDialog.getOpenFileName(
                self,
                translate_text("Sélectionner un fichier donneur"),
                "",
                "Médias (*.mkv *.mp4 *.m4v *.avi *.ts *.m2ts *.webm *.mka *.ac3 *.dts *.flac *.srt *.ass *.sup);;Tous les fichiers (*)",
            )
            if path:
                p = Path(path).resolve()
                master_src = next((s for s in self.matrix.sources if s.role == SourceRole.MASTER), None)
                donor_src = next((s for s in self.matrix.sources if s != master_src), None)
                if not donor_src:
                    donor_src = self.matrix.add_source(p.parent, role=SourceRole.DONOR, label="Manuel")
                self.ep.donor_files = [(donor_src, p)]
                self.ep.status = "ready"
                self.ep.status_message = translate_text("Prêt (1 donneur)")
                self._populate()
                self.on_changed(self.row, self.ep)
            else:
                self._populating = True
                self.setCurrentIndex(0)
                self._populating = False
            return

        if data == "__none__":
            self.ep.donor_files = []
            self.ep.status = "partial"
            self.ep.status_message = translate_text("Donneur manquant")
            self._populate()
            self.on_changed(self.row, self.ep)
            return

        if isinstance(data, tuple) and len(data) == 2:
            src, p = data
            self.ep.donor_files = [(src, p)]
            self.ep.status = "ready"
            self.ep.status_message = translate_text("Prêt (1 donneur)")
            self._populate()
            self.on_changed(self.row, self.ep)


# =============================================================================
# Panneau Studio Hybridation
# =============================================================================

#: Paires préparées simultanément : la préparation ne lit que les en-têtes et
#: l'index des fichiers ; la synchro audio, qui décode les pistes, reste à deux.
_PREPARE_WORKERS = 4
_CALIBRATE_WORKERS = 2


class _GuiLogger:
    def __init__(self, signal, episode: str):
        self.signal, self.episode = signal, episode

    def emit(self, level: str, message: str, **_fields):
        prefix = f"[{self.episode}]"
        self.signal.emit(level.upper(), message if message.startswith(prefix) else f"{prefix} {message}")

    def workflow_log(self, level: str, message: str):
        self.emit(level, message)


class HybridStudio(QWidget):
    # Préparation d'une ligne du lot : génération, ligne, plan, calibration / message.
    prepared = Signal(int, int, object, object)
    failed = Signal(int, int, str)
    ready_changed = Signal(bool)
    op_state_changed = Signal(str, str)
    op_progress = Signal(str)
    op_progress_pct = Signal(int)
    log_message = Signal(str, str)
    open_in_remux = Signal(object)
    witness_inspected = Signal(object)
    single_calibration_done = Signal(int, object, object)
    single_calibration_failed = Signal(int, str)

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.setWindowTitle(translate_text("Studio Hybridation"))
        self.executor = ThreadPoolExecutor(max_workers=_PREPARE_WORKERS)
        self._executor = self.executor
        # Inspections, sous-titres et TMDB déjà lus : ni la vérification audio
        # ni une nouvelle analyse ne relisent un fichier inchangé.
        self._prep_cache = PreparationCache()
        self._prepare_queue: list[int] = []
        self._prepare_running: set[int] = set()
        self._prepare_generation = 0
        self._prepare_calibrate = False
        self._prepare_settings: tuple[Any, ...] = ()
        self.jobs: dict[int, tuple[Any, Any]] = {}
        self._pending_sync_rows: set[int] = set()
        self._defer_audio_sync = False
        # Échecs par ligne : un épisode en échec n'interrompt pas la saison.
        self._prepare_failures: dict[int, str] = {}
        # Seules les sorties confirmées de cette session peuvent être sautées
        # à la relance ; une sortie préexistante ou remplacée reste une collision.
        self._completed_jobs: dict[int, tuple[Any, tuple[int, int, int, int]]] = {}
        self._prepared_signature = None
        self._matrix_signature = None
        self._run_after_prepare = False
        self._export_after_prepare: Path | None = None
        self.execution_rows: list[int] = []
        self.exec_index: int = 0
        self.matrix = HybridMatrix()
        self.matrix_episodes: list[MatrixEpisode] = []
        self.recipe = HybridRecipe()
        self.extra_sources: list[dict] = []
        self._sync_editor = None
        self._sync_editors: list = []
        self._sync_job_key = None
        self.witness_track_items: list[dict] = []
        self._witness_sources: list[SourceInput] = []
        self._witness_signature = None
        self._tmdb_preview = None
        self.stopped = False
        self.cancel_event = threading.Event()
        self.signals = None
        self.busy = False
        self.aux_running = False
        self._operation_active = False
        self._global_controls = False
        self._operation_start_guard = None
        self._build_ui()
        for combo in self.findChildren(QComboBox):
            combo.currentIndexChanged.connect(self._update_run_button)
        for control in (*self.controls.values(), self.profile):
            if isinstance(control, QLineEdit):
                control.textChanged.connect(self._update_output_preview)
        self._update_run_button()

    def _build_ui(self) -> None:
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(_scale(28), _scale(24), _scale(28), _scale(20))
        outer_layout.setSpacing(_scale(12))

        # En-tête de section
        header_row = QHBoxLayout()
        title_lbl = QLabel(translate_text("Studio Hybridation"))
        title_lbl.setStyleSheet(f"""
            color: {_C.TEXT_PRI};
            font-size: {_font_px(22)}px;
            font-weight: 700;
            background: transparent;
        """)
        header_row.addWidget(title_lbl)
        header_row.addStretch()
        self._readiness_badge = QLabel()
        self._readiness_badge.setStyleSheet(f"color:{_C.OK};background:{_C.BADGE_OK_BG};border:1px solid {_C.BADGE_OK_BORDER};border-radius:5px;padding:5px 8px;font-size:{_font_px(10)}px;")
        header_row.addWidget(self._readiness_badge)
        outer_layout.addLayout(header_row)
        subtitle = QLabel(translate_text("Assembler la vidéo de référence avec l’audio et les sous-titres des donneurs."))
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet(f"color:{_C.TEXT_SEC};font-size:{_font_px(11)}px;background:transparent;")
        outer_layout.addWidget(subtitle)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")

        container = QWidget()
        container.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(_scale(12))

        # ── Carte 1 : Dossiers sources et profils ──────────────────────────────
        card_dirs = _card(self)
        self._sources_card = card_dirs
        cd_layout = QVBoxLayout(card_dirs)
        cd_layout.setContentsMargins(_scale(12), _scale(10), _scale(12), _scale(10))
        cd_layout.setSpacing(_scale(8))
        source_header = QHBoxLayout()
        source_header.addWidget(_section_label(translate_text("SOURCES ET SORTIE")))
        source_header.addStretch()
        cd_layout.addLayout(source_header)

        form = QFormLayout()
        form.setSpacing(_scale(8))
        self.reference = DirectoryEdit("/chemin/vers/episodes_reference")
        self.donor = DirectoryEdit("/chemin/vers/episodes_donneur")
        self.output = DirectoryEdit("/chemin/vers/dossier_sortie")

        for label, edit in (("Référence", self.reference), ("Donneur 1", self.donor), ("Sortie", self.output)):
            row = QHBoxLayout()
            row.setSpacing(_scale(6))
            row.addWidget(edit, stretch=1)
            browse = _secondary_button("…", fixed_width=32)
            browse.clicked.connect(lambda checked=False, field=edit: self.browse_dir(field) if field == self.output else self.browse(field))
            row.addWidget(browse)
            lbl = QLabel(translate_text(label))
            lbl.setStyleSheet(f"color: {_C.TEXT_SEC}; font-size: {_font_px(11)}px;")
            form.addRow(lbl, row)

        self.extra_sources_container = QWidget()
        self.extra_sources_layout = QVBoxLayout(self.extra_sources_container)
        self.extra_sources_layout.setContentsMargins(0, 0, 0, 0)
        self.extra_sources_layout.setSpacing(_scale(6))
        cd_layout.addLayout(form)
        cd_layout.addWidget(self.extra_sources_container)

        self.add_source_btn = _secondary_button(translate_text("+ Ajouter une source donneuse supplémentaire…"))
        self.add_source_btn.clicked.connect(lambda: self._add_extra_source_row())
        self.add_source_btn.setText(translate_text("+ Ajouter un donneur"))
        source_header.addWidget(self.add_source_btn)

        matching_row = QHBoxLayout()
        lbl_matching = QLabel(translate_text("Mode de correspondance :"))
        lbl_matching.setStyleSheet(f"color: {_C.TEXT_SEC}; font-size: {_font_px(11)}px;")
        self.matching_mode_combo = QComboBox()
        self.matching_mode_combo.setStyleSheet(_input_style())
        self.matching_mode_combo.addItem(translate_text("Épisodes (Séries / Animes)"), MatchingMode.EPISODE)
        self.matching_mode_combo.addItem(translate_text("Similitude des titres (Films / Collections)"), MatchingMode.FUZZY)
        self.matching_mode_combo.addItem(translate_text("Ordre naturel (Tri alphabétique 1 à 1)"), MatchingMode.ORDER)
        matching_row.addWidget(lbl_matching)
        matching_row.addWidget(self.matching_mode_combo, stretch=1)
        cd_layout.addLayout(matching_row)


        profile_row = QVBoxLayout()
        self.profile = ProfileSelector(self.config.profiles_dir, self)
        self.profile.setToolTip(translate_text(
            "Le profil s’applique après la détection forcés/SDH, sur les pistes du modèle d’assemblage "
            "s’il est défini : sélection finale, noms, flags, ordre et variantes audio."
        ))
        lbl_prof = QLabel(translate_text("Profil décisionnel (optionnel) :"))
        lbl_prof.setStyleSheet(f"color: {_C.TEXT_SEC}; font-size: {_font_px(11)}px;")
        profile_row.addWidget(lbl_prof)
        profile_row.addWidget(self.profile, stretch=1)


        self.detect_cuts = QCheckBox(
            translate_text("Détecter les coupures et variations de cadences (cuts multi-segments)")
        )
        self.detect_cuts.setChecked(True)
        self.detect_cuts.setStyleSheet(_checkbox_style())
        self.detect_cuts.setToolTip(self.detect_cuts.text())
        self.detect_cuts.setText(translate_text("Détecter les coupures"))

        cadence_row = QHBoxLayout()
        self.cadence_auto_apply = QCheckBox(
            translate_text("Appliquer automatiquement la conversion PAL ↔ Cinéma si détectée")
        )
        auto_cadence_default = getattr(self.config, "sync_cadence_auto_apply", True)
        self.cadence_auto_apply.setChecked(bool(auto_cadence_default))
        self.cadence_auto_apply.setStyleSheet(_checkbox_style())
        self.cadence_auto_apply.setToolTip(self.cadence_auto_apply.text())
        self.cadence_auto_apply.setText(translate_text("Conversion PAL ↔ Cinéma automatique"))
        cadence_row.addWidget(self.detect_cuts)
        cadence_row.addSpacing(_scale(18))
        cadence_row.addWidget(self.cadence_auto_apply)

        cadence_row.addSpacing(_scale(16))
        lbl_method = QLabel(translate_text("Méthode audio :"))
        lbl_method.setStyleSheet(f"color: {_C.TEXT_SEC}; font-size: {_font_px(11)}px;")
        cadence_row.addWidget(lbl_method)

        self.cadence_method_combo = QComboBox()
        self.cadence_method_combo.addItem(translate_text("Auto (pitch / tonalité)"), "auto")
        self.cadence_method_combo.addItem(translate_text("Préservation tonalité (atempo)"), "atempo")
        self.cadence_method_combo.addItem(translate_text("Hauteur naturelle (asetrate)"), "asetrate")
        default_method = getattr(self.config, "sync_cadence_audio_method", "auto") or "auto"
        idx = self.cadence_method_combo.findData(default_method)
        if idx >= 0:
            self.cadence_method_combo.setCurrentIndex(idx)
        self.cadence_method_combo.setStyleSheet(_input_style())
        cadence_row.addWidget(self.cadence_method_combo)
        cadence_row.addStretch()


        sync_row = QVBoxLayout()
        sync_row.addWidget(QLabel(translate_text("Analyse synchro :")))
        self.sync_strategy = QComboBox()
        self.sync_strategy.setStyleSheet(_input_style())
        self.sync_strategy.addItem(translate_text("Automatique (analyse acoustique)"), "auto")
        self.sync_strategy.addItem(translate_text("Décalage manuel (sans analyse)"), "manual")
        self.sync_strategy.addItem(translate_text("Aucune synchronisation"), "none")
        self.sync_strategy.setToolTip(translate_text(
            "L'analyse automatique décode plusieurs fenêtres audio par donneur. "
            "Le mode manuel applique le même décalage à chaque donneur ; aucune synchronisation conserve les pistes sans recalage."
        ))
        sync_row.addWidget(self.sync_strategy, stretch=1)
        self.manual_shift_label = QLabel(translate_text("Décalage (ms) :"))
        sync_row.addWidget(self.manual_shift_label)
        self.manual_shift = QSpinBox()
        self.manual_shift.setStyleSheet(_input_style())
        self.manual_shift.setRange(-60000, 60000)
        sync_row.addWidget(self.manual_shift)
        self.manual_shift_label.setVisible(False)
        self.manual_shift.setVisible(False)
        self.sync_strategy.currentIndexChanged.connect(self._on_sync_strategy_changed)
        options_row = QHBoxLayout()
        options_row.setSpacing(_scale(16))
        options_row.addLayout(profile_row, 1)
        mode_row = QVBoxLayout()
        mode_row.addWidget(_section_label(translate_text("SYNCHRONISATION")))
        self.mode = QComboBox()
        self.mode.setStyleSheet(_input_style())
        self.mode.addItem(translate_text("Synchronisation physique"), "physical")
        self.mode.addItem(translate_text("Décalage conteneur"), "container")
        mode_row.addWidget(self.mode)
        options_row.addLayout(mode_row, 1)
        options_row.addLayout(sync_row, 1)
        cd_layout.addLayout(options_row)
        cd_layout.addLayout(cadence_row)
        layout.addWidget(card_dirs)

        # ── Carte 1 bis : Plan d'assemblage type (Épisode témoin) ─────────────
        card_witness = CollapsibleCard(translate_text("MODÈLE D’ASSEMBLAGE · ÉPISODE TÉMOIN"), self)
        self._witness_section = card_witness
        cwit_layout = card_witness.body_layout
        wit_header = card_witness.header_layout
        self.inspect_witness_btn = _secondary_button(translate_text("🔍 Inspecter l'épisode témoin"))
        self.inspect_witness_btn.clicked.connect(self.inspect_witness)
        wit_header.addWidget(self.inspect_witness_btn)


        self.witness_info = QLabel(translate_text("Choisissez vos dossiers sources ci-dessus, puis inspectez l'épisode témoin pour choisir les pistes à mixer."))
        self.witness_info.setStyleSheet(f"color: {_C.TEXT_SEC}; font-size: {_font_px(11)}px;")
        cwit_layout.addWidget(self.witness_info)

        # Colonnes du tableau des pistes du panneau Conteneur (Codec, Langue, Titre, Info).
        headers = ["Conserver", "Source", "Type", "Codec", "Langue", "Titre", "Info"]
        self.witness_table = ContentTableWidget(0, len(headers))
        self.witness_table.setStyleSheet(_table_style())
        self.witness_table.setHorizontalHeaderLabels([translate_text(label) for label in headers])
        for column in range(len(headers)):
            self.witness_table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.witness_table.horizontalHeader().setSectionResizeMode(len(headers) - 1, QHeaderView.ResizeMode.Stretch)
        # « disabled » (FlagEnabled absent) en couleur d'erreur, comme dans le Conteneur.
        self.witness_table.setItemDelegateForColumn(6, _TrackInfoDelegate(self.witness_table))
        cwit_layout.addWidget(self.witness_table)

        wit_actions = QHBoxLayout()
        self.apply_recipe_btn = _primary_button(translate_text("🪄 Appliquer ce modèle à toute la saison"))
        self.apply_recipe_btn.setEnabled(False)
        self.apply_recipe_btn.clicked.connect(self.apply_witness_recipe)
        wit_actions.addWidget(self.apply_recipe_btn)

        self.recipe_status_lbl = QLabel(translate_text("Modèle actuel : Règles automatiques"))
        self.recipe_status_lbl.setWordWrap(True)
        self.recipe_status_lbl.setStyleSheet(f"color: {_C.TEXT_SEC}; font-size: {_font_px(11)}px;")
        wit_actions.addWidget(self.recipe_status_lbl)
        wit_actions.addStretch()
        cwit_layout.addLayout(wit_actions)

        layout.addWidget(card_witness)

        # Les réglages usuels restent visibles ; seuls les réglages experts
        # appartiennent au groupe repliable.
        standard = _card(self)
        self._standard_options = standard
        standard_layout = QVBoxLayout(standard)
        standard_layout.setContentsMargins(_scale(12), _scale(10), _scale(12), _scale(10))
        standard_layout.addWidget(_section_label(translate_text("OPTIONS D’HYBRIDATION")))
        standard_settings = QFormLayout()
        standard_settings.setSpacing(_scale(6))
        standard_layout.addLayout(standard_settings)
        layout.addWidget(standard)

        advanced = QGroupBox(translate_text("Options d'hybridation avancées"))
        self._advanced_options = advanced
        advanced.setStyleSheet(_groupbox_checkable_style())
        settings = QFormLayout(advanced)
        settings.setSpacing(_scale(6))
        self.controls: dict[str, QWidget] = {}
        for option, label, default in (
            ("auto-forced-subs", "Détecter les sous-titres forcés", True),
            ("auto-sdh", "Détecter les sous-titres SDH", True),
            ("remove-closed-captions", "Supprimer les sous-titres embarqués EIA-608/708", True),
            ("no-clean-nfo", "Conserver le chemin complet dans le NFO", False),
            ("no-cover", "Sans jaquette TMDB", False),
        ):
            control = QCheckBox(translate_text(label))
            control.setChecked(default)
            control.setStyleSheet(_checkbox_style())
            target_settings = standard_settings if option in {"auto-forced-subs", "auto-sdh"} else settings
            target_settings.addRow(control)
            self.controls[option] = control
            if option == "remove-closed-captions":
                control.setEnabled(False)
                control.setToolTip(translate_text("Disponible après détection sur une vidéo retenue. Préserve l’image et les métadonnées HDR/Dolby Vision."))

        for option, label, value, maximum in (
            ("forced-threshold", "Seuil forcés", 50, 10000),
            ("crossfade-ms", "Fondu aux raccords (ms)", 80, 1000),
            ("drift-threshold-ms", "Seuil de dérive (ms)", 25, 10000),
        ):
            spin = QSpinBox()
            spin.setStyleSheet(_input_style())
            spin.setRange(0 if option == "crossfade-ms" else 1, maximum)
            spin.setValue(value)
            lbl_spin = QLabel(translate_text(label))
            lbl_spin.setStyleSheet(f"color: {_C.TEXT_SEC}; font-size: {_font_px(11)}px;")
            settings.addRow(lbl_spin, spin)
            self.controls[option] = spin

        for option, label in (
            ("auto-tmdb", "ID TMDB"),
            ("tag", "Groupe de release"),
            ("output-template", "Template de sortie"),
            ("calibration", "Fichier de calibration"),
        ):
            line_edit = OutputTemplateLineEdit(hybrid=True) if option == "output-template" else QLineEdit("MVO" if option == "tag" else "")
            target_settings = standard_settings if option in {"auto-tmdb", "output-template"} else settings
            line_edit.setStyleSheet(_input_style())
            lbl_txt = QLabel(translate_text(label))
            lbl_txt.setStyleSheet(f"color: {_C.TEXT_SEC}; font-size: {_font_px(11)}px;")
            if option == "auto-tmdb":
                tmdb_row = QWidget()
                tmdb_layout = QHBoxLayout(tmdb_row)
                tmdb_layout.setContentsMargins(0, 0, 0, 0)
                tmdb_layout.addWidget(line_edit, 1)
                self.tmdb_search_button = _secondary_button(translate_text("Rechercher…"))
                self.tmdb_search_button.clicked.connect(self._search_tmdb)
                tmdb_layout.addWidget(self.tmdb_search_button)
                target_settings.addRow(lbl_txt, tmdb_row)
            else:
                target_settings.addRow(lbl_txt, line_edit)
            self.controls[option] = line_edit
            if option == "output-template":
                self.output_preview = OutputTemplatePreview(line_edit)
                target_settings.addRow(self.output_preview)

        self.mirror = QCheckBox(translate_text("Recaler les sous-titres"))
        self.mirror.setChecked(True)
        self.mirror.setStyleSheet(_checkbox_style())
        settings.addRow(self.mirror)

        advanced.setCheckable(True)
        advanced.setChecked(False)
        def _toggle_advanced(checked: bool) -> None:
            for i in range(settings.count()):
                item = settings.itemAt(i)
                widget = item.widget() if item is not None else None
                if widget is not None:
                    widget.setVisible(checked)
            self._update_caption_option()

        advanced.toggled.connect(_toggle_advanced)
        advanced.toggled.emit(False)
        layout.addWidget(advanced)

        # ── Carte 3 : Tableau d'appariement et plan de correspondance ────────
        card_table = _card(self)
        ct_layout = QVBoxLayout(card_table)
        ct_layout.setContentsMargins(_scale(12), _scale(10), _scale(12), _scale(10))
        ct_layout.setSpacing(_scale(8))

        ct_header = QHBoxLayout()
        ct_header.addWidget(_section_label(translate_text("PLAN DE CORRESPONDANCE ET APPARIEMENT")))
        ct_header.addStretch()
        self.add_manual_pair_btn = _secondary_button(translate_text("+ Associer manuellement une paire…"))
        self.add_manual_pair_btn.clicked.connect(self._add_manual_pair)
        ct_header.addWidget(self.add_manual_pair_btn)
        self.verify_audio_button = _secondary_button(translate_text("Vérifier la synchronisation audio"))
        self.verify_audio_button.clicked.connect(self.verify_audio)
        ct_header.addWidget(self.verify_audio_button)
        ct_layout.addLayout(ct_header)

        self.table = ContentTableWidget(0, 5, max_visible_rows=10)
        self.table.setStyleSheet(_table_style())
        self.table.setHorizontalHeaderLabels([
            translate_text("Élément / Titre"),
            translate_text("Référence (Master)"),
            translate_text("Donneur(s) assigné(s)"),
            translate_text("Synchro & Calibrage"),
            translate_text("Actions"),
        ])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.currentCellChanged.connect(lambda *_args: self.select_waveform())
        self.table.cellDoubleClicked.connect(lambda row, col: self.open_remux_row(row))
        ct_layout.addWidget(self.table)
        layout.addWidget(card_table)
        layout.removeWidget(card_witness)
        layout.addWidget(card_witness)

        # ── Carte 4 : Forme d'onde et micro-ajustement ─────────────────────────
        card_wave = CollapsibleCard(translate_text("RECALAGE ET PRÉ-ÉCOUTE"), self)
        self._wave_section = card_wave
        cw_layout = card_wave.body_layout

        episode_row = QHBoxLayout()
        episode_row.addWidget(QLabel(translate_text("Élément :")))
        self.sync_episode = QComboBox()
        self.sync_episode.setStyleSheet(_input_style())
        self.sync_episode.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.sync_episode.setMinimumContentsLength(20)
        self.sync_episode.setPlaceholderText(translate_text("Aucun élément analysé"))
        self.sync_episode.setEnabled(False)
        self.sync_episode.currentIndexChanged.connect(self._select_sync_episode)
        episode_row.addWidget(self.sync_episode, 1)
        cw_layout.addLayout(episode_row)

        wave_header = QHBoxLayout()
        self.sync_target = QComboBox()
        self.sync_target.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.sync_target.setMinimumContentsLength(20)
        self.sync_target.setStyleSheet(_input_style())
        self.sync_target.setToolTip(translate_text("Piste donneuse à inspecter"))
        self.sync_target.setEnabled(False)
        self.sync_reference = QComboBox()
        self.sync_reference.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.sync_reference.setMinimumContentsLength(15)
        self.sync_reference.setStyleSheet(_input_style())
        self.sync_reference.setToolTip(translate_text("Piste de référence"))
        self.sync_reference.setEnabled(False)
        wave_header.addWidget(QLabel(translate_text("Donneur :")))
        wave_header.addWidget(self.sync_target, 1)
        wave_header.addWidget(QLabel(translate_text("Référence :")))
        wave_header.addWidget(self.sync_reference, 1)
        cw_layout.addLayout(wave_header)
        self.sync_hint = QLabel(translate_text("Analysez la saison ou un élément du plan pour ouvrir le Sync Studio."))
        self.sync_hint.setWordWrap(True)
        cw_layout.addWidget(self.sync_hint)
        self._sync_content = QVBoxLayout()
        cw_layout.addLayout(self._sync_content)
        self.sync_target.currentIndexChanged.connect(self._show_sync_editor)
        self.sync_reference.currentIndexChanged.connect(self._show_sync_editor)
        layout.addWidget(card_wave)

        scroll.setWidget(container)
        outer_layout.addWidget(scroll, stretch=1)

        # ── Barre d'action inférieure ─────────────────────────────────────────
        bar = QHBoxLayout()
        bar.setSpacing(_scale(8))

        self.scan_button = _secondary_button(translate_text("Analyser la saison"))
        self.scan_button.clicked.connect(self.scan)
        bar.addWidget(self.scan_button)

        self.listen_button = _secondary_button(translate_text("Pré-écoute"))
        self.listen_button.clicked.connect(self.listen)
        bar.addWidget(self.listen_button)

        self.export_button = _secondary_button(translate_text("Sauvegarder les workflows…"))
        self.export_button.clicked.connect(self.export_jobs)
        bar.addWidget(self.export_button)

        self.cancel_button = _secondary_button(translate_text("Annuler"))
        self.cancel_button.clicked.connect(self.cancel)
        bar.addWidget(self.cancel_button)

        bar.addStretch()

        self.run_button = _primary_button(translate_text("Lancer l'hybridation"))
        self.run_button.setEnabled(False)
        self.run_button.clicked.connect(self.run)
        bar.addWidget(self.run_button)

        for field in (self.reference, self.donor, self.output):
            field.textChanged.connect(self._update_run_button)

        outer_layout.addLayout(bar)

        self.status = QLabel()
        self.status.setStyleSheet(f"color: {_C.TEXT_SEC}; font-size: {_font_px(11)}px;")
        outer_layout.addWidget(self.status)

        self.prepared.connect(self.on_prepared)
        self.failed.connect(self._on_episode_failed)
        self.witness_inspected.connect(self._populate_witness_table)
        self.single_calibration_done.connect(self._on_single_calibration_done)
        self.single_calibration_failed.connect(self._on_single_calibration_failed)

    def _add_extra_source_row(self, path: str = "", role: SourceRole = SourceRole.DONOR, label: str = ""):
        row_widget = QWidget()
        row_layout = QHBoxLayout(row_widget)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(_scale(6))

        role_combo = QComboBox()
        role_combo.setStyleSheet(_input_style())
        role_combo.addItem(translate_text("Donneur général"), SourceRole.DONOR)
        role_combo.addItem(translate_text("Audio seul (ex: VF suite/relais)"), SourceRole.DONOR_AUDIO)
        role_combo.addItem(translate_text("Sous-titres seuls (ex: Fansub)"), SourceRole.DONOR_SUBTITLE)
        if role == SourceRole.DONOR_AUDIO:
            role_combo.setCurrentIndex(1)
        elif role == SourceRole.DONOR_SUBTITLE:
            role_combo.setCurrentIndex(2)

        edit = DirectoryEdit("/chemin/vers/source_supplementaire")
        if path:
            edit.setText(path)
        browse = _secondary_button("…", fixed_width=32)
        browse.clicked.connect(lambda checked=False, field=edit: self.browse(field))

        del_btn = _secondary_button("✕", fixed_width=28)

        lbl = QLabel(f"Donneur {len(self.extra_sources) + 2}")
        lbl.setStyleSheet(f"color: {_C.TEXT_SEC}; font-size: {_font_px(11)}px;")

        row_layout.addWidget(lbl)
        row_layout.addWidget(role_combo)
        row_layout.addWidget(edit, stretch=1)
        row_layout.addWidget(browse)
        row_layout.addWidget(del_btn)

        source_info = {"widget": row_widget, "edit": edit, "role": role_combo, "label": label or f"Donneur {len(self.extra_sources) + 2}"}
        self.extra_sources.append(source_info)
        edit.textChanged.connect(self._update_run_button)

        def remove_row():
            if source_info in self.extra_sources:
                self.extra_sources.remove(source_info)
            row_widget.deleteLater()
            self._update_run_button()

        del_btn.clicked.connect(remove_row)
        self.extra_sources_layout.addWidget(row_widget)

    def _build_matrix(self) -> HybridMatrix:
        mode_val = self.matching_mode_combo.currentData() or MatchingMode.EPISODE
        matrix = HybridMatrix(matching_mode=mode_val)
        ref_text = self.reference.text().strip()
        if ref_text:
            matrix.add_source(ref_text, role=SourceRole.MASTER, label="Référence")

        donor_text = self.donor.text().strip()
        if donor_text:
            matrix.add_source(donor_text, role=SourceRole.DONOR, label="Donneur 1", priority=10)

        for i, s in enumerate(self.extra_sources, start=2):
            p = s["edit"].text().strip()
            if p:
                role_val = s["role"].currentData() or SourceRole.DONOR
                matrix.add_source(p, role=role_val, label=s.get("label") or f"Donneur {i}", priority=5)

        return matrix

    def inspect_witness(self):
        matrix = self._build_matrix()
        if not matrix.sources:
            self.on_failed(translate_text("Veuillez sélectionner au moins un dossier de référence."))
            return

        episodes = matrix.scan()
        witness_ep = next((ep for ep in episodes if ep.master_file and ep.donor_files), None)
        if not witness_ep:
            witness_ep = next((ep for ep in episodes if ep.master_file), None)

        if not witness_ep or not witness_ep.master_file:
            self.witness_info.setText(translate_text("Aucun fichier d'épisode valide détecté."))
            return

        self.witness_info.setText(
            translate_text("Inspection de l'épisode témoin : {ep} ({file})…", ep=witness_ep.display_name, file=witness_ep.master_file.name)
        )

        master_file = witness_ep.master_file

        def task():
            from core.inspector import FileInspector
            inspector = FileInspector(str(self.config.tool_ffprobe), str(self.config.tool_mediainfo))
            sources_tracks = []
            try:
                master_info = self._prep_cache.inspect(inspector, master_file)
                sources_tracks.append(("Master", "master", master_info, "master"))
                for d_src, d_path in witness_ep.donor_files:
                    d_info = self._prep_cache.inspect(inspector, d_path)
                    sources_tracks.append((d_src.label or "Donneur", "donor", d_info, d_src.source_id))
                return sources_tracks
            except Exception as exc:
                return exc

        def on_done(future):
            if future.cancelled():
                return
            res = future.result()
            self.witness_inspected.emit(res)

        fut = self.executor.submit(task)
        fut.add_done_callback(on_done)

    def _populate_witness_table(self, res):
        self._witness_section.set_expanded(True)
        if isinstance(res, Exception):
            self.witness_info.setText(translate_text("Erreur d'inspection : {err}", err=str(res)))
            return

        self.witness_table.setRowCount(0)
        self.witness_track_items = []
        self._witness_sources = []
        self._witness_signature = self._source_signature()
        row = 0
        for source_index, source_info in enumerate(res):
            src_label, role, file_info = source_info[:3]
            source_id = source_info[3] if len(source_info) > 3 else ("master" if role == "master" else f"src_{source_index + 1}")
            typed_tracks = [
                *(("video", track) for track in file_info.video_tracks),
                *(("audio", track) for track in file_info.audio_tracks),
                *(("subtitle", track) for track in file_info.subtitle_tracks),
            ]
            entries = tracks_from_file_info(file_info)
            for entry in entries:
                entry.file_id = f"src{source_index}"
            self._witness_sources.append(SourceInput(file_info.path, source_index, entries))
            choices = witness_track_choices(source_id, [replace(entry, enabled=True) for entry in entries])
            for (st_type, stream), entry, choice in zip(typed_tracks, entries, choices):
                self.witness_table.insertRow(row)

                chk = QCheckBox()
                is_master = (role == "master")
                lang = stream.language or ""

                if st_type == "video":
                    checked = is_master
                elif st_type == "audio":
                    if is_master:
                        checked = _language_selected(lang, self.recipe.keep_master_audio_langs)
                    else:
                        checked = _language_selected(lang, self.recipe.donor_audio_langs)
                elif st_type == "subtitle":
                    checked = (not is_master) and _language_selected(lang, self.recipe.donor_sub_langs)
                else:
                    checked = False

                chk.setChecked(choice in self.recipe.track_choices if self.recipe.track_choices is not None else checked)
                chk.setStyleSheet(_checkbox_style())
                self.witness_table.setCellWidget(row, 0, chk)

                item_src = QTableWidgetItem(src_label)
                item_src.setForeground(QColor(_C.ACCENT if is_master else _C.OK))
                self.witness_table.setItem(row, 1, item_src)

                item_type = QTableWidgetItem(translate_text(entry.type_long))
                item_type.setForeground(QColor({"video": _C.TRACK_VIDEO, "audio": _C.TRACK_AUDIO,
                                                "subtitle": _C.TRACK_SUBTITLE}.get(st_type, _C.TEXT_SEC)))
                self.witness_table.setItem(row, 2, item_type)
                self.witness_table.setItem(row, 3, QTableWidgetItem(entry.codec))
                self.witness_table.setItem(row, 4, QTableWidgetItem(entry.language or "-"))
                self.witness_table.setItem(row, 5, QTableWidgetItem(entry.title))

                # Même libellé que la colonne Info du Conteneur : définition, HDR,
                # cadence, canaux, débit, Atmos / DTS:X, éléments et flags.
                item_info = QTableWidgetItem(entry.full_info_label)
                item_info.setForeground(QColor(_C.TEXT_SEC))
                item_info.setToolTip(entry.full_info_label)
                item_info.setData(_TRACK_INFO_DISABLED_LABEL_ROLE,
                                  "" if entry.flag_enabled else TrackEntry.DISABLED_LABEL)
                self.witness_table.setItem(row, 6, item_info)

                self.witness_track_items.append({
                    "checkbox": chk,
                    "role": role,
                    "type": st_type,
                    "lang": stream.language or "",
                    "codec": stream.codec or "",
                    "choice": choice,
                    "track": entry,
                })
                chk.toggled.connect(self._update_output_preview)
                row += 1

        self.apply_recipe_btn.setEnabled(not self.busy)
        self.witness_info.setText(translate_text("Pistes détectées sur l'épisode témoin. Cochez celles à conserver puis appliquez le modèle."))
        self._update_output_preview()

    def apply_witness_recipe(self):
        if self.busy or not self.witness_track_items:
            return

        recipe = replace(self.recipe)
        master_audio_langs = []
        donor_audio_langs = []
        donor_sub_langs = []
        keep_video = False
        choices = []
        master_sub_langs = []

        for item in self.witness_track_items:
            if not item["checkbox"].isChecked():
                continue
            role = item["role"]
            st_type = item["type"]
            lang = item["lang"]
            choices.append(item["choice"])

            if st_type == "video" and role == "master":
                keep_video = True
            elif st_type == "audio":
                if role == "master" and lang and lang not in master_audio_langs:
                    master_audio_langs.append(lang)
                elif role == "donor" and lang and lang not in donor_audio_langs:
                    donor_audio_langs.append(lang)
            elif st_type == "subtitle" and role == "donor":
                if lang and lang not in donor_sub_langs:
                    donor_sub_langs.append(lang)
            elif st_type == "subtitle" and role == "master":
                if lang and lang not in master_sub_langs:
                    master_sub_langs.append(lang)

        recipe.keep_master_video = keep_video
        recipe.keep_master_audio_langs = master_audio_langs
        recipe.donor_audio_langs = donor_audio_langs
        recipe.donor_sub_langs = donor_sub_langs
        recipe.track_choices = tuple(choices)
        recipe.sync_mode = self.mode.currentData() or "physical"
        recipe.sync_subtitles = "mirror" if self.mirror.isChecked() else "none"
        recipe.cadence_auto_apply = self.cadence_auto_apply.isChecked()
        recipe.cadence_audio_method = self.cadence_method_combo.currentData() or "auto"
        self.recipe = recipe

        summary = translate_text(
            "Modèle actif : Vidéo Master ({video}), Audio Donneur : [{donor_audio}], "
            "Audio Master : [{master_audio}], ST Donneur : [{donor_subs}], ST Master : [{master_subs}]",
            video=translate_text("Oui") if keep_video else translate_text("Non"),
            donor_audio=", ".join(recipe.donor_audio_langs),
            master_audio=", ".join(recipe.keep_master_audio_langs),
            donor_subs=", ".join(recipe.donor_sub_langs),
            master_subs=", ".join(master_sub_langs),
        )
        self.recipe_status_lbl.setText(summary)
        self.recipe_status_lbl.setStyleSheet(f"color: {_C.OK}; font-size: {_font_px(11)}px; font-weight: bold;")
        self.status.setText(translate_text("Modèle appliqué à la saison. Vous pouvez lancer l'analyse ou l'hybridation."))
        self._update_output_preview()

    def browse(self, field: QLineEdit):
        if self.busy:
            return
        menu = QMenu(self)
        act_dir = menu.addAction(translate_text("Choisir un dossier…"))
        act_file = menu.addAction(translate_text("Choisir un fichier média…"))
        action = menu.exec(QCursor.pos())
        if action == act_dir:
            path = QFileDialog.getExistingDirectory(self, translate_text("Choisir un dossier"), field.text())
            if path:
                field.setText(path)
        elif action == act_file:
            path, _ = QFileDialog.getOpenFileName(
                self,
                translate_text("Choisir un fichier média"),
                field.text(),
                "Médias (*.mkv *.mp4 *.m4v *.avi *.ts *.m2ts *.webm *.mka *.ac3 *.dts *.flac *.srt *.ass *.sup);;Tous les fichiers (*)",
            )
            if path:
                field.setText(path)

    def browse_dir(self, field: QLineEdit):
        if self.busy:
            return
        path = QFileDialog.getExistingDirectory(self, translate_text("Choisir un dossier de sortie"), field.text())
        if path:
            field.setText(path)

    def _search_tmdb(self):
        """Réutilise la recherche Remux et reporte l'identifiant sur tout le lot."""
        if self.busy:
            return
        from core.media_info_fetcher import clean_filename_for_search, extract_year_from_filename

        row = self.table.currentRow()
        episode = self.matrix_episodes[row] if 0 <= row < len(self.matrix_episodes) else next(
            (ep for ep in self.matrix_episodes if ep.master_file), None,
        )
        path = episode.master_file if episode and episode.master_file else Path(self.reference.text().strip()).expanduser()
        suggested = clean_filename_for_search(path)
        year = extract_year_from_filename(path)
        if year:
            suggested = f"{suggested} {year}".strip()
        season, number = ((episode.season, episode.episode) if episode and episode.season and episode.episode
                          else (parse_episode_key(path) or (0, 0)))
        dialog = TmdbSearchModal(
            self.config, suggested_title=suggested, suggested_season=season,
            suggested_episode=number, parent=self,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.selected_result is not None:
            self._tmdb_preview = (str(dialog.selected_result.tmdb_id), dialog.fetched_details)
            self.controls["auto-tmdb"].setText(str(dialog.selected_result.tmdb_id))
            self._update_output_preview()

    def _update_output_preview(self, *_args):
        """Réutilise le contexte réel, ou les pistes témoins avant la préparation."""
        if not hasattr(self, "output_preview") or not hasattr(self, "table"):
            return
        self._update_caption_option()
        source_signature = self._source_signature()
        sources_current = self._matrix_signature is None or self._matrix_signature == source_signature
        row = self.table.currentRow()
        if row < 0 and self.jobs:
            row = min(self.jobs)
        episode = (self.matrix_episodes[row] if sources_current and 0 <= row < len(self.matrix_episodes)
                   else None)
        job = self.jobs.get(row) if sources_current else None
        snapshot = (episode.naming_context or {}) if episode and job else {}
        profile = self.profile.text().strip()
        note = translate_text("Pistes et métadonnées du dernier plan analysé.")
        tracks = []
        order = None
        if job and job[0].sources:
            plan = job[0]
            source = plan.sources[0].path
            tracks = [track for src in plan.sources for track in src.tracks]
            order = plan.track_order
        elif self._witness_sources and self._witness_signature == source_signature and (
            episode is None or episode.master_file == self._witness_sources[0].path
        ):
            source = self._witness_sources[0].path
            tracks = [replace(item["track"], enabled=item["checkbox"].isChecked()) for item in self.witness_track_items]
            note = translate_text("Aperçu témoin : analysez pour appliquer le profil et les options aux pistes finales.")
        else:
            raw_source = self.reference.text().strip()
            source = episode.master_file if episode else Path(raw_source) if raw_source else None
            note = translate_text("Aperçu partiel : inspectez le témoin ou analysez cet élément pour connaître ses pistes.")
        if source is None:
            self.output_preview.set_context(None, note=note)
            return
        if episode is None:
            season, number = parse_episode_key(source) or (0, 0)
            episode = MatrixEpisode(master_file=source, season=season, episode=number)
        tmdb_id = self.controls["auto-tmdb"].text().strip()
        details = snapshot.get("__details__") if snapshot.get("__tmdb_id__", "") == tmdb_id else None
        if details is None and self._tmdb_preview and self._tmdb_preview[0] == tmdb_id:
            details = self._tmdb_preview[1]
            if details and episode.season and episode.episode and (
                str(episode.season) != details.season or str(episode.episode) != details.episode
            ):
                # Le titre de série vaut pour tout le lot ; pas celui d'un autre épisode.
                details = replace(details, season=str(episode.season), episode=str(episode.episode), episode_title="")
                note = translate_text("Aperçu partiel : le titre TMDB de cet épisode sera chargé pendant l'analyse.")
        if tmdb_id and details is None:
            note = translate_text("Aperçu partiel : recherchez le titre TMDB ou analysez pour charger ses métadonnées.")
        variables = snapshot.get("__profile_variables__", {}) if snapshot.get("__profile__", "") == profile else {}
        if job and snapshot.get("__profile__", "") != profile:
            note = translate_text("Profil modifié : réanalysez pour actualiser les pistes et les alias de nommage.")
        context = matrix_output_context(
            episode, source, details=details, tracks=tracks, track_order=order, variables=variables,
            group=self.controls["tag"].text().strip(),
        )
        self.output_preview.set_context(context, default_name=default_matrix_output_name(source, self.recipe.output_tag),
                                        episode=episode.display_name, note=note)

    def _update_caption_option(self):
        """La disponibilité suit les vidéos détectées, sans modifier le choix utilisateur."""
        signature = self._source_signature()
        detected = (self._witness_signature == signature and any(
            item["track"].is_video and item["track"].has_closed_captions and item["checkbox"].isChecked()
            for item in self.witness_track_items
        )) or (self._matrix_signature == signature and any(
            track.enabled and track.is_video and track.has_closed_captions
            for config, _ in self.jobs.values() for source in config.sources for track in source.tracks
        ))
        self.controls["remove-closed-captions"].setEnabled(bool(detected) and not self.busy)

    def set_busy(self, busy: bool):
        self.busy = busy
        self._sources_card.setEnabled(not busy)
        self._witness_section.body.setEnabled(not busy)
        self._wave_section.body.setEnabled(not busy)
        if busy:
            self._retire_sync_editor()
            self._sync_job_key = None
        self.scan_button.setEnabled(not busy)
        self.listen_button.setEnabled(not busy)
        self.export_button.setEnabled(not busy)
        self._update_run_button()
        for field in (
            self.reference,
            self.donor,
            self.output,
            self.matching_mode_combo,
            self.profile,
            self.detect_cuts,
            self.sync_strategy,
            self.manual_shift,
            self.cadence_auto_apply,
            self.cadence_method_combo,
            self.mode,
            self.mirror,
            self.inspect_witness_btn,
            self.add_source_btn,
            self.add_manual_pair_btn,
            self.tmdb_search_button,
            self.table,
            *self.controls.values(),
        ):
            field.setEnabled(not busy)
        self._on_sync_strategy_changed()
        self._update_caption_option()
        self.apply_recipe_btn.setEnabled(not busy and bool(self.witness_track_items))
        if not busy:
            self.select_waveform()

    def operation_state(self) -> OperationState:
        count = sum(ep.is_complete for ep in self.matrix_episodes)
        output = self.output.text().strip()
        sources = bool(self.reference.text().strip() and (
            self.donor.text().strip() or any(source["edit"].text().strip() for source in self.extra_sources)
        ))
        ready = bool(output and (sources or count) and not self.aux_running)
        if self.aux_running:
            reason = translate_text("Attendez la fin de la préparation audio.")
        elif not output:
            reason = translate_text("Choisissez un dossier de sortie.")
        elif not sources and not count:
            reason = translate_text("Choisissez une référence et au moins une source donneuse.")
        else:
            reason = ""
        batch_label = "Hybrider {count} épisodes" if self.matching_mode_combo.currentData() == MatchingMode.EPISODE else "Hybrider {count} fichiers"
        label = (translate_text(batch_label, count=count) if count > 1 else
                 translate_text("Hybrider le fichier") if count == 1 else translate_text("Lancer l’hybridation"))
        return OperationState("hybrid", 4, label, output or translate_text("Hybridation multi-sources"), ready, reason)

    def set_operation_start_guard(self, guard) -> None:
        self._operation_start_guard = guard

    def use_global_controls(self) -> None:
        self._global_controls = True
        self.run_button.hide()
        self.cancel_button.hide()

    def _begin_operation(self, label: str) -> bool:
        if self._operation_active:
            return True  # continuation analyse → exécution du même lot
        if self.aux_running:
            self.status.setText(translate_text("Attendez la fin de la préparation audio."))
            return False
        if self._operation_start_guard is not None and not self._operation_start_guard():
            return False
        self._operation_active = True
        self.stopped = False
        self.signals = None
        self.cancel_event.clear()
        self.op_state_changed.emit("started", label)
        return True

    def _finish_operation(self, state: str, label: str) -> None:
        self.signals = None
        self.set_busy(False)
        if self._operation_active:
            self._operation_active = False
            self.op_state_changed.emit(state, label)

    def _update_run_button(self, *_):
        self.verify_audio_button.setEnabled(not self.busy and any(ep.is_complete for ep in self.matrix_episodes))
        state = self.operation_state()
        self.run_button.setEnabled(not self.busy and state.ready)
        self._readiness_badge.setText(translate_text("Traitement en cours") if self.busy else
                                     translate_text("Prêt") if state.ready else translate_text("À configurer"))
        self.ready_changed.emit(state.ready and not self.busy)
        self._update_output_preview()

    def _on_sync_strategy_changed(self, *_):
        manual = self.sync_strategy.currentData() == "manual"
        self.manual_shift_label.setVisible(manual)
        self.manual_shift.setVisible(manual)
        automatic = self.sync_strategy.currentData() == "auto"
        self.scan_button.setText(translate_text("Analyser la saison") if automatic
                                 else translate_text("Préparer la saison"))
        self.detect_cuts.setEnabled(automatic and not self.busy)
        self.cadence_auto_apply.setEnabled(automatic and not self.busy)
        self.cadence_method_combo.setEnabled(automatic and not self.busy)

    def _settings_signature(self):
        control_values = tuple((key, control.isChecked() if isinstance(control, QCheckBox)
                                else control.value() if isinstance(control, QSpinBox)
                                else control.text() if isinstance(control, QLineEdit) else None)
                               for key, control in self.controls.items())
        return (
            self._source_signature(), self.output.text(), self.profile.text(), control_values,
            self.sync_strategy.currentData(), self.manual_shift.value(),
            self.mode.currentData(), self.mirror.isChecked(), self.detect_cuts.isChecked(),
            self.cadence_auto_apply.isChecked(), self.cadence_method_combo.currentData(),
            tuple(self.recipe.keep_master_audio_langs), tuple(self.recipe.donor_audio_langs),
            tuple(self.recipe.donor_sub_langs), self.recipe.keep_master_video,
            self.recipe.track_choices,
        )

    def _source_signature(self):
        return (self.reference.text(), self.donor.text(),
                tuple((s["edit"].text(), s["role"].currentData()) for s in self.extra_sources),
                self.matching_mode_combo.currentData())

    def _parse_preparation_args(self, output_dir: str | None = None):
        """Capture les options actuelles pour le lot comme pour une calibration individuelle."""
        from cli.parser import build_parser
        arguments = [
            "hybrid",
            "--ref-dir", self.reference.text(),
            "--donor-dir", self.donor.text(),
            "--output-dir", output_dir if output_dir is not None else self.output.text(),
            "--dry-run",
        ]
        if self.profile.text().strip():
            arguments += ["--profile", self.profile.text().strip()]
        if self.detect_cuts.isChecked():
            arguments += ["--detect-cuts"]
        for option, control in self.controls.items():
            if isinstance(control, QCheckBox):
                if option == "remove-closed-captions":
                    arguments.append("--remove-closed-captions" if control.isChecked() else "--no-remove-closed-captions")
                    continue
                if control.isChecked():
                    arguments.append("--" + option)
            elif isinstance(control, QSpinBox):
                arguments += ["--" + option, str(control.value())]
            elif isinstance(control, QLineEdit) and control.text().strip():
                if option == "auto-tmdb" and not control.text().strip().isdigit():
                    raise ValueError(translate_text("ID TMDB invalide."))
                arguments += ["--" + option, control.text().strip()]
        arguments += [
            "--sync-mode", self.mode.currentData(),
            "--sync-subtitles", "mirror" if self.mirror.isChecked() else "none",
        ]
        with redirect_stderr(StringIO()) as errors:
            try:
                args = build_parser().parse_args(arguments)
            except SystemExit as exc:
                # Une option invalide ne doit ni quitter l'application ni
                # laisser son verrou d'exécution actif.
                raise ValueError(errors.getvalue().strip().splitlines()[-1]) from exc
        args.cancel_event = self.cancel_event
        return args

    def verify_audio(self):
        """Recalibre toutes les paires du plan, y compris les lignes déjà préparées."""
        if self.busy or not any(ep.is_complete for ep in self.matrix_episodes):
            return
        if self._matrix_signature is not None and self._matrix_signature != self._source_signature():
            self.status.setText(translate_text("Les sources ont changé. Analysez à nouveau la saison."))
            return
        self.sync_strategy.setCurrentIndex(self.sync_strategy.findData("auto"))
        self.scan(reuse_matrix=True, verify_audio=True)

    def scan(self, auto_run: bool = False, reuse_matrix: bool = False, *, verify_audio: bool = False) -> bool:
        if self.busy:
            return False
        if not self._begin_operation(translate_text("Analyse des sources")):
            return False
        try:
            self._capture_preparation()
            if not reuse_matrix:
                self.matrix = self._build_matrix()
                self.matrix_episodes = self.matrix.scan()
            if not self.matrix_episodes:
                raise ValueError(translate_text("Aucun épisode trouvé dans les sources indiquées."))

        except Exception as exc:
            self.on_failed(str(exc))
            return False

        self.jobs.clear()
        self._pending_sync_rows.clear()
        self._prepare_failures.clear()
        self._defer_audio_sync = (self.recipe.sync_strategy == "auto" and not verify_audio
                                  and not auto_run and self._export_after_prepare is None)
        self._completed_jobs.clear()
        self._matrix_signature = self._source_signature()
        self._prepared_signature = self._settings_signature()
        self._run_after_prepare = auto_run
        self.stopped = False
        if not reuse_matrix:
            self.table.setRowCount(0)
            candidates = self.matrix.get_all_donor_candidates()
            for row, ep in enumerate(self.matrix_episodes):
                self._insert_table_row(row, ep, candidates)
        else:
            # Plans effacés : aucun ancien statut ne doit survivre à une annulation.
            for row, ep in enumerate(self.matrix_episodes):
                self._reset_row_status(row, ep)

        self.set_busy(True)
        self._start_preparation()
        return True

    def _capture_preparation(self) -> None:
        """Fige recette et options du lot depuis les contrôles (thread GUI)."""
        self.recipe.cadence_auto_apply = self.cadence_auto_apply.isChecked()
        self.recipe.cadence_audio_method = self.cadence_method_combo.currentData() or "auto"
        self.recipe.sync_mode = self.mode.currentData() or "physical"
        self.recipe.sync_subtitles = "mirror" if self.mirror.isChecked() else "none"
        self.recipe.sync_strategy = self.sync_strategy.currentData() or "auto"
        self.recipe.manual_shift_ms = self.manual_shift.value()
        if not self.output.text().strip():
            raise ValueError(translate_text("Choisir un dossier de sortie."))
        self.args = self._parse_preparation_args()
        self.recipe.crossfade_ms = self.args.crossfade_ms
        self.cancel_event.clear()
        self.args.cancel_event = self.cancel_event

    def _complete_missing_plans(self) -> bool:
        """Prépare seulement les paires sans plan ou à synchro différée ; les plans édités restent."""
        if not any(ep.is_complete and (row not in self.jobs or row in self._pending_sync_rows)
                   for row, ep in enumerate(self.matrix_episodes)):
            return False
        try:
            # Une calibration individuelle a pu changer les réglages depuis le dernier scan.
            self._capture_preparation()
        except Exception as exc:
            self.on_failed(str(exc))
            return True
        self._defer_audio_sync = False
        self._prepare_failures.clear()
        self.set_busy(True)
        self._start_preparation()
        return True

    def _insert_table_row(self, row: int, ep: MatrixEpisode,
                          candidates: list[tuple[MatrixSource, Path]] | None = None):
        if row >= self.table.rowCount():
            self.table.insertRow(row)

        item_ep = QTableWidgetItem(ep.display_name)
        self.table.setItem(row, 0, item_ep)

        # Colonne 1 : Master
        if ep.master_file:
            item_m = QTableWidgetItem(ep.master_file.name)
            self.table.setItem(row, 1, item_m)
        else:
            btn_choose = _secondary_button(translate_text("+ Choisir un master…"))
            btn_choose.clicked.connect(lambda checked=False, r=row: self._choose_master_row(r))
            self.table.setCellWidget(row, 1, btn_choose)

        # Colonne 2 : Donneur(s) assigné(s) (Combo interactif)
        # Listage des dossiers donneurs : une seule fois pour tout le tableau.
        all_candidates = self.matrix.get_all_donor_candidates() if candidates is None else candidates
        combo = DonorSelectCombo(row, ep, all_candidates, self._on_donor_reassigned, self.matrix, self)
        self.table.setCellWidget(row, 2, combo)

        # Colonne 3 : Synchro & Calibrage
        self._reset_row_status(row, ep)

        # Colonne 4 : Actions
        actions_w = QWidget()
        act_l = QHBoxLayout(actions_w)
        act_l.setContentsMargins(_scale(2), _scale(2), _scale(2), _scale(2))
        act_l.setSpacing(_scale(4))

        btn_calibrate = _secondary_button("", fixed_width=28)
        btn_calibrate.setIcon(_bolt_icon())
        btn_calibrate.setToolTip(translate_text("Préparer / Recalibrer cet élément individuellement"))
        btn_calibrate.clicked.connect(lambda checked=False, r=row: self.calibrate_single_row(r))
        act_l.addWidget(btn_calibrate)

        btn_listen = _secondary_button("▶", fixed_width=28)
        btn_listen.setToolTip(translate_text("Pré-écoute de cet élément"))
        btn_listen.clicked.connect(lambda checked=False, r=row: self.listen_row(r))
        act_l.addWidget(btn_listen)

        btn_remux = _secondary_button("↗", fixed_width=28)
        btn_remux.setToolTip(translate_text("Ouvrir cet élément dans le studio Remux"))
        btn_remux.clicked.connect(lambda checked=False, r=row: self.open_remux_row(r))
        act_l.addWidget(btn_remux)

        self.table.setCellWidget(row, 4, actions_w)

    def _reset_row_status(self, row: int, ep: MatrixEpisode):
        status_text = translate_text("Prêt pour analyse") if ep.is_complete else translate_text("Incomplet")
        status_color = QColor(_C.TEXT_SEC) if ep.is_complete else QColor(_C.WARN)
        has_cadence = ep.cadence_mismatch and getattr(ep.cadence_mismatch, "cadence_type", None) not in (None, "none")
        if has_cadence:
            status_text = f"⚠️ {status_text}"
        item_status = QTableWidgetItem(status_text)
        item_status.setForeground(status_color)
        if has_cadence:
            item_status.setToolTip(translate_text(
                "Différence de cadence détectée : {desc} — Conversion nécessaire",
                desc=ep.cadence_mismatch.description
            ))
        self.table.setItem(row, 3, item_status)

    def _on_donor_reassigned(self, row: int, ep: MatrixEpisode):
        self._pending_sync_rows.discard(row)
        if row in self.jobs:
            del self.jobs[row]
        item_status = self.table.item(row, 3)
        if item_status is not None:
            if ep.is_complete:
                item_status.setText(translate_text("Modifié (Prêt pour analyse)"))
                item_status.setForeground(QColor(_C.ACCENT))
            else:
                item_status.setText(translate_text("Donneur manquant"))
                item_status.setForeground(QColor(_C.WARN))
        self._update_run_button()
        self.status.setText(translate_text("Donneur mis à jour pour : {name}", name=ep.display_name))
        self.select_waveform()

    def _choose_master_row(self, row: int):
        if not 0 <= row < len(self.matrix_episodes):
            return
        ep = self.matrix_episodes[row]
        path, _ = QFileDialog.getOpenFileName(
            self,
            translate_text("Sélectionner le fichier Master (Référence)"),
            "",
            "Médias (*.mkv *.mp4 *.m4v *.avi *.ts *.m2ts *.webm);;Tous les fichiers (*)",
        )
        if path:
            ep.master_file = Path(path).resolve()
            if not ep.item_key:
                ep.item_key = ep.master_file.stem
            self.table.removeCellWidget(row, 1)
            item_m = QTableWidgetItem(ep.master_file.name)
            self.table.setItem(row, 1, item_m)

            item_name = self.table.item(row, 0)
            if item_name is not None:
                item_name.setText(ep.display_name)

            self._on_donor_reassigned(row, ep)

    def _add_manual_pair(self):
        m_path_str, _ = QFileDialog.getOpenFileName(
            self,
            translate_text("Sélectionner le fichier Master (Référence)"),
            "",
            "Médias (*.mkv *.mp4 *.m4v *.avi *.ts *.m2ts *.webm);;Tous les fichiers (*)",
        )
        if not m_path_str:
            return
        d_path_str, _ = QFileDialog.getOpenFileName(
            self,
            translate_text("Sélectionner le fichier Donneur"),
            "",
            "Médias (*.mkv *.mp4 *.m4v *.avi *.ts *.m2ts *.webm *.mka *.ac3 *.dts *.flac *.srt *.ass *.sup);;Tous les fichiers (*)",
        )
        if not d_path_str:
            return

        m_path = Path(m_path_str).resolve()
        d_path = Path(d_path_str).resolve()

        master_src = next((s for s in self.matrix.sources if s.role == SourceRole.MASTER), None)
        if not master_src:
            master_src = self.matrix.add_source(m_path.parent, role=SourceRole.MASTER, label="Master")
        donor_src = next((s for s in self.matrix.sources if s != master_src), None)
        if not donor_src:
            donor_src = self.matrix.add_source(d_path.parent, role=SourceRole.DONOR, label="Donneur")

        new_ep = MatrixEpisode(
            item_key=m_path.stem,
            master_file=m_path,
            donor_files=[(donor_src, d_path)],
            status="ready",
            status_message=translate_text("Prêt (1 donneur)"),
        )
        row = len(self.matrix_episodes)
        self.matrix_episodes.append(new_ep)
        self._matrix_signature = self._source_signature()
        self._insert_table_row(row, new_ep)
        self.table.selectRow(row)
        self._update_run_button()
        self.status.setText(translate_text("Nouvelle paire associée : {name}", name=new_ep.display_name))

    def calibrate_single_row(self, row: int):
        if self.busy or not 0 <= row < len(self.matrix_episodes):
            return
        ep = self.matrix_episodes[row]
        if not ep.is_complete:
            self.on_failed(translate_text("Impossible de calibrer : Master ou Donneur manquant pour cet élément."))
            return

        if self._matrix_signature is not None and self._matrix_signature != self._source_signature():
            # Le tableau décrit encore les anciennes sources : régénérer ses
            # plans avant de mémoriser une nouvelle signature de préparation.
            self.scan()
            return

        if not self._begin_operation(translate_text("Analyse de {name}", name=ep.display_name)):
            return
        signature = self._settings_signature()
        if self._prepared_signature != signature:
            self.jobs.clear()
            self._pending_sync_rows.clear()
            self._completed_jobs.clear()
        self.jobs.pop(row, None)
        self._completed_jobs.pop(row, None)
        self._prepared_signature = signature
        self._matrix_signature = self._source_signature()
        self.set_busy(True)
        item = self.table.item(row, 3)
        if item is not None:
            item.setText(translate_text("Analyse en cours…") if self.sync_strategy.currentData() == "auto"
                         else translate_text("Préparation en cours…"))
            item.setForeground(QColor(_C.ACCENT))

        recipe = replace(
            self.recipe,
            sync_mode=self.mode.currentData() or "physical",
            sync_subtitles="mirror" if self.mirror.isChecked() else "none",
            sync_strategy=self.sync_strategy.currentData() or "auto",
            manual_shift_ms=self.manual_shift.value(),
            cadence_auto_apply=self.cadence_auto_apply.isChecked(),
            cadence_audio_method=self.cadence_method_combo.currentData() or "auto",
        )

        # Widgets lus dans le thread GUI : le worker ne doit pas y toucher.
        output_dir = self.output.text().strip() or tempfile.gettempdir()
        detect_cuts = self.detect_cuts.isChecked()
        try:
            args = self._parse_preparation_args(output_dir)
            recipe.crossfade_ms = args.crossfade_ms
        except Exception as exc:
            self._on_single_calibration_failed(row, str(exc))
            return

        def task():
            try:
                config, calibration = prepare_matrix_episode(
                    ep,
                    recipe,
                    output_dir,
                    self.config,
                    args,
                    _GuiLogger(self.log_message, ep.display_name),
                    detect_cuts=detect_cuts,
                    drift_threshold_ms=getattr(args, "drift_threshold_ms", 25) or 25,
                    cache=self._prep_cache,
                )
                return config, calibration
            except Exception as exc:
                return exc

        def on_done(fut):
            if fut.cancelled():
                return
            res = fut.result()
            # Signaux : livraison en file vers le thread GUI (invokeMethod
            # n'accepte pas de callable sous PySide6).
            if isinstance(res, Exception):
                self.single_calibration_failed.emit(row, str(res))
            else:
                config, calibration = res
                self.single_calibration_done.emit(row, config, calibration)

        fut = self.executor.submit(task)
        fut.add_done_callback(on_done)

    def _on_single_calibration_done(self, row: int, config, calibration):
        if self.stopped:
            self._finish_operation("cancelled", translate_text("Analyse annulée"))
            return
        self.jobs[row] = (config, calibration)
        self._pending_sync_rows.discard(row)
        item = self.table.item(row, 3)
        if item is not None:
            if calibration and calibration.segments:
                txt = f"{calibration.segments[0].shift_ms:+.1f} ms · {len(calibration.segments)} seg"
                if calibration.cadence_mismatch and getattr(calibration.cadence_mismatch, "cadence_type", None) not in (None, "none"):
                    txt = f"⚠️ {txt}"
                    item.setToolTip(translate_text(
                        "Différence de cadence détectée : {desc} — Conversion nécessaire",
                        desc=calibration.cadence_mismatch.description,
                    ))
                item.setText(txt)
                color = QColor(_C.OK) if len(calibration.segments) == 1 else QColor(_C.WARN)
                item.setForeground(color)
            else:
                item.setText(translate_text("Sans synchro") if self.sync_strategy.currentData() == "none"
                             else translate_text("Prêt (0 ms)"))
                item.setForeground(QColor(_C.OK))
        self._update_run_button()
        self.status.setText(translate_text("Préparation terminée pour : {name}", name=self.matrix_episodes[row].display_name))
        self._finish_operation("finished", translate_text("Analyse terminée"))
        previously_blocked = self.table.blockSignals(True)
        try:
            self.table.selectRow(row)
        finally:
            self.table.blockSignals(previously_blocked)
        # Une ligne déjà sélectionnée n'émet pas itemSelectionChanged. Son
        # offset doit néanmoins suivre la nouvelle calibration, sans l'écraser.
        self.select_waveform()

    def _on_single_calibration_failed(self, row: int, err: str):
        item = self.table.item(row, 3)
        if item is not None:
            item.setText(translate_text("Échec"))
            item.setForeground(QColor(_C.ERROR))
        self.status.setText(translate_text("Erreur calibration : {err}", err=err))
        self.log_message.emit("WARN" if self.stopped else "ERROR", err)
        self._finish_operation("cancelled" if self.stopped else "failed", err)

    def listen_row(self, row: int):
        self.table.selectRow(row)
        self.listen()

    def open_remux_row(self, row: int):
        if not 0 <= row < len(self.matrix_episodes):
            return
        ep = self.matrix_episodes[row]
        if row in self.jobs:
            config, _ = self.jobs[row]
            self.open_in_remux.emit(config)
        else:
            self.open_in_remux.emit(ep.all_paths)

    def _start_preparation(self) -> None:
        """Met en file les paires sans plan (ou à synchro différée), puis remplit les postes."""
        self._prepare_generation += 1
        self._prepare_queue = []
        self._prepare_running.clear()
        for row, episode in enumerate(self.matrix_episodes):
            if row in self.jobs and (self._defer_audio_sync or row not in self._pending_sync_rows):
                continue
            if not episode.is_complete:
                item = self.table.item(row, 3)
                if item is not None:
                    item.setText(translate_text("Incomplet"))
                    item.setForeground(QColor(_C.WARN))
                continue
            self._prepare_queue.append(row)
        # Réglages figés pour tout le lot (widgets lus dans le thread GUI).
        self._prepare_calibrate = not self._defer_audio_sync
        self._prepare_settings = (replace(self.recipe), self.output.text().strip(),
                                  self.detect_cuts.isChecked(), self.args)
        self._fill_preparation()

    def _fill_preparation(self) -> None:
        """Lance les paires en attente dans la limite des postes ; termine le lot quand tout est rendu."""
        recipe = self._prepare_settings[0] if self._prepare_settings else self.recipe
        calibrating = self._prepare_calibrate and recipe.sync_strategy == "auto"
        limit = _CALIBRATE_WORKERS if calibrating else _PREPARE_WORKERS
        while self._prepare_queue and not self.stopped and len(self._prepare_running) < limit:
            self._submit_preparation(self._prepare_queue.pop(0), calibrating)
        if not self._prepare_running:
            self._finish_preparation()

    def _submit_preparation(self, row: int, calibrating: bool) -> None:
        episode = self.matrix_episodes[row]
        recipe, output_dir, detect_cuts, args = self._prepare_settings
        calibrate = self._prepare_calibrate
        self._prepare_running.add(row)
        item = self.table.item(row, 3)
        if item is not None:
            item.setText(translate_text("Analyse en cours…") if calibrating else translate_text("Préparation en cours…"))
        self.status.setText(translate_text(
            "{step} {current}/{total} : {name}",
            step=translate_text("Analyse synchro") if calibrating else translate_text("Préparation"),
            current=row + 1,
            total=len(self.matrix_episodes),
            name=episode.display_name,
        ))
        self.op_state_changed.emit("step", self.status.text())
        generation = self._prepare_generation

        def task():
            try:
                config, calibration = prepare_matrix_episode(
                    episode,
                    replace(recipe),
                    output_dir,
                    self.config,
                    args,
                    _GuiLogger(self.log_message, episode.display_name),
                    detect_cuts=detect_cuts,
                    drift_threshold_ms=getattr(args, "drift_threshold_ms", 25) or 25,
                    calibrate=calibrate,
                    cache=self._prep_cache,
                )
                self.prepared.emit(generation, row, config, calibration)
            except Exception as exc:
                self.failed.emit(generation, row, str(exc))

        self.executor.submit(task)

    def _finish_preparation(self) -> None:
        """Fin du lot : lancement, export ou proposition de vérification audio."""
        offer_sync = self._defer_audio_sync and bool(self._pending_sync_rows) and not self.stopped
        self._defer_audio_sync = False
        failed = [self.matrix_episodes[row].display_name for row in sorted(self._prepare_failures)
                  if row < len(self.matrix_episodes)]
        self._prepare_failures.clear()
        names = ", ".join(failed[:5]) + (" …" if len(failed) > 5 else "")
        self.set_busy(False)
        if self.stopped:
            self._export_after_prepare = None
            self._finish_operation("cancelled", translate_text("Hybridation annulée"))
        elif failed and (self._export_after_prepare is not None or self._run_after_prepare):
            # Jamais de lot partiel : chaque paire complète doit avoir son plan.
            self._export_after_prepare = None
            self._run_after_prepare = False
            message = translate_text("Préparation impossible pour {count} épisode(s) : {names}",
                                     count=len(failed), names=names)
            self.status.setText(message)
            self._finish_operation("failed", message)
        elif self._export_after_prepare is not None:
            directory = self._export_after_prepare
            self._export_after_prepare = None
            self._save_jobs(directory)
        elif self._run_after_prepare:
            self._run_after_prepare = False
            if self.jobs:
                self.run()
            else:
                self._finish_operation("failed", translate_text("Aucun plan d’hybridation prêt."))
        elif failed:
            message = translate_text("Analyse terminée : {count} épisode(s) en échec ({names}).",
                                     count=len(failed), names=names)
            self.status.setText(message)
            self.log_message.emit("WARN", message)
            self._finish_operation("failed", message)
        else:
            self._finish_operation("finished", translate_text("Analyse terminée"))
        # Analyse différée seulement (ni lancement ni export) : paires prêtes même si d'autres ont échoué.
        if offer_sync:
            answer = QMessageBox.question(
                self, translate_text("Vérifier la synchronisation audio"),
                translate_text("Analyse terminée. Voulez-vous lancer la vérification de concordance audio pour les {count} épisodes appariés ?",
                               count=len(self._pending_sync_rows)),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self.verify_audio()

    def _row_returned(self, generation: int, row: int) -> bool:
        """Libère le poste d'une ligne ; faux pour le résultat d'un lot précédent."""
        if generation != self._prepare_generation or row not in self._prepare_running:
            return False
        self._prepare_running.discard(row)
        if self.stopped:
            # Annulation : la ligne retrouve l'état de son plan (seules les
            # paires à synchro différée en ont un), sinon l'état sans plan.
            item = self.table.item(row, 3)
            if row in self.jobs and item is not None:
                item.setText(translate_text("Synchro audio à vérifier"))
                item.setForeground(QColor(_C.TEXT_SEC))
            elif row not in self.jobs:
                self._reset_row_status(row, self.matrix_episodes[row])
        return True

    def on_prepared(self, generation: int, row: int, config, calibration):
        if not self._row_returned(generation, row):
            return
        if not self.stopped:
            self.jobs[row] = (config, calibration)
            if self._defer_audio_sync:
                self._pending_sync_rows.add(row)
            else:
                self._pending_sync_rows.discard(row)
            item = self.table.item(row, 3)
            if item is not None:
                item.setToolTip("")
                if self._defer_audio_sync:
                    item.setText(translate_text("Synchro audio à vérifier"))
                    item.setForeground(QColor(_C.TEXT_SEC))
                elif calibration and calibration.segments:
                    txt = f"{calibration.segments[0].shift_ms:+.1f} ms · {len(calibration.segments)} seg"
                    if calibration.cadence_mismatch and getattr(calibration.cadence_mismatch, "cadence_type", None) not in (None, "none"):
                        txt = f"⚠️ {txt}"
                        item.setToolTip(translate_text(
                            "Différence de cadence détectée : {desc} — Conversion nécessaire",
                            desc=calibration.cadence_mismatch.description,
                        ))
                    item.setText(txt)
                    color = QColor(_C.OK) if len(calibration.segments) == 1 else QColor(_C.WARN)
                    item.setForeground(color)
                else:
                    item.setText(translate_text("Sans synchro") if self.recipe.sync_strategy == "none"
                                 else translate_text("Prêt (0 ms)"))
                    item.setForeground(QColor(_C.OK))
            self._update_run_button()
        self._fill_preparation()

    def _on_episode_failed(self, generation: int, row: int, message: str):
        """Échec d'un épisode : noté sur sa ligne, la préparation de la saison continue."""
        if not self._row_returned(generation, row):
            return
        if not self.stopped:
            self.jobs.pop(row, None)
            self._pending_sync_rows.discard(row)
            self._prepare_failures[row] = message
            item = self.table.item(row, 3)
            if item is not None:
                item.setText(translate_text("Échec"))
                item.setForeground(QColor(_C.ERROR))
                item.setToolTip(message)
            name = self.matrix_episodes[row].display_name if row < len(self.matrix_episodes) else ""
            self.log_message.emit("ERROR", f"[{name}] {message}")
            self._update_run_button()
        self._fill_preparation()

    def on_failed(self, message: str):
        self._defer_audio_sync = False
        self._prepare_failures.clear()
        self._run_after_prepare = False
        self._export_after_prepare = None
        self.status.setText(translate_text("Hybridation impossible : {err}", err=message))
        self.listen_button.setEnabled(True)
        self.aux_running = False
        self.log_message.emit("WARN" if self.stopped else "ERROR", message)
        self._finish_operation("cancelled" if self.stopped else "failed", message)

    def _retire_sync_editor(self):
        editor = self._sync_editor
        self._sync_editor = None
        if editor is not None:
            self._sync_content.removeWidget(editor)
            editor.hide()
            editor.close()

    def _sync_editor_closed(self, editor):
        if editor in self._sync_editors:
            self._sync_editors.remove(editor)
        editor.deleteLater()

    @staticmethod
    def _sync_track_label(track):
        return f"#{track.mkv_tid} · {track.language or 'und'} · {track.codec} {track.title}".strip()

    def _select_sync_episode(self, _index):
        row = self.sync_episode.currentData()
        if self.busy or row is None:
            return
        previously_blocked = self.table.blockSignals(True)
        try:
            self.table.selectRow(row)
        finally:
            self.table.blockSignals(previously_blocked)
        self.select_waveform()

    def select_waveform(self):
        if self.busy:
            return
        row = self.table.currentRow()
        ready_rows = [r for r in sorted(self.jobs) if 0 <= r < self.table.rowCount() and r not in self._pending_sync_rows]
        # Une analyse de saison ne sélectionne aucune ligne dans le tableau.
        # Ouvrir le premier plan prêt plutôt que laisser les pistes vides.
        if row < 0 and ready_rows:
            row = ready_rows[0]
            previously_blocked = self.table.blockSignals(True)
            try:
                self.table.selectRow(row)
            finally:
                self.table.blockSignals(previously_blocked)
        previously_blocked = self.sync_episode.blockSignals(True)
        try:
            self.sync_episode.clear()
            for ready_row in ready_rows:
                config, _ = self.jobs[ready_row]
                name = (self.matrix_episodes[ready_row].display_name if ready_row < len(self.matrix_episodes)
                        else config.sources[0].path.stem)
                self.sync_episode.addItem(name, ready_row)
            self.sync_episode.setCurrentIndex(self.sync_episode.findData(row))
            self.sync_episode.setEnabled(bool(ready_rows))
        finally:
            self.sync_episode.blockSignals(previously_blocked)
        self._update_output_preview()
        job = self.jobs.get(row) if row not in self._pending_sync_rows else None
        key = (row, id(job[0])) if job else None
        if key == self._sync_job_key and self._sync_editor is not None:
            return
        self._retire_sync_editor()
        self._sync_job_key = key
        self.sync_target.blockSignals(True)
        self.sync_reference.blockSignals(True)
        self.sync_target.clear()
        self.sync_reference.clear()
        if job:
            config, _ = job
            references = [t for t in config.sources[0].tracks if t.is_audio or t.is_subtitle]
            reference = next((t for t in references if t.is_audio and t.enabled), None)
            reference = reference or next((t for t in references if t.is_audio), None)
            for track in references:
                self.sync_reference.addItem(self._sync_track_label(track), track)
                if track is reference:
                    self.sync_reference.setCurrentIndex(self.sync_reference.count() - 1)
            preferred = None
            for source_index, source in enumerate(config.sources[1:], 1):
                for track in source.tracks:
                    if not (track.is_audio or track.is_subtitle):
                        continue
                    label = f"{source.path.name} · {self._sync_track_label(track)}"
                    self.sync_target.addItem(label, (source_index, track))
                    if preferred is None and track.enabled:
                        preferred = self.sync_target.count() - 1
                    # Même langue que le Master : le repère acoustique peut être
                    # une VO donneuse décochée dans le plan d'assemblage.
                    if source_index == 1 and reference is not None and track.is_audio and _language_selected(track.language, [reference.language]):
                        preferred = self.sync_target.count() - 1
            if preferred is not None:
                self.sync_target.setCurrentIndex(preferred)
        self.sync_target.setEnabled(self.sync_target.count() > 0)
        self.sync_reference.setEnabled(self.sync_reference.count() > 0)
        self.sync_target.blockSignals(False)
        self.sync_reference.blockSignals(False)
        self._show_sync_editor()

    def _show_sync_editor(self, *_args):
        if self.busy:
            return
        self._retire_sync_editor()
        row = self.table.currentRow()
        target = self.sync_target.currentData()
        if row not in self.jobs or row in self._pending_sync_rows or target is None:
            self.sync_hint.setText(translate_text(
                "Vérifiez la synchronisation audio pour ouvrir le Sync Studio." if row in self._pending_sync_rows else
                "Analysez la saison ou un élément du plan pour ouvrir le Sync Studio." if not self.jobs else
                "Choisissez un élément analysé ci-dessus ou analysez cet élément dans le plan." if row not in self.jobs else
                "Aucune piste audio ou sous-titre donneuse disponible pour cet élément."
            ))
            self.sync_hint.show()
            return
        from ui.panels.remux_panel.widgets.sync_studio_dialog import SyncStudioDialog
        config, primary = self.jobs[row]
        source_index, track = target
        source = config.sources[source_index]
        reference = self.sync_reference.currentData()
        calibration = config.sync_calibrations.get(str(source_index))
        if calibration is None:
            calibration = primary if source_index == 1 else SyncCalibration.linear(track.time_shift_ms)
        editor = SyncStudioDialog(
            target_entry=track, target_source_path=source.path, target_stream_index=track.mkv_tid,
            reference_entry=reference, reference_source_path=config.sources[0].path if reference else None,
            reference_stream_index=reference.mkv_tid if reference else None,
            calibration=calibration, ffmpeg_bin=self.config.tool_ffmpeg,
            ffprobe_bin=self.config.tool_ffprobe, crossfade_ms=config.crossfade_ms,
            parent=self, embedded=True,
        )
        editor.calibration_changed.connect(lambda cal: self._apply_studio_calibration(row, source_index, config, cal))
        editor.finished.connect(lambda _result: self._sync_editor_closed(editor))
        editor._preview_error.connect(lambda token, message: self.log_message.emit("WARN", message)
                                     if token == editor._preview_token else None)
        self._sync_editor = editor
        self._sync_editors.append(editor)
        self._sync_content.addWidget(editor)
        self.sync_hint.hide()
        self._wave_section.set_expanded(True)
        editor.show()
        if self.sync_strategy.currentData() == "none":
            editor.spin_shift.setEnabled(False)

    def _apply_studio_calibration(self, row, source_index, config, calibration):
        if self.busy or row not in self.jobs or self.jobs[row][0] is not config:
            return
        if len(calibration.segments) > 1:
            config.sync_mode = "physical"
        offset = round(calibration.segments[0].shift_ms)
        payload = calibration.to_dict()
        physical = config.sync_mode == "physical"
        if physical:
            config.sync_calibrations[str(source_index)] = payload
        else:
            config.sync_calibrations.pop(str(source_index), None)
        for track in config.sources[source_index].tracks:
            if track.is_audio or (track.is_subtitle and config.sync_subtitles == "mirror"):
                track.time_shift_ms = 0 if physical else offset
                track.sync_calibration = payload if physical else None
        primary = calibration if source_index == 1 else self.jobs[row][1]
        self.jobs[row] = (config, primary)
        self._completed_jobs.pop(row, None)
        if source_index == 1 and row < len(self.matrix_episodes):
            episode = self.matrix_episodes[row]
            episode.calibration = calibration
            episode.shift_ms = calibration.segments[0].shift_ms
            episode.segments_count = len(calibration.segments)
        item = self.table.item(row, 3)
        if item is not None:
            item.setText(f"{calibration.segments[0].shift_ms:+.1f} ms · {len(calibration.segments)} seg")
        self.log_message.emit("INFO", translate_text(
            "Synchronisation mise à jour : {source} ({count} segments).",
            source=config.sources[source_index].path.name, count=len(calibration.segments),
        ))

    def listen(self):
        if self.busy:
            return
        self.select_waveform()
        if self.table.currentRow() in self._pending_sync_rows:
            self.status.setText(translate_text("Vérifiez la synchronisation audio pour ouvrir le Sync Studio."))
            return
        if self.table.currentRow() not in self.jobs:
            self.status.setText(translate_text("Veuillez d'abord analyser ou calibrer cet élément (bouton ⚡)."))
            return
        if self._sync_editor is not None:
            self._sync_editor._toggle_listen()
        else:
            self.status.setText(translate_text("Aucune piste audio donneuse disponible pour la pré-écoute."))

    def export_jobs(self):
        if self.busy:
            return
        directory = QFileDialog.getExistingDirectory(self, translate_text("Sauvegarder les workflows…"))
        if not directory:
            return
        if self._prepared_signature != self._settings_signature():
            self._export_after_prepare = Path(directory)
            started = self.scan(reuse_matrix=bool(
                self.matrix_episodes and self._matrix_signature == self._source_signature()
            ))
            if not started:
                self._export_after_prepare = None
            return
        if any(ep.is_complete and (row not in self.jobs or row in self._pending_sync_rows)
               for row, ep in enumerate(self.matrix_episodes)):
            if not self._begin_operation(translate_text("Analyse des sources")):
                return
            self._export_after_prepare = Path(directory)
            self._complete_missing_plans()
            return
        self._save_jobs(Path(directory))

    def _save_jobs(self, directory: Path):
        from cli.batch import PlannedBatchJob, assert_unique_batch_outputs
        from core.profiles.selectors import remux_config_to_exact_job
        from core.workflows.workflow_store import save_workflow

        try:
            if not self.jobs:
                raise ValueError(translate_text("Aucun plan d’hybridation prêt."))
            # Valider tout le lot avant d'écrire le premier workflow.
            assert_unique_batch_outputs([
                PlannedBatchJob(row, str(config.sources[0].path), str(config.output), remux_config=config)
                for row, (config, _) in sorted(self.jobs.items())
            ])
            payloads = [(directory / (config.output.stem + ".exact-job.json"), remux_config_to_exact_job(config))
                        for config, _ in self.jobs.values()]
            if len({path for path, _ in payloads}) != len(payloads):
                raise ValueError(translate_text("Plusieurs workflows portent le même nom."))
            for path, payload in payloads:
                save_workflow(path, payload)
        except Exception as exc:
            self.on_failed(str(exc))
            return
        message = translate_text("Workflows sauvegardés : {count}", count=len(self.jobs))
        self.status.setText(message)
        self.log_message.emit("INFO", message)
        self._finish_operation("finished", message)

    def run(self):
        if self.busy:
            return
        if not self._begin_operation(translate_text("Préparation de l’hybridation")):
            return
        if self._prepared_signature != self._settings_signature():
            self.scan(auto_run=True, reuse_matrix=bool(
                self.matrix_episodes and self._matrix_signature == self._source_signature()
            ))
            return
        self._run_after_prepare = True
        if self._complete_missing_plans():
            return
        self._run_after_prepare = False
        if not self.jobs:
            self._finish_operation("failed", translate_text("Aucun plan d’hybridation prêt."))
            return
        self.execution_rows = [row for row in sorted(self.jobs) if not self._job_already_completed(row)]
        try:
            from cli.batch import PlannedBatchJob, assert_unique_batch_outputs
            assert_unique_batch_outputs([
                PlannedBatchJob(row, str(config.sources[0].path), str(config.output), remux_config=config)
                for row, (config, _calibration) in sorted(self.jobs.items())
            ])
            for row in self.execution_rows:
                output = self.jobs[row][0].output
                if output.exists():
                    raise ValueError(translate_text("La sortie existe déjà : {path}", path=str(output)))
        except Exception as exc:
            self.on_failed(str(exc))
            return
        self.exec_index = 0
        self.stopped = False
        self.set_busy(True)
        self.run_next()

    @staticmethod
    def _output_identity(path: Path) -> tuple[int, int, int, int] | None:
        try:
            if not path.is_file():
                return None
            stat = path.stat()
            return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns
        except OSError:
            return None

    def _job_already_completed(self, row: int) -> bool:
        previous = self._completed_jobs.get(row)
        return bool(previous is not None and previous[0] == self.jobs[row]
                    and previous[1] == self._output_identity(self.jobs[row][0].output))

    def run_next(self):
        from core.workflows.remux import RemuxWorkflow

        if self.stopped or self.exec_index >= len(self.execution_rows):
            self._finish_operation("cancelled" if self.stopped else "finished",
                                   translate_text("Hybridation annulée" if self.stopped else "Hybridation terminée"))
            return

        row = self.execution_rows[self.exec_index]
        config, _ = self.jobs[row]
        if config.output.exists():
            self.on_failed(translate_text("La sortie existe déjà : {path}", path=str(config.output)))
            return
        try:
            config.output.parent.mkdir(parents=True, exist_ok=True)
            self.workflow = RemuxWorkflow(
                ffmpeg_bin=str(self.config.tool_ffmpeg),
                ffprobe_bin=str(self.config.tool_ffprobe),
                mediainfo_bin=str(self.config.tool_mediainfo),
            )
        except Exception as exc:
            self.on_failed(str(exc))
            return
        episode = self.matrix_episodes[row].display_name if row < len(self.matrix_episodes) else config.output.stem
        self._active_episode = episode
        self.op_state_changed.emit("step", translate_text("Épisode {current}/{total} · {name}",
                                   current=self.exec_index + 1, total=len(self.execution_rows), name=episode))
        self.workflow.log_message.connect(
            lambda level, message, name=episode: self.log_message.emit(level, f"[{name}] {message}"),
            Qt.ConnectionType.QueuedConnection,
        )
        try:
            self.signals = self.workflow.run(replace(config, allow_missing_output_dir=False))
            self.signals.connect_terminal(
                finished=self.on_finished,
                failed=lambda message, exc: self.on_failed(message),
                cancelled=lambda: self._finish_operation("cancelled", translate_text("Hybridation annulée")),
            )
            self.signals.progress.connect(self.op_progress, Qt.ConnectionType.QueuedConnection)
            self.signals.progress_pct.connect(self.op_progress_pct, Qt.ConnectionType.QueuedConnection)
        except Exception as exc:
            self.on_failed(str(exc))

    def on_finished(self, message: str):
        if self.exec_index < len(self.execution_rows):
            row = self.execution_rows[self.exec_index]
            identity = self._output_identity(self.jobs[row][0].output)
            if identity is not None:
                self._completed_jobs[row] = (copy.deepcopy(self.jobs[row]), identity)
            item = self.table.item(row, 3)
            if item is not None:
                item.setText(translate_text("Terminé"))
                item.setForeground(QColor(_C.OK))
        self.log_message.emit("OK", f"[{getattr(self, '_active_episode', '')}] {message}")
        self.signals = None
        self.exec_index += 1
        self.run_next()

    def cancel(self):
        self.stopped = True
        self._run_after_prepare = False
        self._export_after_prepare = None
        self.cancel_event.set()
        if self.signals is not None:
            self.signals.cancel()

    def closeEvent(self, event):
        if not hasattr(self, "_shutdown"):
            self.setEnabled(False)
            self.stopped = True
            self.cancel_event.set()
            for editor in list(self._sync_editors):
                editor.close()
            self._shutdown = Shutdown(executors=(self.executor, *(e._executor for e in self._sync_editors)), tasks=(self.signals,))
        if defer_close(self, event, ready=self._shutdown.done.is_set()):
            return
        super().closeEvent(event)
