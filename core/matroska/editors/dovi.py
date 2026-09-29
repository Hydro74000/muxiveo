"""
core/matroska/editors/dovi.py

Injection native (pure Python) du ``BlockAdditionMapping`` Dolby Vision dans
le ``TrackEntry`` HEVC d'un fichier Matroska, sans outil externe.

Contexte
========
Quand on wrappe un HEVC contenant des NALs RPU/HDR10+ dans un MKV via
``ffmpeg -f hevc -i ... -c copy ...``, les NALs sont conservés dans le
bytestream du Track mais le muxer Matroska de ffmpeg n'écrit **pas** le
``BlockAdditionMapping`` de niveau Track qui annonce explicitement la
configuration Dolby Vision au niveau conteneur. Conséquence : les players
qui s'appuient sur ce signal (Plex, mpv avec gpu-next, certains TV) n'activent
pas le mode DV.

Ce module patche le fichier MKV en injectant la structure manquante :

    BlockAdditionMapping (0x41E4)
    ├── BlockAddIDValue (0x41F0)        uint
    ├── BlockAddIDName (0x41A4)         string "Dolby Vision configuration"
    ├── BlockAddIDType (0x41E7)         uint = FourCC big-endian "dvcC" (= 0x64766343)
    └── BlockAddIDExtraData (0x41ED)    binary, 24 octets ISO/IEC 14496-15 dvcC

L'opération réécrit l'intégralité du bloc ``Tracks`` (level-1) avec ce
nouveau sous-élément, en s'appuyant sur l'éditeur EBML existant.

Référence
=========
- ISO/IEC 14496-15 (HEVC dans ISO BMFF), section dvcC (Dolby Vision config record).
- Matroska element IDs : 0x41E4 (BlockAdditionMapping), 0x41F0, 0x41A4,
  0x41E7, 0x41ED. Cf https://www.matroska.org/technical/elements.html
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import BinaryIO

from .segment_info import (
    MatroskaSegmentInfoHeaderEditor,
    MatroskaSegmentInfoHeaderEditorOptions,
)


def sanitize_dovi_level(
    level: int,
    fps: float | str | None = None,
    height: int | None = None,
) -> int:
    """Assainit le niveau Dolby Vision pour éviter le rejet par les décodeurs matériels.

    Un niveau 10 (0x55 = UHD 120fps) généré par défaut par certains encodeurs
    (ex: NVEncC) fait échouer les décodeurs TV (Amlogic, Realtek, MediaTek) et
    provoque un fallback vers DAV1 ou SDR.
    Cette fonction ramène le niveau à une valeur valide :
    - UHD <= 30 fps : Level 6 (UHD 30fps)
    - UHD > 30 fps : Level 9 (UHD 60fps)
    - 1080p <= 30 fps : Level 4 (FHD 30fps)
    - 1080p > 30 fps : Level 5 (FHD 60fps)
    """
    parsed_fps: float | None = None
    if fps is not None:
        try:
            if isinstance(fps, str) and "/" in fps:
                num, den = fps.split("/", 1)
                parsed_fps = float(num) / float(den)
            else:
                parsed_fps = float(fps)
        except (ValueError, ZeroDivisionError):
            parsed_fps = None

    if level <= 0 or level >= 10:
        if parsed_fps is not None and parsed_fps > 30.0:
            return 9 if (height is None or height > 1080) else 5
        return 6 if (height is None or height > 1080) else 4
    return level


# Element IDs Matroska (cf spec)
_TRACKS_ID = b"\x16\x54\xae\x6b"
_TRACK_ENTRY_ID = b"\xae"
_CODEC_ID_ID = b"\x86"
_TRACK_NUMBER_ID = b"\xd7"
_TRACK_TYPE_ID = b"\x83"
_BLOCK_ADD_MAPPING_ID = b"\x41\xe4"
_BLOCK_ADD_ID_VALUE_ID = b"\x41\xf0"
_BLOCK_ADD_ID_NAME_ID = b"\x41\xa4"
_BLOCK_ADD_ID_TYPE_ID = b"\x41\xe7"
_BLOCK_ADD_ID_EXTRA_DATA_ID = b"\x41\xed"
_MAX_BLOCK_ADD_ID = b"\x55\xee"

# FourCCs
_FOURCC_DVCC = 0x64766343  # "dvcC" — Dolby Vision configuration record v1 (profiles <= 7 et fallback universel MKV)
_FOURCC_DVVC = 0x64767643  # "dvvC" — version 2 (profiles > 7, ex: P8.1)

_HEVC_CODEC_IDS = {
    "V_MPEGH/ISO/HEVC",
    "V_MPEG4/ISO/AVC",  # rare : DV peut être encapsulé dans AVC chez certains acteurs
}


# ============================================================================
# Modèle public
# ============================================================================


@dataclass(frozen=True)
class DolbyVisionConfigRecord:
    """
    Configuration Dolby Vision selon ISO/IEC 14496-15 (record dvcC v1).

    24 octets, big-endian, structure compacte :

        offset 0   : dv_version_major (1 octet)        = 1
        offset 1   : dv_version_minor (1 octet)        = 0
        offset 2   : 7 bits dv_profile + 1er bit dv_level
        offset 3   : 5 bits dv_level + bl_present + el_present + rpu_present
        offset 4   : 4 bits dv_bl_signal_compat_id + 4 bits réservés
        offsets 5..23 : 20 octets réservés (zéro)
    """

    profile: int            # ex. 8 (P8.x)
    level: int              # ex. 6
    rpu_present: bool       # True
    el_present: bool        # False pour mono-layer (P8.1 typique)
    bl_present: bool        # True
    bl_signal_compat_id: int  # 1 pour P8.1 (HDR10 fallback), 0 pour P8.0
    fourcc: str | int | None = None  # None -> auto ("dvvC" si profile > 7 sinon "dvcC"), ou forcer "dvcC" / "dvvC"

    def __post_init__(self) -> None:
        if not (0 <= self.profile < 128):
            raise ValueError(f"DV profile invalide: {self.profile}")
        if not (0 <= self.level < 64):
            raise ValueError(f"DV level invalide: {self.level}")
        if not (0 <= self.bl_signal_compat_id < 16):
            raise ValueError(f"DV bl_signal_compat_id invalide: {self.bl_signal_compat_id}")

    def to_bytes(self) -> bytes:
        out = bytearray(24)
        out[0] = 1  # dv_version_major
        out[1] = 0  # dv_version_minor
        # 7 bits profile + 1 bit (high bit du level)
        out[2] = ((self.profile & 0x7F) << 1) | ((self.level >> 5) & 0x01)
        # 5 bits low du level + 3 flags
        flags = (
            ((self.level & 0x1F) << 3)
            | ((1 if self.rpu_present else 0) << 2)
            | ((1 if self.el_present else 0) << 1)
            | (1 if self.bl_present else 0)
        )
        out[3] = flags & 0xFF
        # 4 bits compat_id + 4 bits réservés
        out[4] = (self.bl_signal_compat_id & 0x0F) << 4
        # 19 octets réservés (offsets 5..23) déjà à 0
        return bytes(out)


@dataclass(frozen=True)
class DoviBlockAdditionPatchResult:
    applied: bool
    skipped: bool
    reason: str = ""
    patched_track_number: int | None = None
    bytes_delta: int = 0


# ============================================================================
# Éditeur
# ============================================================================


class MatroskaDoviBlockAdditionEditor:
    """
    Patche un fichier MKV pour ajouter le ``BlockAdditionMapping`` Dolby Vision
    sur le premier TrackEntry vidéo HEVC trouvé.

    Stratégie :
        1. Localiser le bloc Tracks (level-1).
        2. Parcourir les TrackEntry, identifier le premier HEVC.
        3. Vérifier qu'il n'a pas déjà un BlockAdditionMapping DOVI.
        4. Reconstruire le payload Tracks avec le nouveau sous-élément.
        5. Déléguer à ``replace_level1_element`` pour l'écriture in-place
           (gère les Voids, le SeekHead, la taille du Segment).

    Exemple :
        editor = MatroskaDoviBlockAdditionEditor()
        record = DolbyVisionConfigRecord(profile=8, level=6,
                                         rpu_present=True, el_present=False,
                                         bl_present=True, bl_signal_compat_id=1)
        result = editor.patch(mkv_path, record=record)
    """

    def __init__(self) -> None:
        # On réutilise l'éditeur header existant pour bénéficier de toutes
        # ses primitives EBML (parsing, Voids, SeekHead, etc.).
        self._base = MatroskaSegmentInfoHeaderEditor(
            options=MatroskaSegmentInfoHeaderEditorOptions(
                allow_post_cluster_rebuild=False,
                fallback_mode="raise",
            ),
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def patch(
        self,
        path: Path,
        *,
        record: DolbyVisionConfigRecord,
        block_add_id_value: int = 1,
        block_add_id_name: str = "Dolby Vision configuration",
        force: bool = False,
        fourcc: str | int | None = None,
    ) -> DoviBlockAdditionPatchResult:
        if not path.is_file():
            raise ValueError(f"Fichier introuvable: {path}")

        # Assainissement de sécurité : éviter Level 10 (120fps) qui casse le décodeur Amlogic/TV
        if record.level >= 10:
            record = replace(record, level=sanitize_dovi_level(record.level))

        # 1) Lire le bloc Tracks.
        with path.open("rb") as fh:
            tracks_offset, tracks_payload = self._read_tracks_payload(fh)

        # 2) Décortiquer les TrackEntry.
        entries = self._parse_track_entries(tracks_payload)
        if not entries:
            return DoviBlockAdditionPatchResult(
                applied=False, skipped=True,
                reason="Aucun TrackEntry trouvé dans Tracks.",
            )

        # 3) Trouver le 1er TrackEntry HEVC à patcher ou mettre à niveau.
        target_idx = -1
        for i, entry_info in enumerate(entries):
            if not entry_info.is_hevc:
                continue
            if entry_info.has_dovi_block_addition and not force:
                return DoviBlockAdditionPatchResult(
                    applied=False, skipped=True,
                    reason=f"TrackEntry #{entry_info.track_number} a déjà un "
                           "BlockAdditionMapping DOVI.",
                    patched_track_number=entry_info.track_number,
                )
            target_idx = i
            break

        if target_idx < 0:
            return DoviBlockAdditionPatchResult(
                applied=False, skipped=True,
                reason="Aucun TrackEntry HEVC trouvé.",
            )

        target = entries[target_idx]

        # 4) Construire le BlockAdditionMapping et l'injecter dans le payload
        #    du TrackEntry cible.
        bam_element = self._build_block_addition_mapping_element(
            record=record,
            id_value=block_add_id_value,
            id_name=block_add_id_name,
            fourcc_override=fourcc,
        )
        new_track_entry_bytes = self._inject_into_track_entry(
            tracks_payload[target.payload_offset:target.end],
            bam_element,
            old_entry_header=tracks_payload[target.offset:target.payload_offset],
        )

        # 5) Reconstruire le payload Tracks avec ce TrackEntry modifié.
        new_tracks_payload = (
            tracks_payload[:target.offset]
            + new_track_entry_bytes
            + tracks_payload[target.end:]
        )
        new_tracks_payload = self._base._refresh_crc32_in_payload(new_tracks_payload)

        # 6) Encapsuler en élément Tracks complet et déléguer la réécriture.
        new_tracks_element = (
            _TRACKS_ID
            + self._base._encode_ebml_size_prefer_length(
                len(new_tracks_payload),
                preferred_length=2,
            )
            + new_tracks_payload
        )

        delta = self._base.replace_level1_element(
            path,
            element_id=_TRACKS_ID,
            new_element_bytes=new_tracks_element,
        )

        return DoviBlockAdditionPatchResult(
            applied=True, skipped=False,
            reason="BlockAdditionMapping DOVI injecté.",
            patched_track_number=target.track_number,
            bytes_delta=delta,
        )

    # ------------------------------------------------------------------
    # Lecture du bloc Tracks
    # ------------------------------------------------------------------

    def _read_tracks_payload(self, fh: BinaryIO) -> tuple[int, bytes]:
        state = self._base._analyze_file(fh, parse_fast=True)
        for entry in state.data:
            if entry.element_id == _TRACKS_ID and not entry.unknown_size:
                payload = self._base._read_exact(fh, entry.payload_offset, entry.size)
                return entry.payload_offset, payload
        raise ValueError("Élément Tracks introuvable dans le segment.")

    # ------------------------------------------------------------------
    # Parsing des TrackEntry
    # ------------------------------------------------------------------

    @dataclass
    class _TrackEntryInfo:
        offset: int
        payload_offset: int
        end: int
        track_number: int
        codec_id: str
        is_hevc: bool
        has_dovi_block_addition: bool
        max_block_addition_id: int | None = None
        dovi_config_bytes: bytes | None = None

    def _parse_track_entries(self, tracks_payload: bytes) -> list["MatroskaDoviBlockAdditionEditor._TrackEntryInfo"]:
        out: list[MatroskaDoviBlockAdditionEditor._TrackEntryInfo] = []
        pos = 0
        n = len(tracks_payload)
        while pos < n:
            try:
                el = self._base._read_ebml_element_from_bytes(tracks_payload, pos)
            except ValueError:
                break
            if el.unknown_size:
                break
            if el.element_id == _TRACK_ENTRY_ID:
                entry_payload = tracks_payload[el.payload_offset:el.end]
                track_number, codec_id, has_bam, max_id, dovi_bytes = self._scan_track_entry_children(entry_payload)
                out.append(
                    MatroskaDoviBlockAdditionEditor._TrackEntryInfo(
                        offset=el.offset,
                        payload_offset=el.payload_offset,
                        end=el.end,
                        track_number=track_number,
                        codec_id=codec_id,
                        is_hevc=codec_id in _HEVC_CODEC_IDS,
                        has_dovi_block_addition=has_bam,
                        max_block_addition_id=max_id,
                        dovi_config_bytes=dovi_bytes,
                    )
                )
            pos = el.end if not el.unknown_size else n
        return out

    def _scan_track_entry_children(
        self, entry_payload: bytes
    ) -> tuple[int, str, bool, int | None, bytes | None]:
        """Retourne (track_number, codec_id, has_dovi_block_addition, max_block_addition_id, dovi_config_bytes)."""
        track_number = 0
        codec_id = ""
        has_bam = False
        max_block_add_id: int | None = None
        dovi_config_bytes: bytes | None = None
        pos = 0
        n = len(entry_payload)
        while pos < n:
            try:
                el = self._base._read_ebml_element_from_bytes(entry_payload, pos)
            except ValueError:
                break
            if el.unknown_size:
                break
            payload = entry_payload[el.payload_offset:el.end]
            if el.element_id == _TRACK_NUMBER_ID:
                track_number = int.from_bytes(payload, "big") if payload else 0
            elif el.element_id == _CODEC_ID_ID:
                codec_id = payload.rstrip(b"\x00").decode("ascii", errors="replace")
            elif el.element_id == _MAX_BLOCK_ADD_ID:
                max_block_add_id = int.from_bytes(payload, "big") if payload else 0
            elif el.element_id == _BLOCK_ADD_MAPPING_ID:
                is_dovi, cfg = self._parse_block_addition_mapping(payload)
                if is_dovi:
                    has_bam = True
                    dovi_config_bytes = cfg
            pos = el.end
        return track_number, codec_id, has_bam, max_block_add_id, dovi_config_bytes

    def _parse_block_addition_mapping(self, bam_payload: bytes) -> tuple[bool, bytes | None]:
        pos = 0
        n = len(bam_payload)
        is_dovi = False
        extra_data = None
        while pos < n:
            try:
                el = self._base._read_ebml_element_from_bytes(bam_payload, pos)
            except ValueError:
                break
            if el.unknown_size:
                break
            if el.element_id == _BLOCK_ADD_ID_TYPE_ID:
                value_bytes = bam_payload[el.payload_offset:el.end]
                value = int.from_bytes(value_bytes, "big") if value_bytes else 0
                if value in (_FOURCC_DVCC, _FOURCC_DVVC):
                    is_dovi = True
            elif el.element_id == _BLOCK_ADD_ID_EXTRA_DATA_ID:
                extra_data = bam_payload[el.payload_offset:el.end]
            pos = el.end
        return is_dovi, extra_data

    def _block_addition_mapping_is_dovi(self, bam_payload: bytes) -> bool:
        is_dovi, _ = self._parse_block_addition_mapping(bam_payload)
        return is_dovi

    # ------------------------------------------------------------------
    # Construction du BlockAdditionMapping
    # ------------------------------------------------------------------

    def _build_block_addition_mapping_element(
        self,
        *,
        record: DolbyVisionConfigRecord,
        id_value: int,
        id_name: str,
        fourcc_override: int | str | None = None,
    ) -> bytes:
        """Construit l'élément complet ``BlockAdditionMapping`` (header + payload)."""
        fourcc_val = fourcc_override if fourcc_override is not None else record.fourcc
        if isinstance(fourcc_val, str):
            fourcc = _FOURCC_DVCC if fourcc_val.lower() == "dvcc" else _FOURCC_DVVC
        elif isinstance(fourcc_val, int):
            fourcc = fourcc_val
        else:
            fourcc = _FOURCC_DVVC if record.profile > 7 else _FOURCC_DVCC
        children = b"".join([
            self._build_uint_element(_BLOCK_ADD_ID_VALUE_ID, id_value),
            self._build_string_element(_BLOCK_ADD_ID_NAME_ID, id_name),
            self._build_uint_element(_BLOCK_ADD_ID_TYPE_ID, fourcc),
            self._build_binary_element(_BLOCK_ADD_ID_EXTRA_DATA_ID, record.to_bytes()),
        ])
        return self._wrap_element(_BLOCK_ADD_MAPPING_ID, children)

    def _wrap_element(self, element_id: bytes, payload: bytes) -> bytes:
        size_bytes = self._base._encode_ebml_size_prefer_length(
            len(payload), preferred_length=1,
        )
        return element_id + size_bytes + payload

    def _build_uint_element(self, element_id: bytes, value: int) -> bytes:
        encoded = self._base._encode_uint(value)
        return self._wrap_element(element_id, encoded)

    def _build_string_element(self, element_id: bytes, value: str) -> bytes:
        return self._wrap_element(element_id, value.encode("utf-8"))

    def _build_binary_element(self, element_id: bytes, value: bytes) -> bytes:
        return self._wrap_element(element_id, value)

    # ------------------------------------------------------------------
    # Injection dans le TrackEntry
    # ------------------------------------------------------------------

    def _inject_into_track_entry(
        self,
        old_entry_payload: bytes,
        bam_element: bytes,
        *,
        old_entry_header: bytes,
    ) -> bytes:
        """
        Reconstruit le TrackEntry complet en s'assurant que MaxBlockAdditionID >= 1,
        en append-ant le BlockAdditionMapping à la fin de son payload,
        et en réencodant la taille du TrackEntry.
        """
        _ = old_entry_header
        has_max_block_add_id = False
        rebuilt_children: list[bytes] = []
        pos = 0
        n = len(old_entry_payload)
        while pos < n:
            try:
                el = self._base._read_ebml_element_from_bytes(old_entry_payload, pos)
            except ValueError:
                break
            if el.unknown_size:
                break
            payload = old_entry_payload[el.payload_offset:el.end]
            if el.element_id == _MAX_BLOCK_ADD_ID:
                current_val = int.from_bytes(payload, "big") if payload else 0
                rebuilt_children.append(self._build_uint_element(_MAX_BLOCK_ADD_ID, max(current_val, 1)))
                has_max_block_add_id = True
            elif el.element_id == _BLOCK_ADD_MAPPING_ID:
                if not self._block_addition_mapping_is_dovi(payload):
                    rebuilt_children.append(old_entry_payload[el.offset:el.end])
                # Sinon on omet l'ancien mapping DOVI car bam_element le remplace à jour
            else:
                rebuilt_children.append(old_entry_payload[el.offset:el.end])
            pos = el.end

        if not has_max_block_add_id:
            rebuilt_children.append(self._build_uint_element(_MAX_BLOCK_ADD_ID, 1))
        rebuilt_children.append(bam_element)
        new_payload = b"".join(rebuilt_children)

        return _TRACK_ENTRY_ID + self._base._encode_ebml_size_prefer_length(
            len(new_payload), preferred_length=2,
        ) + new_payload


