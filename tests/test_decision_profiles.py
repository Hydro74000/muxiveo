from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.profiles.decision import (
    DecisionProfileManager,
    DecisionProfileError,
    apply_decision_profile,
    build_video_flags_hex,
    DECISION_KEYWORDS,
    remux_config_to_decision_profile,
    render_title_pattern,
    validate_decision_profile,
)
from core.profiles.expressions import CriteriaExpressionError, compile_criteria_expression
from core.profiles.keywords import KEYWORD_CATEGORIES, lang_name, normalize_lang
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


def test_rules_and_title_keywords_use_current_flags_without_changing_source_identity():
    track = _audio(1)
    track.flag_visual_impaired = True  # Détecté ou édité depuis l'inspection.
    assert not track.orig_flag_visual_impaired
    profile = {"version": 1, "kind": "decision-profile", "name": "Flags courants", "rules": [{
        "id": "description", "priority": 10, "scope": "all", "match": {"all": [
            {"field": "flag_visual_impaired", "op": "is", "value": True},
        ]}, "actions": [{"type": "set_flags", "value": {"commentary": True}}],
    }, {
        "id": "rename", "scope": "all", "match": {"all": [
            {"field": "flag_commentary", "op": "is", "value": True},
        ]}, "actions": [{"type": "set_title", "pattern": "{flag_visual_impaired} {flag_commentary}"}],
    }]}
    apply_decision_profile(profile, [track])
    assert track.title == "Visual Impaired Commentary"
    assert not track.orig_flag_visual_impaired and not track.orig_flag_commentary


def test_profile_from_current_state_reads_edited_values_as_source_and_keeps_file_origins():
    """Valeurs éditées = source de travail ; origines du fichier intactes pour le mux."""
    track = _audio(1, language="fr-CA", title="VFQ")
    # Éditions du panneau Conteneur : la piste est en réalité une VFF.
    track.language, track.title = "fr-FR", "VFF"
    track.flag_hearing_impaired = True
    profile = {"version": 1, "kind": "decision-profile", "name": "Source de travail", "rules": [{
        "id": "vfq", "priority": 20, "scope": "all", "match": {"all": [
            {"field": "source_title", "op": "contains", "value": "VFQ"},
        ]}, "actions": [{"type": "set_language", "value": "fr-CA"}],
    }, {
        "id": "vff", "priority": 10, "scope": "all", "match": {"all": [
            {"field": "source_language", "op": "is", "value": "fr-FR"},
            {"field": "source_title", "op": "contains", "value": "VFF"},
        ]}, "actions": [{"type": "set_title", "pattern": "{lang_name} {flag_hearing_impaired}"}],
    }]}
    edited = copy.deepcopy(track)

    apply_decision_profile(profile, [edited], from_current_state=True)
    apply_decision_profile(profile, [track])

    assert (edited.language, edited.title) == ("fr-FR", "French Hearing Impaired")
    # Valeurs d'origine du fichier intactes : le mux voit toujours les modifications.
    origins = (edited.orig_language, edited.orig_title, edited.orig_flag_hearing_impaired)
    assert origins == ("fr-CA", "VFQ", False)
    assert edited.working_source is None
    # Sans état de travail, les critères « source » lisent le fichier.
    assert (track.language, track.title) == ("fr-CA", "VFF")


def test_profile_from_current_state_freezes_cleared_source_title():
    """Titre effacé : le titre source reste vide malgré les écritures des règles."""
    track = _audio(1, title="Original")
    track.title = ""  # Titre effacé dans le tableau.
    profile = {"version": 1, "kind": "decision-profile", "name": "Instantané", "rules": [{
        "id": "rename", "priority": 20, "scope": "all", "match": {"all": [
            {"field": "type", "op": "is", "value": "audio"},
        ]}, "actions": [{"type": "set_title", "value": "Auto"}],
    }, {
        "id": "drop_auto", "priority": 10, "scope": "all", "match": {"all": [
            {"field": "source_title", "op": "is", "value": "Auto"},
        ]}, "actions": [{"type": "set_enabled", "value": False}],
    }]}

    result = apply_decision_profile(profile, [track], from_current_state=True)

    # La source de travail reste le titre vide du tableau, pas le titre écrit par la règle.
    assert track.title == "Auto" and track.enabled
    assert [rule["rule_id"] for rule in result.report["missing_rules"]] == ["drop_auto"]


def test_lang_name_keeps_region_only_for_non_default_variants():
    assert lang_name("fr-FR") == "French"
    assert lang_name("fr-CA") == "French (Canada)"
    assert lang_name("pt-PT") == "Portuguese"
    assert lang_name("pt-BR") == "Portuguese (Brazil)"
    assert render_title_pattern("{lang_name}", _audio(1, language="fr-CA", title="VFQ")) == "French (Canada)"


def test_decision_keywords_include_registry_only_fields():
    assert "source_language" in DECISION_KEYWORDS
    assert "track_tags" in DECISION_KEYWORDS
    assert "none" in DECISION_KEYWORDS
    flattened = {keyword for _category, keywords in KEYWORD_CATEGORIES for keyword in keywords}
    assert "source_language" in flattened
    assert "track_tags" in flattened
    assert "none" in flattened


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


def test_render_aliases_can_be_global_or_keyword_scoped():
    track = _audio(1, codec="EAC3", display_info="5.1  640 kbps")
    variables = {
        "aliases": {
            "*": {"EAC3": "DDP", "French": "FR"},
            "lang_name": {"French": "Français"},
        }
    }

    assert render_title_pattern("{codec} {lang_name}", track, variables=variables) == "DDP Français"


def test_render_aliases_do_not_affect_matching():
    track = _audio(1, codec="EAC3", display_info="5.1  640 kbps")
    profile = {
        "version": 1,
        "kind": "decision-profile",
        "name": "aliases do not match",
        "variables": {"aliases": {"codec_name": {"EAC3": "DDP"}}},
        "rules": [
            {
                "id": "rename",
                "match": {"all": [{"field": "codec_name", "op": "is", "value": "DDP", "required": True}]},
                "actions": [{"type": "set_title", "pattern": "{codec}"}],
            }
        ],
    }

    result = apply_decision_profile(profile, [track])

    assert result.report["applied_rules"] == 0
    assert track.title == "VF"


def test_criteria_expression_parser_supports_quotes_and_escapes():
    condition = compile_criteria_expression(
        r'"A | B" | Dolby \& DTS | \(VFQ\)',
        lambda atom: {"field": "source_title", "op": "contains", "value": atom, "required": True},
    )

    assert [item["value"] for item in condition["any"]] == ["A | B", "Dolby & DTS", "(VFQ)"]

    with pytest.raises(CriteriaExpressionError):
        compile_criteria_expression(
            "VFQ \\",
            lambda atom: {"field": "source_title", "op": "contains", "value": atom},
        )


def test_criteria_expression_parser_supports_not_forms():
    condition = compile_criteria_expression(
        "!(VFQ | VFF) & NOT(Forced) & !{flag_commentary}",
        lambda atom: {"field": "source_title", "op": "contains", "value": atom, "required": True},
    )

    assert condition["all"][0]["not"]["any"][0]["value"] == "VFQ"
    assert condition["all"][0]["not"]["any"][1]["value"] == "VFF"
    assert condition["all"][1]["not"]["value"] == "Forced"
    assert condition["all"][2]["not"]["value"] == "{flag_commentary}"


def test_criteria_expression_parser_rejects_invalid_not_forms():
    for expression in ("!", "! foo", "NOT"):
        with pytest.raises(CriteriaExpressionError):
            compile_criteria_expression(
                expression,
                lambda atom: {"field": "source_title", "op": "contains", "value": atom},
            )


def test_decision_profile_expr_condition_matches_nested_title_values():
    vff = TrackEntry(5, "subtitle", "SUBRIP", "", "fr-FR", "VFF", file_id="src0", orig_language="fr-FR", orig_title="VFF")
    vfq_forced = TrackEntry(6, "subtitle", "SUBRIP", "", "fr-FR", "VFQ Forced", file_id="src0", orig_language="fr-FR", orig_title="VFQ Forced")
    vfq = TrackEntry(7, "subtitle", "SUBRIP", "", "fr-FR", "VFQ", file_id="src0", orig_language="fr-FR", orig_title="VFQ")
    profile = {
        "version": 1,
        "kind": "decision-profile",
        "name": "expr subtitles",
        "rules": [
            {
                "id": "vfq_forced",
                "scope": "all",
                "match": {
                    "all": [
                        {"field": "type", "op": "is", "value": "subtitle", "required": True},
                        {"field": "source_title", "op": "contains", "expr": "(VFQ | VFF) & Forced", "required": True},
                    ]
                },
                "actions": [{"type": "set_language", "value": "fr-CA"}],
            }
        ],
    }

    result = apply_decision_profile(profile, [vff, vfq_forced, vfq])

    assert result.report["applied_rules"] == 1
    assert vff.language == "fr-FR"
    assert vfq_forced.language == "fr-CA"
    assert vfq.language == "fr-FR"


