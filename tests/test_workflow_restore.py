"""Restauration GUI et CLI des mêmes propriétés du workflow."""

import time

from core.config import AppConfig
from core.workflows.remux_models import RemuxConfig, SourceInput, TrackEntry
from core.profiles.selectors import remux_config_to_exact_job


def test_gui_restores_workflow(qt_app, tmp_path):
    from core.inspector import FileInfo, ChapterEntry
    from ui.panels.remux_panel.panel import RemuxPanel
    from ui.panels.remux_panel.functions.workflow import restore
    info = FileInfo(path=tmp_path / "source.mkv", format="Matroska", duration_s=5, size_bytes=0, bit_rate=0)
    info.path.touch()
    audio = TrackEntry(0, "audio", "FLAC", "2.0", "fra", "Voix", file_id="src0", time_shift_ms=125,
                       flag_default=True, flag_hearing_impaired=True)
    disabled = TrackEntry(1, "subtitle", "SRT", "", "eng", "Disabled", file_id="src0", enabled=False)
    config = RemuxConfig([SourceInput(info.path, 0, [audio, disabled])], tmp_path / "out.mkv",
        [(0, 0, audio.entry_id)], chapter_overrides=[ChapterEntry(1.5, "Chapitre")],
        tag_overrides={"TITLE": "Titre"}, file_title="Titre du fichier", mux_backend="native",
        sync_mode="physical", clean_nfo=False, tmdb_cover=("https://image.tmdb.org/t/p/w500/cover.jpg", "cover.jpg"))
    panel = RemuxPanel(AppConfig())
    restore(panel, config, [info])
    result = panel.collect_config()
    assert result is not None
    assert result.file_title == config.file_title
    assert result.tag_overrides == config.tag_overrides
    assert result.tmdb_cover == config.tmdb_cover
    assert result.sync_mode == "physical" and result.clean_nfo is False
    assert result.sources[0].tracks[0].time_shift_ms == 125
    assert result.sources[0].tracks[0].flag_hearing_impaired
    assert not result.sources[0].tracks[1].enabled
    assert result.chapter_overrides and result.chapter_overrides[0].name == "Chapitre"
    panel.close()


def test_exact_job_preserves_backend_and_sync(tmp_path):
    track = TrackEntry(0, "audio", "FLAC", "", "fra", "", file_id="src0")
    config = RemuxConfig([SourceInput(tmp_path / "source.mkv", 0, [track])], tmp_path / "out.mkv",
        [(0, 0, track.entry_id)], mux_backend="native", sync_mode="physical", clean_nfo=False)
    job = remux_config_to_exact_job(config)
    assert job["mux_backend"] == "native"
    assert job["sync_mode"] == "physical"
    assert job["clean_nfo"] is False


def test_exact_job_preserves_explicit_empty_selection_and_tags(tmp_path):
    track = TrackEntry(0, "audio", "FLAC", "", "fra", "", file_id="src0", enabled=False)
    config = RemuxConfig([SourceInput(tmp_path / "source.mkv", 0, [track])],
                         tmp_path / "out.mkv", [], tag_overrides={})
    job = remux_config_to_exact_job(config)
    assert job["track_order"] == []
    assert job["tag_overrides"] == {}


def test_exact_job_selector_uses_original_empty_language(tmp_path):
    track = TrackEntry(0, "audio", "FLAC", "2.0", "fra", "VF", file_id="src0",
                       orig_language="", orig_title="")
    config = RemuxConfig([SourceInput(tmp_path / "source.mkv", 0, [track])],
                         tmp_path / "out.mkv", [(0, 0, track.entry_id)])
    selector = remux_config_to_exact_job(config)["tracks"][0]["selector"]
    assert "language" not in selector


