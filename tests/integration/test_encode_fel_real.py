"""Validation FEL facultative sur un média local, jamais redistribué avec les tests."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from core.fel.engine import FelEngine
from core.workflows.encode import EncodeConfig, EncodeWorkflow, VideoCropSettings, VideoEncodeSettings
from tests.integration._synth import wait_task

_SOURCE = os.environ.get("MUXIVEO_TEST_FEL_SOURCE", "")
_LIBRARY = os.environ.get("MUXIVEO_TEST_FEL_LIBRARY", "")
pytestmark = pytest.mark.skipif(
    not (_SOURCE and _LIBRARY and all(shutil.which(t) for t in ("ffmpeg", "ffprobe", "dovi_tool"))),
    reason="Média FEL local et plugin requis (MUXIVEO_TEST_FEL_SOURCE/LIBRARY)",
)


def _probe(path: Path, *args: str) -> dict:
    result = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", *args,
        "-of", "json", str(path)], check=True, text=True, capture_output=True)
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def real_excerpt(tmp_path_factory):
    """Coupe au CRA et exclut les RASL précédentes, avec un RPU par image décodable."""
    root = tmp_path_factory.mktemp("real-fel-excerpt")
    source = Path(_SOURCE)
    start = os.environ.get("MUXIVEO_TEST_FEL_START", "480")
    packets = _probe(source, "-read_intervals", f"{start}%+0.3", "-show_packets")["packets"]
    key = next(packet["pts_time"] for packet in packets if "K" in packet.get("flags", ""))
    excerpt = root / "excerpt.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", key, "-i", str(source),
        "-t", "1", "-map", "0:v:0", "-c", "copy", "-bsf:v", "noise=drop=lt(pts\\,0)",
        "-map_chapters", "-1", "-an", "-sn", "-dn", str(excerpt)], check=True, capture_output=True)
    stream = _probe(excerpt, "-count_frames", "-show_streams")["streams"][0]
    record = next(item for item in stream.get("side_data_list", [])
        if item.get("side_data_type") == "DOVI configuration record")
    assert record["dv_profile"] == 7 and record["el_present_flag"] == 1
    return excerpt, stream


def _level5(path: Path, work: Path) -> dict:
    """Zone active L5 de la première image du RPU réellement présent dans le fichier."""
    rpu = work / f"{path.stem}-l5.bin"
    subprocess.run(["dovi_tool", "-m", "0", "extract-rpu", "-i", str(path), "-o", str(rpu)],
        check=True, capture_output=True)
    info = subprocess.run(["dovi_tool", "info", "-i", str(rpu), "-f", "0"], check=True, capture_output=True,
        text=True).stdout

    def find(node):
        if isinstance(node, dict):
            if "Level5" in node:
                return node["Level5"]
            return next((found for value in node.values() if (found := find(value)) is not None), None)
        if isinstance(node, list):
            return next((found for value in node if (found := find(value)) is not None), None)
        return None

    return find(json.loads(info[info.index("{"):])) or {}


@pytest.mark.parametrize("mode", ["x265_dv", "x265_dv_crop", "x265_hdr10", "ffmpeg_nvenc_dv", "nvencc_dv",
                                  "nvencc_dv_crop", "bl_only", "sdr"])
def test_real_fel_encode(qt_app, tmp_path, monkeypatch, real_excerpt, mode):
    source, source_stream = real_excerpt
    if mode.startswith("nvencc") and not shutil.which("nvencc"):
        pytest.skip("NVEncC requis")
    engine = FelEngine(Path(_LIBRARY))
    monkeypatch.setattr(FelEngine, "installed", classmethod(lambda cls: engine))
    workflow = EncodeWorkflow(ffmpeg_threads=12, ram_buffer_enabled=False, generate_nfo=False,
        nvencc_bin=shutil.which("nvencc"))
    master, light = workflow._extract_static_hdr_metadata(source, 0) if mode == "x265_hdr10" else ("", "")
    codec = "hevc_nvenc" if mode.startswith("ffmpeg_nvenc") else "nvencc_hevc" if mode.startswith("nvencc") else "libx265"
    video = VideoEncodeSettings(codec=codec, source_path=source,
        preset="p1" if codec == "hevc_nvenc" else "default" if codec == "nvencc_hevc" else "ultrafast",
        bake_dovi_fel=mode != "bl_only", dovi_source_profile="p7_fel", copy_dv="_dv" in mode,
        bit_depth="8" if mode == "sdr" else "10", tonemap_to_sdr=mode == "sdr",
        crop=VideoCropSettings(enabled=True, top=8, bottom=8) if mode.endswith("_crop") else VideoCropSettings(),
        source_bit_depth=10, source_color_transfer="smpte2084",
        inject_hdr_meta=mode in ("x265_hdr10", "nvencc_dv"), master_display=master, max_cll=light,
        static_hdr_metadata_source="source", static_hdr_light_level_source="source",
        input_frame_rate=source_stream["avg_frame_rate"], crf=24)
    output = tmp_path / f"{mode}.mkv"
    config = EncodeConfig(source=source, output=output, video=video, audio_tracks=[],
        copy_subtitles=False, keep_chapters=False, duration_s=1.05, work_dir=tmp_path)
    messages = []
    workflow.log_message.connect(lambda level, message: messages.append((level, message)))
    started = time.monotonic()
    state = wait_task(workflow.run(config), timeout=180)
    seconds = time.monotonic()-started
    (tmp_path / "workflow.json").write_text(json.dumps({"seconds": seconds, "log": messages,
        "progress": state["progress"], "failed": state["failed"]}, ensure_ascii=False, indent=2))
    assert state["failed"] is None, state["failed"]
    if mode.startswith("nvencc") and "zero" in (engine.capabilities.get("timestamps") or []):
        # Image finale, recadrage compris : NVEncC lit le NUT du plugin, sans étage FFmpeg.
        assert any("alimenté directement" in line for line in state["progress"])
    if mode != "bl_only":
        assert any("FEL confirmé" in message for _, message in messages), messages
        assert any("reconstruction FEL réussie" in message for _, message in messages), messages
        assert not any("repli BL" in message or "reprise complète sur le BL" in message
            for message in [*(m for _, m in messages), *state["progress"]])
    stream = _probe(output, "-count_frames", "-show_streams")["streams"][0]
    assert stream["nb_read_frames"] == source_stream["nb_read_frames"]
    if mode.endswith("_crop"):
        # RPU original réutilisé puis réaligné : zone active décalée du recadrage (saturée à 0).
        assert stream["height"] == source_stream["height"] - 16
        before, after = _level5(source, tmp_path), _level5(output, tmp_path)
        for edge in ("active_area_top_offset", "active_area_bottom_offset"):
            assert after.get(edge) == max(0, int(before.get(edge, 0)) - 8), (before, after)
    assert stream["color_transfer"] == ("bt709" if mode == "sdr" else "smpte2084")
    assert stream["pix_fmt"] == ("yuv420p" if mode == "sdr" else "yuv420p10le")
    if mode != "sdr" and source_stream.get("chroma_location"):
        # Images reconstruites sous-échantillonnées et signalées comme le BL.
        assert stream.get("chroma_location") == source_stream["chroma_location"]
    record = next((item for item in stream.get("side_data_list", [])
        if item.get("side_data_type") == "DOVI configuration record"), None)
    if "_dv" in mode:
        assert record is not None
        assert record["dv_profile"] == 8 and record["dv_bl_signal_compatibility_id"] == 1
        assert record["rpu_present_flag"] == record["bl_present_flag"] == 1 and record["el_present_flag"] == 0
        # Contrôler aussi le RPU réellement injecté, indépendamment du record du conteneur.
        rpu = tmp_path / "encoded-rpu.bin"
        subprocess.run(["dovi_tool", "-m", "0", "extract-rpu", "-i", str(output), "-o", str(rpu)],
            check=True, capture_output=True)
        assert engine.classify(rpu, lambda: False) == "sans_el"
        info = subprocess.run(["dovi_tool", "info", "-i", str(rpu), "-f", "0"],
            check=True, capture_output=True, text=True).stdout
        assert json.loads(info[info.index("{"):])["header"]["disable_residual_flag"] is True
    else:
        assert record is None
    def timestamps(path):
        frames = _probe(path, "-show_frames", "-show_entries", "frame=pts_time")["frames"]
        pts = [float(frame["pts_time"]) for frame in frames]
        return [value-pts[0] for value in pts]
    # Un tick Matroska (1 ms), avec une marge pour la représentation des floats.
    assert timestamps(output) == pytest.approx(timestamps(source), abs=.001+1e-9)
    if mode in ("x265_hdr10", "nvencc_dv"):
        # MaxCLL/MaxFALL mesurés sur l'image reconstruite (plus jamais l'estimation L1), même sans HDR10+.
        assert not any("estimés depuis L1" in message for _, message in messages)
        estimate_message = next(message for _, message in messages
            if "MaxCLL/MaxFALL exacts de l'image reconstruite" in message)
        estimated_light = estimate_message.rsplit(" : ", 1)[1].removesuffix(".")
        frame = _probe(output, "-read_intervals", "%+#1", "-show_frames")["frames"][0]
        side_data = frame.get("side_data_list", [])
        assert any(item.get("side_data_type") == "Mastering display metadata" for item in side_data)
        content = next(item for item in side_data if item.get("side_data_type") == "Content light level metadata")
        assert f"{content['max_content']},{content['max_average']}" == estimated_light
    # Le fallback HDR10 se décode sans lecteur Dolby Vision, sur toutes les images.
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(output), "-map", "0:v:0", "-f", "null", "-"],
        check=True, capture_output=True)
