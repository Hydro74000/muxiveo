"""Assemblage natif avec pistes MP4 lues en flux : oracle face à la canonicalisation fichier.

Le flux (producteurs FFmpeg sur pipes) doit donner exactement la sortie de la
canonicalisation fichier : mêmes TrackEntry (CodecPrivate, signalisation,
DefaultDuration…), mêmes blocs (horodatage, payload, compléments), avec des
horodatages égaux à ceux de la source rebasés sur son ``start_time``.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from core.inspector import FileInspector
from core.matroska.reader import MatroskaReader
from core.workflows.remux import RemuxWorkflow
from core.workflows.remux_models import RemuxConfig, SourceInput, tracks_from_file_info

from ._synth import make_mkv_with_srt, wait_task

pytestmark = [
    pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                       reason="ffmpeg/ffprobe requis"),
    pytest.mark.usefixtures("qt_app"),
]


def _mp4_with_bframes_and_late_audio(path: Path) -> Path:
    """H.264 à B-frames, AAC (préchargement, liste d'édition) démarrant 0,5 s après la vidéo."""
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc=size=320x180:rate=24",
        "-itsoffset", "0.5", "-f", "lavfi", "-i", "sine=frequency=800:sample_rate=48000",
        "-t", "4", "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-bf", "2", "-g", "24",
        "-c:a", "aac", "-shortest", str(path),
    ], check=True)
    return path


def _config(tmp_path: Path, output: Path) -> RemuxConfig:
    mp4 = _mp4_with_bframes_and_late_audio(tmp_path / "video.mp4")
    mkv = tmp_path / "subs.mkv"
    if not mkv.exists():
        make_mkv_with_srt(mkv, duration=4.0)
    sources = []
    order: list[tuple[int, int]] = []
    for index, path in enumerate((mp4, mkv)):
        tracks = tracks_from_file_info(FileInspector().inspect(path), file_id=f"src-{index}")
        sources.append(SourceInput(path=path, file_index=index, tracks=tracks))
        order.extend((index, track.mkv_tid) for track in tracks
                     if (index == 0 and track.track_type in ("video", "audio"))
                     or (index == 1 and track.track_type in ("audio", "subtitle")))
    return RemuxConfig(sources=sources, output=output, track_order=order, keep_chapters=False,
                       mux_backend="native", work_dir=tmp_path / "work")


def _remux(tmp_path: Path, output: Path) -> Path:
    state = wait_task(RemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe").run(_config(tmp_path, output)),
                      timeout=120.0)
    assert state["failed"] is None, state["failed"]
    return output


def _tracks(path: Path) -> list[tuple]:
    return [(t.codec_id, t.codec_private, t.default_duration_ns, t.video, t.audio, t.block_addition_mappings,
             t.language, t.name) for t in MatroskaReader(path).tracks()]


def _blocks(path: Path) -> dict[int, list[tuple]]:
    blocks: dict[int, list[tuple]] = {}
    for b in MatroskaReader(path).blocks(first_lace_only=True):
        blocks.setdefault(b.track_number, []).append(
            (b.timestamp_ns, b.duration_ns, b.payload, b.block_additions, b.discard_padding_ns, b.is_keyframe)
        )
    return blocks


def test_streamed_mp4_equals_file_canonicalization(tmp_path: Path, monkeypatch) -> None:
    from core.workflows import remux_plan

    streamed = _remux(tmp_path, tmp_path / "streamed.mkv")
    # Aucun artefact MKV de la source MP4 n'a été écrit (seulement de petits en-têtes).
    assert not list((tmp_path / "work").rglob("source_*.mkv"))

    monkeypatch.setattr(remux_plan, "is_streamable_source", lambda _path: False)
    monkeypatch.setattr("core.workflows.remux_backend.is_streamable_source", lambda _path: False)
    canonical = _remux(tmp_path, tmp_path / "canonical.mkv")

    assert _tracks(streamed) == _tracks(canonical)
    assert _blocks(streamed) == _blocks(canonical)


def test_streamed_timestamps_follow_source_origin(tmp_path: Path) -> None:
    output = _remux(tmp_path, tmp_path / "out.mkv")
    source = tmp_path / "video.mp4"
    probe = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=start_time:packet=stream_index,pts_time",
         "-of", "json", str(source)], capture_output=True, text=True, check=True,
    ).stdout)
    origin = float(probe["format"]["start_time"])
    expected: dict[int, list[int]] = {}
    for packet in probe["packets"]:
        expected.setdefault(int(packet["stream_index"]), []).append(
            round((float(packet["pts_time"]) - origin) * 1000)
        )
    written = _blocks(output)
    # Pistes 1 (vidéo) et 2 (audio) de la sortie = flux 0 et 1 du MP4 ; ordre de décodage conservé.
    for output_track, source_stream in ((1, 0), (2, 1)):
        actual = [round(item[0] / 1_000_000) for item in written[output_track]]
        assert len(actual) == len(expected[source_stream])
        assert all(abs(a - e) <= 1 for a, e in zip(actual, expected[source_stream]))
    # Audio démarrant 0,5 s après la vidéo : décalage relatif conservé.
    assert 450 <= written[2][0][0] / 1_000_000 - written[1][0][0] / 1_000_000 <= 550


