"""HDR probing and static metadata helpers for encode workflows."""

from __future__ import annotations

import json
import re
import subprocess
from collections import OrderedDict
from pathlib import Path
from typing import Callable, cast

from core.subprocess_utils import subprocess_text_kwargs


class _LRUCache(OrderedDict):
    """Small bounded cache used for repeated preview probes."""

    def __init__(self, maxsize: int = 256) -> None:
        super().__init__()
        self._maxsize = maxsize

    def __setitem__(self, key, value) -> None:
        if key in self:
            self.move_to_end(key)
        super().__setitem__(key, value)
        while len(self) > self._maxsize:
            self.popitem(last=False)


class HdrMetadataProbeService:
    """Probe dynamic HDR presence and static HDR metadata with local caching."""

    _MASTER_DISPLAY_PRIMARIES: dict[str, tuple[tuple[float, float], ...]] = {
        "bt.2020":    ((0.170, 0.797), (0.131, 0.046), (0.708, 0.292), (0.3127, 0.3290)),
        "display p3": ((0.265, 0.690), (0.150, 0.060), (0.680, 0.320), (0.3127, 0.3290)),
        "p3-d65":     ((0.265, 0.690), (0.150, 0.060), (0.680, 0.320), (0.3127, 0.3290)),
        "bt.709":     ((0.300, 0.600), (0.150, 0.060), (0.640, 0.330), (0.3127, 0.3290)),
    }

    DEFAULT_MAX_CLL = "1000,400"

    def __init__(
        self,
        *,
        ffmpeg_bin: Callable[[], str],
        tool_bin: Callable[[str], str],
    ) -> None:
        self._ffmpeg_bin = ffmpeg_bin
        self._tool_bin = tool_bin
        self._ffprobe_payload_cache: _LRUCache = _LRUCache(maxsize=256)
        self._ffprobe_frame_hdr_cache: _LRUCache = _LRUCache(maxsize=256)
        self._mediainfo_hdr_cache: _LRUCache = _LRUCache(maxsize=256)

    @staticmethod
    def ffprobe_bin_from_ffmpeg(ffmpeg_bin: str) -> str:
        ffmpeg_path = Path(ffmpeg_bin)
        name = ffmpeg_path.name.lower()
        if name in {"ffmpeg", "ffmpeg.exe"}:
            return str(ffmpeg_path.with_name("ffprobe" + ffmpeg_path.suffix))
        return "ffprobe"

    def load_mediainfo_video_track(self, path: Path) -> dict | None:
        mediainfo_bin = self._tool_bin("mediainfo")
        try:
            result = subprocess.run(
                [mediainfo_bin, "--Output=JSON", str(path)],
                capture_output=True,
                check=False,
                **subprocess_text_kwargs(),
            )
        except (FileNotFoundError, OSError):
            return None
        if result.returncode != 0:
            return None
        try:
            data = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return None
        media = data.get("media") or {}
        for track in media.get("track") or []:
            if isinstance(track, dict) and track.get("@type") == "Video":
                return track
        return None

    def detect_source_dynamic_hdr_presence(
        self,
        source: Path,
        *,
        ffprobe_streams_payload: Callable[[Path], dict[str, object] | None] | None = None,
        ffprobe_stream_dicts: Callable[[dict[str, object]], list[dict[str, object]]] | None = None,
        mediainfo_hdr_flags: Callable[[Path], tuple[bool, bool] | None] | None = None,
        ffprobe_frame_dynamic_hdr_flags: Callable[[Path], tuple[bool, bool] | None] | None = None,
    ) -> tuple[bool, bool] | None:
        payload_fn = ffprobe_streams_payload or self.ffprobe_streams_payload
        stream_dicts_fn = ffprobe_stream_dicts or self.ffprobe_stream_dicts
        mediainfo_fn = mediainfo_hdr_flags or self.mediainfo_hdr_flags
        frame_flags_fn = ffprobe_frame_dynamic_hdr_flags or self.ffprobe_frame_dynamic_hdr_flags

        payload = payload_fn(source)
        has_dv = False
        has_hdr10plus = False
        frame_flags: tuple[bool, bool] | None = None
        if payload is not None:
            for stream in stream_dicts_fn(payload):
                if stream.get("codec_type") != "video":
                    continue
                side_data_obj = stream.get("side_data_list")
                side_data: list[dict[str, object]] = []
                if isinstance(side_data_obj, list):
                    for item in side_data_obj:
                        if isinstance(item, dict):
                            side_data.append(cast(dict[str, object], item))
                if any(sd.get("side_data_type") == "DOVI configuration record" for sd in side_data):
                    has_dv = True
                if any(
                    sd.get("side_data_type") == "HDR Dynamic Metadata SMPTE2094-40 (HDR10+)"
                    for sd in side_data
                ):
                    has_hdr10plus = True
                if has_dv and has_hdr10plus:
                    break

        mediainfo_flags = mediainfo_fn(source)
        if mediainfo_flags is not None:
            mi_dv, mi_hdr10plus = mediainfo_flags
            has_dv = has_dv or mi_dv
            has_hdr10plus = has_hdr10plus or mi_hdr10plus

        if not has_dv or not has_hdr10plus:
            frame_flags = frame_flags_fn(source)
            if frame_flags is not None:
                frame_dv, frame_hdr10plus = frame_flags
                has_dv = has_dv or frame_dv
                has_hdr10plus = has_hdr10plus or frame_hdr10plus

        if payload is None and mediainfo_flags is None and frame_flags is None:
            return None
        return has_dv, has_hdr10plus

    def ffprobe_streams_payload(self, source: Path) -> dict[str, object] | None:
        cache_key = self.source_cache_key(source)
        if cache_key is not None and cache_key in self._ffprobe_payload_cache:
            return self._ffprobe_payload_cache[cache_key]

        cmd = [
            self.ffprobe_bin_from_ffmpeg(self._ffmpeg_bin()),
            "-v", "quiet",
            "-print_format", "json",
            "-show_streams",
            str(source),
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                check=False,
                timeout=20,
                **subprocess_text_kwargs(),
            )
        except FileNotFoundError:
            payload = None
        else:
            if result.returncode != 0:
                payload = None
            else:
                try:
                    payload = json.loads(result.stdout or "{}")
                except json.JSONDecodeError:
                    payload = None

        if cache_key is not None:
            self._ffprobe_payload_cache[cache_key] = payload
        return payload

    @staticmethod
    def source_cache_key(source: Path) -> tuple[str, int, int] | None:
        try:
            st = source.stat()
        except OSError:
            return None
        return (str(source), st.st_mtime_ns, st.st_size)

    @staticmethod
    def ffprobe_stream_dicts(payload: dict[str, object]) -> list[dict[str, object]]:
        streams_obj = payload.get("streams")
        if not isinstance(streams_obj, list):
            return []
        out: list[dict[str, object]] = []
        for item in streams_obj:
            if isinstance(item, dict):
                out.append(cast(dict[str, object], item))
        return out

    def ffprobe_frame_dynamic_hdr_flags(
        self,
        source: Path,
        *,
        max_frames: int = 240,
    ) -> tuple[bool, bool] | None:
        cache_key = self.source_cache_key(source)
        if cache_key is not None and cache_key in self._ffprobe_frame_hdr_cache:
            return self._ffprobe_frame_hdr_cache[cache_key]

        cmd = [
            self.ffprobe_bin_from_ffmpeg(self._ffmpeg_bin()),
            "-v", "quiet",
            "-print_format", "json",
            "-select_streams", "v:0",
            "-read_intervals", f"%+#{max(1, int(max_frames))}",
            "-show_frames",
            "-show_entries", "frame_side_data=side_data_type",
            str(source),
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                check=False,
                timeout=30,
                **subprocess_text_kwargs(),
            )
        except FileNotFoundError:
            flags: tuple[bool, bool] | None = None
        else:
            if result.returncode != 0:
                flags = None
            else:
                try:
                    payload = json.loads(result.stdout or "{}")
                except json.JSONDecodeError:
                    flags = None
                else:
                    frames_obj = payload.get("frames")
                    has_dv = False
                    has_hdr10plus = False
                    if isinstance(frames_obj, list):
                        for frame in frames_obj:
                            if not isinstance(frame, dict):
                                continue
                            side_data_obj = frame.get("side_data_list")
                            if not isinstance(side_data_obj, list):
                                continue
                            for side_data in side_data_obj:
                                if not isinstance(side_data, dict):
                                    continue
                                side_type = str(side_data.get("side_data_type", "") or "")
                                side_type_lower = side_type.lower()
                                if ("dolby vision" in side_type_lower) or (side_type == "DOVI configuration record"):
                                    has_dv = True
                                if (
                                    "hdr dynamic metadata smpte2094-40" in side_type_lower
                                    or "hdr10+" in side_type_lower
                                    or "smpte st 2094" in side_type_lower
                                    or "smpte2094" in side_type_lower
                                ):
                                    has_hdr10plus = True
                                if has_dv and has_hdr10plus:
                                    break
                            if has_dv and has_hdr10plus:
                                break
                    flags = (has_dv, has_hdr10plus)

        if cache_key is not None:
            self._ffprobe_frame_hdr_cache[cache_key] = flags
        return flags

    def mediainfo_hdr_flags(self, source: Path) -> tuple[bool, bool] | None:
        cache_key = self.source_cache_key(source)
        if cache_key is not None and cache_key in self._mediainfo_hdr_cache:
            return self._mediainfo_hdr_cache[cache_key]

        mediainfo_bin = self._tool_bin("mediainfo")
        try:
            hdr_format = subprocess.run(
                [mediainfo_bin, "--Inform=Video;%HDR_Format%", str(source)],
                capture_output=True,
                check=False,
                timeout=20,
                **subprocess_text_kwargs(),
            )
            hdr_compat = subprocess.run(
                [mediainfo_bin, "--Inform=Video;%HDR_Format_Compatibility%", str(source)],
                capture_output=True,
                check=False,
                timeout=20,
                **subprocess_text_kwargs(),
            )
        except FileNotFoundError:
            result: tuple[bool, bool] | None = None
        else:
            hdr_text = f"{hdr_format.stdout or ''}\n{hdr_compat.stdout or ''}".lower()
            result = (
                "dolby vision" in hdr_text,
                (
                    "hdr10+" in hdr_text
                    or "smpte st 2094" in hdr_text
                    or "smpte2094" in hdr_text
                ),
            )

        if cache_key is not None:
            self._mediainfo_hdr_cache[cache_key] = result
        return result

    def build_master_display_for_primaries(self, primaries_label: str) -> str:
        primaries = self._MASTER_DISPLAY_PRIMARIES.get(primaries_label.strip().lower())
        if not primaries:
            return ""
        (gx, gy), (bx, by), (rx, ry), (wx, wy) = primaries
        c = lambda f: int(round(f * 50000))
        return (
            f"G({c(gx)},{c(gy)})"
            f"B({c(bx)},{c(by)})"
            f"R({c(rx)},{c(ry)})"
            f"WP({c(wx)},{c(wy)})"
            f"L(10000000,1)"
        )

    def color_primaries_label(self, source: Path) -> str:
        try:
            result = subprocess.run(
                [self._tool_bin("ffprobe"), "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=color_primaries",
                 "-of", "default=nw=1:nk=1", str(source)],
                capture_output=True, check=False, timeout=10,
                **subprocess_text_kwargs(),
            )
        except (FileNotFoundError, OSError):
            return ""
        return (result.stdout or "").strip().lower()

    def extract_static_hdr_via_ffprobe(self, source: Path) -> tuple[str, str]:
        try:
            result = subprocess.run(
                [self._tool_bin("ffprobe"), "-v", "error", "-select_streams", "v:0",
                 "-show_frames", "-read_intervals", "%+#1",
                 "-print_format", "json", str(source)],
                capture_output=True, check=False, timeout=20,
                **subprocess_text_kwargs(),
            )
        except (FileNotFoundError, OSError):
            return "", ""
        if result.returncode != 0:
            return "", ""
        try:
            data = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return "", ""
        frames = data.get("frames") or []
        if not frames:
            return "", ""
        side_data_list = frames[0].get("side_data_list") or []

        def _num(rat: str) -> int:
            try:
                return int(str(rat).split("/", 1)[0])
            except (ValueError, AttributeError):
                return 0

        master_display = ""
        max_cll = ""
        for sd in side_data_list:
            stype = sd.get("side_data_type") or ""
            if stype == "Mastering display metadata":
                gx, gy = _num(sd.get("green_x")), _num(sd.get("green_y"))
                bx, by = _num(sd.get("blue_x")), _num(sd.get("blue_y"))
                rx, ry = _num(sd.get("red_x")), _num(sd.get("red_y"))
                wx, wy = _num(sd.get("white_point_x")), _num(sd.get("white_point_y"))
                lmin = _num(sd.get("min_luminance"))
                lmax = _num(sd.get("max_luminance"))
                if lmax > 0 and (rx > 0 or gx > 0 or bx > 0):
                    master_display = (
                        f"G({gx},{gy})B({bx},{by})R({rx},{ry})"
                        f"WP({wx},{wy})L({lmax},{lmin})"
                    )
            elif stype == "Content light level metadata":
                try:
                    mc = int(sd.get("max_content") or 0)
                    ma = int(sd.get("max_average") or 0)
                except (TypeError, ValueError):
                    mc = ma = 0
                if mc > 0:
                    max_cll = f"{mc},{ma}"
        return master_display, max_cll

    def extract_static_hdr_metadata(self, source: Path) -> tuple[str, str]:
        mi_video = self.load_mediainfo_video_track(source)
        if mi_video is None:
            return "", ""

        master_display = ""
        primaries_label = str(mi_video.get("MasteringDisplay_ColorPrimaries") or "").strip().lower()
        primaries = self._MASTER_DISPLAY_PRIMARIES.get(primaries_label)
        try:
            lmin = float(mi_video.get("MasteringDisplay_Luminance_Min") or 0)
            lmax = float(mi_video.get("MasteringDisplay_Luminance_Max") or 0)
        except (TypeError, ValueError):
            lmin = lmax = 0.0
        if primaries and lmax > 0:
            (gx, gy), (bx, by), (rx, ry), (wx, wy) = primaries
            c = lambda f: int(round(f * 50000))
            l_ = lambda f: int(round(f * 10000))
            master_display = (
                f"G({c(gx)},{c(gy)})"
                f"B({c(bx)},{c(by)})"
                f"R({c(rx)},{c(ry)})"
                f"WP({c(wx)},{c(wy)})"
                f"L({l_(lmax)},{l_(lmin)})"
            )

        max_cll = ""
        try:
            max_content = int(re.sub(r"[^\d]", "", str(mi_video.get("MaxCLL") or "")) or 0)
            max_average = int(re.sub(r"[^\d]", "", str(mi_video.get("MaxFALL") or "")) or 0)
        except (TypeError, ValueError):
            max_content = max_average = 0
        if max_content > 0:
            max_cll = f"{max_content},{max_average}"
        return master_display, max_cll
