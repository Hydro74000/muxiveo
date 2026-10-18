"""Instantané HDR d'une piste vidéo (niveau 1) : en-têtes déjà lus par l'inspection."""
from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction

from core.inspector import FileInfo, HDRType, VideoTrack
from core.workflows.encode.runtime.hdr_metadata import select_mediainfo_video_track

#: Primaires de référence (x, y) : R, G, B, point blanc.
PRIMARY_SETS = {
    "Display P3": ((0.680, 0.320), (0.265, 0.690), (0.150, 0.060), (0.3127, 0.3290)),
    "BT.2020": ((0.708, 0.292), (0.170, 0.797), (0.131, 0.046), (0.3127, 0.3290)),
    "BT.709": ((0.640, 0.330), (0.300, 0.600), (0.150, 0.060), (0.3127, 0.3290)),
}
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")


@dataclass(frozen=True)
class MasteringDisplay:
    primaries: str
    min_nits: float | None
    max_nits: float | None


@dataclass(frozen=True)
class DoviRecord:
    """Enregistrement de configuration Dolby Vision du conteneur."""

    profile: int
    level: int | None
    compat_id: int | None
    bl: bool
    el: bool
    rpu: bool

    @property
    def label(self) -> str:
        compat = f".{self.compat_id}" if self.profile == 8 and self.compat_id is not None else ""
        return f"P{self.profile}{compat}"


@dataclass(frozen=True)
class HdrSnapshot:
    path: str
    stream_index: int
    codec: str
    width: int | None
    height: int | None
    fps: float | None
    bit_depth: int | None
    transfer: str | None
    primaries: str | None
    matrix: str | None
    hdr_type: HDRType
    mastering: MasteringDisplay | None
    max_cll: int | None
    max_fall: int | None
    dovi: DoviRecord | None
    hdr10plus: bool
    duration_s: float | None
    #: Nombre d'images de la piste ; ``frames_exact`` : validé par la durée de la piste.
    frames: int | None
    frames_exact: bool

    @property
    def is_pq(self) -> bool:
        return (self.transfer or "").lower() == "smpte2084"

    @property
    def is_hlg(self) -> bool:
        return (self.transfer or "").lower() == "arib-std-b67"

    @property
    def formats(self) -> tuple[str, ...]:
        """Formats présents, du plus riche au plus simple."""
        names: list[str] = []
        if self.dovi is not None:
            names.append("Dolby Vision")
        if self.hdr10plus:
            names.append("HDR10+")
        if self.is_pq:
            names.append("HDR10" if self.mastering or self.max_cll else "PQ")
        if self.is_hlg:
            names.append("HLG")
        return tuple(names) or ("SDR",)


def _fraction(value: object) -> float | None:
    try:
        return float(Fraction(str(value)))
    except (ValueError, ZeroDivisionError):
        return None


def _number(value: object) -> float | None:
    match = _NUMBER_RE.search(str(value or ""))
    return float(match.group(0).replace(",", ".")) if match else None


def primaries_name(points: tuple[tuple[float, float], ...]) -> str:
    """Nom des primaires (R, G, B, blanc) reconnues à 0,005 près, sinon coordonnées."""
    for name, reference in PRIMARY_SETS.items():
        if all(abs(a - b) <= 0.005 for point, ref in zip(points, reference) for a, b in zip(point, ref)):
            return name
    return " ".join(f"{x:.3f},{y:.3f}" for x, y in points)


def _side_data(track: VideoTrack, kind: str) -> dict | None:
    for item in track.raw.get("side_data_list") or ():
        if isinstance(item, dict) and item.get("side_data_type") == kind:
            return item
    return None


def _mediainfo_track(info: FileInfo, stream_index: int) -> dict:
    media = (info.mediainfo_json or {}).get("media") or {}
    tracks = [t for t in media.get("track") or () if isinstance(t, dict) and t.get("@type") == "Video"]
    return select_mediainfo_video_track(tracks, stream_index) or {}


