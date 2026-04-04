"""
ui/main_window.py — Fenêtre principale de Mediarecode.

Architecture :
    ┌────────────────────────────────────────────────────────────┐
    │  MainWindow (QMainWindow)                                  │
    │  ┌──────────┬─────────────────────────────────────────┐   │
    │  │ Sidebar  │  QStackedWidget (pages)                 │   │
    │  │ (NavBar) │  ─ DashboardPage       (index 0)        │   │
    │  │          │  ─ MergeDoviPanel      (index 1) ✓      │   │
    │  │          │  ─ AudioConvPage       (index 2) TODO   │   │
    │  │          │  ─ RemuxPanel          (index 3) ✓      │   │
    │  │          │  ─ SettingsPage        (index 4) TODO   │   │
    │  └──────────┴─────────────────────────────────────────┘   │
    │  ┌──────────────────────────────────────────────────────┐  │
    │  │  LogPanel (niveaux colorés INFO / OK / WARN / ERROR) │  │
    │  └──────────────────────────────────────────────────────┘  │
    └────────────────────────────────────────────────────────────┘

Signals exposés :
    MainWindow.log_requested(level: str, message: str)
        → peut être connecté depuis n'importe quel worker/module
"""

from __future__ import annotations

import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Callable, TYPE_CHECKING

from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import (
    QColor, QFont, QIcon, QPalette,
    QTextCharFormat, QTextCursor,
)
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QMainWindow, QMessageBox,
    QProgressBar, QPushButton, QScrollArea, QSizePolicy,
    QSplitter, QStackedWidget, QTextEdit,
    QVBoxLayout, QWidget,
)

from core.config import AppConfig
from core.runner import TaskSignals
from core.workflows.encode import EncodeError
from core.workflows.remux import RemuxError
from ui.panels.encode_panel import EncodePanel
from ui.panels.encode_panel.theme import _FPS_RE, _TIME_RE, _fmt_eta
from ui.panels.merge_dovi_panel import MergeDoviPanel
from ui.panels.remux_panel import RemuxPanel

if TYPE_CHECKING:
    from core.workflows.encode.models import EncodeConfig
    from core.workflows.remux import RemuxConfig


# ---------------------------------------------------------------------------
# Palette de couleurs (thème sombre)
# ---------------------------------------------------------------------------

class _Colors:
    BG_DEEP    = "#0d0f14"
    BG_PANEL   = "#141720"
    BG_SIDEBAR = "#0f1117"
    BG_CARD    = "#1a1e2a"
    BG_HOVER   = "#1f2435"
    BG_ACTIVE  = "#232840"

    BORDER     = "#252a3a"
    BORDER_LT  = "#2e3450"

    TEXT_PRI   = "#e8ecf4"
    TEXT_SEC   = "#7a85a0"
    TEXT_DIM   = "#3d4560"

    ACCENT     = "#4f6ef7"
    ACCENT_DIM = "#2a3a8a"

    # Log levels
    LOG_INFO   = "#7ab3f5"
    LOG_OK     = "#5dcc8a"
    LOG_WARN   = "#f5c842"
    LOG_ERROR  = "#f55a5a"
    LOG_TS     = "#3d4560"
    LOG_BG     = "#0a0c11"


# ---------------------------------------------------------------------------
# Niveaux de log
# ---------------------------------------------------------------------------

class LogLevel(str, Enum):
    INFO  = "INFO"
    OK    = "OK"
    WARN  = "WARN"
    ERROR = "ERROR"


_LEVEL_COLORS: dict[LogLevel, str] = {
    LogLevel.INFO:  _Colors.LOG_INFO,
    LogLevel.OK:    _Colors.LOG_OK,
    LogLevel.WARN:  _Colors.LOG_WARN,
    LogLevel.ERROR: _Colors.LOG_ERROR,
}

_LEVEL_LABELS: dict[LogLevel, str] = {
    LogLevel.INFO:  " INFO ",
    LogLevel.OK:    "  OK  ",
    LogLevel.WARN:  " WARN ",
    LogLevel.ERROR: " ERR  ",
}


# ---------------------------------------------------------------------------
# LogPanel
# ---------------------------------------------------------------------------

class _LogHeader(QWidget):
    """Barre d'en-tête cliquable du panneau de logs."""
    clicked = Signal()

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        self.clicked.emit()
        super().mousePressEvent(event)