def test_nfo_opt_out_and_error(tmp_path):
    import subprocess
    from core.workflows.remux_attachments import write_mediainfo_nfo
    source = tmp_path / "movie.mkv"
    text = f"Complete name : {source}\n"
    write_mediainfo_nfo(source, lambda *_: None, clean_nfo=False,
        run_cmd=lambda *a, **k: subprocess.CompletedProcess([], 0, text, ""))
    assert source.with_suffix(".nfo").read_text() == text
    write_mediainfo_nfo(source, lambda *_: None,
        run_cmd=lambda *a, **k: subprocess.CompletedProcess([], 1, "", "error"))
    assert source.with_suffix(".nfo").read_text() == text


def _test_panel(tmp_path):
    import shutil
    from ui.panels.remux_panel.panel import RemuxPanel
    app_config = AppConfig()
    app_config.config_dir = tmp_path
    app_config.output_dir = tmp_path
    app_config.tool_ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    app_config.tool_ffprobe = shutil.which("ffprobe") or "ffprobe"
    app_config.tool_mediainfo = shutil.which("mediainfo") or "mediainfo"
    return RemuxPanel(app_config)


def test_restored_sources_keep_distinct_colors_order_and_tag_edits(qt_app, tmp_path):
    from core.inspector import FileInfo
    from ui.panels.remux_panel.functions.workflow import restore
    from ui.panels.remux_panel.models import _pick_file_color

    first = FileInfo(path=tmp_path / "first.mkv", format="Matroska", duration_s=5,
                     size_bytes=1, bit_rate=0, global_tags={"TITLE": "Original"})
    second = FileInfo(path=tmp_path / "second.mkv", format="Matroska", duration_s=5,
                      size_bytes=1, bit_rate=0)
    video = TrackEntry(0, "video", "HEVC", "1920x1080", "", "Video", file_id="src0")
    audio = TrackEntry(1, "audio", "FLAC", "2.0", "fra", "Audio", file_id="src0")
    other = TrackEntry(0, "audio", "FLAC", "2.0", "eng", "Other", file_id="src1")
    config = RemuxConfig(
        [SourceInput(first.path, 0, [video, audio], copy_tags=True),
         SourceInput(second.path, 1, [other])],
        tmp_path / "out.mkv",
        [(0, 1, audio.entry_id), (1, 0, other.entry_id), (0, 0, video.entry_id)],
        tag_overrides={"TITLE": "Custom"},
    )
    panel = _test_panel(tmp_path)
    restore(panel, config, [first, second])

    assert len({source.id for source in panel._source_files}) == 2
    assert _pick_file_color(panel._color_index) not in {source.color for source in panel._source_files}
    assert [track.title for track in panel._track_table.current_tracks()] == ["Audio", "Other", "Video"]
    assert [item[1] for item in panel.collect_config().track_order] == [1, 0, 0]

    tag_item = next(item for item in panel._attachment_panel._items if item.is_tag)
    tag_item._cb.setChecked(False)
    assert panel.collect_config().tag_overrides == {"TITLE": "Custom"}
    panel.close()


