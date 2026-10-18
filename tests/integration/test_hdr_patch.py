"""Intégration : correction des MaxCLL/MaxFALL d'un MKV HDR10 sans réencodage (HDR Studio).

Source x265 10 bits PQ (SEI MDCV/CLL) avec audio et sous-titres : la sortie porte les nouvelles
valeurs dans le flux (SEI) et le conteneur (Colour), les autres pistes sont recopiées à l'identique.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from core.hdr_inspection.patch import MasteringEdit, MetadataPatcher, PatchPlan, StaticEdit


def _has_libx265() -> bool:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        return False
    encoders = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True,
                              check=False).stdout
    return "libx265" in encoders


pytestmark = pytest.mark.skipif(not _has_libx265(), reason="ffmpeg avec libx265 requis")


def _source(path: Path) -> Path:
    srt = path.with_suffix(".srt")
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nbonjour\n", encoding="utf-8")
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=192x108:rate=24:duration=1",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1",
        "-i", str(srt),
        "-map", "0:v", "-map", "1:a", "-map", "2:s",
        "-c:v", "libx265", "-pix_fmt", "yuv420p10le", "-preset", "ultrafast",
        "-x265-params", "log-level=error:hdr10=1:repeat-headers=1:"
                        "master-display=G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,50):"
                        "max-cll=1000,400:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc",
        "-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc",
        "-c:a", "aac", "-c:s", "srt", str(path),
    ], check=True)
    return path


def _probe(path: Path, *args: str) -> dict:
    result = subprocess.run(["ffprobe", "-v", "error", "-of", "json", *args, str(path)],
                            capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def _first_frame_side_data(path: Path, kind: str) -> dict:
    frames = _probe(path, "-select_streams", "v:0", "-read_intervals", "%+#1", "-show_frames",
                    "-show_entries", "frame=side_data_list")["frames"]
    return next(d for d in frames[0].get("side_data_list") or () if d.get("side_data_type") == kind)


def _first_frame_cll(path: Path) -> tuple[int, int]:
    frames = _probe(path, "-select_streams", "v:0", "-read_intervals", "%+#1", "-show_frames",
                    "-show_entries", "frame=side_data_list")["frames"]
    for data in frames[0].get("side_data_list") or ():
        if data.get("side_data_type") == "Content light level metadata":
            return int(data["max_content"]), int(data["max_average"])
    raise AssertionError("SEI CLL absent")


def _md5(path: Path, selector: str) -> str:
    return subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-map", selector, "-c", "copy", "-f", "md5", "-"],
                          capture_output=True, text=True, check=True).stdout.strip()


def test_static_light_levels_rewritten_without_reencoding(tmp_path):
    source = _source(tmp_path / "source.mkv")
    assert _first_frame_cll(source) == (1000, 400)
    output = tmp_path / "corrige.mkv"
    work = tmp_path / "work"
    work.mkdir()

    def run(cmd):
        subprocess.run(list(cmd), check=True, capture_output=True)

    patcher = MetadataPatcher(ffmpeg_bin="ffmpeg", dovi_tool_bin="dovi_tool", hdr10plus_bin="hdr10plus_tool",
                              run_cmd=run, work_dir=work)
    assert patcher.run(PatchPlan(source, 0, output, static=StaticEdit(640, 120))) == output
    assert _first_frame_cll(output) == (640, 120)
    streams = _probe(output, "-show_streams")["streams"]
    assert [s["codec_type"] for s in streams] == ["video", "audio", "subtitle"]
    container = {d["side_data_type"]: d for d in streams[0].get("side_data_list") or ()}
    assert container["Content light level metadata"]["max_content"] == 640
    assert container["Content light level metadata"]["max_average"] == 120
    assert _md5(output, "0:a:0") == _md5(source, "0:a:0") and _md5(output, "0:s:0") == _md5(source, "0:s:0")
    packets = lambda path: len(_probe(path, "-select_streams", "v:0", "-show_packets",  # noqa: E731
                                      "-show_entries", "packet=pts")["packets"])
    assert packets(output) == packets(source) == 24
    assert not list(work.iterdir()) and not list(tmp_path.glob("*.partial"))


def test_mastering_display_rewritten_without_reencoding(tmp_path):
    source = _source(tmp_path / "source.mkv")
    output = tmp_path / "mdcv.mkv"
    work = tmp_path / "work"
    work.mkdir()

    def run(cmd):
        subprocess.run(list(cmd), check=True, capture_output=True)

    patcher = MetadataPatcher(ffmpeg_bin="ffmpeg", dovi_tool_bin="dovi_tool", hdr10plus_bin="hdr10plus_tool",
                              run_cmd=run, work_dir=work)
    edit = MasteringEdit("BT.2020", 0.005, 4000)
    assert patcher.run(PatchPlan(source, 0, output, static=StaticEdit(mastering=edit))) == output
    frame = _first_frame_side_data(output, "Mastering display metadata")
    assert frame["max_luminance"] == "40000000/10000" and frame["min_luminance"] == "50/10000"
    assert frame["red_x"] == "35400/50000" and frame["green_y"] == "39850/50000"
    assert _first_frame_cll(output) == (1000, 400)  # CLL non demandé : conservé
    streams = _probe(output, "-show_streams")["streams"]
    container = {d["side_data_type"]: d for d in streams[0].get("side_data_list") or ()}
    mastering = container["Mastering display metadata"]
    assert mastering["max_luminance"].split("/")[0] in ("4000", "40000000") and mastering["red_x"].startswith("177")
    source_container = {d["side_data_type"] for d in _probe(source, "-show_streams")["streams"][0].get("side_data_list") or ()}
    assert ("Content light level metadata" in container) == ("Content light level metadata" in source_container)
    assert _md5(output, "0:a:0") == _md5(source, "0:a:0")
    assert not list(work.iterdir())