class LogPanel(QWidget):
    """
    Panneau de logs à niveaux colorés.

    Usage :
        panel.log("Vérification des dépendances...", LogLevel.INFO)
        panel.log("Toutes les dépendances sont présentes.", LogLevel.OK)
        panel.log("Différence de 2 frames tolérée.", LogLevel.WARN)
        panel.log("Fichier introuvable.", LogLevel.ERROR)
    """

    collapse_toggled = Signal(bool)   # True = collapsed

    def __init__(self, max_lines: int = 2000, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._max_lines = max_lines
        self._collapsed = False
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # En-tête cliquable
        header = _LogHeader()
        header.setFixedHeight(32)
        header.setCursor(Qt.CursorShape.PointingHandCursor)
        header.setStyleSheet(f"""
            _LogHeader {{
                background: {_Colors.BG_PANEL};
                border-top: 1px solid {_Colors.BORDER};
                border-bottom: 1px solid {_Colors.BORDER};
            }}
        """)
        h_layout = QHBoxLayout(header)
        h_layout.setContentsMargins(12, 0, 8, 0)
        h_layout.setSpacing(8)

        title = QLabel("LOGS")
        title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        title.setStyleSheet(f"""
            color: {_Colors.TEXT_DIM};
            font-size: 10px;
            font-weight: 700;
            letter-spacing: 2px;
            background: transparent;
            border: none;
        """)
        h_layout.addWidget(title)
        h_layout.addStretch()

        # Bouton collapse (▲/▼)
        self._collapse_btn = QPushButton("▲")
        self._collapse_btn.setFixedSize(20, 20)
        self._collapse_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._collapse_btn.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._collapse_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {_Colors.TEXT_DIM};
                border: none;
                font-size: 10px;
                padding: 0;
            }}
        """)
        h_layout.addWidget(self._collapse_btn)

        # Bouton clear
        clear_btn = QPushButton("Effacer")
        clear_btn.setFixedHeight(20)
        clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        clear_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {_Colors.TEXT_DIM};
                border: 1px solid {_Colors.BORDER};
                border-radius: 3px;
                font-size: 10px;
                padding: 0 8px;
            }}
            QPushButton:hover {{
                color: {_Colors.TEXT_SEC};
                border-color: {_Colors.BORDER_LT};
            }}
        """)
        clear_btn.clicked.connect(self.clear)
        h_layout.addWidget(clear_btn)

        header.clicked.connect(self._toggle)
        layout.addWidget(header)

        # Zone de texte
        self._text = QTextEdit()
        self._text.setReadOnly(True)
        self._text.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._text.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        mono = QFont("JetBrains Mono", 9)
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self._text.setFont(mono)
        self._text.setStyleSheet(f"""
            QTextEdit {{
                background: {_Colors.LOG_BG};
                color: {_Colors.TEXT_PRI};
                border: none;
                padding: 8px 12px;
                selection-background-color: {_Colors.ACCENT_DIM};
            }}
            QScrollBar:vertical {{
                background: {_Colors.BG_DEEP};
                width: 8px;
                border: none;
            }}
            QScrollBar::handle:vertical {{
                background: {_Colors.BORDER_LT};
                border-radius: 4px;
                min-height: 20px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0;
            }}
        """)
        self._text.setMinimumHeight(0)
        layout.addWidget(self._text)

    # ------------------------------------------------------------------
    # API publique
    # ------------------------------------------------------------------

    def log(self, message: str, level: LogLevel = LogLevel.INFO) -> None:
        """Ajoute une ligne de log avec horodatage et niveau coloré."""
        cursor = self._text.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)

        ts = datetime.now().strftime("%H:%M:%S")
        color = _LEVEL_COLORS[level]
        label = _LEVEL_LABELS[level]

        # Timestamp
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(_Colors.LOG_TS))
        cursor.insertText(f"{ts}  ", fmt)

        # Badge niveau
        fmt.setForeground(QColor(color))
        fmt.setFontWeight(QFont.Weight.Bold)
        cursor.insertText(label, fmt)

        # Message
        fmt.setFontWeight(QFont.Weight.Normal)
        fmt.setForeground(QColor(color if level != LogLevel.INFO else _Colors.TEXT_PRI))
        cursor.insertText(f"  {message}\n", fmt)

        # Défilement automatique vers le bas
        self._text.setTextCursor(cursor)
        self._text.ensureCursorVisible()

        # Limite du nombre de lignes
        doc = self._text.document()
        while doc.blockCount() > self._max_lines:
            cur = QTextCursor(doc.begin())
            cur.select(QTextCursor.SelectionType.BlockUnderCursor)
            cur.removeSelectedText()
            cur.deleteChar()

    def info(self, message: str)  -> None: self.log(message, LogLevel.INFO)
    def ok(self, message: str)    -> None: self.log(message, LogLevel.OK)
    def warn(self, message: str)  -> None: self.log(message, LogLevel.WARN)
    def error(self, message: str) -> None: self.log(message, LogLevel.ERROR)

    def clear(self) -> None:
        self._text.clear()

    def _toggle(self) -> None:
        self._collapsed = not self._collapsed
        self._text.setVisible(not self._collapsed)
        self._collapse_btn.setText("▼" if self._collapsed else "▲")
        self.collapse_toggled.emit(self._collapsed)


# ---------------------------------------------------------------------------
# Page placeholder (remplacée en Phase 2 par des pages réelles)
# ---------------------------------------------------------------------------

class _PlaceholderPage(QWidget):
    def __init__(self, title: str, icon: str, description: str) -> None:
        super().__init__()
        self.setStyleSheet(f"background: {_Colors.BG_DEEP};")
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(16)

        icon_lbl = QLabel(icon)
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_lbl.setStyleSheet(f"font-size: 48px; color: {_Colors.TEXT_DIM}; background: transparent;")
        layout.addWidget(icon_lbl)

        title_lbl = QLabel(title)
        title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_lbl.setStyleSheet(f"""
            font-size: 18px;
            font-weight: 700;
            color: {_Colors.TEXT_PRI};
            background: transparent;
            letter-spacing: 0.5px;
        """)
        layout.addWidget(title_lbl)

        desc_lbl = QLabel(description)
        desc_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        desc_lbl.setWordWrap(True)
        desc_lbl.setMaximumWidth(400)
        desc_lbl.setStyleSheet(f"color: {_Colors.TEXT_SEC}; font-size: 12px; background: transparent;")
        layout.addWidget(desc_lbl)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

