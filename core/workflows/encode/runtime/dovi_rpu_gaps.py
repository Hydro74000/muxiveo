"""Images vidéo sans RPU Dolby Vision : localisation et complément (règle de répétition).

Un remux peut garder une unité d'accès sans RPU (ni couche EL), le plus souvent la dernière
image décodée du film. Le RPU extrait compte alors une trame de moins que la vidéo et l'audit
de fin d'encodage refuse le résultat. Un lecteur Dolby Vision garde le dernier RPU reçu, et
mvo-fel recompose une telle image avec la courbe du RPU précédent : on répète donc, à la même
position d'affichage, le RPU de l'image précédente (le suivant pour la première image).

Règle (validée par l'utilisateur, 2026-11-11) : complément seulement pour une image unique, ou
pour des images toutes situées en fin de flux (ordre de décodage) ; sinon refus avant
l'encodage. Avec reconstruction FEL, les images concernées ne doivent pas porter de couche EL
(le plugin les recompose sans résiduel). Localisation par lecture des blocs Matroska seulement.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

_RPU_NAL = 62
_EL_NAL = 63


@dataclass(frozen=True)
class RpuGaps:
    """Relevé des images sans RPU d'une piste vidéo (positions comptées depuis 0)."""

    frames: int
    #: Rangs d'affichage des images sans RPU (ordre croissant).
    missing: tuple[int, ...]
    #: Parmi ``missing``, rangs des images portant une couche EL.
    with_el: tuple[int, ...]
    #: Toutes les images sans RPU sont les dernières du flux en ordre de décodage.
    trailing: bool


def _nal_kinds(payload: bytes, length_size: int) -> set[int]:
    kinds: set[int] = set()
    pos = 0
    while pos + length_size <= len(payload):
        size = int.from_bytes(payload[pos:pos + length_size], "big")
        start = pos + length_size
        if size < 2 or start + size > len(payload):
            break
        kinds.add((payload[start] >> 1) & 0x3F)
        pos = start + size
    return kinds


def scan_rpu_gaps(path: Path, track_index: int, *, cancelled: Callable[[], bool] | None = None,
                  progress: Callable[[float], None] | None = None) -> RpuGaps:
    """Lit tous les blocs vidéo d'un MKV (sans décodage) : images sans RPU et leur rang d'affichage."""
    from core.matroska.reader import MatroskaReader

    reader = MatroskaReader(Path(path))
    reader.segment()
    tracks = reader.tracks()
    if not 0 <= track_index < len(tracks) or tracks[track_index].track_type != 1:
        raise ValueError(f"Piste vidéo #{track_index} absente.")
    track = tracks[track_index]
    private = track.codec_private
    length_size = (private[21] & 0x03) + 1 if len(private) >= 23 and private[0] == 1 else 4
    total = max(1, Path(path).stat().st_size)
    stamps: list[int] = []
    gaps: list[tuple[int, bool]] = []  # (rang de décodage, couche EL présente)
    consumed = 0
    for block in reader.blocks(track_numbers={track.number}):
        decode_index = len(stamps)
        if cancelled is not None and decode_index % 2048 == 0 and cancelled():
            raise InterruptedError
        stamps.append(block.timestamp_ns if block.timestamp_ns is not None else block.timestamp_ms * 1_000_000)
        kinds = _nal_kinds(block.payload, length_size)
        if _RPU_NAL not in kinds:
            gaps.append((decode_index, _EL_NAL in kinds))
        consumed += len(block.payload)
        if progress is not None and decode_index % 4096 == 0:
            progress(min(0.99, consumed / total))
    order = sorted(range(len(stamps)), key=lambda index: (stamps[index], index))
    rank = {decode: display for display, decode in enumerate(order)}
    decode_missing = sorted(index for index, _el in gaps)
    trailing = bool(decode_missing) and decode_missing == list(range(len(stamps) - len(decode_missing), len(stamps)))
    return RpuGaps(
        frames=len(stamps),
        missing=tuple(sorted(rank[index] for index, _el in gaps)),
        with_el=tuple(sorted(rank[index] for index, el in gaps if el)),
        trailing=trailing,
    )


def gap_refusal(gaps: RpuGaps, *, fel: bool) -> str:
    """Motif de refus du complément ; chaîne vide si la règle s'applique."""
    if not gaps.missing:
        return ""
    if len(gaps.missing) > 1 and not gaps.trailing:
        shown = ", ".join(str(value) for value in gaps.missing[:8]) + ("…" if len(gaps.missing) > 8 else "")
        return (f"{len(gaps.missing)} images sans RPU réparties dans le flux (rangs d'affichage {shown}) : "
                "complément limité à une image unique ou à la fin du flux")
    if fel and gaps.with_el:
        return (f"image(s) sans RPU portant une couche EL (rangs {', '.join(map(str, gaps.with_el[:8]))}) : "
                "reconstruction FEL impossible")
    return ""


def fill_rpu_gaps(*, dovi_tool_bin: str, rpu_input: Path, output_rpu: Path, positions: Sequence[int],
                  run_cmd: Callable[[list[str]], object]) -> Path:
    """Insère, à chaque rang d'affichage manquant, une copie du RPU précédent (suivant pour le rang 0).

    Rangs traités par ordre croissant : les images qui précèdent sont alors déjà complètes, et la
    position d'insertion est celle de la liste finale.
    """
    current = rpu_input
    edit_json = output_rpu.with_suffix(".fill.json")
    try:
        for step, position in enumerate(sorted(positions)):
            target = output_rpu if step == len(positions) - 1 else output_rpu.with_suffix(f".fill{step}.bin")
            source = max(0, position - 1)
            edit_json.write_text(json.dumps({"duplicate": [{"source": source, "offset": position, "length": 1}]}),
                                 encoding="utf-8")
            target.unlink(missing_ok=True)
            run_cmd([dovi_tool_bin, "editor", "-i", str(current), "-j", str(edit_json), "-o", str(target)])
            if current not in (rpu_input, output_rpu):
                current.unlink(missing_ok=True)
            current = target
    finally:
        edit_json.unlink(missing_ok=True)
    return output_rpu
