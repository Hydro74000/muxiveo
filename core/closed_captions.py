"""Détection et retrait ciblé des CC ATSC, sans décoder les images H.264/HEVC."""
from __future__ import annotations

import re
from collections.abc import Callable

from core.h26x_sei import build_sei_rbsp, iter_sei_messages, remove_emulation_prevention_bytes


def caption_codec(codec: str) -> str:
    value = codec.upper().replace("-", "")
    if value in {"HEVC", "H265", "V_MPEGH/ISO/HEVC"}:
        return "hevc"
    if value in {"AVC", "H264", "V_MPEG4/ISO/AVC"}:
        return "h264"
    return ""


def _is_caption(kind: int, payload: bytes) -> bool:
    # Identifiant ATSC GA94, user_data_type_code=3. Les autres T.35
    # (HDR10+, Dolby, AFD...) sont toujours conservés, même dans le même SEI.
    return kind == 4 and payload.startswith(b"\xb5\x00\x31GA94\x03")


class CaptionFilter:
    def __init__(self, codec: str, codec_private: bytes = b"") -> None:
        self.codec = caption_codec(codec)
        self.length_size: int | None = None
        if codec_private[:1] == b"\x01":
            index = 21 if self.codec == "hevc" else 4
            if len(codec_private) > index:
                self.length_size = (codec_private[index] & 3) + 1

    def nal(self, data: bytes) -> bytes:
        if not data or not self.codec:
            return data
        header_size = 2 if self.codec == "hevc" else 1
        kind = (data[0] >> 1) & 63 if self.codec == "hevc" else data[0] & 31
        if kind not in ({39, 40} if self.codec == "hevc" else {6}):
            return data
        if b"\xb5\x00\x31GA94\x03" not in data:
            return data
        rbsp = remove_emulation_prevention_bytes(data[header_size:]).rstrip(b"\x00")
        messages = iter_sei_messages(rbsp, strict=True)
        kept = [(kind, value) for kind, value in messages if not _is_caption(kind, value)]
        if len(kept) == len(messages):
            return data
        return data[:header_size] + build_sei_rbsp(kept) if kept else b""

    def packet(self, data: bytes) -> bytes:
        if not self.codec or not data:
            return data
        parts: list[bytes] = []
        changed = False
        if self.length_size is not None:
            pos = 0
            while pos < len(data):
                start = pos + self.length_size
                size = int.from_bytes(data[pos:start], "big")
                if start > len(data) or not size or start + size > len(data):
                    raise ValueError("Longueur NAL invalide lors du filtrage des sous-titres embarqués")
                original = data[start:start + size]
                filtered = self.nal(original)
                changed |= filtered != original
                if filtered:
                    parts.extend((len(filtered).to_bytes(self.length_size, "big"), filtered))
                pos = start + size
        else:
            starts = list(re.finditer(b"\x00\x00(?:\x00)?\x01", data))
            if not starts:
                return data
            parts.append(data[:starts[0].start()])
            for index, match in enumerate(starts):
                end = starts[index + 1].start() if index + 1 < len(starts) else len(data)
                original = data[match.end():end]
                filtered = self.nal(original)
                changed |= filtered != original
                if filtered:
                    parts.extend((match.group(), filtered))
        return b"".join(parts) if changed else data

    def private(self, data: bytes) -> bytes:
        """Filtre aussi les NAL SEI éventuellement présents dans le hvcC/avcC."""
        if not self.codec or not data:
            return data
        if data[0] != 1:
            return self.packet(data)
        if self.codec == "h264":
            # avcC : SPS/PPS, puis éventuelle extension (conservée intégralement).
            # Ces arrays ne contiennent pas de SEI.
            return data
        if len(data) < 23:
            raise ValueError("hvcC tronqué")
        pos = 23
        arrays = []
        changed = False
        for _ in range(data[22]):
            if pos + 3 > len(data):
                raise ValueError("Array hvcC tronqué")
            header = data[pos:pos + 1]
            count = int.from_bytes(data[pos + 1:pos + 3], "big")
            pos += 3
            nals = []
            for _ in range(count):
                size = int.from_bytes(data[pos:pos + 2], "big")
                pos += 2
                if not size or pos + size > len(data):
                    raise ValueError("NAL hvcC tronqué")
                original = data[pos:pos + size]
                filtered = self.nal(original)
                changed |= filtered != original
                pos += size
                if filtered:
                    nals.append(len(filtered).to_bytes(2, "big") + filtered)
            if nals:
                arrays.append(header + len(nals).to_bytes(2, "big") + b"".join(nals))
        return data[:22] + bytes([len(arrays)]) + b"".join(arrays) + data[pos:] if changed else data


def detect_matroska_captions(path, *, cancel_cb: Callable[[], bool] | None = None) -> set[int]:
    """Sonde bornée des premières secondes compressées, sans réencodage/décodage."""
    from core.matroska.reader import MatroskaReader
    from core.subprocess_utils import ProbeCancelledError

    reader = MatroskaReader(path)
    tracks = reader.tracks()
    filters = {track.number: CaptionFilter(track.codec_id, track.codec_private)
               for track in tracks if track.track_type == 1 and caption_codec(track.codec_id)}
    if not filters:
        return set()
    found = set()
    counts = dict.fromkeys(filters, 0)
    starts: dict[int, int] = {}
    for number, filter_ in filters.items():
        original = tracks[next(i for i, track in enumerate(tracks) if track.number == number)].codec_private
        if filter_.private(original) != original:
            found.add(number)
    blocks = reader.blocks(track_numbers=set(filters))
    try:
        for block in blocks:
            if cancel_cb and cancel_cb():
                raise ProbeCancelledError("Détection des CC annulée")
            start = starts.setdefault(block.track_number, block.timestamp_ms)
            if counts[block.track_number] >= 120 or block.timestamp_ms - start > 10000:
                break
            counts[block.track_number] += 1
            if filters[block.track_number].packet(block.payload) != block.payload:
                found.add(block.track_number)
            if found == set(filters):
                break
    finally:
        close = getattr(blocks, "close", None)
        if close is not None:
            close()
    return {index for index, track in enumerate(tracks) if track.number in found}


def ffprobe_hex_bytes(value: str) -> bytes:
    """Décode un champ -show_data (colonne hexadécimale, sans la colonne ASCII)."""
    return bytes.fromhex("".join(
        line.split(":", 1)[1].strip().split("  ", 1)[0].replace(" ", "")
        for line in value.splitlines() if ":" in line
    ))


def detect_probed_captions(payload: dict) -> set[int]:
    filters = {stream["index"]: CaptionFilter(stream.get("codec_name", ""),
               ffprobe_hex_bytes(stream.get("extradata", "")))
               for stream in payload.get("streams", [])}
    found = set()
    for packet in payload.get("packets", []):
        index = packet.get("stream_index")
        filter_ = filters.get(index)
        data = ffprobe_hex_bytes(packet.get("data", ""))
        if filter_ and filter_.packet(data) != data:
            found.add(index)
    return found
