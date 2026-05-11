"""Panneau principal RemuxPanel."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QDropEvent, QFont
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
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
from core.workflows.audio_sync import AudioSyncTrack, AudioSyncWorkflow
from core.workflows.common.ffmpeg_runtime import ffmpeg_progress_args
from core.workflows.remux_models import (
    RemuxConfig,
    SourceInput,
    TrackEntry,
    clone_track_entry,
)
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


class _AudioSyncReferenceDialog(QDialog):
    def __init__(
        self,
        choices: list[tuple[str, TrackEntry]],
        *,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(translate_text("Source de référence"))
        self.setModal(True)
        self._combo = QComboBox()
        for label, entry in choices:
            self._combo.addItem(label, entry)

        root = QVBoxLayout(self)
        root.setContentsMargins(_scale(18), _scale(16), _scale(18), _scale(16))
        root.setSpacing(_scale(12))
        self.setStyleSheet(f"""
            QDialog {{ background: {_C.BG_DEEP}; color: {_C.TEXT_PRI}; }}
            QLabel {{ color: {_C.TEXT_SEC}; background: transparent; }}
            QComboBox {{
                background: {_C.BG_CARD};
                color: {_C.TEXT_PRI};
                border: 1px solid {_C.BORDER};
                border-radius: 5px;
                padding: {_scale(6)}px {_scale(8)}px;
                min-width: {_scale(420)}px;
            }}
        """)

        label = QLabel(translate_text("Choisir la source audio qui servira de référence."))
        root.addWidget(label)
        root.addWidget(self._combo)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        root.addWidget(buttons)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

    def selected_entry(self) -> TrackEntry | None:
        data = self._combo.currentData()
        return data if isinstance(data, TrackEntry) else None


class RemuxPanel(QWidget):
    """
    Panneau de remuxage MKV/MP4 — support multi-sources.

    Signaux :
        log_message(level: str, message: str)
        tool_output(label: str, line: str)
        extract_started(TaskSignals, dict) — extraction de piste lancée depuis le menu contextuel
        audio_sync_started(dict) — synchronisation audio lancée depuis le tableau des pistes
        audio_sync_finished(bool, dict) — synchronisation audio terminée
        video_tracks_changed(list)  — pistes vidéo activées (FileInfo, TrackEntry, couleur)
        audio_tracks_changed(list)  — pistes audio activées (AudioTrack, couleur, Path source)
        ready_changed(bool)         — True quand au moins un fichier est inspecté
    """

    log_message = Signal(str, str)
    tool_output = Signal(str, str)
    extract_started = Signal(object, object)
    audio_sync_started = Signal(object)
    audio_sync_finished = Signal(bool, object)

    _inspection_done = Signal(str, object)
    _inspection_error = Signal(str, str)
    _audio_sync_done = Signal(str, str, int, float)
    _audio_sync_error = Signal(str, str)

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
        self._audio_sync_done.connect(
            self._on_audio_sync_done, Qt.ConnectionType.QueuedConnection
        )
        self._audio_sync_error.connect(
            self._on_audio_sync_error, Qt.ConnectionType.QueuedConnection
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
        self._track_table.order_changed.connect(self._on_track_order_changed)
        self._track_table.extract_requested.connect(self._on_extract_track)
        self._track_table.audio_sync_requested.connect(self._on_audio_sync_requested)
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

    def add_sources(self, paths: list[str | Path]) -> None:
        """Ajoute des fichiers source depuis l'extérieur du panneau."""
        normalized = [str(Path(path)) for path in paths if str(path).strip()]
        if not normalized:
            return
        self._on_add_files(normalized)

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
        self._refresh_audio_sync_buttons()
        self._track_table.refresh_filter()
        self._rebuild_preview()
        self._emit_signals()

    def _on_track_order_changed(self) -> None:
        self._refresh_audio_sync_buttons()
        self._rebuild_preview()
        self._emit_signals()

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

    def update_audio_track_meta(
        self,
        stream_index: int,
        source_path,
        lang: str,
        title: str,
        entry_id,
    ) -> None:
        file_id = next(
            (sf.id for sf in self._source_files if sf.info and sf.info.path == source_path),
            None,
        )
        if file_id is None:
            return
        self._track_table.update_audio_meta(
            file_id,
            stream_index,
            lang,
            title,
            entry_id=str(entry_id or "").strip() or None,
        )
        self._rebuild_preview()

    @staticmethod
    def _audio_encode_codec_label(codec: str) -> str:
        normalized = (codec or "copy").strip().lower()
        return {
            "aac": "AAC",
            "ac3": "AC3",
            "eac3": "EAC3",
            "flac": "FLAC",
        }.get(normalized, normalized.upper() if normalized else "COPY")

    @staticmethod
    def _audio_encode_display_info(source_display_info: str, codec: str, bitrate_kbps: int) -> str:
        normalized = (codec or "copy").strip().lower()
        if normalized == "copy":
            return source_display_info
        parts: list[str] = []
        for raw_part in str(source_display_info or "").replace("·", "  ").split("  "):
            part = raw_part.strip()
            if not part or "kbps" in part.lower():
                continue
            parts.append(part)
        if bitrate_kbps > 0:
            parts.append(f"{int(bitrate_kbps)} kbps")
        return "  ".join(parts)

    def _source_track_for_variant(self, entry: TrackEntry) -> TrackEntry:
        source = self._find_source(entry.file_id)
        if source is None:
            return entry
        source_entry_id = entry.source_entry_id or entry.entry_id
        return next((track for track in source.tracks if track.entry_id == source_entry_id), entry)

    def _apply_audio_encoding_to_entry(
        self,
        entry: TrackEntry,
        codec: str,
        bitrate_kbps: int,
    ) -> None:
        source_entry = self._source_track_for_variant(entry)
        normalized = (codec or "copy").strip().lower()
        if normalized == "copy":
            entry.codec = source_entry.orig_codec or source_entry.codec
            entry.display_info = source_entry.orig_display_info or source_entry.display_info
            return
        entry.codec = self._audio_encode_codec_label(normalized)
        entry.display_info = self._audio_encode_display_info(
            source_entry.orig_display_info or source_entry.display_info,
            normalized,
            bitrate_kbps,
        )

    def add_audio_track_variant(
        self,
        template_entry: TrackEntry,
        entry_id: str = "",
        codec: str = "copy",
        bitrate_kbps: int = 0,
    ) -> None:
        if template_entry.track_type != "audio":
            return
        if self._track_table.has_entry_id(entry_id or template_entry.entry_id):
            return

        source = self._find_source(template_entry.file_id)
        if source is None or source.info is None:
            self.log_message.emit("WARN", translate_text("Source introuvable pour cette piste."))
            return

        new_entry = clone_track_entry(template_entry, entry_id=entry_id or None)
        self._apply_audio_encoding_to_entry(new_entry, codec, bitrate_kbps)
        source.tracks.append(new_entry)
        source_color = self._source_colors.get(template_entry.file_id, _C.BORDER)
        self._track_table.append_tracks(source_color, [new_entry])
        self._refresh_audio_sync_buttons()
        self._track_table.refresh_filter()
        self._rebuild_preview()
        self._emit_audio_tracks()

    def remove_audio_track_variant(self, entry_id) -> None:
        entry_id_str = str(entry_id or "").strip()
        if not entry_id_str:
            return
        for source in self._source_files:
            source.tracks = [track for track in source.tracks if track.entry_id != entry_id_str]
        if self._track_table.remove_track_by_entry_id(entry_id_str):
            self._refresh_audio_sync_buttons()
            self._track_table.refresh_filter()
            self._rebuild_preview()
            self._emit_audio_tracks()

    def update_audio_track_encoding(self, entry_id, codec: str, bitrate_kbps: int) -> None:
        entry_id_str = str(entry_id or "").strip()
        if not entry_id_str:
            return
        for source in self._source_files:
            for entry in source.tracks:
                if entry.entry_id != entry_id_str or entry.track_type != "audio":
                    continue
                previous = (entry.codec, entry.display_info)
                self._apply_audio_encoding_to_entry(entry, codec, bitrate_kbps)
                if (entry.codec, entry.display_info) == previous:
                    return
                if self._track_table.update_audio_encoding(
                    entry.entry_id,
                    entry.codec,
                    entry.display_info,
                ):
                    self._rebuild_preview()
                    self._emit_audio_tracks()
                return

    def _audio_entries_by_source(self) -> dict[str, list[TrackEntry]]:
        by_source: dict[str, list[TrackEntry]] = {}
        for entry in self._track_table.current_tracks():
            if entry.track_type != "audio":
                continue
            by_source.setdefault(entry.file_id, []).append(entry)
        return by_source

    def _refresh_audio_sync_buttons(self) -> None:
        by_source = self._audio_entries_by_source()
        self._track_table.set_audio_sync_available(len(by_source) >= 2)

    @staticmethod
    def _audio_sync_family(entry: TrackEntry) -> str | None:
        info = (entry.display_info or "").lower()
        if (
            "5.1" in info or "7.1" in info or
            "6 ch" in info or "8 ch" in info or
            "6 channel" in info or "8 channel" in info
        ):
            return "surround"
        if (
            "stereo" in info or "2.0" in info or
            "2 ch" in info or "2ch" in info or "2 channel" in info
        ):
            return "stereo"
        return None

    def _audio_sync_reference_choices(self, target: TrackEntry) -> list[tuple[str, TrackEntry]]:
        choices: list[tuple[str, TrackEntry]] = []
        target_family = self._audio_sync_family(target)
        if target_family is None:
            return choices
        seen_sources: set[str] = set()
        for sf in self._source_files:
            if sf.id == target.file_id or sf.id in seen_sources:
                continue
            candidates = [
                entry for entry in self._track_table.current_tracks()
                if entry.file_id == sf.id
                and entry.track_type == "audio"
                and self._audio_sync_family(entry) == target_family
            ]
            if not candidates:
                continue
            ref = candidates[0]
            title = f" - {ref.title}" if ref.title else ""
            choices.append((
                f"{sf.path.name} | #{ref.mkv_tid} {ref.codec} {ref.display_info}{title}",
                ref,
            ))
            seen_sources.add(sf.id)
        return choices

    def _audio_sync_track(self, entry: TrackEntry) -> AudioSyncTrack | None:
        source = self._find_source(entry.file_id)
        if source is None:
            return None
        return AudioSyncTrack(source_path=source.path, stream_index=int(entry.mkv_tid))

    def _on_audio_sync_requested(self, entry: TrackEntry) -> None:
        if entry.track_type != "audio":
            return
        target_family = self._audio_sync_family(entry)
        if target_family is None:
            self.log_message.emit("ERROR", "impossible d'utiliser la synchronisation améliorée")
            return

        choices = self._audio_sync_reference_choices(entry)
        if not choices:
            self.log_message.emit("ERROR", "impossible d'utiliser la synchronisation améliorée")
            return

        dialog = _AudioSyncReferenceDialog(choices, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        reference_entry = dialog.selected_entry()
        if reference_entry is None:
            return
        if target_family == "stereo":
            QMessageBox.warning(
                self,
                translate_text("Synchronisation stéréo"),
                translate_text(
                    "La synchronisation stéréo n'est ni précise ni conseillée. "
                    "Elle cherche de gros marqueurs sonores sur le même côté, "
                    "mais le résultat doit être vérifié."
                ),
            )

        reference = self._audio_sync_track(reference_entry)
        target = self._audio_sync_track(entry)
        if reference is None or target is None:
            self.log_message.emit("ERROR", "impossible d'utiliser la synchronisation améliorée")
            return

        self.log_message.emit(
            "INFO",
            translate_text(
                "Synchronisation audio améliorée : piste #{target} vs référence #{reference}…",
                target=entry.mkv_tid,
                reference=reference_entry.mkv_tid,
            ),
        )
        self.audio_sync_started.emit({
            "label": translate_text(
                "Synchronisation audio améliorée : piste #{target} vs référence #{reference}…",
                target=entry.mkv_tid,
                reference=reference_entry.mkv_tid,
            )
        })

        target_entry_id = entry.entry_id
        reference_entry_id = reference_entry.entry_id

        def _task() -> None:
            try:
                workflow = AudioSyncWorkflow(
                    ffmpeg_bin=self._config.tool_ffmpeg,
                    ffprobe_bin=self._config.tool_ffprobe,
                    log_cb=self.log_message.emit,
                )
                result = workflow.detect_offset(reference, target)
                self._audio_sync_done.emit(
                    target_entry_id,
                    reference_entry_id,
                    result.offset_ms,
                    result.confidence,
                )
            except Exception as exc:
                self._audio_sync_error.emit(target_entry_id, str(exc))

        self._executor.submit(_task)

    def _on_audio_sync_done(
        self,
        entry_id: str,
        reference_entry_id: str,
        offset_ms: int,
        confidence: float,
    ) -> None:
        try:
            if reference_entry_id and reference_entry_id != entry_id:
                self._track_table.update_time_shift(reference_entry_id, 0)
            if self._track_table.update_time_shift(entry_id, offset_ms):
                offset_label = f"{int(offset_ms):+d}"
                confidence_label = f"{float(confidence):.2f}"
                self.log_message.emit(
                    "OK",
                    translate_text(
                        "Synchronisation audio améliorée appliquée : {offset} ms (confiance {confidence}).",
                        offset=offset_label,
                        confidence=confidence_label,
                    ),
                )
                self._rebuild_preview()
                self._emit_audio_tracks()
        finally:
            self.audio_sync_finished.emit(True, {"entry_id": entry_id})

    def _on_audio_sync_error(self, _entry_id: str, detail: str) -> None:
        try:
            self.log_message.emit("ERROR", "impossible d'utiliser la synchronisation améliorée")
            if detail:
                self.log_message.emit("ERROR", detail)
        finally:
            self.audio_sync_finished.emit(False, {"entry_id": _entry_id})

    def update_video_track_encoding(self, plans) -> None:
        plan_map: dict[str, str] = {}
        for plan in plans or []:
            entry_id = str(getattr(plan, "track_entry_id", "") or "").strip()
            if not entry_id:
                continue
            target_codec = str(getattr(plan, "target_codec", "") or "").strip().lower()
            if not target_codec:
                summary = str(getattr(plan, "codec_summary", "") or "").strip()
                if summary.lower() == "copy":
                    target_codec = "copy"
                else:
                    target_codec = summary.split(" - ", 1)[0].strip().lower()
            plan_map[entry_id] = target_codec

        table_changed = self._track_table.update_video_encoding_plans(plan_map, clear_missing=True)
        model_changed = False
        for source in self._source_files:
            for entry in source.tracks:
                if entry.track_type != "video":
                    continue
                target_codec = plan_map.get(entry.entry_id, "")
                modified = bool(target_codec and target_codec != "copy")
                if entry.encode_plan_codec == target_codec and entry.encode_plan_modified == modified:
                    continue
                entry.encode_plan_codec = target_codec
                entry.encode_plan_summary = ""
                entry.encode_plan_hdr_badges = ()
                entry.encode_plan_modified = modified
                model_changed = True

        if table_changed or model_changed:
            self._rebuild_preview()
            self._emit_video_tracks()

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
        self._refresh_audio_sync_buttons()
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
            progress_args=ffmpeg_progress_args(),
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
        label = f"extract-sub-{entry.mkv_tid}"
        signals = runner.run(cmd, label=label)
        self.extract_started.emit(
            signals,
            {
                "duration_s": source.info.duration_s,
                "label": translate_text("Extraction du sous-titre"),
                "output_name": out_path.name,
            },
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
        self._executor.shutdown(wait=True)
        super().closeEvent(event)


__all__ = ["RemuxPanel"]
