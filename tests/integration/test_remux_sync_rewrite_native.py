"""Synchro réécrite (mode container) assemblée en natif : même résultat que le backend FFmpeg."""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from core.inspector import FileInspector
from core.matroska.reader import MatroskaReader
from core.matroska.validation import interleave_warnings, scan_interleave
from core.workflows.remux import RemuxWorkflow
from core.workflows.remux_models import RemuxConfig, SourceInput, tracks_from_file_info
from core.workflows.remux_sync_rewrite import (
    SyncRewriteSettings,
    ffmpeg_input_origin,
    plan_sync_rewrites,
    probe_origin_payload,
)
from core.workflows.remux_mapping import resolve_mapped_tracks
from core.workflows.sync_calibration import SyncCalibration

from ._synth import wait_task

pytestmark = [
    pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                       reason="ffmpeg/ffprobe requis"),
    pytest.mark.usefixtures("qt_app"),
]

_SRT = """1
00:00:00,800 --> 00:00:01,600
Bonjour

2
00:00:02,200 --> 00:00:03,000
Au revoir
"""


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def _video_source(path: Path) -> Path:
    """Vidéo FFV1 + audio AC-3 démarrant à 0,5 s (repère acoustique)."""
    _ffmpeg("-f", "lavfi", "-i", "color=s=64x64:r=25:d=4",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3",
            "-af", "adelay=500:all=1", "-c:v", "ffv1", "-c:a", "ac3", "-shortest", str(path))
    return path


def _foreign_source(path: Path, *, audio_codec: str = "eac3", channels: int = 2) -> Path:
    """Audio d'une autre source + sous-titres texte."""
    srt = path.with_suffix(".srt")
    srt.write_text(_SRT, encoding="utf-8")
    layout = "7.1" if channels == 8 else "stereo"
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000:duration=3.5",
            "-i", str(srt), "-af", f"adelay=300:all=1,aformat=channel_layouts={layout}",
            "-map", "0:a", "-map", "1:s", "-c:a", audio_codec, "-c:s", "srt", str(path))
    return path


def _config(tmp_path: Path, output: Path, *, backend: str, offset_ms: int = 700,
            calibration: SyncCalibration | None = None, foreign: Path | None = None,
            audio_variant: str | None = None) -> RemuxConfig:
    video = _video_source(tmp_path / "video.mkv") if not (tmp_path / "video.mkv").exists() else tmp_path / "video.mkv"
    foreign = foreign or (tmp_path / "foreign.mkv")
    if not foreign.exists():
        _foreign_source(foreign)
    sources: list[SourceInput] = []
    order: list[tuple[int, int, str]] = []
    for index, path in enumerate((video, foreign)):
        tracks = tracks_from_file_info(FileInspector().inspect(path), file_id=f"src-{index}")
        if index == 1:
            tracks = [
                replace(track, time_shift_ms=offset_ms) if track.track_type in ("audio", "subtitle") else track
                for track in tracks
            ]
            if audio_variant is not None:
                tracks = [
                    replace(track, orig_codec=track.codec, codec=audio_variant) if track.track_type == "audio" else track
                    for track in tracks
                ]
        sources.append(SourceInput(path=path, file_index=index, tracks=tracks))
        order.extend((index, track.mkv_tid, track.entry_id) for track in tracks)
    calibrations = {"1": calibration.to_dict()} if calibration is not None else {}
    return RemuxConfig(sources=sources, output=output, track_order=order, keep_chapters=False,
                       mux_backend=backend, work_dir=tmp_path / "work", sync_calibrations=calibrations)


def _run(config: RemuxConfig, *, logs: list[str] | None = None) -> Path:
    workflow = RemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe", sync_rewrite_enabled=True)
    if logs is not None:
        workflow.log_message.connect(lambda level, message: logs.append(f"{level} {message}"))
    state = wait_task(workflow.run(config), timeout=180.0)
    assert state["failed"] is None, state["failed"]
    return config.output


def _blocks(path: Path) -> list[list[tuple[int, bytes]]]:
    reader = MatroskaReader(path)
    per_track: dict[int, list[tuple[int, bytes]]] = {track.number: [] for track in reader.tracks()}
    for block in reader.blocks(first_lace_only=True):
        timestamp = block.timestamp_ns if block.timestamp_ns is not None else block.timestamp_ms * 1_000_000
        per_track[block.track_number].append((round(timestamp / 1_000_000), block.payload))
    return [per_track[number] for number in sorted(per_track)]


