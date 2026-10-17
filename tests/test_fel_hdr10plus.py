"""HDR10+ et MaxCLL/MaxFALL recalculés sur l'image reconstruite (statistiques du plugin mvo-fel)."""
import json
import threading
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from core.fel.engine import FelError, FelSource
from core.fel.hdr10plus import (
    FrameStatisticsError, deferred_light_levels, fel_statistics_path, hdr10plus_document,
    read_frame_statistics,
)
from core.fel.preparation import prepare_fel
from core.workflows.encode.models import EncodePreset, VideoEncodeSettings, fel_hdr10plus_choice


def write_statistics(path: Path, frames: list[dict], scenes: list[tuple[int, int, list[list[int]]]]) -> Path:
    lines = [{"format": "mvo-fel-frame-statistics", "version": 1, "bins": 1024, "width": 4, "height": 2}]
    for index, frame in enumerate(frames):
        lines.append({"frame": index, **frame})
    for first, count, hist in scenes:
        lines.append({"scene_first": first, "frames": count, "maxrgb_hist": hist})
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    return path


def frame(scene_start=False, max_nits=(100.0, 80.0, 60.0), avg=10.0, y9999=90.0, y100=0.5,
          percentiles=(0.0, 1.0, 2.0, 4.0, 8.0, 16.0, 64.0)):
    return {"scene_start": scene_start, "max_nits": list(max_nits), "avg_nits": avg, "y9999_nits": y9999, "y100": y100,
            "maxrgb_percentiles": list(percentiles)}


def test_document_per_scene_follows_the_hdr10plus_conventions(tmp_path):
    # Plan 0 : deux images aux percentiles distincts ; plan 1 : une image.
    frames = [frame(True, (1000.0, 200.0, 50.0), avg=20.0, y9999=300.0, y100=0.75,
                    percentiles=(0.0, 1.0, 2.0, 4.0, 10.0, 20.0, 400.0)),
              frame(False, (400.0, 600.0, 70.0), avg=40.0, y9999=500.0, y100=0.25,
                    percentiles=(0.2, 3.0, 6.0, 8.0, 30.0, 40.0, 900.0)),
              frame(True, (5.0, 4.0, 3.0), avg=2.0, y9999=4.0, y100=1.0)]
    path = write_statistics(tmp_path / "s.jsonl", frames, [(0, 2, [[100, 12], [500, 4]]), (2, 1, [[50, 8]])])
    statistics = read_frame_statistics(path)
    document = hdr10plus_document(statistics)
    assert document["JSONInfo"]["HDR10plusProfile"] == "A"
    assert document["SceneInfoSummary"] == {"SceneFirstFrameIndex": [0, 2], "SceneFrameNumbers": [2, 1]}
    entries = document["SceneInfo"]
    assert [(e["SceneId"], e["SceneFrameIndex"], e["SequenceFrameIndex"]) for e in entries] == [(0, 0, 0), (0, 1, 1), (1, 0, 2)]
    first = entries[0]["LuminanceParameters"]
    assert first["MaxScl"] == [10000, 6000, 700]               # maximum par canal, 0,1 nit
    assert first["AverageRGB"] == 300                           # moyenne des images
    values = dict(zip(first["LuminanceDistributions"]["DistributionIndex"],
                      first["LuminanceDistributions"]["DistributionValues"]))
    # 1 à 95 % : moyenne des percentiles des images ; 99,98 % : leur maximum.
    assert [values[k] for k in (1, 25, 50, 75, 90, 95)] == [1, 20, 40, 60, 200, 300]
    assert values[99] == 9000
    assert values[5] == 5000                                    # DistributionY99 : maximum des images
    assert values[10] == 50                                     # DistributionY100nit : moyenne, en %
    assert "BezierCurveData" not in entries[0] and entries[0]["TargetedSystemDisplayMaximumLuminance"] == 0
    assert entries[0]["LuminanceParameters"] is entries[1]["LuminanceParameters"]
    assert statistics.static_light_levels() == "1000,40"


