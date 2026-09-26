"""Matérialisation audio/sous-titres avant assemblage, sans délai utilisateur."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from core.workflows.remux_models import RemuxError, SourceInput
from core.workflows.remux_mapping import resolve_mapped_tracks
from core.workflows.sync_calibration import SyncCalibration
from core.workflows.common.sync_rewrite import (
    audio_bitrate_kbps_from_display_info,
    normalized_rewrite_codec,
    sync_rewrite_forced_offset,
)


def audio_filter(calibration: SyncCalibration, crossfade_ms=80) -> str:
    """Fondus de bord sans chevauchement : la durée et les ancrages restent exacts."""
    from core.workflows.cadence import CadenceType, build_cadence_audio_filter

    segments = calibration.segments
    parts, labels, previous_end = [], [], 0.0
    in_labels = ["[0:a:0]"] * len(segments)

    if calibration.cadence_mismatch and getattr(calibration.cadence_mismatch, "cadence_type", None) not in (None, "none", CadenceType.NONE):
        cadence_str = build_cadence_audio_filter(
            calibration.cadence_mismatch,
            getattr(calibration, "cadence_audio_method", "atempo"),
        )
        if len(segments) > 1:
            split_labels = "".join(f"[cin{i}]" for i in range(len(segments)))
            parts.append(f"[0:a:0]{cadence_str},asplit={len(segments)}{split_labels}")
            in_labels = [f"[cin{i}]" for i in range(len(segments))]
        else:
            parts.append(f"[0:a:0]{cadence_str}[cadence_in]")
            in_labels = ["[cadence_in]"]

    ratio = calibration.cadence_mismatch.time_stretch_ratio if calibration.cadence_mismatch else 1.0
    for index, segment in enumerate(segments):
        stop = segments[index + 1].start_ms * ratio if index + 1 < len(segments) else None
        start = max(segment.start_ms * ratio, previous_end - segment.shift_ms, -segment.shift_ms)
        if stop is not None and start >= stop:
            previous_end = max(previous_end, stop + segment.shift_ms)
            continue
        gap = max(0, start + segment.shift_ms - previous_end)
        chain = f"{in_labels[index]}atrim=start={start / 1000:.6f}"
        if stop is not None:
            chain += f":end={stop / 1000:.6f}"
        chain += ",asetpts=PTS-STARTPTS"
        fade = max(0, crossfade_ms / 1000 / 2)
        if stop is not None:
            fade = min(fade, (stop - start) / 2000)
        if fade and index:
            chain += f",afade=t=in:d={fade:.6f}"
        if fade and stop is not None:
            chain += f",afade=t=out:st={max(0, (stop - start) / 1000 - fade):.6f}:d={fade:.6f}"
        if gap:
            chain += f",adelay={gap:.6f}:all=1"
        label = f"seg{index}"
        parts.append(chain + f"[{label}]")
        labels.append(f"[{label}]")
        if stop is not None:
            previous_end = max(previous_end, stop + segment.shift_ms)
    parts.append("".join(labels) + f"concat=n={len(labels)}:v=0:a=1,asetpts=PTS-STARTPTS[out]")
    return ";".join(parts)


def track_calibration(config, mapped):
    payload = mapped.track.sync_calibration or config.sync_calibrations.get(str(mapped.source_file_index))
    return SyncCalibration.from_dict(payload) if payload else SyncCalibration.linear(mapped.track.time_shift_ms)


def preparation_commands(config, root, ffmpeg):
    """Décrit chaque piste transformée ; le même contrat sert à la preview."""
    result = []
    root = Path(root)
    if config.sync_mode != "physical":
        return result
    mapped_tracks = resolve_mapped_tracks(config)
    for index, mapped in enumerate(mapped_tracks):
        track = mapped.track
        if sync_rewrite_forced_offset(track):
            continue
        calibrated = bool(track.sync_calibration) or str(mapped.source_file_index) in config.sync_calibrations
        mirror_offset = 0
        if track.track_type == "subtitle" and not track.time_shift_ms and not calibrated and config.sync_subtitles == "mirror":
            offsets = {m.track.time_shift_ms for m in mapped_tracks if m.source_file_index == mapped.source_file_index and m.track.track_type == "audio"}
            if len(offsets) > 1:
                raise RemuxError("Recalage miroir ambigu : plusieurs décalages audio dans la même source.")
            mirror_offset = next(iter(offsets), 0)
        if track.track_type not in {"audio", "subtitle"} or not (track.time_shift_ms or calibrated or mirror_offset):
            continue
        if track.track_type == "subtitle" and config.sync_subtitles == "none" and not track.time_shift_ms:
            continue
        calibration = SyncCalibration.linear(mirror_offset) if mirror_offset else track_calibration(config, mapped)
        base = [str(ffmpeg), "-y", "-nostdin", "-v", "error", "-i", str(mapped.source_path),
                "-map", f"0:{mapped.stream_index}"]
        output = root / f"sync_{index}.mka"
        if track.track_type == "audio":
            codec = normalized_rewrite_codec(track.codec)
            has_cadence = bool(
                calibration.cadence_mismatch
                and getattr(calibration.cadence_mismatch, "cadence_type", None) not in (None, "none")
            )
            copy = (not has_cadence) and len(calibration.segments) == 1 and calibration.segments[0].shift_ms == 0
            if copy:
                # Les codecs à pré-roll doivent conserver leur CodecDelay.
                if codec in {"aac", "opus", "mp3"}:
                    continue
                command = base + ["-c:a", "copy", "-bsf:a", "setts=pts=PTS-STARTPTS:dts=DTS-STARTDTS"]
            else:
                if codec not in {"aac", "ac3", "eac3", "flac"} or any(
                        tag in (track.display_info + track.title).casefold() for tag in ("atmos", "dts:x")):
                    raise RemuxError("Synchronisation physique : codec immersif/non encodable ; choisir explicitement une variante AAC, AC3, EAC3 ou FLAC.")
                graph = audio_filter(calibration, config.crossfade_ms).replace("[0:a:0]", f"[0:{mapped.stream_index}]")
                command = base[:-2] + ["-filter_complex", graph, "-map", "[out]", "-c:a", codec]
                bitrate = audio_bitrate_kbps_from_display_info(track.display_info)
                if codec != "flac" and bitrate:
                    command += ["-b:a", f"{bitrate}k"]
                if codec in {"ac3", "eac3", "flac"}:
                    command += ["-bsf:a", "setts=pts=PTS-STARTPTS:dts=DTS-STARTDTS"]
            command += [str(output)]
        else:
            codec = track.codec.casefold()
            if codec in {"ass", "ssa"}:
                suffix, encoder = ".ass", "ass"
            elif codec in {"subrip", "srt", "webvtt", "mov_text", "text"}:
                suffix, encoder = ".srt", "srt"
            else:
                raise RemuxError("Recalage miroir bitmap PGS/VobSub non pris en charge ; fournir des sous-titres texte ou choisir --sync-subtitles none.")
            output = root / f"sync_{index}{suffix}"
            command = base + ["-c:s", encoder, str(output)]
        result.append((mapped, output, command, calibration))
    return result


def prepare_physical(config, root: Path, ffmpeg, run, log=None):
    from core.workflows.subtitle_sync import shift_file
    sources = [replace(s, tracks=[replace(t) for t in s.tracks]) for s in config.sources]
    order = list(config.track_order)
    for mapped, output, command, calibration in preparation_commands(config, root, ffmpeg):
        if log is not None:
            track = mapped.track
            segments = calibration.segments
            cadence_info = ""
            if calibration.cadence_mismatch and getattr(calibration.cadence_mismatch, "cadence_type", None) not in (None, "none"):
                cadence_info = f" — cadence {calibration.cadence_mismatch.description} ({calibration.cadence_audio_method})"
            if len(segments) > 1:
                log(
                    "INFO",
                    f"Synchronisation physique (réécriture exacte) : piste #{mapped.stream_index} "
                    f"({track.track_type} {track.codec}){cadence_info} — {len(segments)} segments ({len(segments) - 1} coupures, fondu {config.crossfade_ms} ms) :"
                )
                for line in calibration.summary_lines():
                    log("INFO", f"  • {line}")
            else:
                shift = segments[0].shift_ms if segments else 0.0
                log(
                    "INFO",
                    f"Synchronisation physique (réécriture exacte) : piste #{mapped.stream_index} "
                    f"({track.track_type} {track.codec}){cadence_info} — décalage {shift:+.1f} ms"
                )
        run(command, "physical-sync")
        if mapped.track.track_type == "subtitle":
            shift_file(output, output, calibration)
        track = replace(mapped.track, mkv_tid=0, time_shift_ms=0, sync_rewrite_label="", sync_rewrite_mode="", sync_calibration=None, is_new=False, orig_codec=mapped.track.codec)
        source_index = max(s.file_index for s in sources) + 1
        sources.append(SourceInput(output, source_index, [track]))
        for index, item in enumerate(order):
            if item[0] == mapped.source_file_index and item[1] == mapped.stream_index and (len(item) < 3 or item[2] == track.entry_id):
                order[index] = (source_index, 0, track.entry_id)
    return replace(config, sources=sources, track_order=order, sync_mode="container", sync_calibrations={})
