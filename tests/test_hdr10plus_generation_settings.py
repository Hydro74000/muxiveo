"""HDR10+ généré depuis le Dolby Vision : règle effective, exclusivité avec la copie, propagation, presets, jobs, CLI."""
from __future__ import annotations

import argparse
from types import SimpleNamespace

import pytest

from cli.encode_commands import _preset_from_args
from cli.encode_options import add_encode_options, encode_block_from_args
from core.inspector import HDRType
from core.jobs.errors import JobError
from core.service import ApiError
from core.service.schemas.encode import EncodeBlock
from core.workflows.encode.hdr10plus_output import hdr10plus_from_source, outputs_dynamic_hdr, outputs_hdr10plus
from core.workflows.encode.models import EncodePreset
from core.workflows.encode.planning import track_settings as ts
from tests.test_encode_request import _resolve
from tests.test_encode_track_settings import _ctx

DV = HDRType.DOLBY_VISION
DV_PLUS = HDRType.DOLBY_VISION_HDR10PLUS


def _state(ctx: ts.SourceVideoContext, **extra) -> dict:
    return ts.normalize_video_state({**ts.default_video_state(ctx), **extra}, ctx)


def _settings(state: dict, ctx: ts.SourceVideoContext):
    return ts.video_settings_from_state(state, ctx, stream_index=0, source_path=None, track_entry_id="v0")


# ── Règle effective ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("codec,expected", [
    ("libx265", True), ("hevc_nvenc", True), ("hevc_qsv", True), ("hevc_vaapi", True), ("hevc_amf", True),
    ("nvencc_hevc", True), ("nvencc_av1", True), ("copy", True),
    ("libsvtav1", False), ("av1_nvenc", False), ("libx264", False),
])
def test_generation_follows_codecs_that_carry_hdr10plus(codec, expected):
    ctx = _ctx(DV, dovi=8, compat=1)
    assert ts.hdr10plus_generation_available(ctx, codec, tonemap=False) is expected


def test_generation_requires_a_pq_dolby_vision_source():
    assert not ts.hdr10plus_generation_available(_ctx(HDRType.HDR10), "libx265", tonemap=False)
    assert not ts.hdr10plus_generation_available(_ctx(HDRType.HDR10PLUS), "libx265", tonemap=False)
    for compat in (2, 4):  # P8.2 (SDR) et P8.4 (HLG) : image de base non PQ
        assert not ts.hdr10plus_generation_available(_ctx(DV, dovi=8, compat=compat), "libx265", tonemap=False)
    assert not ts.hdr10plus_generation_available(_ctx(DV, dovi=8, compat=1), "libx265", tonemap=True)


def test_p5_is_generated_when_reencoded_never_in_copy():
    ctx = _ctx(DV, dovi=5, compat=0)
    assert ctx.dovi_profile == "p5"
    assert ts.hdr10plus_generation_available(ctx, "libx265", tonemap=False)
    assert not ts.hdr10plus_generation_available(ctx, "copy", tonemap=False)
    # Sous-profil inconnu : admis, contrôlé au lancement.
    assert ts.hdr10plus_generation_available(_ctx(DV), "copy", tonemap=False)


# ── Exclusivité et mémoire de l'intention ────────────────────────────────────
def test_generation_replaces_the_source_hdr10plus_copy():
    ctx = _ctx(DV_PLUS, dovi=8, compat=1)
    for codec in ("libx265", "copy"):
        state = _state(ctx, codec=codec, copy_hdr10plus=True, generate_hdr10plus=True)
        assert state["copy_hdr10plus"] is False and state["generate_hdr10plus"] is True
        settings = _settings(state, ctx)
        assert settings.generate_hdr10plus and not settings.copy_hdr10plus
        assert outputs_hdr10plus(settings) and not hdr10plus_from_source(settings) and outputs_dynamic_hdr(settings)
    # Sans génération, la copie de la source revient en Copy.
    state = _state(ctx, codec="copy", generate_hdr10plus=False)
    assert state["copy_hdr10plus"] is True


def test_intent_survives_a_codec_without_hdr10plus():
    ctx = _ctx(DV, dovi=8, compat=1)
    state = _state(ctx, codec="libsvtav1", generate_hdr10plus=True)
    assert state["generate_hdr10plus"] is True and not _settings(state, ctx).generate_hdr10plus
    state = ts.normalize_video_state({**state, "codec": "libx265"}, ctx)
    assert _settings(state, ctx).generate_hdr10plus


def test_copy_keeps_the_dovi_subprofile_only_when_generating():
    ctx = _ctx(DV, dovi=8, compat=1)
    assert _settings(_state(ctx, codec="copy"), ctx).dovi_source_profile == ""
    assert _settings(_state(ctx, codec="copy", generate_hdr10plus=True), ctx).dovi_source_profile == "p8_1"


def test_generation_in_copy_is_not_a_pure_copy():
    video = SimpleNamespace(codec="copy", inject_hdr_meta=False, tonemap_to_sdr=False,
                            has_video_transform=lambda: False, copy_dv=True, dovi_profile="0", generate_hdr10plus=True)
    assert not ts.is_pure_copy(SimpleNamespace(video_tracks=[video], audio_tracks=[]))


