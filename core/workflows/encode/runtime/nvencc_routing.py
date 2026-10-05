"""NVEncC input timing and routing helpers."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from core.workflows.encode.models import EncodeConfig, EncodeError, VideoEncodeSettings


FALLBACK_HEVC_FRAME_RATE = "24000/1001"
RAW_VIDEO_SUFFIXES = {".hevc", ".h265", ".265", ".x265", ".h264", ".264", ".avc", ".ivf"}
RAW_HEVC_SUFFIXES = {".hevc", ".h265", ".265", ".x265"}


@dataclass(frozen=True)
class NvenccInputRouting:
    input_path: Path
    stream_index: int
    video: VideoEncodeSettings
    input_reader: str | None = None
    input_fps: str | None = None
    input_avsync: str | None = None
    dovi_rpu_prm: str | None = None
    rebased_to_source: bool = False
    forced_reader: str | None = None
    source_fps: str | None = None
    vpp_pad: tuple[int, int, int, int] | None = None
    needs_rpu_alignment: bool = False
    pad_offsets: tuple[int, int, int, int] | None = None
    crop_offsets: tuple[int, int, int, int] | None = None
    #: Image source (L×H) pour un redimensionnement natif au ratio conservé.
    source_dimensions: tuple[int, int] | None = None


@dataclass(frozen=True)
class NvenccRoutingCallbacks:
    primary_video_settings: Callable[[EncodeConfig], VideoEncodeSettings]
    video_source_path: Callable[[EncodeConfig], Path]
    video_stream_index: Callable[[EncodeConfig], int]
    video_codec_of: Callable[[Path, int], str]
    source_video_fps_expr: Callable[[Path], str]
    source_is_vfr: Callable[[Path], bool]
    nvencc_input_fps_hint: Callable[[Path, Path | str | None], str | None]
    nvencc_input_avsync_mode: Callable[[Path, Path | str | None], str | None]
    nvencc_dovi_rpu_prm: Callable[[VideoEncodeSettings], str | None]
    source_video_dimensions: Callable[[Path], tuple[int, int]] | None = None
    probe_dovi_l5_offsets: Callable[[Path], tuple[int, int, int, int] | None] | None = None


def normalize_frame_rate_expr(value: object) -> str | None:
    raw = str(value or "").strip()
    if raw in {"", "0", "0/0", "N/A"}:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", raw):
        return raw
    if re.fullmatch(r"\d+/\d+", raw):
        return raw
    return None


def fps_expr_to_float(value: object) -> float | None:
    raw = str(value or "").strip()
    if raw in {"", "0", "0/0", "N/A"}:
        return None
    if "/" in raw:
        try:
            num_s, den_s = raw.split("/", 1)
            num, den = float(num_s), float(den_s)
            if den == 0:
                return None
            return num / den
        except ValueError:
            return None
    try:
        return float(raw)
    except ValueError:
        return None


def mediainfo_video_fps_expr(
    source: Path,
    *,
    load_mediainfo_video_track: Callable[[Path], dict | None],
) -> str | None:
    track = load_mediainfo_video_track(source)
    if not isinstance(track, dict):
        return None

    num = normalize_frame_rate_expr(track.get("FrameRate_Num"))
    den = normalize_frame_rate_expr(track.get("FrameRate_Den"))
    if num is not None and den is not None:
        try:
            if float(den) != 0:
                return f"{int(float(num))}/{int(float(den))}"
        except (TypeError, ValueError):
            pass

    for key in ("FrameRate_Original", "FrameRate", "FrameRate_Nominal"):
        raw = str(track.get(key) or "").strip().replace(",", ".")
        if not raw:
            continue
        fraction = re.search(r"\b(\d+/\d+)\b", raw)
        if fraction is not None:
            fps_expr = normalize_frame_rate_expr(fraction.group(1))
            if fps_expr is not None:
                return fps_expr
        decimal = re.search(r"\b(\d+(?:\.\d+)?)\b", raw)
        if decimal is not None:
            fps_expr = normalize_frame_rate_expr(decimal.group(1))
            if fps_expr is not None:
                return fps_expr
    return None


def mediainfo_video_is_vfr(
    source: Path,
    *,
    load_mediainfo_video_track: Callable[[Path], dict | None],
) -> bool | None:
    track = load_mediainfo_video_track(source)
    if not isinstance(track, dict):
        return None
    for key in ("FrameRate_Mode_Original", "FrameRate_Mode"):
        raw = str(track.get(key) or "").strip().lower()
        if not raw:
            continue
        if "vfr" in raw or "variable" in raw:
            return True
        if "cfr" in raw or "constant" in raw:
            return False
    return None


def source_video_fps_expr(
    source: Path,
    *,
    stream_index: int | None = None,
    ffprobe_streams_payload: Callable[[Path], dict[str, object] | None],
    ffprobe_stream_dicts: Callable[[dict[str, object]], list[dict[str, object]]],
    mediainfo_fps_expr: Callable[[Path], str | None],
) -> str:
    """Cadence du flux vidéo ``stream_index`` (premier flux vidéo si absent ou introuvable)."""
    payload = ffprobe_streams_payload(source)
    if payload is not None:
        videos = [stream for stream in ffprobe_stream_dicts(payload) if stream.get("codec_type") == "video"]
        selected = next((stream for stream in videos if stream.get("index") == stream_index), None)
        stream = selected if selected is not None else (videos[0] if videos else None)
        if stream is not None:
            for key in ("avg_frame_rate", "r_frame_rate"):
                fps_expr = normalize_frame_rate_expr(stream.get(key))
                if fps_expr is not None:
                    return fps_expr
    return mediainfo_fps_expr(source) or FALLBACK_HEVC_FRAME_RATE


def nvencc_raw_input_needs_fps_hint(path: Path | str | None) -> bool:
    if path is None:
        return False
    return Path(path).suffix.lower() in RAW_VIDEO_SUFFIXES


def nvencc_can_use_native_timestamps(path: Path | str | None) -> bool:
    return path is not None and not nvencc_raw_input_needs_fps_hint(path)


def nvencc_crop_offsets_from_extra_params(extra_params: str) -> tuple[int, int, int, int] | None:
    raw = (extra_params or "").strip()
    if not raw:
        return None
    try:
        tokens = shlex.split(raw)
    except ValueError:
        tokens = raw.split()

    i = 0
    while i < len(tokens):
        token = tokens[i]
        value: str | None = None
        if token.startswith("--crop="):
            value = token.split("=", 1)[1]
            i += 1
        elif token == "--crop":
            value = tokens[i + 1] if i + 1 < len(tokens) else None
            i += 2
        else:
            i += 1
            continue
        if not value:
            return None
        parts = [part.strip() for part in value.split(",")]
        if len(parts) != 4:
            return None
        try:
            return tuple(int(part) for part in parts)  # type: ignore[return-value]
        except ValueError:
            return None
    return None


def nvencc_dovi_rpu_prm(video: VideoEncodeSettings) -> str | None:
    if not getattr(video, "copy_dv", False):
        return None
    crop = nvencc_crop_offsets_from_extra_params(video.extra_params)
    if crop is not None and any(component != 0 for component in crop):
        return "crop=true"
    crop_obj = getattr(video, "crop", None)
    if crop_obj is not None and getattr(crop_obj, "is_active", lambda: False)():
        if not getattr(crop_obj, "auto", False) and getattr(crop_obj, "unit", "") != "percent":
            left = max(0, int(getattr(crop_obj, "left", 0) or 0))
            top = max(0, int(getattr(crop_obj, "top", 0) or 0))
            right = max(0, int(getattr(crop_obj, "right", 0) or 0))
            bottom = max(0, int(getattr(crop_obj, "bottom", 0) or 0))
            if any(c != 0 for c in (left, top, right, bottom)):
                return "crop=true"
    return None


def source_video_dimensions(
    source: Path,
    *,
    stream_index: int | None = None,
    ffprobe_streams_payload: Callable[[Path], dict[str, object] | None],
    ffprobe_stream_dicts: Callable[[dict[str, object]], list[dict[str, object]]],
) -> tuple[int, int]:
    payload = ffprobe_streams_payload(source)
    if payload is None:
        return (0, 0)
    for stream in ffprobe_stream_dicts(payload):
        if stream.get("codec_type") != "video":
            continue
        if stream_index is not None and stream.get("index") != stream_index:
            continue
        try:
            width = stream.get("width")
            height = stream.get("height")
            w = int(width) if isinstance(width, (int, str)) else 0
            h = int(height) if isinstance(height, (int, str)) else 0
        except (TypeError, ValueError):
            return (0, 0)
        return (w, h)
    return (0, 0)


def source_is_vfr(
    source: Path,
    *,
    ffprobe_streams_payload: Callable[[Path], dict[str, object] | None],
    ffprobe_stream_dicts: Callable[[dict[str, object]], list[dict[str, object]]],
    mediainfo_is_vfr: Callable[[Path], bool | None],
    tolerance: float = 0.01,
) -> bool:
    payload = ffprobe_streams_payload(source)
    if payload is not None:
        for stream in ffprobe_stream_dicts(payload):
            if stream.get("codec_type") != "video":
                continue
            r = fps_expr_to_float(stream.get("r_frame_rate"))
            a = fps_expr_to_float(stream.get("avg_frame_rate"))
            if r is None or a is None or r <= 0 or a <= 0:
                break
            return abs(r - a) / r > tolerance
    return bool(mediainfo_is_vfr(source))


class NvenccInputRouter:
    def __init__(self, callbacks: NvenccRoutingCallbacks) -> None:
        self._cb = callbacks

    def resolve(self, config: EncodeConfig) -> NvenccInputRouting:
        video = self._cb.primary_video_settings(config)
        input_path = self._cb.video_source_path(config)
        stream_index = self._cb.video_stream_index(config)
        dynamic_hdr_copy = bool(video.copy_dv or video.copy_hdr10plus)
        rebased_to_source = False
        forced_reader: str | None = None
        input_reader: str | None = None

        if dynamic_hdr_copy:
            original_source = Path(config.source)
            if (
                nvencc_raw_input_needs_fps_hint(input_path)
                and input_path != original_source
                and nvencc_can_use_native_timestamps(original_source)
            ):
                input_path = original_source
                stream_index = self._cb.video_stream_index(config)
                rebased_to_source = True
            elif not nvencc_can_use_native_timestamps(input_path):
                input_reader = "avsw"
                forced_reader = "avsw"

            if video.copy_dv:
                codec_name = ""
                if nvencc_raw_input_needs_fps_hint(input_path):
                    if Path(input_path).suffix.lower() in RAW_HEVC_SUFFIXES:
                        codec_name = "hevc"
                    else:
                        codec_name = "unsupported-raw"
                else:
                    codec_name = self._cb.video_codec_of(Path(input_path), stream_index)
                if codec_name not in {"", "hevc"}:
                    raise EncodeError(
                        "NVEncC --dolby-vision-rpu copy exige une entrée vidéo HEVC "
                        "lisible depuis la source d'origine ou via avsw."
                    )

        routed_video = video
        if video.copy_dv and str(video.dovi_profile or "").strip().lower() in {"", "0", "copy"}:
            routed_video = replace(video, dovi_profile="8.1")

        vpp_pad = None
        needs_rpu_alignment = False
        pad_offsets = None
        crop_offsets = None
        dovi_rpu_prm = self._cb.nvencc_dovi_rpu_prm(routed_video)

        dims = (0, 0)
        needs_dims = (video.copy_dv and video.codec == "nvencc_hevc") or video.resize.is_active()
        if needs_dims and self._cb.source_video_dimensions is not None:
            try:
                dims = self._cb.source_video_dimensions(Path(input_path))
                if dims == (0, 0) and Path(input_path) != Path(config.source):
                    dims = self._cb.source_video_dimensions(Path(config.source))
            except Exception:
                dims = (0, 0)

        if video.copy_dv and video.codec == "nvencc_hevc":
            from core.workflows.encode.runtime.dovi_geometry import (
                align_nvencc_dovi_geometry, nvencc_dovi_resize_changes_scale,
            )

            l5 = None
            resamples = nvencc_dovi_resize_changes_scale(routed_video, dims)
            if not resamples and self._cb.probe_dovi_l5_offsets is not None:
                try:
                    l5 = self._cb.probe_dovi_l5_offsets(Path(input_path))
                    if l5 is None and Path(input_path) != Path(config.source):
                        l5 = self._cb.probe_dovi_l5_offsets(Path(config.source))
                except Exception:
                    l5 = None

            if resamples:
                routed_video = replace(routed_video, copy_dv=False, inject_hdr_meta=True)
                dovi_rpu_prm = None
            elif dims != (0, 0):
                geom = align_nvencc_dovi_geometry(routed_video, dims, l5_offsets=l5)
                routed_video = geom.video
                vpp_pad = geom.vpp_pad
                dovi_rpu_prm = geom.dovi_rpu_prm
                needs_rpu_alignment = geom.needs_rpu_alignment
                pad_offsets = geom.pad_offsets
                crop_offsets = geom.crop_offsets

        source_for_timing = Path(input_path)
        source_fps: str | None = None
        try:
            source_fps = self._cb.source_video_fps_expr(source_for_timing)
        except Exception:
            source_fps = None

        return NvenccInputRouting(
            input_path=Path(input_path),
            stream_index=int(stream_index),
            video=routed_video,
            input_reader=input_reader,
            input_fps=(
                None
                if dynamic_hdr_copy
                else self._cb.nvencc_input_fps_hint(source_for_timing, input_path)
            ),
            input_avsync=self._cb.nvencc_input_avsync_mode(source_for_timing, input_path),
            dovi_rpu_prm=dovi_rpu_prm,
            rebased_to_source=rebased_to_source,
            forced_reader=forced_reader,
            source_fps=source_fps,
            vpp_pad=vpp_pad,
            needs_rpu_alignment=needs_rpu_alignment,
            pad_offsets=pad_offsets,
            crop_offsets=crop_offsets,
            source_dimensions=dims if dims != (0, 0) else None,
        )
