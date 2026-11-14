"""Bloc ``encode`` : contrat strict, couches (source → preset → défaut → piste), politique HDR, options du CLI."""

from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cli.encode_options import add_encode_options, apply_encode_options, encode_block_from_args
from core.inspector import AudioTrack, FileInfo, HDRType, VideoTrack
from core.jobs.encode import resolve_encode
from core.jobs.errors import JobError
from core.service import ApiError
from core.service.schemas.encode import EncodeBlock
from core.workflows.encode.catalog import default_preset_for_codec
from core.workflows.encode.models import EncodePreset
from core.workflows.remux_models import RemuxConfig, SourceInput, TrackEntry

SRC = Path("/films/source.mkv")
MD = "G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,50)"


def _video(hdr: HDRType) -> VideoTrack:
    pq = hdr is not HDRType.NONE
    return VideoTrack(index=0, codec="hevc", codec_long="hevc", width=3840, height=2160, frame_rate="24000/1001",
                      bit_depth=10 if pq else 8, color_space=None, color_primaries=None,
                      color_transfer="smpte2084" if pq else "bt709", color_matrix=None, hdr_type=hdr,
                      raw={"side_data_list": [{"side_data_type": "Content light level metadata", "max_content": 1000,
                                               "max_average": 400}]} if pq else {})


def _audio() -> AudioTrack:
    return AudioTrack(index=1, codec="truehd", codec_long="truehd", channels=8, channel_layout="7.1",
                      sample_rate=48000, bit_rate=None, language="eng", title="")


def _job(hdr: HDRType = HDRType.HDR10, *, variant: TrackEntry | None = None) -> tuple[RemuxConfig, dict[str, FileInfo]]:
    video = TrackEntry(0, "video", "HEVC", "2160p", "und", "", entry_id="v0")
    audio = TrackEntry(1, "audio", "TRUEHD", "7.1", "eng", "", entry_id="a0")
    tracks = [video, audio] + ([variant] if variant else [])
    source = SourceInput(path=SRC, file_index=0, tracks=tracks)
    order = [(0, t.mkv_tid, t.entry_id) for t in tracks]
    remux = RemuxConfig(sources=[source], output=Path("/out/sortie.mkv"), track_order=order)
    info = FileInfo(path=SRC, format="matroska", duration_s=60.0, size_bytes=1, bit_rate=1,
                    video_tracks=[_video(hdr)], audio_tracks=[_audio()], hdr_type=hdr,
                    mediainfo_json={"media": {"track": [{"@type": "Video", "MasteringDisplay_ColorPrimaries": "Display P3",
                                                         "MasteringDisplay_Luminance_Min": "0.005",
                                                         "MasteringDisplay_Luminance_Max": "1000"}]}} if hdr is not HDRType.NONE else None)
    return remux, {str(SRC): info}


def _resolve(payload: dict[str, Any], hdr: HDRType = HDRType.HDR10, **kwargs: Any):
    remux, infos = _job(hdr, variant=kwargs.pop("variant", None))
    config = SimpleNamespace(work_dir=Path("/tmp/work"), aac_bitrate_per_channel_kbps=96,
                             eac3_bitrate_per_channel_kbps=96)
    return resolve_encode(EncodeBlock.from_dict(payload), remux, infos=infos, config=config, **kwargs)


def test_encode_block_is_strict():
    with pytest.raises(ApiError) as caught:
        EncodeBlock.from_dict({"video": {"default": {"codec": "libx265", "crf": 99, "speed": 1}}})
    assert {d.path for d in caught.value.details} == {"video.default.crf", "video.default.speed"}


def test_all_copy_stays_a_remux_unless_static_hdr_is_rewritten():
    resolved = _resolve({"video": {"default": {"codec": "copy"}}}, HDRType.NONE)
    assert resolved.pure_copy and resolved.config is None
    # Source HDR10 : la copie réécrit le HDR10 statique (case cochée par défaut, comme le panneau).
    resolved = _resolve({"video": {"default": {"codec": "copy"}}})
    assert not resolved.pure_copy and resolved.video[0]["inject_hdr_meta"]


