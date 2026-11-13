"""Extraction (core.jobs.extract, opération media.extract, commande extract) ; outils simulés."""

from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cli.extract import add_extract_parser, build_request
from core.extractor import TrackExtractor
from core.inspector import AttachmentInfo, AudioTrack, FileInfo, HDRType, SubtitleTrack, VideoTrack
from core.jobs import extract as ex
from core.jobs.errors import JobError
from core.pipeline_command import PipelineCommand
from core.service import ApiError, ExecutionContext, ListSink, call
from core.service.catalog import CATALOG
from core.service.ops import extract as extract_ops


def _video(index: int = 0, codec: str = "hevc", profile: int | None = 7,
           hdr: HDRType = HDRType.DOLBY_VISION_HDR10PLUS) -> VideoTrack:
    return VideoTrack(index=index, codec=codec, codec_long=codec, width=3840, height=2160, frame_rate="24",
                      bit_depth=10, color_space=None, color_primaries=None, color_transfer="smpte2084",
                      color_matrix=None, hdr_type=hdr, dovi_profile=profile, raw={})


def _info(path: Path, video: VideoTrack | None = None, attachments: list[AttachmentInfo] | None = None) -> FileInfo:
    return FileInfo(
        path=path, format="matroska", duration_s=10.0, size_bytes=1, bit_rate=1,
        video_tracks=[video or _video()],
        audio_tracks=[AudioTrack(1, "truehd", "TrueHD", 8, "7.1", 48000, None, "fra", None),
                      AudioTrack(2, "pcm_s24le", "PCM", 2, "stereo", 48000, None, "eng", None)],
        subtitle_tracks=[SubtitleTrack(3, "subrip", "fra", None), SubtitleTrack(4, "dvb_subtitle", "eng", None)],
        attachments=attachments if attachments is not None else [
            AttachmentInfo(5, 0, "police.ttf", "font/ttf"), AttachmentInfo(6, 1, "police.ttf", "font/ttf")],
        hdr_type=HDRType.DOLBY_VISION_HDR10PLUS,
    )


# --- Plans -------------------------------------------------------------------------------------


def test_plans_choose_elementary_formats_and_explicit_muxers(tmp_path):
    info = _info(tmp_path / "film.mkv")
    assert ex.plan_track(info, "v:0").plan.output_format == "hevc"
    truehd = ex.plan_track(info, "a:0")
    assert truehd.default_name == "film.fra.t1.thd" and truehd.plan.output_format == "truehd"
    assert ex.plan_track(info, "2").plan.extension == ".wav"
    assert ex.plan_track(info, "a:0", container=True).plan.extension == ".mka"
    assert ex.plan_track(info, "s:0", container=True).plan.extension == ".mks"
    assert TrackExtractor.plan_subtitle("dvd_subtitle").output_format == "matroska"  # FFmpeg n'écrit pas VobSub
    cmd = ex.stream_command(truehd, ex.ExtractTools("ffmpeg"), info.path, tmp_path / "x.partial")
    assert cmd[cmd.index("-f") + 1] == "truehd" and cmd[cmd.index("-map") + 1] == "0:1"
    hevc = TrackExtractor.build_command("ffmpeg", info.path, 0, "video", TrackExtractor.plan_video("hevc"),
                                        tmp_path / "o")
    assert "hevc_mp4toannexb" in hevc


@pytest.mark.parametrize("selector", ["x:0", "a:9", "12", "v"])
def test_bad_track_selectors_are_refused(tmp_path, selector):
    with pytest.raises(JobError) as caught:
        ex.plan_track(_info(tmp_path / "f.mkv"), selector)
    assert caught.value.exit_code == 2


def test_layer_rules(tmp_path):
    p7 = _info(tmp_path / "f.mkv")
    assert ex.plan_track(p7, "v:0", layer="el").default_name == "f.t0.el.hevc"
    for layer in ("bl", "rpu", "hdr10plus"):
        ex.plan_track(p7, 0, layer=layer)
    p81 = _info(tmp_path / "f.mkv", _video(profile=8, hdr=HDRType.DOLBY_VISION))
    for layer, message in (("el", "profil 7"), ("hdr10plus", "HDR10+")):
        with pytest.raises(JobError, match=message):
            ex.plan_track(p81, 0, layer=layer)
    hdr10 = _info(tmp_path / "f.mkv", _video(profile=None, hdr=HDRType.HDR10))
    with pytest.raises(JobError, match="RPU"):
        ex.plan_track(hdr10, 0, layer="rpu")
    with pytest.raises(JobError, match="HEVC"):
        ex.plan_track(p7, "a:0", layer="rpu")
    with pytest.raises(JobError, match="HEVC"):
        ex.plan_track(_info(tmp_path / "f.mkv", _video(codec="h264")), 0, layer="rpu")


