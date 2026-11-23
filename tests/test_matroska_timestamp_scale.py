"""Échelle de temps Matroska 1 ms : option FFmpeg ``-timestamp_scale`` et quantification native."""
from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from core.extractor import TrackExtractor
from core.matroska import ebml
from core.matroska.assembly import (
    MatroskaAssemblyPlan,
    MatroskaAssemblyTrack,
    assembly_output_contract,
    compile_assembly_plan,
    quantize_block,
)
from core.matroska.ids import CODEC_ID_ID, TRACK_NUMBER_ID, TRACK_TYPE_ID, TRACK_UID_ID
from core.matroska.mux_plan import MatroskaMuxPacket, MatroskaMuxPlan, MatroskaMuxTrack
from core.matroska.reader import MatroskaBlock, MatroskaReader, MatroskaTrack
from core.matroska.writer import MatroskaWriter
from core.workflows.common import ffmpeg_runtime
from core.workflows.common.ffmpeg_runtime import matroska_output_args
from core.workflows.encode import AudioTrackSettings, EncodeConfig, EncodeWorkflow, VideoEncodeSettings
from core.workflows.remux_models import SourceInput, TrackEntry
from core.workflows.remux_plan import (
    NativeAudioVariantPlan,
    build_audio_variant_command,
    build_canonicalization_command,
)

_OPTION = ["-timestamp_scale", "1000000"]


@pytest.fixture
def option_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ffmpeg_runtime, "matroska_timestamp_scale_supported", lambda _ffmpeg: True)


def _fake_ffmpeg(path: Path, *, with_option: bool) -> Path:
    line = "  -timestamp_scale   <int64>  E... clock of the file" if with_option else "  -live <boolean>"
    path.write_text(f"#!/bin/sh\necho 'Muxer matroska [Matroska]:'\necho '{line}'\n")
    path.chmod(0o755)
    return path


@pytest.mark.real_matroska_timestamp_scale_probe
@pytest.mark.skipif(os.name == "nt", reason="binaire factice en script shell")
def test_probe_reads_muxer_help_and_follows_binary_changes(tmp_path: Path) -> None:
    binary = _fake_ffmpeg(tmp_path / "ffmpeg", with_option=True)
    assert ffmpeg_runtime.matroska_timestamp_scale_supported(binary)
    assert matroska_output_args(binary, tmp_path / "out.mkv") == _OPTION
    # Binaire remplacé (taille différente) : nouvelle sonde, pas le résultat en cache.
    _fake_ffmpeg(binary, with_option=False)
    assert not ffmpeg_runtime.matroska_timestamp_scale_supported(binary)
    assert matroska_output_args(binary, tmp_path / "out.mkv") == []


@pytest.mark.real_matroska_timestamp_scale_probe
def test_probe_without_binary_adds_nothing(tmp_path: Path) -> None:
    assert not ffmpeg_runtime.matroska_timestamp_scale_supported(tmp_path / "absent")
    assert not ffmpeg_runtime.matroska_timestamp_scale_supported(None)


@pytest.mark.usefixtures("option_supported")
def test_option_only_for_matroska_outputs(tmp_path: Path) -> None:
    assert matroska_output_args("ffmpeg", tmp_path / "a.MKA") == _OPTION
    assert matroska_output_args("ffmpeg", tmp_path / "a.webm") == _OPTION
    assert matroska_output_args("ffmpeg") == _OPTION  # pipe ou -f matroska explicite
    assert matroska_output_args("ffmpeg", tmp_path / "a.mp4") == []


def _assert_option_before_output(cmd: list[str], output: Path) -> None:
    assert cmd[-1] == str(output)
    index = cmd.index("-timestamp_scale")
    assert cmd[index + 1] == "1000000"
    assert index > max(i for i, token in enumerate(cmd) if token == "-i")


@pytest.mark.usefixtures("option_supported")
def test_canonicalization_and_audio_variant_commands(tmp_path: Path) -> None:
    source_path = tmp_path / "in.mp4"
    source_path.write_bytes(b"fixture")
    source = SourceInput(source_path, 0, [
        TrackEntry(mkv_tid=0, track_type="video", codec="COPY", display_info="", language="und",
                   title="", file_id="src0"),
    ])
    canonical = tmp_path / "canon.mkv"
    _assert_option_before_output(
        build_canonicalization_command(source, canonical, "ffmpeg", selected_streams=[0]), canonical,
    )
    variant = NativeAudioVariantPlan(
        order_index=0, source_file_index=0, stream_index=1, entry_id="a", target_codec="aac",
        encoder="aac", bitrate_kbps=192, downmix_51=False, target_name="audio_variant_0.mkv",
    )
    target = tmp_path / "audio_variant_0.mkv"
    _assert_option_before_output(build_audio_variant_command(variant, source_path, 1, target, "ffmpeg"), target)


@pytest.mark.usefixtures("option_supported")
def test_extraction_adds_option_for_matroska_only(tmp_path: Path) -> None:
    source = tmp_path / "in.mkv"
    mkv = TrackExtractor.build_command(
        "ffmpeg", source, 0, "video", TrackExtractor.plan_video("hevc", container=True), tmp_path / "out.mkv",
    )
    _assert_option_before_output(mkv, tmp_path / "out.mkv")
    raw = TrackExtractor.build_command(
        "ffmpeg", source, 0, "video", TrackExtractor.plan_video("hevc"), tmp_path / "out.hevc",
    )
    assert "-timestamp_scale" not in raw