def test_decision_profile_expr_condition_can_target_keyword_atoms():
    target = _audio(1, display_info="7.1  4000 kbps  Atmos", codec="TRUEHD")
    other = _audio(2, display_info="5.1  640 kbps", codec="EAC3")
    profile = {
        "version": 1,
        "kind": "decision-profile",
        "name": "expr keywords",
        "rules": [
            {
                "id": "immersive",
                "scope": "all",
                "match": {"all": [{"field": "type", "op": "is", "value": "audio"}, {"expr": "{atmos} | {dtsx}"}]},
                "actions": [{"type": "set_title", "value": "Immersive"}],
            }
        ],
    }

    result = apply_decision_profile(profile, [target, other])

    assert result.report["applied_rules"] == 1
    assert target.title == "Immersive"
    assert other.title == "VF"


def test_decision_profile_expr_condition_can_exclude_values_and_keywords():
    clean = _audio(1, title="Main")
    commentary = _audio(2, title="Commentary")
    commentary.orig_flag_commentary = True
    profile = {
        "version": 1,
        "kind": "decision-profile",
        "name": "not commentary",
        "rules": [
            {
                "id": "clean",
                "scope": "all",
                "match": {
                    "all": [
                        {"field": "source_title", "op": "contains", "expr": "!(Commentary | Descriptive)", "required": True},
                        {"expr": "!{flag_commentary}"},
                    ]
                },
                "actions": [{"type": "set_title", "value": "Clean"}],
            }
        ],
    }

    result = apply_decision_profile(profile, [clean, commentary])

    assert result.report["applied_rules"] == 1
    assert clean.title == "Clean"
    assert commentary.title == "Commentary"


def test_decision_profile_validation_rejects_expr_with_value():
    profile = {
        "version": 1,
        "kind": "decision-profile",
        "name": "invalid expr",
        "rules": [
            {
                "id": "bad",
                "match": {"field": "source_title", "op": "contains", "value": "VFQ", "expr": "VFF"},
                "actions": [],
            }
        ],
    }

    with pytest.raises(DecisionProfileError, match="value and expr"):
        validate_decision_profile(profile)


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


def test_remove_track_tags_runs_before_add_track_tags():
    track = _audio(1, title="French")
    profile = {
        "version": 1,
        "kind": "decision-profile",
        "name": "replace tags",
        "rules": [
            {
                "id": "tag_old",
                "priority": 20,
                "scope": "all",
                "match": {"all": [{"field": "language", "op": "is", "value": "fr-FR"}]},
                "actions": [{"type": "add_track_tags", "value": ["old"]}],
            },
            {
                "id": "replace",
                "priority": 10,
                "scope": "all",
                "match": {"all": [{"field": "track_tags", "op": "in", "value": ["old"]}]},
                "actions": [
                    {"type": "add_track_tags", "value": ["new"]},
                    {"type": "remove_track_tags", "value": ["old"]},
                ],
            },
        ],
    }

    result = apply_decision_profile(profile, [track])

    assert result.report["track_tags"][track.entry_id] == ["new"]


def test_none_keyword_can_match_tracks_without_flags():
    plain = _audio(1, title="Plain")
    default = _audio(2, title="Default")
    default.flag_default = True
    profile = {
        "version": 1,
        "kind": "decision-profile",
        "name": "no flags",
        "rules": [
            {
                "id": "plain",
                "scope": "all",
                "match": {"all": [{"field": "flags", "expr": "{none}", "required": True}]},
                "actions": [{"type": "set_title", "value": "No Flags"}],
            }
        ],
    }

    result = apply_decision_profile(profile, [plain, default])

    assert result.report["applied_rules"] == 1
    assert plain.title == "No Flags"
    assert default.title == "Default"


def test_all_scope_filters_tracks_on_optional_title_criteria():
    vff = TrackEntry(5, "subtitle", "SUBRIP", "", "fr-FR", "VFF", file_id="src0", orig_language="fr-FR", orig_title="VFF")
    vfq_forced = TrackEntry(6, "subtitle", "SUBRIP", "", "fr-FR", "VFQ Forced", file_id="src0", orig_language="fr-FR", orig_title="VFQ Forced")
    vfq = TrackEntry(7, "subtitle", "SUBRIP", "", "fr-FR", "VFQ", file_id="src0", orig_language="fr-FR", orig_title="VFQ")
    profile = {
        "version": 1,
        "kind": "decision-profile",
        "name": "vfq subtitles",
        "rules": [
            {
                "id": "vfq_lang",
                "scope": "all",
                "match": {
                    "all": [
                        {"field": "type", "op": "is", "value": "subtitle", "required": True},
                        {"field": "source_title", "op": "contains", "value": "VFQ", "required": False},
                    ]
                },
                "actions": [{"type": "set_language", "value": "fr-CA"}],
            }
        ],
    }

    result = apply_decision_profile(profile, [vff, vfq_forced, vfq])

    assert result.report["applied_rules"] == 2
    assert vff.language == "fr-FR"
    assert vfq_forced.language == "fr-CA"
    assert vfq.language == "fr-CA"


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


def test_set_flags_action_can_clear_existing_flag():
    track = _audio(1)
    track.flag_default = True
    profile = {
        "version": 1,
        "kind": "decision-profile",
        "name": "clear flag",
        "rules": [
            {
                "id": "clear_default",
                "scope": "all",
                "match": {"all": [{"field": "type", "op": "is", "value": "audio"}]},
                "actions": [{"type": "set_flags", "value": {"default": False}}],
            }
        ],
    }

    apply_decision_profile(profile, [track])

    assert track.flag_default is False


def test_order_priority_reorders_tracks_stably():
    video = _video(0, "1920x1080 SDR")
    audio_fr = _audio(1, language="fr-FR", title="French")
    audio_en = _audio(2, language="en-US", title="English")
    profile = {
        "version": 1,
        "kind": "decision-profile",
        "name": "order",
        "rules": [
            {
                "id": "audio_first",
                "scope": "all",
                "match": {"all": [{"field": "type", "op": "is", "value": "audio"}]},
                "actions": [{"type": "set_order_priority", "value": 1}],
            },
            {
                "id": "video_second",
                "scope": "all",
                "match": {"all": [{"field": "type", "op": "is", "value": "video"}]},
                "actions": [{"type": "set_order_priority", "value": 2}],
            },
        ],
    }

    result = apply_decision_profile(profile, [video, audio_fr, audio_en])

    assert [track.entry_id for track in result.tracks] == [audio_fr.entry_id, audio_en.entry_id, video.entry_id]
    assert result.report["order_changed"] is True


def test_order_priority_conflicts_use_rule_group_priority():
    video = _video(0, "1920x1080 SDR")
    audio = _audio(1, language="fr-FR", title="French")
    profile = {
        "version": 1,
        "kind": "decision-profile",
        "name": "order groups",
        "groups": [
            {"id": "late", "label": "Late", "enabled": True, "priority": 200},
            {"id": "early", "label": "Early", "enabled": True, "priority": 100},
            {"id": "video", "label": "Video", "enabled": True, "priority": 50},
        ],
        "rules": [
            {
                "id": "audio_late",
                "group_id": "late",
                "scope": "all",
                "match": {"all": [{"field": "type", "op": "is", "value": "audio"}]},
                "actions": [{"type": "set_order_priority", "value": 5}],
            },
            {
                "id": "audio_early",
                "group_id": "early",
                "scope": "all",
                "match": {"all": [{"field": "type", "op": "is", "value": "audio"}]},
                "actions": [{"type": "set_order_priority", "value": 1}],
            },
            {
                "id": "video_middle",
                "group_id": "video",
                "scope": "all",
                "match": {"all": [{"field": "type", "op": "is", "value": "video"}]},
                "actions": [{"type": "set_order_priority", "value": 2}],
            },
        ],
    }

    result = apply_decision_profile(profile, [audio, video])

    assert [track.entry_id for track in result.tracks] == [video.entry_id, audio.entry_id]
    assert result.report["conflicts"] == []
    assert result.report["skipped_writes"]


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
    before = _states(first.tracks)  # instantané avant la réapplication
    second = apply_decision_profile(profile, first.tracks)
    assert _states(second.tracks) == before

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