def sanitize_dovi_mkv(
    path: Path | str,
    *,
    fps: float | str | None = None,
    fourcc: str | int | None = "dvcC",
    target_compat_id: int | None = None,
    target_level: int | None = None,
) -> DoviBlockAdditionPatchResult:
    """Assainit un fichier MKV pour garantir un signal Dolby Vision valide et compatible.

    - Force le niveau Dolby Vision à une valeur supportée (Level 6 <=30fps, Level 9 <=60fps) au lieu de Level 10 (120fps).
    - Enforce MaxBlockAdditionID = 1.
    - Utilise le FourCC dvcC (ou celui spécifié) pour compatibilité maximale TV/ExoPlayer/Android TV.
    - Préserve fidèlement la compatibilité existante (compat_id 0, 1, 2, 4) sauf si target_compat_id est forcé.
    - Recalcule le CRC-32 du bloc Tracks.
    """
    path = Path(path)
    if not path.is_file():
        return DoviBlockAdditionPatchResult(applied=False, skipped=True, reason="Fichier introuvable.")

    editor = MatroskaDoviBlockAdditionEditor()
    with path.open("rb") as fh:
        try:
            _, tracks_payload = editor._read_tracks_payload(fh)
        except Exception as exc:
            return DoviBlockAdditionPatchResult(applied=False, skipped=True, reason=f"Lecture Tracks impossible: {exc}")

    entries = editor._parse_track_entries(tracks_payload)
    hevc_entries = [e for e in entries if e.is_hevc]
    if not hevc_entries:
        return DoviBlockAdditionPatchResult(applied=False, skipped=True, reason="Aucune piste HEVC.")

    # Déterminer le profil et level existants si possible
    profile = 8
    level = target_level if target_level is not None else sanitize_dovi_level(10, fps=fps)
    compat_id = target_compat_id
    first_hevc = hevc_entries[0]
    if first_hevc.dovi_config_bytes and len(first_hevc.dovi_config_bytes) >= 5:
        cfg = first_hevc.dovi_config_bytes
        profile = (cfg[2] >> 1) & 0x7F
        raw_level = ((cfg[2] & 0x01) << 5) | ((cfg[3] >> 3) & 0x1F)
        level = target_level if target_level is not None else sanitize_dovi_level(raw_level, fps=fps)
        raw_compat = (cfg[4] >> 4) & 0x0F
        compat_id = target_compat_id if target_compat_id is not None else raw_compat
    elif compat_id is None:
        compat_id = 1 if profile == 8 else 0

    record = DolbyVisionConfigRecord(
        profile=profile,
        level=level,
        rpu_present=True,
        el_present=False,
        bl_present=True,
        bl_signal_compat_id=compat_id,
        fourcc=fourcc,
    )
    return editor.patch(path, record=record, force=True, fourcc=fourcc)


__all__ = [
    "DolbyVisionConfigRecord",
    "DoviBlockAdditionPatchResult",
    "MatroskaDoviBlockAdditionEditor",
    "sanitize_dovi_level",
    "sanitize_dovi_mkv",
]
