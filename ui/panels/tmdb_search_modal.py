"""
ui/panels/tmdb_search_modal.py — Modale de recherche TMDB.

Contient l'interface et la logique backend de la recherche de métadonnées
film/série (requêtes TMDB asynchrones + récupération des détails).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from core.i18n import apply_translations, translate_text
from core.media_info_fetcher import (
    MediaDetails,
    MediaSearchResult,
    TmdbFetcher,
    default_tmdb_bearer_token,
    normalize_tmdb_search_query,
)
from ui.design_system import colors as _C, font_px as _font_px, scale as _scale

if TYPE_CHECKING:
    from core.config import AppConfig

_SEASON_EPISODE_RE = (
    re.compile(
        r"(?<!\d)[s](?P<season>\d{1,2})[\s._-]*[e](?P<episode>\d{1,4})(?!\d)",
        re.IGNORECASE,
    ),
    re.compile(r"(?<!\d)(?P<season>\d{1,2})\s*[xX]\s*(?P<episode>\d{1,4})(?!\d)"),
)


def extract_season_episode(text: str) -> tuple[int, int] | None:
    """
    Extrait (saison, épisode) depuis un texte libre.

    Formats supportés (casse insensible) :
        - s01 e01
        - .s01.e01.
        - .s01e01.
        - .s1e1.
        - .01x01.
    """
    raw = text.strip()
    if not raw:
        return None
    for rx in _SEASON_EPISODE_RE:
        m = rx.search(raw)
        if not m:
            continue
        season = int(m.group("season"))
        episode = int(m.group("episode"))
        if season <= 0 or episode <= 0:
            continue
        return season, episode
    return None


class _TmdbSearchWorker(QThread):
    """
    Thread secondaire pour les appels TMDB (recherche et détails).

    Deux modes :
        - query fourni  → appelle fetcher.search(), émet results_ready.
        - result fourni → appelle fetcher.get_details(), émet details_ready.
    """

    results_ready = Signal(list)    # list[MediaSearchResult]
    details_ready = Signal(object)  # MediaDetails
    error = Signal(str)

    def __init__(
        self,
        fetcher: TmdbFetcher,
        *,
        query: str | None = None,
        result: MediaSearchResult | None = None,
        season: str = "",
        episode: str = "",
        kind: str = "all",
        year: str = "",
    ) -> None:
        super().__init__()
        self._fetcher: TmdbFetcher = fetcher
        self._query: str | None = query
        self._result: MediaSearchResult | None = result
        self._season = season
        self._episode = episode
        self._kind = kind
        self._year = year

    def run(self) -> None:
        try:
            if self._query is not None:
                results = self._fetcher.search(self._query, kind=self._kind, year=self._year)
                self.results_ready.emit(results)
            elif self._result is not None:
                details = self._fetcher.get_details(
                    self._result,
                    season=self._season,
                    episode=self._episode,
                )
                self.details_ready.emit(details)
        except Exception as exc:
            self.error.emit(str(exc))


class TmdbSearchModal(QDialog):
    """
    Modale de recherche film/série pour récupérer les métadonnées TMDB.

    Attribut public après accept() :
        fetched_details — MediaDetails | None
    """

    fetched_details: MediaDetails | None = None

    def __init__(
        self,
        config: AppConfig,
        suggested_title: str = "",
        suggested_season: int = 0,
        suggested_episode: int = 0,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._results: list[MediaSearchResult] = []
        self._worker: _TmdbSearchWorker | None = None

        self.setWindowTitle("Recherche film / série — IMDb / TMDB")
        self.setMinimumSize(_scale(560), _scale(560))
        self.setModal(True)
        self.setStyleSheet(f"""
            QDialog  {{ background: {_C.BG_DEEP}; color: {_C.TEXT_PRI}; }}
            QLabel   {{ color: {_C.TEXT_PRI}; background: transparent; border: none; font-size: {_font_px(11)}px; }}
            QGroupBox {{ color: {_C.TEXT_DIM}; border: 1px solid {_C.BORDER}; border-radius: {_scale(4)}px;
                         font-size: {_font_px(9)}px; font-weight: 700; letter-spacing: {_scale(1)}px;
                         margin-top: {_scale(8)}px; padding-top: {_scale(6)}px; }}
            QGroupBox::title {{ subcontrol-origin: margin; left: {_scale(8)}px; }}
        """)
        self._build_ui(suggested_title, suggested_season, suggested_episode)
        apply_translations(self)

    def _build_ui(
        self,
        suggested_title: str,
        suggested_season: int = 0,
        suggested_episode: int = 0,
    ) -> None:
        detected_from_title = extract_season_episode(suggested_title)
        effective_season = suggested_season if suggested_season > 0 else (
            detected_from_title[0] if detected_from_title is not None else 0
        )
        effective_episode = suggested_episode if suggested_episode > 0 else (
            detected_from_title[1] if detected_from_title is not None else 0
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(_scale(14), _scale(14), _scale(14), _scale(14))
        root.setSpacing(_scale(10))

        search_row = QWidget()
        search_row.setStyleSheet("background: transparent;")
        search_h = QHBoxLayout(search_row)
        search_h.setContentsMargins(0, 0, 0, 0)
        search_h.setSpacing(_scale(6))

        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("Titre du film ou de la série…")
        self._search_edit.setText(suggested_title)
        self._search_edit.setStyleSheet(self._line_style())
        self._search_edit.returnPressed.connect(self._on_search)
        search_h.addWidget(self._search_edit, stretch=1)

        self._kind_combo = QComboBox()
        self._kind_combo.addItems(["Tout", "Films", "Séries"])
        if effective_season > 0 and effective_episode > 0:
            self._kind_combo.setCurrentIndex(2)
        else:
            self._kind_combo.setCurrentIndex(0)
        self._kind_combo.setFixedWidth(_scale(90))
        self._kind_combo.setStyleSheet(f"""
            QComboBox {{
                background: {_C.BG_PANEL}; color: {_C.TEXT_PRI};
                border: 1px solid {_C.BORDER}; border-radius: {_scale(4)}px;
                font-size: {_font_px(11)}px; padding: {_scale(2)}px {_scale(6)}px;
            }}
            QComboBox::drop-down {{ border: none; }}
            QComboBox QAbstractItemView {{
                background: {_C.BG_PANEL}; color: {_C.TEXT_PRI};
                selection-background-color: {_C.ACCENT_DIM};
            }}
        """)
        search_h.addWidget(self._kind_combo)

        self._search_btn = QPushButton("Rechercher")
        self._search_btn.setFixedHeight(_scale(28))
        self._search_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._search_btn.setStyleSheet(f"""
            QPushButton {{
                background: {_C.ACCENT}; color: #fff;
                border: none; border-radius: {_scale(4)}px;
                font-size: {_font_px(11)}px; font-weight: 600; padding: 0 {_scale(14)}px;
            }}
            QPushButton:hover {{ background: #6070f8; }}
            QPushButton:disabled {{ background: {_C.BG_PANEL}; color: {_C.TEXT_DIM}; }}
        """)
        self._search_btn.clicked.connect(self._on_search)
        search_h.addWidget(self._search_btn)
        root.addWidget(search_row)

        self._results_list = QListWidget()
        self._results_list.setMinimumHeight(_scale(200))
        self._results_list.setStyleSheet(f"""
            QListWidget {{
                background: {_C.BG_CARD}; color: {_C.TEXT_PRI};
                border: 1px solid {_C.BORDER}; border-radius: {_scale(4)}px;
                font-size: {_font_px(11)}px;
            }}
            QListWidget::item {{ padding: {_scale(6)}px {_scale(10)}px; border-bottom: 1px solid {_C.BORDER}; }}
            QListWidget::item:selected {{
                background: {_C.ACCENT_DIM}; color: #fff;
            }}
            QListWidget::item:hover:!selected {{ background: {_C.BG_PANEL}; }}
        """)
        self._results_list.currentRowChanged.connect(self._on_row_changed)
        self._results_list.itemDoubleClicked.connect(lambda _: self._fetch_details())
        root.addWidget(self._results_list, stretch=1)

        self._overview_lbl = QLabel()
        self._overview_lbl.setWordWrap(True)
        self._overview_lbl.setMaximumHeight(_scale(60))
        self._overview_lbl.setStyleSheet(
            f"color: {_C.TEXT_DIM}; font-size: {_font_px(10)}px; font-style: italic; "
            f"background: {_C.BG_CARD}; border: 1px solid {_C.BORDER}; "
            f"border-radius: {_scale(4)}px; padding: {_scale(4)}px {_scale(8)}px;"
        )
        self._overview_lbl.setVisible(False)
        root.addWidget(self._overview_lbl)

        self._series_row = QWidget()
        self._series_row.setStyleSheet("background: transparent;")
        series_h = QHBoxLayout(self._series_row)
        series_h.setContentsMargins(0, 0, 0, 0)
        series_h.setSpacing(_scale(12))

        def _spin_label(text: str) -> QLabel:
            lbl = QLabel(text)
            lbl.setStyleSheet(f"color: {_C.TEXT_DIM}; font-size: {_font_px(10)}px; font-weight: 600;")
            return lbl

        series_h.addWidget(_spin_label("Saison :"))
        self._season_spin = QSpinBox()
        self._season_spin.setRange(0, 99)
        self._season_spin.setSpecialValueText("—")
        self._season_spin.setFixedWidth(_scale(60))
        self._season_spin.setStyleSheet(self._spin_style())
        if effective_season > 0:
            self._season_spin.setValue(min(effective_season, 99))
        series_h.addWidget(self._season_spin)

        series_h.addWidget(_spin_label("Épisode :"))
        self._episode_spin = QSpinBox()
        self._episode_spin.setRange(0, 9999)
        self._episode_spin.setSpecialValueText("—")
        self._episode_spin.setFixedWidth(_scale(72))
        self._episode_spin.setStyleSheet(self._spin_style())
        if effective_episode > 0:
            self._episode_spin.setValue(min(effective_episode, 9999))
        series_h.addWidget(self._episode_spin)
        series_h.addStretch()

        self._series_row.setVisible(False)
        root.addWidget(self._series_row)

        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet(f"color: {_C.TEXT_DIM}; font-size: {_font_px(10)}px;")
        root.addWidget(self._status_lbl)

        bottom_row = QWidget()
        bottom_row.setStyleSheet("background: transparent;")
        bottom_h = QHBoxLayout(bottom_row)
        bottom_h.setContentsMargins(0, 0, 0, 0)
        bottom_h.setSpacing(_scale(8))

        _logo_path = Path(__file__).parent.parent / "assets" / "tmdb_logo.svg"
        logo_lbl = QLabel()
        logo_lbl.setFixedSize(_scale(80), _scale(34))
        logo_lbl.setToolTip("This product uses the TMDB API")
        if _logo_path.exists():
            renderer = QSvgRenderer(str(_logo_path))
            pix = QPixmap(_scale(80), _scale(34))
            pix.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pix)
            renderer.render(painter)
            painter.end()
            logo_lbl.setPixmap(pix)
        bottom_h.addWidget(logo_lbl)

        tos_lbl = QLabel(
            "This product uses the TMDB API but is not\n"
            "endorsed or certified by TMDB."
        )
        tos_lbl.setStyleSheet(
            f"color: {_C.TEXT_DIM}; font-size: {_font_px(9)}px; background: transparent;"
        )
        bottom_h.addWidget(tos_lbl)
        bottom_h.addStretch()

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._ok_btn = btns.button(QDialogButtonBox.StandardButton.Ok)
        self._ok_btn.setText("Insérer les balises")
        self._ok_btn.setEnabled(False)
        cancel_btn = btns.button(QDialogButtonBox.StandardButton.Cancel)
        if cancel_btn is not None:
            cancel_btn.setText("Annuler")
        btns.accepted.connect(self._fetch_details)
        btns.rejected.connect(self.reject)
        btns.setStyleSheet(f"""
            QPushButton {{
                background: {_C.BG_PANEL}; color: {_C.TEXT_PRI};
                border: 1px solid {_C.BORDER_LT}; border-radius: {_scale(4)}px;
                font-size: {_font_px(11)}px; padding: {_scale(4)}px {_scale(16)}px;
            }}
            QPushButton:hover {{ background: {_C.BG_CARD}; }}
            QPushButton:disabled {{ color: {_C.TEXT_DIM}; border-color: {_C.BORDER}; }}
        """)
        bottom_h.addWidget(btns)
        root.addWidget(bottom_row)

        if suggested_title:
            QTimer.singleShot(0, self._on_search)

    def _line_style(self) -> str:
        return (
            f"QLineEdit {{ background: {_C.BG_PANEL}; color: {_C.TEXT_PRI}; "
            f"border: 1px solid {_C.BORDER}; border-radius: {_scale(4)}px; "
            f"font-size: {_font_px(11)}px; padding: {_scale(4)}px {_scale(8)}px; }} "
            f"QLineEdit:focus {{ border-color: {_C.ACCENT}; }}"
        )

    def _spin_style(self) -> str:
        return (
            f"QSpinBox {{ background: {_C.BG_PANEL}; color: {_C.TEXT_PRI}; "
            f"border: 1px solid {_C.BORDER}; border-radius: {_scale(4)}px; "
            f"font-size: {_font_px(11)}px; padding: {_scale(2)}px {_scale(4)}px; }} "
            f"QSpinBox:focus {{ border-color: {_C.ACCENT}; }}"
        )

    def _kind_param(self) -> str:
        idx = self._kind_combo.currentIndex()
        return {0: "all", 1: "movie", 2: "tv"}.get(idx, "all")

    def _make_fetcher(self) -> TmdbFetcher | None:
        from core.media_info_fetcher import TmdbError, iso639_2_to_tmdb_lang

        key = self._config.tmdb_api_key.strip()
        bearer_token = self._config.tmdb_bearer_token.strip() or default_tmdb_bearer_token()
        lang = iso639_2_to_tmdb_lang(self._config.language)
        try:
            return TmdbFetcher(api_key=key, language=lang, bearer_token=bearer_token)
        except TmdbError as exc:
            msg = translate_text(str(exc))
            self._status_lbl.setText(translate_text("Erreur : {message}", message=msg))
            return None

    def _set_busy(self, busy: bool) -> None:
        self._search_btn.setEnabled(not busy)
        self._search_edit.setEnabled(not busy)
        self._kind_combo.setEnabled(not busy)
        if busy:
            self._status_lbl.setText(translate_text("Chargement…"))
        else:
            self._status_lbl.setText("")

    def _on_search(self) -> None:
        raw_query = self._search_edit.text().strip()
        if not raw_query:
            return
        fetcher = self._make_fetcher()
        if fetcher is None:
            return

        query, year = normalize_tmdb_search_query(raw_query)

        self._ok_btn.setEnabled(False)
        self._results_list.clear()
        self._results.clear()
        self._overview_lbl.setVisible(False)
        self._series_row.setVisible(False)
        self._set_busy(True)

        if self._worker and self._worker.isRunning():
            self._worker.quit()
            self._worker.wait()

        self._worker = _TmdbSearchWorker(fetcher, query=query, year=year, kind=self._kind_param())
        self._worker.results_ready.connect(self._on_results, Qt.ConnectionType.QueuedConnection)
        self._worker.error.connect(self._on_error, Qt.ConnectionType.QueuedConnection)
        self._worker.finished.connect(lambda: self._set_busy(False), Qt.ConnectionType.QueuedConnection)
        self._worker.start()

    def _on_results(self, results: list[MediaSearchResult]) -> None:
        self._results = results
        self._results_list.clear()
        if not results:
            self._status_lbl.setText(translate_text("Aucun résultat."))
            return
        for r in results:
            year_str = f" ({r.year})" if r.year else ""
            kind_str = translate_text("Série") if r.kind == "tv" else translate_text("Film")
            item = QListWidgetItem(f"{r.title}{year_str}  —  {kind_str}")
            item.setData(Qt.ItemDataRole.UserRole, r)
            self._results_list.addItem(item)
        self._results_list.setCurrentRow(0)

    def _on_row_changed(self, row: int) -> None:
        if row < 0 or row >= len(self._results):
            self._ok_btn.setEnabled(False)
            self._overview_lbl.setVisible(False)
            self._series_row.setVisible(False)
            return
        r = self._results[row]
        is_tv = r.kind == "tv"
        self._series_row.setVisible(is_tv)
        if r.overview:
            text = r.overview if len(r.overview) <= 200 else r.overview[:200] + "…"
            self._overview_lbl.setText(text)
            self._overview_lbl.setVisible(True)
        else:
            self._overview_lbl.setVisible(False)
        self._ok_btn.setEnabled(True)

    def _fetch_details(self) -> None:
        row = self._results_list.currentRow()
        if row < 0 or row >= len(self._results):
            return
        r = self._results[row]

        fetcher = self._make_fetcher()
        if fetcher is None:
            return

        season = str(self._season_spin.value()) if self._series_row.isVisible() and self._season_spin.value() > 0 else ""
        episode = str(self._episode_spin.value()) if self._series_row.isVisible() and self._episode_spin.value() > 0 else ""

        self._ok_btn.setEnabled(False)
        self._set_busy(True)

        if self._worker and self._worker.isRunning():
            self._worker.quit()
            self._worker.wait()

        self._worker = _TmdbSearchWorker(fetcher, result=r, season=season, episode=episode)
        self._worker.details_ready.connect(self._on_details, Qt.ConnectionType.QueuedConnection)
        self._worker.error.connect(self._on_error, Qt.ConnectionType.QueuedConnection)
        self._worker.start()

    def _on_details(self, details: MediaDetails) -> None:
        self.fetched_details = details
        super().accept()

    def _on_error(self, msg: str) -> None:
        self._set_busy(False)
        self._ok_btn.setEnabled(self._results_list.currentRow() >= 0)
        self._status_lbl.setText(f"⚠ {translate_text(msg)}")

    def closeEvent(self, event) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.quit()
            self._worker.wait()
        super().closeEvent(event)
