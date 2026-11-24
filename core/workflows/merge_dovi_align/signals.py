"""Repères de Film 2 lus dans ses métadonnées, sans décodage vidéo.

- **Points de changement** : images dont les métadonnées diffèrent de la précédente (octets de la NAL RPU ;
  champs de rendu HDR10+, champs administratifs exclus). Entre deux points, une erreur de position est
  sans effet sur le fichier produit : seuls ces points sont à vérifier.
- **Coupes de référence** : ``scene_refresh`` du RPU, sinon débuts de scène HDR10+ s'ils sont plausibles.
- **Luminance décrite** par image (L1 avg du RPU ou AverageRGB du HDR10+, en code PQ 12 bits), pour la
  relation de luminance des plans.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from core.hdr_inspection.json_stream import iter_json_array
from core.hdr_inspection.pq import nits_to_pq

_START_CODE = b"\x00\x00\x00\x01"
#: Champs HDR10+ sans effet sur le rendu (indices, numérotation).
_HDR10PLUS_ADMIN = frozenset({"SceneFrameIndex", "SceneId", "SequenceFrameIndex"})
#: Débuts de scène HDR10+ jugés exploitables comme coupes : plan médian ≥ 12 images.
_MIN_MEDIAN_SCENE = 12


@dataclass(frozen=True)
class MetadataTrack:
    """Repères d'un flux de métadonnées de Film 2 (une entrée par image, ordre d'affichage)."""

    kind: str  # « rpu » ou « hdr10plus »
    frames: int
    change_points: np.ndarray
    reference_cuts: np.ndarray
    #: Luminance moyenne décrite par image (code PQ 12 bits), ou tableau vide.
    luminance: np.ndarray
    #: Pic décrit par image (code PQ 12 bits : L1 max, MaxScl), ou tableau vide.
    peak: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float32))


@dataclass(frozen=True)
class Film2Metadata:
    """Repères combinés des métadonnées de Film 2."""

    frames: int
    change_points: np.ndarray
    reference_cuts: np.ndarray
    luminance: np.ndarray
    kinds: tuple[str, ...]
    peak: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float32))

    @property
    def has_reference_cuts(self) -> bool:
        return self.reference_cuts.size > 1


def _changes(digests: list[bytes]) -> np.ndarray:
    points = [0] if digests else []
    points += [index for index in range(1, len(digests)) if digests[index] != digests[index - 1]]
    return np.asarray(points, dtype=np.int64)


def _check(cancelled: Callable[[], bool] | None, index: int) -> None:
    if cancelled is not None and index % 8192 == 0 and cancelled():
        raise InterruptedError("Alignement annulé.")


def _tenths(value: object) -> float:
    """Valeurs HDR10+ en dixièmes de nit."""
    return float(value) / 10.0 if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def rpu_track(rpu_bin: Path, scene_starts: np.ndarray, l1_avg: np.ndarray | None = None,
              l1_max: np.ndarray | None = None, *, cancelled: Callable[[], bool] | None = None) -> MetadataTrack:
    """Repères du RPU ``rpu.bin`` (NAL précédées d'un code de départ, une par image, ordre d'affichage).

    ``scene_starts`` : images à ``scene_refresh`` (``dovi_tool export -d scenes``) ; ``l1_avg`` : L1 avg par
    image (code PQ 12 bits), facultatif.
    """
    data = Path(rpu_bin).read_bytes()
    digests: list[bytes] = []
    position = data.find(_START_CODE)
    while position >= 0:
        following = data.find(_START_CODE, position + 4)
        payload = data[position + 4: following if following >= 0 else len(data)]
        digests.append(hashlib.blake2b(payload, digest_size=16).digest())
        _check(cancelled, len(digests))
        position = following
    frames = len(digests)
    cuts = np.unique(np.concatenate([[0], np.asarray(scene_starts, dtype=np.int64)])) if frames else np.zeros(0, np.int64)
    def per_frame(values: np.ndarray | None) -> np.ndarray:
        return np.asarray(values, dtype=np.float32) if values is not None and len(values) == frames \
            else np.zeros(0, dtype=np.float32)

    return MetadataTrack("rpu", frames, _changes(digests), cuts[cuts < max(frames, 1)], per_frame(l1_avg),
                         per_frame(l1_max))


def hdr10plus_track(json_path: Path, *, cancelled: Callable[[], bool] | None = None) -> MetadataTrack:
    """Repères du JSON HDR10+ (entrées ``SceneInfo`` lues en flux)."""
    digests: list[bytes] = []
    starts: list[int] = []
    average: list[float] = []
    peaks: list[float] = []
    for index, entry in enumerate(iter_json_array(Path(json_path), key="SceneInfo")):
        _check(cancelled, index)
        if not isinstance(entry, dict):
            raise ValueError(f"JSON HDR10+ invalide (image {index}).")
        rendering = {key: value for key, value in entry.items() if key not in _HDR10PLUS_ADMIN}
        digests.append(hashlib.blake2b(json.dumps(rendering, sort_keys=True).encode(), digest_size=16).digest())
        if entry.get("SceneFrameIndex") == 0 or index == 0:
            starts.append(index)
        parameters = entry.get("LuminanceParameters") or {}
        average.append(_tenths(parameters.get("AverageRGB")))
        peaks.append(max((_tenths(value) for value in parameters.get("MaxScl") or ()), default=0.0))
    frames = len(digests)
    cuts = np.asarray(starts, dtype=np.int64)
    if cuts.size > 1 and (float(np.median(np.diff(np.append(cuts, frames)))) < _MIN_MEDIAN_SCENE):
        cuts = np.zeros(0, dtype=np.int64)  # scènes recalculées à chaque image : inutilisables comme coupes
    def pq12(values: list[float]) -> np.ndarray:
        if not frames:
            return np.zeros(0, dtype=np.float32)
        return (np.asarray(nits_to_pq(np.asarray(values, dtype=np.float64)), dtype=np.float32) * 4095.0)

    return MetadataTrack("hdr10plus", frames, _changes(digests), cuts, pq12(average), pq12(peaks))


def combine(tracks: list[MetadataTrack]) -> Film2Metadata:
    """Repères combinés : points de changement réunis ; coupes et luminance du RPU de préférence."""
    if not tracks:
        raise ValueError("Aucune métadonnée dynamique.")
    frames = {track.frames for track in tracks}
    if len(frames) != 1:
        raise ValueError("Flux de métadonnées de longueurs différentes : " +
                         ", ".join(f"{track.kind} {track.frames}" for track in tracks))
    ordered = sorted(tracks, key=lambda track: track.kind != "rpu")
    changes = np.unique(np.concatenate([track.change_points for track in tracks]))
    cuts = next((track.reference_cuts for track in ordered if track.reference_cuts.size > 1),
                np.zeros(0, dtype=np.int64))
    source = next((track for track in ordered if track.luminance.size), None)
    empty = np.zeros(0, dtype=np.float32)
    return Film2Metadata(frames.pop(), changes, cuts, source.luminance if source else empty,
                         tuple(track.kind for track in ordered), source.peak if source else empty)