def test_saved_vfq_rule_still_applies_after_region_detection():
    """Règle « fr-FR + titre VFQ » écrite avant la détection de VFQ (fr-CA) : toujours appliquée."""
    from core.profiles.decision import apply_decision_profile
    profile = {
        "kind": "decision-profile", "version": 1, "name": "VFQ",
        "rules": [{
            "id": "vfq", "label": "VFQ", "scope": "all", "write_mode": "priority",
            "match": {"all": [
                {"field": "type", "op": "is", "value": "audio", "required": True},
                {"field": "language", "op": "is", "value": "fr-FR", "required": True},
                {"field": "source_title", "op": "contains", "value": "VFQ", "required": True},
            ]},
            "actions": [{"type": "set_title", "value": "Français (Québec)"}],
        }],
    }
    for language in ("fra", "fr-CA"):  # étiquette brute, ou déjà régionalisée par l'inspection
        vff = _audio(1, language="fr-FR" if language == "fr-CA" else language, title="VFF")
        vfq = _audio(2, language=language, title="VFQ")
        result = apply_decision_profile(profile, [vff, vfq])
        assert result.report["applied_rules"] == 1
        assert (vff.title, vfq.title) == ("VFF", "Français (Québec)")


def test_fr_ca_condition_targets_only_quebec_tracks():
    from core.profiles.decision import apply_decision_profile
    profile = {
        "kind": "decision-profile", "version": 1, "name": "QC",
        "rules": [{
            "id": "qc", "label": "QC", "scope": "all", "write_mode": "priority",
            "match": {"all": [{"field": "language", "op": "is", "value": "fr-CA", "required": True}]},
            "actions": [{"type": "set_title", "value": "Québec"}],
        }],
    }
    vff = _audio(1, language="fr-FR", title="VFF")
    vfq = _audio(2, language="fr-CA", title="VFQ")
    apply_decision_profile(profile, [vff, vfq])
    assert (vff.title, vfq.title) == ("VFF", "Québec")


def test_report_lists_fields_written_even_with_an_unchanged_value():
    track = _audio(1)
    track.time_shift_ms = 50
    profile = {"version": 1, "kind": "decision-profile", "name": "Décalage", "rules": [{
        "id": "r1", "match": {"all": []}, "actions": [{"type": "set_time_shift_ms", "value": 50}],
    }]}
    result = apply_decision_profile(profile, [track])
    assert result.report["written_fields"] == {track.entry_id: ["time_shift_ms"]}
    assert result.tracks[0].time_shift_ms == 50


# --- Audit 18 : capture → application des effacements et des drapeaux (originaux et variantes) -------------------


def _capture(*tracks: TrackEntry) -> dict:
    config = RemuxConfig(
        sources=[SourceInput(path=Path("/tmp/source.mkv"), file_index=0, tracks=list(tracks))],
        output=Path("/tmp/out.mkv"),
        track_order=[(0, track.mkv_tid, track.entry_id) for track in tracks],
    )
    return remux_config_to_decision_profile(config, name="capture")


def _fresh_audio() -> TrackEntry:
    """Piste telle qu'une nouvelle inspection la donne (valeurs physiques)."""
    return _audio(1, language="fr-FR", title="Audio original", codec="DTS", display_info="5.1  768 kbps")


@pytest.mark.parametrize("cleared", [("title",), ("language",), ("title", "language")])
def test_capture_replays_cleared_title_and_language_of_an_original(cleared):
    track = _fresh_audio()
    if "title" in cleared:
        track.title = ""
    if "language" in cleared:
        track.language = ""
    profile = _capture(track)
    actions = profile["rules"][0]["actions"]
    target = _fresh_audio()

    apply_decision_profile(profile, [target])

    assert ({"type": "set_title", "value": ""} in actions) is ("title" in cleared)
    assert ({"type": "set_language", "value": ""} in actions) is ("language" in cleared)
    assert target.title == ("" if "title" in cleared else "Audio original")
    assert target.language == ("" if "language" in cleared else "fr-FR")


def test_capture_never_clears_a_value_that_was_never_set():
    track = _audio(1, language="", title="")
    actions = _capture(track)["rules"][0]["actions"]
    assert not [action for action in actions if action["type"] in {"set_title", "set_language"}]


@pytest.mark.parametrize("cleared", [("title",), ("language",), ("title", "language")])
def test_capture_replays_cleared_title_and_language_of_an_audio_variant(cleared):
    source = _fresh_audio()
    variant = clone_track_entry(source)
    variant.codec = "AC3"
    if "title" in cleared:
        variant.title = ""
    if "language" in cleared:
        variant.language = ""
    profile = _capture(source, variant)
    action = profile["rules"][-1]["actions"][0]
    target = _fresh_audio()

    result = apply_decision_profile(profile, [target])

    assert action["clear"] == [name for name in ("title", "language") if name in cleared]
    created = [track for track in result.tracks if track.is_new]
    assert len(created) == 1
    assert created[0].title == ("" if "title" in cleared else "Audio original")
    assert created[0].language == ("" if "language" in cleared else "fr-FR")
    assert (target.title, target.language) == ("Audio original", "fr-FR")  # original intact


def test_legacy_variant_action_with_empty_values_still_inherits_the_source():
    """Profils antérieurs : ``title`` / ``language`` vides sans ``clear`` → valeurs de la source (inchangé)."""
    profile = {"version": 1, "kind": "decision-profile", "name": "ancien", "rules": [{
        "id": "v", "match": {"all": [{"field": "type", "op": "is", "value": "audio"}]},
        "actions": [{"type": "create_audio_variant", "codec": "ac3", "title": "", "language": ""}],
    }]}
    result = apply_decision_profile(profile, [_fresh_audio()])
    created = [track for track in result.tracks if track.is_new]
    assert [(track.title, track.language) for track in created] == [("Audio original", "fr-FR")]


def test_title_pattern_wins_over_a_clear_request():
    profile = {"version": 1, "kind": "decision-profile", "name": "motif", "rules": [{
        "id": "v", "match": {"all": [{"field": "type", "op": "is", "value": "audio"}]},
        "actions": [{"type": "create_audio_variant", "codec": "ac3", "title_pattern": "VF AC3 {channels}",
                     "clear": ["title"]}],
    }]}
    result = apply_decision_profile(profile, [_fresh_audio()])
    assert [track.title for track in result.tracks if track.is_new] == ["VF AC3 5.1"]


@pytest.mark.parametrize("named_first", [True, False])
def test_named_and_cleared_variants_never_merge_and_stay_stable(named_first):
    named = {"type": "create_audio_variant", "codec": "ac3", "title": "VF AC3", "language": "fr-FR"}
    cleared = {"type": "create_audio_variant", "codec": "ac3", "title": "", "language": "",
               "clear": ["title", "language"]}
    actions = [named, cleared] if named_first else [cleared, named]
    profile = {"version": 1, "kind": "decision-profile", "name": "deux", "rules": [{
        "id": "v", "match": {"all": [{"field": "type", "op": "is", "value": "audio"}]}, "actions": actions,
    }]}
    first = apply_decision_profile(profile, [_fresh_audio()])
    before = _states(first.tracks)  # instantané avant la réapplication
    second = apply_decision_profile(profile, first.tracks)
    assert _states(second.tracks) == before
    for result in (first, second):
        variants = sorted((track.title, track.language) for track in result.tracks if track.is_new)
        assert variants == [("", ""), ("VF AC3", "fr-FR")]


@pytest.mark.parametrize("source_flag, variant_flag", [(True, False), (False, True)])
def test_capture_reapplies_the_mkv_enabled_flag_of_an_audio_variant(source_flag, variant_flag):
    source = _fresh_audio()
    source.flag_enabled = source.orig_flag_enabled = source_flag
    variant = clone_track_entry(source)
    variant.codec = "AC3"
    variant.flag_enabled = variant_flag
    variant.flag_commentary = True
    profile = _capture(source, variant)
    action = profile["rules"][-1]["actions"][0]
    assert action["enabled"] is True and action["flags"]["enabled"] is variant_flag
    target = _fresh_audio()
    target.flag_enabled = target.orig_flag_enabled = source_flag

    first = apply_decision_profile(profile, [target])
    before = _states(first.tracks)  # instantané avant la réapplication
    second = apply_decision_profile(profile, first.tracks)  # variante reprise
    assert _states(second.tracks) == before

    for result in (first, second):
        created = [track for track in result.tracks if track.is_new]
        assert len(created) == 1
        assert created[0].flag_enabled is variant_flag and created[0].enabled  # drapeau ≠ sélection
        assert created[0].flag_commentary is True
    assert target.flag_enabled is source_flag  # original intact


def test_variant_without_mkv_enabled_flag_inherits_it_from_the_source():
    profile = {"version": 1, "kind": "decision-profile", "name": "v", "rules": [{
        "id": "v", "match": {"all": [{"field": "type", "op": "is", "value": "audio"}]},
        "actions": [{"type": "create_audio_variant", "codec": "ac3", "flags": {"default": False}}],
    }]}
    source = _fresh_audio()
    source.flag_enabled = False
    result = apply_decision_profile(profile, [source])
    assert [track.flag_enabled for track in result.tracks if track.is_new] == [False]