def test_layers_source_preset_default_track(tmp_path):
    preset = EncodePreset(name="P", codec="libx265", crf=24, preset="medium", master_display="G(1,1)", max_cll="1,1")
    resolved = _resolve({"preset": "P", "video": {"default": {"crf": 20},
                                                  "tracks": [{"select": {"stream": 0}, "encoder_preset": "slower"}]}},
                        load_preset=lambda name: preset)
    video = resolved.video[0]
    assert (video["codec"], video["crf"], video["preset"]) == ("libx265", 20, "slower")
    assert video["inject_hdr_meta"] and video["max_cll"] == "1000,400", "HDR de la source, jamais celui du preset"
    assert video["output_bit_depth"] == 10
    config = resolved.config
    assert config is not None and config.video_tracks[0].stream_index == 0 and config.output == Path("/out/sortie.mkv")


def test_codec_change_picks_its_default_preset_and_rate_control_family():
    video = _resolve({"video": {"default": {"codec": "hevc_nvenc", "rate_control": "vbr_cq", "cq": 22}}}).video[0]
    assert video["preset"] == "slow", "préréglage conservé quand le nouveau codec le propose (liste du panneau)"
    assert video["quality_mode"] == "cq" and video["cq"] == 22
    video = _resolve({"video": {"default": {"codec": "libsvtav1"}}}).video[0]
    assert video["preset"] == default_preset_for_codec("libsvtav1")
    with pytest.raises(JobError, match="mode de débit"):
        _resolve({"video": {"default": {"codec": "libx265", "rate_control": "vbr_cq"}}})


@pytest.mark.parametrize("spec,message", [
    ({"dolby_vision": {"mode": "off"}}, "Dolby Vision"),
    ({"static_hdr": {"mode": "off"}}, "HDR10 statique"),
    ({"tonemap": {"enabled": True}}, "tone mapping"),
])
def test_copy_cannot_strip_source_hdr(spec, message):
    with pytest.raises(JobError, match=message) as caught:
        _resolve({"video": {"default": {"codec": "copy", **spec}}}, HDRType.DOLBY_VISION)
    assert caught.value.exit_code == 3


def test_reencode_can_strip_dv_and_warns_when_copy_is_impossible():
    resolved = _resolve({"video": {"default": {"codec": "libx265", "dolby_vision": {"mode": "off"}}}},
                        HDRType.DOLBY_VISION)
    assert resolved.video[0]["copy_dv"] is False and not resolved.warnings
    resolved = _resolve({"video": {"default": {"codec": "libx264", "dolby_vision": {"mode": "copy"}}}},
                        HDRType.DOLBY_VISION)
    assert any("Dolby Vision non copié" in w for w in resolved.warnings)


def test_static_hdr_manual_and_estimate():
    video = _resolve({"video": {"default": {"codec": "libx265", "static_hdr": {"max_cll": "1500,300"}}}}).video[0]
    assert video["inject_hdr_meta"] and video["max_cll"] == "1500,300"
    video = _resolve({"video": {"default": {"codec": "libx265", "static_hdr": {"estimate": "fast"}}}}).video[0]
    assert video["static_hdr_metadata_analysis_request"] == "fast"


def test_auto_crop_is_measured_into_the_plan():
    seen: dict[str, Any] = {}

    def detect(path, **kwargs):
        seen.update(path=path, **kwargs)
        return 140, 140, 0, 0

    video = _resolve({"video": {"default": {"codec": "libx265", "crop": {"auto": True}}}},
                     detect_crop=detect).video[0]
    assert video["crop"]["enabled"] and video["crop"]["top"] == 140 and not video["crop"]["auto"]
    assert seen["dimensions"] == (3840, 2160) and seen["stream_index"] == 0


def test_blocks_enable_their_feature_on_presence():
    video = _resolve({"video": {"default": {"codec": "libx265", "restoration": {"level": "fort"},
                                            "interpolation": {"factor": 2}}}}).video[0]
    assert video["restoration"]["enabled"] and video["restoration"]["level"] == "fort"
    assert video["interpolation"]["enabled"] and video["interpolation"]["factor"] == 2