def test_plan_all_skips_ocr_subtitles_and_renames_duplicate_attachments(tmp_path):
    names = [plan.default_name for plan in ex.plan_all(_info(tmp_path / "f.mkv"))]
    assert names == ["f.und.t0.hevc", "f.fra.t1.thd", "f.eng.t2.wav", "f.fra.t3.srt", "police.ttf", "police.1.ttf"]
    attachment = ex.plan_attachment(_info(tmp_path / "f.mkv"), "police.ttf")
    assert attachment.attachment == 0 and attachment.kind == "attachment"
    with pytest.raises(JobError):
        ex.plan_attachment(_info(tmp_path / "f.mkv"), "7")


def test_outputs_refuse_collisions_before_extracting(tmp_path):
    info = _info(tmp_path / "f.mkv")
    outputs = [tmp_path / plan.default_name for plan in ex.plan_all(info)]
    assert ex.skipped_tracks(info, ex.plan_all(info)) == ["#4 (dvb_subtitle)"]
    (tmp_path / "f.fra.t1.thd").write_bytes(b"x")
    with pytest.raises(JobError) as caught:
        ex.check_outputs(outputs, source=info.path, force=False)
    assert caught.value.exit_code == 5
    ex.check_outputs(outputs, source=info.path, force=True)
    for bad, code in (([outputs[0], outputs[0]], 5), ([info.path], 2)):
        with pytest.raises(JobError) as caught:
            ex.check_outputs(bad, source=info.path, force=True)
        assert caught.value.exit_code == code


# --- Couches : lecture directe ou pipe ---------------------------------------------------------


def test_layer_commands_follow_the_hdr_inspection_decision(tmp_path, monkeypatch):
    tools = ex.ExtractTools("ffmpeg", "dovi_tool", "hdr10plus_tool")
    monkeypatch.setattr("core.hdr_inspection.inspector.direct_tool_input", lambda path, index: True)
    mkv = ex.layer_commands("rpu", tools, tmp_path / "f.mkv", 0, tmp_path / "o", tmp_path / "s")
    assert mkv[0][:2] == ["dovi_tool", "extract-rpu"] and isinstance(mkv[1], PipelineCommand)
    raw = ex.layer_commands("hdr10plus", tools, tmp_path / "f.hevc", 0, tmp_path / "o", tmp_path / "s")
    assert len(raw) == 1 and raw[0][:2] == ["hdr10plus_tool", "extract"]
    monkeypatch.setattr("core.hdr_inspection.inspector.direct_tool_input", lambda path, index: False)
    piped = ex.layer_commands("rpu", tools, tmp_path / "f.mkv", 1, tmp_path / "o", tmp_path / "s")
    assert len(piped) == 1 and "0:1" in piped[0].upstream[0]
    el = ex.layer_commands("el", tools, tmp_path / "f.mkv", 0, tmp_path / "o", tmp_path / "s")[0]
    assert el[:3] == ["dovi_tool", "demux", "-"] and "--el-only" in el
    bl = ex.layer_commands("bl", tools, tmp_path / "f.mkv", 0, tmp_path / "o", tmp_path / "s")[0]
    assert bl[bl.index("-b") + 1] == str(tmp_path / "o")


# --- Exécution ---------------------------------------------------------------------------------


def _writer(content: bytes = b"data", fail_first: bool = False):
    calls: list[Any] = []

    def run(cmd: Any, _label: str) -> None:
        calls.append(cmd)
        if fail_first and len(calls) == 1:
            raise JobError("lecteur Matroska refusé", 6)
        target = Path(cmd[cmd.index("-o") + 1]) if "-o" in cmd else Path(cmd[-1])
        target.write_bytes(content)

    return run, calls


def test_extract_publishes_and_falls_back_to_the_pipe(tmp_path, monkeypatch):
    monkeypatch.setattr("core.hdr_inspection.inspector.direct_tool_input", lambda path, index: True)
    info = _info(tmp_path / "f.mkv")
    run, calls = _writer(fail_first=True)
    out = ex.extract(info, ex.plan_track(info, 0, layer="rpu"), tmp_path / "f.rpu.bin",
                     tools=ex.ExtractTools("ffmpeg"), run=run)
    assert out.read_bytes() == b"data" and len(calls) == 2 and isinstance(calls[1], PipelineCommand)
    assert not list(tmp_path.glob("*.partial"))