def test_decision_profile_schema_accepts_the_clear_intent():
    from core.jobs.schema import build_decision_profile_schema_v1

    def action_schemas(node):
        if isinstance(node, dict):
            if "title_pattern" in node.get("properties", {}):
                yield node
            for value in node.values():
                yield from action_schemas(value)
        elif isinstance(node, list):
            for value in node:
                yield from action_schemas(value)

    schemas = list(action_schemas(build_decision_profile_schema_v1()))
    assert schemas and all(schema["properties"]["clear"]["items"]["enum"] == ["title", "language"]
                           for schema in schemas)
    source = _fresh_audio()
    variant = clone_track_entry(source)
    variant.title = ""
    validate_decision_profile(_capture(source, variant))


# --- Audit 19 : ancrage des règles capturées, clear d'après l'héritage, affectation des variantes ------------------


def _states(tracks: list[TrackEntry]) -> list[tuple]:
    """Instantané indépendant : identifiant, filiation, codec, débit, métadonnées, sélection, tous les drapeaux."""
    from core.profiles.selectors import FLAG_NAMES

    return [
        (track.entry_id, track.source_entry_id, track.is_new, track.codec, track.display_info, track.title,
         track.language, track.enabled, *(getattr(track, f"flag_{name}") for name in FLAG_NAMES),
         track.time_shift_ms, track.file_language, track.file_title, track.orig_language, track.orig_title)
        for track in tracks
    ]


def _clean_report(result) -> None:
    assert result.report["missing_rules"] == [] and result.report["ambiguous_matches"] == []
    assert result.report["conflicts"] == []


def _edited_table(original_edit) -> list[TrackEntry]:
    original = _fresh_audio()
    original_edit(original)
    variant = clone_track_entry(original)
    variant.codec = "AC3"
    variant.title, variant.language = "", ""
    variant.flag_enabled = False
    return [original, variant]


@pytest.mark.parametrize("edit", [
    lambda t: (setattr(t, "title", ""), setattr(t, "language", "")),
    lambda t: setattr(t, "language", "en-US"),
], ids=["cleared", "changed"])
def test_capture_with_original_and_variant_reapplies_to_fresh_and_edited_tables(edit):
    table = _edited_table(edit)
    profile = _capture(*table)
    assert all(item["field"] != "language" for rule in profile["rules"] for item in rule["match"]["all"])

    fresh = apply_decision_profile(profile, [_fresh_audio()], from_current_state=True)
    _clean_report(fresh)
    expected = [(table[0].title, table[0].language), ("", "")]
    assert [(t.title, t.language) for t in fresh.tracks] == expected
    assert [t.flag_enabled for t in fresh.tracks] == [True, False] and all(t.enabled for t in fresh.tracks)

    before = _states(fresh.tracks)  # instantané pris avant la deuxième application (mêmes objets)
    again = apply_decision_profile(profile, fresh.tracks, from_current_state=True)  # deuxième application (panneau)
    _clean_report(again)
    assert _states(again.tracks) == before

    on_table = apply_decision_profile(profile, table, from_current_state=True)  # tableau déjà édité
    _clean_report(on_table)
    assert len(on_table.tracks) == 2 and [(t.title, t.language) for t in on_table.tracks] == expected


def test_capture_of_a_language_set_on_a_track_without_one_still_matches_the_file():
    track = _audio(1, language="", title="")
    track.language = "fr-FR"
    profile = _capture(track)
    criterion = next(item for item in profile["rules"][0]["match"]["all"] if item["field"] == "file_language")
    assert criterion["op"] == "missing"
    fresh = apply_decision_profile(profile, [_audio(1, language="", title="")], from_current_state=True)
    _clean_report(fresh)
    assert fresh.tracks[0].language == "fr-FR"


def test_file_language_is_stable_when_a_title_changes_the_region():
    track = _audio(1, language="fra", title="")
    track.title = "VFQ"  # régionaliserait fr-CA si la langue de travail suivait le titre
    profile = _capture(track)
    fresh = apply_decision_profile(profile, [_audio(1, language="fra", title="")], from_current_state=True)
    before = _states(fresh.tracks)  # instantané avant la réapplication
    again = apply_decision_profile(profile, fresh.tracks, from_current_state=True)
    assert _states(again.tracks) == before
    for result in (fresh, again):
        _clean_report(result)
        assert result.tracks[0].title == "VFQ"


def test_capture_of_two_originals_with_swapped_languages_is_stable():
    def fresh_pair():
        return [_audio(1, language="fr-FR", title="A"), _audio(2, language="en-US", title="B")]

    table = fresh_pair()
    table[0].language, table[1].language = "en-US", "fr-FR"
    profile = _capture(*table)
    first = apply_decision_profile(profile, fresh_pair(), from_current_state=True)
    before = _states(first.tracks)  # instantané avant la réapplication
    second = apply_decision_profile(profile, first.tracks, from_current_state=True)
    assert _states(second.tracks) == before
    for result in (first, second):
        _clean_report(result)
        assert [(t.title, t.language) for t in result.tracks] == [("A", "en-US"), ("B", "fr-FR")]


@pytest.mark.parametrize("include_metadata", [True, False])
@pytest.mark.parametrize("field", ["title", "language"])
def test_variant_clear_follows_what_the_clone_would_inherit(include_metadata, field):
    """Source physiquement vide, valeur ajoutée à l'original, variante vidée : ``clear`` présent."""
    def fresh():
        return _audio(1, language="", title="")

    original = fresh()
    setattr(original, field, "Titre ajouté" if field == "title" else "fr-FR")
    variant = clone_track_entry(original)
    variant.codec = "AC3"
    setattr(variant, field, "")
    config = RemuxConfig(
        sources=[SourceInput(path=Path("/tmp/s.mkv"), file_index=0, tracks=[original, variant])],
        output=Path("/tmp/o.mkv"),
        track_order=[(0, 1, original.entry_id), (0, 1, variant.entry_id)],
    )
    profile = remux_config_to_decision_profile(config, name="c", include_metadata=include_metadata)
    action = profile["rules"][-1]["actions"][0]
    assert action["clear"] == [field]
    first = apply_decision_profile(profile, [fresh()], from_current_state=True)
    before = _states(first.tracks)  # instantané avant la réapplication
    second = apply_decision_profile(profile, first.tracks, from_current_state=True)
    assert _states(second.tracks) == before
    for result in (first, second):
        _clean_report(result)
        created = [t for t in result.tracks if t.is_new]
        assert len(created) == 1 and getattr(created[0], field) == ""
        expected = (getattr(original, field) if include_metadata else "")
        assert getattr(result.tracks[0], field) == expected


def test_no_clear_when_neither_the_original_nor_the_file_carries_a_value():
    original = _audio(1, language="fr-FR", title="")
    variant = clone_track_entry(original)
    variant.codec = "AC3"
    assert "clear" not in _capture(original, variant)["rules"][-1]["actions"][0]


_INHERIT = {"type": "create_audio_variant", "codec": "ac3"}
_CLEAR_TITLE = {"type": "create_audio_variant", "codec": "ac3", "clear": ["title"]}


def _variant_profile(*actions: dict) -> dict:
    return {"version": 1, "kind": "decision-profile", "name": "variantes", "rules": [{
        "id": "v", "match": {"all": [{"field": "type", "op": "is", "value": "audio"}]}, "actions": list(actions),
    }]}


@pytest.mark.parametrize("order", ["inherit_first", "clear_first"])
@pytest.mark.parametrize("preexisting", [False, True])
def test_inherited_and_cleared_variants_are_assigned_whatever_the_order(order, preexisting):
    actions = [_INHERIT, _CLEAR_TITLE] if order == "inherit_first" else [_CLEAR_TITLE, _INHERIT]
    source = _fresh_audio()
    tracks = [source]
    if preexisting:  # variante déjà effacée dans le tableau
        cleared = clone_track_entry(source)
        cleared.codec, cleared.title = "AC3", ""
        tracks.append(cleared)
    first = apply_decision_profile(_variant_profile(*actions), tracks)
    before = _states(first.tracks)
    second = apply_decision_profile(_variant_profile(*actions), first.tracks)
    for result in (first, second):
        _clean_report(result)
        assert sorted(t.title for t in result.tracks if t.is_new) == ["", "Audio original"]
    assert _states(second.tracks) == before  # mêmes variantes reprises, rien de recréé ni modifié
    if preexisting:
        assert next(t for t in second.tracks if t.is_new and not t.title).entry_id == cleared.entry_id


