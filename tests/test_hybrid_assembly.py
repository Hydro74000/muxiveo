"""Le témoin, le profil et les options doivent aboutir au même plan exportable."""
from __future__ import annotations

import copy
from dataclasses import replace
import json
from pathlib import Path
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

    monkeypatch.setattr("core.jobs.remux_config.inspect_sources", inspect)
    monkeypatch.setattr("core.jobs.profile.inspect_sources", inspect)
    return episode, recipe, sources


def test_preparation_uses_isolated_profile_snapshot_after_file_rewrite(assembly, tmp_path):
    from cli.profile import snapshot_decision_profile

    episode, recipe, _ = assembly
    path = tmp_path / "profile.json"
    profile = {"kind": "decision-profile", "version": 1, "name": "Audit", "output_template": "avant.mkv",
               "rules": [{"id": "rename", "scope": "all", "match": {"all": []},
                          "actions": [{"type": "set_title", "value": "Figé"}]}]}
    path.write_text(json.dumps(profile))
    version, snapshot = snapshot_decision_profile(path)
    options = SimpleNamespace(**vars(CommonOptions()), profile=str(path), profile_snapshot=snapshot, profile_version=version)
    path.write_text("{")
    result, _ = prepare_matrix_episode(episode, recipe, tmp_path, AppConfig(), options, Logger())
    assert result.output.name == "avant.mkv"
    assert all(track.title == "Figé" for source in result.sources for track in source.tracks if track.enabled)
    assert episode.naming_context["__profile_version__"] == version
    assert episode.naming_context["__profile_template__"] == "avant.mkv"
    assert snapshot == profile


def test_season_analysis_prepares_tracks_and_metadata_without_sync(assembly, tmp_path, monkeypatch):
    episode, recipe, _ = assembly
    recipe.sync_strategy = "auto"

    def forbidden(*_args, **_kwargs):
        pytest.fail("L'analyse de saison ne doit pas démarrer de scanner de synchro")

    monkeypatch.setattr("core.workflows.audio_sync_scan.AudioSyncScanner", forbidden)
    monkeypatch.setattr("core.workflows.subtitle_sync_scan.SubtitleSyncScanner", forbidden)
    result, calibration = prepare_matrix_episode(
        episode, recipe, tmp_path, AppConfig(),
        SimpleNamespace(**vars(CommonOptions(calibration="deferred.json")), output_template="{season_episode}"), Logger(),
        calibrate=False,
    )
    assert calibration is None and result.sync_calibrations == {}
    assert result.output.name == "S01E01.mkv"
    assert result.track_order
    assert result.sources[0].tracks
    assert any(t.enabled and t.is_audio for t in result.sources[1].tracks)
    assert recipe.sync_strategy == "auto"


