"""Panneau principal RemuxPanel."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QDropEvent, QFont
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.config import AppConfig
from core.extractor import TrackExtractor
from core.file_types import is_accepted
from core.i18n import apply_translations, translate_text
from core.matroska_attachment_extractor import extract_matroska_attachment_bytes
from core.inspector import AttachmentInfo, ChapterEntry, FileInfo
from core.runner import TaskSignals, ToolRunner
from core.workflows.remux import RemuxWorkflow
from core.workflows.remux_models import RemuxConfig, SourceInput, TrackEntry
from ui.panels.remux_panel.functions import chapters as chapter_functions
from ui.panels.remux_panel.functions import config_builder, inspection, signals, tmdb
from ui.panels.remux_panel.models import SourceFile
from ui.panels.remux_panel.theme import (
    _C,
    _card,
    _input_style,
    _secondary_button,
    _section_label,
    _separator,
)
from ui.design_system import font_px as _font_px, scale as _scale
from ui.panels.remux_panel.widgets.attachments import _AttachmentPanel
from ui.panels.remux_panel.widgets.chapters import _ChapterPanel
from ui.panels.remux_panel.widgets.file_list import _FileListWidget
from ui.panels.remux_panel.widgets.track_table import _TrackTable


class RemuxPanel(QWidget):
    """
    Panneau de remuxage MKV/MP4 — support multi-sources.

    Signaux :
        log_message(level: str, message: str)
        video_tracks_changed(list)  — pistes vidéo activées (FileInfo, TrackEntry, couleur)
        audio_tracks_changed(list)  — pistes audio activées (AudioTrack, couleur, Path source)
        ready_changed(bool)         — True quand au moins un fichier est inspecté
    """

    log_message = Signal(str, str)

    _inspection_done = Signal(str, object)
    _inspection_error = Signal(str, str)

    video_tracks_changed = Signal(object)
    audio_tracks_changed = Signal(object)
    ready_changed = Signal(bool)

    def __init__(
        self,
        config: AppConfig,
        parent: QWidget | None = None,
        *,
        writing_application: str = "",
    ) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self._config = config
        self._writing_application = writing_application
        self._workflow: RemuxWorkflow = self._make_workflow()
        self._executor = ThreadPoolExecutor(max_workers=2)

        self._source_files: list[SourceFile] = []
        self._source_names: dict[str, str] = {}
        self._source_colors: dict[str, str] = {}
        self._color_index: int = 0

        self._workflow.log_message.connect(
            self.log_message, Qt.ConnectionType.QueuedConnection
        )
        self._inspection_done.connect(
            self._apply_inspection, Qt.ConnectionType.QueuedConnection
        )
        self._inspection_error.connect(
            self._on_inspection_error, Qt.ConnectionType.QueuedConnection
        )

        self._build_ui()
        apply_translations(self)

    def _make_workflow(self) -> RemuxWorkflow:
        return RemuxWorkflow(
            ffmpeg_bin=self._config.tool_ffmpeg,
            ffprobe_bin=self._config.tool_ffprobe,
            ffmpeg_threads=self._config.ffmpeg_threads,
            writing_application=self._writing_application,
            generate_nfo=self._config.generate_nfo,
            mediainfo_bin=self._config.tool_mediainfo,
        )

    def _recreate_workflow(self) -> None:
        try:
            self._workflow.log_message.disconnect(self.log_message)
        except Exception:
            pass
        self._workflow = self._make_workflow()
        self._workflow.log_message.connect(
            self.log_message, Qt.ConnectionType.QueuedConnection
        )

    def _build_ui(self) -> None:
        self.setStyleSheet(f"background: {_C.BG_DEEP};")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet(f"""
            QScrollArea {{ background: {_C.BG_DEEP}; border: none; }}
            QScrollBar:vertical {{
                background: {_C.BG_DEEP};
                width: {_scale(6)}px;
                border: none;
            }}
            QScrollBar::handle:vertical {{
                background: {_C.BORDER_LT};
                border-radius: {_scale(3)}px;
                min-height: {_scale(24)}px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
        """)

        content = QWidget()
        content.setStyleSheet(f"background: {_C.BG_DEEP};")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(_scale(28), _scale(24), _scale(28), _scale(24))
        content_layout.setSpacing(_scale(20))
        content_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinAndMaxSize)

        title = QLabel("Manipulation Conteneur")
        title.setStyleSheet(f"""
            font-size: {_font_px(20)}px;
            font-weight: 800;
            color: {_C.TEXT_PRI};
            background: transparent;
            letter-spacing: -{_scale(1)}px;
        """)
        subtitle = QLabel("Remuxage, fusion et sélection de pistes (vidéo/audio/sous-titres externes) — sans réencodage")
        subtitle.setStyleSheet(f"color: {_C.TEXT_SEC}; font-size: {_font_px(12)}px; background: transparent;")
        content_layout.addWidget(title)
        content_layout.addWidget(subtitle)
        content_layout.addWidget(_separator())

        content_layout.addWidget(_section_label("FICHIERS SOURCES"))
        self._file_list = _FileListWidget()
        self._file_list.add_requested.connect(self._on_add_files)
        self._file_list.remove_requested.connect(self._on_remove_file)
        content_layout.addWidget(self._file_list)

        content_layout.addWidget(_separator())

        track_header = QHBoxLayout()
        track_header.setSpacing(_scale(8))
        track_header.addWidget(_section_label("PISTES"))
        track_header.addStretch()

        btn_all = _secondary_button("Tout activer")
        btn_none = _secondary_button("Tout désactiver")
        btn_all.clicked.connect(lambda: self._set_all_tracks(True))
        btn_none.clicked.connect(lambda: self._set_all_tracks(False))
        track_header.addWidget(btn_all)
        track_header.addWidget(btn_none)

        self._filter_btn = QPushButton("Sélectionnées seulement")
        self._filter_btn.setCheckable(True)
        self._filter_btn.setFixedHeight(_scale(28))
        self._filter_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._filter_btn.setStyleSheet(f"""
            QPushButton {{
                background: {_C.BG_CARD};
                color: {_C.TEXT_SEC};
                border: 1px solid {_C.BORDER};
                border-radius: {_scale(5)}px;
                font-size: {_font_px(11)}px;
                font-weight: 500;
                padding: 0 {_scale(12)}px;
            }}
            QPushButton:hover {{
                background: {_C.BG_HOVER};
                color: {_C.TEXT_PRI};
                border-color: {_C.BORDER_LT};
            }}
            QPushButton:checked {{
                background: {_C.ACCENT_DIM};
                color: {_C.ACCENT};
                border-color: {_C.ACCENT};
            }}
        """)
        self._filter_btn.toggled.connect(
            lambda checked: self._track_table.set_filter_selected(checked)
        )
        track_header.addWidget(self._filter_btn)
        content_layout.addLayout(track_header)

        hint = QLabel("Glisser-déposer les lignes pour réordonner · Double-clic pour éditer Langue / Titre")
        hint.setStyleSheet(f"color: {_C.TEXT_DIM}; font-size: {_font_px(10)}px; background: transparent;")
        content_layout.addWidget(hint)

        self._track_table = _TrackTable()
        self._track_table.itemChanged.connect(self._on_table_changed)
        self._track_table.order_changed.connect(self._rebuild_preview)
        self._track_table.extract_requested.connect(self._on_extract_track)
        content_layout.addWidget(self._track_table)

        content_layout.addWidget(_separator())

        title_card = _card()
        title_card_layout = QVBoxLayout(title_card)
        title_card_layout.setContentsMargins(_scale(16), _scale(10), _scale(16), _scale(10))
        title_card_layout.setSpacing(_scale(6))
        title_card_layout.addWidget(_section_label("TITRE DU FICHIER"))

        self._file_title_edit = QLineEdit()
        self._file_title_edit.setPlaceholderText("Titre du conteneur MKV (balise Title)")
        self._file_title_edit.setStyleSheet(_input_style())
        self._file_title_edit.textChanged.connect(self._rebuild_preview)
        self._file_title_edit.textChanged.connect(self._sync_tmdb_suggested_title)
        title_card_layout.addWidget(self._file_title_edit)

        content_layout.addWidget(title_card)

        self._attachment_panel = _AttachmentPanel(self._config)
        self._attachment_panel.set_embedded_attachment_loader(
            self._extract_embedded_attachment_bytes
        )
        self._attachment_panel.changed.connect(self._rebuild_preview)
        self._attachment_panel.tmdb_details_selected.connect(self._on_tmdb_details_selected)
        content_layout.addWidget(self._attachment_panel)
        self._sync_tmdb_suggested_title()

        content_layout.addWidget(_separator())

        self._chapter_panel = _ChapterPanel()
        self._chapter_panel.changed.connect(self._on_chapters_changed)
        content_layout.addWidget(self._chapter_panel)

        content_layout.addWidget(_separator())

        content_layout.addWidget(_section_label("FICHIER DE SORTIE"))
        out_row = QHBoxLayout()
        out_row.setSpacing(_scale(8))

        self._output_edit = QLineEdit()
        self._output_edit.setPlaceholderText("/chemin/vers/sortie.mkv")
        self._output_edit.setStyleSheet(_input_style())
        self._output_edit.textChanged.connect(self._rebuild_preview)
        out_row.addWidget(self._output_edit, stretch=1)

        browse_out = _secondary_button("Choisir…")
        browse_out.clicked.connect(self._browse_output)
        out_row.addWidget(browse_out)

        content_layout.addLayout(out_row)
        content_layout.addWidget(_separator())

        cmd_header = QHBoxLayout()
        cmd_header.addWidget(_section_label("APERÇU COMMANDE"))
        cmd_header.addStretch()
        copy_btn = _secondary_button("Copier")
        copy_btn.clicked.connect(self._copy_command)
        cmd_header.addWidget(copy_btn)
        content_layout.addLayout(cmd_header)

        self._cmd_preview = QPlainTextEdit()
        self._cmd_preview.setReadOnly(True)
        self._cmd_preview.setFixedHeight(_scale(120))
        mono = QFont("JetBrains Mono", _font_px(9))
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self._cmd_preview.setFont(mono)
        self._cmd_preview.setStyleSheet(f"""
            QPlainTextEdit {{
                background: {_C.BG_DEEP};
                color: {_C.TEXT_SEC};
                border: 1px solid {_C.BORDER};
                border-radius: {_scale(6)}px;
                padding: {_scale(8)}px {_scale(12)}px;
            }}
        """)
        self._cmd_preview.setPlaceholderText(
            "Ajoutez au moins un fichier source et définissez le chemin de sortie…"
        )
        content_layout.addWidget(self._cmd_preview)

        content_layout.addStretch()
        scroll.setWidget(content)
        root.addWidget(scroll, stretch=1)
        self._install_global_drop_targets(scroll, content)

    def _on_add_files(self, paths: list[str]) -> None:
        inspection.on_add_files(self, paths)

    def _install_global_drop_targets(self, scroll: QScrollArea, content: QWidget) -> None:
        for target in (self, scroll, scroll.viewport(), content):
            target.setAcceptDrops(True)
            target.installEventFilter(self)

    def _collect_folder_drop_paths(self, folder: Path) -> tuple[list[str], list[str]]:
        """
        Parcourt récursivement un dossier dropé.

        Seuls les types reconnus par file_types vont en sources.
        Seuls les .jpg sont retenus comme pièces jointes (cover).
        """
        source_paths: list[str] = []
        attachment_paths: list[str] = []

        for child in sorted(folder.rglob("*")):
            if not child.is_file():
                continue
            child_str = str(child)
            if is_accepted(child_str):
                source_paths.append(child_str)
            elif child.suffix.lower() == ".jpg":
                attachment_paths.append(child_str)

        return source_paths, attachment_paths

    def _route_dropped_paths(self, paths: list[str]) -> None:
        source_paths: list[str] = []
        attachment_paths: list[str] = []
        seen_sources: set[str] = set()
        seen_attachments: set[str] = set()

        for path_str in paths:
            path = Path(path_str)
            if path.is_dir():
                folder_sources, folder_attachments = self._collect_folder_drop_paths(path)
                for folder_source in folder_sources:
                    if folder_source not in seen_sources:
                        source_paths.append(folder_source)
                        seen_sources.add(folder_source)
                for folder_attachment in folder_attachments:
                    if folder_attachment not in seen_attachments:
                        attachment_paths.append(folder_attachment)
                        seen_attachments.add(folder_attachment)
                continue

            if not path.is_file():
                continue

            normalized_path = str(path)
            if is_accepted(normalized_path):
                if normalized_path not in seen_sources:
                    source_paths.append(normalized_path)
                    seen_sources.add(normalized_path)
            elif normalized_path not in seen_attachments:
                attachment_paths.append(normalized_path)
                seen_attachments.add(normalized_path)

        if source_paths:
            self._on_add_files(source_paths)
        if attachment_paths:
            self._attachment_panel.add_manual_paths(attachment_paths)
            self.log_message.emit(
                "INFO",
                translate_text(
                    "{count} fichier(s) ajouté(s) comme pièce(s) jointe(s).",
                    count=len(attachment_paths),
                ),
            )

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        event_type = event.type()
        if event_type in (QEvent.Type.DragEnter, QEvent.Type.DragMove, QEvent.Type.Drop):
            mime = getattr(event, "mimeData", lambda: None)()
            urls = mime.urls() if mime is not None and mime.hasUrls() else []
            local_paths = [
                url.toLocalFile()
                for url in urls
                if url.isLocalFile() and Path(url.toLocalFile()).exists()
            ]
            if not local_paths:
                return super().eventFilter(watched, event)

            if event_type == QEvent.Type.Drop:
                self._route_dropped_paths(local_paths)
            if isinstance(event, QDropEvent):
                event.acceptProposedAction()
            return True

        return super().eventFilter(watched, event)

    def _inspect_file(self, file_id: str, path: Path) -> None:
        inspection.inspect_file(self, file_id, path)

    def _extract_embedded_attachment_bytes(
        self,
        file_id: str,
        attachment: AttachmentInfo,
    ) -> bytes | None:
        source = self._find_source(file_id)
        if source is None:
            raise RuntimeError(
                translate_text("Source introuvable pour cet attachement embarqué.")
            )
        try:
            return extract_matroska_attachment_bytes(source.path, attachment.local_index)
        except Exception as exc:
            raise RuntimeError(
                translate_text("Impossible d'extraire cet attachement embarqué depuis la source.")
            ) from exc

    def _apply_inspection(self, file_id: str, info: FileInfo) -> None:
        inspection.apply_inspection(self, file_id, info)

    def _on_inspection_error(self, file_id: str, message: str) -> None:
        inspection.on_inspection_error(self, file_id, message)

    def _on_remove_file(self, file_id: str) -> None:
        inspection.on_remove_file(self, file_id)

    def _find_source(self, file_id: str) -> SourceFile | None:
        return inspection.find_source(self, file_id)

    def _has_ready_files(self) -> bool:
        return inspection.has_ready_files(self)

    def _default_tmdb_suggested_title(self) -> str:
        return tmdb.default_tmdb_suggested_title(self)

    def _default_tmdb_season_episode(self) -> tuple[int, int]:
        return tmdb.default_tmdb_season_episode(self)

    def _sync_tmdb_suggested_title(self, _text: str = "") -> None:
        tmdb.sync_tmdb_suggested_title(self, _text)

    def _on_tmdb_details_selected(self, details: object) -> None:
        tmdb.on_tmdb_details_selected(self, details)

    def _rebuild_preview(self) -> None:
        config = self._current_config()
        if config is None:
            self._cmd_preview.setPlainText("")
            return
        try:
            text = self._workflow.preview_command(config)
            self._cmd_preview.setPlainText(text)
        except Exception:
            self._cmd_preview.setPlainText("(erreur de construction de la commande)")

    def _on_table_changed(self, _item: QTableWidgetItem | None = None) -> None:
        self._track_table.refresh_filter()
        self._rebuild_preview()
        self._emit_audio_tracks()

    def _emit_signals(self) -> None:
        signals.emit_signals(self)

    def _emit_video_tracks(self) -> None:
        signals.emit_video_tracks(self)

    def _emit_audio_tracks(self) -> None:
        signals.emit_audio_tracks(self)

    def _current_config(self) -> RemuxConfig | None:
        return config_builder.current_config(self)

    def collect_config(self) -> RemuxConfig | None:
        return self._current_config()

    def refresh_runtime_settings(self) -> None:
        self._workflow.set_ffmpeg_bin(self._config.tool_ffmpeg)
        self._workflow.set_ffprobe_bin(self._config.tool_ffprobe)
        self._workflow.set_ffmpeg_threads(self._config.ffmpeg_threads)
        self._workflow.set_generate_nfo(self._config.generate_nfo)
        self._workflow.set_mediainfo_bin(self._config.tool_mediainfo)
        self._rebuild_preview()

    def update_audio_track_meta(self, stream_index: int, source_path, lang: str, title: str) -> None:
        file_id = next(
            (sf.id for sf in self._source_files if sf.info and sf.info.path == source_path),
            None,
        )
        if file_id is None:
            return
        self._track_table.update_audio_meta(file_id, stream_index, lang, title)
        self._rebuild_preview()

    def current_output_path(self) -> Path | None:
        text = self._output_edit.text().strip()
        return Path(text) if text else None

    def current_file_title(self) -> str:
        return self._file_title_edit.text().strip()

    def current_extra_attachments(self) -> list:
        return self._attachment_panel.get_extra_attachments()

    def current_tmdb_cover(self) -> tuple[str, str] | None:
        return self._attachment_panel.get_pending_tmdb_cover()

    def current_tag_overrides(self) -> dict[str, str] | None:
        return self._attachment_panel.get_global_tag_overrides()

    def current_chapter_overrides(self) -> list | None:
        keep_ch = self._chapter_panel.keep_chapters()
        if keep_ch and self._chapter_panel.is_modified():
            return self._chapter_panel.get_chapters()
        return None

    def _on_chapters_changed(self) -> None:
        chapter_functions.on_chapters_changed(self)

    def _update_chapters_from_sources(self) -> None:
        chapter_functions.update_chapters_from_sources(self)

    def _reset_empty_state(self) -> None:
        chapter_functions.reset_empty_state(self)

    def _resolve_base_chapters(self) -> list[ChapterEntry]:
        return chapter_functions.resolve_base_chapters(self)

    def is_ready(self) -> bool:
        return self._has_ready_files()

    def get_duration_s(self) -> float | None:
        """Durée de la première source (pour le calcul de progression dans MainWindow)."""
        for sf in self._source_files:
            if sf.info and sf.info.duration_s:
                return sf.info.duration_s
        return None

    def run_operation(self, config: RemuxConfig) -> TaskSignals:
        return self._workflow.run(config)

    def validate_config(self, config: RemuxConfig) -> list[str]:
        return self._workflow.validate(config)

    def _set_all_tracks(self, enabled: bool) -> None:
        self._track_table.set_all_enabled(enabled)
        self._track_table.refresh_filter()
        self._rebuild_preview()

    def _browse_output(self) -> None:
        default = self._output_edit.text() or str(self._config.output_dir)
        path, _ = QFileDialog.getSaveFileName(
            self,
            translate_text("Fichier de sortie"),
            default,
            translate_text("Matroska (*.mkv);;Tous les fichiers (*)"),
        )
        if path:
            self._output_edit.setText(path)

    def _on_extract_track(self, entry: TrackEntry) -> None:
        source = self._find_source(entry.file_id)
        if source is None or source.info is None:
            self.log_message.emit("WARN", translate_text("Source introuvable pour cette piste."))
            return

        codec = (entry.codec or "").lower()
        try:
            plan = TrackExtractor.plan_subtitle(codec)
        except ValueError as exc:
            QMessageBox.warning(
                self,
                translate_text("Extraction impossible"),
                str(exc),
            )
            return

        default_name = TrackExtractor.default_output_name(
            source.path.stem, entry.language, entry.mkv_tid, plan.extension,
        )
        default_path = source.path.parent / default_name
        out_str, _ = QFileDialog.getSaveFileName(
            self,
            translate_text("Extraire le sous-titre"),
            str(default_path),
            plan.file_filter,
        )
        if not out_str:
            return

        out_path = Path(out_str)
        cmd = TrackExtractor.build_subtitle_command(
            self._config.tool_ffmpeg,
            source.path,
            entry.mkv_tid,
            codec,
            out_path,
        )

        self.log_message.emit(
            "INFO",
            translate_text(
                "Extraction du sous-titre #{idx} ({codec}) vers {name}…",
                idx=entry.mkv_tid, codec=plan.format_label, name=out_path.name,
            ),
        )

        runner = ToolRunner()
        self._extract_runner = runner  # conserve la référence pendant l'exécution
        signals = runner.run(cmd, label=f"extract-sub-{entry.mkv_tid}")
        signals.progress.connect(
            lambda line: self.log_message.emit("INFO", line),
            Qt.ConnectionType.QueuedConnection,
        )
        signals.finished.connect(
            lambda _=None, p=out_path: self.log_message.emit(
                "OK", translate_text("Sous-titre extrait : {path}", path=str(p)),
            ),
            Qt.ConnectionType.QueuedConnection,
        )
        signals.failed.connect(
            lambda msg, _exc: self.log_message.emit(
                "ERROR", translate_text("Extraction échouée : {msg}", msg=msg),
            ),
            Qt.ConnectionType.QueuedConnection,
        )

    def _copy_command(self) -> None:
        from PySide6.QtWidgets import QApplication

        text = self._cmd_preview.toPlainText()
        if text:
            QApplication.clipboard().setText(text)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._executor.shutdown(wait=False)
        super().closeEvent(event)


__all__ = ["RemuxPanel"]