class DashboardPage(QWidget):
    """Page d'accueil — résumé des outils disponibles et raccourcis."""

    _hw_detected = Signal(object)   # set[str] — encodeurs HW disponibles

    # codec_id → (label affiché, badge QLabel) pour mise à jour async
    _HW_VIDEO: list[tuple[str, str]] = [
        ("hevc_nvenc", "NVENC·HEVC"), ("hevc_amf", "AMF·HEVC"), ("hevc_vaapi", "VAAPI·HEVC"), ("hevc_qsv", "QSV·HEVC"),
        ("h264_nvenc", "NVENC·H264"), ("h264_amf", "AMF·H264"), ("h264_vaapi", "VAAPI·HEVC"), ("h264_qsv", "QSV·H264"),
    ]
    _SW_VIDEO: list[tuple[str, str]] = [
        ("libx265", "x265"), ("libx264", "x264"), ("libsvtav1", "SVT-AV1"),
    ]
    _AUDIO: list[tuple[str, str]] = [
        ("aac", "AAC"), ("eac3", "EAC-3"), ("flac", "FLAC"), ("ac3", "AC-3"), ("libopus", "Opus"),
    ]

    def __init__(self, config: AppConfig, log: Callable[[str, LogLevel], None]) -> None:
        super().__init__()
        self._config = config
        self._log = log
        self._hw_badges: dict[str, tuple[QLabel, str]] = {}   # codec_id → (badge, label)
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._hw_detected.connect(self._on_hw_detected, Qt.ConnectionType.QueuedConnection)
        self._build_ui()
        self._start_hw_detection()

    def _build_ui(self) -> None:
        self.setStyleSheet(f"background: {_Colors.BG_DEEP};")
        root = QVBoxLayout(self)
        root.setContentsMargins(32, 32, 32, 32)
        root.setSpacing(24)
        root.setAlignment(Qt.AlignmentFlag.AlignTop)

        # Titre
        title = QLabel("Mediarecode")
        title.setStyleSheet(f"""
            font-size: 26px;
            font-weight: 800;
            color: {_Colors.TEXT_PRI};
            background: transparent;
            letter-spacing: -0.5px;
        """)
        root.addWidget(title)

        subtitle = QLabel("Manipulation, encodage et injection de métadonnées HDR")
        subtitle.setStyleSheet(f"color: {_Colors.TEXT_SEC}; font-size: 13px; background: transparent;")
        root.addWidget(subtitle)

        # Séparateur
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {_Colors.BORDER};")
        root.addWidget(sep)

        # Statut des outils
        status_title = QLabel("Outils disponibles")
        status_title.setStyleSheet(f"""
            font-size: 11px;
            font-weight: 700;
            color: {_Colors.TEXT_DIM};
            letter-spacing: 1.5px;
            background: transparent;
        """)
        root.addWidget(status_title)

        tools_grid = QWidget()
        tools_grid.setStyleSheet("background: transparent;")
        grid_layout = QHBoxLayout(tools_grid)
        grid_layout.setContentsMargins(0, 0, 0, 0)
        grid_layout.setSpacing(8)
        grid_layout.setAlignment(Qt.AlignmentFlag.AlignLeft)

        availability = self._config.all_tools_available()
        for tool_name, available in availability.items():
            badge = self._make_tool_badge(tool_name, available)
            grid_layout.addWidget(badge)

        root.addWidget(tools_grid)

        # Vérification manuelle
        check_btn = QPushButton("↻  Vérifier les outils")
        check_btn.setFixedWidth(200)
        check_btn.setFixedHeight(34)
        check_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        check_btn.setStyleSheet(f"""
            QPushButton {{
                background: {_Colors.BG_CARD};
                color: {_Colors.TEXT_SEC};
                border: 1px solid {_Colors.BORDER};
                border-radius: 6px;
                font-size: 12px;
                font-weight: 600;
                padding: 0 16px;
            }}
            QPushButton:hover {{
                background: {_Colors.BG_HOVER};
                color: {_Colors.TEXT_PRI};
                border-color: {_Colors.BORDER_LT};
            }}
            QPushButton:pressed {{
                background: {_Colors.BG_ACTIVE};
            }}
        """)
        check_btn.clicked.connect(self._check_tools)
        root.addWidget(check_btn)

        self._build_encoder_section(root)

        root.addStretch()

        # Infos de chemins
        paths_title = QLabel("Chemins configurés")
        paths_title.setStyleSheet(f"""
            font-size: 11px;
            font-weight: 700;
            color: {_Colors.TEXT_DIM};
            letter-spacing: 1.5px;
            background: transparent;
        """)
        root.addWidget(paths_title)

        for label, value in [
            ("Dossier travail", self._config.work_dir),
            ("Dossier sortie",  self._config.output_dir),
            ("App data",        self._config.app_data_dir),
        ]:
            row = QWidget()
            row.setStyleSheet("background: transparent;")
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.setSpacing(12)

            key_lbl = QLabel(label)
            key_lbl.setFixedWidth(110)
            key_lbl.setStyleSheet(f"color: {_Colors.TEXT_SEC}; font-size: 11px; background: transparent;")

            val_lbl = QLabel(str(value))
            val_lbl.setStyleSheet(f"""
                color: {_Colors.TEXT_DIM};
                font-size: 11px;
                font-family: 'JetBrains Mono', monospace;
                background: transparent;
            """)
            rl.addWidget(key_lbl)
            rl.addWidget(val_lbl)
            rl.addStretch()
            root.addWidget(row)

    def _make_tool_badge(self, name: str, available: bool) -> QLabel:
        color  = _Colors.LOG_OK  if available else _Colors.LOG_ERROR
        bg     = "#0f2318"       if available else "#1f0e0e"
        border = "#1a4a2e"       if available else "#3a1515"
        symbol = "●"             if available else "○"
        lbl = QLabel(f" {symbol}  {name} ")
        lbl.setStyleSheet(f"""
            QLabel {{
                background: {bg};
                color: {color};
                border: 1px solid {border};
                border-radius: 4px;
                font-size: 11px;
                font-family: 'JetBrains Mono', monospace;
                padding: 3px 8px;
            }}
        """)
        return lbl

    # ------------------------------------------------------------------
    # Section encodeurs
    # ------------------------------------------------------------------

    def _build_encoder_section(self, root: QVBoxLayout) -> None:
        """Ajoute la section encodeurs au layout root. Détection SW synchrone, HW asynchrone."""
        self._hw_badges = {}

        # Détection synchrone des encodeurs logiciels via ffmpeg -encoders
        all_sw_ids = [c for c, _ in self._SW_VIDEO] + [c for c, _ in self._AUDIO]
        try:
            r = subprocess.run(
                ["ffmpeg", "-encoders"],
                capture_output=True, text=True, check=False,
            )
            sw_avail = {
                c: bool(re.search(rf"\b{re.escape(c)}\b", r.stdout))
                for c in all_sw_ids
            }
        except FileNotFoundError:
            sw_avail = {c: False for c in all_sw_ids}

        # Séparateur + titre
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {_Colors.BORDER};")
        root.addWidget(sep)

        section_title = QLabel("ENCODEURS DISPONIBLES")
        section_title.setStyleSheet(f"""
            font-size: 11px; font-weight: 700;
            color: {_Colors.TEXT_DIM}; letter-spacing: 1.5px; background: transparent;
        """)
        root.addWidget(section_title)

        def _row(sub_label: str) -> tuple[QWidget, QHBoxLayout]:
            row = QWidget()
            row.setStyleSheet("background: transparent;")
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.setSpacing(8)
            lbl = QLabel(sub_label)
            lbl.setFixedWidth(130)
            lbl.setStyleSheet(f"color:{_Colors.TEXT_SEC};font-size:11px;background:transparent;")
            rl.addWidget(lbl)
            return row, rl

        # Vidéo logiciel
        row, rl = _row("Vidéo — logiciel")
        for codec_id, label in self._SW_VIDEO:
            state = "available" if sw_avail.get(codec_id) else "unavailable"
            rl.addWidget(self._make_encoder_badge(label, state))
        rl.addStretch()
        root.addWidget(row)

        # Vidéo matériel (badges en attente → mis à jour par _on_hw_detected)
        row, rl = _row("Vidéo — matériel")
        for codec_id, label in self._HW_VIDEO:
            badge = self._make_encoder_badge(label, "pending")
            self._hw_badges[codec_id] = (badge, label)
            rl.addWidget(badge)
        rl.addStretch()
        root.addWidget(row)

        # Audio
        row, rl = _row("Audio")
        for codec_id, label in self._AUDIO:
            state = "available" if sw_avail.get(codec_id) else "unavailable"
            rl.addWidget(self._make_encoder_badge(label, state))
        rl.addStretch()
        root.addWidget(row)

    def _make_encoder_badge(self, label: str, state: str) -> QLabel:
        """Crée un badge d'encodeur. state ∈ {"available", "unavailable", "pending"}."""
        badge = QLabel()
        self._apply_encoder_badge_state(badge, label, state)
        return badge

    def _apply_encoder_badge_state(self, badge: QLabel, label: str, state: str) -> None:
        if state == "available":
            symbol, color, bg, border = "●", _Colors.LOG_OK,    "#0f2318", "#1a4a2e"
        elif state == "unavailable":
            symbol, color, bg, border = "○", _Colors.LOG_ERROR,  "#1f0e0e", "#3a1515"
        else:  # pending
            symbol, color, bg, border = "…", _Colors.TEXT_DIM, _Colors.BG_CARD, _Colors.BORDER
        badge.setText(f" {symbol}  {label} ")
        badge.setStyleSheet(f"""
            QLabel {{
                background: {bg}; color: {color};
                border: 1px solid {border}; border-radius: 4px;
                font-size: 11px; font-family: 'JetBrains Mono', monospace;
                padding: 3px 8px;
            }}
        """)

    # ------------------------------------------------------------------
    # Détection asynchrone des encodeurs matériels
    # ------------------------------------------------------------------

    def _start_hw_detection(self) -> None:
        """Remet les badges HW en état "pending" et soumet la détection à l'executor."""
        for codec_id, (badge, label) in self._hw_badges.items():
            self._apply_encoder_badge_state(badge, label, "pending")
        self._executor.submit(self._run_hw_detection)

    def _run_hw_detection(self) -> None:
        """Thread worker : probe runtime de chaque encodeur HW."""
        from core.workflows.encode import HardwareEncoderDetector
        available = HardwareEncoderDetector().detect()
        self._hw_detected.emit(available)

    def _on_hw_detected(self, available: set[str]) -> None:
        """Slot Qt (thread principal) : met à jour les badges HW."""
        for codec_id, (badge, label) in self._hw_badges.items():
            state = "available" if codec_id in available else "unavailable"
            self._apply_encoder_badge_state(badge, label, state)

    # ------------------------------------------------------------------
    # Vérification manuelle des outils
    # ------------------------------------------------------------------

    def _check_tools(self) -> None:
        self._log("Vérification des outils...", LogLevel.INFO)
        availability = self._config.all_tools_available()
        all_ok = True
        for name, available in availability.items():
            if available:
                self._log(f"{name} — trouvé", LogLevel.OK)
            else:
                self._log(f"{name} — introuvable dans PATH", LogLevel.WARN)
                all_ok = False
        if all_ok:
            self._log("Tous les outils sont disponibles.", LogLevel.OK)
        else:
            self._log("Certains outils sont manquants. Vérifiez votre PATH ou les paramètres.", LogLevel.WARN)
        # Rafraîchit l'affichage des badges + relance la détection HW
        self._build_ui()
        self._start_hw_detection()