@pytest.mark.parametrize("use_profile", [False, True])
def test_witness_selection_survives_profile_and_exact_job_roundtrip(assembly, tmp_path, use_profile):
    episode, recipe, _ = assembly
    options = SimpleNamespace(**vars(CommonOptions()))
    if use_profile:
        profile = {"version": 1, "kind": "decision-profile", "name": "Test", "rules": [{
            "id": "rename", "scope": "all", "match": {"all": []}, "actions": [
                {"type": "set_title", "value": "Profil"},
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


def test_profile_filters_model_candidates_and_keeps_audio_variants(assembly, tmp_path):
    episode, recipe, _ = assembly
    profile = {"version": 1, "kind": "decision-profile", "name": "Après témoin", "rules": [{
        "id": "enable", "scope": "all", "match": {"all": []},
        "actions": [{"type": "set_enabled", "value": True}],
    }, {
        # Le deuxième donneur a la même piste FR : il ne doit ni gagner ni
        # provoquer une ambiguïté après avoir été décoché dans le témoin.
        "id": "best-fr", "scope": "best", "tie_break": "ambiguous",
        "match": {"all": [{"field": "type", "op": "is", "value": "audio"},
                            {"field": "language", "op": "is", "value": "fr-FR"}]},
        "actions": [{"type": "set_title", "value": "French DDP 5.1"},
                    {"type": "create_audio_variant", "codec": "ac3", "bitrate_kbps": 640,
                     "title_pattern": "French AC3"}],
    }, {
        "id": "remove-full-fr", "scope": "all", "write_mode": "override",
        "match": {"all": [{"field": "type", "op": "is", "value": "subtitle"},
                            {"field": "language", "op": "is", "value": "fr-FR"},
                            {"field": "source_title", "op": "is", "value": "Complets"}]},
        "actions": [{"type": "set_enabled", "value": False}],
    }]}
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(profile))
    options = SimpleNamespace(**vars(CommonOptions()), profile=str(path))
    result, _ = prepare_matrix_episode(episode, recipe, tmp_path, AppConfig(), options, Logger())
    active = [t for s in result.sources for t in s.tracks if t.enabled]
    assert not any(t.enabled for t in result.sources[2].tracks)
    assert not any(t.language == "es-ES" or t.orig_title in {"Commentary", "VO"} for t in active)
    assert not any(t.is_subtitle and t.language == "fr-FR" and t.orig_title == "Complets" for t in active)
    assert next(t for t in active if t.is_audio and t.language == "fr-FR" and not t.is_new).title == "French DDP 5.1"
    variant = next(t for t in active if t.is_new)
    assert variant.codec == "AC3" and variant.title == "French AC3"
    assert any(item[2] == variant.entry_id for item in result.track_order)
    path = tmp_path / "model-profile.exact-job.json"
    save_workflow(path, remux_config_to_exact_job(result))
    restored = build_remux_config(load_workflow(path), AppConfig(), CommonOptions(), Logger())
    assert remux_config_to_exact_job(restored) == remux_config_to_exact_job(result)


def test_profile_alone_selects_tracks_without_language_recipe_fallback(assembly, tmp_path):
    episode, recipe, _ = assembly
    recipe.track_choices = None
    recipe.keep_master_audio_langs = recipe.donor_audio_langs = recipe.donor_sub_langs = []
    profile = {"version": 1, "kind": "decision-profile", "name": "Profil seul",
               "selection_policy": {"disable_unmatched_types": ["video", "audio", "subtitle"]},
               "rules": [{"id": "donor-en", "scope": "all", "match": {"all": [
                   {"field": "type", "op": "is", "value": "audio"},
                   {"field": "source_title", "op": "is", "value": "VO"},
               ]}, "actions": [{"type": "set_enabled", "value": True},
                               {"type": "set_title", "value": "English from profile"}]}]}
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(profile))
    result, _ = prepare_matrix_episode(episode, recipe, tmp_path, AppConfig(),
                                      SimpleNamespace(**vars(CommonOptions()), profile=str(path)), Logger())
    assert [(s.file_index, t.title) for s in result.sources for t in s.tracks if t.enabled] == [(1, "English from profile")]


def test_profile_has_last_word_after_subtitle_detection(assembly, tmp_path, monkeypatch):
    episode, recipe, sources = assembly
    # Forcés FR non taggés, audio par défaut EN dans la source : le profil
    # pourra choisir le français après la détection, puis renommer les ST.
    sources[0].tracks[1].flag_default = True
    sources[1].tracks[3].flag_forced = sources[1].tracks[3].orig_flag_forced = False
    from core.workflows.hybrid_matrix import witness_track_choices
    recipe.track_choices = tuple(
        choice for index, source in enumerate(sources)
        for choice in witness_track_choices("master" if index == 0 else f"src_{index + 1}", source.tracks)
    )
    extracts = []

    def extract(command, **kwargs):
        source = Path(command[command.index("-i") + 1]).name
        maps = [index for index, value in enumerate(command) if value == "-map"]
        extracts.append((source, [command[index + 1] for index in maps]))
        for index in maps:
            # -map 0:N -c:s srt -f srt <sortie> ; l'inspecteur simulé inverse
            # les pistes : ID 12 = forcés du donneur.
            forced = "source1" in source and command[index + 1] == "0:12"
            content = "Hello" if forced else "[Music]" + " --> " * 60
            Path(command[index + 6]).write_text("1\n00:00:01,000 --> 00:00:02,000\n" + content)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("core.workflows.subtitle_text.run_probe", extract)
    profile = {"version": 1, "kind": "decision-profile", "name": "Flags détectés", "rules": [{
        "id": "subtitles", "scope": "all", "match": {"all": [
            {"field": "type", "op": "is", "value": "subtitle"},
        ]}, "actions": [{"type": "set_title", "pattern": "{lang_name} {flag_forced} {flag_hearing_impaired}"},
                        {"type": "set_flags", "value": {"default": False}}],
    }, {
        "id": "sdh-first", "scope": "all", "match": {"all": [
            {"field": "type", "op": "is", "value": "subtitle"},
            {"field": "flag_hearing_impaired", "op": "is", "value": True},
        ]}, "actions": [{"type": "set_order_priority", "value": 1}],
    }], "variables": {"aliases": {"*": {"Hearing Impaired": "SDH"}}}}
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(profile))
    options = SimpleNamespace(**vars(CommonOptions(auto_forced_subs=True, auto_sdh=True)), profile=str(path))
    result, _ = prepare_matrix_episode(episode, recipe, tmp_path, AppConfig(), options, Logger())
    active_subs = [t for s in result.sources for t in s.tracks if t.enabled and t.is_subtitle]
    assert sum(len(maps) for _source, maps in extracts) == len(active_subs) == 3
    assert len({source for source, _maps in extracts}) == len(extracts)  # Une seule lecture par fichier.
    forced = next(t for t in active_subs if t.orig_title == "Forcés")
    assert forced.flag_forced and forced.title == "French Forced"
    assert not any(t.flag_default for t in active_subs)  # La détection ne réécrit pas le profil.
    assert all(t.flag_hearing_impaired and "SDH" in t.title for t in active_subs if t is not forced)
    subtitle_ids = {t.entry_id for t in active_subs}
    subtitle_order = [item[2] for item in result.track_order if item[2] in subtitle_ids]
    assert subtitle_order[-1] == forced.entry_id  # L'ordre du profil prime sur forced_first.
    forced.flag_enabled = False  # drapeau Matroska désactivé, piste gardée (sélection)
    path = tmp_path / "detected.exact-job.json"
    save_workflow(path, remux_config_to_exact_job(result))
    restored = build_remux_config(load_workflow(path), AppConfig(), CommonOptions(), Logger())
    assert remux_config_to_exact_job(restored) == remux_config_to_exact_job(result)

    def states(config):  # état des pistes reconstruites, pas seulement deux exports
        return sorted((s.file_index, t.track_type, int(t.mkv_tid), t.is_new, t.title, t.language, t.enabled,
                       t.flag_enabled, t.flag_default, t.flag_forced, t.flag_hearing_impaired)
                      for s in config.sources for t in s.tracks)

    assert states(restored) == states(result)
    assert not next(t for s in restored.sources for t in s.tracks if t.orig_title == "Forcés").flag_enabled


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

    monkeypatch.setattr("core.jobs.remux_config.resolve_tmdb_metadata", metadata)
    monkeypatch.setattr("core.workflows.subtitle_heuristics.classify_subtitles", classify)
    options = SimpleNamespace(**vars(CommonOptions(ffmpeg="ffmpeg", clean_nfo=False, crossfade_ms=137, auto_forced_subs=True, auto_sdh=True, forced_threshold=17)),
                              auto_tmdb=213562, no_cover=not cover, tag="MVO",
                              output_template="{title:release}.S{season_num:02d}E{episode_num:02d}.{audio-multi}.{audio-fr-tag}.{audio-codec-release:best}.{release_group}")
    result, _ = prepare_matrix_episode(episode, recipe, tmp_path, AppConfig(), options, Logger())
    assert result.output.name == "Crystal.Lake.S01E01.MULTi.VFF.DDP.MVO.mkv"
    assert result.crossfade_ms == 137 and not result.clean_nfo
    assert calls == [{"enabled": True, "kind": "tv", "season": "1", "episode": "1", "cover": cover, "id": 213562}]
    assert {key: classified[0][1][key] for key in ("ffmpeg", "forced", "sdh", "threshold")} == {
        "ffmpeg": "ffmpeg", "forced": True, "sdh": True, "threshold": 17}
    assert (0, "SDH") in classified[0][0] and (0, "Complets") not in classified[0][0]
    job = remux_config_to_exact_job(result)
    assert job["file_title"] == result.file_title and job["tag_overrides"]["SEASON"] == "1"
    assert bool(job.get("tmdb_cover")) is cover


def test_live_name_preview_matches_prepared_jobs_and_preserves_metadata_and_aliases(assembly, tmp_path, monkeypatch, qt_app):
    from ui.panels.hybrid_studio import HybridStudio
    from ui.panels.remux_panel.widgets.sync_studio_dialog import SyncStudioDialog

    episode, recipe, sources = assembly
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"version": 1, "kind": "decision-profile", "name": "Aliases", "rules": [],
                                   "variables": {"release_group": "PROFILE", "aliases": {"codec": {"EAC3": "DDP"}}}}))
    calls = []

    def metadata(job, *_a, **_k):
        if not job.get("tmdb"):
            return "", None, None, None
        calls.append(job["tmdb"])
        number = job["tmdb"]["episode"]
        details = MediaDetails(title="Crystal Lake", year="2026", season="1", episode=number, episode_title=f"Épisode {number}")
        return details.formatted_container_title(), details.to_mkv_tags(), None, details

    monkeypatch.setattr("core.jobs.remux_config.resolve_tmdb_metadata", metadata)
    monkeypatch.setattr(SyncStudioDialog, "_load_segment_audio_async", lambda *_a: None)
    template = "{title:release}.{season_episode}.{episode_title:release}.{audio-multi}.{audio-codec:best}.{release_group}"
    options = SimpleNamespace(**vars(CommonOptions()), profile=str(profile), auto_tmdb=213562,
                              output_template=template, tag="MVO")
    episodes, jobs = [], {}
    for row in range(2):
        current = copy.deepcopy(episode)
        current.episode = row + 1
        sources[0].path = tmp_path / f"Crystal.Lake.S01E{row+1:02d}.2160p.WEB.mkv"
        current.master_file = sources[0].path
        jobs[row] = prepare_matrix_episode(current, recipe, tmp_path, AppConfig(), options, Logger())
        episodes.append(current)
    panel = HybridStudio(AppConfig())
    try:
        panel.reference.setText(str(tmp_path))
        panel.profile.setText(str(profile))
        panel.controls["auto-tmdb"].setText("213562")
        panel.controls["output-template"].setText(template)
        panel.matrix_episodes, panel.jobs = episodes, jobs
        panel.table.setRowCount(2)
        panel.set_busy(False)
        assert panel.output_preview.name.text() == jobs[0][0].output.name
        original = remux_config_to_exact_job(jobs[0][0])
        panel.controls["tag"].setText("NEW")
        assert panel.output_preview.name.text().endswith(".DDP.NEW.mkv")
        panel.controls["tag"].clear()
        assert panel.output_preview.name.text().endswith(".DDP.PROFILE.mkv")
        panel.controls["tag"].setText("MVO")
        panel.sync_episode.setCurrentIndex(1)
        assert panel.output_preview.name.text() == jobs[1][0].output.name
        assert "S01E02.Episode.2" in panel.output_preview.name.text()
        assert "S01E02" in panel.output_preview.caption.text()
        panel.controls["output-template"].setText("{title:release}.{episode_title:release}-{video-codec:best}")
        assert panel.output_preview.name.text() == "Crystal.Lake.Episode.2-HEVC.mkv"
        panel.controls["auto-tmdb"].setText("999")
        assert "Crystal.Lake" not in panel.output_preview.name.text()
        panel.controls["auto-tmdb"].setText("213562")
        assert panel.output_preview.name.text() == "Crystal.Lake.Episode.2-HEVC.mkv"
        assert len(calls) == 2  # Aucun TMDB pendant les éditions ou changements d'épisode.
        assert remux_config_to_exact_job(jobs[0][0]) == original
    finally:
        panel.close()


