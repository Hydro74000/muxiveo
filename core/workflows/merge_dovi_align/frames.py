"""Table d'images exacte d'une piste vidéo : rang d'affichage ↔ horodatage du conteneur.

Une entrée est **indexable** (lecture de fenêtres par recherche) seulement si son conteneur donne
des PTS entiers uniques pour chaque paquet vidéo. Les autres (flux de transport, programmes MPEG,
HEVC brut…) sont lues d'une traite : le rang est alors l'ordre de sortie du décodeur.
"""
from __future__ import annotations

import bisect
import json
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from core.subprocess_utils import run_cancellable_capture, subprocess_text_kwargs

from .process import ProcessHooks

#: Conteneurs dont les PTS de paquets sont fiables pour une recherche exacte.
INDEXABLE_SUFFIXES: frozenset[str] = frozenset({".mkv", ".mk3d", ".mp4", ".m4v", ".mov", ".webm"})


class FrameTableError(RuntimeError):
    """Piste vidéo illisible par FFprobe."""


@dataclass(frozen=True)
class VideoProps:
    """Propriétés de la première piste vidéo utiles à l'alignement."""

    codec: str
    width: int
    height: int
    #: Rapport d'aspect des pixels (1 si absent ou non renseigné).
    sar: Fraction
    #: ``tv`` (limité), ``pc`` (plein) ou vide.
    color_range: str
    transfer: str
    primaries: str
    time_base: Fraction
    pix_fmt: str = ""
    #: Départ du conteneur (secondes) : origine des recherches ``-ss`` de FFmpeg.
    start_time: Fraction = Fraction(0)
    #: Cadence annoncée (``avg_frame_rate``, sinon ``r_frame_rate``), 0 si inconnue.
    frame_rate: Fraction = Fraction(0)

    @property
    def full_range(self) -> bool:
        return self.color_range == "pc"

    @property
    def pq(self) -> bool:
        """Image de base PQ (HDR10, DV P7/P8.1, HDR10+)."""
        return self.transfer == "smpte2084"


@dataclass(frozen=True)
class FrameTable:
    """Images d'une piste vidéo en ordre d'affichage."""

    path: Path
    props: VideoProps
    indexable: bool
    #: Motif de non-indexabilité (vide si indexable).
    reason: str = ""
    #: PTS (base de temps du flux) en ordre d'affichage, strictement croissants ; vide si non indexable.
    pts: tuple[int, ...] = field(default=(), repr=False)

    @property
    def frames(self) -> int | None:
        """Nombre d'images (None pour une entrée non indexable : compté par la passe complète)."""
        return len(self.pts) if self.indexable else None

    @property
    def frame_interval(self) -> int:
        """Écart minimal entre deux PTS (base de temps du flux), 1 au moins."""
        if len(self.pts) < 2:
            return 1
        return max(1, min(b - a for a, b in zip(self.pts, self.pts[1:])))

    def rank_of(self, pts: int) -> int | None:
        """Rang d'affichage de l'image au PTS ``pts`` exact, None s'il n'existe pas."""
        index = bisect.bisect_left(self.pts, pts)
        return index if index < len(self.pts) and self.pts[index] == pts else None

    def seek_seconds(self, rank: int) -> str:
        """Position ``-ss`` (secondes depuis le départ du conteneur) visant exactement l'image ``rank`` :
        une demi-image avant son PTS. FFmpeg ajoute lui-même ``start_time`` à la position ; avec
        ``-seek_timestamp 1`` et ``-copyts``, il écarterait toutes les images."""
        if not self.indexable or not 0 <= rank < len(self.pts):
            raise ValueError(f"Rang {rank} hors de la table.")
        target = (Fraction(self.pts[rank]) - Fraction(self.frame_interval, 2)) * self.props.time_base
        return f"{float(max(Fraction(0), target - self.props.start_time)):.9f}"


def _fraction(value: object, default: Fraction) -> Fraction:
    try:
        result = Fraction(str(value).replace(":", "/"))
    except (ValueError, ZeroDivisionError):
        return default
    return result if result > 0 else default


