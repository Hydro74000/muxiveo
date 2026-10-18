"""Réécriture des payloads HEVC d'un MKV encodé avec les access units injectés.

Pipeline lot 3 : le flux vidéo encodé existe d'abord sous forme d'un MKV
mono-piste horodaté (timestamps écrits par l'encodeur, jamais réassociés
depuis la source d'origine). Après extraction annexB et injection
HDR10+/Dolby Vision, ce module consomme **en lockstep** :

- les blocs vidéo du MKV encodé (ordre de décodage, PTS, durées, keyframes
  et références conservés tels quels) ;
- les access units HEVC injectés (streaming, mémoire bornée) ;
- les métadonnées Dolby Vision (record ``BlockAdditionMapping``).

Il produit un itérateur de :class:`MatroskaMuxPacket` consommé par le writer
générique, qui écrit un MKV mono-piste candidat (commit atomique). Une
différence de nombre entre blocs encodés et access units injectés provoque
un échec strict avant commit (le candidat ``.partial`` est supprimé).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from ..editors.dovi import DolbyVisionConfigRecord
from ..ids import (
    ATTACHMENTS_ID,
    BLOCK_ADDITION_MAPPING_ID,
    CHAPTERS_ID,
    TAGS_ID,
    CODEC_PRIVATE_ID,
    MAX_BLOCK_ADDITION_ID_ID,
    COLOUR_ID,
    MAX_CLL_ID,
    MAX_FALL_ID,
    VIDEO_ID,
)
from ..ebml import binary_element, element, uint_element
from .access_units import (
    DEFAULT_HEVC_CHUNK_SIZE,
    HevcAccessUnit,
    iter_hevc_access_units,
)
from ..mux_plan import MatroskaMuxPacket, MatroskaMuxPlan, MatroskaMuxTrack
from ..native_muxer import (
    _build_dovi_block_addition_mapping,
    _build_hvcc,
    _extract_hvcc_components,
    _HvccComponents,
)
from ..reader import MatroskaReader, payload_children
from ..writer import MatroskaWriteProgress, MatroskaWriter
from ..validation import MatroskaPacketValidation


#: Types NAL des parameter sets (CodecPrivate hvcC et in-band).
_PARAMETER_SET_TYPES = frozenset({32, 33, 34})  # VPS, SPS, PPS

_TRACK_TYPE_VIDEO = 1


class HevcPayloadAlignmentError(RuntimeError):
    """Blocs encodés et access units injectés désalignés (échec strict)."""


@dataclass(frozen=True)
class HevcPayloadRewriteResult:
    """Résultat de la réécriture (artefact MKV mono-piste horodaté)."""

    output: Path
    frames_rewritten: int
    codec_private_updated: bool
    dovi_mapping_written: bool


def _hvcc_length_size(codec_private: bytes) -> int | None:
    """Taille des préfixes de longueur NAL du hvcC (1/2/4), ou None si non-hvcC."""
    if len(codec_private) < 23 or codec_private[0] != 1:
        return None
    return (codec_private[21] & 0x03) + 1


def _updated_hvcc(original: bytes, components: _HvccComponents) -> bytes:
    """Reconstruit le hvcC : header d'origine conservé, arrays VPS/SPS/PPS remplacés.

    Le header de 23 octets écrit par l'encodeur (profil, niveau, chroma…)
    est plus précis que le header minimal ; seuls les NAL arrays sont mis à
    jour depuis le flux injecté. Repli sur le hvcC minimal si l'original
    n'est pas un hvcC valide.
    """
    if not (components.vps and components.sps and components.pps):
        return original
    if len(original) < 23 or original[0] != 1:
        return _build_hvcc(components)
    out = bytearray(original[:22])
    arrays: list[tuple[int, list[bytes]]] = [
        (32, components.vps), (33, components.sps), (34, components.pps),
    ]
    out.append(len(arrays))
    for nal_type, nals in arrays:
        out.append(0x80 | (nal_type & 0x3F))
        out.extend(len(nals).to_bytes(2, "big"))
        for nal in nals:
            out.extend(len(nal).to_bytes(2, "big"))
            out.extend(nal)
    return bytes(out)


def _canonical_nal_rank(nal_type: int) -> int:
    """Rang canonique d'un NAL unit HEVC selon ITU-T H.265 § 7.4.2.4.4.

    Garantit l'ordre légal des flux HEVC dans chaque Access Unit :
    AUD (35) -> Parameter sets VPS/SPS/PPS (32, 33, 34) -> Prefix SEI (39) -> Slices (0..31) -> Suffix SEI (40) -> UNSPEC/DoVi RPU (62, 63)
    """
    if nal_type == 35:
        return 0
    if nal_type in (32, 33, 34):
        return 10
    if nal_type == 39:
        return 20
    if 0 <= nal_type <= 31:
        return 30
    if nal_type == 40:
        return 40
    if nal_type in (62, 63):
        return 50
    return 35


def _au_to_block_payload(au: HevcAccessUnit, length_size: int | None) -> bytes:
    """Convertit un access unit injecté vers le framing des blocs du MKV.

    hvcC (``length_size`` connu) : tous les NAL préfixés par leur longueur,
    parameter sets conservés in-band en plus du CodecPrivate (comme l'oracle
    et la façade native) : dovi_tool / hdr10plus_tool lisent les blocs MKV
    sans exploiter le hvcC. Sans hvcC : payload annexB inchangé. Un AU réduit
    à des parameter sets n'est pas une image : payload vide (échec strict).
    """
    if all(nal.nal_type in _PARAMETER_SET_TYPES for nal in au.nal_units):
        return b""
    # Tri stable selon l'ordre canonique H.265 pour garantir que VPS/SPS/PPS
    # précèdent impérativement les SEI prefix sur les keyframes.
    ordered_nals = sorted(au.nal_units, key=lambda n: _canonical_nal_rank(n.nal_type))
    if length_size is None:
        return b"".join(b"\x00\x00\x00\x01" + nal.payload for nal in ordered_nals)
    parts: list[bytes] = []
    for nal in ordered_nals:
        parts.append(len(nal.payload).to_bytes(length_size, "big"))
        parts.append(nal.payload)
    return b"".join(parts)


def _first_access_unit(injected_hevc: Path, chunk_size: int) -> HevcAccessUnit | None:
    """Pré-scan borné : premier access unit du flux injecté (parameter sets)."""
    for access_unit in iter_hevc_access_units(injected_hevc, chunk_size):
        return access_unit
    return None


def _with_light_levels(video_payload: bytes, light_levels: tuple[int, int]) -> bytes:
    """Video → Colour : MaxCLL et MaxFALL remplacés (ou ajoutés), autres éléments inchangés."""
    levels = uint_element(MAX_CLL_ID, light_levels[0]) + uint_element(MAX_FALL_ID, light_levels[1])
    children: list[bytes] = []
    found = False
    for element_id, payload in payload_children(video_payload):
        if element_id == COLOUR_ID:
            colour = b"".join(element(child_id, child) for child_id, child in payload_children(payload)
                              if child_id not in (MAX_CLL_ID, MAX_FALL_ID))
            children.append(element(COLOUR_ID, colour + levels))
            found = True
            continue
        children.append(element(element_id, payload))
    if not found:
        children.append(element(COLOUR_ID, levels))
    return b"".join(children)


def _patched_raw_entry(
    raw_entry: bytes,
    *,
    codec_private: bytes | None,
    dovi_mapping: bytes | None,
    light_levels: tuple[int, int] | None = None,
) -> bytes:
    """Reconstruit le payload TrackEntry : CodecPrivate remplacé, mapping DoVi ajouté, MaxCLL/MaxFALL posés."""
    children: list[bytes] = []
    has_max_block_add_id = False
    for element_id, payload in payload_children(raw_entry):
        if element_id == VIDEO_ID and light_levels is not None:
            children.append(element(VIDEO_ID, _with_light_levels(payload, light_levels)))
            continue
        if element_id == CODEC_PRIVATE_ID and codec_private is not None:
            children.append(binary_element(CODEC_PRIVATE_ID, codec_private))
            continue
        if element_id == BLOCK_ADDITION_MAPPING_ID and dovi_mapping is not None:
            # Remplacé par le mapping reconstruit (ajouté en fin d'entrée).
            continue
        if element_id == MAX_BLOCK_ADDITION_ID_ID and dovi_mapping is not None:
            current_val = int.from_bytes(payload, "big") if payload else 0
            children.append(uint_element(MAX_BLOCK_ADDITION_ID_ID, max(current_val, 1)))
            has_max_block_add_id = True
            continue
        children.append(element(element_id, payload))
    if dovi_mapping is not None:
        if not has_max_block_add_id:
            children.append(uint_element(MAX_BLOCK_ADDITION_ID_ID, 1))
        children.append(dovi_mapping)
    return b"".join(children)


class MatroskaHevcPayloadRewriter:
    """Remplace les payloads vidéo d'un MKV encodé par les AUs injectés."""

    def __init__(self, *, chunk_size: int = DEFAULT_HEVC_CHUNK_SIZE) -> None:
        self._chunk_size = max(64 * 1024, int(chunk_size))

    def rewrite(
        self,
        *,
        encoded_mkv: Path,
        injected_hevc: Path,
        output: Path,
        dovi_record: DolbyVisionConfigRecord | None = None,
        cancel_cb: Callable[[], bool] | None = None,
        progress_cb: Callable[[MatroskaWriteProgress], None] | None = None,
        external_validator: Callable[[Path, MatroskaPacketValidation], None] | None = None,
        light_levels: tuple[int, int] | None = None,
    ) -> HevcPayloadRewriteResult:
        """Écrit ``output`` : blocs du MKV encodé, payloads du flux injecté.

        ``light_levels`` : MaxCLL/MaxFALL posés dans l'élément Colour de la piste.

        L'ordre de décodage, les PTS, durées, keyframes et références des
        blocs encodés sont conservés à l'identique ; seul le payload change.
        """
        reader = MatroskaReader(encoded_mkv)
        reader.segment()
        tracks = reader.tracks()
        video_tracks = [track for track in tracks if track.track_type == _TRACK_TYPE_VIDEO]
        if len(video_tracks) != 1 or len(tracks) != 1:
            raise HevcPayloadAlignmentError(
                "MKV encodé mono-piste vidéo attendu : "
                f"{len(video_tracks)} piste(s) vidéo / {len(tracks)} piste(s) au total."
            )
        video = video_tracks[0]

        # Pré-scan borné du 1er AU injecté : parameter sets pour le CodecPrivate.
        first_au = _first_access_unit(injected_hevc, self._chunk_size)
        if first_au is None:
            raise HevcPayloadAlignmentError(
                f"Aucun access unit HEVC dans le flux injecté {injected_hevc.name}."
            )
        components = _extract_hvcc_components(first_au)
        length_size = _hvcc_length_size(video.codec_private)
        updated_codec_private: bytes | None = None
        if video.codec_private and components.sps:
            updated_codec_private = _updated_hvcc(video.codec_private, components)
            if updated_codec_private == video.codec_private:
                updated_codec_private = None

        dovi_mapping = (
            _build_dovi_block_addition_mapping(dovi_record)
            if dovi_record is not None
            else None
        )
        new_raw_entry = _patched_raw_entry(
            video.raw_entry,
            codec_private=updated_codec_private,
            dovi_mapping=dovi_mapping,
            light_levels=light_levels,
        )
        rewritten_track = replace(
            video,
            raw_entry=new_raw_entry,
            codec_private=updated_codec_private or video.codec_private,
        )
        mux_track = MatroskaMuxTrack(
            source=encoded_mkv,
            source_track=rewritten_track,
            output_number=1,
            output_uid=video.uid or 1,
            patch_language=False,
            patch_name=False,
            patch_flags=False,
        )

        frames = {"count": 0}

        def _packets() -> Iterator[MatroskaMuxPacket]:
            """Lockstep blocs encodés ↔ AUs injectés (mémoire bornée)."""
            au_iter = iter_hevc_access_units(
                injected_hevc, self._chunk_size, cancel_cb=cancel_cb,
            )
            sequence = 0
            for block in reader.blocks():
                if block.track_number != video.number:
                    continue
                if block.lace_count > 1 or block.lacing_mode:
                    raise HevcPayloadAlignmentError(
                        "Blocs lacés non supportés par la réécriture lockstep "
                        f"(bloc #{sequence + 1})."
                    )
                access_unit = next(au_iter, None)
                if access_unit is None:
                    raise HevcPayloadAlignmentError(
                        "Désalignement strict : plus de blocs encodés que "
                        f"d'access units injectés (bloc #{sequence + 1})."
                    )
                rewritten_payload = _au_to_block_payload(access_unit, length_size)
                if not rewritten_payload:
                    # Un bloc vide dans la sortie livrée violerait le contrat
                    # Matroska attendu des lecteurs : échec strict avant commit.
                    raise HevcPayloadAlignmentError(
                        "Access unit injecté sans payload utilisable "
                        f"(bloc #{sequence + 1})."
                    )
                rewritten_block = block.__class__(**{
                    **block.__dict__,
                    "payload": rewritten_payload,
                })
                frames["count"] += 1
                yield MatroskaMuxPacket(1, rewritten_block, sequence)
                sequence += 1
            if next(au_iter, None) is not None:
                raise HevcPayloadAlignmentError(
                    "Désalignement strict : plus d'access units injectés que "
                    f"de blocs encodés ({sequence} bloc(s))."
                )

        plan = MatroskaMuxPlan(
            output=output,
            tracks=(mux_track,),
            packets=_packets(),
            timestamp_scale_ns=reader.timestamp_scale_ns(),
            muxing_app="Muxiveo payload rewriter",
            writing_app="Muxiveo",
        )
        MatroskaWriter().write(
            plan,
            external_validator=external_validator,
            cancel_cb=cancel_cb,
            progress_cb=progress_cb,
        )
        return HevcPayloadRewriteResult(
            output=output,
            frames_rewritten=frames["count"],
            codec_private_updated=updated_codec_private is not None,
            dovi_mapping_written=dovi_mapping is not None,
        )