def test_profile_output_template_names_the_plan_and_its_preview(assembly, tmp_path, qt_app):
    from core.workflows.hybrid_matrix import default_matrix_output_name
    from ui.panels.hybrid_studio import HybridStudio

    episode, recipe, _ = assembly
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"version": 1, "kind": "decision-profile", "name": "Nom", "rules": [],
                                   "output_template": "{season_episode}.profile-name"}))
    options = SimpleNamespace(**vars(CommonOptions()), profile=str(profile))
    result, _ = prepare_matrix_episode(episode, recipe, tmp_path, AppConfig(), options, Logger())
    assert result.output.name == "S01E01.profile-name.mkv"
    panel = HybridStudio(AppConfig())
    try:
        panel.reference.setText(str(episode.master_file))
        panel.profile.setText(str(profile))
        assert panel.controls["output-template"].placeholderText() == "{season_episode}.profile-name"
        assert panel.output_preview.name.text() == "S01E01.profile-name.mkv"
        panel.controls["output-template"].setText("{season_episode}.explicite")
        assert panel.output_preview.name.text() == "S01E01.explicite.mkv"
        panel.controls["output-template"].clear()
        panel.profile.setText("")
        assert panel.controls["output-template"].placeholderText() == ""
        assert panel.output_preview.name.text() == default_matrix_output_name(episode.master_file, panel.recipe.output_tag)
    finally:
        panel.close()