def test_legacy_profile_reuses_a_variant_renamed_or_cleared_in_the_table():
    """Profil sans ``clear`` : variante renommée ou vidée à la main reprise (les règles ne visent jamais les
    variantes), aucun doublon."""
    profile = _variant_profile(_INHERIT)
    for edited_title in ("Renommée à la main", ""):
        first = apply_decision_profile(profile, [_fresh_audio()])
        variant = next(t for t in first.tracks if t.is_new)
        variant.title = edited_title
        before = _states(first.tracks)  # instantané avant la réapplication
        again = apply_decision_profile(profile, first.tracks)
        assert _states(again.tracks) == before
        assert [t.entry_id for t in again.tracks] == [t.entry_id for t in first.tracks]
        assert next(t for t in again.tracks if t.is_new).title == edited_title


def test_language_clear_and_inherit_never_merge():
    clear_language = {"type": "create_audio_variant", "codec": "ac3", "clear": ["language"]}
    for actions in ([_INHERIT, clear_language], [clear_language, _INHERIT]):
        first = apply_decision_profile(_variant_profile(*actions), [_fresh_audio()])
        before = _states(first.tracks)  # instantané avant la réapplication
        second = apply_decision_profile(_variant_profile(*actions), first.tracks)
        assert _states(second.tracks) == before
        for result in (first, second):
            assert sorted(t.language for t in result.tracks if t.is_new) == ["", "fr-FR"]


# --- Audit 20 : demandes de variantes affectées après les règles, file_title, provenance physique des clones -------


def _rule(rule_id: str, *actions: dict, **extra) -> dict:
    return {"id": rule_id, "scope": "all", "match": {"all": [{"field": "type", "op": "is", "value": "audio"}]},
            "actions": list(actions), **extra}


def _preexisting_cleared(source: TrackEntry) -> TrackEntry:
    cleared = clone_track_entry(source)
    cleared.codec, cleared.title = "AC3", ""
    return cleared


@pytest.mark.parametrize("inapplicable", ["disabled_rule", "disabled_group", "other_codec", "other_source"])
def test_inapplicable_clear_request_never_reserves_the_cleared_variant(inapplicable):
    source = _fresh_audio()
    other = _audio(2, language="en-US", title="English", codec="DTS")
    cleared = _preexisting_cleared(source)
    clear_action = {"type": "create_audio_variant", "codec": "eac3" if inapplicable == "other_codec" else "ac3",
                    "clear": ["title"]}
    clear_rule = _rule("vider", clear_action)
    if inapplicable == "disabled_rule":
        clear_rule["enabled"] = False
    if inapplicable == "disabled_group":
        clear_rule["group_id"] = "off"
    if inapplicable == "other_source":
        clear_rule["match"] = {"all": [{"field": "language", "op": "is", "value": "en-US"}]}
    profile = {"version": 1, "kind": "decision-profile", "name": "p",
               "groups": [{"id": "off", "enabled": False}],
               "rules": [_rule("heriter", _INHERIT, priority=10), clear_rule]}
    result = apply_decision_profile(profile, [source, other, cleared])
    ac3_of_source = [t for t in result.tracks if t.is_new and t.source_entry_id == source.entry_id
                     and t.codec == "AC3"]
    assert [t.entry_id for t in ac3_of_source] == [cleared.entry_id]  # variante vide reprise, aucun doublon


def _legacy_assignment(actions: list[dict], tracks: list[TrackEntry]) -> list[tuple]:
    """Oracle : ``_apply_audio_variant_action`` / ``_find_existing_variant`` de 6cc89f0 recopiés (motifs de titre et
    de langue, codec cible, débit, écritures immédiates) pour une règle « audio, all » sans autre action ; seule
    différence voulue : ``flags.enabled`` appliqué. États structurels complets (identifiants des variantes créées
    remplacés par leur rang de création)."""
    tracks = copy.deepcopy(tracks)
    created: list[str] = []
    for source in [t for t in tracks if not t.is_new and t.track_type == "audio"]:
        for action in actions:
            codec = str(action.get("codec") or action.get("target_codec") or "copy").strip()
            title_pattern = str(action.get("title_pattern") or action.get("pattern") or "")
            title = (render_title_pattern(title_pattern, source, temporary_tags=set())
                     if title_pattern else str(action.get("title") or ""))
            raw_language = str(action.get("language") or source.language)
            if "{" in raw_language and "}" in raw_language:
                raw_language = render_title_pattern(raw_language, source, temporary_tags=set())
            language = normalize_lang(raw_language, title)
            target = source.codec if not codec or codec.lower() == "copy" else codec.upper()
            variant = next((t for t in tracks if t.is_new and t.source_entry_id == source.entry_id
                            and (not target or str(t.codec or "").upper() == target.upper())
                            and (not title or t.title == title)
                            and (not language or normalize_lang(t.language, t.title) == language)), None)
            if variant is None:
                variant = clone_track_entry(source)
                tracks.append(variant)
                created.append(variant.entry_id)
            if codec and codec.lower() != "copy":
                variant.codec = codec.upper()
                bitrate = int(action.get("bitrate_kbps") or 0)
                parts = [part.strip() for part in str(source.orig_display_info or source.display_info or "")
                         .replace("·", "  ").split("  ") if part.strip() and "kbps" not in part.lower()]
                if bitrate > 0:
                    parts.append(f"{bitrate} kbps")
                variant.display_info = "  ".join(parts)
            if title:
                variant.title = title
            if language:
                variant.language = language
            variant.enabled = bool(action.get("enabled", True))
            for name, value in (action.get("flags") or {}).items():
                setattr(variant, f"flag_{name}", bool(value))
    return _structural(tracks, created)


def _structural(tracks: list[TrackEntry], created: list[str]) -> list[tuple]:
    rank = {entry_id: f"new{index}" for index, entry_id in enumerate(created)}
    return [(rank.get(state[0], state[0]), *state[1:]) for state in _states(tracks)]


def _engine(actions: list[dict], tracks: list[TrackEntry]) -> tuple[list[tuple], dict]:
    before = {t.entry_id for t in tracks}
    result = apply_decision_profile(_variant_profile(*actions), copy.deepcopy(tracks))
    created = [t.entry_id for t in result.tracks if t.entry_id not in before]
    return _structural(result.tracks, created), result.report


def _copie(source: TrackEntry, language: str) -> TrackEntry:
    variant = clone_track_entry(source)
    variant.codec, variant.title, variant.language = "AC3", "Copie", language
    return variant


_HISTORICAL_CASES = {
    "copie-fr-en-sans-langue": (
        lambda: (lambda s: [s, _copie(s, "fr-FR"), _copie(s, "en-US")])(_audio(1, language="", title="")),
        [{"type": "create_audio_variant", "codec": "ac3", "title": "Copie"},
         {"type": "create_audio_variant", "codec": "ac3", "title": "Copie", "language": "fr-FR"}]),
    "heritage-et-titre-source": (
        lambda: [_audio(1, title="VF")],
        [_INHERIT, {"type": "create_audio_variant", "codec": "ac3", "title": "VF"}]),
    "nommee-et-heritage": (
        lambda: [_fresh_audio()],
        [{"type": "create_audio_variant", "codec": "ac3", "title": "VF AC3"}, _INHERIT]),
    "champs-vides-historiques": (
        lambda: [_fresh_audio()],
        [{"type": "create_audio_variant", "codec": "ac3", "title": "", "language": "", "enabled": False,
          "flags": {"commentary": True}}, {"type": "create_audio_variant", "codec": "eac3", "bitrate_kbps": 640}]),
    "motifs-copy-debits": (
        lambda: [_fresh_audio()],
        [{"type": "create_audio_variant", "codec": "ac3", "title_pattern": "{lang_name} AC3 {channels}",
          "bitrate_kbps": 448},
         {"type": "create_audio_variant", "codec": "ac3", "title_pattern": "{lang_name} AC3 {channels}",
          "bitrate_kbps": 640, "flags": {"default": True}},
         {"type": "create_audio_variant", "codec": "copy", "language": "{language}"}]),
    "copy-codec-source-vide": (
        lambda: (lambda s: [s, _copie(s, "fr-FR")])((lambda t: (setattr(t, "codec", ""), t)[1])(_fresh_audio())),
        [{"type": "create_audio_variant", "codec": "copy", "title": "Copie"}]),
    "variante-renommee": (
        lambda: (lambda s: [s, (lambda v: (setattr(v, "codec", "AC3"), setattr(v, "title", "Main"), v)[2])(
            clone_track_entry(s))])(_fresh_audio()),
        [_INHERIT, {"type": "create_audio_variant", "codec": "ac3", "flags": {"enabled": False}}]),
}


