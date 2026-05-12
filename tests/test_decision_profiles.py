from __future__ import annotations

import json
from pathlib import Path

from core.profiles.decision import (
    DecisionProfileManager,
    apply_decision_profile,
    build_video_flags_hex,
    remux_config_to_decision_profile,
    render_title_pattern,
)
from core.workflows.remux_models import RemuxConfig, SourceInput, TrackEntry, clone_track_entry


def _audio(
    mkv_tid: int,
    language: str = "fr-FR",
    title: str = "VF",
    *,
    codec: str = "EAC3",
    display_info: str = "5.1  640 kbps  Atmos",
    enabled: bool = True,
) -> TrackEntry:
    return TrackEntry(
        mkv_tid=mkv_tid,
        track_type="audio",
        codec=codec,
        display_info=display_info,
        language=language,
        title=title,
        enabled=enabled,
        file_id="src0",
        orig_language=language,
        orig_title=title,
    )


def _video(mkv_tid: int, display_info: str, *, enabled: bool = True, file_id: str = "src0") -> TrackEntry:
    return TrackEntry(
        mkv_tid=mkv_tid,
        track_type="video",
        codec="HEVC",
        display_info=display_info,
        language="und",
        title="",
        enabled=enabled,
        file_id=file_id,
        orig_language="und",
    )