@pytest.mark.parametrize("edited", [False, True])
def test_witness_language_edit_reaches_the_profile(assembly, tmp_path, edited):
    import io
    from core.workflows.hybrid_matrix import HybridTrackChoice

    episode, _, sources = assembly
    # Dossier donneur de SRT seuls : piste sans langue, langue saisie sur le témoin.
    sources[2].tracks = [TrackEntry(0, "subtitle", "SUBRIP", "", "", "", file_id="src2")]
    recipe = HybridRecipe.from_witness_config(RemuxConfig(sources, tmp_path / "out.mkv", [], crossfade_ms=0))
    recipe.sync_strategy = "none"
    if edited:
        choice = next(c for c in recipe.track_choices if c.source_id == "src_3")
        assert isinstance(choice, HybridTrackChoice)
        recipe.track_overrides = {choice: {"language": "fr-FR", "title": "French SDH"}}
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({
        "version": 1, "kind": "decision-profile", "name": "FR",
        "selection_policy": {"disable_unmatched_types": ["subtitle"]},
        "rules": [{"id": "fr", "scope": "all", "match": {"all": [
            {"field": "type", "op": "is", "value": "subtitle"}, {"field": "language", "op": "is", "value": "fr-FR"},
        ]}, "actions": [{"type": "set_enabled", "value": True}]}],
    }))
    stream = io.StringIO()
    options = SimpleNamespace(**vars(CommonOptions()), profile=str(profile))
    result, _ = prepare_matrix_episode(episode, recipe, tmp_path, AppConfig(), options, Logger(stream=stream))
    srt = result.sources[2].tracks[0]
    if edited:
        assert srt.enabled and (srt.language, srt.title) == ("fr-FR", "French SDH")
        assert "#10 subtitle" not in stream.getvalue()
    else:
        # Sans langue, aucune règle ne la retient : l'écart est signalé, pas silencieux.
        assert not srt.enabled
        assert "écartée(s) par le profil" in stream.getvalue() and "#10 subtitle und" in stream.getvalue()


