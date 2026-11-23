"""
core/matroska/stream_source.py — Lecture séquentielle d'un flux Matroska non positionnable (pipe).

Un producteur FFmpeg écrit une piste en Matroska sur sa sortie standard. Le
flux est lu une seule fois, dans l'ordre :

- l'en-tête (tout ce qui précède le premier Cluster : Info, Tracks, Chapters,
  Tags…) est recopié dans un petit fichier, exploité par :class:`MatroskaReader`
  sans nouveau code d'analyse (lecture linéaire, Segment de taille inconnue) ;
- les Clusters sont ensuite lus élément par élément, au moment où l'assembleur
  demande le bloc suivant : jamais un Cluster entier en mémoire.

Aucune position de fichier n'est requise (pas de SeekHead, ni de Cues, ni de
Duration). Tout écart au format attendu lève ``ValueError`` explicitement.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

from .reader import MatroskaBlock, MatroskaReader, MatroskaTrack

_EBML_HEADER_ID = bytes.fromhex("1a45dfa3")
_SEGMENT_ID = bytes.fromhex("18538067")
_CLUSTER_ID = bytes.fromhex("1f43b675")
_TIMESTAMP_ID = bytes.fromhex("e7")
_SIMPLE_BLOCK_ID = bytes.fromhex("a3")
_BLOCK_GROUP_ID = bytes.fromhex("a0")
#: Segment de taille inconnue (VINT 8 octets) : le fichier d'en-tête s'arrête avant les Clusters.
_UNKNOWN_SIZE_8 = bytes.fromhex("01ffffffffffffff")

#: Plus grand élément accepté (bloc, BlockGroup, élément d'en-tête ou de fin ignoré).
MAX_STREAM_ELEMENT_BYTES = 64 * 1024 * 1024

#: Codecs dont le CodecPrivate est indispensable au décodage : absent de l'en-tête,
#: la piste serait inutilisable (configuration connue seulement en fin de flux).
CODEC_PRIVATE_REQUIRED = frozenset({
    "V_MPEG4/ISO/AVC", "V_MPEGH/ISO/HEVC", "V_AV1",
    "A_AAC", "A_VORBIS", "A_OPUS", "A_FLAC",
    "S_TEXT/ASS", "S_TEXT/SSA", "S_ASS", "S_SSA",
})


class _Stream:
    """Lecture exacte sur un flux binaire, avec position consommée (diagnostic)."""

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream
        self.position = 0

    def read(self, size: int) -> bytes:
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            chunk = self._stream.read(remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        self.position += len(data)
        return data

    def exact(self, size: int, what: str) -> bytes:
        data = self.read(size)
        if len(data) != size:
            raise ValueError(f"Flux Matroska tronqué ({what}, octet {self.position}).")
        return data

    def skip(self, size: int, what: str) -> None:
        remaining = size
        while remaining:
            step = min(remaining, 1024 * 1024)
            self.exact(step, what)
            remaining -= step


def _vint_length(first: int) -> int:
    if not first:
        raise ValueError("VINT Matroska invalide dans le flux.")
    return 9 - first.bit_length()


def _read_element_header(stream: _Stream) -> tuple[bytes, int | None, bytes] | None:
    """``(ID, taille ou None si inconnue, octets de l'en-tête)`` ; ``None`` en fin de flux propre."""
    first = stream.read(1)
    if not first:
        return None
    id_length = _vint_length(first[0])
    if id_length > 4:
        raise ValueError("Identifiant EBML invalide dans le flux.")
    element_id = first + (stream.exact(id_length - 1, "identifiant") if id_length > 1 else b"")
    size_first = stream.exact(1, "taille")
    size_length = _vint_length(size_first[0])
    size_bytes = size_first + (stream.exact(size_length - 1, "taille") if size_length > 1 else b"")
    value = int.from_bytes(size_bytes, "big") & ((1 << (7 * size_length)) - 1)
    size = None if value == (1 << (7 * size_length)) - 1 else value
    return element_id, size, element_id + size_bytes


class MatroskaStreamSource:
    """Flux Matroska d'une piste lu séquentiellement : en-tête recopié, puis blocs à la demande."""

    def __init__(self, stream: BinaryIO, header_path: Path, *, max_element_bytes: int | None = None) -> None:
        self._stream = _Stream(stream)
        self._header_path = Path(header_path)
        self._max = MAX_STREAM_ELEMENT_BYTES if max_element_bytes is None else max_element_bytes
        self._pending_cluster: int | None = None
        self._header_read = False
        self._blocks_started = False
        self._scale_ns = 1_000_000

    def _bounded(self, size: int | None, what: str) -> int:
        if size is None:
            raise ValueError(f"Flux Matroska : {what} de taille inconnue, non pris en charge.")
        if size > self._max:
            raise ValueError(f"Flux Matroska : {what} de {size} octets au-delà de la borne ({self._max}).")
        return size

    def read_header(self) -> MatroskaReader:
        """Lit l'en-tête jusqu'au premier Cluster, le recopie dans ``header_path`` et renvoie son lecteur."""
        if self._header_read:
            raise ValueError("En-tête du flux Matroska déjà lu.")
        self._header_read = True
        parts: list[bytes] = []
        header = _read_element_header(self._stream)
        if header is None or header[0] != _EBML_HEADER_ID:
            raise ValueError("Flux Matroska sans en-tête EBML.")
        size = self._bounded(header[1], "en-tête EBML")
        parts.append(header[2] + self._stream.exact(size, "en-tête EBML"))
        segment = _read_element_header(self._stream)
        if segment is None or segment[0] != _SEGMENT_ID:
            raise ValueError("Flux Matroska sans Segment.")
        # Fichier d'en-tête : Segment de taille inconnue (il s'arrête avant les Clusters).
        parts.append(_SEGMENT_ID + _UNKNOWN_SIZE_8)
        while True:
            element = _read_element_header(self._stream)
            if element is None:
                raise ValueError("Flux Matroska sans Cluster.")
            element_id, element_size, raw_header = element
            if element_id == _CLUSTER_ID:
                self._pending_cluster = self._bounded_cluster(element_size)
                break
            size = self._bounded(element_size, "élément d'en-tête")
            parts.append(raw_header + self._stream.exact(size, "élément d'en-tête"))
        self._header_path.parent.mkdir(parents=True, exist_ok=True)
        self._header_path.write_bytes(b"".join(parts))
        reader = MatroskaReader(self._header_path)
        self._scale_ns = reader.timestamp_scale_ns()
        check_codec_private(reader.tracks())
        return reader

    @staticmethod
    def _bounded_cluster(size: int | None) -> int:
        if size is None:
            raise ValueError("Flux Matroska : Cluster de taille inconnue, non pris en charge.")
        return size

    def blocks(self, *, first_lace_only: bool = False) -> Iterator[MatroskaBlock]:
        """Blocs du flux, dans l'ordre ; à usage unique, après :meth:`read_header`."""
        if not self._header_read:
            raise ValueError("En-tête du flux Matroska non lu.")
        if self._blocks_started:
            raise ValueError("Blocs du flux Matroska déjà lus (lecture unique).")
        self._blocks_started = True
        return self._iter_blocks(first_lace_only)

    def _iter_blocks(self, first_lace_only: bool) -> Iterator[MatroskaBlock]:
        cluster_size = self._pending_cluster
        while cluster_size is not None:
            yield from self._cluster_blocks(cluster_size, first_lace_only)
            cluster_size = None
            # Éléments suivants : autre Cluster, ou éléments de fin (Cues, Tags) ignorés.
            while True:
                element = _read_element_header(self._stream)
                if element is None:
                    return
                element_id, element_size, _raw = element
                if element_id == _CLUSTER_ID:
                    cluster_size = self._bounded_cluster(element_size)
                    break
                self._stream.skip(self._bounded(element_size, "élément de fin"), "élément de fin")

    def _cluster_blocks(self, cluster_size: int, first_lace_only: bool) -> Iterator[MatroskaBlock]:
        remaining = cluster_size
        timestamp = 0
        while remaining:
            start = self._stream.position
            element = _read_element_header(self._stream)
            if element is None:
                raise ValueError("Flux Matroska tronqué (Cluster incomplet).")
            element_id, element_size, _raw = element
            size = self._bounded(element_size, "élément de Cluster")
            if element_id == _TIMESTAMP_ID:
                timestamp = int.from_bytes(self._stream.exact(size, "Timestamp"), "big")
            elif element_id == _SIMPLE_BLOCK_ID:
                data = self._stream.exact(size, "SimpleBlock")
                yield from MatroskaReader._decode_block(
                    data, timestamp, timestamp_scale_ns=self._scale_ns, first_lace_only=first_lace_only,
                )
            elif element_id == _BLOCK_GROUP_ID:
                data = self._stream.exact(size, "BlockGroup")
                yield from MatroskaReader.decode_block_group(
                    data, timestamp, timestamp_scale_ns=self._scale_ns, first_lace_only=first_lace_only,
                )
            else:
                self._stream.skip(size, "élément de Cluster")
            remaining -= self._stream.position - start
            if remaining < 0:
                raise ValueError("Flux Matroska : élément débordant de son Cluster.")


def check_codec_private(tracks: list[MatroskaTrack]) -> None:
    """Refuse une piste dont le codec exige un CodecPrivate absent de l'en-tête."""
    for track in tracks:
        if track.codec_id in CODEC_PRIVATE_REQUIRED and not track.codec_private:
            raise ValueError(
                f"Flux Matroska : CodecPrivate absent pour la piste #{track.number} ({track.codec_id}) ; "
                "la configuration du codec n'est pas connue au début du flux."
            )


class StreamedMatroskaReader(MatroskaReader):
    """Lecteur d'un flux : métadonnées du fichier d'en-tête, blocs lus une seule fois depuis le flux."""

    def __init__(self, source: MatroskaStreamSource, header_path: Path, *, duration_ns: int | None = None) -> None:
        super().__init__(header_path)
        self._source = source
        self._duration_hint_ns = duration_ns

    def blocks(
        self,
        *,
        track_numbers: set[int] | None = None,
        read_payload: bool = True,
        first_lace_only: bool = False,
    ) -> Iterator[MatroskaBlock]:
        del read_payload  # le flux est lu tel quel, payload compris
        for block in self._source.blocks(first_lace_only=first_lace_only):
            if track_numbers is None or block.track_number in track_numbers:
                yield block

    def segment_duration_ns(self) -> int | None:
        """Durée de la source (inspection) pour la progression : le flux n'en porte pas."""
        return self._duration_hint_ns


def open_stream_reader(
    stream: BinaryIO, header_path: Path, *, duration_ns: int | None = None,
    max_element_bytes: int | None = None,
) -> StreamedMatroskaReader:
    """Lit l'en-tête du flux et renvoie son lecteur (blocs à usage unique)."""
    source = MatroskaStreamSource(stream, header_path, max_element_bytes=max_element_bytes)
    source.read_header()
    return StreamedMatroskaReader(source, header_path, duration_ns=duration_ns)


__all__ = [
    "CODEC_PRIVATE_REQUIRED", "MAX_STREAM_ELEMENT_BYTES", "MatroskaStreamSource",
    "StreamedMatroskaReader", "check_codec_private", "open_stream_reader",
]