@pytest.mark.usefixtures("option_supported")
def test_encode_final_mux_adds_option_for_mkv_output_only(qt_app, tmp_path: Path) -> None:
    _ = qt_app
    src = tmp_path / "s.mkv"
    src.write_bytes(b"")
    for output, expected in ((tmp_path / "o.mkv", True), (tmp_path / "o.mp4", False)):
        cfg = EncodeConfig(
            source=src, output=output, video=VideoEncodeSettings(codec="copy"),
            audio_tracks=[AudioTrackSettings(stream_index=1, codec="copy")],
            copy_subtitles=False, keep_chapters=False, duration_s=10,
        )
        built = EncodeWorkflow(ffmpeg_bin="ffmpeg", generate_nfo=False).build_command(cfg)
        commands = cast(list[list[str]], built) if isinstance(built[0], list) else [cast(list[str], built)]
        final = commands[-1]
        if expected:
            _assert_option_before_output(final, output)
        else:
            assert "-timestamp_scale" not in final


# ---------------------------------------------------------------------------
# Quantification native
# ---------------------------------------------------------------------------

_MS = 1_000_000


def test_quantize_keeps_millisecond_blocks_unchanged() -> None:
    block = MatroskaBlock(1, 40, 0x80, b"x", timestamp_ns=40 * _MS, duration_ns=20 * _MS,
                          references_ns=(-40 * _MS,))
    assert quantize_block(block, _MS) is block


def test_quantize_rounds_endpoints_not_deltas() -> None:
    # t = 41,708 ms, référence vers 0,000 ms, durée jusqu'à 83,417 ms.
    block = MatroskaBlock(1, 42, 0, b"x", timestamp_ns=41_708_000, duration_ns=41_709_000,
                          references_ns=(-41_708_000,))
    quantized = quantize_block(block, _MS)
    assert quantized.timestamp_ns == 42 * _MS
    assert quantized.timestamp_ms == 42
    # Fin q(83,417) = 83 ms : durée 41 ms (l'arrondi indépendant de l'écart donnerait 42).
    assert quantized.duration_ns == 41 * _MS
    # Cible q(0) − q(41,708) = −42 ms : la référence vise toujours le bloc à 0.
    assert quantized.references_ns == (-42 * _MS,)
    assert quantized.references == (-42,)


def test_quantize_never_collapses_nonzero_values() -> None:
    block = MatroskaBlock(1, 0, 0, b"x", timestamp_ns=10_000, duration_ns=200_000, references_ns=(300_000,))
    quantized = quantize_block(block, _MS)
    assert quantized.timestamp_ns == 0
    assert quantized.duration_ns == _MS
    assert quantized.references_ns == (_MS,)
    zero = quantize_block(MatroskaBlock(1, 0, 0, b"x", timestamp_ns=10_000, duration_ns=0), _MS)
    assert zero.duration_ns == 0


def _audio_track(number: int, uid: int) -> MatroskaTrack:
    raw = b"".join((
        ebml.uint_element(TRACK_NUMBER_ID, number),
        ebml.uint_element(TRACK_UID_ID, uid),
        ebml.uint_element(TRACK_TYPE_ID, 2),
        ebml.ascii_element(CODEC_ID_ID, "A_AC3"),
    ))
    return MatroskaTrack(number, uid, 2, "A_AC3", b"", "", "und", "", raw)


def test_fine_scale_artifact_is_written_at_one_millisecond(tmp_path: Path) -> None:
    """Artefact à 10 µs (FFmpeg récent) : sortie native à 1 ms, ordre et références conservés."""
    source = tmp_path / "fine.mkv"
    timestamps = [0, 31_990_000, 64_010_000, 96_000_000]
    packets = tuple(
        MatroskaMuxPacket(1, MatroskaBlock(
            1, round(ts / _MS), 0, f"a{i}".encode(), timestamp_ns=ts, duration_ns=31_990_000,
            references_ns=((timestamps[i - 1] - ts),) if i else (),
        ), i)
        for i, ts in enumerate(timestamps)
    )
    MatroskaWriter().write(MatroskaMuxPlan(
        source, (MatroskaMuxTrack(Path("a.mkv"), _audio_track(1, 10), 1, 20),), packets,
        timestamp_scale_ns=10_000, duration_ns=128 * _MS,
    ))
    assert MatroskaReader(source).timestamp_scale_ns() == 10_000

    assembly = MatroskaAssemblyPlan(
        output=tmp_path / "out.mkv",
        ordered_tracks=(MatroskaAssemblyTrack(artifact=source, artifact_track_index=0, source_identity="t"),),
    )
    assembly = replace(assembly, expected_output_contract=assembly_output_contract(assembly))
    plan = compile_assembly_plan(assembly)
    assert plan.timestamp_scale_ns == _MS
    MatroskaWriter().write(plan)

    reader = MatroskaReader(tmp_path / "out.mkv")
    assert reader.timestamp_scale_ns() == _MS
    blocks = list(reader.blocks())
    assert [block.timestamp_ns for block in blocks] == [0, 32 * _MS, 64 * _MS, 96 * _MS]
    assert [block.payload for block in blocks] == [b"a0", b"a1", b"a2", b"a3"]
    # Références vers le bloc précédent : écart entre extrémités quantifiées.
    assert [block.references_ns for block in blocks[1:]] == [(-32 * _MS,), (-32 * _MS,), (-32 * _MS,)]
