"""Lot Hybrid réel piloté par la commande globale, de la préparation au MKV."""
import shutil
import json
import subprocess
import time

import pytest
from PySide6.QtCore import QCoreApplication

from core.matroska.reader import MatroskaReader
from tests.test_unified_operations import window as window


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg requis")
def test_global_action_executes_a_complete_hybrid_batch(window, tmp_path):
    reference, donor, output = (tmp_path / name for name in ("reference", "donor", "output"))
    reference.mkdir()
    donor.mkdir()
    for episode in (1, 2):
        for directory, language in ((reference, "eng"), (donor, "fra")):
            subprocess.run([
                "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=s=64x64:r=24:d=0.5",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=0.5",
                "-c:v", "ffv1", "-c:a", "eac3", "-metadata:s:a", f"language={language}",
                str(directory / f"Show.S01E{episode:02}.mkv"),
            ], check=True, capture_output=True, timeout=15)
    panel = window._hybrid_panel
    panel.reference.setText(str(reference))
    panel.donor.setText(str(donor))
    panel.output.setText(str(output))
    panel.sync_strategy.setCurrentIndex(panel.sync_strategy.findData("manual"))
    panel.manual_shift.setValue(125)
    window._stack.setCurrentIndex(4)
    window._refresh_operation_bar()
    states = []
    panel.op_state_changed.connect(lambda state, label: states.append((state, label)))
    window._run_btn.click()
    assert window._running
    deadline = time.monotonic() + 30
    while window._running and time.monotonic() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.01)
    assert not window._running, states
    assert states[-1][0] == "finished", states
    assert sum(state == "started" for state, _ in states) == 1  # pas de déverrouillage entre préparation et lot
    assert len(panel.jobs) == 2
    assert panel.sync_episode.count() == 2
    assert panel.sync_episode.currentData() == 0
    assert panel.sync_target.count() > 0 and panel.sync_reference.count() > 0
    panel.sync_episode.setCurrentIndex(1)
    assert panel._sync_editor.target_source_path.name == "Show.S01E02.mkv"
    for config, _ in panel.jobs.values():
        assert config.output.exists()
        reader = MatroskaReader(config.output)
        french = next(track for track in reader.tracks() if track.language in {"fre", "fra"})
        assert next(reader.blocks(track_numbers={french.number})).timestamp_ms == 0
    window._log_panel._flush_pending()
    text = window._log_panel._text.toPlainText()
    assert "HYBRID" in text
    assert "S01E01" in text and "S01E02" in text
    assert "Synchronisation physique" in text
    assert "Hybridation terminée" in text


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg requis")
def test_witness_profile_template_and_export_match_the_real_mkv(window, tmp_path, monkeypatch):
    from cli.logging import Logger
    from cli.options import CommonOptions
    from cli.remux_config import build_remux_config
    from core.inspector import FileInspector
    from core.media_info_fetcher import MediaDetails
    from core.profiles.selectors import remux_config_to_exact_job
    from core.workflows.workflow_store import load_workflow

    reference, donor, output = (tmp_path / name for name in ("reference", "donor", "output"))
    reference.mkdir()
    donor.mkdir()
    subtitles = []
    for index, text in enumerate(("Hello", "[MUSIC]", "Hola", "Bonjour", "Salut")):
        path = tmp_path / f"subtitle{index}.srt"
        path.write_text(f"1\n00:00:00,100 --> 00:00:00,400\n{text}\n")
        subtitles.append(path)
    for directory, language, subs in ((reference, "eng", [(0, "eng", "Complets"), (1, "eng", "SDH"), (2, "spa", "SDH")]),
                                      (donor, "fra", [(3, "fra", "Forcés"), (4, "fra", "Complets"), (0, "eng", "Complets")])):
        command = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=s=64x64:r=24:d=0.5",
                   "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=0.5"]
        for index, _, _ in subs:
            command += ["-i", str(subtitles[index])]
        command += ["-map", "0:v", "-map", "1:a", "-map", "2:s", "-map", "3:s", "-map", "4:s",
                    "-c:v", "ffv1", "-c:a", "eac3", "-c:s", "srt", "-metadata:s:a", f"language={language}"]
        for index, (_, lang, title) in enumerate(subs):
            command += [f"-metadata:s:s:{index}", f"language={lang}", f"-metadata:s:s:{index}", f"title={title}"]
        if directory == donor:
            command += ["-disposition:s:0", "forced"]
        command += [str(directory / "Show.S01E01.mkv")]
        subprocess.run(command, check=True, capture_output=True, timeout=15)

    panel = window._hybrid_panel
    inspector = FileInspector()
    panel._populate_witness_table([
        ("Master", "master", inspector.inspect(reference / "Show.S01E01.mkv"), "master"),
        ("Donneur", "donor", inspector.inspect(donor / "Show.S01E01.mkv"), "src_2"),
    ])
    for item in panel.witness_track_items:
        selected = (item["role"] == "master" and item["type"] in {"video", "audio"} or
                    item["role"] == "master" and item["type"] == "subtitle" and item["choice"].title == "sdh" and item["lang"].startswith("en") or
                    item["role"] == "donor" and item["lang"].startswith("fr"))
        item["checkbox"].setChecked(selected)
    panel.apply_witness_recipe()
    panel.reference.setText(str(reference))
    panel.donor.setText(str(donor))
    panel.output.setText(str(output))
    panel.sync_strategy.setCurrentIndex(panel.sync_strategy.findData("manual"))
    panel.manual_shift.setValue(125)
    panel.controls["crossfade-ms"].setValue(0)
    panel.controls["no-clean-nfo"].setChecked(True)
    panel.controls["auto-forced-subs"].setChecked(True)
    panel.controls["auto-sdh"].setChecked(True)
    panel.controls["auto-tmdb"].setText("213562")
    panel.controls["tag"].setText("MVO")
    panel.controls["output-template"].setText("{title:release}.S{season_num:02d}E{episode_num:02d}.{audio-multi}.{audio-fr-tag}.{audio-codec-release:best}.Hybrid.{release_group}")
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"version": 1, "kind": "decision-profile", "name": "Titles", "rules": [{
        "id": "rename", "scope": "all", "match": {"all": []},
        "actions": [{"type": "set_title", "pattern": "Plan {lang_name}"}],
    }]}))
    panel.profile.setText(str(profile))
    tmdb_calls = []

    def metadata(job, *_args, **_kwargs):
        if not job.get("tmdb") or not job["tmdb"].get("enabled", False):
            return "", None, None, None
        tmdb_calls.append(job["tmdb"])
        details = MediaDetails(title="Crystal Lake", season="1", episode="1", episode_title="Camp Crystal")
        return details.formatted_container_title(), details.to_mkv_tags(), None, details

    monkeypatch.setattr("cli.remux_config.resolve_tmdb_metadata", metadata)
    window._stack.setCurrentIndex(4)
    window._refresh_operation_bar()
    window._run_btn.click()
    deadline = time.monotonic() + 30
    while window._running and time.monotonic() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.01)
    assert not window._running, panel.status.text()
    assert len(panel.jobs) == 1, panel.status.text()
    config, _ = panel.jobs[0]
    assert config.output.name == "Crystal.Lake.S01E01.MULTi.VFF.DDP.Hybrid.MVO.mkv"
    assert config.output.is_file(), panel.status.text()
    assert config.crossfade_ms == 0 and not config.clean_nfo
    assert tmdb_calls == [{"enabled": True, "kind": "tv", "cover": True, "season": "1", "episode": "1", "id": 213562}]
    tracks = MatroskaReader(config.output).tracks()
    assert len(tracks) == 6
    assert sum(track.track_type == 2 for track in tracks) == 2
    assert not any(track.language in {"spa", "es"} for track in tracks)
    assert any(track.flag_forced for track in tracks)
    assert any(track.flag_hearing_impaired for track in tracks)
    workflows = tmp_path / "WF"
    workflows.mkdir()
    panel._save_jobs(workflows)
    saved = next(workflows.glob("*.json"))
    assert saved.stem == config.output.stem + ".exact-job"
    restored = build_remux_config(load_workflow(saved), panel.config, CommonOptions(), Logger())
    assert remux_config_to_exact_job(restored) == remux_config_to_exact_job(config)


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg requis")
def test_shared_sync_studio_renders_a_preview_after_a_cut(qt_app, tmp_path, monkeypatch):
    from core.workflows.remux_models import TrackEntry
    from core.workflows.sync_calibration import SyncCalibration, SyncSegment
    from ui.panels.remux_panel.widgets.sync_studio_dialog import SyncStudioDialog

    source = tmp_path / "donor.mka"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "sine=frequency=440:sample_rate=48000:duration=40", "-c:a", "eac3", str(source)],
                   check=True, capture_output=True, timeout=15)
    previews = []
    monkeypatch.setattr(SyncStudioDialog, "_load_segment_audio_async", lambda *_a: None)
    monkeypatch.setattr(SyncStudioDialog, "_on_preview_ready", lambda _self, token, path: previews.append(path))
    editor = SyncStudioDialog(
        TrackEntry(0, "audio", "EAC3", "", "fra", ""), source, 0, None, None, None,
        SyncCalibration((SyncSegment(0, 0), SyncSegment(8000, 1000))), embedded=True, crossfade_ms=0,
    )
    try:
        editor.goto_edit.setText("00:00:20")
        editor._goto_timestamp()
        assert editor._current_segment_index == 1
        assert editor._current_start_s == 10
        editor._toggle_listen()
        deadline = time.monotonic() + 15
        while not previews and time.monotonic() < deadline:
            qt_app.processEvents()
            time.sleep(0.01)
        assert previews, editor.listen_status.text()
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                "-of", "json", previews[0]], check=True, capture_output=True, text=True, timeout=10)
        assert float(json.loads(probe.stdout)["format"]["duration"]) == pytest.approx(15, abs=0.01)
    finally:
        editor.close()
        deadline = time.monotonic() + 5
        while not editor._shutdown.done.is_set() and time.monotonic() < deadline:
            qt_app.processEvents()
            time.sleep(0.01)
        qt_app.processEvents()
