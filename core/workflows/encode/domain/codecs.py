"""Codec domain helpers extracted from EncodeWorkflow."""

from __future__ import annotations

from dataclasses import dataclass

from core.workflows.encode.catalog import (
    AMF_VIDEO_CODECS,
    NVENC_VIDEO_CODECS,
    QSV_VIDEO_CODECS,
    VAAPI_VIDEO_CODECS,
    is_h264_video_codec,
)
from core.workflows.encode.models import (
    AudioTrackSettings,
    QualityMode,
    VideoEncodeSettings,
    normalize_audio_bitrate_kbps,
)


@dataclass(frozen=True)
class EncodeCodecDomainCallbacks:
    platform: str
    vaapi_device: str | None = None
    qsv_device: str | None = None
    amf_device: str | None = None
    nvenc_device: str | None = None


def force_h264_8bit(video: VideoEncodeSettings) -> bool:
    return bool(getattr(video, "force_8bit", False)) and is_h264_video_codec(video.codec)


def h264_8bit_pix_fmt_args(video: VideoEncodeSettings) -> list[str]:
    if not force_h264_8bit(video):
        return []
    if video.codec == "libx264":
        return ["-pix_fmt", "yuv420p"]
    return ["-pix_fmt", "nv12"]


def x265_params(video: VideoEncodeSettings) -> str:
    parts: list[str] = []
    if video.extra_params:
        parts.append(video.extra_params.strip(":"))
    if video.inject_hdr_meta and not video.tonemap_to_sdr:
        if video.master_display:
            parts.append(f"master-display={video.master_display}")
        if video.max_cll:
            parts.append(f"max-cll={video.max_cll}")
    return ":".join(part for part in parts if part)


def video_codec_args(video: VideoEncodeSettings, bitrate_kbps: int, *, callbacks: EncodeCodecDomainCallbacks) -> list[str]:
    if video.quality_mode == QualityMode.CRF:
        return video_codec_args_crf(video, callbacks=callbacks)
    return video_codec_args_bitrate(video, bitrate_kbps, callbacks=callbacks)