def rewrite_file_video_payloads(
    *,
    source: Path,
    track_index: int,
    injected_hevc: Path,
    output: Path,
    light_levels: tuple[int, int] | None = None,
    cancel_cb: Callable[[], bool] | None = None,
    progress_cb: Callable[[MatroskaWriteProgress], None] | None = None,
    chunk_size: int = DEFAULT_HEVC_CHUNK_SIZE,
) -> HevcPayloadRewriteResult:
    """Fichier complet : payloads de la piste vidéo ``track_index`` (rang dans Tracks) remplacés.

    Les access units du flux injecté remplacent les blocs vidéo un pour un (ordre de décodage,
    PTS, durées, keyframes conservés) ; autres pistes, chapitres, pièces jointes et tags recopiés.
    Correction de métadonnées sans réencodage (RPU, HDR10+, SEI statiques). ``light_levels`` :
    MaxCLL/MaxFALL posés dans l'élément Colour de la piste.
    """
    reader = MatroskaReader(source)
    reader.segment()
    muxing_app, writing_app = reader.segment_info_apps()
    tracks = reader.tracks()
    if not 0 <= track_index < len(tracks) or tracks[track_index].track_type != _TRACK_TYPE_VIDEO:
        raise HevcPayloadAlignmentError(f"Piste vidéo #{track_index} absente du fichier source.")
    video = tracks[track_index]
    first_au = _first_access_unit(injected_hevc, max(64 * 1024, int(chunk_size)))
    if first_au is None:
        raise HevcPayloadAlignmentError(f"Aucun access unit HEVC dans le flux injecté {injected_hevc.name}.")
    components = _extract_hvcc_components(first_au)
    length_size = _hvcc_length_size(video.codec_private)
    codec_private: bytes | None = None
    if video.codec_private and components.sps:
        codec_private = _updated_hvcc(video.codec_private, components)
        if codec_private == video.codec_private:
            codec_private = None
    rewritten_video = replace(
        video,
        raw_entry=_patched_raw_entry(video.raw_entry, codec_private=codec_private, dovi_mapping=None,
                                     light_levels=light_levels),
        codec_private=codec_private or video.codec_private,
    )
    mux_tracks = tuple(
        MatroskaMuxTrack(source, rewritten_video if track is video else track, track.number, track.uid,
                         patch_language=False, patch_name=False, patch_flags=False)
        for track in tracks
    )
    frames = {"count": 0}

    def _packets() -> Iterator[MatroskaMuxPacket]:
        au_iter = iter_hevc_access_units(injected_hevc, max(64 * 1024, int(chunk_size)), cancel_cb=cancel_cb)
        for index, block in enumerate(reader.blocks(first_lace_only=True)):
            if block.track_number == video.number:
                if block.lace_count > 1 or block.lacing_mode:
                    raise HevcPayloadAlignmentError(f"Bloc vidéo lacé non pris en charge (bloc #{frames['count'] + 1}).")
                access_unit = next(au_iter, None)
                if access_unit is None:
                    raise HevcPayloadAlignmentError(
                        f"Désalignement : plus de blocs vidéo que d'access units injectés (bloc #{frames['count'] + 1}).")
                payload = _au_to_block_payload(access_unit, length_size)
                if not payload:
                    raise HevcPayloadAlignmentError(
                        f"Access unit injecté sans payload utilisable (bloc #{frames['count'] + 1}).")
                block = replace(block, payload=payload)
                frames["count"] += 1
            yield MatroskaMuxPacket(block.track_number, block, index)
        if next(au_iter, None) is not None:
            raise HevcPayloadAlignmentError(
                f"Désalignement : plus d'access units injectés que de blocs vidéo ({frames['count']} bloc(s)).")

    plan = MatroskaMuxPlan(
        output, mux_tracks, _packets(),
        timestamp_scale_ns=reader.timestamp_scale_ns(),
        duration_ns=reader.segment_duration_ns() or 0,
        opaque_top_level=tuple(raw for kind in (ATTACHMENTS_ID, CHAPTERS_ID, TAGS_ID)
                               for raw in reader.raw_top_level(kind)),
        title=reader.segment_title(), regenerate_statistics=True,
        muxing_app=muxing_app or "Muxiveo", writing_app=writing_app or "Muxiveo",
    )
    MatroskaWriter().write(plan, cancel_cb=cancel_cb, progress_cb=progress_cb)
    return HevcPayloadRewriteResult(
        output=output,
        frames_rewritten=frames["count"],
        codec_private_updated=codec_private is not None,
        dovi_mapping_written=False,
    )


__all__ = [
    "HevcPayloadAlignmentError",
    "HevcPayloadRewriteResult",
    "MatroskaHevcPayloadRewriter",
    "rewrite_file_video_payloads",
]