def test_audio_tracks_selection_bitrate_and_variants():
    variant = TrackEntry(1, "audio", "EAC3", "7.1  640 kbps", "fra", "", entry_id="var", is_new=True)
    resolved = _resolve({"video": {"default": {"codec": "libx265"}},
                         "audio": [{"select": {"stream": 1}, "codec": "aac"}]}, variant=variant)
    by_entry = {a["entry_id"]: a for a in resolved.audio}
    assert by_entry["a0"]["codec"] == "aac" and by_entry["a0"]["bitrate_kbps"] > 0
    assert by_entry["var"]["codec"] == "eac3" and by_entry["var"]["bitrate_kbps"] == 640
    with pytest.raises(JobError, match="audio"):
        _resolve({"audio": [{"select": {"stream": 9}, "codec": "aac"}]})
    with pytest.raises(JobError, match="codec audio"):
        _resolve({"audio": [{"select": {"stream": 1}, "codec": "mp3"}]})


def test_unknown_video_track_is_refused():
    with pytest.raises(JobError, match="piste vidéo"):
        _resolve({"video": {"tracks": [{"select": {"source": 1}, "codec": "libx265"}]}})


# --- Options courtes du CLI ----------------------------------------------------------------

def _args(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    add_encode_options(parser)
    return parser.parse_args(list(argv))


def test_cli_shortcuts_build_the_block():
    block = encode_block_from_args(_args(
        "--video-codec", "libx265", "--crf", "20", "--crop", "140,140,0,0", "--resize", "1920x1080",
        "--denoise", "mvo-r2:fort", "--interpolate", "x2.5", "--dv", "copy", "--audio-codec", "0:1=eac3:640",
        "--video-set", "restoration.mode=fast", "--target-size", "4000", "--encode-preset", "P"))
    assert block is not None
    video = block["video"]["default"]
    assert video["crop"] == {"unit": "px", "top": 140, "bottom": 140, "left": 0, "right": 0}
    assert video["resize"] == {"mode": "size", "width": 1920, "height": 1080}
    assert video["restoration"] == {"enabled": True, "level": "fort", "mode": "fast"}
    assert video["interpolation"] == {"factor": 2.5} and video["dolby_vision"] == {"mode": "copy"}
    assert block["audio"] == [{"select": {"source": 0, "stream": 1}, "codec": "eac3", "bitrate_kbps": 640}]
    assert block["target_size_mb"] == 4000 and block["preset"] == "P"
    EncodeBlock.from_dict(block)  # contrat respecté


def test_cli_shortcuts_target_one_track_and_override_the_job():
    job = {"encode": {"video": {"default": {"codec": "libx264", "crf": 18}},
                      "audio": [{"select": {"stream": 2}, "codec": "aac"}]}}
    merged = apply_encode_options(job, _args("--crf", "22", "--video-track", "0:0", "--audio-codec", "0:1=flac"))
    assert merged["encode"]["video"]["default"] == {"codec": "libx264", "crf": 18}
    assert merged["encode"]["video"]["tracks"] == [{"select": {"source": 0, "stream": 0}, "crf": 22}]
    assert len(merged["encode"]["audio"]) == 2 and job["encode"]["audio"] == [{"select": {"stream": 2}, "codec": "aac"}]
    assert apply_encode_options({"a": 1}, _args()) == {"a": 1}


@pytest.mark.parametrize("argv", [["--crop", "1,2"], ["--audio-codec", "1=eac3"], ["--video-track", "0:0"],
                                  ["--denoise", "magie"], ["--video-set", "sans-egal"]])
def test_cli_shortcuts_reject_malformed_values(argv):
    with pytest.raises(JobError):
        encode_block_from_args(_args(*argv))


def test_manual_static_hdr_on_an_sdr_source_is_kept():
    md = "G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,50)"
    video = _resolve({"video": {"default": {"codec": "libx265", "static_hdr": {
        "mode": "manual", "master_display": md, "max_cll": "1000,400"}}}}, HDRType.NONE).video[0]
    assert video["inject_hdr_meta"] and video["master_display"] == md and video["max_cll"] == "1000,400"
    inherited = _resolve({"video": {"default": {"codec": "libx265", "static_hdr": {"mode": "source"}}}},
                         HDRType.NONE).video[0]
    assert not inherited["inject_hdr_meta"]  # rien à recopier d'une source SDR
