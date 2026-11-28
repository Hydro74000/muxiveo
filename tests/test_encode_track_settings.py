"""Réglages de piste partagés (core.workflows.encode.planning.track_settings) : règles du panneau Encodage."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from core.inspector import FileInfo, HDRType, VideoTrack
from core.workflows.encode.models import EncodePreset, QualityMode, RestoreSettings, VideoCropSettings
from core.workflows.encode.planning import track_settings as ts

MD = "G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,50)"


def _video(index: int = 0, hdr: HDRType = HDRType.HDR10, *, transfer: str = "smpte2084", bit_depth: int = 10,
           dovi: int | None = None, compat: int | None = None) -> VideoTrack:
    track = VideoTrack(index=index, codec="hevc", codec_long="hevc", width=3840, height=2160, frame_rate="24000/1001",
                       bit_depth=bit_depth, color_space=None, color_primaries=None, color_transfer=transfer,
                       color_matrix=None, hdr_type=hdr, raw={"pix_fmt": "yuv420p10le"})
    track.dovi_profile = dovi
    track.dovi_compat_id = compat
    return track


def _info(*videos: VideoTrack, hdr: HDRType = HDRType.HDR10) -> FileInfo:
    return FileInfo(path=Path("/src.mkv"), format="matroska", duration_s=60.0, size_bytes=1, bit_rate=1,
                    video_tracks=list(videos), hdr_type=hdr)


def _ctx(hdr: HDRType = HDRType.HDR10, **kwargs) -> ts.SourceVideoContext:
    return ts.SourceVideoContext.from_file_info(_info(_video(hdr=hdr, **kwargs), hdr=hdr), 0, master_display=MD,
                                                max_cll="1000,400")


def test_source_context_reads_the_video_track():
    ctx = ts.SourceVideoContext.from_file_info(_info(_video(dovi=8, compat=1), hdr=HDRType.DOLBY_VISION), 0)
    assert (ctx.hdr_type, ctx.transfer, ctx.bit_depth, ctx.pix_fmt, ctx.codec) == \
        (HDRType.HDR10, "smpte2084", 10, "yuv420p10le", "hevc")
    assert ctx.dovi_profile == "p8_1"


def test_default_state_copies_source_hdr():
    state = ts.default_video_state(_ctx(HDRType.DOLBY_VISION_HDR10PLUS))
    assert state["codec"] == "copy" and state["inject_hdr_meta"] is True
    assert state["copy_dv"] is True and state["copy_hdr10plus"] is True
    assert state["master_display"] == state["default_master_display"] == MD


def test_sdr_source_has_no_hdr_options():
    state = ts.default_video_state(_ctx(HDRType.NONE, transfer="bt709", bit_depth=8))
    assert not state["inject_hdr_meta"] and not state["copy_dv"] and not state["copy_hdr10plus"]


def test_copy_keeps_source_hdr_whatever_the_state_says():
    state = ts.default_video_state(_ctx(HDRType.DOLBY_VISION))
    state.update(copy_dv=False, inject_hdr_meta=False, tonemap_to_sdr=True)
    normalized = ts.normalize_video_state(state, _ctx(HDRType.DOLBY_VISION))
    assert normalized["copy_dv"] is True and normalized["inject_hdr_meta"] is True
    assert normalized["tonemap_to_sdr"] is False


def test_sdr_codec_drops_hdr_and_10bit_falls_back_to_auto():
    state = ts.default_video_state(_ctx(HDRType.DOLBY_VISION))
    state.update(codec="libx264", bit_depth="10")
    env = ts.EncoderEnvironment(supports_10bit=lambda codec: False)
    normalized = ts.normalize_video_state(state, _ctx(HDRType.DOLBY_VISION), env)
    assert normalized["bit_depth"] == "auto"
    assert not normalized["copy_dv"] and not normalized["inject_hdr_meta"]


def test_propagation_keeps_hdr_of_each_source():
    template = ts.default_video_state(_ctx(HDRType.HDR10))
    template.update(codec="libx265", crf=20, master_display="AUTRE", default_master_display="AUTRE")
    own = ts.default_video_state(_ctx(HDRType.HDR10))
    propagated = ts.propagate_state(template, own, _ctx(HDRType.HDR10))
    assert propagated["crf"] == 20 and propagated["codec"] == "libx265"
    assert propagated["master_display"] == MD and propagated["default_master_display"] == MD


def test_preset_never_overrides_source_specific_fields():
    ctx = _ctx(HDRType.HDR10)
    state = ts.default_video_state(ctx)
    preset = EncodePreset(name="x265", codec="libx265", crf=22, preset="medium", inject_hdr_meta=False,
                          master_display="G(0,0)B(0,0)R(0,0)WP(0,0)L(1,1)", max_cll="1,1", tonemap_to_sdr=True,
                          tonemap_algorithm="mobius", fel_precision="fast", bake_dovi_fel=True,
                          restoration=RestoreSettings(enabled=True, level="fort"))
    applied = ts.apply_preset_to_state(state, preset, ctx)
    assert (applied["codec"], applied["crf"], applied["preset"]) == ("libx265", 22, "medium")
    assert applied["tonemap_algorithm"] == "mobius" and applied["fel_precision"] == "fast"
    assert applied["bake_dovi_fel"] is True and RestoreSettings.from_value(applied["restoration"]).level == "fort"
    for key in ("inject_hdr_meta", "master_display", "max_cll", "tonemap_to_sdr"):
        assert applied[key] == state[key], key
    assert set(ts.PRESET_FIELDS).isdisjoint(ts.PRESET_PROTECTED_FIELDS)


def test_video_settings_from_state_carries_source_and_effective_hdr():
    ctx = _ctx(HDRType.HDR10)
    state = ts.default_video_state(ctx)
    state.update(codec="libx265", crf=19, crop=VideoCropSettings(enabled=True, top=140, bottom=140))
    settings = ts.video_settings_from_state(state, ctx, stream_index=0, source_path=Path("/src.mkv"),
                                            track_entry_id="v0")
    assert settings.codec == "libx265" and settings.quality_mode == QualityMode.CRF and settings.crf == 19
    assert settings.inject_hdr_meta and settings.master_display == MD and settings.max_cll == "1000,400"
    assert settings.static_hdr_master_display_source == "source"
    assert (settings.source_bit_depth, settings.source_color_transfer) == (10, "smpte2084")
    assert settings.crop.top == 140


def test_tonemap_disables_every_hdr_output():
    ctx = _ctx(HDRType.DOLBY_VISION)
    state = ts.default_video_state(ctx)
    state.update(codec="libx265", tonemap_to_sdr=True)
    settings = ts.video_settings_from_state(state, ctx, stream_index=0, source_path=None, track_entry_id=None)
    assert settings.tonemap_to_sdr and not settings.copy_dv and not settings.inject_hdr_meta


def test_hdr_meta_extraction_prefers_mediainfo_then_side_data():
    mediainfo = {"media": {"track": [{"@type": "Video", "MasteringDisplay_ColorPrimaries": "BT.2020",
                                      "MasteringDisplay_Luminance_Min": "0.0050",
                                      "MasteringDisplay_Luminance_Max": "1000", "MaxCLL": "1 000 cd/m2",
                                      "MaxFALL": "400 cd/m2"}]}}
    md, cll = ts.extract_hdr_meta_fields({}, mediainfo_json=mediainfo)
    assert md == "G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(10000000,50)" and cll == "1000,400"
    raw = {"side_data_list": [{"side_data_type": "Content light level metadata", "max_content": 800,
                               "max_average": 200}]}
    frames_called = []
    md, cll = ts.extract_hdr_meta_fields(raw, mediainfo_json={}, from_frames=lambda: frames_called.append(1)
                                         or ("G(1,1)B(1,1)R(1,1)WP(1,1)L(1,1)", ""))
    assert cll == "800,200" and md.startswith("G(1,1)") and frames_called


def test_pure_copy_rules():
    video = SimpleNamespace(codec="copy", inject_hdr_meta=False, tonemap_to_sdr=False,
                            has_video_transform=lambda: False, copy_dv=False, dovi_profile="0")
    assert ts.is_pure_copy(SimpleNamespace(video_tracks=[video], audio_tracks=[]))
    assert not ts.is_pure_copy(SimpleNamespace(video_tracks=[video],
                                               audio_tracks=[SimpleNamespace(codec="eac3")]))
    normalized = SimpleNamespace(**{**vars(video), "copy_dv": True, "dovi_profile": "2"})
    assert not ts.is_pure_copy(SimpleNamespace(video_tracks=[normalized], audio_tracks=[]))


# --- Neuvième audit : codec sans édition HDR10 ----------------------------------------------------------------


def _typed_state(ctx: ts.SourceVideoContext, codec: str, **extra) -> dict:
    """Master Display et MaxCLL ressaisis à l'identique (marqueurs explicites)."""
    return {**ts.default_video_state(ctx), "codec": codec, "master_display": MD, "max_cll": "1000,400",
            "static_hdr_master_display_manual": True, "static_hdr_max_cll_manual": True, **extra}