@pytest.mark.parametrize("case", sorted(_HISTORICAL_CASES))
@pytest.mark.parametrize("reverse", [False, True])
def test_profiles_without_clear_keep_the_historical_assignment(case, reverse):
    """Audit 21 : sans effacement, affectation identique à l'historique dans les deux ordres (réutilisation par
    plusieurs demandes comprise : variante « Copie » française reprise deux fois, anglaise intacte)."""
    make_tracks, actions = _HISTORICAL_CASES[case]
    actions = list(reversed(actions)) if reverse else actions
    tracks = make_tracks()
    engine, report = _engine(actions, tracks)
    assert engine == _legacy_assignment(actions, tracks)
    _clean_report(SimpleNamespace(report=report))
    if case == "copie-fr-en-sans-langue":
        assert report["created_variants"] == [] and len(engine) == 3
    if case == "heritage-et-titre-source":
        assert len([state for state in engine if state[2]]) == 1  # une seule AC3 « VF »


@pytest.mark.parametrize("reverse", [False, True])
def test_clear_requests_are_matched_without_greed_and_never_merge_with_others(reverse):
    source = _audio(1, language="", title="Audio")
    empty_fr, empty_en = _preexisting_cleared(source), _preexisting_cleared(source)
    empty_fr.language, empty_en.language = "fr-FR", "en-US"
    requests = [{"type": "create_audio_variant", "codec": "ac3", "clear": ["title"]},
                {"type": "create_audio_variant", "codec": "ac3", "clear": ["title"], "language": "fr-FR"}]
    requests = list(reversed(requests)) if reverse else requests
    result = apply_decision_profile(_variant_profile(*requests, _INHERIT), [source, empty_fr, empty_en])
    _clean_report(result)
    variants = [t for t in result.tracks if t.is_new]
    assert [(t.entry_id, t.title, t.language) for t in variants[:2]] == [
        (empty_fr.entry_id, "", "fr-FR"), (empty_en.entry_id, "", "en-US")]  # aucune création pour les effacements
    assert [(t.title, t.is_new) for t in variants[2:]] == [("Audio", True)]  # l'héritage ne les reprend pas


@pytest.mark.parametrize("order", [(0, 1, 2), (2, 1, 0), (1, 0, 2), (2, 0, 1)])
def test_cleared_variant_is_isolated_whatever_the_order(order):
    """La variante effacée est toujours à part ; nommée et héritée suivent l'affectation historique (l'héritage
    reprend toute variante du codec déjà présente : ordre historique)."""
    named = {"type": "create_audio_variant", "codec": "ac3", "title": "VF AC3"}
    requests = [named, _INHERIT, _CLEAR_TITLE]
    actions = [requests[index] for index in order]
    source = _fresh_audio()
    first = apply_decision_profile(_variant_profile(*actions), copy.deepcopy([source]))
    before = _states(first.tracks)
    second = apply_decision_profile(_variant_profile(*actions), first.tracks)
    _clean_report(first)
    _clean_report(second)
    cleared = [t for t in first.tracks if t.is_new and not t.title]
    assert len(cleared) == 1
    others = [t for t in first.tracks if t not in cleared]
    engine = _structural(others, [t.entry_id for t in others if t.is_new])
    assert engine == _legacy_assignment([a for a in actions if a is not _CLEAR_TITLE], [source])
    assert _states(second.tracks) == before


@pytest.mark.parametrize("reverse", [False, True])
def test_clear_targets_keep_their_variant_and_writes_on_a_second_panel_application(reverse):
    """Audit 22 : deux effacements aux titre / langue identiques en sortie, débits et drapeaux différents → la
    deuxième application (panneau) retrouve chacun sa variante, sans échange de propriétés."""
    requests = [
        {"type": "create_audio_variant", "codec": "ac3", "clear": ["title", "language"], "bitrate_kbps": 640,
         "flags": {"default": True}},
        {"type": "create_audio_variant", "codec": "ac3", "clear": ["title"], "bitrate_kbps": 448,
         "flags": {"default": False}},
    ]
    requests = list(reversed(requests)) if reverse else requests
    profile = _variant_profile(*requests)
    first = apply_decision_profile(profile, [_audio(1, language="", title="Audio", codec="DTS")],
                                   from_current_state=True)
    _clean_report(first)
    assert sorted((t.display_info.split("  ")[-1], t.flag_default) for t in first.tracks if t.is_new) == [
        ("448 kbps", False), ("640 kbps", True)]
    before = _states(first.tracks)
    for _ in range(2):
        again = apply_decision_profile(profile, first.tracks, from_current_state=True)
        _clean_report(again)
        assert _states(again.tracks) == before and again.report["created_variants"] == []


@pytest.mark.parametrize("panel", [False, True])
def test_mixed_clear_and_historical_requests_are_stable_on_reapplication(panel):
    """Audit 23 : effacements et demande historique sur des variantes vides préexistantes → chaque demande retrouve
    sa variante à chaque réapplication (aucun échange de débit, aucune création)."""
    source = _audio(1, language="", title="", codec="DTS")
    empty_fr, empty_none = _preexisting_cleared(source), _preexisting_cleared(source)
    empty_fr.language = "fr-FR"
    profile = _variant_profile(
        {"type": "create_audio_variant", "codec": "ac3", "clear": ["title"], "bitrate_kbps": 448},
        {"type": "create_audio_variant", "codec": "ac3", "language": "fr-FR", "bitrate_kbps": 512},
        {"type": "create_audio_variant", "codec": "ac3", "clear": ["title"], "language": "fr-FR", "bitrate_kbps": 576},
    )
    first = apply_decision_profile(profile, [source, empty_fr, empty_none], from_current_state=panel)
    _clean_report(first)
    before = _states(first.tracks)
    for _ in range(3):
        again = apply_decision_profile(profile, first.tracks, from_current_state=panel)
        _clean_report(again)
        assert _states(again.tracks) == before and again.report["created_variants"] == []


def test_clear_targets_of_different_codec_groups_never_share_a_reserved_variant():
    """Audit 24 : effacement ``copy`` (codec source vide : joker) puis effacement AC3 → deux variantes distinctes,
    chacune avec sa sélection."""
    source = _audio(1, language="", title="", codec="")
    existing = clone_track_entry(source)
    existing.codec = "AC3"
    profile = _variant_profile(
        {"type": "create_audio_variant", "codec": "copy", "clear": ["title"], "enabled": False},
        {"type": "create_audio_variant", "codec": "ac3", "clear": ["language"], "enabled": True},
    )
    result = apply_decision_profile(profile, [source, existing])
    _clean_report(result)
    variants = [t for t in result.tracks if t.is_new]
    assert len(variants) == 2 and len({t.entry_id for t in variants}) == 2
    assert sorted(t.enabled for t in variants) == [False, True]
    ids = [entry["entry_id"] for entry in result.report["created_variants"] + result.report["reused_variants"]]
    assert len(ids) == len(set(ids)) == 2


def test_clear_title_and_clear_language_targets_are_distinct():
    title_only = {"type": "create_audio_variant", "codec": "ac3", "clear": ["title"]}
    language_only = {"type": "create_audio_variant", "codec": "ac3", "clear": ["language"]}
    both = {"type": "create_audio_variant", "codec": "ac3", "clear": ["title", "language"]}
    first = apply_decision_profile(_variant_profile(title_only, language_only, both), [_fresh_audio()])
    before = _states(first.tracks)  # instantané avant la réapplication
    second = apply_decision_profile(_variant_profile(both, language_only, title_only), first.tracks)
    assert _states(second.tracks) == before
    for result in (first, second):
        _clean_report(result)
        assert sorted((t.title, t.language) for t in result.tracks if t.is_new) == [
            ("", ""), ("", "fr-FR"), ("Audio original", "")]
    assert len(second.report["created_variants"]) == 0


def test_source_metadata_changed_between_two_requests():
    profile = _variant_profile({"type": "create_audio_variant", "codec": "ac3", "clear": ["title"]},
                               {"type": "set_language", "value": "en-US"},
                               {"type": "create_audio_variant", "codec": "ac3", "clear": ["title"]})
    result = apply_decision_profile(profile, [_fresh_audio()])
    _clean_report(result)
    assert sorted((t.title, t.language) for t in result.tracks if t.is_new) == [("", "en-US"), ("", "fr-FR")]


def test_clone_keeps_the_source_state_at_the_time_of_the_action():
    """``set_title A → cloner → set_title B`` : variante « A », original « B » (sémantique historique)."""
    profile = _variant_profile({"type": "set_title", "value": "A"}, {"type": "create_audio_variant", "codec": "ac3"},
                               {"type": "set_title", "value": "B"},
                               {"type": "create_audio_variant", "codec": "eac3"})
    result = apply_decision_profile(profile, [_fresh_audio()])
    assert [(t.codec, t.title) for t in result.tracks] == [("DTS", "B"), ("AC3", "A"), ("EAC3", "B")]


