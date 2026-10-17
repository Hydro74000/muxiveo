"""Le témoin, le profil et les options doivent aboutir au même plan exportable."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from cli.logging import Logger
from cli.options import CommonOptions
from cli.remux_config import build_remux_config
from core.config import AppConfig
from core.inspector import FileInfo
from core.media_info_fetcher import MediaDetails
from core.profiles.selectors import remux_config_to_exact_job
from core.workflows.hybrid_matrix import HybridRecipe, MatrixEpisode, MatrixSource, prepare_matrix_episode
from core.workflows.remux_models import RemuxConfig, SourceInput, TrackEntry
from core.workflows.sync_calibration import SyncCalibration
from core.workflows.workflow_store import load_workflow, save_workflow


@pytest.fixture
def assembly(tmp_path, monkeypatch):
    def track(index, kind, lang, title, *, enabled=True, **flags):
        codec = {"video": "HEVC", "audio": "EAC3", "subtitle": "SUBRIP"}[kind]
        return TrackEntry(index, kind, codec, "5.1 640 kbps" if kind == "audio" else "", lang, title,
                          enabled=enabled, orig_language=lang, orig_title=title, **flags)

    master = [
        track(0, "video", "", ""), track(1, "audio", "en-US", "Atmos"),
        track(2, "audio", "en-US", "Commentary", enabled=False),
        track(3, "subtitle", "en-US", "Complets", enabled=False),
        track(4, "subtitle", "en-US", "SDH"),
        track(5, "subtitle", "es-ES", "SDH", enabled=False),
    ]
    donor = [
        track(0, "video", "", "", enabled=False), track(1, "audio", "fr-FR", "VF"),
        track(2, "audio", "en-US", "VO", enabled=False),
        track(3, "subtitle", "fr-FR", "Forcés", flag_forced=True, orig_flag_forced=True),
        track(4, "subtitle", "fr-FR", "Complets"),
        track(5, "subtitle", "en-US", "Complets", enabled=False),
    ]
    # Le deuxième donneur contient des pistes de même langue, non sélectionnées.
    second = [track(0, "audio", "fr-FR", "VF", enabled=False)]
    sources = []
    for index, tracks in enumerate((master, donor, second)):
        path = tmp_path / f"Show.S01E01.source{index}.mkv"
        path.touch()
        for t in tracks:
            t.file_id = f"src{index}"
        sources.append(SourceInput(path, index, tracks))
    witness = RemuxConfig(sources, tmp_path / "out.mkv", [], crossfade_ms=0)
    recipe = HybridRecipe.from_witness_config(witness)
    recipe.sync_strategy = "none"
    episode = MatrixEpisode(season=1, episode=1, master_file=sources[0].path, donor_files=[
        (MatrixSource(f"src_{i + 1}", s.path), s.path) for i, s in enumerate(sources[1:], 1)
    ])

    def inspect(*_args, **_kwargs):
        inspected = copy.deepcopy(sources)
        for source in inspected:
            # Les IDs et l'ordre des pistes diffèrent sur l'épisode suivant.
            source.tracks.reverse()
            for index, t in enumerate(source.tracks):
                t.enabled = True
                t.mkv_tid = index + 10
        infos = [FileInfo(s.path, "matroska", 1, 100, None) for s in inspected]
        return inspected, infos, [t for s in inspected for t in s.tracks]

    monkeypatch.setattr("cli.remux_config.inspect_sources", inspect)
    monkeypatch.setattr("cli.profile.inspect_sources", inspect)
    return episode, recipe, sources


@pytest.mark.parametrize("use_profile", [False, True])
def test_witness_selection_survives_profile_and_exact_job_roundtrip(assembly, tmp_path, use_profile):
    episode, recipe, _ = assembly
    options = SimpleNamespace(**vars(CommonOptions()))
    if use_profile:
        profile = {"version": 1, "kind": "decision-profile", "name": "Test", "rules": [{
            "id": "rename", "scope": "all", "match": {"all": []}, "actions": [
                {"type": "set_title", "value": "Profil"}, {"type": "set_enabled", "value": False},
            ],
        }, {
            "id": "video-language", "scope": "all",
            "match": {"all": [{"field": "type", "op": "is", "value": "video"}]},
            "actions": [{"type": "set_language", "value": "und"}],
        }, {
            "id": "subtitle-order", "scope": "all",
            "match": {"all": [{"field": "type", "op": "is", "value": "subtitle"},
                                {"field": "language", "op": "is", "value": "fr-FR"}]},
            "actions": [{"type": "set_order_priority", "value": 1}],
        }]}
        path = tmp_path / "profile.json"
        path.write_text(json.dumps(profile))
        options.profile = str(path)
    result, _ = prepare_matrix_episode(episode, recipe, tmp_path, AppConfig(), options, Logger())
    selected = [(i, t.track_type, t.language, t.orig_title) for i, s in enumerate(result.sources) for t in s.tracks if t.enabled]
    assert set(selected) == {
        (0, "video", "und" if use_profile else "", ""), (0, "audio", "en-US", "Atmos"), (0, "subtitle", "en-US", "SDH"),
        (1, "audio", "fr-FR", "VF"), (1, "subtitle", "fr-FR", "Forcés"), (1, "subtitle", "fr-FR", "Complets"),
    }
    if use_profile:
        assert all(t.title == "Profil" for s in result.sources for t in s.tracks if t.enabled)
        assert result.track_order[0][0] == 1  # Ordre du profil, distinct de l'ordre source.
    assert result.crossfade_ms == 0
    path = tmp_path / "export.exact-job.json"
    save_workflow(path, remux_config_to_exact_job(result))
    restored = build_remux_config(load_workflow(path), AppConfig(), CommonOptions(), Logger())
    assert remux_config_to_exact_job(restored) == remux_config_to_exact_job(result)


def test_no_implicit_fallback_for_unchecked_tracks(assembly, tmp_path):
    episode, recipe, sources = assembly
    recipe.track_choices = ()
    result, _ = prepare_matrix_episode(episode, recipe, tmp_path, AppConfig(), CommonOptions(), Logger())
    assert not any(t.enabled for s in result.sources for t in s.tracks)
    recipe.track_choices = None
    recipe.keep_master_audio_langs = recipe.donor_audio_langs = recipe.donor_sub_langs = []
    result, _ = prepare_matrix_episode(episode, recipe, tmp_path, AppConfig(), CommonOptions(), Logger())
    assert not any(t.enabled and t.is_audio for s in result.sources for t in s.tracks)
    assert not any(t.enabled for s in result.sources[1:] for t in s.tracks)


def test_missing_witness_track_is_reported_instead_of_substituted(assembly, tmp_path):
    episode, recipe, sources = assembly
    sources[0].tracks = [t for t in sources[0].tracks if t.title != "SDH" or t.language != "en-US"]
    with pytest.raises(ValueError, match="Pistes du modèle témoin introuvables.*en-US/sdh"):
        prepare_matrix_episode(episode, recipe, tmp_path, AppConfig(), CommonOptions(), Logger())


@pytest.mark.parametrize("cover", [False, True])
def test_metadata_template_and_advanced_options_use_final_tracks(assembly, tmp_path, monkeypatch, cover):
    episode, recipe, _ = assembly
    calls = []

    def metadata(job, _config, _path, _logger, **kwargs):
        if not job.get("tmdb"):
            return "", None, None, None
        calls.append(job["tmdb"])
        details = MediaDetails(title="Crystal Lake", season="1", episode="1", episode_title="Camp Crystal")
        return details.formatted_container_title(), details.to_mkv_tags(), ("cover-url", "cover.jpg") if cover else None, details

    classified = []

    def classify(sources, infos, **kwargs):
        classified.append(([(s.file_index, t.orig_title) for s in sources for t in s.tracks if t.enabled], kwargs))

    monkeypatch.setattr("cli.remux_config.resolve_tmdb_metadata", metadata)
    monkeypatch.setattr("core.workflows.subtitle_heuristics.classify_subtitles", classify)
    options = SimpleNamespace(**vars(CommonOptions(ffmpeg="ffmpeg", clean_nfo=False, crossfade_ms=137, auto_forced_subs=True, auto_sdh=True, forced_threshold=17)),
                              auto_tmdb=213562, no_cover=not cover, tag="MVO",
                              output_template="{title:release}.S{season_num:02d}E{episode_num:02d}.{audio-multi}.{audio-fr-tag}.{audio-codec-release:best}.{release_group}")
    result, _ = prepare_matrix_episode(episode, recipe, tmp_path, AppConfig(), options, Logger())
    assert result.output.name == "Crystal.Lake.S01E01.MULTi.VFF.DDP.MVO.mkv"
    assert result.crossfade_ms == 137 and not result.clean_nfo
    assert calls == [{"enabled": True, "kind": "tv", "season": "1", "episode": "1", "cover": cover, "id": 213562}]
    assert classified[0][1] == {"ffmpeg": "ffmpeg", "forced": True, "sdh": True, "threshold": 17}
    assert (0, "SDH") in classified[0][0] and (0, "Complets") not in classified[0][0]
    job = remux_config_to_exact_job(result)
    assert job["file_title"] == result.file_title and job["tag_overrides"]["SEASON"] == "1"
    assert bool(job.get("tmdb_cover")) is cover


@pytest.mark.parametrize("mode", ["physical", "container"])
def test_supplied_calibration_is_not_replaced_by_an_acoustic_scan(assembly, tmp_path, monkeypatch, mode):
    episode, recipe, _ = assembly
    episode.donor_files = episode.donor_files[:1]
    recipe.track_choices = tuple(c for c in recipe.track_choices if c.source_id != "src_3")
    recipe.sync_strategy = "auto"
    recipe.sync_mode = mode
    calibration = tmp_path / "calibration.json"
    calibration.write_text(json.dumps(SyncCalibration.linear(75).to_dict()))
    monkeypatch.setattr("core.workflows.audio_sync_scan.AudioSyncScanner.scan", lambda *_a, **_k: pytest.fail("Analyse inattendue"))
    result, calib = prepare_matrix_episode(episode, recipe, tmp_path, AppConfig(),
                                          CommonOptions(calibration=str(calibration)), Logger())
    assert calib.segments[0].shift_ms == 75
    if mode == "physical":
        assert result.sync_calibrations["1"]["segments"][0]["shift_ms"] == 75
    else:
        assert result.sync_calibrations == {}
        assert all(t.time_shift_ms == 75 for t in result.sources[1].tracks if t.enabled)


@pytest.mark.parametrize("mirror", ["mirror", "none"])
def test_container_calibration_exports_the_same_plan_for_each_donor(assembly, tmp_path, monkeypatch, mirror):
    episode, recipe, _ = assembly
    recipe.track_choices = None
    recipe.sync_strategy = "auto"
    recipe.sync_mode = "container"
    recipe.sync_subtitles = mirror
    shifts = iter((75, -125))
    monkeypatch.setattr("core.workflows.audio_sync_scan.AudioSyncScanner.scan",
                        lambda *_a, **_k: SyncCalibration.linear(next(shifts)))
    result, _ = prepare_matrix_episode(episode, recipe, tmp_path, AppConfig(), CommonOptions(), Logger())
    assert result.sync_calibrations == {}
    for source, shift in zip(result.sources[1:], (75, -125)):
        for track in source.tracks:
            if track.enabled:
                assert track.time_shift_ms == (shift if track.is_audio or mirror == "mirror" else 0)
    payload = remux_config_to_exact_job(result)
    restored = build_remux_config(payload, AppConfig(), CommonOptions(), Logger())
    assert remux_config_to_exact_job(restored) == payload


@pytest.mark.parametrize("cadence_first", [False, True])
def test_cadence_promotion_to_physical_applies_to_all_donors(assembly, tmp_path, monkeypatch, cadence_first):
    from core.workflows.cadence import detect_cadence_from_metadata
    from core.workflows.sync_calibration import SyncSegment

    episode, recipe, _ = assembly
    recipe.track_choices = None
    recipe.sync_strategy = "auto"
    recipe.sync_mode = "container"
    cadence = detect_cadence_from_metadata(24, 25)
    calibrations = [
        SyncCalibration((SyncSegment(0, 75),), cadence_mismatch=cadence),
        SyncCalibration((SyncSegment(0, -125), SyncSegment(10000, -250))),
    ]
    if not cadence_first:
        calibrations.reverse()
    scanned = iter(calibrations)
    monkeypatch.setattr("core.workflows.audio_sync_scan.AudioSyncScanner.scan",
                        lambda *_a, **_k: next(scanned))
    result, _ = prepare_matrix_episode(episode, recipe, tmp_path, AppConfig(), CommonOptions(), Logger())
    assert result.sync_mode == "physical"
    assert set(result.sync_calibrations) == {"1", "2"}
    assert len(result.sync_calibrations["2" if cadence_first else "1"]["segments"]) == 2


def test_studio_applies_individual_checkboxes_and_allows_empty_languages(assembly, qt_app):
    from ui.panels.hybrid_studio import HybridStudio
    from core.inspector import AudioTrack, SubtitleTrack, VideoTrack

    _, _, sources = assembly
    panel = HybridStudio(AppConfig())
    try:
        inspected = []
        for source_index, source in enumerate(sources):
            info = FileInfo(source.path, "matroska", 1, 100, None, track_enabled={})
            for track in source.tracks:
                if track.is_video:
                    stream = VideoTrack(track.mkv_tid, track.codec, "", 64, 64, None, None, None, None, None, None,
                                        language=track.language, title=track.title)
                elif track.is_audio:
                    stream = AudioTrack(track.mkv_tid, track.codec, "", 6, "5.1", 48000, None, track.language, track.title)
                else:
                    stream = SubtitleTrack(track.mkv_tid, track.codec, track.language, track.title, forced=track.orig_flag_forced)
                getattr(info, f"{track.track_type}_tracks").append(stream)
            inspected.append((f"Source {source_index}", "master" if source_index == 0 else "donor", info))
        panel._populate_witness_table(inspected)
        assert not next(item for item in panel.witness_track_items
                        if item["role"] == "donor" and item["lang"] == "en-US")["checkbox"].isChecked()
        for item in panel.witness_track_items:
            item["checkbox"].setChecked(item["role"] == "master" and
                                        (item["type"] == "video" or item["choice"].title == "sdh" and item["lang"] == "en-US"))
        before = panel._settings_signature()
        panel.apply_witness_recipe()
        assert panel._settings_signature() != before
        assert panel.recipe.keep_master_audio_langs == panel.recipe.donor_audio_langs == panel.recipe.donor_sub_langs == []
        assert len(panel.recipe.track_choices) == 2
        assert panel.recipe_status_lbl.text().endswith("[en-US]")
        assert not any(c.language == "es-ES" for c in panel.recipe.track_choices)
    finally:
        panel.close()


@pytest.mark.parametrize("change", ["template", "witness"])
def test_export_reprepares_changed_settings_before_writing(assembly, tmp_path, monkeypatch, qt_app, change):
    import time
    from core.workflows.hybrid_matrix import HybridMatrix
    from ui.panels.hybrid_studio import HybridStudio

    episode, recipe, sources = assembly
    panel = HybridStudio(AppConfig())
    directory = tmp_path / "WF"
    directory.mkdir()
    try:
        panel.reference.setText(str(sources[0].path))
        panel.donor.setText(str(sources[1].path))
        panel.output.setText(str(tmp_path / "output"))
        panel.sync_strategy.setCurrentIndex(panel.sync_strategy.findData("none"))
        panel.recipe = recipe
        panel.matrix_episodes = [episode]
        panel._matrix_signature = panel._source_signature()
        panel._prepared_signature = panel._settings_signature()
        panel.jobs[0] = (RemuxConfig(sources, tmp_path / "stale.mkv", []), None)
        if change == "template":
            panel.controls["output-template"].setText("{source_name}.{release_group}")
            panel.controls["tag"].setText("NEW")
        else:
            panel.recipe.track_choices = tuple(c for c in recipe.track_choices if c.track_type == "video")
        panel.matrix = HybridMatrix()
        monkeypatch.setattr("ui.panels.hybrid_studio.QFileDialog.getExistingDirectory", lambda *_a: str(directory))
        panel.export_jobs()
        deadline = time.monotonic() + 5
        while panel._operation_active and time.monotonic() < deadline:
            qt_app.processEvents()
            time.sleep(0.01)
        assert not panel._operation_active
        files = list(directory.glob("*.json"))
        assert len(files) == 1, panel.status.text()
        assert files[0].name != "stale.exact-job.json"
        job = load_workflow(files[0])
        if change == "template":
            assert job["output"].endswith(".NEW.mkv")
        else:
            assert len(job["track_order"]) == 1 and job["track_order"][0]["selector"]["type"] == "video"
        assert panel._prepared_signature == panel._settings_signature()
    finally:
        panel.close()