@pytest.mark.parametrize("scenes,message", [
    ([(0, 1, [])], "en plans"),
    ([(0, 1, []), (2, 1, [])], "discontinus"),
])
def test_incomplete_statistics_are_refused(tmp_path, scenes, message):
    path = write_statistics(tmp_path / "s.jsonl", [frame(True), frame()], scenes)
    with pytest.raises(FrameStatisticsError, match=message):
        read_frame_statistics(path)


def test_unknown_or_missing_file_is_refused(tmp_path):
    with pytest.raises(FrameStatisticsError, match="illisibles"):
        read_frame_statistics(tmp_path / "absent.jsonl")
    (tmp_path / "x.jsonl").write_text('{"format":"autre","version":1,"bins":1024}\n', encoding="utf-8")
    with pytest.raises(FrameStatisticsError, match="format"):
        read_frame_statistics(tmp_path / "x.jsonl")


def test_choice_values_and_preset_roundtrip():
    assert fel_hdr10plus_choice("source") == "source"
    assert fel_hdr10plus_choice("autre") == fel_hdr10plus_choice(None) == "recompute"
    preset = EncodePreset(name="p", fel_hdr10plus="source")
    assert EncodePreset(**preset.to_json_dict()).to_video_settings().fel_hdr10plus == "source"
    assert VideoEncodeSettings().fel_hdr10plus == "recompute"


def prepared(tmp_path, monkeypatch, *, capabilities, **settings):
    import core.fel.preparation as preparation
    monkeypatch.setattr(preparation, "extract_dovi_rpu", Mock())
    monkeypatch.setattr(preparation, "estimate_static_hdr_from_rpu", Mock(return_value=SimpleNamespace(max_cll="2000,500")))
    engine = Mock()
    engine.classify.return_value = "fel"
    engine.devices.return_value = ()
    engine.capabilities = capabilities
    log = Mock()
    video = VideoEncodeSettings(**{"bake_dovi_fel": True, "codec": "libx265", "copy_hdr10plus": True,
                                   "inject_hdr_meta": True, **settings})
    result = prepare_fel(video, source=tmp_path / "original.mkv", work_dir=tmp_path, index=1,
                         ffmpeg="ffmpeg", dovi="dovi_tool", threads=2, run=Mock(), capture=Mock(return_value="{}"),
                         cancelled=lambda: False, log=log, engine_loader=lambda: engine)
    return result, " ".join(str(call.args[1]) for call in log.call_args_list)


def test_recompute_defers_light_levels_and_requests_statistics(tmp_path, monkeypatch):
    result, journal = prepared(tmp_path, monkeypatch, capabilities={"frame_statistics": 1})
    assert fel_statistics_path(result) == tmp_path / "fel_statistics_1.jsonl"
    assert result.fel_context.source.statistics_path == tmp_path / "fel_statistics_1.jsonl"
    assert result.max_cll == "" and deferred_light_levels(result)
    assert "recalculés" in journal and "estimés depuis L1" not in journal


def test_manual_light_levels_are_kept_while_hdr10plus_is_recomputed(tmp_path, monkeypatch):
    result, _ = prepared(tmp_path, monkeypatch, capabilities={"frame_statistics": 1},
                         max_cll="900,100", static_hdr_light_level_source="manual")
    assert fel_statistics_path(result) is not None
    assert result.max_cll == "900,100" and not deferred_light_levels(result)


@pytest.mark.parametrize("capabilities,settings", [
    ({}, {}),                                                   # extension antérieure à 0.3.0
    ({"frame_statistics": 1}, {"fel_hdr10plus": "source"}),     # choix « conservé »
    ({"frame_statistics": 1}, {"codec": "libsvtav1"}),          # hors HEVC (hdr10plus_tool)
])
def test_source_hdr10plus_is_kept_otherwise(tmp_path, monkeypatch, capabilities, settings):
    result, journal = prepared(tmp_path, monkeypatch, capabilities=capabilities, **settings)
    assert fel_statistics_path(result) is None and result.max_cll == "2000,500"
    assert "décrit la BL" in journal
    assert ("antérieure à 0.3.0" in journal) == (capabilities == {})