def test_autosave_skips_locked_file_and_empty_panel_keeps_snapshot(qt_app, tmp_path):
    import json
    import subprocess
    import sys
    from core.inspector import FileInfo
    from ui.panels.remux_panel.functions.workflow import (
        autosave, autosave_path, mark_dirty, restore,
    )

    panels = []
    for number in (1, 2):
        info = FileInfo(path=tmp_path / f"source{number}.mkv", format="Matroska",
                        duration_s=5, size_bytes=1, bit_rate=0)
        track = TrackEntry(0, "audio", "FLAC", "2.0", "fra", f"Audio {number}", file_id="src0")
        panel = _test_panel(tmp_path)
        restore(panel, RemuxConfig([SourceInput(info.path, 0, [track])],
                                   tmp_path / f"out{number}.mkv", [(0, 0, track.entry_id)]), [info])
        panels.append(panel)

    first_path, second_path = (autosave_path(panel) for panel in panels)
    assert first_path == second_path
    mark_dirty(panels[0])
    autosave(panels[0])
    original_snapshot = first_path.read_bytes()
    lock_path = first_path.with_name(first_path.name + ".lock")
    assert not lock_path.exists()

    ready = tmp_path / "lock-ready"
    release = tmp_path / "lock-release"
    hold_lock = """
from pathlib import Path
from PySide6.QtCore import QLockFile
import sys
import time

lock = QLockFile(sys.argv[1])
assert lock.tryLock(0)
Path(sys.argv[2]).touch()
while not Path(sys.argv[3]).exists():
    time.sleep(0.01)
lock.unlock()
"""
    holder = subprocess.Popen([
        sys.executable, "-c", hold_lock,
        str(lock_path), str(ready), str(release),
    ])
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists() and holder.poll() is None
        mark_dirty(panels[1])
        autosave(panels[1])
        assert first_path.read_bytes() == original_snapshot
        assert panels[1]._autosave_dirty
    finally:
        release.touch()
        try:
            holder.wait(timeout=5)
        except subprocess.TimeoutExpired:
            holder.kill()
            holder.wait()
    assert holder.returncode == 0
    autosave(panels[1])
    assert not lock_path.exists()
    assert json.loads(first_path.read_text())["sources"][0]["path"].endswith("source2.mkv")
    second_snapshot = first_path.read_bytes()
    panels[1]._on_remove_file(panels[1]._source_files[0].id)
    autosave(panels[1])
    panels[1].close()
    panels[0].close()
    assert first_path.read_bytes() == second_snapshot


def test_failed_autosave_releases_lock_and_preserves_snapshot(qt_app, tmp_path, monkeypatch):
    from core.inspector import FileInfo
    from ui.panels.remux_panel.functions import workflow

    info = FileInfo(path=tmp_path / "source.mkv", format="Matroska",
                    duration_s=5, size_bytes=1, bit_rate=0)
    track = TrackEntry(0, "audio", "FLAC", "2.0", "fra", "Audio", file_id="src0")
    panel = _test_panel(tmp_path)
    workflow.restore(panel, RemuxConfig([SourceInput(info.path, 0, [track])],
                                        tmp_path / "out.mkv", [(0, 0, track.entry_id)]), [info])
    workflow.mark_dirty(panel)
    workflow.autosave(panel)
    path = workflow.autosave_path(panel)
    original = path.read_bytes()
    panel._file_title_edit.setText("Changed")
    monkeypatch.setattr(workflow, "save_workflow", lambda *_args: (_ for _ in ()).throw(OSError("disk error")))
    workflow.autosave(panel)
    assert path.read_bytes() == original
    assert panel._autosave_dirty
    assert not path.with_name(path.name + ".lock").exists()
    panel._autosave_dirty = False
    panel.close()


def test_close_flushes_changes_before_debounce(qt_app, tmp_path):
    from core.inspector import FileInfo
    from core.workflows.workflow_store import load_workflow
    from ui.panels.remux_panel.functions.workflow import autosave_path, mark_dirty, restore

    info = FileInfo(path=tmp_path / "source.mkv", format="Matroska",
                    duration_s=5, size_bytes=1, bit_rate=0)
    info.path.touch()
    track = TrackEntry(0, "audio", "FLAC", "2.0", "fra", "Audio", file_id="src0")
    panel = _test_panel(tmp_path)
    restore(panel, RemuxConfig([SourceInput(info.path, 0, [track])],
                               tmp_path / "out.mkv", [(0, 0, track.entry_id)]), [info])
    panel._file_title_edit.setText("Titre saisi juste avant la fermeture")
    panel._output_edit.clear()
    mark_dirty(panel)
    path = autosave_path(panel)
    assert not path.exists()
    panel.close()
    saved = load_workflow(path)
    assert saved["file_title"] == "Titre saisi juste avant la fermeture"
    assert saved["_muxiveo_session"]["output_text"] == ""
    assert len(saved["tracks"]) == 1