def test_witness_name_preview_follows_checkboxes_before_analysis(assembly, qt_app):
    from core.inspector import AudioTrack
    from ui.panels.hybrid_studio import HybridStudio

    _, _, sources = assembly
    panel = HybridStudio(AppConfig())
    try:
        source = sources[0].path
        panel.reference.setText(str(source))
        panel.controls["output-template"].setText("{season_episode}.{audio-multi}.{audio-fr-tag}.{audio-codec-release:best}")
        master = FileInfo(source, "matroska", 1, 100, None,
                          audio_tracks=[AudioTrack(0, "EAC3", "", 6, "5.1", 48000, None, "en-US", "VO")])
        donor = FileInfo(sources[1].path, "matroska", 1, 100, None,
                         audio_tracks=[AudioTrack(0, "EAC3", "", 6, "5.1", 48000, None, "fr-FR", "VF")])
        panel._populate_witness_table([("Master", "master", master), ("Donneur", "donor", donor)])
        assert panel.output_preview.name.text() == "S01E01.MULTi.VFF.DDP.mkv"
        assert panel.output_preview.note.text()
        panel.witness_track_items[1]["checkbox"].setChecked(False)
        assert panel.output_preview.name.text() == "S01E01.DDP.mkv"
        panel.reference.setText(str(source.with_name("other.mkv")))
        assert "DDP" not in panel.output_preview.name.text()  # Pas de données d'anciennes sources.
    finally:
        panel.close()


def test_scene_numbers_and_default_name_are_available_without_tmdb(assembly, tmp_path):
    from cli.output_template import render_output_filename
    from core.workflows.hybrid_matrix import default_matrix_output_name

    episode, recipe, _ = assembly
    template = "{season_episode}.S{season}E{episode}.{audio-codec-release:best}"
    options = SimpleNamespace(**vars(CommonOptions()), output_template=template)
    result, _ = prepare_matrix_episode(episode, recipe, tmp_path, AppConfig(), options, Logger())
    assert result.output.name == "S01E01.S01E01.DDP.mkv"
    assert render_output_filename(template, episode.naming_context) == result.output.name
    options.output_template = ""
    result, _ = prepare_matrix_episode(episode, recipe, tmp_path, AppConfig(), options, Logger())
    assert result.output.name == default_matrix_output_name(episode.master_file)
    assert render_output_filename(template, episode.naming_context) == "S01E01.S01E01.DDP.mkv"


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
        # Les sources sont des fichiers vides : ce test porte sur la
        # régénération des plans, pas sur l'extraction des sous-titres.
        panel.controls["auto-forced-subs"].setChecked(False)
        panel.controls["auto-sdh"].setChecked(False)
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