def video_codec_args_crf(video: VideoEncodeSettings, *, callbacks: EncodeCodecDomainCallbacks) -> list[str]:
    match video.codec:
        case "copy":
            return ["-c:v", "copy"]
        case "libx265":
            args = ["-c:v", "libx265", "-crf", str(video.crf), "-preset", video.preset]
            x265 = x265_params(video)
            if x265:
                args.extend(["-x265-params", x265])
            return args
        case "libx264":
            return [
                "-c:v", "libx264", "-crf", str(video.crf), "-preset", video.preset,
                *h264_8bit_pix_fmt_args(video),
            ]
        case "libsvtav1":
            args = ["-c:v", "libsvtav1", "-crf", str(video.crf), "-preset", video.preset]
            if video.extra_params:
                args.extend(["-svtav1-params", video.extra_params])
            return args
        case "hevc_nvenc":
            return [
                "-c:v", "hevc_nvenc", "-rc:v", "vbr", "-cq:v", str(video.crf), "-preset:v", video.preset,
                *nvenc_device_args(callbacks),
            ]
        case "hevc_amf":
            args = ["-c:v", "hevc_amf", "-rc", "cqp", "-qp_p", str(video.crf), "-qp_i", str(video.crf)]
            if video.preset:
                args.extend(["-quality", video.preset])
            return args
        case "hevc_qsv":
            args = ["-c:v", "hevc_qsv", "-global_quality", str(video.crf), "-look_ahead", "1", "-async_depth", "4"]
            if video.preset:
                args.extend(["-preset", video.preset])
            return args
        case "hevc_vaapi":
            return [
                "-c:v", "hevc_vaapi", "-rc_mode", "CQP", "-qp", str(video.crf),
                "-compression_level", (video.preset or "4"),
                "-async_depth", "4",
            ]
        case "h264_nvenc":
            return [
                "-c:v", "h264_nvenc", "-rc:v", "vbr", "-cq:v", str(video.crf), "-preset:v", video.preset,
                *nvenc_device_args(callbacks),
                *h264_8bit_pix_fmt_args(video),
            ]
        case "h264_amf":
            args = ["-c:v", "h264_amf", "-rc", "cqp", "-qp_p", str(video.crf), "-qp_i", str(video.crf)]
            if video.preset:
                args.extend(["-quality", video.preset])
            args.extend(h264_8bit_pix_fmt_args(video))
            return args
        case "h264_qsv":
            args = ["-c:v", "h264_qsv", "-global_quality", str(video.crf), "-async_depth", "4"]
            if video.preset:
                args.extend(["-preset", video.preset])
            args.extend(h264_8bit_pix_fmt_args(video))
            return args
        case "h264_vaapi":
            return [
                "-c:v", "h264_vaapi", "-rc_mode", "CQP", "-qp", str(video.crf),
                "-compression_level", (video.preset or "4"),
                "-async_depth", "4",
                *h264_8bit_pix_fmt_args(video),
            ]
        case "av1_nvenc":
            return [
                "-c:v", "av1_nvenc", "-rc:v", "vbr", "-cq:v", str(video.crf), "-preset:v", video.preset,
                *nvenc_device_args(callbacks),
            ]
        case "av1_amf":
            args = ["-c:v", "av1_amf", "-rc", "cqp", "-qp_p", str(video.crf), "-qp_i", str(video.crf)]
            if video.preset:
                args.extend(["-quality", video.preset])
            return args
        case "av1_qsv":
            args = ["-c:v", "av1_qsv", "-global_quality", str(video.crf), "-async_depth", "4"]
            if video.preset:
                args.extend(["-preset", video.preset])
            return args
        case "av1_vaapi":
            return [
                "-c:v", "av1_vaapi", "-rc_mode", "CQP", "-qp", str(video.crf),
                "-compression_level", (video.preset or "4"),
                "-async_depth", "4",
            ]
        case _:
            return ["-c:v", video.codec, "-crf", str(video.crf)]