def test_requests_of_the_same_target_share_one_variant_and_merge_their_writes():
    first = {"type": "create_audio_variant", "codec": "ac3", "bitrate_kbps": 448, "flags": {"commentary": True}}
    second = {"type": "create_audio_variant", "codec": "ac3", "bitrate_kbps": 640, "enabled": False,
              "flags": {"default": True, "enabled": True}}
    profile = {"version": 1, "kind": "decision-profile", "name": "p",
               "rules": [_rule("a", first, priority=2), _rule("b", second, priority=1)]}
    result = apply_decision_profile(profile, [_fresh_audio()])
    variants = [t for t in result.tracks if t.is_new]
    assert len(variants) == 1
    variant = variants[0]
    assert variant.flag_commentary and variant.flag_default  # drapeau par drapeau
    assert variant.enabled is False and variant.flag_enabled is True  # sélection ≠ FlagEnabled
    assert variant.display_info.endswith("640 kbps")
    assert len(result.report["created_variants"]) == 1 and len(result.report["reused_variants"]) == 1


def test_copy_variant_targets_the_source_codec_and_is_reused():
    copy_action = {"type": "create_audio_variant", "codec": "copy", "title": "Copie"}
    first = apply_decision_profile(_variant_profile(copy_action), [_fresh_audio()])
    before = _states(first.tracks)  # instantané avant la réapplication
    second = apply_decision_profile(_variant_profile(copy_action), first.tracks)
    assert _states(second.tracks) == before
    assert [(t.codec, t.title) for t in second.tracks if t.is_new] == [("DTS", "Copie")]
    assert [t.entry_id for t in second.tracks] == [t.entry_id for t in first.tracks]


def test_title_pattern_variant_is_rendered_on_the_source_at_the_time_of_the_action():
    profile = _variant_profile({"type": "add_track_tags", "value": ["vf"]},
                               {"type": "create_audio_variant", "codec": "ac3",
                                "title_pattern": "{lang_name} AC3 {track_tags}"})
    result = apply_decision_profile(profile, [_fresh_audio()])
    assert [t.title for t in result.tracks if t.is_new] == ["French AC3 vf"]


def _sub(mkv_tid: int, title: str) -> TrackEntry:
    return TrackEntry(mkv_tid=mkv_tid, track_type="subtitle", codec="SUBRIP", display_info="", language="fr-FR",
                      title=title, file_id="src0", orig_language="fr-FR", orig_title=title)


@pytest.mark.parametrize("titles", [("Complets", "Forcés"), ("VF - Complets", "VF - Forcés")])
def test_captured_identical_subtitles_stay_distinct_on_a_second_panel_application(titles):
    table = [_sub(3, titles[0]), _sub(4, titles[1])]
    for track in table:
        track.title = ""
    profile = _capture(*table)
    file_titles = [next(i for i in rule["match"]["all"] if i["field"] == "file_title") for rule in profile["rules"]]
    assert [(i["value"], i["required"]) for i in file_titles] == [(titles[0], False), (titles[1], False)]
    first = apply_decision_profile(profile, [_sub(3, titles[0]), _sub(4, titles[1])], from_current_state=True)
    before = _states(first.tracks)
    second = apply_decision_profile(profile, first.tracks, from_current_state=True)
    for result in (first, second):
        _clean_report(result)
        assert [(t.mkv_tid, t.title, t.enabled) for t in result.tracks] == [(3, "", True), (4, "", True)]
    assert _states(second.tracks) == before


def test_captured_audio_tracks_of_the_same_language_and_codec_are_told_apart_by_file_title():
    def pair():
        return [_audio(1, title="VF"), _audio(2, title="Commentaire")]

    table = pair()
    table[0].title, table[1].enabled = "", False
    profile = _capture(*table)
    first = apply_decision_profile(profile, pair(), from_current_state=True)
    _clean_report(first)
    assert [(t.mkv_tid, t.title, t.enabled) for t in first.tracks] == [(1, "", True), (2, "Commentaire", False)]
    before = _states(first.tracks)  # avant la deuxième application (mêmes objets)
    second = apply_decision_profile(profile, first.tracks, from_current_state=True)
    _clean_report(second)
    assert _states(second.tracks) == before


def test_no_file_title_criterion_when_the_file_has_no_title():
    assert all(item["field"] != "file_title" for item in _capture(_audio(1, title=""))["rules"][0]["match"]["all"])


def test_variant_keeps_the_file_language_and_title_of_its_source_stream():
    from core.profiles.keywords import track_field_values

    original = _audio(1, language="fra", title="VFQ")  # titre physique régionalisant
    original.language, original.title = "en-US", "Modifié"
    variant = clone_track_entry(original)
    clone_of_clone = clone_track_entry(variant)
    for track in (variant, clone_of_clone):
        values = track_field_values(track)
        assert (values["file_language"], values["file_title"]) == ("fr-CA", "VFQ")
        assert (track.orig_language, track.orig_title) == ("en-US", "Modifié")  # édition détectée au mux
    empty = _audio(2, language="", title="")
    empty.language, empty.title = "fr-FR", "Ajouté"
    clone = clone_track_entry(empty)
    assert (clone.file_language, clone.file_title) == ("", "")  # valeur physique vide, aucun repli
    assert (track_field_values(clone)["file_language"], track_field_values(clone)["file_title"]) == ("", "")



# --- Audit 26 : capture du débit d'une variante de même codec, ordre du tableau ---------------------------------


def test_capture_keeps_the_bitrate_of_a_same_codec_variant_on_fresh_tracks():
    def fresh():
        return _audio(1, codec="AC3", title="VF", display_info="5.1  640 kbps")

    source = fresh()
    variant = clone_track_entry(source)
    variant.title, variant.display_info = "Compatibilite", "5.1  448 kbps"
    pure_copy = clone_track_entry(source)
    pure_copy.title = "Copie"
    action, copy_action = [rule["actions"][0] for rule in _capture(source, variant, pure_copy)["rules"][-2:]]
    assert (action["codec"], action["bitrate_kbps"]) == ("ac3", 448)
    assert copy_action["codec"] == "copy"  # copie réelle (même débit)
    result = apply_decision_profile(_capture(source, variant, pure_copy), [fresh()], from_current_state=True)
    _clean_report(result)
    created = {t.title: t.display_info for t in result.tracks if t.is_new}
    assert created == {"Compatibilite": "5.1  448 kbps", "Copie": "5.1  640 kbps"}


def test_capture_restores_the_table_order_across_sources_and_variants():
    video = _video(0, "1920x1080")
    audio = _audio(1, title="VF")
    other = _audio(0, language="en-US", title="VO")
    other.file_id = "src1"
    variant = clone_track_entry(audio)
    variant.codec = "AC3"
    config = RemuxConfig(
        sources=[SourceInput(path=Path("/tmp/a.mkv"), file_index=0, tracks=[video, audio, variant]),
                 SourceInput(path=Path("/tmp/b.mkv"), file_index=1, tracks=[other])],
        output=Path("/tmp/out.mkv"),
        track_order=[(1, 0, other.entry_id), (0, 0, video.entry_id), (0, 1, variant.entry_id),
                     (0, 1, audio.entry_id)],
    )
    profile = remux_config_to_decision_profile(config, name="ordre")
    validate_decision_profile(profile)
    fresh = [_video(0, "1920x1080"), _audio(1, title="VF"), _audio(0, language="en-US", title="VO")]
    fresh[2].file_id = "src1"
    result = apply_decision_profile(profile, fresh, source_index_by_file_id={"src0": 0, "src1": 1},
                                    from_current_state=True)
    _clean_report(result)
    assert [(t.title, t.codec, t.is_new) for t in result.tracks] == [
        ("VO", "EAC3", False), ("", "HEVC", False), ("VF", "AC3", True), ("VF", "EAC3", False)]