def _assert_same_media(native: Path, ffmpeg: Path) -> None:
    native_tracks, ffmpeg_tracks = _blocks(native), _blocks(ffmpeg)
    assert len(native_tracks) == len(ffmpeg_tracks)
    for position, (ours, theirs) in enumerate(zip(native_tracks, ffmpeg_tracks), start=1):
        assert len(ours) == len(theirs), f"piste {position} : {len(ours)} blocs contre {len(theirs)}"
        assert [payload for _ts, payload in ours] == [payload for _ts, payload in theirs], f"piste {position}"
        drift = max((abs(a - b) for (a, _p), (b, _q) in zip(ours, theirs)), default=0)
        assert drift <= 1, f"piste {position} : horodatages à {drift} ms de la sortie FFmpeg"
    # Sortie native : entrelacement exact (la référence FFmpeg peut ne pas l'être).
    assert interleave_warnings(scan_interleave(native)) == []


@pytest.mark.parametrize("offset_ms", [700, -400])
def test_rewritten_foreign_tracks_match_the_ffmpeg_backend(tmp_path: Path, offset_ms: int) -> None:
    logs: list[str] = []
    native = _run(_config(tmp_path, tmp_path / "native.mkv", backend="auto", offset_ms=offset_ms), logs=logs)
    assert any("selected=native" in line for line in logs), logs
    ffmpeg = _run(_config(tmp_path, tmp_path / "ffmpeg.mkv", backend="ffmpeg", offset_ms=offset_ms))
    _assert_same_media(native, ffmpeg)


def test_multi_segment_calibration_matches_the_ffmpeg_backend(tmp_path: Path) -> None:
    calibration = SyncCalibration.from_dict({
        "version": 1,
        "segments": [
            {"start_ms": 0, "end_ms": 1500, "shift_ms": 200},
            {"start_ms": 1500, "end_ms": None, "shift_ms": 600},
        ],
    })
    native = _run(_config(tmp_path, tmp_path / "native.mkv", backend="native", offset_ms=0, calibration=calibration))
    ffmpeg = _run(_config(tmp_path, tmp_path / "ffmpeg.mkv", backend="ffmpeg", offset_ms=0, calibration=calibration))
    _assert_same_media(native, ffmpeg)


def test_variant_of_a_rewritten_track_is_encoded_once(tmp_path: Path) -> None:
    """E-AC-3 → AC-3 décalé : la réécriture produit la variante, aucune seconde perte (deux backends)."""
    logs: list[str] = []
    native = _run(_config(tmp_path, tmp_path / "native.mkv", backend="native", audio_variant="AC3"), logs=logs)
    ffmpeg_logs: list[str] = []
    ffmpeg = _run(_config(tmp_path, tmp_path / "ffmpeg.mkv", backend="ffmpeg", audio_variant="AC3"), logs=ffmpeg_logs)
    _assert_same_media(native, ffmpeg)
    final = next(line for line in ffmpeg_logs if "ffmpeg-remux" in line or ("$ " in line and "-f matroska" in line
                                                                            and ".partial" in line))
    assert "-c:a:1 ac3" not in final  # piste déjà au contrat : copiée
    assert [track.codec_id for track in MatroskaReader(native).tracks()].count("A_AC3") == 2


def test_rewrite_intents_follow_the_ffmpeg_rules(tmp_path: Path) -> None:
    config = _config(tmp_path, tmp_path / "out.mkv", backend="native")
    mapped = resolve_mapped_tracks(config)
    _tracks, intents = plan_sync_rewrites(config, mapped, SyncRewriteSettings(enabled=True))
    assert [intent.mapped_track.track.track_type for intent in intents] == ["audio", "subtitle"]
    assert all(not intent.required for intent in intents)
    # Mode « offset » forcé : décalage conteneur, aucune intention.
    forced = replace(config, sources=[
        replace(source, tracks=[replace(track, sync_rewrite_mode="offset") for track in source.tracks])
        for source in config.sources
    ])
    assert plan_sync_rewrites(forced, resolve_mapped_tracks(forced), SyncRewriteSettings(enabled=True))[1] == []
    # Réécriture désactivée : aucune intention, décalage conteneur.
    assert plan_sync_rewrites(config, mapped, SyncRewriteSettings(enabled=False))[1] == []


def test_preview_describes_the_rewrites(tmp_path: Path) -> None:
    config = _config(tmp_path, tmp_path / "out.mkv", backend="auto")
    plan = RemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe", sync_rewrite_enabled=True).compile_plan(config)
    assert plan.selected_backend == "native"
    actions = [action for action in plan.preparation_actions if action.kind == "sync_rewrite"]
    assert len(actions) == 2 and all("après sonde" in action.description for action in actions)
    assert "ffmpeg" in plan.required_tools