def test_pending_source_survives_autosave_and_resume(qt_app, tmp_path, monkeypatch):
    import subprocess
    from PySide6.QtWidgets import QMessageBox
    from core.inspector import FileInspector
    from core.workflows.workflow_store import load_workflow
    from ui.panels.remux_panel.functions.workflow import autosave, autosave_path, load, mark_dirty, restore
    from ui.panels.remux_panel.models import SourceFile

    paths = [tmp_path / "first.mkv", tmp_path / "second.mkv", tmp_path / "third.mkv"]
    for path in paths:
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                        "sine=duration=0.1", "-c:a", "flac", str(path)], check=True)
    panel = _test_panel(tmp_path)
    inspector = FileInspector(ffprobe_bin="ffprobe", mediainfo_bin="mediainfo")
    info = inspector.inspect(paths[0])
    third_info = inspector.inspect(paths[2])
    from core.workflows.remux_models import tracks_from_file_info
    track = tracks_from_file_info(info, file_id="src0")[0]
    third_track = tracks_from_file_info(third_info, file_id="src1")[0]
    track.title = "Titre modifié"
    third_track.title = "Troisième source"
    restore(panel, RemuxConfig([SourceInput(paths[0], 0, [track]),
                                SourceInput(paths[2], 1, [third_track])],
                               tmp_path / "out.mkv", [(0, track.mkv_tid, track.entry_id),
                                                      (1, third_track.mkv_tid, third_track.entry_id)],
                               file_title="Titre du fichier"), [info, third_info])
    pending = SourceFile("pending-source", paths[1], "#123456")
    panel._source_files.insert(1, pending)
    mark_dirty(panel)
    autosave(panel)
    saved = load_workflow(autosave_path(panel))
    assert len(saved["sources"]) == 3
    assert saved["_muxiveo_session"]["pending_source_indices"] == [1]
    assert [item["selector"]["source"] for item in saved["tracks"]] == [0, 2]

    resumed = _test_panel(tmp_path)
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: errors.append(args[2]))
    load(resumed, autosave_path(panel))
    deadline = time.monotonic() + 5
    while resumed._workflow_loading and time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.01)
    assert not errors, errors
    assert not resumed._workflow_loading
    assert len(resumed._source_files) == 3
    assert resumed._file_title_edit.text() == "Titre du fichier"
    assert resumed._source_files[0].tracks[0].title == "Titre modifié"
    assert resumed._source_files[2].tracks[0].title == "Troisième source"
    assert all(source.tracks[0].enabled for source in resumed._source_files)
    panel.close()
    resumed.close()


def test_only_pending_source_is_saved_and_restored(qt_app, tmp_path, monkeypatch):
    import subprocess
    from PySide6.QtWidgets import QMessageBox
    from core.workflows.workflow_store import load_workflow
    from ui.panels.remux_panel.functions.workflow import autosave_path, load, mark_dirty
    from ui.panels.remux_panel.models import SourceFile

    source = tmp_path / "pending.mkv"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "sine=duration=0.1", "-c:a", "flac", str(source)], check=True)
    panel = _test_panel(tmp_path)
    panel._source_files.append(SourceFile("pending", source, "#123456"))
    panel._file_title_edit.setText("Titre en attente")
    mark_dirty(panel)
    path = autosave_path(panel)
    panel.close()
    saved = load_workflow(path)
    assert len(saved["sources"]) == 1
    assert "track_order" not in saved

    resumed = _test_panel(tmp_path)
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: errors.append(args[2]))
    load(resumed, path)
    deadline = time.monotonic() + 5
    while resumed._workflow_loading and time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.01)
    assert not errors, errors
    assert len(resumed._source_files) == 1
    assert resumed._source_files[0].tracks[0].enabled
    assert resumed._file_title_edit.text() == "Titre en attente"
    resumed.close()