def test_producer_sends_the_statistics_path_or_refuses(tmp_path):
    lib = Mock()
    lib.mvo_fel_create.return_value = 7
    lib.mvo_fel_set_frame_statistics.return_value = 0
    path = tmp_path / "stats.jsonl"
    source = FelSource(SimpleNamespace(lib=lib, capabilities={"frame_statistics": 1}), tmp_path / "s.mkv", 0,
                       statistics_path=path)
    with patch("core.fel.engine.threading.Thread"):
        source.start(Mock(), threading.Event())
    lib.mvo_fel_set_frame_statistics.assert_called_once_with(7, str(path).encode("utf-8"))
    old = FelSource(SimpleNamespace(lib=lib, capabilities={}), tmp_path / "s.mkv", 0, statistics_path=path)
    with pytest.raises(FelError, match="Statistiques"):
        old.start(Mock(), threading.Event())


def test_direct_transport_is_not_used_with_statistics(tmp_path, monkeypatch):
    from core.fel import direct
    from core.fel.context import FelExecution
    from core.pipeline_command import PipelineCommand
    binary = tmp_path / "ffmpeg-fel"
    binary.write_text("")
    monkeypatch.setattr(direct.sys, "platform", "linux")
    engine = SimpleNamespace(direct_ffmpeg=binary, capabilities={"direct_formats": ["p010"]})
    source = FelSource(engine, tmp_path / "s.mkv", 0, device_plan=Mock(), statistics_path=tmp_path / "s.jsonl")
    video = VideoEncodeSettings(codec="hevc_nvenc", bit_depth="10",
                                fel_context=FelExecution(source, tmp_path / "rpu.bin", "", ""))
    fallback = PipelineCommand(["ffmpeg", "-i", "pipe:0"], [], producer=source)
    assert direct.direct_command(fallback, ["ffmpeg", "-i", "src", "-c:v", "hevc_nvenc"], video, None) is fallback


def test_light_levels_replace_or_create_the_colour_values():
    from core.matroska.ebml import element, uint_element
    from core.matroska.hevc.payload_rewriter import _with_light_levels
    from core.matroska.ids import COLOUR_ID, MAX_CLL_ID, MAX_FALL_ID
    from core.matroska.reader import payload_children
    pixel_width = bytes.fromhex("b0")
    existing = element(pixel_width, b"\x0f\x00") + element(
        COLOUR_ID, uint_element(bytes.fromhex("55b9"), 1) + uint_element(MAX_CLL_ID, 1000) + uint_element(MAX_FALL_ID, 400))
    for video in (existing, element(pixel_width, b"\x0f\x00")):
        children = dict(payload_children(_with_light_levels(video, (1648, 77))))
        colour = dict(payload_children(children[COLOUR_ID]))
        assert int.from_bytes(colour[MAX_CLL_ID], "big") == 1648 and int.from_bytes(colour[MAX_FALL_ID], "big") == 77
        assert children[pixel_width] == b"\x0f\x00"
    assert bytes.fromhex("55b9") in dict(payload_children(dict(payload_children(_with_light_levels(existing, (1, 2))))[COLOUR_ID]))


def run_nvencc_step(tmp_path, monkeypatch, *, encoded_frames, deferred=True, hdr10plus=True):
    import core.workflows.encode.runtime.nvencc_execution as execution
    statistics = write_statistics(tmp_path / "s.jsonl", [frame(True, (1500.0, 900.0, 300.0), avg=70.0)] * 2,
                                  [(0, 2, [[600, 16]])])
    intermediate = tmp_path / "nvencc.mkv"
    intermediate.write_bytes(b"mkv")
    commands: list[list[str]] = []

    def run(cmd, _label):
        commands.append(cmd)
        Path(cmd[-1]).write_bytes(b"hevc")
    rewrite = Mock(side_effect=lambda **kw: kw["output"].write_bytes(b"new"))
    monkeypatch.setattr(execution.MatroskaHevcPayloadRewriter, "rewrite", lambda self, **kw: rewrite(**kw))
    sei = Mock(side_effect=lambda src, dst, **kw: (dst.write_bytes(b"cll"), SimpleNamespace(applied=True))[1])
    monkeypatch.setattr(execution, "replace_content_light_level_sei_file", sei)
    video = VideoEncodeSettings(inject_hdr_meta=True, copy_hdr10plus=hdr10plus,
                                static_hdr_light_level_source="fel_frame_statistics" if deferred else "manual")
    log = Mock()
    execution._apply_fel_hdr10plus(
        video=video, intermediate=intermediate, statistics_path=statistics, cwd=tmp_path, frame_ratio=Fraction(1),
        encoded_frames=encoded_frames, ffmpeg_bin="ffmpeg", hdr10plus_bin="hdr10plus_tool", dovi_bin="dovi_tool",
        run=run, cancelled=lambda: False, log=log, progress=Mock(),
    )
    return commands, rewrite, sei, intermediate


