"""Couches d'un flux Dolby Vision P7 sans décodage : débits BL / EL / RPU et résolution de l'EL.

Lecture des blocs Matroska de la piste (NAL préfixés par leur longueur, hvcC) : NAL 63 = EL,
NAL 62 = RPU, autres = BL. La résolution de l'EL vient de son SPS (NAL HEVC imbriqué dans un NAL 63).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

_EL_NAL, _RPU_NAL, _SPS_NAL = 63, 62, 33


@dataclass(frozen=True)
class LayerFacts:
    frames: int
    duration_s: float
    bl_bytes: int
    el_bytes: int
    rpu_bytes: int
    el_size: tuple[int, int] | None

    @property
    def total_bytes(self) -> int:
        return self.bl_bytes + self.el_bytes + self.rpu_bytes

    def bitrate(self, size: int) -> float:
        """Débit (bit/s) d'une part du flux."""
        return size * 8 / self.duration_s if self.duration_s > 0 else 0.0

    @property
    def el_share(self) -> float:
        return self.el_bytes / self.total_bytes if self.total_bytes else 0.0


class _Bits:
    """Lecteur de bits RBSP (octets d'émulation retirés)."""

    def __init__(self, data: bytes) -> None:
        self.data = data.replace(b"\x00\x00\x03", b"\x00\x00")
        self.pos = 0

    def bit(self) -> int:
        byte = self.data[self.pos >> 3]
        value = (byte >> (7 - (self.pos & 7))) & 1
        self.pos += 1
        return value

    def bits(self, count: int) -> int:
        value = 0
        for _ in range(count):
            value = (value << 1) | self.bit()
        return value

    def ue(self) -> int:
        zeros = 0
        while self.bit() == 0:
            zeros += 1
            if zeros > 31:
                raise ValueError("Exp-Golomb invalide")
        return (1 << zeros) - 1 + self.bits(zeros)


def sps_size(nal: bytes) -> tuple[int, int] | None:
    """Largeur et hauteur codées (fenêtre de conformité appliquée) d'un NAL SPS HEVC (en-tête compris)."""
    try:
        reader = _Bits(nal[2:])
        reader.bits(4)  # sps_video_parameter_set_id
        max_sub_layers = reader.bits(3)
        reader.bits(1)
        reader.bits(96)  # profile_tier_level : partie générale + general_level_idc
        present = [(reader.bit(), reader.bit()) for _ in range(max_sub_layers)]
        if max_sub_layers:
            reader.bits(2 * (8 - max_sub_layers))
        for profile, level in present:
            reader.bits(88 if profile else 0)
            reader.bits(8 if level else 0)
        reader.ue()  # sps_seq_parameter_set_id
        chroma = reader.ue()
        if chroma == 3:
            reader.bit()
        width, height = reader.ue(), reader.ue()
        if reader.bit():  # conformance_window_flag
            sub_w, sub_h = (2, 2) if chroma == 1 else (2, 1) if chroma == 2 else (1, 1)
            left, right, top, bottom = reader.ue(), reader.ue(), reader.ue(), reader.ue()
            width -= sub_w * (left + right)
            height -= sub_h * (top + bottom)
        return (width, height) if 0 < width <= 16384 and 0 < height <= 16384 else None
    except (IndexError, ValueError):
        return None


def scan_layers(path: Path, track_index: int, *, progress: Callable[[float], None] | None = None,
                cancelled: Callable[[], bool] | None = None) -> LayerFacts:
    """Parts BL / EL / RPU de la piste vidéo ``track_index`` d'un MKV (lecture complète, sans décodage)."""
    from core.matroska.reader import MatroskaReader

    reader = MatroskaReader(Path(path))
    reader.segment()
    tracks = reader.tracks()
    if not 0 <= track_index < len(tracks) or tracks[track_index].track_type != 1:
        raise ValueError(f"Piste vidéo #{track_index} absente.")
    track = tracks[track_index]
    private = track.codec_private
    length_size = (private[21] & 0x03) + 1 if len(private) >= 23 and private[0] == 1 else 4
    duration = (reader.segment_duration_ns() or 0) / 1e9
    total_size = max(1, Path(path).stat().st_size)
    sizes = {"bl": 0, "el": 0, "rpu": 0}
    el_size: tuple[int, int] | None = None
    frames = 0
    consumed = 0
    for block in reader.blocks(track_numbers={track.number}):
        if cancelled is not None and frames % 2048 == 0 and cancelled():
            raise InterruptedError
        frames += 1
        payload = block.payload
        consumed += len(payload)
        pos = 0
        while pos + length_size <= len(payload):
            size = int.from_bytes(payload[pos:pos + length_size], "big")
            start = pos + length_size
            if size < 2 or start + size > len(payload):
                break
            kind = (payload[start] >> 1) & 0x3F
            if kind == _EL_NAL:
                sizes["el"] += size + length_size
                if el_size is None and size > 4 and (payload[start + 2] >> 1) & 0x3F == _SPS_NAL:
                    el_size = sps_size(payload[start + 2:start + size])
            elif kind == _RPU_NAL:
                sizes["rpu"] += size + length_size
            else:
                sizes["bl"] += size + length_size
            pos = start + size
        if progress is not None and frames % 4096 == 0:
            progress(min(0.99, consumed / total_size))
    return LayerFacts(frames, duration, sizes["bl"], sizes["el"], sizes["rpu"], el_size)


def scan_in_background(path: Path, track_index: int, *,
                       cancelled: Callable[[], bool] | None = None) -> Callable[[], LayerFacts | None]:
    """Lance ``scan_layers`` dans un fil à part ; la fonction renvoyée attend son résultat (None : pas un
    MKV, annulé ou illisible). Le relevé lit tout le fichier : il se fait pendant l'échantillonnage."""
    from concurrent.futures import ThreadPoolExecutor

    if Path(path).suffix.lower() != ".mkv":
        return lambda: None
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fel-couches")
    future = executor.submit(scan_layers, Path(path), track_index, cancelled=cancelled)
    executor.shutdown(wait=False)

    def result() -> LayerFacts | None:
        try:
            return future.result()
        except Exception:  # relevé facultatif : l'analyse reste valable sans les débits
            return None

    return result

