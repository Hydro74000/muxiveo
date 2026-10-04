"""Menu workflow, restauration transactionnelle et sauvegarde de session."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QLockFile, Qt, QTimer
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QFileDialog, QMenu, QMessageBox

from core.i18n import translate_text
from core.workflows.workflow_store import load_workflow, save_workflow
from core.workflows.remux_models import RemuxConfig
from core.profiles.selectors import remux_config_to_exact_job
from ui.panels.remux_panel.functions import config_builder
from ui.panels.remux_panel.theme import _C, _font_px, _scale, _secondary_button


def setup(panel):
    button = _secondary_button(translate_text("Workflow"))
    button.setStyleSheet(f"""
        QPushButton {{
            background: {_C.BG_CARD};
            color: {_C.TEXT_SEC};
            border: 1px solid {_C.BORDER};
            border-radius: 5px;
            font-size: {_font_px(11)}px;
            font-weight: 500;
            padding: 0 {_scale(18)}px 0 {_scale(12)}px;
        }}
        QPushButton:hover {{
            background: {_C.BG_HOVER};
            color: {_C.TEXT_PRI};
            border-color: {_C.BORDER_LT};
        }}
        QPushButton:pressed, QPushButton:open {{
            background: {_C.BG_ACTIVE};
            color: {_C.TEXT_PRI};
            border-color: {_C.BORDER_LT};
        }}
        QPushButton::menu-indicator {{
            subcontrol-origin: padding;
            subcontrol-position: center right;
            right: {_scale(6)}px;
            width: {_scale(8)}px;
        }}
    """)
    menu = QMenu(button)
    menu.setStyleSheet(f"""
        QMenu {{
            background-color: {_C.BG_CARD};
            color: {_C.TEXT_PRI};
            border: 1px solid {_C.BORDER};
            border-radius: 6px;
            padding: {_scale(4)}px;
            font-size: {_font_px(11)}px;
        }}
        QMenu::item {{
            background-color: transparent;
            padding: {_scale(6)}px {_scale(18)}px {_scale(6)}px {_scale(12)}px;
            border-radius: 4px;
        }}
        QMenu::item:selected {{
            background-color: {_C.BG_HOVER};
            color: {_C.TEXT_PRI};
        }}
        QMenu::item:disabled {{
            color: {_C.TEXT_DIM};
        }}
        QMenu::separator {{
            height: 1px;
            background: {_C.BORDER};
            margin: {_scale(4)}px 0;
        }}
    """)
    for text, shortcut, callback in (
        ("Sauvegarder le workflow…", QKeySequence.StandardKey.Save, panel._export_exact_json),
        ("Charger un workflow…", QKeySequence.StandardKey.Open, lambda: browse(panel)),
        ("Reprendre la dernière session", None, lambda: load(panel, autosave_path(panel))),
    ):
        action = menu.addAction(translate_text(text))
        action.triggered.connect(callback)
        if shortcut is not None:
            action.setShortcut(QKeySequence(shortcut))
            action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            panel.addAction(action)
    button.setMenu(menu)
    if not hasattr(panel, "_workflow_options"):
        panel._workflow_options = {}
    panel._workflow_loading = False
    panel._workflow_loaded.connect(lambda config, infos, session: _restore_loaded(panel, config, infos, session))
    panel._workflow_load_error.connect(lambda error: load_failed(panel, error))
    panel._autosave_dirty = False
    panel._autosave_timer = QTimer(panel)
    panel._autosave_timer.setSingleShot(True)
    panel._autosave_timer.setInterval(2000)
    panel._autosave_timer.timeout.connect(lambda: autosave(panel))
    return button


def autosave_path(panel):
    return panel._config.config_dir / "session_autosave.json"


def mark_dirty(panel):
    if (not hasattr(panel, "_autosave_timer") or panel._workflow_loading
            or panel._closing or not panel._source_files):
        return
    panel._autosave_dirty = True
    panel._autosave_timer.start(2000)


def _track_key(track, source_index):
    key = {"source": source_index, "id": int(track.mkv_tid)}
    if track.is_new:
        key["entry_id"] = track.entry_id
    return key


def _remap_selector_sources(job, compact_to_full):
    for item in job.get("tracks", []):
        selector = item.get("selector")
        if isinstance(selector, dict) and "source" in selector:
            selector["source"] = compact_to_full[int(selector["source"])]
    for item in job.get("audio_variants", []):
        for name in ("selector", "source_selector"):
            selector = item.get(name)
            if isinstance(selector, dict) and "source" in selector:
                selector["source"] = compact_to_full[int(selector["source"])]
    for item in job.get("track_order", []):
        selector = item.get("selector")
        if isinstance(selector, dict) and "source" in selector:
            selector["source"] = compact_to_full[int(selector["source"])]
    chapters = job.get("chapters")
    if isinstance(chapters, dict) and chapters.get("source_index") is not None:
        chapters["source_index"] = compact_to_full[int(chapters["source_index"])]
    if "sync_calibrations" in job:
        job["sync_calibrations"] = {
            str(compact_to_full[int(index)]): value
            for index, value in job["sync_calibrations"].items()
        }


def _session_job(panel):
    source_index = {source.id: index for index, source in enumerate(panel._source_files)}
    ready_indices = [index for index, source in enumerate(panel._source_files) if source.info is not None]
    pending_indices = [index for index, source in enumerate(panel._source_files) if source.info is None]
    first = panel._source_files[0].path
    default_output = str(panel._config.output_dir / f"{first.stem}-MVO.mkv")
    config = config_builder.current_config(panel, output_fallback=default_output)
    if config is not None:
        full_to_compact = {full: compact for compact, full in enumerate(ready_indices)}
        compact = replace(
            config,
            sources=[replace(source, file_index=full_to_compact[source.file_index]) for source in config.sources],
            track_order=[(full_to_compact[int(item[0])], *item[1:]) for item in config.track_order],
            chapter_source_index=(full_to_compact.get(config.chapter_source_index)
                                  if config.chapter_source_index is not None else None),
            sync_calibrations={
                str(full_to_compact[int(index)]): value
                for index, value in config.sync_calibrations.items()
                if int(index) in full_to_compact
            },
        )
        job = remux_config_to_exact_job(compact)
        ready_sources = list(job["sources"])
        _remap_selector_sources(job, {compact: full for full, compact in full_to_compact.items()})
        job["sources"] = [
            ready_sources[full_to_compact[index]] if index in full_to_compact else
            {"path": str(source.path), "attachments": "none", "copy_tags": False}
            for index, source in enumerate(panel._source_files)
        ]
    else:
        output = panel._output_edit.text().strip() or default_output
        empty_config = RemuxConfig(
            sources=[], output=Path(output), track_order=[],
            keep_chapters=panel._chapter_panel.keep_chapters(),
            chapter_overrides=panel.current_chapter_overrides(),
            extra_attachments=panel._attachment_panel.get_extra_attachments(),
            file_title=panel._file_title_edit.text().strip(),
            tag_overrides=panel._attachment_panel.get_global_tag_overrides(),
            tmdb_cover=panel._attachment_panel.get_pending_tmdb_cover(),
            mux_backend=panel.current_mux_backend(),
            **panel._workflow_options,
        )
        job = remux_config_to_exact_job(empty_config)
        # Aucune piste n'a encore été inspectée : les pistes découvertes à la
        # reprise conservent leur sélection par défaut.
        job.pop("track_order", None)
        job["sources"] = [
            {"path": str(source.path), "attachments": "none", "copy_tags": False}
            for source in panel._source_files
        ]
    job["_muxiveo_session"] = {
        "version": 1,
        "pending_source_indices": pending_indices,
        "row_order": [
            _track_key(track, source_index[track.file_id])
            for track in panel._track_table.current_tracks()
            if track.file_id in source_index
        ],
        "source_sync_offsets_ms": {
            str(source_index[file_id]): offset
            for file_id, offset in panel._source_sync_offsets_ms.items()
            if file_id in source_index
        },
        "auto_sync_tracks": [
            _track_key(track, source_index[track.file_id])
            for track in panel._track_table.current_tracks()
            if track.entry_id in panel._auto_sync_entry_ids and track.file_id in source_index
        ],
        "chapter_sync_cancelled_source_indices": [
            source_index[file_id] for file_id in panel._chapter_sync_cancelled_source_ids
            if file_id in source_index
        ],
        "tag_edits": dict(panel._attachment_panel._tag_edits),
        "output_text": panel._output_edit.text(),
    }
    return job


def autosave(panel, *, retry_if_locked=True):
    if (not hasattr(panel, "_autosave_timer") or panel._workflow_loading
            or panel._closing or not panel._source_files):
        return
    if not panel._autosave_dirty:
        return
    try:
        path = autosave_path(panel)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Le verrou porte sur un chemin stable : save_workflow remplace le JSON
        # atomiquement, ce qui rendrait un verrou posé sur ce JSON inefficace.
        lock = QLockFile(str(path) + ".lock")
        if not lock.tryLock(0):
            if lock.error() == QLockFile.LockError.LockFailedError:
                if retry_if_locked:
                    panel._autosave_timer.start(5000)
                else:
                    panel.log_message.emit("WARN", translate_text("Sauvegarde de session ignorée : fichier verrouillé."))
                return
            raise OSError(f"Verrou de session indisponible : {lock.error().name}")
        try:
            save_workflow(path, _session_job(panel))
        finally:
            lock.unlock()
        panel._autosave_dirty = False
        panel._autosave_timer.stop()
    except Exception as exc:
        panel.log_message.emit("WARN", translate_text("Sauvegarde impossible : {err}", err=str(exc)))


def _restore_loaded(panel, config, infos, session):
    try:
        restore(panel, config, infos, session)
    except Exception as exc:
        load_failed(panel, str(exc))
    else:
        mark_dirty(panel)


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
            session = job.get("_muxiveo_session")
            if isinstance(session, dict):
                pending = {int(index) for index in session.get("pending_source_indices", [])}
                if pending and "track_order" in job:
                    config.track_order = list(config.track_order) + [
                        (source.file_index, track.mkv_tid, track.entry_id)
                        for source in config.sources if source.file_index in pending
                        for track in source.tracks if track.enabled
                    ]
            panel._workflow_loaded.emit(config, infos, session)
        except Exception as exc:
            panel._workflow_load_error.emit(str(exc))
    panel._inspection_executor.submit(task)


def restore(panel, config, infos, session=None):
    from ui.panels.remux_panel.models import SourceFile, _pick_file_color
    from core.workflows.remux_mapping import resolve_mapped_tracks
    if len(config.sources) != len(infos):
        raise ValueError("Nombre d'inspections différent du nombre de sources.")
    session = session if isinstance(session, dict) else {}
    ordered = [m.track for m in resolve_mapped_tracks(config)]
    ordered += [t for s in config.sources for t in s.tracks if t not in ordered]
    if isinstance(session.get("row_order"), list):
        by_key = {
            (index, track.mkv_tid, track.entry_id if track.is_new else None): track
            for index, source in enumerate(config.sources)
            for track in source.tracks
        }
        restored_order = []
        for key in session["row_order"]:
            if not isinstance(key, dict):
                continue
            try:
                track = by_key.get((int(key["source"]), int(key["id"]), key.get("entry_id")))
            except (KeyError, TypeError, ValueError):
                continue
            if track is not None and track not in restored_order:
                restored_order.append(track)
        ordered = restored_order + [track for track in ordered if track not in restored_order]
    panel._workflow_loading = True
    for sf in list(panel._source_files):
        panel._on_remove_file(sf.id)
    panel._attachment_panel.clear_all()
    panel._track_table.clear_all()
    panel._chapter_panel.clear_all()
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
    panel._color_index = len(panel._source_files)
    panel._workflow_options = {key: getattr(config, key) for key in
        ("sync_mode", "sync_subtitles", "sync_calibrations", "crossfade_ms", "clean_nfo")}
    panel._source_sync_offsets_ms = {
        panel._source_files[int(index)].id: int(offset)
        for index, offset in session.get("source_sync_offsets_ms", {}).items()
        if str(index).isdigit() and 0 <= int(index) < len(panel._source_files)
    }
    panel._chapter_sync_cancelled_source_ids = {
        panel._source_files[int(index)].id
        for index in session.get("chapter_sync_cancelled_source_indices", [])
        if str(index).isdigit() and 0 <= int(index) < len(panel._source_files)
    }
    auto_sync_keys = {
        (int(key["source"]), int(key["id"]), key.get("entry_id"))
        for key in session.get("auto_sync_tracks", [])
        if isinstance(key, dict) and str(key.get("source", "")).isdigit()
        and str(key.get("id", "")).isdigit()
    }
    panel._auto_sync_entry_ids = {
        track.entry_id
        for index, source in enumerate(config.sources)
        for track in source.tracks
        if (index, track.mkv_tid, track.entry_id if track.is_new else None) in auto_sync_keys
    }
    panel._replace_track_table_tracks(ordered)
    panel._output_edit.setText(str(config.output))
    if isinstance(session.get("output_text"), str):
        panel._output_edit.setText(session["output_text"])
    panel._file_title_edit.setText(config.file_title)
    panel._mux_backend_combo.setCurrentIndex(panel._mux_backend_combo.findData(config.mux_backend))
    panel._attachment_panel.add_manual_paths(config.extra_attachments)
    panel._attachment_panel.restore_tag_overrides(
        config.tag_overrides,
        session.get("tag_edits") if isinstance(session.get("tag_edits"), dict) else None,
    )
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
    panel._sync_entry_calibrations()
    panel._track_table.refresh_all_entries_info()
    panel._workflow_loading = False
    panel.setEnabled(True)
    panel._refresh_sync_action_buttons()
    panel._refresh_audio_sync_buttons()
    panel.ready_changed.emit(True)
    panel._emit_signals()
    panel._rebuild_preview()