def test_nvencc_output_receives_the_recomputed_metadata(tmp_path, monkeypatch):
    commands, rewrite, sei, intermediate = run_nvencc_step(tmp_path, monkeypatch, encoded_frames=2)
    assert commands[0][:3] == ["ffmpeg", "-nostdin", "-y"] and "hevc_mp4toannexb" in commands[0]
    assert commands[1][:2] == ["hdr10plus_tool", "inject"] and "-j" in commands[1]
    assert sei.call_args.kwargs == {"max_cll": "1500,70"}
    assert rewrite.call_args.kwargs["light_levels"] == (1500, 70)
    assert rewrite.call_args.kwargs["encoded_mkv"] == intermediate and intermediate.read_bytes() == b"new"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["nvencc.mkv", "s.jsonl"]   # intermédiaires retirés


def test_nvencc_manual_light_levels_are_not_replaced(tmp_path, monkeypatch):
    _, rewrite, sei, _ = run_nvencc_step(tmp_path, monkeypatch, encoded_frames=None, deferred=False)
    sei.assert_not_called()
    assert rewrite.call_args.kwargs["light_levels"] is None


def test_nvencc_exact_light_levels_without_hdr10plus(tmp_path, monkeypatch):
    """Copie HDR10+ abandonnée (source sans HDR10+) : MaxCLL/MaxFALL différés tout de même insérés."""
    commands, rewrite, sei, _ = run_nvencc_step(tmp_path, monkeypatch, encoded_frames=2, hdr10plus=False)
    assert len(commands) == 1 and "hevc_mp4toannexb" in commands[0]
    assert sei.call_args.kwargs == {"max_cll": "1500,70"} and rewrite.call_args.kwargs["light_levels"] == (1500, 70)


def test_nvencc_frame_count_mismatch_is_refused(tmp_path, monkeypatch):
    from core.workflows.encode.models import EncodeError
    with pytest.raises(EncodeError, match="2 trames pour 3"):
        run_nvencc_step(tmp_path, monkeypatch, encoded_frames=3)
    assert (tmp_path / "nvencc.mkv").read_bytes() == b"mkv"


def test_content_light_level_sei_is_replaced_and_mastering_kept(tmp_path):
    """SEI x265 (MDCV + CLL 0,0 dans un même NAL) : CLL exact à la place, MDCV conservé, autres AU intactes."""
    from core.workflows.hevc_static_hdr_metadata import (
        _build_static_hdr_sei_nal, _sei_messages, replace_content_light_level_sei_file,
    )
    mdcv_cll, _ = _build_static_hdr_sei_nal(
        master_display="G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,50)", max_cll="0,0")
    start = b"\x00\x00\x00\x01"
    idr = bytes([19 << 1, 1, 0x80, 0x11])            # tranche IDR, premier segment
    trail = bytes([1 << 1, 1, 0x80, 0x22])            # tranche TRAIL_R
    stream = start + mdcv_cll + start + idr + start + trail
    source, output = tmp_path / "in.hevc", tmp_path / "out.hevc"
    source.write_bytes(stream)
    result = replace_content_light_level_sei_file(source, output, max_cll="2252,27")
    assert result.applied and result.injected_access_units == 1
    nals = [n for n in output.read_bytes().split(start) if n]
    seis = [_sei_messages(n) for n in nals if (n[0] >> 1) & 0x3F == 39]
    types = sorted(t for messages in seis for t, _ in messages)
    assert types == [137, 144]                        # un seul CLL, MDCV conservé
    cll = next(p for messages in seis for t, p in messages if t == 144)
    assert int.from_bytes(cll[:2], "big") == 2252 and int.from_bytes(cll[2:4], "big") == 27
    assert nals[-2:] == [idr, trail]
