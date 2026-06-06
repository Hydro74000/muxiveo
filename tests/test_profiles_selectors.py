"""
tests/test_profiles_selectors.py — Couvre l'API publique de
``core.profiles.selectors`` : sérialisation de pistes en sélecteurs stables,
matching, résolution stricte/non-stricte, application de spec et export
exact-job d'une RemuxConfig.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.profiles.selectors import (
    SelectorResolutionError,
    apply_track_spec,
    match_track_selector,
    normalize_lang,
    remux_config_to_exact_job,
    resolve_track_selector,
    resolve_track_selector_relaxed,
    track_selector_for_entry,
    track_summary,
)
from core.workflows.remux_models import RemuxConfig, SourceInput, TrackEntry


def _audio(
    mkv_tid: int,
    *,
    language: str = "fr-FR",
    title: str = "VF",
    codec: str = "EAC3",
    display_info: str = "5.1  640 kbps  Atmos",
    file_id: str = "src0",
) -> TrackEntry:
    return TrackEntry(
        mkv_tid=mkv_tid,
        track_type="audio",
        codec=codec,
        display_info=display_info,
        language=language,
        title=title,
        file_id=file_id,
        orig_language=language,
        orig_title=title,
        orig_codec=codec,
        orig_display_info=display_info,
    )


def _video(
    mkv_tid: int = 0,
    *,
    codec: str = "HEVC",
    display_info: str = "3840x2160 HDR10 Dolby Vision Main 10",
    title: str = "",
    file_id: str = "src0",
) -> TrackEntry:
    return TrackEntry(
        mkv_tid=mkv_tid,
        track_type="video",
        codec=codec,
        display_info=display_info,
        language="",
        title=title,
        file_id=file_id,
        orig_codec=codec,
        orig_display_info=display_info,
        orig_title=title,
    )


def _subtitle(mkv_tid: int, *, language: str = "eng", title: str = "Forced", file_id: str = "src0") -> TrackEntry:
    return TrackEntry(
        mkv_tid=mkv_tid,
        track_type="subtitle",
        codec="SUBRIP",
        display_info="",
        language=language,
        title=title,
        file_id=file_id,
        orig_language=language,
        orig_title=title,
        orig_codec="SUBRIP",
    )


# --------------------------------------------------------------------- normalize_lang


def test_normalize_lang_returns_empty_for_blank_inputs():
    assert normalize_lang(None) == ""
    assert normalize_lang("") == ""


def test_normalize_lang_regionalizes_french_variant_from_title():
    # VFQ déclenche la régionalisation fr-CA via Rfc5646LanguageTags
    result = normalize_lang("fra", "VFQ")
    assert result.lower().startswith("fr")


def test_normalize_lang_returns_canonical_or_regionalized_form():
    # Selon Rfc5646LanguageTags : "fr" → variante régionalisée (fr-FR) ou canonique (fra).
    assert normalize_lang("fr").lower().startswith("fr")
    assert normalize_lang("en").lower().startswith("en")


# --------------------------------------------------------------------- track_summary


def test_track_summary_includes_flags_and_normalized_language():
    track = _audio(2, language="fr", title="VF")
    summary = track_summary(track)
    assert summary["type"] == "audio"
    assert summary["id"] == 2
    assert summary["source"] == 0  # file_id="src0"
    assert "flags" in summary
    assert isinstance(summary["flags"], dict)
    assert "default" in summary["flags"]


def test_track_summary_for_video_exposes_resolution_payload_and_flags_hex():
    track = _video(0)
    summary = track_summary(track)
    assert "video_flags_hex" in summary
    assert summary["video_flags_hex"].startswith("0x")
    assert summary["resolution"]["width"] == 3840
    assert summary["resolution"]["height"] == 2160
    assert summary["resolution"]["bucket"] == "uhd"


def test_track_summary_uses_explicit_source_index_mapping():
    track = _audio(1, file_id="custom-uuid")
    summary = track_summary(track, source_index_by_file_id={"custom-uuid": 5})
    assert summary["source"] == 5


# --------------------------------------------------------------------- track_selector_for_entry


def test_track_selector_for_entry_emits_position_within_type():
    a1 = _audio(1, language="fr-FR", title="VF")
    a2 = _audio(2, language="en", title="EN")
    selector = track_selector_for_entry(a2, tracks=[a1, a2])
    assert selector["type"] == "audio"
    assert selector["source"] == 0
    assert selector["position"] == 1  # 2e piste audio de la source
    assert selector["codec"] == "EAC3"
    assert selector["channels"] == "5.1"
    assert selector["audio_object"] == "Atmos"


def test_track_selector_for_entry_strips_empty_and_zero_value_fields():
    track = _audio(1, title="")  # sans titre original => "title" absent
    selector = track_selector_for_entry(track, tracks=[track])
    assert "title" not in selector
    # flags = {} doit être supprimé
    assert "flags" not in selector or selector["flags"] != {}


def test_track_selector_for_entry_includes_resolution_payload_for_video():
    track = _video(0)
    selector = track_selector_for_entry(track, tracks=[track])
    assert selector["type"] == "video"
    assert "resolution" in selector
    assert selector["resolution"]["bucket"] == "uhd"
    assert selector["video_flags_hex"].startswith("0x")


def test_relaxed_selector_resolves_gui_export_video_noise():
    video = TrackEntry(
        0,
        "video",
        "HEVC",
        "3840×2080  Dolby Vision P8.1 + HDR10+  23.976 fps",
        "",
        "",
        file_id="src0",
    )
    video.flag_default = video.orig_flag_default = True
    video.flag_original = video.orig_flag_original = True

    selector = {
        "source": 0,
        "type": "video",
        "position": 0,
        "codec": "HEVC",
        "language": "und",
        "channels": "1",
        "resolution": {"width": 3840, "height": 2080, "bucket": "uhd"},
        "video_flags_hex": "0x000000D8",
        "flags": {"default": True, "original": True},
    }

    assert resolve_track_selector_relaxed(selector, [video]) is video


def test_relaxed_selector_resolves_reordered_audio_by_identity():
    fr = _audio(1, language="fr-FR", title="French (France)", display_info="5.1(side)  640 kbps")
    fr_ca = _audio(2, language="fr-CA", title="French (Canadien)", display_info="5.1(side)  640 kbps")
    en = _audio(3, language="en-US", title="", display_info="5.1(side)  576 kbps  Atmos")
    en.flag_original = en.orig_flag_original = True
    tracks = [fr, fr_ca, en]

    selector = {
        "source": 0,
        "type": "audio",
        "position": 1,
        "codec": "EAC3",
        "language": "en-US",
        "channels": "5.1",
        "audio_object": "Atmos",
        "flags": {"original": True},
    }

    assert resolve_track_selector_relaxed(selector, tracks) is en


def test_relaxed_batch_edits_ignore_reencoded_audio_variants():
    from cli.remux_config import apply_audio_variants, apply_explicit_track_edits

    source = _audio(
        2,
        language="en-US",
        title="Anglais [DTS 5.1]",
        codec="DTS",
        display_info="5.1(side)  1536 kbps",
    )
    tracks = [source]
    selector = {
        "source": 0,
        "type": "audio",
        "position": 0,
        "codec": "DTS",
        "language": "en-US",
        "channels": "5.1",
        "title": "Anglais [DTS-HDMA - 5.1 @ 4000 Kbps]",
    }
    job = {
        "audio_variants": [
            {
                "source_selector": selector,
                "enabled": True,
                "language": "en-US",
                "title": "English Dolby Digital 5.1",
                "codec": "AC3",
                "bitrate_kbps": 576,
            }
        ],
        "tracks": [
            {
                "selector": selector,
                "title": "English DTS 5.1",
            }
        ],
    }
    source_input = SourceInput(path=Path("source.mkv"), file_index=0, tracks=[source])

    apply_audio_variants(job, [source_input], tracks, relaxed_selectors=True)
    assert len(tracks) == 2
    assert tracks[1].is_new is True
    assert tracks[1].codec == "AC3"
    assert tracks[1].orig_codec == "DTS"

    apply_explicit_track_edits(job, tracks, relaxed_selectors=True)

    assert source.title == "English DTS 5.1"
    assert tracks[1].title == "English Dolby Digital 5.1"


def test_relaxed_batch_edits_ignore_missing_disabled_tracks():
    from cli.remux_config import apply_explicit_track_edits

    tracks = [_audio(1, language="fr-FR", title="VF", codec="AC3", display_info="5.1(side)  384 kbps")]
    job = {
        "tracks": [
            {
                "selector": {
                    "source": 0,
                    "type": "audio",
                    "position": 1,
                    "codec": "AC3",
                    "language": "en-US",
                    "channels": "stereo",
                    "title": "Anglais - Commentaire [AC3 - 2.0 @ 192 Kbps]",
                },
                "enabled": False,
            }
        ]
    }

    apply_explicit_track_edits(job, tracks, relaxed_selectors=True)

    assert tracks[0].enabled is True


def test_track_selector_for_entry_emits_flags_only_when_set():
    track = _audio(1)
    track.orig_flag_forced = True
    selector = track_selector_for_entry(track, tracks=[track])
    assert selector["flags"] == {"forced": True}


# --------------------------------------------------------------------- match_track_selector


def test_match_track_selector_filters_by_type_and_codec():
    a = _audio(1, codec="EAC3")
    b = _audio(2, codec="AC3")
    v = _video(0)
    matches = match_track_selector({"type": "audio", "codec": "EAC3"}, [a, b, v])
    assert matches == [a]


def test_match_track_selector_filters_by_language_normalized():
    fr = _audio(1, language="fra")
    en = _audio(2, language="en", title="EN")
    matches = match_track_selector({"type": "audio", "language": "fr"}, [fr, en])
    assert matches == [fr]


def test_match_track_selector_filters_by_atmos_flag():
    atmos = _audio(1, display_info="7.1 1024 kbps Atmos")
    plain = _audio(2, display_info="5.1 640 kbps")
    assert match_track_selector({"type": "audio", "atmos": True}, [atmos, plain]) == [atmos]
    assert match_track_selector({"type": "audio", "atmos": False}, [atmos, plain]) == [plain]


def test_match_track_selector_filters_by_channels_and_audio_object():
    atmos71 = _audio(1, display_info="7.1 1024 kbps Atmos")
    dtsx = _audio(2, display_info="7.1 1536 kbps DTS:X", codec="DTS")
    plain51 = _audio(3, display_info="5.1 640 kbps")
    assert match_track_selector({"type": "audio", "channels": "7.1"}, [atmos71, dtsx, plain51]) == [atmos71, dtsx]
    assert match_track_selector({"type": "audio", "audio_object": "DTS:X"}, [atmos71, dtsx, plain51]) == [dtsx]


def test_match_track_selector_uses_position_within_type():
    a1 = _audio(1, language="fra")
    a2 = _audio(2, language="en", title="EN")
    matches = match_track_selector({"type": "audio", "position": 1, "source": 0}, [a1, a2])
    assert matches == [a2]


def test_match_track_selector_filters_by_title_contains():
    forced = _subtitle(3, title="Forced Subs")
    full = _subtitle(4, title="Full")
    matches = match_track_selector({"type": "subtitle", "title_contains": "forced"}, [forced, full])
    assert matches == [forced]


def test_match_track_selector_filters_by_resolution_object():
    uhd = _video(0)
    hd = _video(1, display_info="1920x1080 SDR")
    matches = match_track_selector(
        {"type": "video", "resolution": {"width": 3840, "height": 2160}},
        [uhd, hd],
    )
    assert matches == [uhd]


def test_match_track_selector_rejects_non_mapping_selector():
    assert match_track_selector(None, [_audio(1)]) == []  # pyright: ignore[reportArgumentType]
    assert match_track_selector([], [_audio(1)]) == []  # pyright: ignore[reportArgumentType]


# --------------------------------------------------------------------- resolve_track_selector


def test_resolve_track_selector_returns_unique_match():
    a1 = _audio(1, language="fra")
    a2 = _audio(2, language="en", title="EN")
    track = resolve_track_selector({"type": "audio", "language": "en"}, [a1, a2])
    assert track is a2


def test_resolve_track_selector_strict_raises_when_ambiguous():
    a1 = _audio(1, language="fra")
    a2 = _audio(2, language="fra", title="VFQ")
    with pytest.raises(SelectorResolutionError) as exc:
        resolve_track_selector({"type": "audio", "language": "fra"}, [a1, a2], context="test")
    assert exc.value.report["error"] == "track_selector_ambiguous"
    assert exc.value.report["match_count"] == 2


def test_resolve_track_selector_strict_raises_when_no_match():
    a1 = _audio(1, language="fra")
    with pytest.raises(SelectorResolutionError) as exc:
        resolve_track_selector({"type": "audio", "language": "jpn"}, [a1])
    assert exc.value.report["error"] == "track_selector_unmatched"


def test_resolve_track_selector_non_strict_returns_first_or_none():
    a1 = _audio(1, language="fra")
    a2 = _audio(2, language="fra", title="VFQ")
    assert resolve_track_selector({"type": "audio", "language": "fra"}, [a1, a2], strict=False) is a1
    assert resolve_track_selector({"type": "audio", "language": "jpn"}, [a1, a2], strict=False) is None


def test_resolve_track_selector_attaches_suggested_profile():
    with pytest.raises(SelectorResolutionError) as exc:
        resolve_track_selector(
            {"type": "audio", "language": "jpn"},
            [_audio(1, language="fra")],
            suggested_profile="fallback.json",
        )
    assert exc.value.report["suggested_profile"] == "fallback.json"


# --------------------------------------------------------------------- apply_track_spec


def test_apply_track_spec_updates_enabled_language_title_and_flags():
    track = _audio(1, language="en", title="EN")
    apply_track_spec(
        track,
        {
            "enabled": False,
            "language": "fr",
            "title": "VF",
            "flags": {"forced": True, "commentary": True},
        },
    )
    assert track.enabled is False
    assert track.language.lower().startswith("fr")
    assert track.title == "VF"
    assert track.flag_forced is True
    assert track.flag_commentary is True


def test_apply_track_spec_normalizes_time_shift_and_sync_rewrite_mode():
    track = _audio(1)
    apply_track_spec(track, {"time_shift_ms": "150", "sync_rewrite_mode": "offset"})
    assert track.time_shift_ms == 150
    assert track.sync_rewrite_mode == "offset"

    apply_track_spec(track, {"time_shift_ms": None, "sync_rewrite_mode": ""})
    assert track.time_shift_ms == 0
    assert track.sync_rewrite_mode == ""


def test_apply_track_spec_ignores_unknown_flag_names():
    track = _audio(1)
    apply_track_spec(track, {"flags": {"forced": True, "unknown_flag": True}})
    assert track.flag_forced is True
    assert not hasattr(track, "flag_unknown_flag")


# --------------------------------------------------------------------- remux_config_to_exact_job


def _config_with_two_audio_tracks(tmp_path: Path) -> RemuxConfig:
    video = _video(0)
    audio_fr = _audio(1, language="fra", title="VF", file_id="src0")
    audio_en = _audio(2, language="en", title="EN", file_id="src0")
    source = SourceInput(
        path=tmp_path / "movie.mkv",
        file_index=0,
        tracks=[video, audio_fr, audio_en],
    )
    return RemuxConfig(
        sources=[source],
        output=tmp_path / "out.mkv",
        track_order=[(0, 0), (0, 1), (0, 2)],
    )


def test_remux_config_to_exact_job_has_no_paths_in_track_payload(tmp_path):
    config = _config_with_two_audio_tracks(tmp_path)
    job = remux_config_to_exact_job(config, name="demo", fallback_profile="default")
    assert job["version"] == 1
    assert job["kind"] == "exact-job"
    assert job["name"] == "demo"
    assert job["fallback_profile"] == "default"
    assert job["sources"][0]["path"] == str(tmp_path / "movie.mkv")
    assert job["output"] == str(tmp_path / "out.mkv")
    for payload in job["tracks"]:
        # Aucune référence à un chemin disque dans le sélecteur
        assert "path" not in payload["selector"]
        assert payload["selector"]["source"] == 0


def test_remux_config_to_exact_job_track_order_uses_selectors(tmp_path):
    config = _config_with_two_audio_tracks(tmp_path)
    job = remux_config_to_exact_job(config)
    assert len(job["track_order"]) == 3
    for item in job["track_order"]:
        assert "selector" in item
        assert item["selector"]["source"] == 0


def test_remux_config_to_exact_job_serializes_sync_rewrite_mode(tmp_path):
    config = _config_with_two_audio_tracks(tmp_path)
    config.sources[0].tracks[1].sync_rewrite_mode = "offset"
    config.sources[0].tracks[1].time_shift_ms = 50
    job = remux_config_to_exact_job(config)
    audio_payload = next(
        payload for payload in job["tracks"] if payload["selector"]["type"] == "audio"
    )
    assert audio_payload["sync_rewrite_mode"] == "offset"
    assert audio_payload["time_shift_ms"] == 50


def test_remux_config_to_exact_job_skips_chapters_when_disabled(tmp_path):
    config = _config_with_two_audio_tracks(tmp_path)
    config.keep_chapters = False
    job = remux_config_to_exact_job(config)
    assert job["chapters"] is False


def test_remux_config_to_exact_job_adds_disabled_tmdb_series_hint(tmp_path):
    config = _config_with_two_audio_tracks(tmp_path)
    config.tag_overrides = {"SEASON": "1", "EPISODE": "1", "SYNOPSIS": "Old"}

    job = remux_config_to_exact_job(config)

    assert job["tmdb"] == {
        "enabled": False,
        "kind": "tv",
        "season": "auto",
        "episode": "auto",
    }


def test_remux_config_to_exact_job_records_attachments_none_label(tmp_path):
    config = _config_with_two_audio_tracks(tmp_path)
    job = remux_config_to_exact_job(config)
    assert job["sources"][0]["attachments"] == "none"


def test_remux_config_to_exact_job_separates_new_audio_variants(tmp_path):
    config = _config_with_two_audio_tracks(tmp_path)
    new_variant = _audio(99, language="fra", title="VFF Atmos", codec="EAC3", file_id="src0")
    new_variant.is_new = True
    new_variant.source_entry_id = config.sources[0].tracks[1].entry_id
    config.sources[0].tracks.append(new_variant)
    config.track_order = list(config.track_order) + [(0, 99, new_variant.entry_id)]

    job = remux_config_to_exact_job(config)
    assert "audio_variants" in job
    assert len(job["audio_variants"]) == 1
    variant_payload = job["audio_variants"][0]
    assert variant_payload["codec"] == "EAC3"
    assert "source_selector" in variant_payload
    # La piste source du variant pointe vers l'audio_fr (entry source)
    assert variant_payload["source_selector"]["type"] == "audio"
    # Les pistes d'origine restent dans tracks_payload
    assert all(not payload.get("codec") for payload in job["tracks"])
