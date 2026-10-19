"""Filtrage des CC partagé par l'assemblage natif et le candidat FFmpeg."""
from __future__ import annotations

import tempfile
from dataclasses import replace
from pathlib import Path

from core.closed_captions import CaptionFilter, caption_codec
from .ebml import binary_element, element
from .ids import ATTACHMENTS_ID, CHAPTERS_ID, CODEC_PRIVATE_ID, TAGS_ID
from .mux_plan import MatroskaMuxPacket, MatroskaMuxPlan, MatroskaMuxTrack, deterministic_uid128
from .reader import MatroskaReader, _split_laces, payload_children


def filter_caption_plan(plan: MatroskaMuxPlan) -> MatroskaMuxPlan:
    filters = {}
    tracks = []
    for track in plan.tracks:
        source = track.source_track
        if track.remove_closed_captions and source.track_type == 1:
            if not caption_codec(source.codec_id):
                raise ValueError(f"Retrait EIA-608/708 non pris en charge pour {source.codec_id}")
            filter_ = CaptionFilter(source.codec_id, source.codec_private)
            filters[track.output_number] = filter_
            private = filter_.private(source.codec_private)
            if private != source.codec_private:
                raw_entry = b"".join(
                    binary_element(kind, private) if kind == CODEC_PRIVATE_ID else element(kind, value)
                    for kind, value in payload_children(source.raw_entry)
                )
                source = replace(source, codec_private=private, raw_entry=raw_entry)
            track = replace(track, source_track=source, remove_closed_captions=False)
        tracks.append(track)
    if not filters:
        return plan

    def filtered(packet):
        filter_ = filters.get(packet.output_track_number)
        if filter_ is None:
            return packet
        block = packet.block
        codec_state = block.codec_state
        if codec_state:
            filter_ = CaptionFilter(filter_.codec, codec_state)
            filters[packet.output_track_number] = filter_
            codec_state = filter_.private(codec_state)
        if block.lace_count > 1 and block.encoded_frames_payload:
            frames = _split_laces(block.encoded_frames_payload, block.flags)
            rewritten = tuple(filter_.packet(frame) for frame in frames)
            if rewritten == frames and codec_state == block.codec_state:
                return packet
            # Reconstituer un lacing Xiph, même si les tailles ont changé.
            header = bytes([len(rewritten) - 1]) + b"".join(
                b"\xff" * (len(frame) // 255) + bytes([len(frame) % 255]) for frame in rewritten[:-1]
            )
            block = replace(block, payload=rewritten[0], encoded_frames_payload=header + b"".join(rewritten),
                            flags=(block.flags & ~6) | 2, lacing_mode=1, codec_state=codec_state)
        else:
            data = filter_.packet(block.payload)
            if data == block.payload and codec_state == block.codec_state:
                return packet
            block = replace(block, payload=data, codec_state=codec_state)
        if not block.payload:
            raise ValueError("Bloc vidéo sans image après retrait des sous-titres embarqués")
        return replace(packet, block=block)

    original = plan.packets
    def stream():
        try:
            for packet in original:
                yield filtered(packet)
        finally:
            close = getattr(original, "close", None)
            if close is not None:
                close()

    packets = tuple(filtered(packet) for packet in original) if isinstance(original, (tuple, list)) else stream()
    return replace(plan, tracks=tuple(tracks), packets=packets,
                   segment_uid=deterministic_uid128(plan.segment_uid, "sans-cc"))


def strip_candidate_captions(path: Path, output_indexes: set[int], *, cancel_cb=None, progress_cb=None) -> None:
    """Filtre le candidat FFmpeg avant sa validation/publication, atomiquement."""
    from .writer import MatroskaWriter

    reader = MatroskaReader(path)
    muxing_app, writing_app = reader.segment_info_apps()
    source_tracks = reader.tracks()
    tracks = tuple(MatroskaMuxTrack(
        path, track, track.number, track.uid, patch_language=False, patch_name=False, patch_flags=False,
        remove_closed_captions=index in output_indexes,
    ) for index, track in enumerate(source_tracks))
    # Même système de fichiers que le candidat : remplacement atomique final.
    with tempfile.TemporaryDirectory(prefix=".muxiveo-cc-", dir=path.parent) as folder:
        output = Path(folder) / "filtered.mkv"
        plan = MatroskaMuxPlan(
            output, tracks,
            (MatroskaMuxPacket(block.track_number, block, index)
             for index, block in enumerate(reader.blocks(first_lace_only=True))),
            timestamp_scale_ns=reader.timestamp_scale_ns(),
            duration_ns=reader.segment_duration_ns() or 0,
            opaque_top_level=tuple(raw for kind in (ATTACHMENTS_ID, CHAPTERS_ID, TAGS_ID)
                                   for raw in reader.raw_top_level(kind)),
            title=reader.segment_title(), regenerate_statistics=True,
            muxing_app=muxing_app or "Muxiveo", writing_app=writing_app or "Muxiveo",
        )
        MatroskaWriter().write(plan, cancel_cb=cancel_cb, progress_cb=progress_cb)
        output.replace(path)