# ---------------------------------------------------------------------------
# Bouton de navigation sidebar
# ---------------------------------------------------------------------------

class _NavButton(QWidget):
    """Bouton sidebar avec icône à largeur fixe et label aligné."""

    clicked = Signal()

    def __init__(self, label: str, icon_char: str, page_index: int, is_sub: bool = False) -> None:
        super().__init__()
        self.page_index = page_index
        self._checked  = False
        self._is_sub   = is_sub
        self.setFixedHeight(36 if is_sub else 40)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(28 if is_sub else 14, 0, 14, 0)
        lay.setSpacing(10)

        self._icon_lbl = QLabel(icon_char)
        self._icon_lbl.setFixedWidth(20)
        self._icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._icon_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        self._text_lbl = QLabel(label)
        self._text_lbl.setAlignment(
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
        )
        self._text_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        lay.addWidget(self._icon_lbl)
        lay.addWidget(self._text_lbl)
        lay.addStretch()

        self._update_style(False)

    # ------------------------------------------------------------------

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        self.clicked.emit()
        super().mousePressEvent(event)

    def setChecked(self, checked: bool) -> None:
        self._checked = checked
        self._update_style(checked)

    def isCheckable(self) -> bool:
        return True

    def _update_style(self, checked: bool) -> None:
        if checked:
            bg     = _Colors.BG_ACTIVE
            color  = _Colors.TEXT_PRI
            icon_c = _Colors.ACCENT
            border = f"border-left: 2px solid {_Colors.ACCENT};"
            weight = "600"
        else:
            bg     = "transparent"
            color  = _Colors.TEXT_SEC
            icon_c = _Colors.TEXT_DIM
            border = "border-left: 2px solid transparent;"
            weight = "400"

        self.setStyleSheet(f"""
            _NavButton {{
                background: {bg};
                {border}
                border-right: none;
                border-top: none;
                border-bottom: none;
            }}
            _NavButton:hover {{
                background: {_Colors.BG_HOVER};
            }}
        """)
        self._icon_lbl.setStyleSheet(
            f"color: {icon_c}; font-size: 14px; background: transparent; border: none;"
        )
        font_size = "11px" if self._is_sub else "12px"
        self._text_lbl.setStyleSheet(
            f"color: {color}; font-size: {font_size}; font-weight: {weight};"
            f" background: transparent; border: none;"
        )


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