def test_captured_profile_with_a_cleared_variant_survives_hybrid_and_exact_job(assembly, tmp_path):
    """Audit 19 : capture réelle d'un épisode préparé (VF vidée, variante AC3 vidée et FlagEnabled=0), appliquée
    par Hybrid à l'épisode, puis exact-job reconstruit : pistes identiques."""
    from core.profiles.decision import remux_config_to_decision_profile
    from core.workflows.remux_models import clone_track_entry

    episode, recipe, _sources = assembly
    base, _ = prepare_matrix_episode(episode, recipe, tmp_path, AppConfig(), CommonOptions(), Logger())
    vf = next(t for s in base.sources for t in s.tracks if t.enabled and t.is_audio and t.language == "fr-FR")
    vf.title = vf.language = ""
    variant = clone_track_entry(vf)
    variant.codec, variant.flag_enabled = "AC3", False
    source = next(s for s in base.sources if vf in s.tracks)
    source.tracks.append(variant)
    base.track_order = [*base.track_order, (source.file_index, variant.mkv_tid, variant.entry_id)]
    path = tmp_path / "capture.json"
    path.write_text(json.dumps(remux_config_to_decision_profile(base, name="Capture")))
    options = SimpleNamespace(**vars(CommonOptions()), profile=str(path))

    result, _ = prepare_matrix_episode(episode, recipe, tmp_path, AppConfig(), options, Logger())

    donor_audio = sorted((t.title, t.language, t.flag_enabled, t.is_new, t.codec)
                         for s in result.sources for t in s.tracks if t.is_audio and t.enabled and t.file_id == vf.file_id)
    assert donor_audio == [("", "", False, True, "AC3"), ("", "", True, False, "EAC3")]
    restored = build_remux_config(json.loads(json.dumps(remux_config_to_exact_job(result))), AppConfig(),
                                  CommonOptions(), Logger())

    def states(config):
        return sorted((s.file_index, t.track_type, int(t.mkv_tid), t.is_new, t.codec, t.title, t.language, t.enabled,
                       t.flag_enabled, t.flag_default, t.flag_forced)
                      for s in config.sources for t in s.tracks)

    assert states(restored) == states(result)


def _variant_witness(sources, tmp_path, *, original_checked, extra_variant=False):
    from core.workflows.remux_models import clone_track_entry

    witness_sources = copy.deepcopy(sources)
    donor = witness_sources[1]
    vf = next(t for t in donor.tracks if t.is_audio and t.title == "VF")
    vf.enabled = original_checked
    variant = clone_track_entry(vf)
    variant.codec, variant.enabled, variant.flag_enabled = "AC3", True, False
    donor.tracks.append(variant)
    if extra_variant:
        second = clone_track_entry(vf)
        second.codec, second.enabled = "EAC3", True
        donor.tracks.append(second)
    return RemuxConfig(witness_sources, tmp_path / "out.mkv", [], crossfade_ms=0)


@pytest.mark.parametrize("path", ["object", "json", "overrides"])
@pytest.mark.parametrize("original_checked", [True, False])
def test_witness_with_an_audio_variant_builds_an_applicable_recipe(assembly, tmp_path, original_checked, path):
    """Audits 20–21 : variante du témoin jamais comptée comme piste physique ; recréée par le profil (capture du
    témoin), y compris quand seule la variante est cochée, après export / relecture JSON ou surcharge d'option ;
    sans profil : refus explicite ; original servant seulement au clonage décoché en sortie."""
    from core.jobs.hybrid_studio import recipe_with_overrides
    from core.profiles.decision import remux_config_to_decision_profile

    episode, _recipe, sources = assembly
    witness = _variant_witness(sources, tmp_path, original_checked=original_checked)
    recipe = HybridRecipe.from_witness_config(witness)
    recipe.sync_strategy = "none"
    assert recipe.witness_variants == 1
    assert all(choice.codec != "AC3" for choice in recipe.track_choices or ())
    assert len(recipe.variant_source_choices) == 1  # provenance gardée, original coché ou non
    if path == "json":
        recipe = HybridRecipe.from_dict(json.loads(json.dumps(recipe.to_dict())))
    elif path == "overrides":
        recipe = recipe_with_overrides(recipe, crossfade_ms=20)
    assert recipe.witness_variants == 1
    assert len(recipe.variant_source_choices) == 1

    with pytest.raises(ValueError, match="variante"):
        prepare_matrix_episode(episode, recipe, tmp_path, AppConfig(), CommonOptions(), Logger())

    profile_path = tmp_path / "capture.json"
    profile_path.write_text(json.dumps(remux_config_to_decision_profile(witness, name="Témoin")))
    options = SimpleNamespace(**vars(CommonOptions()), profile=str(profile_path))
    result, _ = prepare_matrix_episode(episode, recipe, tmp_path, AppConfig(), options, Logger())
    vf_tracks = [(t.codec, t.is_new, t.enabled, t.flag_enabled)
                 for s in result.sources for t in s.tracks if t.is_audio and t.title == "VF"]
    assert ("AC3", True, True, False) in vf_tracks
    assert ("EAC3", False, original_checked, True) in vf_tracks  # original de clonage seul : décoché
    order_ids = {item[2] for item in result.track_order if len(item) > 2}
    original = next(t for s in result.sources for t in s.tracks if t.is_audio and t.title == "VF" and not t.is_new)
    assert (original.entry_id in order_ids) is original_checked
    restored = build_remux_config(json.loads(json.dumps(remux_config_to_exact_job(result))), AppConfig(),
                                  CommonOptions(), Logger())

    def states(config):
        return sorted((s.file_index, t.track_type, int(t.mkv_tid), t.is_new, t.codec, t.title, t.language, t.enabled,
                       t.flag_enabled) for s in config.sources for t in s.tracks)

    assert states(restored) == states(result)


