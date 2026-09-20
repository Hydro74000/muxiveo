"""Restauration GUI et CLI des mêmes propriétés du workflow."""

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
    assert result.file_title == config.file_title
    assert result.tag_overrides == config.tag_overrides
    assert result.tmdb_cover == config.tmdb_cover
    assert result.sync_mode == "physical" and result.clean_nfo is False
    assert result.sources[0].tracks[0].time_shift_ms == 125
    assert result.sources[0].tracks[0].flag_hearing_impaired
    assert not result.sources[0].tracks[1].enabled
    assert result.chapter_overrides[0].name == "Chapitre"
    panel.close()


def test_exact_job_preserves_backend_and_sync(tmp_path):
    track = TrackEntry(0, "audio", "FLAC", "", "fra", "", file_id="src0")
    config = RemuxConfig([SourceInput(tmp_path / "source.mkv", 0, [track])], tmp_path / "out.mkv",
        [(0, 0, track.entry_id)], mux_backend="native", sync_mode="physical", clean_nfo=False)
    job = remux_config_to_exact_job(config)
    assert job["mux_backend"] == "native"
    assert job["sync_mode"] == "physical"
    assert job["clean_nfo"] is False


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