def _mastering(track: VideoTrack, mediainfo: dict) -> MasteringDisplay | None:
    data = _side_data(track, "Mastering display metadata")
    if data is not None:
        try:
            points = tuple(
                (float(Fraction(str(data[f"{c}_x"]))), float(Fraction(str(data[f"{c}_y"]))))
                for c in ("red", "green", "blue", "white_point")
            )
            primaries = primaries_name(points)
        except (KeyError, ValueError, ZeroDivisionError):
            primaries = "?"
        return MasteringDisplay(primaries, _fraction(data.get("min_luminance")), _fraction(data.get("max_luminance")))
    if not any(key.startswith("MasteringDisplay") for key in mediainfo):
        return None
    return MasteringDisplay(
        str(mediainfo.get("MasteringDisplay_ColorPrimaries") or "?"),
        _number(mediainfo.get("MasteringDisplay_Luminance_Min")),
        _number(mediainfo.get("MasteringDisplay_Luminance_Max")),
    )


def _light_levels(track: VideoTrack, mediainfo: dict) -> tuple[int | None, int | None]:
    data = _side_data(track, "Content light level metadata")
    if data is not None:
        return int(data.get("max_content") or 0), int(data.get("max_average") or 0)
    cll, fall = _number(mediainfo.get("MaxCLL")), _number(mediainfo.get("MaxFALL"))
    return (int(cll) if cll is not None else None), (int(fall) if fall is not None else None)


def _dovi(track: VideoTrack) -> DoviRecord | None:
    data = _side_data(track, "DOVI configuration record")
    if data is not None and isinstance(data.get("dv_profile"), int):
        return DoviRecord(
            profile=int(data["dv_profile"]),
            level=data.get("dv_level") if isinstance(data.get("dv_level"), int) else None,
            compat_id=data.get("dv_bl_signal_compatibility_id")
            if isinstance(data.get("dv_bl_signal_compatibility_id"), int) else None,
            bl=bool(data.get("bl_present_flag", 1)),
            el=bool(data.get("el_present_flag", 0)),
            rpu=bool(data.get("rpu_present_flag", 1)),
        )
    if track.dovi_profile is not None:
        return DoviRecord(track.dovi_profile, None, track.dovi_compat_id, True, False, True)
    return None


def build_snapshot(info: FileInfo, stream_index: int | None = None) -> HdrSnapshot:
    """Instantané de la piste ``stream_index`` (index ffprobe ; défaut : première vidéo)."""
    tracks = info.video_tracks
    if not tracks:
        raise ValueError("Aucune piste vidéo.")
    track = next((t for t in tracks if t.index == stream_index), tracks[0])
    mediainfo = _mediainfo_track(info, track.index)
    max_cll, max_fall = _light_levels(track, mediainfo)
    hdr_format = str(mediainfo.get("HDR_Format") or "")
    fps = _fraction(track.frame_rate) if track.frame_rate else None
    duration = track.duration_s or info.duration_s
    frames = info.frame_count if track is info.primary_video else None
    exact = frames is not None
    if frames is None and duration and fps:
        frames = int(round(duration * fps))
    return HdrSnapshot(
        path=str(info.path),
        stream_index=track.index,
        codec=track.codec,
        width=track.width,
        height=track.height,
        fps=fps,
        bit_depth=track.bit_depth,
        transfer=track.color_transfer,
        primaries=track.color_primaries,
        matrix=track.color_matrix,
        hdr_type=track.hdr_type,
        mastering=_mastering(track, mediainfo),
        max_cll=max_cll,
        max_fall=max_fall,
        dovi=_dovi(track),
        hdr10plus=track.hdr_type in (HDRType.HDR10PLUS, HDRType.DOLBY_VISION_HDR10PLUS) or "2094" in hdr_format,
        duration_s=duration,
        frames=frames,
        frames_exact=exact,
    )