def test_witness_with_several_variants_of_one_source(assembly, tmp_path):
    from core.profiles.decision import remux_config_to_decision_profile

    episode, _recipe, sources = assembly
    witness = _variant_witness(sources, tmp_path, original_checked=False, extra_variant=True)
    recipe = HybridRecipe.from_witness_config(witness)
    recipe.sync_strategy = "none"
    assert recipe.witness_variants == 2 and len(recipe.variant_source_choices) == 1
    profile_path = tmp_path / "capture.json"
    profile_path.write_text(json.dumps(remux_config_to_decision_profile(witness, name="Témoin")))
    options = SimpleNamespace(**vars(CommonOptions()), profile=str(profile_path))
    result, _ = prepare_matrix_episode(episode, recipe, tmp_path, AppConfig(), options, Logger())
    assert sorted(t.codec for s in result.sources for t in s.tracks if t.is_new and t.enabled) == ["AC3", "EAC3"]


@pytest.mark.parametrize("payload, message", [
    ({"witness_variants": True}, "witness_variants"),
    ({"witness_variants": -1}, "witness_variants"),
    ({"variant_source_choices": {}}, "variant_source_choices"),
    ({"witness_variants": 1, "variant_source_choices": [{"source_id": "src_2"}]}, "piste invalide"),
    ("choix-valide", "sans variante"),
])
def test_recipe_variant_fields_are_validated(payload, message):
    from dataclasses import asdict

    from core.workflows.hybrid_matrix import HybridTrackChoice

    if payload == "choix-valide":  # pistes sources sans variante : incohérent
        track = TrackEntry(1, "audio", "EAC3", "", "fr-FR", "VF")
        payload = {"variant_source_choices": [asdict(HybridTrackChoice.from_track("src_2", track))]}
    with pytest.raises(ValueError, match=message):
        HybridRecipe.from_dict({"format": HybridRecipe.FORMAT, "version": 1, **payload})


def test_recipe_without_variants_keeps_its_json():
    assert not {"witness_variants", "variant_source_choices"} & set(HybridRecipe().to_dict())


def test_missing_variant_source_track_is_reported(assembly, tmp_path):
    episode, _recipe, sources = assembly
    witness = _variant_witness(sources, tmp_path, original_checked=False)
    recipe = HybridRecipe.from_witness_config(witness)
    recipe.variant_source_choices = (replace(recipe.variant_source_choices[0], title="Absente"),)
    options = SimpleNamespace(**vars(CommonOptions()), profile=str(tmp_path / "p.json"))
    (tmp_path / "p.json").write_text(json.dumps({"version": 1, "kind": "decision-profile", "name": "p", "rules": []}))
    with pytest.raises(ValueError, match="introuvables"):
        prepare_matrix_episode(episode, recipe, tmp_path, AppConfig(), options, Logger())


def test_rechecked_variant_source_is_selected_not_reported_missing(assembly, tmp_path):
    """Audit 22 : source de variante recochée après relecture de la recette (présente dans les deux listes) →
    retenue, jamais « introuvable »."""
    episode, _recipe, sources = assembly
    recipe = HybridRecipe.from_witness_config(_variant_witness(sources, tmp_path, original_checked=False))
    recipe = HybridRecipe.from_dict(json.loads(json.dumps(recipe.to_dict())))
    recipe.sync_strategy = "none"
    recipe.track_choices = (*recipe.track_choices, *recipe.variant_source_choices)  # recochée, liste non élaguée
    profile_path = tmp_path / "variante.json"
    profile_path.write_text(json.dumps({"version": 1, "kind": "decision-profile", "name": "v", "rules": [{
        "id": "v", "scope": "all", "match": {"all": [{"field": "codec", "op": "is", "value": "EAC3"},
                                                     {"field": "language", "op": "is", "value": "fr-FR"}]},
        "actions": [{"type": "create_audio_variant", "codec": "ac3"}]}]}))
    options = SimpleNamespace(**vars(CommonOptions()), profile=str(profile_path))
    result, _ = prepare_matrix_episode(episode, recipe, tmp_path, AppConfig(), options, Logger())
    vf = [(t.codec, t.is_new, t.enabled) for s in result.sources for t in s.tracks if t.is_audio and t.title == "VF"]
    assert ("EAC3", False, True) in vf and ("AC3", True, True) in vf