def parse_stream_props(payload: str) -> VideoProps:
    """Propriétés de la première piste vidéo depuis la sortie JSON de FFprobe (``-show_entries stream=…``)."""
    try:
        streams = json.loads(payload or "{}").get("streams") or []
    except ValueError as exc:
        raise FrameTableError(f"Sortie FFprobe illisible : {exc}") from exc
    if not streams:
        raise FrameTableError("Aucune piste vidéo.")
    stream = streams[0]
    try:
        start = Fraction(str((json.loads(payload or "{}").get("format") or {}).get("start_time") or "0"))
    except (ValueError, ZeroDivisionError):
        start = Fraction(0)
    try:
        width, height = int(stream.get("width") or 0), int(stream.get("height") or 0)
    except (TypeError, ValueError):
        width = height = 0
    if width <= 0 or height <= 0:
        raise FrameTableError("Dimensions de la piste vidéo inconnues.")
    return VideoProps(
        codec=str(stream.get("codec_name") or ""),
        width=width,
        height=height,
        sar=_fraction(stream.get("sample_aspect_ratio") or "1", Fraction(1)),
        color_range=str(stream.get("color_range") or ""),
        transfer=str(stream.get("color_transfer") or ""),
        primaries=str(stream.get("color_primaries") or ""),
        time_base=_fraction(stream.get("time_base") or "1/1000", Fraction(1, 1000)),
        pix_fmt=str(stream.get("pix_fmt") or ""),
        start_time=start,
        frame_rate=_fraction(stream.get("avg_frame_rate"), Fraction(0)) or _fraction(stream.get("r_frame_rate"),
                                                                                       Fraction(0)),
    )


def parse_packet_pts(payload: str) -> tuple[tuple[int, ...], str]:
    """PTS triés (ordre d'affichage) depuis la sortie CSV de FFprobe ``packet=pts`` ; motif de refus sinon."""
    values: list[int] = []
    for line in (payload or "").splitlines():
        text = line.strip().rstrip(",")
        if not text:
            continue
        if text == "N/A":
            return (), "paquet vidéo sans PTS"
        try:
            values.append(int(text))
        except ValueError:
            return (), f"PTS illisible ({text!r})"
    if not values:
        return (), "aucun paquet vidéo"
    ordered = sorted(values)
    if any(b <= a for a, b in zip(ordered, ordered[1:])):
        return (), "PTS répétés"
    return tuple(ordered), ""


def read_frame_table(path: Path, *, ffprobe: str = "ffprobe", hooks: ProcessHooks | None = None) -> FrameTable:
    """Table d'images de la première piste vidéo de ``path`` (lecture des paquets, sans décodage)."""
    hooks = hooks or ProcessHooks()
    path = Path(path)

    def run(command: list[str]) -> str:
        result = run_cancellable_capture(
            command, cancel_cb=hooks.cancelled, check_cancelled=hooks.check,
            on_start=hooks.on_start, on_end=hooks.on_end, **subprocess_text_kwargs(),
        )
        if result.returncode != 0:
            raise FrameTableError(f"FFprobe a échoué ({path.name}) : {(result.stderr or '').strip()[:300]}")
        return result.stdout or ""

    props = parse_stream_props(run([
        ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
        "stream=codec_name,width,height,sample_aspect_ratio,pix_fmt,color_range,color_transfer,color_primaries,"
        "time_base,avg_frame_rate,r_frame_rate:format=start_time",
        "-of", "json", str(path),
    ]))
    if path.suffix.lower() not in INDEXABLE_SUFFIXES:
        return FrameTable(path, props, False, f"conteneur {path.suffix or 'sans extension'} lu d'une traite")
    pts, reason = parse_packet_pts(run([
        ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts", "-of", "csv=p=0", str(path),
    ]))
    if reason:
        return FrameTable(path, props, False, reason)
    return FrameTable(path, props, True, "", pts)