def test_codec_without_hdr10_editing_imposes_the_source_values_and_normalization_is_idempotent():
    ctx = _ctx(HDRType.HDR10)
    kept = ts.normalize_video_state(_typed_state(ctx, "libx265"), ctx)
    assert kept["static_hdr_master_display_manual"] and kept["static_hdr_max_cll_manual"]  # codec éditable
    for codec in ("libx264", "copy"):
        once = ts.normalize_video_state(_typed_state(ctx, codec, static_hdr_manual=True), ctx)
        assert not once["static_hdr_master_display_manual"] and not once["static_hdr_max_cll_manual"], codec
        assert (once["master_display"], once["max_cll"]) == (MD, "1000,400")
        assert once["static_hdr_manual"] is (codec != "copy")  # case cochée : gardée hors Copy
        assert ts.normalize_video_state(once, ctx) == once  # idempotente
        back = ts.normalize_video_state({**once, "codec": "libx265"}, ctx)
        settings = ts.video_settings_from_state(back, ctx, stream_index=0, source_path=None, track_entry_id=None)
        assert settings.static_hdr_master_display_source == settings.static_hdr_light_level_source == "source"


def test_copy_with_dovi_normalized_to_p81_keeps_hdr10_entries_editable():
    ctx = _ctx(HDRType.DOLBY_VISION, dovi=7, compat=6)
    state = _typed_state(ctx, "copy", inject_hdr_meta=True, copy_dv=True, dovi_profile="2")
    assert ts.static_hdr_editable(state, "copy")
    normalized = ts.normalize_video_state(state, ctx)
    assert ts.state_allows_copy_static_hdr_edit(normalized, "copy")  # exception Copy + P8.1 + HDR10 injecté
    assert normalized["static_hdr_master_display_manual"] and normalized["static_hdr_max_cll_manual"]