def test_studio_keeps_variant_sources_across_recheck_and_uncheck(assembly, qt_app):
    from core.inspector import AudioTrack, SubtitleTrack, VideoTrack
    from ui.panels.hybrid_studio import HybridStudio

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
                    stream = AudioTrack(track.mkv_tid, track.codec, "", 6, "5.1", 48000, None, track.language,
                                        track.title)
                else:
                    stream = SubtitleTrack(track.mkv_tid, track.codec, track.language, track.title,
                                           forced=track.orig_flag_forced)
                getattr(info, f"{track.track_type}_tracks").append(stream)
            inspected.append((f"Source {source_index}", "master" if source_index == 0 else "donor", info))
        panel._populate_witness_table(inspected)
        vf_item = next(item for item in panel.witness_track_items
                       if item["role"] == "donor" and item["type"] == "audio" and item["choice"].title.lower() == "vf")
        other = next(item for item in panel.witness_track_items if item is not vf_item and item["type"] == "audio")
        panel.recipe = replace(panel.recipe, witness_variants=1,
                               variant_source_choices=(vf_item["choice"], other["choice"]))
        vf_item["checkbox"].setChecked(True)
        other["checkbox"].setChecked(False)
        panel.apply_witness_recipe()
        # Audit 23 : provenance gardée (une source recochée est retenue par la sélection, et redevient candidate
        # de clonage une fois décochée).
        assert panel.recipe.variant_source_choices == (vf_item["choice"], other["choice"])
        assert vf_item["choice"] in panel.recipe.track_choices
        vf_item["checkbox"].setChecked(False)
        panel.apply_witness_recipe()
        assert panel.recipe.variant_source_choices == (vf_item["choice"], other["choice"])
        assert vf_item["choice"] not in panel.recipe.track_choices and panel.recipe.witness_variants == 1
    finally:
        panel.close()


@pytest.mark.parametrize("field, value", [("source_id", []), ("occurrence", -1), ("forced", "true"),
                                          ("occurrence", True), ("title", 3)])
def test_recipe_choices_are_type_checked(field, value):
    from dataclasses import asdict

    from core.workflows.hybrid_matrix import HybridTrackChoice

    raw = asdict(HybridTrackChoice.from_track("src_2", TrackEntry(1, "audio", "EAC3", "", "fr-FR", "VF")))
    raw[field] = value
    for payload in ({"witness_variants": 1, "variant_source_choices": [raw]}, {"track_choices": [raw]}):
        with pytest.raises(ValueError, match="piste invalide"):
            HybridRecipe.from_dict({"format": HybridRecipe.FORMAT, "version": 1, **payload})


@pytest.mark.parametrize("value", [2, False, {}, "", "x"])
def test_recipe_containers_are_validated(value):
    for key in ("track_choices", "track_overrides"):
        with pytest.raises(ValueError, match=key):
            HybridRecipe.from_dict({"format": HybridRecipe.FORMAT, "version": 1, key: value})


def test_original_checked_at_capture_then_unchecked_still_recreates_its_variant(assembly, tmp_path):
    """Audit 24 : original coché au moment de la capture, décoché ensuite (Studio) → toujours candidat de clonage,
    variante recréée, original écarté de la sortie."""
    from core.profiles.decision import remux_config_to_decision_profile

    episode, _recipe, sources = assembly
    witness = _variant_witness(sources, tmp_path, original_checked=True)
    recipe = HybridRecipe.from_witness_config(witness)
    recipe.sync_strategy = "none"
    source_choice = recipe.variant_source_choices[0]
    assert source_choice in recipe.track_choices
    recipe.track_choices = tuple(choice for choice in recipe.track_choices if choice != source_choice)  # décochée
    recipe = HybridRecipe.from_dict(json.loads(json.dumps(recipe.to_dict())))
    profile_path = tmp_path / "capture.json"
    profile_path.write_text(json.dumps(remux_config_to_decision_profile(witness, name="Témoin")))
    options = SimpleNamespace(**vars(CommonOptions()), profile=str(profile_path))
    result, _ = prepare_matrix_episode(episode, recipe, tmp_path, AppConfig(), options, Logger())
    vf = [(t.codec, t.is_new, t.enabled) for s in result.sources for t in s.tracks if t.is_audio and t.title == "VF"]
    assert ("AC3", True, True) in vf and ("EAC3", False, False) in vf