@pytest.mark.parametrize("fresh_tracks", [True, False])
def test_capture_keeps_twin_variants_apart_by_their_bitrate(fresh_tracks):
    """Audit 27 : deux variantes de même codec, titre et langue (448 / 192 kb/s) → capturées avec un critère de débit,
    recréées (pistes fraîches) ou reprises (tableau) distinctes, chacune à son rang."""
    def fresh():
        return _audio(1, codec="AC3", title="VF", display_info="5.1  640 kbps")

    source = fresh()
    high, low = clone_track_entry(source), clone_track_entry(source)
    high.display_info, low.display_info = "5.1  448 kbps", "5.1  192 kbps"
    config = RemuxConfig(
        sources=[SourceInput(path=Path("/tmp/s.mkv"), file_index=0, tracks=[source, high, low])],
        output=Path("/tmp/o.mkv"),
        track_order=[(0, 1, high.entry_id), (0, 1, source.entry_id), (0, 1, low.entry_id)],
    )
    profile = remux_config_to_decision_profile(config, name="jumelles")
    validate_decision_profile(profile)
    actions = [rule["actions"][0] for rule in profile["rules"] if rule["id"].startswith("audio_variant")]
    assert all(action["match_bitrate"] and action["codec"] == "ac3" for action in actions)
    tracks = [fresh()] if fresh_tracks else copy.deepcopy([source, high, low])
    result = apply_decision_profile(profile, tracks, from_current_state=True)
    _clean_report(result)
    assert [t.display_info.split("  ")[-1] for t in result.tracks] == ["448 kbps", "640 kbps", "192 kbps"]
    before = _states(result.tracks)
    again = apply_decision_profile(profile, result.tracks, from_current_state=True)
    assert _states(again.tracks) == before and again.report["created_variants"] == []


def test_variants_without_match_bitrate_keep_the_historical_reuse():
    """Profils existants (sans ``match_bitrate``) : deux demandes de même codec / titre / langue reprennent la même
    variante (affectation historique)."""
    profile = _variant_profile({"type": "create_audio_variant", "codec": "ac3", "bitrate_kbps": 448},
                               {"type": "create_audio_variant", "codec": "ac3", "bitrate_kbps": 192})
    result = apply_decision_profile(profile, [_audio(1, codec="AC3", display_info="5.1  640 kbps")])
    assert [t.display_info.split("  ")[-1] for t in result.tracks if t.is_new] == ["192 kbps"]



@pytest.mark.parametrize("cleared", [("title",), ("language",), ("title", "language")])
@pytest.mark.parametrize("fresh_tracks", [True, False])
def test_capture_keeps_cleared_twin_variants_apart(cleared, fresh_tracks):
    """Audit 28 : jumelles (448 / 192 kb/s) aux titre et / ou langue vidés → effacements distincts par débit, chacune
    recréée ou reprise à son rang."""
    def fresh():
        return _audio(1, codec="AC3", title="VF", display_info="5.1  640 kbps")

    source = fresh()
    high, low = clone_track_entry(source), clone_track_entry(source)
    high.display_info, low.display_info = "5.1  448 kbps", "5.1  192 kbps"
    for variant in (high, low):
        for field in cleared:
            setattr(variant, field, "")
    config = RemuxConfig(
        sources=[SourceInput(path=Path("/tmp/s.mkv"), file_index=0, tracks=[source, high, low])],
        output=Path("/tmp/o.mkv"),
        track_order=[(0, 1, high.entry_id), (0, 1, source.entry_id), (0, 1, low.entry_id)],
    )
    profile = remux_config_to_decision_profile(config, name="jumelles")
    actions = [rule["actions"][0] for rule in profile["rules"] if rule["id"].startswith("audio_variant")]
    assert all(action["match_bitrate"] and action["clear"] == list(cleared) for action in actions)
    tracks = [fresh()] if fresh_tracks else copy.deepcopy([source, high, low])
    result = apply_decision_profile(profile, tracks, from_current_state=True)
    _clean_report(result)
    assert [t.display_info.split("  ")[-1] for t in result.tracks] == ["448 kbps", "640 kbps", "192 kbps"]
    before = _states(result.tracks)
    again = apply_decision_profile(profile, result.tracks, from_current_state=True)
    assert _states(again.tracks) == before and again.report["created_variants"] == []



def test_same_value_from_a_lower_rule_keeps_the_winning_priority():
    """Audit 30 : une règle plus basse réécrivant la même valeur n'abaisse pas la priorité retenue (sinon une règle
    suivante de son niveau entrait en conflit)."""
    audio = {"all": [{"field": "type", "op": "is", "value": "audio"}]}
    profile = {"version": 1, "kind": "decision-profile", "name": "p", "rules": [
        {"id": "b", "priority": 9, "scope": "all", "match": audio, "actions": [{"type": "set_title", "value": "Y"}]},
        {"id": "c", "priority": 7, "scope": "all", "match": audio, "actions": [{"type": "set_title", "value": "Y"}]},
        {"id": "d", "priority": 7, "scope": "all", "match": audio, "actions": [{"type": "set_title", "value": "X"}]},
    ]}
    result = apply_decision_profile(profile, [_fresh_audio()])
    assert result.report["conflicts"] == [] and result.tracks[0].title == "Y"


def test_equal_priority_same_value_keeps_the_historical_conflict_identity():
    """Égalité de priorité : comportement historique (dernière règle de même valeur retenue pour l'identifiant)."""
    audio = {"all": [{"field": "type", "op": "is", "value": "audio"}]}
    profile = {"version": 1, "kind": "decision-profile", "name": "p", "rules": [
        {"id": "a", "priority": 9, "scope": "all", "match": audio, "actions": [{"type": "set_title", "value": "X"}]},
        {"id": "b", "priority": 9, "scope": "all", "match": audio, "actions": [{"type": "set_title", "value": "X"}]},
        {"id": "c", "priority": 9, "scope": "all", "match": audio, "actions": [{"type": "set_title", "value": "Y"}]},
    ]}
    conflicts = apply_decision_profile(profile, [_fresh_audio()]).report["conflicts"]
    assert [(c["current_rule_id"], c["new_rule_id"]) for c in conflicts] == [("b", "c")]



@pytest.mark.parametrize("mode", ["override", "add"])
def test_replacing_with_the_same_value_hands_the_write_over_as_before(mode):
    """Audit 31 : « Remplacer » (et « Ajouter ») d'une même valeur transfère l'écriture à la règle (comportement
    historique) : son écriture suivante en priorité n'est pas ignorée."""
    audio = {"all": [{"field": "type", "op": "is", "value": "audio"}]}
    profile = {"version": 1, "kind": "decision-profile", "name": "p", "rules": [
        {"id": "a", "priority": 100, "scope": "all", "match": audio, "actions": [{"type": "set_title", "value": "X"}]},
        {"id": "b", "priority": 50, "scope": "all", "match": audio,
         "actions": [{"type": "set_title", "value": "X", "mode": mode}, {"type": "set_title", "value": "Y"}]},
    ]}
    result = apply_decision_profile(profile, [_fresh_audio()])
    assert result.tracks[0].title == "Y" and result.report["skipped_writes"] == []


@pytest.mark.parametrize("clear_titles", [False, True])
@pytest.mark.parametrize("fresh_tracks", [True, False])
def test_capture_keeps_equal_bitrate_twin_variants_with_their_own_selection_and_flags(clear_titles, fresh_tracks):
    """Audit 32 : deux variantes identiques (codec, débit, titre, langue) mais de sélection / drapeaux différents →
    capturées avec leur rang de jumelle, recréées ou reprises distinctes, chacune avec ses propriétés, stables à la
    réapplication."""
    def fresh():
        source = _audio(1, codec="AC3", title="VF", display_info="5.1  640 kbps")
        source.enabled = False
        return source

    source = fresh()
    first, second = clone_track_entry(source), clone_track_entry(source)
    first.enabled, first.flag_default = True, True
    second.enabled, second.flag_commentary = False, True
    if clear_titles:
        first.title = second.title = ""
    config = RemuxConfig(
        sources=[SourceInput(path=Path("/tmp/s.mkv"), file_index=0, tracks=[source, first, second])],
        output=Path("/tmp/o.mkv"),
        track_order=[(0, 1, first.entry_id)],
    )
    profile = remux_config_to_decision_profile(config, name="jumelles")
    validate_decision_profile(profile)
    assert [rule["actions"][0]["twin"] for rule in profile["rules"] if rule["id"].startswith("audio_variant")] == [1, 2]
    tracks = [fresh()] if fresh_tracks else copy.deepcopy([source, first, second])
    result = apply_decision_profile(profile, tracks, from_current_state=True)
    _clean_report(result)
    variants = sorted(((t.enabled, t.flag_default, t.flag_commentary) for t in result.tracks if t.is_new),
                      reverse=True)
    assert variants == [(True, True, False), (False, False, True)]
    before = _states(result.tracks)
    again = apply_decision_profile(profile, result.tracks, from_current_state=True)
    _clean_report(again)
    assert _states(again.tracks) == before and again.report["created_variants"] == []


def test_profiles_without_twin_rank_keep_sharing_one_variant():
    profile = _variant_profile({"type": "create_audio_variant", "codec": "ac3", "flags": {"default": True}},
                               {"type": "create_audio_variant", "codec": "ac3", "flags": {"commentary": True}})
    result = apply_decision_profile(profile, [_audio(1, codec="AC3")])
    variants = [t for t in result.tracks if t.is_new]
    assert len(variants) == 1 and variants[0].flag_default and variants[0].flag_commentary