def test_ffmpeg_input_origin_matches_real_ffmpeg_on_partial_ts(tmp_path: Path) -> None:
    """Format à discontinuités : FFmpeg retire l'origine des flux retenus, pas celle du format."""
    ts = tmp_path / "late_audio.ts"
    _ffmpeg("-f", "lavfi", "-i", "color=s=64x64:r=25:d=3", "-f", "lavfi", "-i",
            "sine=frequency=440:sample_rate=48000:duration=2", "-filter:a", "adelay=1000:all=1",
            "-c:v", "mpeg2video", "-c:a", "mp2", "-output_ts_offset", "600", str(ts))
    payload = probe_origin_payload(ts, "ffprobe")
    audio_start = next(float(stream["start_time"]) for stream in payload["streams"] if stream["index"] == 1)
    assert ffmpeg_input_origin(payload, [1]) == pytest.approx(audio_start)
    assert ffmpeg_input_origin(payload, []) == pytest.approx(float(payload["format"]["start_time"]))
    # Oracle : muxage FFmpeg réel du seul flux audio, premier PTS en sortie = PTS source − Δ.
    out = tmp_path / "audio.mkv"
    _ffmpeg("-i", str(ts), "-map", "0:1", "-c", "copy", "-t", "1", str(out))
    first = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "packet=pts_time", "-read_intervals", "%+#1",
         "-of", "json", str(out)], capture_output=True, text=True, check=True,
    ).stdout)["packets"][0]
    assert float(first["pts_time"]) == pytest.approx(0.0, abs=0.002)
    matroska = probe_origin_payload(out, "ffprobe")
    assert ffmpeg_input_origin(matroska, [0]) == pytest.approx(float(matroska["format"]["start_time"]))


def _foreign_mp4(path: Path) -> Path:
    """MP4 (AAC à liste d'édition, mov_text) : piste étrangère lue en flux par le natif."""
    srt = path.with_suffix(".srt")
    srt.write_text(_SRT, encoding="utf-8")
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000:duration=3.5", "-i", str(srt),
            "-map", "0:a", "-map", "1:s", "-c:a", "aac", "-c:s", "mov_text", str(path))
    return path


def _foreign_ts(path: Path) -> Path:
    """TS à origine 600 s, deux audios dont un seul réécrit : source canonicalisée, origine mesurée."""
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000:duration=3.5",
            "-f", "lavfi", "-i", "sine=frequency=660:sample_rate=48000:duration=3.5",
            "-filter:a:1", "adelay=400:all=1",
            "-map", "0:a", "-map", "1:a", "-c:a", "ac3", "-output_ts_offset", "600", str(path))
    return path


def test_streamed_mp4_foreign_track_matches_the_ffmpeg_backend(tmp_path: Path) -> None:
    foreign = _foreign_mp4(tmp_path / "foreign.mp4")
    native = _run(_config(tmp_path, tmp_path / "native.mkv", backend="native", foreign=foreign))
    ffmpeg = _run(_config(tmp_path, tmp_path / "ffmpeg.mkv", backend="ffmpeg", foreign=foreign))
    _assert_same_media(native, ffmpeg)


def test_canonicalized_ts_foreign_track_matches_the_ffmpeg_backend(tmp_path: Path) -> None:
    foreign = _foreign_ts(tmp_path / "foreign.ts")

    def one_rewritten(config: RemuxConfig) -> RemuxConfig:
        # Seul le premier audio du TS est décalé (le second garde une route native).
        sources = list(config.sources)
        tracks = [replace(track, time_shift_ms=700 if position == 0 else 0)
                  for position, track in enumerate(sources[1].tracks)]
        sources[1] = replace(sources[1], tracks=tracks)
        return replace(config, sources=sources)

    native = _run(one_rewritten(_config(tmp_path, tmp_path / "native.mkv", backend="native", foreign=foreign)))
    ffmpeg = _run(one_rewritten(_config(tmp_path, tmp_path / "ffmpeg.mkv", backend="ffmpeg", foreign=foreign)))
    _assert_same_media(native, ffmpeg)


def test_subtitle_emptied_by_the_rewrite_keeps_its_track(tmp_path: Path) -> None:
    """Tous les événements avant zéro : piste conservée, vide, TrackEntry de l'origine (natif seul)."""
    config = _config(tmp_path, tmp_path / "native.mkv", backend="native", offset_ms=-400)
    sources = list(config.sources)
    sources[1] = replace(sources[1], tracks=[
        replace(track, time_shift_ms=-5000) if track.track_type == "subtitle" else track
        for track in sources[1].tracks
    ])
    output = _run(replace(config, sources=sources))
    reader = MatroskaReader(output)
    subtitle = next(track for track in reader.tracks() if track.track_type == 17)
    assert subtitle.codec_id == "S_TEXT/UTF8"
    assert not [block for block in reader.blocks() if block.track_number == subtitle.number]