def test_extract_failures_leave_nothing_behind(tmp_path):
    info = _info(tmp_path / "f.mkv")
    plan = ex.plan_track(info, "a:0")
    empty, _calls = _writer(b"")
    with pytest.raises(JobError, match="vide"):
        ex.extract(info, plan, tmp_path / "a.thd", tools=ex.ExtractTools("ffmpeg"), run=empty)
    (tmp_path / "a.thd").write_bytes(b"old")
    run, _calls = _writer()
    with pytest.raises(JobError) as caught:
        ex.extract(info, plan, tmp_path / "a.thd", tools=ex.ExtractTools("ffmpeg"), run=run)
    assert caught.value.exit_code == 5 and (tmp_path / "a.thd").read_bytes() == b"old"
    ex.extract(info, plan, tmp_path / "a.thd", tools=ex.ExtractTools("ffmpeg"), run=run, force=True)
    assert (tmp_path / "a.thd").read_bytes() == b"data"
    with pytest.raises(JobError, match="source"):
        ex.extract(info, plan, info.path, tools=ex.ExtractTools("ffmpeg"), run=run, force=True)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.thd"]


def test_extract_writes_matroska_attachments(tmp_path):
    info = _info(tmp_path / "f.mkv")
    out = ex.extract(info, ex.plan_attachment(info, "1"), tmp_path / "p.ttf", tools=ex.ExtractTools("ffmpeg"),
                     run=lambda *a: None, attachment_reader=lambda path, index: f"att{index}".encode())
    assert out.read_bytes() == b"att1"


# --- Opération et CLI --------------------------------------------------------------------------


def _ctx(tmp_path: Path, sink: ListSink | None = None) -> ExecutionContext:
    return ExecutionContext.create(SimpleNamespace(tool_ffmpeg="ffmpeg", tool_ffprobe="ffprobe",
                                                   tool_mediainfo="mediainfo"), sink or ListSink())


def test_operation_dry_run_and_request_rules(tmp_path, monkeypatch):
    info = _info(tmp_path / "f.mkv")
    monkeypatch.setattr(extract_ops.FileInspector, "inspect", lambda self, path: info)
    result: Any = call(CATALOG, "media.extract", {"source": str(info.path), "all": True, "dry_run": True,
                                                  "output_dir": str(tmp_path / "out")}, _ctx(tmp_path))
    assert result.dry_run and result.outputs[-1]["path"] == str(tmp_path / "out" / "police.1.ttf")
    for payload in ({"source": str(info.path)},
                    {"source": str(info.path), "all": True, "items": [{"track": "0"}]},
                    {"source": str(info.path), "items": [{"track": "0", "attachment": "0"}]},
                    {"source": str(info.path), "items": [{"attachment": "0", "layer": "rpu"}]}):
        with pytest.raises(ApiError) as caught:
            call(CATALOG, "media.extract", payload, _ctx(tmp_path))
        assert caught.value.code.value == "invalid_request"
    with pytest.raises(ApiError, match="deux éléments"):
        call(CATALOG, "media.extract", {"source": str(info.path), "dry_run": True, "items": [
            {"track": "0", "output": str(tmp_path / "x")}, {"track": "1", "output": str(tmp_path / "x")}]},
             _ctx(tmp_path))


def test_operation_runs_each_item_and_reports_artifacts(tmp_path, monkeypatch):
    info = _info(tmp_path / "f.mkv")
    monkeypatch.setattr(extract_ops.FileInspector, "inspect", lambda self, path: info)
    monkeypatch.setattr(extract_ops, "required_tools", lambda plans, tools: None)
    run, calls = _writer()
    monkeypatch.setattr(extract_ops, "tool_runner", lambda cancel, on_pct=None: run)
    sink = ListSink()
    call(CATALOG, "media.extract", {"source": str(info.path), "items": [{"track": "a:0"}, {"track": "s:0"}]},
         _ctx(tmp_path, sink))
    assert len(calls) == 2 and (tmp_path / "f.fra.t1.thd").exists() and (tmp_path / "f.fra.t3.srt").exists()
    artifacts = [e.to_dict() for e in sink.events if e.type.value == "artifact"]
    assert [a["path"] for a in artifacts] == [str(tmp_path / "f.fra.t1.thd"), str(tmp_path / "f.fra.t3.srt")]


def _args(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    add_extract_parser(parser.add_subparsers())
    return parser.parse_args(["extract", "-i", "f.mkv", *argv])


def test_cli_layers_follow_their_track():
    request = build_request(_args("--track", "v:0", "--layer", "el", "--track", "a:0", "--track", "v:0",
                                  "--layer", "rpu", "--attachment", "cover.jpg"))
    assert request["items"] == [{"track": "v:0", "layer": "el"}, {"track": "a:0", "layer": "stream"},
                                {"track": "v:0", "layer": "rpu"}, {"attachment": "cover.jpg"}]
    assert build_request(_args("--track", "1", "-o", "x.thd"))["items"][0]["output"] == "x.thd"
    for argv in (("--all", "--track", "1"), (), ("--track", "1", "--track", "2", "-o", "x")):
        with pytest.raises(JobError):
            build_request(_args(*argv))
    with pytest.raises(SystemExit):
        _args("--layer", "rpu")
