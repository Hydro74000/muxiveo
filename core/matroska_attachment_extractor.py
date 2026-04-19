"""Extraction built-in des pièces jointes Matroska."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator


_ID_SEGMENT = b"\x18\x53\x80\x67"
_ID_ATTACHMENTS = b"\x19\x41\xa4\x69"
_ID_ATTACHED_FILE = b"\x61\xa7"
_ID_FILE_NAME = b"\x46\x6e"
_ID_FILE_DATA = b"\x46\x5c"


@dataclass(frozen=True)
class _EbmlElement:
    element_id: bytes
    offset: int
    id_len: int
    size: int
    size_len: int
    payload_offset: int
    unknown_size: bool

    @property
    def end(self) -> int:
        return self.payload_offset + self.size


def _read_exact(fh: BinaryIO, offset: int, size: int) -> bytes:
    fh.seek(offset)
    data = fh.read(size)
    if len(data) != size:
        raise ValueError("Données insuffisantes pour lire l'élément EBML.")
    return data


def _ebml_vint_length(first_byte: int) -> int:
    mask = 0x80
    for length in range(1, 9):
        if first_byte & mask:
            return length
        mask >>= 1
    return 0


def _read_ebml_id_from_file(fh: BinaryIO, offset: int, file_size: int) -> tuple[bytes, int]:
    if offset < 0 or offset >= file_size:
        raise ValueError("Offset EBML ID invalide.")
    first = _read_exact(fh, offset, 1)[0]
    length = _ebml_vint_length(first)
    if length == 0 or length > 4:
        raise ValueError("Longueur EBML ID invalide.")
    end = offset + length
    if end > file_size:
        raise ValueError("Données insuffisantes pour EBML ID.")
    return _read_exact(fh, offset, length), length


def _read_ebml_size_from_file(fh: BinaryIO, offset: int, file_size: int) -> tuple[int, int, bool]:
    if offset < 0 or offset >= file_size:
        raise ValueError("Offset EBML size invalide.")
    first = _read_exact(fh, offset, 1)[0]
    length = _ebml_vint_length(first)
    if length == 0 or length > 8:
        raise ValueError("Longueur EBML size invalide.")
    end = offset + length
    if end > file_size:
        raise ValueError("Données insuffisantes pour EBML size.")

    raw = _read_exact(fh, offset, length)
    value = raw[0] & (0xFF >> length)
    for b in raw[1:]:
        value = (value << 8) | b

    max_value = (1 << (7 * length)) - 1
    return value, length, value == max_value


def _read_ebml_element_from_file(fh: BinaryIO, offset: int, file_size: int) -> _EbmlElement:
    element_id, id_len = _read_ebml_id_from_file(fh, offset, file_size)
    size_offset = offset + id_len
    size, size_len, unknown = _read_ebml_size_from_file(fh, size_offset, file_size)
    payload_offset = size_offset + size_len

    if unknown:
        return _EbmlElement(
            element_id=element_id,
            offset=offset,
            id_len=id_len,
            size=0,
            size_len=size_len,
            payload_offset=payload_offset,
            unknown_size=True,
        )

    payload_end = payload_offset + size
    if payload_end > file_size:
        raise ValueError("Payload EBML hors plage.")

    return _EbmlElement(
        element_id=element_id,
        offset=offset,
        id_len=id_len,
        size=size,
        size_len=size_len,
        payload_offset=payload_offset,
        unknown_size=False,
    )


def _iter_children(
    fh: BinaryIO,
    start_offset: int,
    end_offset: int,
    file_size: int,
) -> Iterator[_EbmlElement]:
    cursor = start_offset
    while cursor < end_offset:
        element = _read_ebml_element_from_file(fh, cursor, file_size)
        yield element
        cursor = end_offset if element.unknown_size else element.end


def _decode_text(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace").rstrip(" \x00")


def extract_matroska_attachment_bytes(path: Path, attachment_index: int) -> bytes:
    """
    Extrait la pièce jointe Matroska d'index local donné (0-based).

    L'index suit l'ordre des éléments ``AttachedFile`` dans ``Attachments``.
    """
    if attachment_index < 0:
        raise ValueError("Index d'attachement négatif.")
    if not path.is_file():
        raise FileNotFoundError(path)

    file_size = path.stat().st_size
    with path.open("rb") as fh:
        cursor = 0
        while cursor < file_size:
            root = _read_ebml_element_from_file(fh, cursor, file_size)
            root_end = file_size if root.unknown_size else root.end
            if root.element_id == _ID_SEGMENT:
                current_index = 0
                for child in _iter_children(fh, root.payload_offset, root_end, file_size):
                    if child.element_id != _ID_ATTACHMENTS:
                        continue
                    attachments_end = file_size if child.unknown_size else child.end
                    for attached in _iter_children(
                        fh,
                        child.payload_offset,
                        attachments_end,
                        file_size,
                    ):
                        if attached.element_id != _ID_ATTACHED_FILE:
                            continue
                        file_name = ""
                        file_data: bytes | None = None
                        attached_end = file_size if attached.unknown_size else attached.end
                        for entry in _iter_children(
                            fh,
                            attached.payload_offset,
                            attached_end,
                            file_size,
                        ):
                            if entry.element_id == _ID_FILE_NAME:
                                file_name = _decode_text(
                                    _read_exact(fh, entry.payload_offset, entry.size)
                                )
                            elif entry.element_id == _ID_FILE_DATA:
                                file_data = _read_exact(fh, entry.payload_offset, entry.size)
                        if current_index == attachment_index:
                            if file_data is None:
                                raise ValueError(
                                    f"Attachement Matroska incomplet: FileData manquant pour {file_name or attachment_index}."
                                )
                            return file_data
                        current_index += 1
            cursor = root_end

    raise ValueError(f"Attachement Matroska introuvable à l'index {attachment_index}.")


__all__ = ["extract_matroska_attachment_bytes"]
