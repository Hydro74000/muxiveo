from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.profiles.hybrid import (
    HybridProfileManager,
    HybridResolutionError,
    apply_decision_profile,
    match_track_selector,
    remux_config_to_decision_profile,
    resolve_track_selector,
    track_selector_for_entry,
)
from core.workflows.remux_models import RemuxConfig, SourceInput, TrackEntry, clone_track_entry


def test_hybrid_profile_manager_uses_json_files(tmp_path):
    manager = HybridProfileManager(tmp_path / "hybrid")

    path = manager.save(
        {
            "name": "Série FR/EN",
            "profile_mode": "decision",
            "sources": [{"path": "/tmp/source.mkv"}],
            "output": "/tmp/out.mkv",
            "rules": {"normalize_languages": True},
        }
    )

    assert path.parent == tmp_path / "hybrid"
    assert path.name == "Série_FR_EN.json"
    assert manager.names() == ["Série FR/EN"]
    assert manager.load("Série FR/EN")["version"] == 2
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["kind"] == "hybrid-profile"
    assert "sources" not in payload
    assert "output" not in payload


def test_track_selector_matches_by_stable_characteristics_not_entry_id():
    original = TrackEntry(
        mkv_tid=7,
        track_type="audio",
        codec="EAC3",
        display_info="5.1  768 kbps  Atmos",
        language="fra",
        title="VF",
        file_id="src0",
        orig_language="fra",
        orig_title="VF",
    )
    replacement = TrackEntry(
        mkv_tid=11,
        track_type="audio",
        codec="EAC3",
        display_info="5.1  640 kbps  Atmos",
        language="fr-FR",
        title="VF",
        file_id="src0",
        orig_language="fr-FR",
        orig_title="VF",
    )

    selector = track_selector_for_entry(original, source_index=0, tracks=[original])
    selector.pop("id", None)

    assert match_track_selector(selector, [replacement]) == [replacement]
    assert resolve_track_selector(selector, [replacement]) is replacement


def test_track_selector_reports_ambiguous_matches():
    tracks = [
        TrackEntry(mkv_tid=1, track_type="subtitle", codec="PGS", display_info="", language="fra", title="", file_id="src0"),
        TrackEntry(mkv_tid=2, track_type="subtitle", codec="PGS", display_info="", language="fra", title="", file_id="src0"),
    ]

    with pytest.raises(HybridResolutionError) as excinfo:
        resolve_track_selector({"source": 0, "type": "subtitle", "codec": "PGS"}, tracks)

    assert excinfo.value.report["error"] == "track_selector_ambiguous"
    assert excinfo.value.report["match_count"] == 2


def _audio(
    mkv_tid: int,
    language: str,
    title: str,
    *,
    codec: str = "EAC3",
    display_info: str = "5.1  640 kbps",
    file_id: str = "src0",
    enabled: bool = True,
) -> TrackEntry:
    return TrackEntry(
        mkv_tid=mkv_tid,
        track_type="audio",
        codec=codec,
        display_info=display_info,
        language=language,
        title=title,
        file_id=file_id,
        enabled=enabled,
        orig_language=language,
        orig_title=title,
    )


def _video(
    mkv_tid: int = 0,
    *,
    file_id: str = "src0",
    codec: str = "HEVC",
    display_info: str = "1920x1080  23.976 fps",
    enabled: bool = True,
) -> TrackEntry:
    return TrackEntry(
        mkv_tid=mkv_tid,
        track_type="video",
        codec=codec,
        display_info=display_info,
        language="und",
        title="",
        file_id=file_id,
        enabled=enabled,
        orig_language="und",
    )


def test_decision_profile_has_no_source_or_output_paths():
    video = _video()
    audio = _audio(1, "fra", "VF", enabled=True)
    disabled = _audio(2, "spa", "Spanish", enabled=False)
    config = RemuxConfig(
        sources=[SourceInput(path=Path("/tmp/source-a.mkv"), file_index=0, tracks=[video, audio, disabled])],
        output=Path("/tmp/out.mkv"),
        track_order=[(0, 0, video.entry_id), (0, 1, audio.entry_id)],
    )

    profile = remux_config_to_decision_profile(config, name="FR")

    assert profile["kind"] == "hybrid-profile"
    assert profile["profile_mode"] == "decision"
    assert "sources" not in profile
    assert "output" not in profile
    assert "/tmp/source-a.mkv" not in json.dumps(profile)
    assert "entry_id" not in json.dumps(profile)


def test_decision_profile_video_rule_stores_hex_flags_and_resolution():
    video = _video(display_info="3840x2160  Dolby Vision P8.1 + HDR10  23.976 fps")
    config = RemuxConfig(
        sources=[SourceInput(path=Path("/tmp/source-a.mkv"), file_index=0, tracks=[video])],
        output=Path("/tmp/out.mkv"),
        track_order=[(0, 0, video.entry_id)],
    )

    profile = remux_config_to_decision_profile(config, name="Video")
    video_rule = next(rule for rule in profile["track_rules"] if rule["match"]["type"] == "video")
    match = video_rule["match"]

    assert match["video_flags_hex"].startswith("0x")
    assert int(match["video_flags_hex"], 16) > 0
    assert match["resolution"] == {"width": 3840, "height": 2160, "bucket": "uhd"}
    assert "display_contains" not in match