# ── « Appliquer à toutes » ───────────────────────────────────────────────────
def test_common_intent_reaches_dolby_vision_sources_only():
    dv, hdr10 = _ctx(DV, dovi=8, compat=1), _ctx(HDRType.HDR10)
    template = {**_state(dv, codec="libx265", generate_hdr10plus=True),
                "hdr_common": {ts.HDR10PLUS_GENERATION_OPTION: True}}
    assert ts.propagate_state(template, _state(dv), dv)["generate_hdr10plus"] is True
    assert ts.propagate_state(template, _state(hdr10), hdr10)["generate_hdr10plus"] is False
    # Intention inconnue : choix propre à la piste, jamais celui de la piste modèle.
    plain = _state(dv, codec="libx265", generate_hdr10plus=True)
    assert ts.propagate_state(plain, _state(dv), dv)["generate_hdr10plus"] is False


def test_generation_is_not_mixed_with_the_hdr10plus_copy_memory():
    ctx = _ctx(DV_PLUS, dovi=8, compat=1)
    template = {**_state(ctx, codec="libx264"), "hdr_common": {ts.HDR10PLUS_GENERATION_OPTION: True}}
    propagated = ts.propagate_state(template, _state(ctx, codec="libx265"), ctx)
    assert propagated["generate_hdr10plus"] is True
    assert ts.restore_flags(propagated) == (True, True)  # mémoires de copie DV / HDR10+ inchangées
    back = ts.normalize_video_state({**propagated, "codec": "libx265"}, ctx)
    assert back["generate_hdr10plus"] is True and back["copy_hdr10plus"] is False


# ── Presets ──────────────────────────────────────────────────────────────────
def test_preset_carries_the_generation_intent():
    ctx = _ctx(DV, dovi=8, compat=1)
    preset = EncodePreset(name="G", codec="libx265", generate_hdr10plus=True)
    assert preset.to_video_settings().generate_hdr10plus
    applied = ts.apply_preset_to_state(ts.default_video_state(ctx), preset, ctx)
    assert applied["generate_hdr10plus"] is True and _settings(applied, ctx).generate_hdr10plus
    assert "generate_hdr10plus" in preset.to_json_dict()


def _args(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    add_encode_options(parser)
    parser.add_argument("--name", default="P")
    parser.add_argument("--description", default="")
    return parser.parse_args(list(argv))


def test_cli_option_builds_the_generate_mode_and_preset():
    block = encode_block_from_args(_args("--video-codec", "libx265", "--hdr10plus", "generate"))
    assert block is not None and block["video"]["default"]["hdr10plus"] == {"mode": "generate"}
    EncodeBlock.from_dict(block)
    preset = _preset_from_args(_args("--video-codec", "libx265", "--hdr10plus", "generate"))
    assert preset["generate_hdr10plus"] is True
    with pytest.raises(JobError, match="hdr10plus"):
        _preset_from_args(_args("--video-codec", "libx265", "--hdr10plus", "copy"))


def test_schema_accepts_generate_only():
    EncodeBlock.from_dict({"video": {"default": {"hdr10plus": {"mode": "generate"}}}})
    with pytest.raises(ApiError):
        EncodeBlock.from_dict({"video": {"default": {"hdr10plus": {"mode": "dv"}}}})


# ── Jobs ─────────────────────────────────────────────────────────────────────
def test_job_generates_in_copy_and_publishes_the_intent():
    resolved = _resolve({"video": {"default": {"codec": "copy", "hdr10plus": {"mode": "generate"},
                                               "static_hdr": {"mode": "source"}}}}, DV)
    assert not resolved.pure_copy
    video = resolved.video[0]
    assert video["generate_hdr10plus"] is True and video["copy_hdr10plus"] is False
    assert resolved.config is not None and resolved.config.video_tracks[0].generate_hdr10plus
    assert not resolved.warnings


def test_job_warns_when_generation_does_not_apply():
    resolved = _resolve({"video": {"default": {"codec": "libx265", "hdr10plus": {"mode": "generate"}}}},
                        HDRType.HDR10)
    assert resolved.video[0]["generate_hdr10plus"] is False
    assert any("HDR10+ non généré" in w for w in resolved.warnings)


def test_a_later_layer_replaces_generation_with_copy():
    resolved = _resolve({"video": {"default": {"codec": "libx265", "hdr10plus": {"mode": "generate"}},
                                   "tracks": [{"select": {"stream": 0}, "hdr10plus": {"mode": "copy"}}]}}, DV_PLUS)
    video = resolved.video[0]
    assert video["copy_hdr10plus"] is True and video["generate_hdr10plus"] is False


@pytest.mark.parametrize("hdr,layer", [
    (HDRType.HDR10, {}),
    (DV, {"codec": "libsvtav1"}),
    (DV, {"tonemap": {"enabled": True}}),
])
def test_preset_generation_that_does_not_apply_warns(hdr, layer):
    preset = EncodePreset(name="G", codec="libx265", generate_hdr10plus=True)
    resolved = _resolve({"preset": "G", "video": {"default": layer}}, hdr, load_preset=lambda name: preset)
    assert resolved.video[0]["generate_hdr10plus"] is False
    assert any("HDR10+ non généré" in w for w in resolved.warnings)


def test_preset_generation_overridden_by_copy_or_off_does_not_warn():
    preset = EncodePreset(name="G", codec="libx265", generate_hdr10plus=True)
    for mode in ("copy", "off"):
        resolved = _resolve({"preset": "G", "video": {"default": {"hdr10plus": {"mode": mode}}}}, DV,
                            load_preset=lambda name: preset)
        assert resolved.video[0]["generate_hdr10plus"] is False
        assert not any("HDR10+ non généré" in w for w in resolved.warnings)
    resolved = _resolve({"preset": "G"}, DV, load_preset=lambda name: preset)
    assert resolved.video[0]["generate_hdr10plus"] is True and not resolved.warnings
