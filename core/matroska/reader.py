"""Streaming-safe primitives for inspecting Matroska/EBML documents.

This module is intentionally codec agnostic: it exposes element boundaries and
raw payloads so the native remux planner can preserve packet bytes verbatim.
"""

from __future__ import annotations

from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from io import BytesIO
import mmap
from pathlib import Path
import struct
from typing import BinaryIO, Iterator


_CONTENT_ENCODINGS_ID = bytes.fromhex("6d80")
_CONTENT_ENCODING_ID = bytes.fromhex("6240")
_CONTENT_COMPRESSION_ID = bytes.fromhex("5034")
_CONTENT_ENCRYPTION_ID = bytes.fromhex("5035")
_INFO_DURATION_ID = bytes.fromhex("4489")


@dataclass(frozen=True)
class EbmlElement:
    element_id: bytes
    offset: int
    payload_offset: int
    size: int | None
    header_size: int

    @property
    def end(self) -> int | None:
        return None if self.size is None else self.payload_offset + self.size


def _vint_length(first: int) -> int:
    for length in range(1, 9):
        if first & (0x80 >> (length - 1)):
            return length
    raise ValueError("VINT EBML invalide")


def _read_exact(fh: BinaryIO, count: int) -> bytes:
    value = fh.read(count)
    if len(value) != count:
        raise ValueError("EBML tronqué")
    return value


def _read_vint_value(data: bytes, offset: int = 0) -> tuple[int, int]:
    if offset >= len(data):
        raise ValueError("VINT absent")
    length = _vint_length(data[offset])
    if offset + length > len(data):
        raise ValueError("VINT tronqué")
    value = data[offset] & (0xFF >> length)
    for byte in data[offset + 1:offset + length]:
        value = (value << 8) | byte
    return value, length