class _Sidebar(QWidget):
    page_changed = Signal(int)

    _NAV_ITEMS = [
        ("Tableau de bord", "⌂", 0, False),
        ("Conteneur",       "⊞", 3, False),
        ("Encodage",        "🎬", 2, True),   # sous-menu de Conteneur
        ("DoVi / HDR10+",   "◈", 1, False),
        ("Paramètres",      "⚙", 4, False),
    ]

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedWidth(200)
        self.setStyleSheet(f"""
            QWidget {{
                background: {_Colors.BG_SIDEBAR};
                border-right: 1px solid {_Colors.BORDER};
            }}
        """)
        self._buttons: list[_NavButton] = []
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Logo / App name
        logo_area = QWidget()
        logo_area.setFixedHeight(56)
        logo_area.setStyleSheet(f"""
            QWidget {{
                background: {_Colors.BG_SIDEBAR};
                border-bottom: 1px solid {_Colors.BORDER};
                border-right: none;
            }}
        """)
        la = QHBoxLayout(logo_area)
        la.setContentsMargins(16, 0, 16, 0)

        logo_icon = QLabel("▣")
        logo_icon.setStyleSheet(f"color: {_Colors.ACCENT}; font-size: 18px; background: transparent; border: none;")
        logo_text = QLabel("Mediarecode")
        logo_text.setStyleSheet(f"""
            color: {_Colors.TEXT_PRI};
            font-size: 13px;
            font-weight: 700;
            background: transparent;
            border: none;
            letter-spacing: 0.3px;
        """)
        la.addWidget(logo_icon)
        la.addSpacing(8)
        la.addWidget(logo_text)
        la.addStretch()
        layout.addWidget(logo_area)

        # Navigation
        nav_label = QLabel("NAVIGATION")
        nav_label.setContentsMargins(16, 16, 0, 8)
        nav_label.setStyleSheet(f"""
            color: {_Colors.TEXT_DIM};
            font-size: 9px;
            font-weight: 700;
            letter-spacing: 2px;
            background: transparent;
            border: none;
        """)
        layout.addWidget(nav_label)

        for label, icon, idx, is_sub in self._NAV_ITEMS:
            btn = _NavButton(label, icon, idx, is_sub)
            btn.clicked.connect(lambda b=btn: self._on_nav_click(b))
            self._buttons.append(btn)
            layout.addWidget(btn)

        layout.addStretch()

        # Version
        version_lbl = QLabel("v0.1.0 — Phase 6")
        version_lbl.setContentsMargins(16, 0, 0, 12)
        version_lbl.setStyleSheet(f"""
            color: {_Colors.TEXT_DIM};
            font-size: 9px;
            background: transparent;
            border: none;
        """)
        layout.addWidget(version_lbl)

        # Sélectionner le premier bouton
        if self._buttons:
            self._buttons[0].setChecked(True)

    def _on_nav_click(self, clicked: _NavButton) -> None:
        for btn in self._buttons:
            if btn is not clicked:
                btn.setChecked(False)
        clicked.setChecked(True)
        self.page_changed.emit(clicked.page_index)

    def select_page(self, index: int) -> None:
        for btn in self._buttons:
            btn.setChecked(btn.page_index == index)