def test_streamed_remux_is_interleaved(tmp_path: Path) -> None:
    output = _remux(tmp_path, tmp_path / "out.mkv")
    highest = lag = 0
    for block in MatroskaReader(output).blocks():
        highest = max(highest, block.timestamp_ms)
        lag = max(lag, highest - block.timestamp_ms)
    assert lag < 200  # réordonnancement des B-frames seulement


# ---------------------------------------------------------------------------
# Encodage natif (vidéo copiée depuis MP4)
# ---------------------------------------------------------------------------

def _run_command(command, _label=""):
    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return result.stdout


def _encode_config(source: Path, output: Path, *, offsets=()):
    from core.workflows.encode import AudioTrackSettings, EncodeConfig, VideoEncodeSettings

    return EncodeConfig(
        source=source, output=output, work_dir=output.parent,
        video=VideoEncodeSettings(codec="copy"), audio_tracks=[AudioTrackSettings(1)],
        copy_subtitles=False, keep_chapters=False, track_time_offsets=list(offsets), mux_backend="native",
        duration_s=4,
    )


def _assemble_encode(config) -> Path:
    from core.workflows.common.sync_rewrite import SyncRewriteService
    from core.workflows.encode.runtime.native_mux import NativeVideoArtifactRef, assemble_encode_output_native

    return assemble_encode_output_native(
        config, video_artifacts=[NativeVideoArtifactRef(config.source, 0)], work_dir=config.work_dir,
        signals=None, run_cmd=_run_command, log=lambda *_: None, resolved_subtitles=[],
        sync_rewrite=SyncRewriteService(ffmpeg_bin="ffmpeg"),
    )


def test_encode_copy_from_mp4_streams_like_file_canonicalization(tmp_path: Path, monkeypatch) -> None:
    source = _mp4_with_bframes_and_late_audio(tmp_path / "video.mp4")
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    streamed = _assemble_encode(_encode_config(source, tmp_path / "a" / "out.mkv"))
    assert not list((tmp_path / "a").rglob("native_source_*.mkv"))
    monkeypatch.setattr("core.workflows.encode.runtime.native_mux.is_streamable_source", lambda _path: False)
    canonical = _assemble_encode(_encode_config(source, tmp_path / "b" / "out.mkv"))
    assert _tracks(streamed) == _tracks(canonical)
    assert _blocks(streamed) == _blocks(canonical)


def test_encode_sync_rewrite_reads_the_original_mp4(tmp_path: Path, monkeypatch) -> None:
    """Réécriture depuis le MP4 d'origine (jamais un en-tête) : même sortie qu'avec la canonicalisation."""
    from core.workflows.common.track_types import TrackOffset
    from core.workflows.sync_calibration import SyncCalibration, SyncSegment

    source = _mp4_with_bframes_and_late_audio(tmp_path / "video.mp4")
    calibration = SyncCalibration((SyncSegment(0, 300),)).to_dict()
    offsets = [TrackOffset("audio", source, 1, calibration=calibration)]
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    streamed = _assemble_encode(_encode_config(source, tmp_path / "a" / "out.mkv", offsets=offsets))
    monkeypatch.setattr("core.workflows.encode.runtime.native_mux.is_streamable_source", lambda _path: False)
    canonical = _assemble_encode(_encode_config(source, tmp_path / "b" / "out.mkv", offsets=offsets))
    assert _tracks(streamed) == _tracks(canonical)
    streamed_blocks, canonical_blocks = _blocks(streamed), _blocks(canonical)
    assert streamed_blocks[1] == canonical_blocks[1]  # vidéo copiée : identique
    # Audio réencodé par la réécriture : mêmes horodatages, même son (l'encodeur AAC
    # n'est pas déterministe au bit près selon l'entrée décodée).
    assert [item[0] for item in streamed_blocks[2]] == [item[0] for item in canonical_blocks[2]]
    a, b = _pcm(streamed), _pcm(canonical)
    # Fin de flux : la découpe de la dernière trame peut différer de quelques échantillons.
    assert abs(len(a) - len(b)) < 1024
    # Même son au même instant, à moins d'une milliseconde près : la canonicalisation
    # arrondit ses horodatages au tick de 1 ms, la lecture du MP4 d'origine non.
    onsets = [next(i for i, value in enumerate(pcm) if abs(value) > 1000) for pcm in (a, b)]
    assert abs(onsets[0] - onsets[1]) < 48
    assert abs(onsets[0] / 48 - 800) <= 25  # 0,5 s de départ + 0,3 s de synchro
    shift = onsets[1] - onsets[0]
    pairs = list(zip(a[max(0, -shift):], b[max(0, shift):]))
    error = sum((x - y) ** 2 for x, y in pairs)
    energy = sum(y * y for _x, y in pairs)
    assert error <= 0.01 * energy