def video_codec_args_bitrate(
    video: VideoEncodeSettings,
    bitrate_kbps: int,
    *,
    callbacks: EncodeCodecDomainCallbacks,
) -> list[str]:
    match video.codec:
        case "copy":
            return ["-c:v", "copy"]
        case "libx265":
            args = ["-c:v", "libx265", "-b:v", f"{bitrate_kbps}k", "-preset", video.preset]
            x265 = x265_params(video)
            if x265:
                args.extend(["-x265-params", x265])
            return args
        case "libx264":
            return [
                "-c:v", "libx264", "-b:v", f"{bitrate_kbps}k", "-preset", video.preset,
                *h264_8bit_pix_fmt_args(video),
            ]
        case "libsvtav1":
            args = ["-c:v", "libsvtav1", "-b:v", f"{bitrate_kbps}k", "-preset", video.preset]
            if video.extra_params:
                args.extend(["-svtav1-params", video.extra_params])
            return args
        case "hevc_nvenc":
            return [
                "-c:v", "hevc_nvenc", "-b:v", f"{bitrate_kbps}k", "-preset:v", video.preset,
                *nvenc_device_args(callbacks),
            ]
        case "hevc_amf":
            args = ["-c:v", "hevc_amf", "-b:v", f"{bitrate_kbps}k"]
            if video.preset:
                args.extend(["-quality", video.preset])
            return args
        case "hevc_qsv":
            args = ["-c:v", "hevc_qsv", "-b:v", f"{bitrate_kbps}k", "-async_depth", "4"]
            if video.preset:
                args.extend(["-preset", video.preset])
            return args
        case "hevc_vaapi":
            return [
                "-c:v", "hevc_vaapi", "-rc_mode", "VBR", "-b:v", f"{bitrate_kbps}k",
                "-compression_level", (video.preset or "4"),
                "-async_depth", "4",
            ]
        case "h264_nvenc":
            return [
                "-c:v", "h264_nvenc", "-b:v", f"{bitrate_kbps}k", "-preset:v", video.preset,
                *nvenc_device_args(callbacks),
                *h264_8bit_pix_fmt_args(video),
            ]
        case "h264_amf":
            args = ["-c:v", "h264_amf", "-b:v", f"{bitrate_kbps}k"]
            if video.preset:
                args.extend(["-quality", video.preset])
            args.extend(h264_8bit_pix_fmt_args(video))
            return args
        case "h264_qsv":
            args = ["-c:v", "h264_qsv", "-b:v", f"{bitrate_kbps}k", "-async_depth", "4"]
            if video.preset:
                args.extend(["-preset", video.preset])
            args.extend(h264_8bit_pix_fmt_args(video))
            return args
        case "h264_vaapi":
            return [
                "-c:v", "h264_vaapi", "-rc_mode", "VBR", "-b:v", f"{bitrate_kbps}k",
                "-compression_level", (video.preset or "4"),
                "-async_depth", "4",
                *h264_8bit_pix_fmt_args(video),
            ]
        case "av1_nvenc":
            return [
                "-c:v", "av1_nvenc", "-b:v", f"{bitrate_kbps}k", "-preset:v", video.preset,
                *nvenc_device_args(callbacks),
            ]
        case "av1_amf":
            args = ["-c:v", "av1_amf", "-b:v", f"{bitrate_kbps}k"]
            if video.preset:
                args.extend(["-quality", video.preset])
            return args
        case "av1_qsv":
            args = ["-c:v", "av1_qsv", "-b:v", f"{bitrate_kbps}k", "-async_depth", "4"]
            if video.preset:
                args.extend(["-preset", video.preset])
            return args
        case "av1_vaapi":
            return [
                "-c:v", "av1_vaapi", "-rc_mode", "VBR", "-b:v", f"{bitrate_kbps}k",
                "-compression_level", (video.preset or "4"),
                "-async_depth", "4",
            ]
        case _:
            return ["-c:v", video.codec, "-b:v", f"{bitrate_kbps}k"]


def build_encoder_vf(video: VideoEncodeSettings, *, callbacks: EncodeCodecDomainCallbacks) -> str:
    vf = build_vf(video)
    force_8bit = force_h264_8bit(video)
    if video.codec not in VAAPI_VIDEO_CODECS:
        if (
            callbacks.platform == "win32"
            and video.codec in AMF_VIDEO_CODECS
            and callbacks.amf_device is not None
            and (video.tonemap_to_sdr or force_8bit)
        ):
            amf_upload = "format=nv12,hwupload"
            if force_8bit:
                if vf and "format=yuv420p" not in {part.strip() for part in vf.split(",")}:
                    vf = f"{vf},format=yuv420p"
                elif not vf:
                    vf = "format=yuv420p"
            return f"{vf},{amf_upload}" if vf else amf_upload
        if force_8bit:
            force_8bit_filter = "format=yuv420p"
            if vf and force_8bit_filter in {part.strip() for part in vf.split(",")}:
                return vf
            return f"{vf},{force_8bit_filter}" if vf else force_8bit_filter
        return vf
    if video.tonemap_to_sdr or force_8bit:
        vaapi_upload = "format=nv12,hwupload"
        return f"{vf},{vaapi_upload}" if vf else vaapi_upload
    return vf


def build_vf(video: VideoEncodeSettings) -> str:
    if not video.tonemap_to_sdr:
        return ""
    algo = video.tonemap_algorithm or "hable"
    return (
        "zscale=transfer=linear:npl=100,"
        "format=gbrpf32le,"
        "zscale=primaries=bt709,"
        f"tonemap=tonemap={algo}:desat=0,"
        "zscale=transfer=bt709:matrix=bt709:range=tv,"
        "format=yuv420p"
    )