def _split_lace_sizes(payload: bytes, flags: int, total_payload_len: int) -> tuple[int, ...]:
    mode = (flags >> 1) & 0x03
    if mode == 0:
        return (total_payload_len,)
    if not payload:
        raise ValueError("En-tête de lacing absent")
    count = payload[0] + 1
    cursor = 1
    sizes: list[int] = []
    if mode == 1:  # Xiph
        for _ in range(count - 1):
            size = 0
            while True:
                if cursor >= len(payload):
                    raise ValueError("Lacing Xiph tronqué")
                byte = payload[cursor]
                cursor += 1
                size += byte
                if byte != 255:
                    break
            sizes.append(size)
    elif mode == 2:  # fixed
        remaining = total_payload_len - cursor
        if remaining % count:
            raise ValueError("Lacing fixed de taille non divisible")
        sizes = [remaining // count] * (count - 1)
    else:  # EBML
        first, length = _read_vint_value(payload, cursor)
        cursor += length
        sizes.append(first)
        for _ in range(count - 2):
            encoded, length = _read_vint_value(payload, cursor)
            cursor += length
            bias = (1 << (7 * length - 1)) - 1
            sizes.append(sizes[-1] + encoded - bias)
            if sizes[-1] < 0:
                raise ValueError("Lacing EBML avec taille négative")
    last = total_payload_len - cursor - sum(sizes)
    if last < 0:
        raise ValueError("Tailles de lacing hors payload")
    sizes.append(last)
    return tuple(sizes)


def _split_laces(payload: bytes, flags: int) -> tuple[bytes, ...]:
    mode = (flags >> 1) & 0x03
    if mode == 0:
        return (payload,)
    sizes = _split_lace_sizes(payload, flags, len(payload))
    cursor = len(payload) - sum(sizes)
    frames: list[bytes] = []
    for size in sizes:
        frames.append(payload[cursor:cursor + size])
        cursor += size
    if cursor != len(payload):
        raise ValueError("Payload de lacing incohérent")
    return tuple(frames)


def read_element(fh: BinaryIO, *, limit: int | None = None) -> EbmlElement | None:
    offset = fh.tell()
    if limit is not None and offset >= limit:
        return None
    first = fh.read(1)
    if not first:
        return None
    id_len = _vint_length(first[0])
    element_id = first + _read_exact(fh, id_len - 1)
    first_size = _read_exact(fh, 1)[0]
    size_len = _vint_length(first_size)
    raw_size = bytes([first_size]) + _read_exact(fh, size_len - 1)
    value = raw_size[0] & (0xFF >> size_len)
    for byte in raw_size[1:]:
        value = (value << 8) | byte
    unknown = value == (1 << (7 * size_len)) - 1
    payload_offset = fh.tell()
    size = None if unknown else value
    if size is not None and limit is not None and payload_offset + size > limit:
        raise ValueError("Élément EBML hors limites")
    return EbmlElement(element_id, offset, payload_offset, size, id_len + size_len)


def iter_children(fh: BinaryIO, parent: EbmlElement, *, file_size: int) -> Iterator[EbmlElement]:
    end = parent.end if parent.end is not None else file_size
    while fh.tell() < end:
        child = read_element(fh, limit=end)
        if child is None:
            return
        yield child
        child_end = child.end
        if child_end is None:
            # Unknown-size children are containers extending to their parent.
            return
        fh.seek(child_end)


def payload_children(payload: bytes) -> Iterator[tuple[bytes, bytes]]:
    stream = BytesIO(payload)
    while stream.tell() < len(payload):
        child = read_element(stream, limit=len(payload))
        if child is None or child.size is None:
            raise ValueError("Conteneur EBML invalide")
        stream.seek(child.payload_offset)
        yield child.element_id, _read_exact(stream, child.size)
        child_end = child.end
        if child_end is None:
            raise ValueError("Conteneur EBML de taille inconnue")
        stream.seek(child_end)


class MatroskaReader:
    """Read top-level Matroska elements without loading media payloads."""

    SEGMENT_ID = bytes.fromhex("18538067")
    TRACKS_ID = bytes.fromhex("1654ae6b")
    TRACK_ENTRY_ID = bytes.fromhex("ae")
    TRACK_NUMBER_ID = bytes.fromhex("d7")
    TRACK_UID_ID = bytes.fromhex("73c5")
    TRACK_TYPE_ID = bytes.fromhex("83")
    CODEC_ID = bytes.fromhex("86")
    CODEC_PRIVATE = bytes.fromhex("63a2")
    LANGUAGE = bytes.fromhex("22b59c")
    LANGUAGE_BCP47 = bytes.fromhex("22b59d")
    NAME = bytes.fromhex("536e")
    FLAG_ENABLED_ID = bytes.fromhex("b9")
    FLAG_DEFAULT_ID = bytes.fromhex("88")
    FLAG_FORCED_ID = bytes.fromhex("55aa")
    FLAG_HEARING_IMPAIRED_ID = bytes.fromhex("55ab")
    FLAG_VISUAL_IMPAIRED_ID = bytes.fromhex("55ac")
    FLAG_ORIGINAL_ID = bytes.fromhex("55ae")
    FLAG_COMMENTARY_ID = bytes.fromhex("55af")
    DEFAULT_DURATION_ID = bytes.fromhex("23e383")
    VIDEO_ID = bytes.fromhex("e0")
    AUDIO_ID = bytes.fromhex("e1")
    COLOUR_ID = bytes.fromhex("55b0")
    MASTERING_METADATA_ID = bytes.fromhex("55d0")
    BLOCK_ADDITION_MAPPING_ID = bytes.fromhex("41e4")
    #: Tampon des parcours qui sautent les payloads (en-têtes seuls). Une page
    #: mesure le meilleur compromis : au-delà, le gain de temps est marginal
    #: alors que le volume lu — et l'éviction du cache — croît vite.
    _SKIPPING_READ_BUFFER = 4096

    CLUSTER_ID = bytes.fromhex("1f43b675")
    TIMESTAMP_ID = bytes.fromhex("e7")
    SIMPLE_BLOCK_ID = bytes.fromhex("a3")
    BLOCK_GROUP_ID = bytes.fromhex("a0")
    BLOCK_ID = bytes.fromhex("a1")
    BLOCK_DURATION_ID = bytes.fromhex("9b")
    REFERENCE_BLOCK_ID = bytes.fromhex("fb")
    DISCARD_PADDING_ID = bytes.fromhex("75a2")
    CODEC_STATE_ID = bytes.fromhex("a4")
    BLOCK_ADDITIONS_ID = bytes.fromhex("75a1")
    INFO_ID = bytes.fromhex("1549a966")
    #: Éléments level-1 obligatoires : leur absence de l'index n'est jamais crue.
    _MANDATORY_LEVEL1_IDS = frozenset({INFO_ID, TRACKS_ID})
    VOID_ID = bytes.fromhex("ec")
    MUXING_APP_ID = bytes.fromhex("4d80")
    WRITING_APP_ID = bytes.fromhex("5741")
    TITLE_ID = bytes.fromhex("7ba9")
    TIMESTAMP_SCALE_ID = bytes.fromhex("2ad7b1")
    ATTACHMENTS_ID = bytes.fromhex("1941a469")
    ATTACHED_FILE_ID = bytes.fromhex("61a7")
    FILE_DESCRIPTION_ID = bytes.fromhex("467e")
    FILE_NAME_ID = bytes.fromhex("466e")
    FILE_MEDIA_TYPE_ID = bytes.fromhex("4660")
    FILE_DATA_ID = bytes.fromhex("465c")
    FILE_UID_ID = bytes.fromhex("46ae")
    CHAPTERS_ID = bytes.fromhex("1043a770")
    EDITION_ENTRY_ID = bytes.fromhex("45b9")
    EDITION_UID_ID = bytes.fromhex("45bc")
    CHAPTER_ATOM_ID = bytes.fromhex("b6")
    CHAPTER_UID_ID = bytes.fromhex("73c4")
    CHAPTER_TIME_START_ID = bytes.fromhex("91")
    CHAPTER_TIME_END_ID = bytes.fromhex("92")
    CHAPTER_DISPLAY_ID = bytes.fromhex("80")
    CHAP_STRING_ID = bytes.fromhex("85")
    CHAP_LANGUAGE_ID = bytes.fromhex("437c")
    TAGS_ID = bytes.fromhex("1254c367")
    TAG_ID = bytes.fromhex("7373")
    TARGETS_ID = bytes.fromhex("63c0")
    SIMPLE_TAG_ID = bytes.fromhex("67c8")
    TAG_NAME_ID = bytes.fromhex("45a3")
    TAG_STRING_ID = bytes.fromhex("4487")
    SEEK_HEAD_ID = bytes.fromhex("114d9b74")
    SEEK_ID = bytes.fromhex("4dbb")
    SEEK_ID_FIELD_ID = bytes.fromhex("53ab")
    SEEK_POSITION_ID = bytes.fromhex("53ac")
    LEVEL1_IDS = frozenset({
        bytes.fromhex(value) for value in (
            "114d9b74", "1549a966", "1654ae6b", "1f43b675",
            "1c53bb6b", "1941a469", "1043a770", "1254c367",
        )
    })

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        # Mémoïsation niveau instance : segment et tracks sont reparcourus
        # plusieurs fois par compilation de plan (capacités natives + contrat de
        # sortie). Un reader étant lié à un seul thread et jetable, le cache est
        # sûr et sans risque de péremption (un fichier modifié => nouveau reader).
        self._segment_cache: EbmlElement | None = None
        self._tracks_cache: tuple["MatroskaTrack", ...] | None = None
        self._clusters_cache: list[EbmlElement] | None = None
        self._metadata_cache: tuple[EbmlElement, ...] | None = None
        self._metadata_exhaustive = False
        self._metadata_indexed = False

    def segment(self) -> EbmlElement:
        if self._segment_cache is not None:
            return self._segment_cache
        with self.path.open("rb") as fh:
            size = self.path.stat().st_size
            while element := read_element(fh, limit=size):
                if element.element_id == self.SEGMENT_ID:
                    self._segment_cache = element
                    return element
                if element.end is None:
                    break
                fh.seek(element.end)
        raise ValueError("Segment Matroska introuvable")

    def top_level(self) -> Iterator[EbmlElement]:
        size = self.path.stat().st_size
        with self.path.open("rb") as fh:
            segment = self.segment()
            fh.seek(segment.payload_offset)
            segment_end = segment.end if segment.end is not None else size
            while fh.tell() < segment_end:
                item = read_element(fh, limit=segment_end)
                if item is None:
                    return
                if item.size is not None:
                    yield item
                    item_end = item.end
                    if item_end is None:
                        raise ValueError("Élément Matroska de taille inconnue")
                    fh.seek(item_end)
                    continue
                if item.element_id != self.CLUSTER_ID:
                    yield item
                    return
                # Unknown-size Clusters end at the next level-1 element.
                cursor = item.payload_offset
                boundary = segment_end
                fh.seek(cursor)
                while fh.tell() < segment_end:
                    child_offset = fh.tell()
                    child = read_element(fh, limit=segment_end)
                    if child is None:
                        break
                    if child.element_id in self.LEVEL1_IDS:
                        boundary = child_offset
                        break
                    if child.end is None:
                        boundary = segment_end
                        break
                    fh.seek(child.end)
                yield EbmlElement(
                    item.element_id, item.offset, item.payload_offset,
                    boundary - item.payload_offset, item.header_size,
                )
                fh.seek(boundary)

    def _metadata_elements(self) -> tuple[EbmlElement, ...]:
        """Éléments level-1 hors Clusters, sans parcourir les Clusters.

        Ceux qui précèdent le premier Cluster sont lus directement ; ceux qui
        le suivent (Cues, Tags, ou Tracks déplacé en fin de segment par un
        éditeur) sont retrouvés par le SeekHead, comme le font les lecteurs
        usuels ; un élément absent d'un SeekHead cohérent est absent du fichier
        (sauf Info/Tracks, obligatoires). Un élément non indexé juste après un
        élément indexé de fin de segment rend l'index incohérent. Sans SeekHead, seuls les éléments
        précédant les Clusters sont mémorisés : le parcours complet est différé
        jusqu'à une recherche infructueuse ou une énumération. Une entrée
        incohérente impose le repli.
        """
        if self._metadata_cache is not None:
            return self._metadata_cache
        size = self.path.stat().st_size
        segment = self.segment()
        segment_end = segment.end if segment.end is not None else size
        found: dict[int, EbmlElement] = {}
        reached_cluster = False
        first_cluster_offset = segment_end
        consistent = True
        with self.path.open("rb") as fh:
            fh.seek(segment.payload_offset)
            while fh.tell() < segment_end:
                item = read_element(fh, limit=segment_end)
                if item is None:
                    break
                if item.element_id == self.CLUSTER_ID:
                    reached_cluster = True
                    first_cluster_offset = item.offset
                    break
                found[item.offset] = item
                if item.end is None:
                    break
                fh.seek(item.end)
            pending = [item for item in found.values() if item.element_id == self.SEEK_HEAD_ID]
            indexed = bool(pending)
            visited: set[int] = set()
            while reached_cluster and consistent and pending:
                head = pending.pop()
                if head.offset in visited:
                    continue
                visited.add(head.offset)
                try:
                    entries = self._seek_entries(head)
                    for target_id, position in entries:
                        offset = segment.payload_offset + position
                        if target_id == self.CLUSTER_ID:
                            continue
                        known = found.get(offset)
                        if known is not None:
                            if known.element_id != target_id:
                                consistent = False
                                break
                            continue
                        if offset >= segment_end:
                            consistent = False
                            break
                        fh.seek(offset)
                        item = read_element(fh, limit=segment_end)
                        if item is None or item.element_id != target_id or item.size is None:
                            consistent = False
                            break
                        found[offset] = item
                        if item.element_id == self.SEEK_HEAD_ID:
                            pending.append(item)
                except ValueError:
                    consistent = False
            if reached_cluster and consistent and indexed:
                consistent = self._index_tail_complete(fh, found, first_cluster_offset, segment_end)
        if not consistent:
            return self._exhaustive_metadata_elements()
        self._metadata_cache = tuple(sorted(found.values(), key=lambda item: item.offset))
        self._metadata_exhaustive = not reached_cluster
        self._metadata_indexed = indexed
        return self._metadata_cache

    def _index_tail_complete(
        self,
        fh: BinaryIO,
        found: dict[int, EbmlElement],
        first_cluster_offset: int,
        segment_end: int,
    ) -> bool:
        """Faux si un élément level-1 non indexé suit un élément indexé placé après
        les Clusters (Void ignorés) : Tags ajoutés en fin de fichier sans mise à
        jour du SeekHead. Aucune lecture si ces éléments finissent le segment."""
        for item in tuple(found.values()):
            if item.offset < first_cluster_offset or item.end is None:
                continue
            cursor = item.end
            while cursor < segment_end and cursor not in found:
                fh.seek(cursor)
                try:
                    follower = read_element(fh, limit=segment_end)
                except ValueError:
                    break
                if follower is None:
                    break
                if follower.element_id == self.VOID_ID and follower.end is not None:
                    cursor = follower.end
                    continue
                if follower.element_id in self.LEVEL1_IDS and follower.element_id != self.CLUSTER_ID:
                    return False
                break
        return True

    def _exhaustive_metadata_elements(self) -> tuple[EbmlElement, ...]:
        """Parcours linéaire complet (repli lent : chaque Cluster est visité)."""
        if not self._metadata_exhaustive:
            self._metadata_cache = tuple(
                item for item in self.top_level() if item.element_id != self.CLUSTER_ID
            )
            self._metadata_exhaustive = True
        return self._metadata_cache or ()

    def tracks_indexed_for_strict_readers(self) -> bool:
        """Tracks trouvable par un lecteur strict (matroska-demuxer de dovi_tool / hdr10plus_tool).

        Ces lecteurs ne suivent que le premier SeekHead (et les SeekHead qu'il
        référence) et ne parcourent pas les éléments level-1 dès qu'il en existe
        un : un second SeekHead non chaîné (RFC 9559 §6.3 non respectée) rend
        Tracks introuvable. Sans SeekHead, Tracks doit précéder les Clusters.
        Seuls les en-têtes du début de fichier et les SeekHead sont lus.
        """
        size = self.path.stat().st_size
        segment = self.segment()
        segment_end = segment.end if segment.end is not None else size
        first_head: EbmlElement | None = None
        tracks_before_clusters = False
        with self.path.open("rb") as fh:
            fh.seek(segment.payload_offset)
            while fh.tell() < segment_end:
                item = read_element(fh, limit=segment_end)
                if item is None or item.element_id == self.CLUSTER_ID:
                    break
                if item.element_id == self.SEEK_HEAD_ID and first_head is None:
                    first_head = item
                tracks_before_clusters |= item.element_id == self.TRACKS_ID
                if item.end is None:
                    break
                fh.seek(item.end)
            if first_head is None:
                return tracks_before_clusters
            pending, visited = [first_head], set()
            while pending:
                head = pending.pop()
                if head.offset in visited or head.size is None:
                    continue
                visited.add(head.offset)
                for target_id, position in self._seek_entries(head):
                    if target_id not in (self.TRACKS_ID, self.SEEK_HEAD_ID):
                        continue
                    # L'entrée ne vaut que si elle désigne réellement l'élément annoncé.
                    offset = segment.payload_offset + position
                    if offset >= segment_end:
                        continue
                    fh.seek(offset)
                    try:
                        pointed = read_element(fh, limit=segment_end)
                    except ValueError:
                        continue
                    if pointed is None or pointed.element_id != target_id or pointed.size is None:
                        continue
                    if target_id == self.TRACKS_ID:
                        return True
                    pending.append(pointed)
        return False

    def _seek_entries(self, head: EbmlElement) -> list[tuple[bytes, int]]:
        """Entrées ``(SeekID, SeekPosition)`` d'un SeekHead."""
        entries: list[tuple[bytes, int]] = []
        for seek_id, seek_payload in payload_children(self.payload(head)):
            if seek_id != self.SEEK_ID:
                continue
            target_id = b""
            position: int | None = None
            for child_id, value in payload_children(seek_payload):
                if child_id == self.SEEK_ID_FIELD_ID:
                    target_id = value
                elif child_id == self.SEEK_POSITION_ID:
                    position = int.from_bytes(value, "big")
            if target_id and position is not None:
                entries.append((target_id, position))
        return entries

    def _level1(self, element_id: bytes) -> tuple[EbmlElement, ...]:
        """Éléments level-1 d'un ID (ordre du fichier), Clusters exclus."""
        if element_id == self.CLUSTER_ID:
            return tuple(self.cluster_elements())
        items = tuple(item for item in self._metadata_elements() if item.element_id == element_id)
        # Sans index, une énumération doit aussi retrouver les éléments répétés
        # après les Clusters ; un SeekHead cohérent fait foi (comme les lecteurs
        # usuels) : élément facultatif non indexé = absent, sans parcours.
        if not self._metadata_indexed or (not items and element_id in self._MANDATORY_LEVEL1_IDS):
            items = tuple(item for item in self._exhaustive_metadata_elements() if item.element_id == element_id)
        return items

    def _first_level1(self, element_id: bytes) -> EbmlElement | None:
        """Premier élément d'un ID ; parcours complet seulement sans index ou pour Info/Tracks introuvables."""
        for item in self._metadata_elements():
            if item.element_id == element_id:
                return item
        if self._metadata_indexed and element_id not in self._MANDATORY_LEVEL1_IDS:
            return None
        return next((item for item in self._exhaustive_metadata_elements() if item.element_id == element_id), None)

    def payload(self, element: EbmlElement) -> bytes:
        if element.size is None:
            raise ValueError("Un élément de taille inconnue ne peut pas être lu brut")
        with self.path.open("rb") as fh:
            fh.seek(element.payload_offset)
            return _read_exact(fh, element.size)

    def raw_element(self, element: EbmlElement) -> bytes:
        if element.size is None:
            raise ValueError("Copie brute impossible pour une taille inconnue")
        with self.path.open("rb") as fh:
            fh.seek(element.offset)
            return _read_exact(fh, element.header_size + element.size)

    def raw_top_level(self, element_id: bytes) -> tuple[bytes, ...]:
        return tuple(self.raw_element(item) for item in self._level1(element_id))

    def attachment_headers(self) -> list["MatroskaAttachmentHeader"]:
        """Métadonnées d'attachments sans charger les contenus ``FileData``."""
        root = self._first_level1(self.ATTACHMENTS_ID)
        if root is None:
            return []
        result: list[MatroskaAttachmentHeader] = []
        size = self.path.stat().st_size
        with self.path.open("rb") as fh:
            fh.seek(root.payload_offset)
            for attached in iter_children(fh, root, file_size=size):
                if attached.element_id != self.ATTACHED_FILE_ID:
                    continue
                values: dict[bytes, bytes] = {}
                data_offset = 0
                data_size = 0
                fh.seek(attached.payload_offset)
                for child in iter_children(fh, attached, file_size=size):
                    if child.size is None:
                        continue
                    if child.element_id == self.FILE_DATA_ID:
                        data_offset = child.payload_offset
                        data_size = child.size
                    else:
                        fh.seek(child.payload_offset)
                        values[child.element_id] = _read_exact(fh, child.size)
                def text(key: bytes) -> str:
                    return values.get(key, b"").decode("utf-8", "replace").rstrip("\0")

                result.append(MatroskaAttachmentHeader(
                    uid=int.from_bytes(values.get(self.FILE_UID_ID, b"\0"), "big"),
                    name=text(self.FILE_NAME_ID), media_type=text(self.FILE_MEDIA_TYPE_ID),
                    description=text(self.FILE_DESCRIPTION_ID), size=data_size,
                    data_offset=data_offset,
                ))
        return result

    def attachments(self) -> list["MatroskaAttachment"]:
        result: list[MatroskaAttachment] = []
        with self.path.open("rb") as fh:
            for header in self.attachment_headers():
                fh.seek(header.data_offset)
                result.append(MatroskaAttachment(
                    uid=header.uid,
                    name=header.name,
                    media_type=header.media_type,
                    description=header.description,
                    data=_read_exact(fh, header.size),
                ))
        return result

    def attachment_data(self, attachment_index: int) -> bytes:
        """Read one attachment payload without loading the other payloads."""
        if attachment_index < 0:
            raise ValueError("Index d'attachement négatif.")
        headers = self.attachment_headers()
        try:
            header = headers[attachment_index]
        except IndexError as exc:
            raise ValueError(
                f"Attachement Matroska introuvable à l'index {attachment_index}."
            ) from exc
        if header.data_offset <= 0:
            raise ValueError(
                "Attachement Matroska incomplet: FileData manquant pour "
                f"{header.name or attachment_index}."
            )
        with self.path.open("rb") as fh:
            fh.seek(header.data_offset)
            return _read_exact(fh, header.size)

    def chapter_editions(self) -> tuple["MatroskaEdition", ...]:
        editions: list[MatroskaEdition] = []

        def parse_atom(payload: bytes) -> MatroskaChapter:
            uid = start = 0
            end: int | None = None
            displays: list[tuple[str, str]] = []
            children: list[MatroskaChapter] = []
            for element_id, value in payload_children(payload):
                if element_id == self.CHAPTER_UID_ID:
                    uid = int.from_bytes(value, "big")
                elif element_id == self.CHAPTER_TIME_START_ID:
                    start = int.from_bytes(value, "big")
                elif element_id == self.CHAPTER_TIME_END_ID:
                    end = int.from_bytes(value, "big")
                elif element_id == self.CHAPTER_DISPLAY_ID:
                    name = ""
                    language = "und"
                    for display_id, display_value in payload_children(value):
                        if display_id == self.CHAP_STRING_ID:
                            name = display_value.decode("utf-8", "replace").rstrip("\0")
                        elif display_id == self.CHAP_LANGUAGE_ID:
                            language = display_value.decode("ascii", "replace").rstrip("\0")
                    displays.append((name, language))
                elif element_id == self.CHAPTER_ATOM_ID:
                    children.append(parse_atom(value))
            return MatroskaChapter(uid, start, end, tuple(displays), tuple(children))

        for raw in self.raw_top_level(self.CHAPTERS_ID):
            for element_id, value in payload_children(self._raw_payload(raw)):
                if element_id != self.EDITION_ENTRY_ID:
                    continue
                uid = 0
                chapters: list[MatroskaChapter] = []
                for edition_id, edition_value in payload_children(value):
                    if edition_id == self.EDITION_UID_ID:
                        uid = int.from_bytes(edition_value, "big")
                    elif edition_id == self.CHAPTER_ATOM_ID:
                        chapters.append(parse_atom(edition_value))
                editions.append(MatroskaEdition(uid, tuple(chapters)))
        return tuple(editions)

    @staticmethod
    def _raw_payload(raw: bytes) -> bytes:
        stream = BytesIO(raw)
        element = read_element(stream, limit=len(raw))
        if element is None or element.size is None:
            raise ValueError("Élément EBML brut invalide")
        return raw[element.payload_offset:element.end]

    def tags(self) -> tuple["MatroskaTag", ...]:
        tags: list[MatroskaTag] = []
        for raw in self.raw_top_level(self.TAGS_ID):
            for element_id, tag_payload in payload_children(self._raw_payload(raw)):
                if element_id != self.TAG_ID:
                    continue
                targets: dict[str, int] = {}
                values: list[tuple[str, str]] = []
                for tag_id, value in payload_children(tag_payload):
                    if tag_id == self.TARGETS_ID:
                        targets.update({child_id.hex(): int.from_bytes(child_value, "big") for child_id, child_value in payload_children(value)})
                    elif tag_id == self.SIMPLE_TAG_ID:
                        name = text = ""
                        for simple_id, simple_value in payload_children(value):
                            if simple_id == self.TAG_NAME_ID:
                                name = simple_value.decode("utf-8", "replace").rstrip("\0")
                            elif simple_id == self.TAG_STRING_ID:
                                text = simple_value.decode("utf-8", "replace").rstrip("\0")
                        values.append((name, text))
                tags.append(MatroskaTag(targets, tuple(values)))
        return tuple(tags)

    def tracks(self) -> list["MatroskaTrack"]:
        """Return core TrackEntry metadata while retaining its raw EBML body."""
        if self._tracks_cache is not None:
            return list(self._tracks_cache)
        tracks_element = self._first_level1(self.TRACKS_ID)
        if tracks_element is None:
            self._tracks_cache = ()
            return []
        out: list[MatroskaTrack] = []
        size = self.path.stat().st_size
        with self.path.open("rb") as fh:
            fh.seek(tracks_element.payload_offset)
            for entry in iter_children(fh, tracks_element, file_size=size):
                if entry.element_id != self.TRACK_ENTRY_ID or entry.size is None:
                    continue
                fields: dict[bytes, bytes] = {}
                fh.seek(entry.payload_offset)
                for child in iter_children(fh, entry, file_size=size):
                    if child.size is not None and child.element_id in {
                        self.TRACK_NUMBER_ID, self.TRACK_UID_ID, self.TRACK_TYPE_ID,
                        self.CODEC_ID, self.CODEC_PRIVATE, self.LANGUAGE,
                        self.LANGUAGE_BCP47, self.NAME,
                        self.FLAG_ENABLED_ID, self.FLAG_DEFAULT_ID, self.FLAG_FORCED_ID,
                        self.FLAG_HEARING_IMPAIRED_ID, self.FLAG_VISUAL_IMPAIRED_ID,
                        self.FLAG_ORIGINAL_ID, self.FLAG_COMMENTARY_ID,
                        self.DEFAULT_DURATION_ID,
                    }:
                        fields[child.element_id] = self.payload(child)
                def uint(key: bytes, default: int = 0) -> int:
                    return int.from_bytes(fields.get(key, b""), "big") if fields.get(key) else default
                def text(key: bytes) -> str:
                    return fields.get(key, b"").decode("utf-8", "replace").rstrip("\0")
                nested = {child_id: value for child_id, value in payload_children(self.payload(entry))}
                video: dict[str, int | float] = {}
                audio: dict[str, int | float] = {}
                block_addition_mappings: list[dict[str, int | str | bytes]] = []

                def uint_values(payload: bytes, names: dict[bytes, str]) -> dict[str, int]:
                    return {
                        names[element_id]: int.from_bytes(value, "big")
                        for element_id, value in payload_children(payload)
                        if element_id in names
                    }

                def float_value(value: bytes) -> float:
                    if len(value) == 4:
                        return float(struct.unpack(">f", value)[0])
                    if len(value) == 8:
                        return float(struct.unpack(">d", value)[0])
                    raise ValueError("Float EBML de taille invalide")

                video_names = {
                    bytes.fromhex(key): name for key, name in {
                        "b0": "pixel_width", "ba": "pixel_height",
                        "54b0": "display_width", "54ba": "display_height",
                        "54b2": "display_unit", "54b3": "aspect_ratio_type",
                        "9a": "flag_interlaced", "9d": "field_order",
                        "53b8": "stereo_mode", "53c0": "alpha_mode",
                        "54aa": "pixel_crop_bottom", "54bb": "pixel_crop_top",
                        "54cc": "pixel_crop_left", "54dd": "pixel_crop_right",
                    }.items()
                }
                colour_names = {
                    bytes.fromhex(key): name for key, name in {
                        "55b1": "matrix_coefficients", "55b2": "bits_per_channel",
                        "55b3": "chroma_subsampling_horz", "55b4": "chroma_subsampling_vert",
                        "55b5": "cb_subsampling_horz", "55b6": "cb_subsampling_vert",
                        "55b7": "chroma_siting_horz", "55b8": "chroma_siting_vert",
                        "55b9": "range", "55ba": "transfer_characteristics",
                        "55bb": "primaries", "55bc": "max_cll", "55bd": "max_fall",
                    }.items()
                }
                mastering_names = {
                    bytes.fromhex(key): name for key, name in {
                        "55d1": "primary_r_x", "55d2": "primary_r_y",
                        "55d3": "primary_g_x", "55d4": "primary_g_y",
                        "55d5": "primary_b_x", "55d6": "primary_b_y",
                        "55d7": "white_point_x", "55d8": "white_point_y",
                        "55d9": "luminance_max", "55da": "luminance_min",
                    }.items()
                }
                if self.VIDEO_ID in nested:
                    video.update(uint_values(nested[self.VIDEO_ID], video_names))
                    video_children = dict(payload_children(nested[self.VIDEO_ID]))
                    colour_payload = video_children.get(self.COLOUR_ID)
                    if colour_payload is not None:
                        video.update(uint_values(colour_payload, colour_names))
                        colour_children = dict(payload_children(colour_payload))
                        mastering = colour_children.get(self.MASTERING_METADATA_ID)
                        if mastering is not None:
                            for element_id, value in payload_children(mastering):
                                if element_id in mastering_names:
                                    video[mastering_names[element_id]] = float_value(value)
                if self.AUDIO_ID in nested:
                    audio_names = {
                        bytes.fromhex("9f"): "channels", bytes.fromhex("6264"): "bit_depth",
                    }
                    audio.update(uint_values(nested[self.AUDIO_ID], audio_names))
                    for element_id, value in payload_children(nested[self.AUDIO_ID]):
                        if element_id == bytes.fromhex("b5"):
                            audio["sampling_frequency"] = float_value(value)
                        elif element_id == bytes.fromhex("78b5"):
                            audio["output_sampling_frequency"] = float_value(value)
                for element_id, value in payload_children(self.payload(entry)):
                    if element_id != self.BLOCK_ADDITION_MAPPING_ID:
                        continue
                    mapping: dict[str, int | str | bytes] = {}
                    for mapping_id, mapping_value in payload_children(value):
                        if mapping_id == bytes.fromhex("41f0"):
                            mapping["value"] = int.from_bytes(mapping_value, "big")
                        elif mapping_id == bytes.fromhex("41a4"):
                            mapping["name"] = mapping_value.decode("utf-8", "replace").rstrip("\0")
                        elif mapping_id == bytes.fromhex("41e7"):
                            mapping["type"] = int.from_bytes(mapping_value, "big")
                        elif mapping_id == bytes.fromhex("41ed"):
                            mapping["extra_data"] = mapping_value
                    block_addition_mappings.append(mapping)
                out.append(MatroskaTrack(
                    number=uint(self.TRACK_NUMBER_ID), uid=uint(self.TRACK_UID_ID),
                    track_type=uint(self.TRACK_TYPE_ID), codec_id=text(self.CODEC_ID),
                    codec_private=fields.get(self.CODEC_PRIVATE, b""),
                    language_bcp47=text(self.LANGUAGE_BCP47), language=text(self.LANGUAGE) or "und",
                    name=text(self.NAME), raw_entry=self.payload(entry),
                    default_duration_ns=uint(self.DEFAULT_DURATION_ID),
                    video=video, audio=audio,
                    block_addition_mappings=tuple(block_addition_mappings),
                    flag_enabled=bool(uint(self.FLAG_ENABLED_ID, 1)),
                    flag_default=bool(uint(self.FLAG_DEFAULT_ID, 1)),
                    flag_forced=bool(uint(self.FLAG_FORCED_ID, 0)),
                    flag_hearing_impaired=bool(uint(self.FLAG_HEARING_IMPAIRED_ID, 0)),
                    flag_visual_impaired=bool(uint(self.FLAG_VISUAL_IMPAIRED_ID, 0)),
                    flag_original=bool(uint(self.FLAG_ORIGINAL_ID, 0)),
                    flag_commentary=bool(uint(self.FLAG_COMMENTARY_ID, 0)),
                ))
        self._tracks_cache = tuple(out)
        return out

    def content_encodings_by_track(self) -> list[tuple[bool, bool]]:
        """Retourne ``(compression, chiffrement)`` pour chaque piste, dans l'ordre du fichier."""
        from io import BytesIO

        container_ids = {_CONTENT_ENCODINGS_ID, _CONTENT_ENCODING_ID}
        capabilities: list[tuple[bool, bool]] = []

        def inspect(payload: bytes, state: dict[str, bool]) -> None:
            stream = BytesIO(payload)
            while stream.tell() < len(payload):
                child = read_element(stream, limit=len(payload))
                if child is None or child.size is None:
                    raise ValueError("ContentEncodings EBML invalide")
                stream.seek(child.payload_offset)
                child_payload = _read_exact(stream, child.size)
                if child.element_id == _CONTENT_COMPRESSION_ID:
                    state["compression"] = True
                elif child.element_id == _CONTENT_ENCRYPTION_ID:
                    state["encryption"] = True
                elif child.element_id in container_ids:
                    inspect(child_payload, state)
                child_end = child.end
                if child_end is None:
                    raise ValueError("ContentEncodings de taille inconnue")
                stream.seek(child_end)

        for track in self.tracks():
            state = {"compression": False, "encryption": False}
            stream_payload = track.raw_entry
            stream = BytesIO(stream_payload)
            while stream.tell() < len(stream_payload):
                child = read_element(stream, limit=len(stream_payload))
                if child is None or child.size is None:
                    raise ValueError("TrackEntry EBML invalide")
                if child.element_id == _CONTENT_ENCODINGS_ID:
                    stream.seek(child.payload_offset)
                    inspect(_read_exact(stream, child.size), state)
                child_end = child.end
                if child_end is None:
                    raise ValueError("TrackEntry de taille inconnue")
                stream.seek(child_end)
            capabilities.append((state["compression"], state["encryption"]))
        return capabilities

    def content_encoding_capabilities(self) -> tuple[bool, bool]:
        """Return ``(uses_compression, uses_encryption)`` for all tracks."""
        compression = False
        encryption = False
        for track_compression, track_encryption in self.content_encodings_by_track():
            compression = compression or track_compression
            encryption = encryption or track_encryption
        return compression, encryption

    def segment_duration_ns(self) -> int | None:
        """Durée du segment en nanosecondes (Info.Duration × TimestampScale), ou None."""
        info = self._first_level1(self.INFO_ID)
        if info is None:
            return None
        size = self.path.stat().st_size
        duration_raw: bytes | None = None
        with self.path.open("rb") as fh:
            fh.seek(info.payload_offset)
            for child in iter_children(fh, info, file_size=size):
                if child.element_id == _INFO_DURATION_ID and child.size is not None:
                    duration_raw = self.payload(child)
        if not duration_raw:
            return None
        if len(duration_raw) == 4:
            duration_ticks = float(struct.unpack(">f", duration_raw)[0])
        elif len(duration_raw) == 8:
            duration_ticks = float(struct.unpack(">d", duration_raw)[0])
        else:
            raise ValueError("Info.Duration EBML de taille invalide")
        return round(duration_ticks * self.timestamp_scale_ns())

    def segment_info_apps(self) -> tuple[str, str]:
        info = self._first_level1(self.INFO_ID)
        if info is None:
            return "", ""
        values: dict[bytes, str] = {}
        size = self.path.stat().st_size
        with self.path.open("rb") as fh:
            fh.seek(info.payload_offset)
            for child in iter_children(fh, info, file_size=size):
                if child.element_id in {self.MUXING_APP_ID, self.WRITING_APP_ID} and child.size is not None:
                    values[child.element_id] = self.payload(child).decode("utf-8", "replace").rstrip("\0")
        return values.get(self.MUXING_APP_ID, ""), values.get(self.WRITING_APP_ID, "")

    def segment_title(self) -> str:
        info = self._first_level1(self.INFO_ID)
        if info is None:
            return ""
        size = self.path.stat().st_size
        with self.path.open("rb") as fh:
            fh.seek(info.payload_offset)
            for child in iter_children(fh, info, file_size=size):
                if child.element_id == self.TITLE_ID and child.size is not None:
                    return self.payload(child).decode("utf-8", "replace").rstrip("\0")
        return ""

    def timestamp_scale_ns(self) -> int:
        info = self._first_level1(self.INFO_ID)
        if info is None:
            return 1_000_000
        size = self.path.stat().st_size
        with self.path.open("rb") as fh:
            fh.seek(info.payload_offset)
            for child in iter_children(fh, info, file_size=size):
                if child.element_id == self.TIMESTAMP_SCALE_ID and child.size is not None:
                    return int.from_bytes(self.payload(child), "big") or 1_000_000
        return 1_000_000

    @staticmethod
    def _decode_block(
        raw: bytes,
        cluster_timestamp: int,
        *,
        duration_ms: int | None = None,
        references: tuple[int, ...] = (),
        discard_padding_ns: int = 0,
        codec_state: bytes = b"",
        block_additions: bytes = b"",
        duration_ns: int | None = None,
        references_ns: tuple[int, ...] = (),
        is_keyframe: bool | None = None,
    ) -> tuple["MatroskaBlock", ...]:
        track_no, length = _read_vint_value(raw)
        if len(raw) < length + 3:
            raise ValueError("Block Matroska tronqué")
        relative = int.from_bytes(raw[length:length + 2], "big", signed=True)
        flags = raw[length + 2]
        encoded_frames_payload = raw[length + 3:]
        frames = _split_laces(encoded_frames_payload, flags)
        return tuple(MatroskaBlock(
            track_number=track_no, timestamp_ms=cluster_timestamp + relative,
            flags=flags, payload=frame, lace_index=index, lace_count=len(frames),
            duration_ms=duration_ms,
            references=references,
            discard_padding_ns=discard_padding_ns,
            codec_state=codec_state,
            block_additions=block_additions,
            duration_ns=duration_ns,
            references_ns=references_ns,
            lacing_mode=(flags >> 1) & 0x03,
            encoded_frames_payload=encoded_frames_payload,
            is_keyframe=bool(flags & 0x80) if is_keyframe is None else is_keyframe,
            payload_bytes=len(frame),
        ) for index, frame in enumerate(frames))

    @classmethod
    def _decode_block_header_only_stream(
        cls,
        fh: BinaryIO,
        payload_offset: int,
        elem_size: int,
        cluster_timestamp: int,
        *,
        header: bytes,
        track_no: int,
        vint_len: int,
        duration_ms: int | None = None,
        references: tuple[int, ...] = (),
        discard_padding_ns: int = 0,
        codec_state: bytes = b"",
        block_additions: bytes = b"",
        duration_ns: int | None = None,
        references_ns: tuple[int, ...] = (),
        is_keyframe: bool | None = None,
    ) -> tuple["MatroskaBlock", ...]:
        if len(header) < vint_len + 3:
            raise ValueError("Block Matroska tronqué")
        relative = int.from_bytes(header[vint_len:vint_len + 2], "big", signed=True)
        flags = header[vint_len + 2]
        mode = (flags >> 1) & 0x03
        total_payload_len = elem_size - (vint_len + 3)
        probe_size = len(header)
        while True:
            try:
                sizes = _split_lace_sizes(header[vint_len + 3:], flags, total_payload_len)
                break
            except ValueError:
                if probe_size >= elem_size:
                    raise
                probe_size = min(elem_size, probe_size * 4)
                fh.seek(payload_offset)
                header = _read_exact(fh, probe_size)

        return tuple(MatroskaBlock(
            track_number=track_no,
            timestamp_ms=cluster_timestamp + relative,
            flags=flags,
            payload=b"",
            lace_index=index,
            lace_count=len(sizes),
            duration_ms=duration_ms,
            references=references,
            discard_padding_ns=discard_padding_ns,
            codec_state=codec_state,
            block_additions=block_additions,
            duration_ns=duration_ns,
            references_ns=references_ns,
            lacing_mode=mode,
            encoded_frames_payload=b"",
            is_keyframe=bool(flags & 0x80) if is_keyframe is None else is_keyframe,
            payload_bytes=frame_size,
        ) for index, frame_size in enumerate(sizes))

    def blocks(
        self,
        *,
        track_numbers: set[int] | None = None,
        read_payload: bool = True,
    ) -> Iterator["MatroskaBlock"]:
        """Yield SimpleBlock and BlockGroup frames, including all lacing modes.

        Permet de filtrer en amont par numéros de pistes (``track_numbers``) et
        de sauter la lecture du contenu brut des frames (``read_payload=False``)
        en ne décodant que les en-têtes et métadonnées temporelles.
        """
        size = self.path.stat().st_size
        scale_ns = self.timestamp_scale_ns()
        with self.path.open("rb") as fh:
            for cluster in self.top_level():
                if cluster.element_id != self.CLUSTER_ID:
                    continue
                timestamp = 0
                fh.seek(cluster.payload_offset)
                for child in iter_children(fh, cluster, file_size=size):
                    if child.size is None:
                        continue
                    if child.element_id == self.TIMESTAMP_ID:
                        fh.seek(child.payload_offset)
                        timestamp = int.from_bytes(_read_exact(fh, child.size), "big")
                    elif child.element_id == self.SIMPLE_BLOCK_ID:
                        probe_len = min(child.size, 256 if not read_payload else 16)
                        fh.seek(child.payload_offset)
                        header = _read_exact(fh, probe_len)
                        track_no, vint_len = _read_vint_value(header)
                        if track_numbers is not None and track_no not in track_numbers:
                            continue
                        if read_payload:
                            fh.seek(child.payload_offset)
                            raw = _read_exact(fh, child.size)
                            decoded = self._decode_block(raw, timestamp)
                        else:
                            decoded = self._decode_block_header_only_stream(
                                fh, child.payload_offset, child.size, timestamp,
                                header=header, track_no=track_no, vint_len=vint_len,
                            )
                        for block in decoded:
                            timestamp_ns = block.timestamp_ms * scale_ns if block.lace_index == 0 else None
                            yield block.__class__(**{
                                **block.__dict__,
                                "timestamp_ms": round(block.timestamp_ms * scale_ns / 1_000_000),
                                "timestamp_ns": timestamp_ns,
                            })
                    elif child.element_id == self.BLOCK_GROUP_ID and child.size is not None:
                        group_children: list[EbmlElement] = []
                        fh.seek(child.payload_offset)
                        for part in iter_children(fh, child, file_size=size):
                            if part.size is not None:
                                group_children.append(part)
                        block_elem = next((p for p in group_children if p.element_id == self.BLOCK_ID), None)
                        if block_elem is None or block_elem.size is None:
                            raise ValueError("BlockGroup sans Block")
                        probe_len = min(block_elem.size, 256 if not read_payload else 16)
                        fh.seek(block_elem.payload_offset)
                        header = _read_exact(fh, probe_len)
                        track_no, vint_len = _read_vint_value(header)
                        if track_numbers is not None and track_no not in track_numbers:
                            continue
                        values: dict[bytes, list[bytes]] = {}
                        for part in group_children:
                            if part.element_id == self.BLOCK_ID:
                                continue
                            fh.seek(part.payload_offset)
                            values.setdefault(part.element_id, []).append(_read_exact(fh, part.size or 0))

                        def uint(key: bytes) -> int:
                            entries = values.get(key)
                            return int.from_bytes(entries[0], "big") if entries else 0

                        def sint_values(key: bytes) -> tuple[int, ...]:
                            return tuple(
                                int.from_bytes(item, "big", signed=True)
                                for item in values.get(key, [])
                            )

                        references = sint_values(self.REFERENCE_BLOCK_ID)
                        duration_ticks = uint(self.BLOCK_DURATION_ID)
                        duration_ms = (
                            round(duration_ticks * scale_ns / 1_000_000)
                            if duration_ticks else None
                        )
                        duration_ns = (
                            duration_ticks * scale_ns
                            if duration_ticks else None
                        )
                        references_ms = tuple(round(value * scale_ns / 1_000_000) for value in references)
                        references_ns = tuple(value * scale_ns for value in references)
                        is_keyframe = not references
                        discard_padding_ns = (sint_values(self.DISCARD_PADDING_ID) or (0,))[0]
                        codec_state = (values.get(self.CODEC_STATE_ID) or [b""])[0]
                        block_additions = (values.get(self.BLOCK_ADDITIONS_ID) or [b""])[0]

                        if read_payload:
                            fh.seek(block_elem.payload_offset)
                            raw = _read_exact(fh, block_elem.size)
                            decoded = self._decode_block(
                                raw, timestamp,
                                duration_ms=duration_ms,
                                duration_ns=duration_ns,
                                references=references_ms,
                                references_ns=references_ns,
                                is_keyframe=is_keyframe,
                                discard_padding_ns=discard_padding_ns,
                                codec_state=codec_state,
                                block_additions=block_additions,
                            )
                        else:
                            decoded = self._decode_block_header_only_stream(
                                fh, block_elem.payload_offset, block_elem.size, timestamp,
                                header=header, track_no=track_no, vint_len=vint_len,
                                duration_ms=duration_ms,
                                duration_ns=duration_ns,
                                references=references_ms,
                                references_ns=references_ns,
                                is_keyframe=is_keyframe,
                                discard_padding_ns=discard_padding_ns,
                                codec_state=codec_state,
                                block_additions=block_additions,
                            )
                        for block in decoded:
                            timestamp_ns = block.timestamp_ms * scale_ns if block.lace_index == 0 else None
                            yield block.__class__(**{
                                **block.__dict__,
                                "timestamp_ms": round(block.timestamp_ms * scale_ns / 1_000_000),
                                "timestamp_ns": timestamp_ns,
                            })

    def simple_blocks(self) -> Iterator["MatroskaBlock"]:
        """Compatibility alias for callers predating BlockGroup support."""
        yield from self.blocks()

    @staticmethod
    def _lacing_overhead(header: bytes, flags: int) -> tuple[int, int]:
        """``(frame_count, octets d'en-tête de lacing)`` d'un bloc lacé.

        ``header`` commence juste après les flags du bloc. Seules les tailles
        déclarées sont lues : les frames elles-mêmes ne sont jamais touchées.
        """
        mode = (flags >> 1) & 0x03
        if mode == 0:
            return 1, 0
        if not header:
            raise ValueError("En-tête de lacing absent")
        count = header[0] + 1
        cursor = 1
        if mode == 1:  # Xiph
            for _ in range(count - 1):
                while True:
                    if cursor >= len(header):
                        raise ValueError("Lacing Xiph tronqué")
                    byte = header[cursor]
                    cursor += 1
                    if byte != 255:
                        break
        elif mode == 3:  # EBML
            for _ in range(count - 1):
                if cursor >= len(header):
                    raise ValueError("Lacing EBML tronqué")
                _value, length = _read_vint_value(header, cursor)
                cursor += length
        return count, cursor

    def cluster_elements(self) -> list[EbmlElement]:
        """Clusters de niveau 1, dans l'ordre du fichier.

        Énumérer le squelette coûte une entrée/sortie par élément : le
        résultat est mémoïsé, plusieurs sondages d'un même reader ne le
        repayent pas.
        """
        if self._clusters_cache is None:
            self._clusters_cache = [
                item for item in self.top_level() if item.element_id == self.CLUSTER_ID
            ]
        return list(self._clusters_cache)

    def block_summaries(
        self,
        *,
        workers: int = 1,
        clusters: list[EbmlElement] | None = None,
    ) -> Iterator["MatroskaBlockSummary"]:
        """Mesure chaque bloc sans matérialiser ses frames.

        Équivalent de :meth:`blocks` pour les compteurs (piste, horodatage,
        durée, nombre de frames, octets), mais seuls les en-têtes sont lus :
        sur un fichier de plusieurs Go, le parcours ne touche qu'une fraction
        des octets.

        ``workers`` > 1 répartit les Clusters sur plusieurs descripteurs. Le
        parcours reste latence-bound (une entrée/sortie par bloc) : plusieurs
        lectures en vol saturent bien mieux un SSD. Les tranches sont émises
        dans l'ordre du fichier, la séquence produite est donc identique au
        mode séquentiel.

        ``clusters`` restreint le parcours à une plage déjà énumérée (sondage
        de début ou de fin de fichier).
        """
        if clusters is None:
            clusters = self.cluster_elements()
        if workers <= 1 or len(clusters) < 2:
            yield from self._scan_clusters(clusters)
            return

        # Tranches courtes : le pool garde des lectures en vol sans empiler
        # de longues listes de résultats en mémoire.
        chunk_size = max(1, min(512, len(clusters) // (workers * 8) or 1))
        chunks = [clusters[index:index + chunk_size] for index in range(0, len(clusters), chunk_size)]
        def scan(chunk: list[EbmlElement]) -> list[MatroskaBlockSummary]:
            return list(self._scan_clusters(chunk))

        with ThreadPoolExecutor(max_workers=workers) as pool:
            pending: deque[Future[list[MatroskaBlockSummary]]] = deque()
            for chunk in chunks:
                while len(pending) >= workers * 2:
                    yield from pending.popleft().result()
                pending.append(pool.submit(scan, chunk))
            while pending:
                yield from pending.popleft().result()

    def _scan_clusters(
        self,
        clusters: list[EbmlElement],
    ) -> Iterator["MatroskaBlockSummary"]:
        """Parcours des en-têtes de blocs d'une plage de Clusters.

        Tente en priorité un parcours direct via projection mémoire (mmap),
        qui évite les millions d'appels système tell/seek/read et le recyclage
        constant du tampon de lecture. Bascule sur le mode flux si mmap échoue.
        """
        if not clusters:
            return
        try:
            yield from self._scan_clusters_mmap(clusters)
            return
        except (OSError, ValueError):
            pass
        yield from self._scan_clusters_stream(clusters)

    def _scan_clusters_mmap(
        self,
        clusters: list[EbmlElement],
    ) -> Iterator["MatroskaBlockSummary"]:
        size = self.path.stat().st_size
        if size == 0:
            return
        scale_ns = self.timestamp_scale_ns()
        with self.path.open("rb") as fh:
            with mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                if hasattr(mmap, "MADV_SEQUENTIAL"):
                    try:
                        mm.madvise(mmap.MADV_SEQUENTIAL)
                    except (OSError, ValueError):
                        pass

                def block_summary_mem(
                    payload_offset: int,
                    elem_size: int,
                    cluster_timestamp: int,
                    duration_ns: int | None,
                ) -> "MatroskaBlockSummary":
                    probe_size = min(elem_size, 256)
                    header = mm[payload_offset:payload_offset + probe_size]
                    track_number, vint_length = _read_vint_value(header)
                    if len(header) < vint_length + 3:
                        raise ValueError("Block Matroska tronqué")
                    relative = int.from_bytes(header[vint_length:vint_length + 2], "big", signed=True)
                    flags = header[vint_length + 2]
                    while True:
                        try:
                            frame_count, lacing_bytes = self._lacing_overhead(
                                header[vint_length + 3:], flags,
                            )
                            break
                        except ValueError:
                            if probe_size >= elem_size:
                                raise
                            probe_size = min(elem_size, probe_size * 4)
                            header = mm[payload_offset:payload_offset + probe_size]
                    header_bytes = vint_length + 3 + lacing_bytes
                    return MatroskaBlockSummary(
                        track_number=track_number,
                        timestamp_ns=(cluster_timestamp + relative) * scale_ns,
                        duration_ns=duration_ns,
                        frame_count=frame_count,
                        payload_bytes=max(0, elem_size - header_bytes),
                    )

                for cluster in clusters:
                    cur = cluster.payload_offset
                    limit = cluster.end if cluster.end is not None else size
                    cluster_timestamp = 0
                    while cur < limit:
                        first = mm[cur]
                        id_len = _vint_length(first)
                        elem_id = bytes(mm[cur:cur + id_len])
                        cur += id_len
                        if cur >= limit:
                            break
                        first_size = mm[cur]
                        size_len = _vint_length(first_size)
                        raw_size = mm[cur:cur + size_len]
                        size_val = raw_size[0] & (0xFF >> size_len)
                        for b in raw_size[1:]:
                            size_val = (size_val << 8) | b
                        unknown = size_val == (1 << (7 * size_len)) - 1
                        cur += size_len
                        if unknown:
                            break
                        payload_offset = cur
                        child_end = payload_offset + size_val
                        cur = child_end

                        if elem_id == self.TIMESTAMP_ID:
                            cluster_timestamp = int.from_bytes(
                                mm[payload_offset:child_end], "big",
                            )
                        elif elem_id == self.SIMPLE_BLOCK_ID:
                            yield block_summary_mem(payload_offset, size_val, cluster_timestamp, None)
                        elif elem_id == self.BLOCK_GROUP_ID:
                            bg_cur = payload_offset
                            block_payload_offset = None
                            block_size_val = None
                            duration_ticks = 0
                            while bg_cur < child_end:
                                p_first = mm[bg_cur]
                                p_id_len = _vint_length(p_first)
                                p_id = bytes(mm[bg_cur:bg_cur + p_id_len])
                                bg_cur += p_id_len
                                if bg_cur >= child_end:
                                    break
                                p_size_first = mm[bg_cur]
                                p_size_len = _vint_length(p_size_first)
                                p_raw_size = mm[bg_cur:bg_cur + p_size_len]
                                p_val = p_raw_size[0] & (0xFF >> p_size_len)
                                for b in p_raw_size[1:]:
                                    p_val = (p_val << 8) | b
                                bg_cur += p_size_len
                                p_payload = bg_cur
                                bg_cur += p_val
                                if p_id == self.BLOCK_ID:
                                    block_payload_offset = p_payload
                                    block_size_val = p_val
                                elif p_id == self.BLOCK_DURATION_ID:
                                    duration_ticks = int.from_bytes(
                                        mm[p_payload:p_payload + p_val], "big",
                                    )
                            if block_payload_offset is None or block_size_val is None:
                                raise ValueError("BlockGroup sans Block")
                            yield block_summary_mem(
                                block_payload_offset,
                                block_size_val,
                                cluster_timestamp,
                                duration_ticks * scale_ns if duration_ticks else None,
                            )

    def _scan_clusters_stream(
        self,
        clusters: list[EbmlElement],
    ) -> Iterator["MatroskaBlockSummary"]:
        """Parcours de secours via descripteur de fichier tamponné."""
        size = self.path.stat().st_size
        scale_ns = self.timestamp_scale_ns()
        # Chaque payload de bloc est sauté : un tampon de lecture large serait
        # rempli puis jeté à chaque saut. Un petit tampon garde des lectures
        # complètes (contrairement au mode non tamponné) sans lire les frames.
        with self.path.open("rb", buffering=self._SKIPPING_READ_BUFFER) as fh:

            def read_at(offset: int, length: int) -> bytes:
                fh.seek(offset)
                return fh.read(length)

            def block_summary(
                element: EbmlElement,
                cluster_timestamp: int,
                duration_ns: int | None,
            ) -> "MatroskaBlockSummary":
                if element.size is None:
                    raise ValueError("Block Matroska de taille inconnue")
                # 8 octets de numéro de piste + 2 d'horodatage + 1 de flags,
                # puis la table de lacing : 256 octets couvrent les blocs
                # usuels, la boucle élargit pour les laçages très fragmentés.
                probe_size = min(element.size, 256)
                header = read_at(element.payload_offset, probe_size)
                track_number, vint_length = _read_vint_value(header)
                if len(header) < vint_length + 3:
                    raise ValueError("Block Matroska tronqué")
                relative = int.from_bytes(header[vint_length:vint_length + 2], "big", signed=True)
                flags = header[vint_length + 2]
                while True:
                    try:
                        frame_count, lacing_bytes = self._lacing_overhead(
                            header[vint_length + 3:], flags,
                        )
                        break
                    except ValueError:
                        if probe_size >= element.size:
                            raise
                        probe_size = min(element.size, probe_size * 4)
                        header = read_at(element.payload_offset, probe_size)
                header_bytes = vint_length + 3 + lacing_bytes
                return MatroskaBlockSummary(
                    track_number=track_number,
                    timestamp_ns=(cluster_timestamp + relative) * scale_ns,
                    duration_ns=duration_ns,
                    frame_count=frame_count,
                    payload_bytes=max(0, element.size - header_bytes),
                )

            for cluster in clusters:
                timestamp = 0
                fh.seek(cluster.payload_offset)
                for child in iter_children(fh, cluster, file_size=size):
                    if child.size is None:
                        continue
                    if child.element_id == self.TIMESTAMP_ID:
                        timestamp = int.from_bytes(
                            read_at(child.payload_offset, child.size), "big",
                        )
                    elif child.element_id == self.SIMPLE_BLOCK_ID:
                        yield block_summary(child, timestamp, None)
                    elif child.element_id == self.BLOCK_GROUP_ID:
                        block: EbmlElement | None = None
                        duration_ticks = 0
                        fh.seek(child.payload_offset)
                        for part in iter_children(fh, child, file_size=size):
                            if part.size is None:
                                continue
                            if part.element_id == self.BLOCK_ID:
                                block = part
                            elif part.element_id == self.BLOCK_DURATION_ID:
                                duration_ticks = int.from_bytes(
                                    read_at(part.payload_offset, part.size), "big",
                                )
                        if block is None:
                            raise ValueError("BlockGroup sans Block")
                        yield block_summary(
                            block,
                            timestamp,
                            duration_ticks * scale_ns if duration_ticks else None,
                        )


@dataclass(frozen=True)
class MatroskaTrack:
    number: int
    uid: int
    track_type: int
    codec_id: str
    codec_private: bytes
    language_bcp47: str
    language: str
    name: str
    raw_entry: bytes
    #: DefaultDuration du TrackEntry (ns par frame), 0 si absent.
    default_duration_ns: int = 0
    video: dict[str, int | float] = field(default_factory=dict)
    audio: dict[str, int | float] = field(default_factory=dict)
    block_addition_mappings: tuple[dict[str, int | str | bytes], ...] = ()
    flag_enabled: bool = True
    flag_default: bool = True
    flag_forced: bool = False
    flag_hearing_impaired: bool = False
    flag_visual_impaired: bool = False
    flag_original: bool = False
    flag_commentary: bool = False

@dataclass(frozen=True)
class MatroskaBlockSummary:
    """Compteurs d'un bloc lus sans matérialiser ses frames."""

    track_number: int
    timestamp_ns: int
    #: BlockDuration explicite converti en ns, ``None`` quand absent.
    duration_ns: int | None
    frame_count: int
    payload_bytes: int


@dataclass(frozen=True)
class MatroskaBlock:
    track_number: int
    timestamp_ms: int
    flags: int
    payload: bytes
    lace_index: int = 0
    lace_count: int = 1
    duration_ms: int | None = None
    references: tuple[int, ...] = ()
    discard_padding_ns: int = 0
    codec_state: bytes = b""
    block_additions: bytes = b""
    timestamp_ns: int | None = None
    duration_ns: int | None = None
    references_ns: tuple[int, ...] = ()
    lacing_mode: int = 0
    encoded_frames_payload: bytes = b""
    # SimpleBlock: bit keyframe. BlockGroup: absence de ReferenceBlock.
    is_keyframe: bool | None = None
    payload_bytes: int = 0


@dataclass(frozen=True)
class MatroskaAttachment:
    uid: int
    name: str
    media_type: str
    description: str
    data: bytes


@dataclass(frozen=True)
class MatroskaAttachmentHeader:
    uid: int
    name: str
    media_type: str
    description: str
    size: int
    data_offset: int


@dataclass(frozen=True)
class MatroskaChapter:
    uid: int
    start_ns: int
    end_ns: int | None
    displays: tuple[tuple[str, str], ...]
    children: tuple["MatroskaChapter", ...] = ()


@dataclass(frozen=True)
class MatroskaEdition:
    uid: int
    chapters: tuple[MatroskaChapter, ...]


@dataclass(frozen=True)
class MatroskaTag:
    targets: dict[str, int]
    values: tuple[tuple[str, str], ...]


def strict_demuxer_reads_tracks(path: Path) -> bool:
    """MKV lisible tel quel par dovi_tool / hdr10plus_tool ; vrai si l'analyse échoue (outil seul juge)."""
    try:
        return MatroskaReader(Path(path)).tracks_indexed_for_strict_readers()
    except (OSError, ValueError):
        return True


__all__ = [
    "EbmlElement", "MatroskaAttachment", "MatroskaAttachmentHeader", "MatroskaBlock",
    "MatroskaBlockSummary", "MatroskaChapter",
    "MatroskaEdition", "MatroskaReader", "MatroskaTag", "MatroskaTrack",
    "iter_children", "payload_children", "read_element", "strict_demuxer_reads_tracks",
]