def test_decision_profile_applies_to_other_source_with_new_track_ids():
    source_video = _video()
    source_audio = _audio(1, "fra", "VF", enabled=True)
    source_extra = _audio(2, "eng", "VO", enabled=False)
    source_audio.title = "VF EAC3 5.1"
    source_audio.flag_default = True
    config = RemuxConfig(
        sources=[SourceInput(path=Path("/tmp/source-a.mkv"), file_index=0, tracks=[source_video, source_audio, source_extra])],
        output=Path("/tmp/out.mkv"),
        track_order=[(0, 0, source_video.entry_id), (0, 1, source_audio.entry_id)],
    )
    profile = remux_config_to_decision_profile(config, name="FR")

    target_video = _video(7)
    target_audio = _audio(11, "fr-FR", "French", display_info="5.1  768 kbps")
    target_extra = _audio(12, "eng", "English")
    result = apply_decision_profile(profile, [target_extra, target_video, target_audio])

    assert result.report["applied_rules"] >= 2
    assert target_audio.enabled is True
    assert target_audio.title == "VF EAC3 5.1"
    assert target_audio.flag_default is True
    assert target_extra.enabled is False
    assert [track.entry_id for track in result.tracks[:2]] == [target_video.entry_id, target_audio.entry_id]


def test_decision_profile_video_picks_closest_resolution():
    profile = {
        "version": 2,
        "kind": "hybrid-profile",
        "profile_mode": "decision",
        "selection_policy": {"disable_unmatched_types": ["video"]},
        "track_rules": [
            {
                "match": {
                    "type": "video",
                    "codec": "HEVC",
                    "resolution": {"width": 3840, "height": 2160, "bucket": "uhd"},
                    "video_flags_hex": "0x00000038",
                },
                "action": {"enabled": True, "title": "Main Video"},
            }
        ],
    }
    hd = _video(0, display_info="1920x1080  SDR", enabled=True)
    uhd = _video(1, display_info="3840x2160  HDR10", enabled=True)

    result = apply_decision_profile(profile, [hd, uhd])

    assert not result.report["ambiguous_rules"]
    assert hd.enabled is False
    assert uhd.enabled is True
    assert uhd.title == "Main Video"


def test_decision_profile_video_tie_uses_first_source_index():
    profile = {
        "version": 2,
        "kind": "hybrid-profile",
        "profile_mode": "decision",
        "selection_policy": {"disable_unmatched_types": ["video"]},
        "track_rules": [
            {
                "match": {
                    "type": "video",
                    "codec": "HEVC",
                    "resolution": {"width": 3840, "height": 2160, "bucket": "uhd"},
                    "video_flags_hex": "0x000000D8",
                },
                "action": {"enabled": True, "title": "Selected"},
            }
        ],
    }
    second_source = _video(0, file_id="src1", display_info="3840x2160  Dolby Vision + HDR10+")
    first_source = _video(0, file_id="src0", display_info="3840x2160  Dolby Vision + HDR10+")

    result = apply_decision_profile(
        profile,
        [second_source, first_source],
        source_index_by_file_id={"src0": 0, "src1": 1},
    )

    assert not result.report["ambiguous_rules"]
    assert first_source.enabled is True
    assert first_source.title == "Selected"
    assert second_source.enabled is False


def test_decision_profile_reports_ambiguous_matches_without_applying():
    profile = {
        "version": 2,
        "kind": "hybrid-profile",
        "profile_mode": "decision",
        "track_rules": [
            {"match": {"type": "audio", "language": "fr-FR"}, "action": {"title": "VF"}}
        ],
    }
    first = _audio(1, "fr-FR", "A")
    second = _audio(2, "fr-FR", "B")

    result = apply_decision_profile(profile, [first, second])

    assert result.report["ambiguous_rules"]
    assert first.title == "A"
    assert second.title == "B"


def test_decision_profile_reports_missing_rules_without_blocking_other_rules():
    profile = {
        "version": 2,
        "kind": "hybrid-profile",
        "profile_mode": "decision",
        "track_rules": [
            {"match": {"type": "audio", "language": "fr-FR"}, "action": {"title": "VF"}},
            {"match": {"type": "subtitle", "language": "fr-FR"}, "action": {"enabled": True}},
        ],
    }
    audio = _audio(1, "fr-FR", "A")

    result = apply_decision_profile(profile, [audio])

    assert audio.title == "VF"
    assert result.report["missing_rules"]
    assert result.report["applied_rules"] == 1


def test_decision_profile_recreates_audio_variants_without_duplicates():
    source = _audio(1, "fra", "VF TrueHD", codec="TRUEHD", display_info="5.1  4000 kbps")
    variant = clone_track_entry(source)
    variant.codec = "AC3"
    variant.display_info = "5.1  640 kbps"
    variant.title = "VF AC3 5.1"
    config = RemuxConfig(
        sources=[SourceInput(path=Path("/tmp/source-a.mkv"), file_index=0, tracks=[source, variant])],
        output=Path("/tmp/out.mkv"),
        track_order=[(0, 1, source.entry_id), (0, 1, variant.entry_id)],
    )
    profile = remux_config_to_decision_profile(config, name="compat")
    target = _audio(9, "fr-FR", "VF TrueHD", codec="TRUEHD", display_info="5.1  4200 kbps")

    first = apply_decision_profile(profile, [target])
    second = apply_decision_profile(profile, first.tracks)

    assert len([track for track in first.tracks if track.is_new]) == 1
    assert len([track for track in second.tracks if track.is_new]) == 1
    created = next(track for track in second.tracks if track.is_new)
    assert created.codec == "AC3"
    assert created.title == "VF AC3 5.1"