def _pcm(path: Path) -> list[int]:
    from array import array

    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0", "-ac", "1", "-ar", "48000",
                          "-f", "s16le", "-"], capture_output=True, check=True).stdout
    return list(array("h", raw))


def test_encode_workflow_skips_native_video_intermediate_for_mp4_copy(tmp_path: Path, monkeypatch) -> None:
    from core.workflows.encode import EncodeWorkflow

    source = _mp4_with_bframes_and_late_audio(tmp_path / "video.mp4")
    workflow = EncodeWorkflow(ffmpeg_bin="ffmpeg", generate_nfo=False)
    monkeypatch.setattr(workflow, "_build_video_only_mkv_commands",
                        lambda *_a, **_k: pytest.fail("intermédiaire vidéo construit"))
    state = wait_task(workflow.run(_encode_config(source, tmp_path / "out.mkv"), validate=False), timeout=120.0)
    assert state["failed"] is None, state["failed"]
    assert len(MatroskaReader(tmp_path / "out.mkv").tracks()) == 2


# ---------------------------------------------------------------------------
# Contexte, duplication, échec d'un producteur
# ---------------------------------------------------------------------------

def _mp4_with_chapters(path: Path) -> Path:
    plain = _mp4_with_bframes_and_late_audio(path.with_name("plain.mp4"))
    metadata = path.with_name("chapters.txt")
    metadata.write_text(
        ";FFMETADATA1\ntitle=Titre global\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=2000\ntitle=Un\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=2000\nEND=4000\ntitle=Deux\n",
        encoding="utf-8",
    )
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(plain), "-i", str(metadata),
                    "-map", "0", "-map_metadata", "1", "-map_chapters", "1", "-c", "copy", str(path)], check=True)
    return path


def test_chapters_and_tags_of_streamed_source_kept(tmp_path: Path, monkeypatch) -> None:
    from core.matroska.ids import CHAPTERS_ID
    from core.workflows import remux_plan

    source = _mp4_with_chapters(tmp_path / "chapters.mp4")

    def remux(name: str) -> Path:
        tracks = tracks_from_file_info(FileInspector().inspect(source), file_id="src-0")
        output = tmp_path / name
        config = RemuxConfig(
            sources=[SourceInput(path=source, file_index=0, tracks=tracks)], output=output,
            track_order=[(0, track.mkv_tid) for track in tracks], keep_chapters=True,
            mux_backend="native", work_dir=tmp_path / "work",
        )
        state = wait_task(RemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe").run(config), timeout=120.0)
        assert state["failed"] is None, state["failed"]
        return output

    streamed = remux("streamed.mkv")
    monkeypatch.setattr(remux_plan, "is_streamable_source", lambda _path: False)
    monkeypatch.setattr("core.workflows.remux_backend.is_streamable_source", lambda _path: False)
    canonical = remux("canonical.mkv")
    chapters = MatroskaReader(streamed).chapter_editions()
    assert chapters and chapters == MatroskaReader(canonical).chapter_editions()
    assert MatroskaReader(streamed).raw_top_level(CHAPTERS_ID)

    def tag_values(path: Path) -> set[tuple[str, str]]:
        return {value for tag in MatroskaReader(path).tags() for value in tag.values
                if value[0].upper() not in {"BPS", "DURATION", "NUMBER_OF_FRAMES", "NUMBER_OF_BYTES",
                                            "_STATISTICS_TAGS", "_STATISTICS_WRITING_APP",
                                            "_STATISTICS_WRITING_DATE_UTC"}}

    assert tag_values(streamed) == tag_values(canonical)


def test_duplicated_mp4_track_reads_one_stream_for_both_outputs(tmp_path: Path) -> None:
    from dataclasses import replace

    from core.workflows.encode import AudioTrackSettings

    source = _mp4_with_bframes_and_late_audio(tmp_path / "video.mp4")
    config = replace(_encode_config(source, tmp_path / "out.mkv"),
                     audio_tracks=[AudioTrackSettings(1), AudioTrackSettings(1)])
    output = _assemble_encode(config)
    blocks = _blocks(output)
    assert len(blocks) == 3
    assert blocks[2] == blocks[3]


def test_failing_producer_fails_the_assembly_without_output(tmp_path: Path, monkeypatch) -> None:
    from core.workflows.common import native_stream_inputs

    source = _mp4_with_bframes_and_late_audio(tmp_path / "video.mp4")
    real = native_stream_inputs.producer_command

    def broken(ffmpeg_bin, spec):
        command = real(ffmpeg_bin, spec)
        if spec.stream_index == 1:
            command.insert(command.index("-f"), "-option_inexistante")
        return command

    monkeypatch.setattr(native_stream_inputs, "producer_command", broken)
    with pytest.raises(RuntimeError, match="FFmpeg a échoué"):
        _assemble_encode(_encode_config(source, tmp_path / "out.mkv"))
    assert not (tmp_path / "out.mkv").exists()
    assert not list(tmp_path.glob("*.partial"))