# ---------------------------------------------------------------------------
# MainWindow
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    """
    Fenêtre principale de l'application.

    Signal :
        log_requested(level: str, message: str)
            Émet un message de log depuis n'importe quel composant.
            Les workers peuvent s'y connecter via Qt.QueuedConnection
            pour poster des logs depuis des threads secondaires.
    """

    log_requested = Signal(str, str)

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self._config = config
        self._running   = False
        self._signals: TaskSignals | None = None
        self._op_start: float = 0.0
        self._op_mode: str = ""   # "remux" ou "encode"
        self._setup_window()
        self._build_ui()
        self._restore_geometry()
        self._connect_signals()
        self._post_init_log()

    # ------------------------------------------------------------------
    # Fenêtre
    # ------------------------------------------------------------------

    def _setup_window(self) -> None:
        self.setWindowTitle("Mediarecode")
        self.setMinimumSize(1024, 680)
        self.resize(1280, 800)

        # Fond global
        self.setStyleSheet(f"""
            QMainWindow {{
                background: {_Colors.BG_DEEP};
            }}
            QSplitter::handle {{
                background: {_Colors.BORDER};
                height: 1px;
            }}
        """)

    # ------------------------------------------------------------------
    # Construction de l'interface
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        central.setStyleSheet(f"background: {_Colors.BG_DEEP};")
        self.setCentralWidget(central)

        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # ── Splitter vertical : (sidebar + stack + action bar) / log panel ──
        self._vsplit = QSplitter(Qt.Orientation.Vertical)
        vsplit = self._vsplit
        vsplit.setHandleWidth(5)
        vsplit.setChildrenCollapsible(False)

        # Partie haute : sidebar + pages + barre d'action globale
        top_widget = QWidget()
        top_widget.setStyleSheet(f"background: {_Colors.BG_DEEP};")
        top_widget.setMinimumHeight(1)
        top_vbox = QVBoxLayout(top_widget)
        top_vbox.setContentsMargins(0, 0, 0, 0)
        top_vbox.setSpacing(0)

        # Ligne principale : sidebar + stack
        content_widget = QWidget()
        content_widget.setStyleSheet(f"background: {_Colors.BG_DEEP};")
        content_widget.setMinimumHeight(1)
        top_layout = QHBoxLayout(content_widget)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(0)

        # Sidebar
        self._sidebar = _Sidebar()
        top_layout.addWidget(self._sidebar)

        # Stack de pages
        self._stack = QStackedWidget()
        self._stack.setStyleSheet(f"background: {_Colors.BG_DEEP};")
        self._stack.setMinimumHeight(0)
        self._stack.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        # Page 0 — Dashboard (fonctionnelle)
        self._dashboard = DashboardPage(self._config, self._log_from_page)
        self._stack.addWidget(self._dashboard)

        # Page 1 — DoVi / HDR10+ (fonctionnelle)
        self._dovi_panel = MergeDoviPanel(self._config)
        self._stack.addWidget(self._dovi_panel)

        # Page 2 — Encodage (fonctionnelle)
        self._encode_panel = EncodePanel(self._config)
        self._stack.addWidget(self._encode_panel)

        # Page 3 — Manipulation Conteneur (fonctionnelle)
        self._remux_panel = RemuxPanel(self._config)
        self._stack.addWidget(self._remux_panel)

        self._stack.addWidget(_PlaceholderPage(
            "Paramètres",
            "⚙",
            "Chemins des outils externes, dossiers de travail\n"
            "et de sortie, préférences d'encodage.",
        ))

        self._page_area = QScrollArea()
        self._page_area.setWidgetResizable(True)
        self._page_area.setFrameShape(QFrame.Shape.NoFrame)
        self._page_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._page_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._page_area.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        self._page_area.setWidget(self._stack)

        top_layout.addWidget(self._page_area, stretch=1)
        top_vbox.addWidget(content_widget, stretch=1)

        # ── Barre d'action globale ───────────────────────────────────────────
        top_vbox.addWidget(self._build_action_bar())

        vsplit.addWidget(top_widget)

        # Partie basse : log panel
        self._log_panel = LogPanel(max_lines=self._config.log_max_lines)
        self._log_panel.setMinimumHeight(32)
        vsplit.addWidget(self._log_panel)

        # Proportion initiale : 70% / 30%
        vsplit.setSizes([560, 240])

        main_layout.addWidget(vsplit)

    def _build_action_bar(self) -> QWidget:
        """Construit la barre d'action globale avec bouton unique 'Exécuter l'opération'."""
        bar = QWidget()
        bar.setStyleSheet(
            f"QWidget{{background:{_Colors.BG_PANEL};"
            f"border-top:1px solid {_Colors.BORDER};}}"
        )
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(28, 10, 28, 10)
        layout.setSpacing(12)

        # Zone progress (barre fine + légende)
        self._prog_widget = QWidget()
        self._prog_widget.setStyleSheet("background:transparent;")
        pv = QVBoxLayout(self._prog_widget)
        pv.setContentsMargins(0, 4, 0, 4)
        pv.setSpacing(4)

        self._prog_bar = QProgressBar()
        self._prog_bar.setRange(0, 100)
        self._prog_bar.setValue(0)
        self._prog_bar.setFixedHeight(6)
        self._prog_bar.setTextVisible(False)
        self._prog_bar.setStyleSheet(
            f"QProgressBar{{background:{_Colors.BG_CARD};border:none;border-radius:3px;}}"
            f"QProgressBar::chunk{{background:{_Colors.ACCENT};border-radius:3px;}}"
        )
        pv.addWidget(self._prog_bar)

        self._prog_lbl = QLabel("")
        self._prog_lbl.setStyleSheet(
            f"color:{_Colors.TEXT_DIM};font-size:10px;"
            f"font-family:'JetBrains Mono',monospace;background:transparent;"
        )
        pv.addWidget(self._prog_lbl)

        self._prog_widget.setVisible(False)
        layout.addWidget(self._prog_widget, stretch=1)

        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet(
            f"color:{_Colors.TEXT_SEC};font-size:11px;background:transparent;"
        )
        layout.addWidget(self._status_lbl)
        layout.addSpacing(4)

        # Bouton principal unique
        self._run_btn = QPushButton("▶  Exécuter l'opération")
        self._run_btn.setFixedHeight(36)
        self._run_btn.setMinimumWidth(220)
        self._run_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._run_btn.setEnabled(False)
        self._run_btn.setStyleSheet(f"""
            QPushButton{{
                background:{_Colors.ACCENT};color:#ffffff;
                border:none;border-radius:6px;
                font-size:12px;font-weight:700;padding:0 20px;
            }}
            QPushButton:hover{{background:#6070f8;}}
            QPushButton:pressed{{background:{_Colors.ACCENT_DIM};}}
            QPushButton:disabled{{background:{_Colors.BG_CARD};
                color:{_Colors.TEXT_DIM};border:1px solid {_Colors.BORDER};}}
        """)
        self._run_btn.clicked.connect(self._on_run)
        layout.addWidget(self._run_btn)

        # Bouton annulation
        self._cancel_btn = QPushButton("✕  Annuler")
        self._cancel_btn.setFixedWidth(110)
        self._cancel_btn.setFixedHeight(36)
        self._cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._cancel_btn.setStyleSheet(f"""
            QPushButton{{background:{_Colors.BG_CARD};color:#f5c842;
                border:1px solid #f5c842;border-radius:6px;
                font-size:12px;font-weight:600;padding:0 14px;}}
            QPushButton:hover{{background:#2a2010;border-color:#f0b030;color:#f0b030;}}
            QPushButton:pressed{{background:#1a1608;}}
        """)
        self._cancel_btn.setVisible(False)
        self._cancel_btn.clicked.connect(self._on_cancel_op)
        layout.addWidget(self._cancel_btn)

        return bar

    # ------------------------------------------------------------------
    # Signaux
    # ------------------------------------------------------------------

    def _connect_signals(self) -> None:
        self._sidebar.page_changed.connect(self._stack.setCurrentIndex)
        self.log_requested.connect(self._on_log_requested)
        self._log_panel.collapse_toggled.connect(self._on_log_collapsed)
        # MergeDoviPanel → LogPanel global (QueuedConnection : signal émis depuis threads)
        self._dovi_panel.log_message.connect(
            self.log_requested, Qt.ConnectionType.QueuedConnection
        )
        # EncodePanel → LogPanel global
        self._encode_panel.log_message.connect(
            self.log_requested, Qt.ConnectionType.QueuedConnection
        )
        # RemuxPanel → LogPanel global
        self._remux_panel.log_message.connect(
            self.log_requested, Qt.ConnectionType.QueuedConnection
        )
        # RemuxPanel → EncodePanel : pistes partagées + chemin de sortie commun
        self._remux_panel.video_tracks_changed.connect(self._encode_panel.set_video_tracks)
        self._remux_panel.audio_tracks_changed.connect(self._encode_panel.set_audio_tracks)
        self._encode_panel.audio_track_meta_changed.connect(self._remux_panel.update_audio_track_meta)
        self._encode_panel.set_output_provider(self._remux_panel.current_output_path)
        self._encode_panel.set_file_title_provider(self._remux_panel.current_file_title)
        self._encode_panel.set_extra_attachments_provider(self._remux_panel.current_extra_attachments)
        self._encode_panel.set_tag_overrides_provider(self._remux_panel.current_tag_overrides)
        self._encode_panel.set_chapters_provider(self._remux_panel.current_chapter_overrides)
        # État "prêt" → bouton Exécuter
        self._remux_panel.ready_changed.connect(self._on_ready_changed)
        self._encode_panel.ready_changed.connect(self._on_ready_changed)

    # ------------------------------------------------------------------
    # Bouton "Exécuter l'opération" — logique hybride remux / encode
    # ------------------------------------------------------------------

    _NOISE_RE = re.compile(r"libvmaf\s+ERROR|could not read model from path")

    def _on_ready_changed(self, _ready: bool) -> None:
        """Met à jour l'état du bouton selon la disponibilité des deux panneaux."""
        enabled = (self._remux_panel.is_ready() or self._encode_panel.collect_config() is not None)
        self._run_btn.setEnabled(enabled and not self._running)

    def _on_run(self) -> None:
        if self._running:
            return

        remux_cfg  = self._remux_panel.collect_config()
        encode_cfg = self._encode_panel.collect_config()

        # ── Décision du mode ────────────────────────────────────────────────
        # Encode si une source vidéo est sélectionnée ET (codec ≠ copy ou HDR actif)
        if encode_cfg is None:
            use_encode = False
        else:
            use_encode = not self._encode_panel.is_pure_copy(encode_cfg)

        if use_encode:
            assert encode_cfg is not None
            # Enrichir l'encode config avec les sous-titres / chapitres du remux
            if remux_cfg is not None:
                encode_cfg = self._merge_remux_extras(encode_cfg, remux_cfg)
            errors = self._encode_panel.validate_config(encode_cfg)
            if errors:
                for e in errors:
                    self.log_requested.emit("ERROR", e)
                return
            self._op_mode = "encode"
            self.log_requested.emit("INFO", f"Encodage → {encode_cfg.output.name}")
            try:
                signals = self._encode_panel.run_operation(encode_cfg)
            except EncodeError as exc:
                self.log_requested.emit("ERROR", str(exc))
                return

        elif remux_cfg is not None:
            errors = self._remux_panel.validate_config(remux_cfg)
            if errors:
                for e in errors:
                    self.log_requested.emit("ERROR", e)
                return
            self._op_mode = "remux"
            self.log_requested.emit("INFO", f"Remuxage → {remux_cfg.output.name}")
            try:
                signals = self._remux_panel.run_operation(remux_cfg)
            except RemuxError as exc:
                self.log_requested.emit("ERROR", str(exc))
                return

        else:
            self.log_requested.emit("WARN", "Aucune opération configurée.")
            return

        # ── Connexion des signaux de progression ────────────────────────────
        self._running  = True
        self._op_start = time.monotonic()
        self._signals  = signals
        self._run_btn.setEnabled(False)
        self._cancel_btn.setVisible(True)
        self._prog_bar.setValue(0)
        self._prog_lbl.setText("")
        self._prog_widget.setVisible(True)
        label = "Encodage en cours…" if self._op_mode == "encode" else "Remuxage en cours…"
        self._status_lbl.setText(label)

        signals.progress.connect(self._on_op_progress, Qt.ConnectionType.QueuedConnection)
        signals.finished.connect(
            lambda _: self._on_op_finished(success=True),
            Qt.ConnectionType.QueuedConnection,
        )
        signals.failed.connect(
            lambda msg, _exc: self._on_op_finished(success=False, error=msg),
            Qt.ConnectionType.QueuedConnection,
        )
        signals.cancelled.connect(self._on_op_cancelled, Qt.ConnectionType.QueuedConnection)

    def _merge_remux_extras(
        self,
        encode_cfg: "EncodeConfig",
        remux_cfg: "RemuxConfig",
    ) -> "EncodeConfig":
        """
        Enrichit l'encode config avec les informations du remux panel :
          - sous-titres multi-sources (subtitle_tracks)
          - attachements MKV (attachment_sources)
          - balises MKV (tag_sources)
          - chapitres (keep_chapters)
          - éditions langue/titre de pistes (track_meta_edits)

        Retourne une EncodeConfig enrichie. Si le remux panel n'apporte rien,
        retourne encode_cfg inchangé (sauf keep_chapters toujours synchronisé).
        """
        from core.workflows.encode.models import EncodeConfig, TrackMetaEdit

        sub_tracks:         list = []
        attachment_streams: list = []   # list[tuple[Path, int]]  — (source, ffprobe_stream_index)
        tag_sources:        list = []

        source_by_index = {src.file_index: src for src in remux_cfg.sources}
        remux_track_map: dict[tuple, object] = {}

        for src in remux_cfg.sources:
            for track in src.tracks:
                remux_track_map[(src.path, track.mkv_tid)] = track
            for att in src.selected_attachments:
                attachment_streams.append((src.path, att.index))
            if src.copy_tags:
                tag_sources.append(src.path)

        ordered_tracks: list[tuple[Path, object]] = []
        for file_index, mkv_tid in remux_cfg.track_order:
            src = source_by_index.get(file_index)
            if src is None:
                continue
            track = remux_track_map.get((src.path, mkv_tid))
            if track is None:
                continue
            ordered_tracks.append((src.path, track))

        sub_tracks = [
            (src_path, track.mkv_tid)
            for src_path, track in ordered_tracks
            if getattr(track, "track_type", None) == "subtitle"
        ]

        # tag_overrides depuis RemuxConfig (balises éditées dans l'UI)
        # Prioritaire sur tag_sources : si présent, on ignore tag_sources pour l'encode.
        tag_overrides = remux_cfg.tag_overrides

        # --- Métadonnées de pistes (langue + titre) via mkvpropedit post-encodage ---
        # ffmpeg ne préserve pas les métadonnées de pistes (langue, titre).
        # On les réécrit systématiquement pour toutes les pistes ayant des métadonnées.
        #
        # Ordre des pistes dans le fichier de sortie ffmpeg :
        #   @1 = vidéo  |  @2…@N+1 = audio  |  @N+2… = sous-titres
        track_meta_edits: list[TrackMetaEdit] = []

        def _make_edit(track_order: int, t) -> "TrackMetaEdit | None":
            """Retourne un TrackMetaEdit si la piste a une langue ou un titre à écrire."""
            lang  = t.language or ""
            title = t.title    or ""
            if not lang and not title:
                return None
            return TrackMetaEdit(
                track_order = track_order,
                language    = lang,
                title       = title if title else None,
            )

        def _find_track(src_path, stream_index, track_type):
            t = remux_track_map.get((src_path, stream_index))
            if t is not None:
                return t
            # Fichier source unique : cherche uniquement par stream_index + type
            for entry in remux_track_map.values():
                if getattr(entry, "mkv_tid", None) == stream_index and \
                   getattr(entry, "track_type", None) == track_type:
                    return entry
            return None

        # @1 — piste vidéo (toujours depuis encode_cfg.source)
        video_entry = _find_track(encode_cfg.source, 0, "video")
        if video_entry is None:
            # La vidéo peut être sur n'importe quel stream_index ; garde le premier ordre remux.
            for _src_path, entry in ordered_tracks:
                if getattr(entry, "track_type", None) == "video":
                    video_entry = entry
                    break
        if video_entry is None:
            for entry in remux_track_map.values():
                if getattr(entry, "track_type", None) == "video":
                    video_entry = entry
                    break
        if video_entry is not None:
            edit = _make_edit(1, video_entry)
            if edit:
                track_meta_edits.append(edit)

        # @2+ — pistes audio
        audio_offset = 2
        for audio_order, ats in enumerate(encode_cfg.audio_tracks):
            src_path = ats.source_path or encode_cfg.source
            t = _find_track(src_path, ats.stream_index, "audio")
            if t is None:
                continue
            edit = _make_edit(audio_offset + audio_order, t)
            if edit:
                track_meta_edits.append(edit)

        # @N+2+ — pistes sous-titres
        sub_offset = audio_offset + len(encode_cfg.audio_tracks)
        used_sub_tracks = sub_tracks or encode_cfg.subtitle_tracks
        for sub_order, (sub_path, sub_sid) in enumerate(used_sub_tracks):
            t = remux_track_map.get((sub_path, sub_sid))
            if t is None:
                continue
            edit = _make_edit(sub_offset + sub_order, t)
            if edit:
                track_meta_edits.append(edit)

        chapter_overrides = remux_cfg.chapter_overrides

        # Rien à fusionner et keep_chapters / chapter_overrides identiques → pas de reconstruction
        if (not sub_tracks and not attachment_streams and not tag_sources
                and tag_overrides is None
                and not track_meta_edits
                and encode_cfg.keep_chapters == remux_cfg.keep_chapters
                and remux_cfg.chapter_overrides is None):
            return encode_cfg

        return EncodeConfig(
            source=encode_cfg.source,
            output=encode_cfg.output,
            video=encode_cfg.video,
            audio_tracks=encode_cfg.audio_tracks,
            copy_subtitles=encode_cfg.copy_subtitles if not sub_tracks else False,
            subtitle_tracks=sub_tracks or encode_cfg.subtitle_tracks,
            keep_chapters=remux_cfg.keep_chapters,
            chapter_overrides=chapter_overrides,
            attachment_streams=attachment_streams,
            tag_sources=[] if tag_overrides is not None else tag_sources,
            tag_overrides=tag_overrides,
            track_meta_edits=track_meta_edits,
            duration_s=encode_cfg.duration_s,
            copy_dv=encode_cfg.copy_dv,
            copy_hdr10plus=encode_cfg.copy_hdr10plus,
            dovi_profile=encode_cfg.dovi_profile,
            work_dir=encode_cfg.work_dir,
            file_title=encode_cfg.file_title,
            extra_attachments=encode_cfg.extra_attachments,
        )

    def _on_op_progress(self, line: str) -> None:
        """Gère la progression selon le mode (remux ou encode)."""
        if self._op_mode == "remux":
            if "Progress:" in line:
                try:
                    pct = int(line.split("%")[0].split()[-1])
                    self._prog_bar.setValue(pct)
                    elapsed = time.monotonic() - self._op_start
                    if pct > 0 and elapsed > 0:
                        eta_s = elapsed * (100 - pct) / pct
                        eta_str = f"ETA {_fmt_eta(eta_s)}"
                    else:
                        eta_str = ""
                    parts = [f"{pct}%", eta_str]
                    self._prog_lbl.setText("  ·  ".join(p for p in parts if p))
                except (ValueError, IndexError):
                    pass
            else:
                self.log_requested.emit("INFO", line)
        else:
            if self._NOISE_RE.search(line):
                return
            m = _TIME_RE.search(line)
            if m:
                elapsed_video = (
                    int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                )
                dur = self._encode_panel.get_duration_s()
                if dur and dur > 0:
                    pct = min(99, int(elapsed_video / dur * 100))
                    self._prog_bar.setValue(pct)
                    fps_m = _FPS_RE.search(line)
                    fps_str = f"{float(fps_m.group(1)):.1f} fps" if fps_m else ""
                    elapsed_wall = time.monotonic() - self._op_start
                    if elapsed_wall > 0 and elapsed_video > 0:
                        speed = elapsed_video / elapsed_wall
                        eta_s = (dur - elapsed_video) / speed
                        eta_str = f"ETA {_fmt_eta(eta_s)}"
                    else:
                        eta_str = ""
                    parts = [f"{pct}%", fps_str, eta_str]
                    self._prog_lbl.setText("  ·  ".join(p for p in parts if p))
                return
            self.log_requested.emit("INFO", line)

    def _on_cancel_op(self) -> None:
        reply = QMessageBox.question(
            self,
            "Confirmer l'annulation",
            "Annuler l'opération en cours ?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes and self._signals is not None:
            self._signals.cancel()

    def _on_op_cancelled(self) -> None:
        self._running = False
        self._signals = None
        self._run_btn.setEnabled(True)
        self._cancel_btn.setVisible(False)
        self._prog_widget.setVisible(False)
        self._prog_lbl.setText("")
        self._status_lbl.setText("Annulé.")
        self.log_requested.emit("WARN", "Opération annulée.")

    def _on_op_finished(self, success: bool, error: str = "") -> None:
        self._running = False
        self._signals = None
        self._run_btn.setEnabled(True)
        self._cancel_btn.setVisible(False)
        if success:
            self._prog_bar.setValue(100)
            self._prog_lbl.setText("100%  ·  terminé")
            self._status_lbl.setText("Terminé.")
            label = "Encodage terminé" if self._op_mode == "encode" else "Remuxage terminé"
            self.log_requested.emit("OK", label)
        else:
            self._prog_widget.setVisible(False)
            self._prog_lbl.setText("")
            self._status_lbl.setText("Échec.")
            if error:
                self.log_requested.emit("ERROR", error)

    # ------------------------------------------------------------------
    # Collapse du panneau de logs
    # ------------------------------------------------------------------

    def _on_log_collapsed(self, collapsed: bool) -> None:
        total = self._vsplit.height()
        if collapsed:
            self._vsplit.setSizes([total - 32, 32])
        else:
            log_h = max(total // 5, 120)
            self._vsplit.setSizes([total - log_h, log_h])

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def _log_from_page(self, message: str, level: LogLevel) -> None:
        """Callback passé aux pages pour poster des logs."""
        self._log_panel.log(message, level)

    def _on_log_requested(self, level: str, message: str) -> None:
        """Slot connecté au signal public log_requested(str, str)."""
        try:
            lv = LogLevel(level.upper())
        except ValueError:
            lv = LogLevel.INFO
        self._log_panel.log(message, lv)

    # Raccourcis directs
    def log_info(self, msg: str)  -> None: self._log_panel.info(msg)
    def log_ok(self, msg: str)    -> None: self._log_panel.ok(msg)
    def log_warn(self, msg: str)  -> None: self._log_panel.warn(msg)
    def log_error(self, msg: str) -> None: self._log_panel.error(msg)

    # ------------------------------------------------------------------
    # Géométrie
    # ------------------------------------------------------------------

    def _restore_geometry(self) -> None:
        if self._config.window_geometry:
            self.restoreGeometry(self._config.window_geometry)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._config.save_geometry(self.saveGeometry().data())
        self._config.save()
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # Post-init
    # ------------------------------------------------------------------

    def _post_init_log(self) -> None:
        self._log_panel.info("Mediarecode démarré.")
        availability = self._config.all_tools_available()
        missing = [n for n, ok in availability.items() if not ok]
        if missing:
            self._log_panel.warn(
                f"Outils manquants dans PATH : {', '.join(missing)}"
            )
        else:
            self._log_panel.ok("Tous les outils externes sont disponibles.")
        self._log_panel.info(
            f"Dossier de travail : {self._config.work_dir}"
        )
        self._log_panel.info(
            f"Dossier de sortie  : {self._config.output_dir}"
        )
