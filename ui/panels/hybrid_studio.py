"""Studio d'hybridation : appariement par lot, calibration acoustique et synchronisation physique."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import uuid

from typing import Any, Callable

from PySide6.QtCore import Qt, QUrl, Signal, QMetaObject
from PySide6.QtGui import QColor, QCursor, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.i18n import translate_text
from core.workflows.audio_sync import AudioSyncTrack
from core.workflows.audio_sync_scan import AudioSyncScanner
from core.workflows.sync_calibration import SyncSegment
from core.workflows.hybrid_matrix import (
    HybridMatrix,
    HybridRecipe,
    MatchingMode,
    MatrixEpisode,
    MatrixSource,
    SourceRole,
    prepare_matrix_episode,
)
from ui.design_system import colors as _C, font_px as _font_px, scale as _scale
from ui.styles import _checkbox_style, _groupbox_checkable_style


# =============================================================================
# Helpers de style Design System
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
    btn.setFixedHeight(_scale(32))
    btn.setStyleSheet(f"""
        QPushButton {{
            background: {_C.ACCENT};
            color: #ffffff;
            border: none;
            border-radius: {_scale(5)}px;
            font-size: {_font_px(11)}px;
            font-weight: 700;
            padding: 0 {_scale(16)}px;
        }}
        QPushButton:hover {{
            background: {_C.ACCENT_HOVER if hasattr(_C, 'ACCENT_HOVER') else '#6070f8'};
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


from ui.widgets.waveform_view import WaveformView


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
                    self.combo.addItem(f"Profil : {name}", name)
        except Exception:
            pass

        if self._custom_path:
            label = f"Fichier : {Path(self._custom_path).name}"
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
            if data == target or self.combo.itemText(i) == target or f"Profil : {target}" == self.combo.itemText(i):
                self.combo.setCurrentIndex(i)
                return
        self._custom_path = target
        self.combo.addItem(f"Fichier : {Path(target).name}", target)
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

class HybridStudio(QWidget):
    prepared = Signal(object, object)
    failed = Signal(str)
    waveform_ready = Signal(object)
    preview_ready = Signal(str)
    log_message = Signal(str, str)
    open_in_remux = Signal(object)
    witness_inspected = Signal(object)

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.setWindowTitle(translate_text("Studio Hybridation"))
        self.executor = ThreadPoolExecutor(max_workers=1)
        self._executor = self.executor
        self.jobs: dict[int, tuple[Any, Any]] = {}
        self.execution_rows: list[int] = []
        self.exec_index: int = 0
        self.matrix = HybridMatrix()
        self.matrix_episodes: list[MatrixEpisode] = []
        self.recipe = HybridRecipe()
        self.extra_sources: list[dict] = []
        self.witness_track_items: list[dict] = []
        self.index = 0
        self.stopped = False
        self.cancel_event = threading.Event()
        self.signals = None
        self.busy = False
        self.aux_running = False
        self.preview_temp = tempfile.TemporaryDirectory(prefix="Muxiveo_listen_")
        self._build_ui()

    def _build_ui(self) -> None:
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(_scale(16), _scale(16), _scale(16), _scale(16))
        outer_layout.setSpacing(_scale(12))

        # En-tête de section
        header_row = QHBoxLayout()
        title_lbl = QLabel(translate_text("Studio Hybridation & Mixage Multi-Sources"))
        title_lbl.setStyleSheet(f"""
            color: {_C.TEXT_PRI};
            font-size: {_font_px(15)}px;
            font-weight: 700;
            background: transparent;
        """)
        header_row.addWidget(title_lbl)
        header_row.addStretch()
        outer_layout.addLayout(header_row)

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
        cd_layout = QVBoxLayout(card_dirs)
        cd_layout.setContentsMargins(_scale(12), _scale(10), _scale(12), _scale(10))
        cd_layout.setSpacing(_scale(8))
        cd_layout.addWidget(_section_label(translate_text("DOSSIERS SOURCES ET SORTIE")))

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
        cd_layout.addWidget(self.add_source_btn)

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

        profile_row = QHBoxLayout()
        self.profile = ProfileSelector(self.config.profiles_dir, self)
        lbl_prof = QLabel(translate_text("Profil décisionnel (optionnel) :"))
        lbl_prof.setStyleSheet(f"color: {_C.TEXT_SEC}; font-size: {_font_px(11)}px;")
        profile_row.addWidget(lbl_prof)
        profile_row.addWidget(self.profile, stretch=1)
        cd_layout.addLayout(profile_row)

        self.detect_cuts = QCheckBox(
            translate_text("Détecter les coupures et variations de cadences (cuts multi-segments)")
        )
        self.detect_cuts.setChecked(True)
        self.detect_cuts.setStyleSheet(_checkbox_style())
        cd_layout.addWidget(self.detect_cuts)

        cadence_row = QHBoxLayout()
        self.cadence_auto_apply = QCheckBox(
            translate_text("Appliquer automatiquement la conversion PAL ↔ Cinéma si détectée")
        )
        auto_cadence_default = getattr(self.config, "sync_cadence_auto_apply", True)
        self.cadence_auto_apply.setChecked(bool(auto_cadence_default))
        self.cadence_auto_apply.setStyleSheet(_checkbox_style())
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
        cd_layout.addLayout(cadence_row)

        layout.addWidget(card_dirs)

        # ── Carte 1 bis : Plan d'assemblage type (Épisode témoin) ─────────────
        card_witness = _card(self)
        cwit_layout = QVBoxLayout(card_witness)
        cwit_layout.setContentsMargins(_scale(12), _scale(10), _scale(12), _scale(10))
        cwit_layout.setSpacing(_scale(8))

        wit_header = QHBoxLayout()
        wit_header.addWidget(_section_label(translate_text("PLAN D'ASSEMBLAGE TYPE (ÉPISODE TÉMOIN)")))
        wit_header.addStretch()
        self.inspect_witness_btn = _secondary_button(translate_text("🔍 Inspecter l'épisode témoin"))
        self.inspect_witness_btn.clicked.connect(self.inspect_witness)
        wit_header.addWidget(self.inspect_witness_btn)
        cwit_layout.addLayout(wit_header)

        self.witness_info = QLabel(translate_text("Choisissez vos dossiers sources ci-dessus, puis inspectez l'épisode témoin pour choisir les pistes à mixer."))
        self.witness_info.setStyleSheet(f"color: {_C.TEXT_SEC}; font-size: {_font_px(11)}px;")
        cwit_layout.addWidget(self.witness_info)

        self.witness_table = QTableWidget(0, 5)
        self.witness_table.setStyleSheet(_table_style())
        self.witness_table.setHorizontalHeaderLabels([
            translate_text("Conserver"),
            translate_text("Source"),
            translate_text("Type"),
            translate_text("Langue"),
            translate_text("Format / Détails"),
        ])
        self.witness_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.witness_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.witness_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.witness_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.witness_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.witness_table.setMinimumHeight(_scale(110))
        cwit_layout.addWidget(self.witness_table)

        wit_actions = QHBoxLayout()
        self.apply_recipe_btn = _primary_button(translate_text("🪄 Appliquer ce modèle à toute la saison"))
        self.apply_recipe_btn.setEnabled(False)
        self.apply_recipe_btn.clicked.connect(self.apply_witness_recipe)
        wit_actions.addWidget(self.apply_recipe_btn)

        self.recipe_status_lbl = QLabel(translate_text("Modèle actuel : Règles automatiques"))
        self.recipe_status_lbl.setStyleSheet(f"color: {_C.TEXT_SEC}; font-size: {_font_px(11)}px;")
        wit_actions.addWidget(self.recipe_status_lbl)
        wit_actions.addStretch()
        cwit_layout.addLayout(wit_actions)

        layout.addWidget(card_witness)

        # ── Carte 2 : Options d'hybridation (dépliables) ───────────────────────
        advanced = QGroupBox(translate_text("Options d'hybridation avancées"))
        advanced.setStyleSheet(_groupbox_checkable_style())
        settings = QFormLayout(advanced)
        settings.setSpacing(_scale(6))
        self.controls = {}
        for option, label, default in (
            ("auto-forced-subs", "Détecter les sous-titres forcés", False),
            ("auto-sdh", "Détecter les sous-titres SDH", False),
            ("no-clean-nfo", "Conserver le chemin complet dans le NFO", False),
            ("no-cover", "Sans jaquette TMDB", False),
        ):
            control = QCheckBox(translate_text(label))
            control.setChecked(default)
            control.setStyleSheet(_checkbox_style())
            settings.addRow(control)
            self.controls[option] = control

        for option, label, value, maximum in (
            ("forced-threshold", "Seuil forcés", 50, 10000),
            ("crossfade-ms", "Fondu aux raccords (ms)", 80, 1000),
            ("drift-threshold-ms", "Seuil de dérive (ms)", 25, 10000),
        ):
            control = QSpinBox()
            control.setStyleSheet(_input_style())
            control.setRange(0 if option == "crossfade-ms" else 1, maximum)
            control.setValue(value)
            lbl_spin = QLabel(translate_text(label))
            lbl_spin.setStyleSheet(f"color: {_C.TEXT_SEC}; font-size: {_font_px(11)}px;")
            settings.addRow(lbl_spin, control)
            self.controls[option] = control

        for option, label in (
            ("auto-tmdb", "ID TMDB"),
            ("tag", "Groupe de release"),
            ("output-template", "Template de sortie"),
            ("calibration", "Fichier de calibration"),
        ):
            control = QLineEdit("MVO" if option == "tag" else "")
            control.setStyleSheet(_input_style())
            lbl_txt = QLabel(translate_text(label))
            lbl_txt.setStyleSheet(f"color: {_C.TEXT_SEC}; font-size: {_font_px(11)}px;")
            settings.addRow(lbl_txt, control)
            self.controls[option] = control

        self.mode = QComboBox()
        self.mode.setStyleSheet(_input_style())
        self.mode.addItem(translate_text("Synchronisation physique"), "physical")
        self.mode.addItem(translate_text("Décalage conteneur"), "container")
        lbl_mode = QLabel(translate_text("Mode de synchro"))
        lbl_mode.setStyleSheet(f"color: {_C.TEXT_SEC}; font-size: {_font_px(11)}px;")
        settings.addRow(lbl_mode, self.mode)

        self.mirror = QCheckBox(translate_text("Recaler les sous-titres"))
        self.mirror.setChecked(True)
        self.mirror.setStyleSheet(_checkbox_style())
        settings.addRow(self.mirror)

        advanced.setCheckable(True)
        advanced.setChecked(False)
        advanced.toggled.connect(lambda checked: [
            settings.itemAt(i).widget().setVisible(checked)
            for i in range(settings.count()) if settings.itemAt(i).widget()
        ])
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
        ct_layout.addLayout(ct_header)

        self.table = QTableWidget(0, 5)
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
        self.table.setMinimumHeight(_scale(160))
        self.table.itemSelectionChanged.connect(self.select_waveform)
        self.table.cellDoubleClicked.connect(lambda row, col: self.open_remux_row(row))
        ct_layout.addWidget(self.table)
        layout.addWidget(card_table)

        # ── Carte 4 : Forme d'onde et micro-ajustement ─────────────────────────
        card_wave = _card(self)
        cw_layout = QVBoxLayout(card_wave)
        cw_layout.setContentsMargins(_scale(12), _scale(10), _scale(12), _scale(10))
        cw_layout.setSpacing(_scale(8))

        wave_header = QHBoxLayout()
        wave_header.addWidget(_section_label(translate_text("ANALYSE ACOUSTIQUE ET CALIBRATION")))
        wave_header.addStretch()

        adj_label = QLabel(translate_text("Ajustement manuel :"))
        adj_label.setStyleSheet(f"color: {_C.TEXT_SEC}; font-size: {_font_px(11)}px;")
        wave_header.addWidget(adj_label)

        self.adjust = QDoubleSpinBox()
        self.adjust.setStyleSheet(_input_style())
        self.adjust.setRange(-60000, 60000)
        self.adjust.setDecimals(1)
        self.adjust.setSuffix(" ms")
        self.adjust.valueChanged.connect(self.adjust_offset)
        wave_header.addWidget(self.adjust)
        cw_layout.addLayout(wave_header)

        self.waveform = WaveformView(self)
        cw_layout.addWidget(self.waveform)
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

        outer_layout.addLayout(bar)

        self.status = QLabel()
        self.status.setStyleSheet(f"color: {_C.TEXT_SEC}; font-size: {_font_px(11)}px;")
        outer_layout.addWidget(self.status)

        self.prepared.connect(self.on_prepared)
        self.failed.connect(self.on_failed)
        self.waveform_ready.connect(self.on_waveform)
        self.preview_ready.connect(self.play_preview)
        self.witness_inspected.connect(self._populate_witness_table)

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

        def remove_row():
            if source_info in self.extra_sources:
                self.extra_sources.remove(source_info)
            row_widget.deleteLater()

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

        def task():
            from core.inspector import FileInspector
            inspector = FileInspector(str(self.config.tool_ffprobe), str(self.config.tool_mediainfo))
            sources_tracks = []
            try:
                master_info = inspector.inspect(witness_ep.master_file)
                sources_tracks.append(("Master", "master", master_info))
                for d_src, d_path in witness_ep.donor_files:
                    d_info = inspector.inspect(d_path)
                    sources_tracks.append((d_src.label or "Donneur", "donor", d_info))
                return sources_tracks
            except Exception as exc:
                return exc

        def on_done(future):
            res = future.result()
            self.witness_inspected.emit(res)

        fut = self.executor.submit(task)
        fut.add_done_callback(on_done)

    def _populate_witness_table(self, res):
        if isinstance(res, Exception):
            self.witness_info.setText(translate_text("Erreur d'inspection : {err}", err=str(res)))
            return

        self.witness_table.setRowCount(0)
        self.witness_track_items = []
        row = 0
        for src_label, role, file_info in res:
            for stream in file_info.streams:
                self.witness_table.insertRow(row)

                chk = QCheckBox()
                st_type = stream.stream_type.casefold()
                is_master = (role == "master")
                lang = (stream.language or "").casefold()

                if st_type == "video":
                    checked = is_master
                elif st_type == "audio":
                    if is_master:
                        checked = lang in {"eng", "jpn", "orig"} or not any(s["role"] == "donor" for s in self.witness_track_items if s["type"] == "audio")
                    else:
                        checked = lang in {"fre", "fra"} or True
                elif st_type == "subtitle":
                    checked = (not is_master) and (lang in {"fre", "fra"} or True)
                else:
                    checked = False

                chk.setChecked(checked)
                chk.setStyleSheet(_checkbox_style())
                self.witness_table.setCellWidget(row, 0, chk)

                item_src = QTableWidgetItem(src_label)
                item_src.setForeground(QColor(_C.ACCENT if is_master else _C.OK))
                self.witness_table.setItem(row, 1, item_src)

                item_type = QTableWidgetItem(stream.stream_type.capitalize())
                self.witness_table.setItem(row, 2, item_type)

                item_lang = QTableWidgetItem(stream.language or "-")
                self.witness_table.setItem(row, 3, item_lang)

                details = f"{stream.codec_name or ''} {stream.display_title or ''}".strip()
                item_details = QTableWidgetItem(details)
                self.witness_table.setItem(row, 4, item_details)

                self.witness_track_items.append({
                    "checkbox": chk,
                    "role": role,
                    "type": st_type,
                    "lang": stream.language or "",
                    "codec": stream.codec_name or "",
                })
                row += 1

        self.apply_recipe_btn.setEnabled(True)
        self.witness_info.setText(translate_text("Pistes détectées sur l'épisode témoin. Cochez celles à conserver puis appliquez le modèle."))

    def apply_witness_recipe(self):
        if not self.witness_track_items:
            return

        recipe = HybridRecipe()
        master_audio_langs = []
        donor_audio_langs = []
        donor_sub_langs = []
        keep_video = False

        for item in self.witness_track_items:
            if not item["checkbox"].isChecked():
                continue
            role = item["role"]
            st_type = item["type"]
            lang = item["lang"]

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

        recipe.keep_master_video = keep_video
        recipe.keep_master_audio_langs = master_audio_langs or ["eng", "jpn"]
        recipe.donor_audio_langs = donor_audio_langs or ["fre"]
        recipe.donor_sub_langs = donor_sub_langs or ["fre"]
        recipe.sync_mode = self.mode.currentData() or "physical"
        recipe.sync_subtitles = "mirror" if self.mirror.isChecked() else "none"
        recipe.cadence_auto_apply = self.cadence_auto_apply.isChecked()
        recipe.cadence_audio_method = self.cadence_method_combo.currentData() or "auto"
        self.recipe = recipe

        summary = f"Modèle actif : Vidéo Master ({'Oui' if keep_video else 'Non'}), "
        summary += f"Audio Donneur : [{', '.join(recipe.donor_audio_langs)}], "
        summary += f"Audio Master : [{', '.join(recipe.keep_master_audio_langs)}], "
        summary += f"ST Donneur : [{', '.join(recipe.donor_sub_langs)}]"
        self.recipe_status_lbl.setText(summary)
        self.recipe_status_lbl.setStyleSheet(f"color: {_C.OK}; font-size: {_font_px(11)}px; font-weight: bold;")
        self.status.setText(translate_text("Modèle appliqué à la saison. Vous pouvez lancer l'analyse ou l'hybridation."))

    def browse(self, field: QLineEdit):
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
        path = QFileDialog.getExistingDirectory(self, translate_text("Choisir un dossier de sortie"), field.text())
        if path:
            field.setText(path)

    def set_busy(self, busy: bool):
        self.busy = busy
        self.scan_button.setEnabled(not busy)
        self.run_button.setEnabled(not busy and bool(self.jobs))
        for field in (
            self.reference,
            self.donor,
            self.output,
            self.matching_mode_combo,
            self.profile,
            self.detect_cuts,
            self.cadence_auto_apply,
            self.cadence_method_combo,
            self.mode,
            self.mirror,
            *self.controls.values(),
        ):
            field.setEnabled(not busy)

    def scan(self):
        from cli.parser import build_parser

        arguments = [
            "hybrid",
            "--ref-dir", self.reference.text(),
            "--donor-dir", self.donor.text(),
            "--output-dir", self.output.text(),
            "--dry-run",
        ]
        if self.profile.text().strip():
            arguments += ["--profile", self.profile.text().strip()]
        if self.detect_cuts.isChecked():
            arguments += ["--detect-cuts"]
        for option, control in self.controls.items():
            if isinstance(control, QCheckBox):
                if control.isChecked():
                    arguments.append("--" + option)
            elif isinstance(control, QSpinBox):
                arguments += ["--" + option, str(control.value())]
            elif control.text().strip():
                if option == "auto-tmdb" and not control.text().strip().isdigit():
                    self.on_failed(translate_text("ID TMDB invalide."))
                    return
                arguments += ["--" + option, control.text().strip()]
        arguments += [
            "--sync-mode", self.mode.currentData(),
            "--sync-subtitles", "mirror" if self.mirror.isChecked() else "none",
        ]
        self.recipe.cadence_auto_apply = self.cadence_auto_apply.isChecked()
        self.recipe.cadence_audio_method = self.cadence_method_combo.currentData() or "auto"
        try:
            if not self.output.text().strip():
                raise ValueError(translate_text("Choisir un dossier de sortie."))
            self.args = build_parser().parse_args(arguments)
            self.cancel_event.clear()
            self.args.cancel_event = self.cancel_event

            self.matrix = self._build_matrix()
            self.matrix_episodes = self.matrix.scan()
            if not self.matrix_episodes:
                raise ValueError(translate_text("Aucun épisode trouvé dans les sources indiquées."))

        except Exception as exc:
            self.on_failed(str(exc))
            return

        self.jobs.clear()
        self.index, self.stopped = 0, False
        self.table.setRowCount(0)
        for row, ep in enumerate(self.matrix_episodes):
            self._insert_table_row(row, ep)

        if self.witness_table.rowCount() == 0:
            self.inspect_witness()

        self.set_busy(True)
        self.prepare_next()

    def _insert_table_row(self, row: int, ep: MatrixEpisode):
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
        all_candidates = self.matrix.get_all_donor_candidates()
        combo = DonorSelectCombo(row, ep, all_candidates, self._on_donor_reassigned, self.matrix, self)
        self.table.setCellWidget(row, 2, combo)

        # Colonne 3 : Synchro & Calibrage
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

        # Colonne 4 : Actions
        actions_w = QWidget()
        act_l = QHBoxLayout(actions_w)
        act_l.setContentsMargins(_scale(2), _scale(2), _scale(2), _scale(2))
        act_l.setSpacing(_scale(4))

        btn_calibrate = _secondary_button("⚡", fixed_width=28)
        btn_calibrate.setToolTip(translate_text("Analyser / Recalibrer cet élément individuellement"))
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

    def _on_donor_reassigned(self, row: int, ep: MatrixEpisode):
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
        self.run_button.setEnabled(not self.busy and bool(self.jobs))
        self.status.setText(translate_text("Donneur mis à jour pour : {name}", name=ep.display_name))

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
        self._insert_table_row(row, new_ep)
        self.table.selectRow(row)
        self.status.setText(translate_text("Nouvelle paire associée : {name}", name=new_ep.display_name))

    def calibrate_single_row(self, row: int):
        if self.busy or not 0 <= row < len(self.matrix_episodes):
            return
        ep = self.matrix_episodes[row]
        if not ep.is_complete:
            self.on_failed(translate_text("Impossible de calibrer : Master ou Donneur manquant pour cet élément."))
            return

        item = self.table.item(row, 3)
        if item is not None:
            item.setText(translate_text("Analyse en cours…"))
            item.setForeground(QColor(_C.ACCENT))

        self.recipe.cadence_auto_apply = self.cadence_auto_apply.isChecked()
        self.recipe.cadence_audio_method = self.cadence_method_combo.currentData() or "auto"

        def task():
            from cli.logging import Logger
            try:
                config, calibration = prepare_matrix_episode(
                    ep,
                    self.recipe,
                    self.output.text().strip() or tempfile.gettempdir(),
                    self.config,
                    self.args if hasattr(self, "args") else None,
                    Logger(),
                    detect_cuts=self.detect_cuts.isChecked(),
                    drift_threshold_ms=25,
                )
                return config, calibration
            except Exception as exc:
                return exc

        def on_done(fut):
            res = fut.result()
            if isinstance(res, Exception):
                QMetaObject.invokeMethod(self, lambda: self._on_single_calibration_failed(row, str(res)))
            else:
                config, calibration = res
                QMetaObject.invokeMethod(self, lambda: self._on_single_calibration_done(row, config, calibration))

        fut = self.executor.submit(task)
        fut.add_done_callback(on_done)

    def _on_single_calibration_done(self, row: int, config, calibration):
        self.jobs[row] = (config, calibration)
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
                item.setText(translate_text("Prêt"))
                item.setForeground(QColor(_C.OK))
        self.run_button.setEnabled(not self.busy and bool(self.jobs))
        self.status.setText(translate_text("Calibration terminée pour : {name}", name=self.matrix_episodes[row].display_name))
        self.table.selectRow(row)

    def _on_single_calibration_failed(self, row: int, err: str):
        item = self.table.item(row, 3)
        if item is not None:
            item.setText(translate_text("Échec"))
            item.setForeground(QColor(_C.ERROR))
        self.status.setText(translate_text("Erreur calibration : {err}", err=err))

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

    def prepare_next(self):
        if self.stopped or self.index >= len(self.matrix_episodes):
            self.set_busy(False)
            return

        episode = self.matrix_episodes[self.index]
        if not episode.is_complete:
            item = self.table.item(self.index, 3)
            if item is not None:
                item.setText(translate_text("Incomplet"))
                item.setForeground(QColor(_C.WARN))
            self.index += 1
            self.prepare_next()
            return

        item = self.table.item(self.index, 3)
        if item is not None:
            item.setText(translate_text("Analyse en cours…"))

        def task():
            from cli.logging import Logger
            try:
                config, calibration = prepare_matrix_episode(
                    episode,
                    self.recipe,
                    self.output.text().strip(),
                    self.config,
                    self.args,
                    Logger(),
                    detect_cuts=self.detect_cuts.isChecked(),
                    drift_threshold_ms=getattr(self.args, "drift_threshold_ms", 25) or 25,
                )
                self.prepared.emit(config, calibration)
            except Exception as exc:
                self.failed.emit(str(exc))

        self.executor.submit(task)

    def on_prepared(self, config, calibration):
        if self.stopped:
            self.set_busy(False)
            return
        self.jobs[self.index] = (config, calibration)
        item = self.table.item(self.index, 3)
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
                item.setText(translate_text("Prêt"))
                item.setForeground(QColor(_C.OK))
        self.run_button.setEnabled(not self.busy and bool(self.jobs))
        self.index += 1
        self.prepare_next()

    def on_failed(self, message: str):
        self.status.setText(translate_text("Hybridation impossible : {err}", err=message))
        if self.index < self.table.rowCount():
            item = self.table.item(self.index, 3)
            if item is not None:
                item.setForeground(QColor(_C.ERROR))
                item.setText(translate_text("Échec"))
        self.listen_button.setEnabled(True)
        self.aux_running = False
        self.set_busy(False)

    def select_waveform(self):
        row = self.table.currentRow()
        if self.busy or row not in self.jobs:
            return
        config, calibration = self.jobs[row]
        if not calibration or not calibration.segments:
            return
        self.adjust.blockSignals(True)
        self.adjust.setValue(calibration.segments[0].shift_ms)
        self.adjust.blockSignals(False)
        self.waveform.shift_ms = calibration.segments[0].shift_ms

        def task():
            import numpy as np
            scanner = AudioSyncScanner(self.config.tool_ffmpeg, self.config.tool_ffprobe)
            try:
                series = []
                for source in config.sources[:2]:
                    track = next(t for t in source.tracks if t.track_type == "audio" and t.enabled)
                    samples = scanner.samples(AudioSyncTrack(source.path, track.mkv_tid), 0, 20)
                    size = max(1, len(samples) // 1000)
                    series.append(np.max(np.abs(samples[:len(samples) // size * size].reshape(-1, size)), axis=1).tolist())
                self.waveform_ready.emit((row, series))
            except Exception as exc:
                self.failed.emit(str(exc))

        self.aux_running = True
        self.executor.submit(task)

    def on_waveform(self, result):
        self.aux_running = False
        row, series = result
        if row == self.table.currentRow():
            self.waveform.series = series
            self.waveform.update()

    def adjust_offset(self, value: float):
        row = self.table.currentRow()
        if self.busy or row not in self.jobs:
            return
        config, calibration = self.jobs[row]
        if not calibration or not calibration.segments:
            return
        delta = value - calibration.segments[0].shift_ms
        calibration = replace(
            calibration,
            segments=tuple(SyncSegment(s.start_ms, s.shift_ms + delta) for s in calibration.segments),
        )
        if config.sync_mode == "physical":
            config.sync_calibrations = {"1": calibration.to_dict()}
        else:
            for track in config.sources[1].tracks:
                if track.track_type == "audio" or (track.track_type == "subtitle" and config.sync_subtitles == "mirror"):
                    track.time_shift_ms = round(value)
        self.jobs[row] = (config, calibration)
        self.waveform.shift_ms = value
        self.waveform.update()

    def listen(self):
        row = self.table.currentRow()
        if self.busy:
            return
        if row not in self.jobs:
            self.status.setText(translate_text("Veuillez d'abord analyser ou calibrer cet élément (bouton ⚡)."))
            return

        has_qt_media = False
        try:
            from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
            has_qt_media = True
        except ImportError:
            has_qt_media = False

        if has_qt_media and not hasattr(self, "player"):
            try:
                self.player = QMediaPlayer(self)
                self.audio = QAudioOutput(self)
                self.player.setAudioOutput(self.audio)
            except Exception:
                has_qt_media = False

        config, calibration = self.jobs[row]
        self.listen_button.setEnabled(False)

        def task():
            from core.subprocess_utils import subprocess_text_kwargs
            from core.workflows.physical_sync import audio_filter

            source = config.sources[1]
            track = next(t for t in source.tracks if t.track_type == "audio" and t.enabled)
            path = Path(self.preview_temp.name) / (uuid.uuid4().hex + ".wav")
            graph = audio_filter(calibration, config.crossfade_ms).replace("[0:a:0]", f"[0:{track.mkv_tid}]")
            try:
                scan = AudioSyncScanner(self.config.tool_ffmpeg, self.config.tool_ffprobe, cancel_event=self.cancel_event)
                result = scan._run(
                    [
                        str(self.config.tool_ffmpeg), "-nostdin", "-v", "error", "-i", str(source.path),
                        "-filter_complex", graph, "-map", "[out]", "-t", "20", "-ac", "2",
                        "-c:a", "pcm_s16le", str(path),
                    ],
                    timeout=120,
                    **subprocess_text_kwargs(),
                )
                if result.returncode:
                    raise ValueError(result.stderr)
                self.preview_ready.emit(str(path))
            except Exception as exc:
                self.failed.emit(str(exc))

        self.aux_running = True
        self.executor.submit(task)

    def play_preview(self, path: str):
        self.aux_running = False
        self.listen_button.setEnabled(True)
        if hasattr(self, "player") and self.player is not None:
            self.player.setSource(QUrl.fromLocalFile(path))
            self.player.play()
        else:
            try:
                import subprocess
                ffplay = Path(str(self.config.tool_ffmpeg)).parent / "ffplay"
                if ffplay.exists():
                    subprocess.Popen([str(ffplay), "-nodisp", "-autoexit", path])
                else:
                    self.status.setText(f"Aperçu audio : {path}")
            except Exception:
                pass

    def export_jobs(self):
        if self.busy or not self.jobs:
            return
        directory = QFileDialog.getExistingDirectory(self, translate_text("Sauvegarder les workflows…"))
        if not directory:
            return
        from core.profiles.selectors import remux_config_to_exact_job
        from core.workflows.workflow_store import save_workflow

        try:
            for config, calibration in self.jobs.values():
                save_workflow(Path(directory) / (config.output.stem + ".exact-job.json"), remux_config_to_exact_job(config))
        except Exception as exc:
            self.on_failed(str(exc))

    def run(self):
        if self.busy or not self.jobs:
            return
        self.execution_rows = sorted(self.jobs.keys())
        self.exec_index = 0
        self.stopped = False
        self.set_busy(True)
        self.run_next()

    def run_next(self):
        from core.workflows.remux import RemuxWorkflow

        if self.stopped or self.exec_index >= len(self.execution_rows):
            self.set_busy(False)
            return

        row = self.execution_rows[self.exec_index]
        config, _ = self.jobs[row]
        if config.output.exists():
            self.on_failed(translate_text("La sortie existe déjà : {path}", path=str(config.output)))
            return
        config.output.parent.mkdir(parents=True, exist_ok=True)
        self.workflow = RemuxWorkflow(
            ffmpeg_bin=str(self.config.tool_ffmpeg),
            ffprobe_bin=str(self.config.tool_ffprobe),
            mediainfo_bin=str(self.config.tool_mediainfo),
        )
        try:
            self.signals = self.workflow.run(replace(config, allow_missing_output_dir=False))
            self.signals.finished.connect(self.on_finished)
            self.signals.failed.connect(lambda message, exc: self.on_failed(message))
            self.signals.cancelled.connect(lambda: self.set_busy(False))
            self.signals.progress.connect(self.status.setText)
        except Exception as exc:
            self.on_failed(str(exc))

    def on_finished(self, message: str):
        if self.exec_index < len(self.execution_rows):
            row = self.execution_rows[self.exec_index]
            item = self.table.item(row, 3)
            if item is not None:
                item.setText(translate_text("Terminé"))
                item.setForeground(QColor(_C.OK))
        self.exec_index += 1
        self.run_next()

    def cancel(self):
        self.stopped = True
        self.cancel_event.set()
        if self.signals is not None:
            self.signals.cancel()

    def closeEvent(self, event):
        if self.busy or self.aux_running:
            self.cancel()
            event.ignore()
            return
        if hasattr(self, "player") and self.player is not None:
            try:
                self.player.stop()
                self.player.setSource(QUrl())
            except Exception:
                pass
        self.executor.shutdown(wait=False, cancel_futures=True)
        try:
            self.preview_temp.cleanup()
        except Exception:
            pass
        super().closeEvent(event)