def test_decision_profile_manager_saves_v1_without_paths(tmp_path):
    manager = DecisionProfileManager(tmp_path / "decision")
    path = manager.save(
        {
            "version": 1,
            "kind": "decision-profile",
            "name": "FR/VO",
            "sources": [{"path": "/tmp/in.mkv"}],
            "output": "/tmp/out.mkv",
            "rules": [{"id": "r1", "match": {"all": []}, "actions": []}],
        }
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["kind"] == "decision-profile"
    assert payload["version"] == 1
    assert "sources" not in payload
    assert "output" not in payload


def test_title_pattern_keywords_and_empty_cleanup():
    track = _audio(1)
    assert render_title_pattern("{lang_name} - {codec} - {channels} - {audio_object} - {flag_forced}", track) == "French - EAC3 - 5.1 - Atmos"


def test_codec_name_variable_keyword():
    track = _audio(1, codec="EAC3", display_info="5.1  640 kbps")
    profile = {
        "version": 1,
        "kind": "decision-profile",
        "name": "codec names",
        "variables": {"codec_names": {"EAC3": "DDP", "AC3": "Dolby Digital"}},
        "rules": [
            {
                "id": "rename",
                "match": {"all": [{"field": "codec_name", "op": "is", "value": "DDP", "required": True}]},
                "actions": [{"type": "set_title", "pattern": "{lang_name} {codec} {channels}"}],
            }
        ],
    }

    result = apply_decision_profile(profile, [track])

    assert result.report["valid"] is True
    assert track.title == "French DDP 5.1"


def test_tags_can_chain_rules():
    track = _audio(1, title="French")
    profile = {
        "version": 1,
        "kind": "decision-profile",
        "name": "chain",
        "rules": [
            {
                "id": "tag_vf",
                "priority": 20,
                "scope": "all",
                "match": {"all": [{"field": "language", "op": "is", "value": "fr-FR"}]},
                "actions": [{"type": "add_track_tags", "value": ["vf_main"]}],
            },
            {
                "id": "rename_vf",
                "priority": 10,
                "scope": "all",
                "match": {"all": [{"field": "track_tags", "op": "in", "value": ["vf_main"]}]},
                "actions": [{"type": "set_title", "pattern": "VF {codec} {channels}"}],
            },
        ],
    }

    result = apply_decision_profile(profile, [track])

    assert track.title == "VF EAC3 5.1"
    assert result.report["track_tags"][track.entry_id] == ["vf_main"]


def test_decision_keyword_fields_match_flags_and_atmos():
    target = _audio(1, display_info="7.1  4000 kbps  Atmos", codec="TRUEHD")
    target.flag_visual_impaired = True
    target.orig_flag_visual_impaired = True
    other = _audio(2, title="Commentary", display_info="5.1  640 kbps")
    profile = {
        "version": 1,
        "kind": "decision-profile",
        "name": "keyword fields",
        "rules": [
            {
                "id": "atmos_visual",
                "scope": "all",
                "match": {
                    "all": [
                        {"field": "codec_atmos", "op": "is", "value": True, "required": True},
                        {"field": "flag_visual_impaired", "op": "is", "value": True, "required": True},
                    ]
                },
                "actions": [{"type": "set_title", "value": "Atmos AD"}],
            }
        ],
    }

    apply_decision_profile(profile, [target, other])

    assert target.title == "Atmos AD"
    assert other.title == "Commentary"


def test_video_profile_selects_closest_video():
    hd = _video(0, "1920x1080  SDR")
    uhd = _video(1, "3840x2160  HDR10")
    profile = {
        "version": 1,
        "kind": "decision-profile",
        "name": "video",
        "rules": [
            {
                "id": "video_main",
                "scope": "best",
                "match": {
                    "all": [
                        {"field": "type", "op": "is", "value": "video", "required": True},
                        {"field": "resolution", "op": "is", "value": {"width": 3840, "height": 2160}, "required": False},
                        {"field": "video_flags_hex", "op": "is", "value": "0x00000038", "required": False},
                    ]
                },
                "actions": [{"type": "set_title", "value": "Main Video"}],
            }
        ],
    }

    result = apply_decision_profile(profile, [hd, uhd])

    assert not result.report["ambiguous_matches"]
    assert uhd.title == "Main Video"
    assert hd.title == ""


def test_video_profile_can_score_width_without_height():
    narrow = _video(0, "1920x1080  HDR10")
    wide = _video(1, "3840x1600  HDR10")
    profile = {
        "version": 1,
        "kind": "decision-profile",
        "name": "video width",
        "rules": [
            {
                "id": "video_width",
                "scope": "best",
                "match": {
                    "all": [
                        {"field": "type", "op": "is", "value": "video", "required": True},
                        {"field": "width", "op": "is", "value": 3840, "required": False},
                    ]
                },
                "actions": [{"type": "set_title", "value": "Wide"}],
            }
        ],
    }

    apply_decision_profile(profile, [narrow, wide])

    assert wide.title == "Wide"
    assert narrow.title == ""


def test_audio_profile_can_require_codec_and_prefer_atmos_with_fallback():
    plain = _audio(1, language="fr-FR", title="French EAC3", codec="EAC3", display_info="5.1  640 kbps")
    atmos = _audio(2, language="fr-FR", title="French Atmos", codec="EAC3", display_info="7.1  4000 kbps  Atmos")
    aac = _audio(3, language="fr-FR", title="French AAC", codec="AAC", display_info="5.1  512 kbps")
    profile = {
        "version": 1,
        "kind": "decision-profile",
        "name": "prefer atmos",
        "rules": [
            {
                "id": "fr_eac3_best",
                "scope": "best",
                "match": {
                    "all": [
                        {"field": "language", "op": "is", "value": "fr-FR", "required": True},
                        {"field": "codec", "op": "is", "value": "EAC3", "required": True},
                        {"field": "codec_atmos", "op": "is", "value": True, "required": False},
                    ]
                },
                "actions": [{"type": "set_title", "value": "Selected"}],
            }
        ],
    }

    apply_decision_profile(profile, [plain, atmos, aac])

    assert atmos.title == "Selected"
    assert plain.title == "French EAC3"
    assert aac.title == "French AAC"

    fallback = _audio(4, language="fr-FR", title="Fallback EAC3", codec="EAC3", display_info="5.1  640 kbps")
    apply_decision_profile(profile, [fallback, aac])

    assert fallback.title == "Selected"
    assert aac.title == "French AAC"


def test_build_video_flags_hex_from_editor_parts():
    value = int(
        build_video_flags_hex(width=3840, height=2160, hdr=True, hdr10plus=True, dolby_vision=True),
        16,
    )

    assert value & 0x00000008
    assert value & 0x00000010
    assert value & 0x00000040
    assert value & 0x00000080


def test_priority_resolves_field_conflicts():
    track = _audio(1)
    profile = {
        "version": 1,
        "kind": "decision-profile",
        "name": "priority",
        "rules": [
            {"id": "a", "priority": 20, "match": {"all": [{"field": "type", "op": "is", "value": "audio"}]}, "actions": [{"type": "set_title", "value": "A"}]},
            {"id": "b", "priority": 10, "match": {"all": [{"field": "type", "op": "is", "value": "audio"}]}, "actions": [{"type": "set_title", "value": "B"}]},
        ],
    }

    result = apply_decision_profile(profile, [track])

    assert result.report["conflicts"] == []
    assert result.report["skipped_writes"]
    assert result.report["valid"] is True
    assert track.title == "A"


def test_equal_priority_conflicts_are_reported():
    track = _audio(1)
    profile = {
        "version": 1,
        "kind": "decision-profile",
        "name": "conflict",
        "rules": [
            {"id": "a", "priority": 20, "match": {"all": [{"field": "type", "op": "is", "value": "audio"}]}, "actions": [{"type": "set_title", "value": "A"}]},
            {"id": "b", "priority": 20, "match": {"all": [{"field": "type", "op": "is", "value": "audio"}]}, "actions": [{"type": "set_title", "value": "B"}]},
        ],
    }

    result = apply_decision_profile(profile, [track])

    assert result.report["conflicts"]
    assert result.report["valid"] is False
    assert track.title == "A"


def test_rule_override_mode_can_replace_higher_priority_value():
    track = _audio(1)
    profile = {
        "version": 1,
        "kind": "decision-profile",
        "name": "override",
        "rules": [
            {"id": "a", "priority": 20, "match": {"all": [{"field": "type", "op": "is", "value": "audio"}]}, "actions": [{"type": "set_title", "value": "A"}]},
            {"id": "b", "priority": 10, "write_mode": "override", "match": {"all": [{"field": "type", "op": "is", "value": "audio"}]}, "actions": [{"type": "set_title", "value": "B"}]},
        ],
    }

    result = apply_decision_profile(profile, [track])

    assert result.report["conflicts"] == []
    assert result.report["resolved_writes"]
    assert track.title == "B"


def test_rule_add_mode_appends_title_fragment():
    track = _audio(1)
    profile = {
        "version": 1,
        "kind": "decision-profile",
        "name": "add",
        "rules": [
            {"id": "a", "priority": 20, "match": {"all": [{"field": "type", "op": "is", "value": "audio"}]}, "actions": [{"type": "set_title", "value": "A"}]},
            {"id": "b", "priority": 10, "write_mode": "add", "match": {"all": [{"field": "type", "op": "is", "value": "audio"}]}, "actions": [{"type": "set_title", "value": "B"}]},
        ],
    }

    result = apply_decision_profile(profile, [track])

    assert result.report["conflicts"] == []
    assert result.report["resolved_writes"]
    assert track.title == "A B"


def test_language_and_tags_can_render_keywords():
    track = _audio(1, language="fr-FR")
    track.flag_original = True
    track.orig_flag_original = True
    profile = {
        "version": 1,
        "kind": "decision-profile",
        "name": "language keywords",
        "rules": [
            {
                "id": "keep_language",
                "match": {"all": [{"field": "type", "op": "is", "value": "audio"}]},
                "actions": [
                    {"type": "set_language", "value": "{language}"},
                    {"type": "add_track_tags", "value": ["{flag_original}"]},
                ],
            }
        ],
    }

    result = apply_decision_profile(profile, [track])

    assert track.language == "fr-FR"
    assert result.report["track_tags"][track.entry_id] == ["Original"]


def test_audio_variant_not_duplicated_on_second_apply():
    track = _audio(1, codec="TRUEHD", display_info="5.1  4000 kbps")
    profile = {
        "version": 1,
        "kind": "decision-profile",
        "name": "variant",
        "rules": [
            {
                "id": "compat",
                "match": {"all": [{"field": "codec", "op": "is", "value": "TRUEHD"}]},
                "actions": [{"type": "create_audio_variant", "codec": "ac3", "bitrate_kbps": 640, "title_pattern": "VF AC3 {channels}"}],
            }
        ],
    }

    first = apply_decision_profile(profile, [track])
    second = apply_decision_profile(profile, first.tracks)

    assert len([item for item in first.tracks if item.is_new]) == 1
    assert len([item for item in second.tracks if item.is_new]) == 1


def test_capture_remux_config_to_decision_profile_v1_has_no_paths():
    video = _video(0, "3840x2160  Dolby Vision")
    audio = _audio(1)
    variant = clone_track_entry(audio)
    variant.codec = "AC3"
    variant.title = "VF AC3 5.1"
    config = RemuxConfig(
        sources=[SourceInput(path=Path("/tmp/source.mkv"), file_index=0, tracks=[video, audio, variant])],
        output=Path("/tmp/out.mkv"),
        track_order=[(0, 0, video.entry_id), (0, 1, audio.entry_id), (0, 1, variant.entry_id)],
    )

    profile = remux_config_to_decision_profile(config, name="capture")

    assert profile["version"] == 1
    assert profile["kind"] == "decision-profile"
    assert "sources" not in profile
    assert "output" not in profile
    assert "/tmp/source.mkv" not in json.dumps(profile)


def test_capture_reapplies_all_track_flags_including_mkv_enabled():
    source_audio = _audio(1)
    source_audio.flag_enabled = False
    source_audio.flag_default = True
    config = RemuxConfig(
        sources=[SourceInput(path=Path("/tmp/source.mkv"), file_index=0, tracks=[source_audio])],
        output=Path("/tmp/out.mkv"),
        track_order=[(0, 1, source_audio.entry_id)],
    )
    profile = remux_config_to_decision_profile(config, name="flags")
    target_audio = _audio(4)
    target_audio.flag_enabled = True
    target_audio.flag_default = False

    apply_decision_profile(profile, [target_audio])

    assert target_audio.flag_enabled is False
    assert target_audio.flag_default is True
