"""Menu workflow, restauration transactionnelle et sauvegarde de session."""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QFileDialog, QMenu, QMessageBox, QPushButton

from core.i18n import translate_text
from core.workflows.workflow_store import load_workflow, save_workflow
from core.profiles.selectors import remux_config_to_exact_job


def setup(panel):
    button = QPushButton(translate_text("Workflow"))
    menu = QMenu(button)
    for text, shortcut, callback in (
        ("Sauvegarder le workflow…", QKeySequence.StandardKey.Save, panel._export_exact_json),
        ("Charger un workflow…", QKeySequence.StandardKey.Open, lambda: browse(panel)),
        ("Reprendre la dernière session", None, lambda: load(panel, autosave_path(panel))),
        ("Studio Hybridation", None, lambda: open_studio(panel)),
    ):
        action = menu.addAction(translate_text(text))
        action.triggered.connect(callback)
        if shortcut is not None:
            action.setShortcut(QKeySequence(shortcut))
            action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            panel.addAction(action)
    physical = menu.addAction(translate_text("Synchronisation physique"))
    physical.setCheckable(True)
    panel._physical_sync_action = physical
    physical.toggled.connect(lambda enabled: set_physical(panel, enabled))
    button.setMenu(menu)
    panel._workflow_options = {}
    panel._workflow_loading = False
    panel._workflow_loaded.connect(lambda config, infos: restore(panel, config, infos))
    panel._workflow_load_error.connect(lambda error: load_failed(panel, error))
    panel._autosave_timer = QTimer(panel)
    panel._autosave_timer.setInterval(30000)
    panel._autosave_timer.timeout.connect(lambda: autosave(panel))
    panel._autosave_timer.start()
    return button


def set_physical(panel, enabled):
    panel._workflow_options["sync_mode"] = "physical" if enabled else "container"
    if not enabled:
        panel._workflow_options["sync_calibrations"] = {}
    panel._rebuild_preview()


def open_studio(panel):
    from ui.panels.hybrid_studio import HybridStudio
    panel._hybrid_studio = HybridStudio(panel._config, parent=panel)
    panel._hybrid_studio.show()


def autosave_path(panel):
    return panel._config.config_dir / "session_autosave.json"


def autosave(panel):
    if panel._workflow_loading or panel._closing or any(s.info is None for s in panel._source_files):
        return
    config = panel.collect_config()
    if config is None:
        return
    try:
        path = autosave_path(panel)
        path.parent.mkdir(parents=True, exist_ok=True)
        save_workflow(path, remux_config_to_exact_job(config))
    except (OSError, ValueError) as exc:
        panel.log_message.emit("WARN", translate_text("Sauvegarde impossible : {err}", err=str(exc)))


def browse(panel):
    path, _ = QFileDialog.getOpenFileName(panel, translate_text("Charger un workflow…"), "", "JSON (*.json)")
    if path:
        load(panel, Path(path))


def load_failed(panel, error):
    panel._workflow_loading = False
    panel.setEnabled(True)
    QMessageBox.warning(panel, translate_text("Workflow"), translate_text("Chargement impossible : {err}", err=error))


def load(panel, path):
    if panel._workflow_loading:
        return
    try:
        job = load_workflow(path, relocate=lambda missing: QFileDialog.getOpenFileName(
            panel, translate_text("Relocaliser la source : {path}", path=missing), str(Path(path).parent))[0])
    except Exception as exc:
        load_failed(panel, str(exc))
        return
    panel._workflow_loading = True
    panel.setEnabled(False)
    def task():
        try:
            from cli.remux_config import build_remux_config
            from cli.options import CommonOptions
            from cli.logging import Logger
            from core.inspector import FileInspector
            config = build_remux_config(job, panel._config, CommonOptions(), Logger())
            inspector = FileInspector(ffprobe_bin=str(panel._config.tool_ffprobe), mediainfo_bin=str(panel._config.tool_mediainfo))
            infos = [inspector.inspect(s.path) for s in config.sources]
            panel._workflow_loaded.emit(config, infos)
        except Exception as exc:
            panel._workflow_load_error.emit(str(exc))
    panel._inspection_executor.submit(task)


def restore(panel, config, infos):
    from ui.panels.remux_panel.models import SourceFile, _pick_file_color
    from core.workflows.remux_mapping import resolve_mapped_tracks
    ordered = [m.track for m in resolve_mapped_tracks(config)]
    ordered += [t for s in config.sources for t in s.tracks if t not in ordered]
    for sf in list(panel._source_files):
        panel._on_remove_file(sf.id)
    panel._attachment_panel.clear_all()
    panel._track_table.clear_all()
    for index, (source, info) in enumerate(zip(config.sources, infos)):
        file_id = uuid4().hex
        color = _pick_file_color(index)
        for track in source.tracks:
            track.file_id = file_id
        sf = SourceFile(file_id, source.path, color, info, source.tracks)
        panel._source_files.append(sf)
        panel._source_names[file_id] = source.path.name
        panel._source_colors[file_id] = color
        panel._file_list.add_file(sf)
        panel._file_list.update_file(sf)
        panel._attachment_panel.add_source_attachments(file_id, color, info.attachments)
        panel._attachment_panel.add_source_tags(file_id, color, info.global_tags)
        selected = {a.local_index for a in source.selected_attachments}
        for item in panel._attachment_panel._items:
            if item.file_id == file_id:
                item._cb.setChecked(source.copy_tags if item.is_tag else item.att is not None and item.att.local_index in selected)
    for track in ordered:
        panel._track_table.append_tracks(panel._source_colors[track.file_id], [track])
    panel._output_edit.setText(str(config.output))
    panel._file_title_edit.setText(config.file_title)
    panel._mux_backend_combo.setCurrentIndex(panel._mux_backend_combo.findData(config.mux_backend))
    panel._attachment_panel.add_manual_paths(config.extra_attachments)
    panel._attachment_panel._panel_tag_overrides = config.tag_overrides
    if config.tmdb_cover:
        from ui.panels.remux_panel.widgets.attachments import _AttachmentItemWidget
        panel._attachment_panel._add_item(_AttachmentItemWidget(file_id="", is_tmdb_pending=True,
            tmdb_cover_url=config.tmdb_cover[0], tmdb_cover_filename=config.tmdb_cover[1]))
    panel._update_chapters_from_sources()
    panel._chapter_panel._keep_cb.setChecked(config.keep_chapters)
    if config.chapter_source_index is not None:
        combo = panel._chapter_panel._src_combo
        combo.setCurrentIndex(combo.findData(config.chapter_source_index))
    if config.chapter_overrides is not None:
        panel._chapter_panel.reset_chapters(config.chapter_overrides)
        panel._chapter_panel._modified = True
    panel._workflow_options = {key: getattr(config, key) for key in
        ("sync_mode", "sync_subtitles", "sync_calibrations", "crossfade_ms", "clean_nfo")}
    panel._physical_sync_action.blockSignals(True)
    panel._physical_sync_action.setChecked(config.sync_mode == "physical")
    panel._physical_sync_action.blockSignals(False)
    panel._workflow_loading = False
    panel.setEnabled(True)
    panel._refresh_audio_sync_buttons()
    panel.ready_changed.emit(True)
    panel._emit_signals()
    panel._rebuild_preview()