def hardware_input_args(video: VideoEncodeSettings, *, callbacks: EncodeCodecDomainCallbacks) -> list[str]:
    args: list[str] = []
    tonemap = bool(video.tonemap_to_sdr)
    force_8bit = force_h264_8bit(video)

    if video.codec in VAAPI_VIDEO_CODECS:
        if callbacks.vaapi_device:
            args.extend(["-vaapi_device", callbacks.vaapi_device])
            if not tonemap and not force_8bit:
                args.extend(["-hwaccel", "vaapi", "-hwaccel_output_format", "vaapi"])
        return args

    if video.codec in QSV_VIDEO_CODECS:
        if callbacks.qsv_device:
            args.extend(["-qsv_device", callbacks.qsv_device])
        if tonemap or force_8bit:
            return args
        args.extend(["-hwaccel", "qsv", "-hwaccel_output_format", "qsv"])
        return args

    if video.codec in AMF_VIDEO_CODECS and callbacks.platform == "win32":
        if callbacks.amf_device:
            args.extend([
                "-init_hw_device", f"d3d11va=mre_amf:{callbacks.amf_device}",
                "-filter_hw_device", "mre_amf",
            ])
        if tonemap or force_8bit:
            return args
        if callbacks.amf_device:
            args.extend([
                "-hwaccel", "d3d11va",
                "-hwaccel_device", "mre_amf",
                "-hwaccel_output_format", "d3d11",
            ])
        else:
            args.extend(["-hwaccel", "d3d11va", "-hwaccel_output_format", "d3d11"])
        return args

    if tonemap or force_8bit:
        return args

    if video.codec in NVENC_VIDEO_CODECS:
        if callbacks.platform == "win32" and callbacks.nvenc_device:
            args.extend(["-hwaccel_device", callbacks.nvenc_device])
        args.extend(["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"])

    return args


def nvenc_device_args(callbacks: EncodeCodecDomainCallbacks) -> list[str]:
    if callbacks.platform != "win32" or not callbacks.nvenc_device:
        return []
    return ["-gpu", callbacks.nvenc_device]


def hdr_meta_args(video: VideoEncodeSettings) -> list[str]:
    if video.codec in ("copy", "libx264", "h264_nvenc", "h264_amf", "h264_qsv"):
        return []
    args = ["-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc"]
    if video.codec == "hevc_nvenc":
        if video.master_display:
            args.extend(["-master_display", video.master_display])
        if video.max_cll:
            args.extend(["-max_cll", video.max_cll])
    return args


def audio_codec_args(out_idx: int, audio: AudioTrackSettings) -> list[str]:
    args: list[str] = []
    needs_downmix = needs_ac3_51_downmix(audio)
    bitrate_kbps = normalize_audio_bitrate_kbps(
        audio.codec,
        audio.bitrate_kbps,
        audio.input_channels,
        None,
        audio.input_channel_layout,
    )
    match audio.codec:
        case "copy":
            args.extend([f"-c:a:{out_idx}", "copy"])
            if audio.extract_truehd_core:
                args.extend([f"-bsf:a:{out_idx}", "truehd_core"])
        case "aac":
            args.extend([f"-c:a:{out_idx}", "aac", f"-b:a:{out_idx}", f"{bitrate_kbps}k"])
        case "ac3":
            args.extend([f"-c:a:{out_idx}", "ac3", f"-b:a:{out_idx}", f"{bitrate_kbps}k"])
        case "eac3":
            args.extend([f"-c:a:{out_idx}", "eac3", f"-b:a:{out_idx}", f"{bitrate_kbps}k"])
        case "flac":
            args.extend([f"-c:a:{out_idx}", "flac"])
        case _:
            args.extend([f"-c:a:{out_idx}", audio.codec])
    if needs_downmix:
        args.extend([f"-ac:a:{out_idx}", "6", f"-channel_layout:a:{out_idx}", "5.1"])
    return args


def needs_ac3_51_downmix(audio: AudioTrackSettings) -> bool:
    if audio.codec not in {"ac3", "eac3"}:
        return False
    if (audio.input_channels or 0) >= 8:
        return True
    layout = (audio.input_channel_layout or "").lower()
    return "7.1" in layout

