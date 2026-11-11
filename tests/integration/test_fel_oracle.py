"""Rendu FEL attendu d'un lecteur Dolby Vision : oracle numpy indépendant du plugin, puis sortie finale.

Média local (jamais redistribué) : MUXIVEO_TEST_FEL_SOURCE (MKV profil 7 FEL), MUXIVEO_TEST_FEL_LIBRARY
(bibliothèque mvo-fel), MUXIVEO_TEST_FEL_START (instant de l'extrait, 480 s par défaut).

1. Composition du plugin (couches « mapped » et reconstruite) face au compositeur de référence
   ``tests/fel_composer_reference.py`` appliqué à la BL et à l'EL décodées par FFmpeg avec le RPU de l'image.
2. Sortie finale (x265, FEL intégré, copie DV) face à la reconstruction du plugin : même image, écart
   d'encodage seul, RPU P8.1 neutre (mapping identité, sans résiduel) d'autant de trames que la vidéo.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from core.fel.engine import CaptureSession, FelEngine
from core.workflows.encode import EncodeConfig, EncodeWorkflow, QualityMode, VideoEncodeSettings
from tests import fel_composer_reference as reference
from tests.integration._synth import wait_task

_SOURCE = os.environ.get("MUXIVEO_TEST_FEL_SOURCE", "")
_LIBRARY = os.environ.get("MUXIVEO_TEST_FEL_LIBRARY", "")
pytestmark = pytest.mark.skipif(
    not (_SOURCE and _LIBRARY and all(shutil.which(t) for t in ("ffmpeg", "ffprobe", "dovi_tool"))),
    reason="Média FEL local et plugin requis (MUXIVEO_TEST_FEL_SOURCE/LIBRARY)",
)


def _probe(path: Path, *args: str) -> dict:
    result = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", *args, "-of", "json", str(path)],
                            check=True, text=True, capture_output=True)
    return json.loads(result.stdout)


def _planes(raw: bytes, width: int, height: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    data = np.frombuffer(raw, "<u2").astype(np.float64)
    chroma = width * height // 4
    return (data[:width * height].reshape(height, width),
            data[width * height:width * height + chroma].reshape(height // 2, width // 2),
            data[width * height + chroma:width * height + 2 * chroma].reshape(height // 2, width // 2))


def _decode(path: Path, frames: int, *, fmt: str | None = None) -> list[tuple[np.ndarray, np.ndarray, np.ndarray]]:
    stream = _probe(path, "-show_streams")["streams"][0] if fmt is None else None
    cmd = ["ffmpeg", "-v", "error", *(["-f", fmt] if fmt else []), "-i", str(path), "-map", "0:v:0",
           "-frames:v", str(frames), "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "yuv420p10le", "-"]
    raw = subprocess.run(cmd, check=True, capture_output=True).stdout
    if stream is None:
        stream = _probe(path, "-show_streams")["streams"][0]
    width, height = int(stream["width"]), int(stream["height"])
    size = width * height * 3
    return [_planes(raw[i * size:(i + 1) * size], width, height) for i in range(len(raw) // size)]


@pytest.fixture(scope="module")
def excerpt(tmp_path_factory):
    """Extrait copié depuis une image clé, première image = image clé (trames décodables)."""
    root = tmp_path_factory.mktemp("fel-oracle")
    source = Path(_SOURCE)
    start = os.environ.get("MUXIVEO_TEST_FEL_START", "480")
    packets = _probe(source, "-read_intervals", f"{start}%+1.2", "-show_packets")["packets"]
    key = next(packet["pts_time"] for packet in packets if "K" in packet.get("flags", ""))
    path = root / "excerpt.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", key, "-i", str(source), "-t", "2", "-map", "0:v:0", "-c",
                    "copy", "-bsf:v", "noise=drop=lt(pts\\,0)", "-map_metadata", "-1", "-map_chapters", "-1",
                    str(path)], check=True, capture_output=True)
    stream = _probe(path, "-show_streams")["streams"][0]
    record = next(item for item in stream.get("side_data_list", [])
                  if item.get("side_data_type") == "DOVI configuration record")
    assert record["dv_profile"] == 7 and record["el_present_flag"] == 1
    return path, stream


def _capture(path: Path, stream: dict, count: int):
    engine = FelEngine(Path(_LIBRARY))
    session = CaptureSession(engine, path, 0, frame_rate=str(stream["avg_frame_rate"]),
                             chroma_location=str(stream.get("chroma_location") or "topleft"), precision="exact")
    try:
        buffer = session.capture_pair(0.0, count)
        size = session.frame_size
    finally:
        session.close()
    width, height = int(stream["width"]), int(stream["height"])
    frames = [_planes(buffer[i * size:(i + 1) * size].tobytes(), width, height) for i in range(2 * count)]
    return frames[0::2], frames[1::2]  # reconstruites, mapped


def _rpu_frame(rpu: Path, index: int) -> dict:
    text = subprocess.run(["dovi_tool", "info", "-i", str(rpu), "-f", str(index)], check=True, text=True,
                          capture_output=True).stdout
    return json.loads(text[text.index("{"):])


def test_plugin_composition_matches_independent_reference(excerpt, tmp_path):
    path, stream = excerpt
    reconstructed, mapped = _capture(path, stream, 1)
    bl = _decode(path, 1)[0]
    hevc = tmp_path / "full.hevc"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(path), "-map", "0:v:0", "-c", "copy", "-bsf:v",
                    "hevc_mp4toannexb", "-f", "hevc", str(hevc)], check=True, capture_output=True)
    subprocess.run(["dovi_tool", "demux", "-i", str(hevc), "--bl-out", str(tmp_path / "bl.hevc"), "--el-out",
                    str(tmp_path / "el.hevc")], check=True, capture_output=True)
    el = _decode(tmp_path / "el.hevc", 1, fmt="hevc")[0]
    subprocess.run(["dovi_tool", "extract-rpu", "-i", str(hevc), "-o", str(tmp_path / "rpu.bin")], check=True,
                   capture_output=True)
    rpu = _rpu_frame(tmp_path / "rpu.bin", 0)

    expected = reference.mapped(bl, rpu)
    for name, mine, plugin in zip("YUV", expected, mapped[0]):
        diff = mine - plugin
        # Convention de normalisation (1023 / 1024) et luminance au site chroma : un demi-code au plus en moyenne.
        assert abs(diff.mean()) <= 0.75, f"{name} : courbes du RPU, écart moyen {diff.mean():+.2f}"
        # Bords : la luminance moyenne 2 × 2 du site chroma (référence) amplifiée par le MMR.
        assert np.percentile(np.abs(diff), 99) <= 6, f"{name} : courbes du RPU, écart p99"

    added = reference.residual(el, rpu, bl[0].shape)
    for name, mine, recon, base in zip("YUV", added, reconstructed[0], mapped[0]):
        plugin = recon - base
        assert abs(mine.mean() - plugin.mean()) <= 0.3, (
            f"{name} : résiduel moyen {mine.mean():+.2f} (référence) contre {plugin.mean():+.2f} (plugin)")
        if np.std(plugin) > 0.5:
            # Suréchantillonnage EL au plus proche (référence) contre filtre de l'EL : structure, pas codes.
            assert np.corrcoef(mine.ravel(), plugin.ravel())[0, 1] >= 0.75, f"{name} : structure du résiduel"


def test_final_output_is_the_reconstruction(qt_app, excerpt, tmp_path, monkeypatch):
    path, stream = excerpt
    engine = FelEngine(Path(_LIBRARY))
    monkeypatch.setattr(FelEngine, "installed", classmethod(lambda cls: engine))
    workflow = EncodeWorkflow(ram_buffer_enabled=False, generate_nfo=False)
    video = VideoEncodeSettings(
        source_path=path, codec="libx265", preset="ultrafast", quality_mode=QualityMode.CRF, crf=8,
        bake_dovi_fel=True, dovi_source_profile="p7_fel", copy_dv=True, bit_depth="auto", source_bit_depth=10,
        source_color_transfer="smpte2084", input_frame_rate=str(stream["avg_frame_rate"]))
    output = tmp_path / "fel.mkv"
    config = EncodeConfig(source=path, output=output, video=video, audio_tracks=[], copy_subtitles=False,
                          keep_chapters=False, work_dir=tmp_path / "work")
    state = wait_task(workflow.run(config), timeout=1800)
    assert state["failed"] is None, state["failed"]

    count = 3
    reconstructed, _mapped = _capture(path, stream, count)
    decoded = _decode(output, count + 1)
    for index, recon in enumerate(reconstructed):
        scores = {}
        for shift in (-1, 0, 1):
            if 0 <= index + shift < len(decoded):
                error = np.mean((decoded[index + shift][0] - recon[0]) ** 2)
                scores[shift] = 10 * np.log10(1023 ** 2 / max(error, 1e-9))
        assert max(scores, key=lambda key: scores[key]) == 0, f"image {index} : décalage {scores}"
        assert scores[0] >= 40, f"image {index} : PSNR-Y {scores[0]:.1f} dB"
        assert abs((decoded[index][0] - recon[0]).mean()) <= 0.3

    frames = int(_probe(output, "-count_packets", "-show_entries", "stream=nb_read_packets")["streams"][0][
        "nb_read_packets"])
    subprocess.run(["dovi_tool", "extract-rpu", "-i", str(output), "-o", str(tmp_path / "out.bin")], check=True,
                   capture_output=True)
    summary = subprocess.run(["dovi_tool", "info", "-i", str(tmp_path / "out.bin"), "--summary"], check=True,
                             text=True, capture_output=True).stdout
    assert f"Frames: {frames}" in summary and "Profile: 8" in summary
    first = _rpu_frame(tmp_path / "out.bin", 0)
    assert first["header"]["disable_residual_flag"] is True and first["rpu_data_mapping"].get("nlq") is None
    for curve in first["rpu_data_mapping"]["curves"]:
        assert curve["mapping_idc"] == "Polynomial" and curve["poly_coef_int"] == [[0, 1]]
        assert curve["poly_coef"] == [[0, 0]]
