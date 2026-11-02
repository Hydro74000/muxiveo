"""Intégration : Restauration (réseau R2 de muxiveo-rife ``--restore``) dans EncodeWorkflow.

Source synthétique granuleuse ; vérifie le nombre d'images, la profondeur de sortie (Auto → 10 bits, 8 bits imposé),
le cumul avec l'interpolation et le retrait du grain. Nécessite un binaire ``muxiveo-rife`` avec l'étage de
Restauration et ses graphes (``MUXIVEO_RIFE_BIN`` ou PATH : ``<exe>/restore-models/r2``) et un périphérique Vulkan.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from core.workflows.encode import (
    AudioTrackSettings,
    EncodeConfig,
    EncodeWorkflow,
    FrameInterpolationSettings,
    QualityMode,
    RestoreSettings,
    VideoEncodeSettings,
)
from core.workflows.encode.interpolation import RifeCapabilities, rife_capabilities
from tests.integration._synth import ffprobe_json, streams_of_type, wait_task


def _rife_bin() -> str | None:
    candidate = os.environ.get("MUXIVEO_RIFE_BIN") or shutil.which("muxiveo-rife")
    if not candidate or not Path(candidate).is_file():
        return None
    capabilities = rife_capabilities(candidate)
    if capabilities is None or not capabilities.supports("restore") or "r2" not in capabilities.restore_models:
        return None
    try:
        probe = subprocess.run([candidate, "--list-gpus"], capture_output=True, text=True, timeout=60, check=False)
        gpus = json.loads(probe.stdout or "{}").get("gpus") or []
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    return candidate if gpus else None


RIFE_BIN = _rife_bin()
_NO_CAPS = RifeCapabilities((0, 0, 0))
W, H, FPS, DURATION = 320, 192, 25, 1.2

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None or RIFE_BIN is None,
    reason="ffmpeg/ffprobe, muxiveo-rife avec la Restauration (graphes r2) et un périphérique Vulkan requis",
)


@pytest.fixture(autouse=True)
def _qt_app(qt_app):
    return qt_app


def _grainy_source(path: Path) -> None:
    """Dégradé qui défile + grain temporel (FFV1 sans perte : le grain reste intact dans la source)."""
    subprocess.run([
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"gradients=size={W}x{H}:rate={FPS}:speed=0.02:seed=3,noise=alls=24:allf=t",
        "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000",
        "-t", str(DURATION), "-c:v", "ffv1", "-pix_fmt", "yuv420p", "-c:a", "flac", "-shortest", str(path),
    ], check=True, capture_output=True, timeout=60)


def _frames(path: Path) -> int:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets", "-show_entries",
                          "stream=nb_read_packets", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True)
    return int(out.stdout.strip().split(",")[0])


def _luma(path: Path) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v:0", "-f", "rawvideo",
                          "-pix_fmt", "gray16le", "-"], capture_output=True, check=True, timeout=60).stdout
    return np.frombuffer(raw, "<u2").reshape(-1, H, W).astype(np.float64)


def _grain(frames: np.ndarray, reference: np.ndarray) -> float:
    """Écart type du résidu face à la moyenne temporelle de la source (grain restant)."""
    return float(np.std(frames[2:-2] - reference.mean(0)))


def _encode(tmp_path: Path, src: Path, name: str, **video) -> tuple[Path, dict]:
    out = tmp_path / name
    cfg = EncodeConfig(
        source=src,
        output=out,
        video=VideoEncodeSettings(codec="libx265", quality_mode=QualityMode.CRF, crf=12, preset="ultrafast", **video),
        audio_tracks=[AudioTrackSettings(stream_index=1, codec="copy")],
        copy_subtitles=False,
        keep_chapters=False,
        duration_s=DURATION,
    )
    wf = EncodeWorkflow(ffmpeg_bin="ffmpeg", ram_buffer_enabled=False, ffmpeg_threads=2, generate_nfo=False,
                        rife_bin=RIFE_BIN)
    assert wf.validate(cfg) == []
    state = wait_task(wf.run(cfg), timeout=300.0)
    assert state["failed"] is None, f"Encode failed: {state['failed']}"
    return out, state


def test_restoration_alone_10_bits_and_grain(tmp_path: Path) -> None:
    src = tmp_path / "src.mkv"
    _grainy_source(src)
    plain, _ = _encode(tmp_path, src, "plain.mkv", bit_depth="10")
    restored, state = _encode(tmp_path, src, "restored.mkv", restoration=RestoreSettings(enabled=True, level="fort"))

    probe = ffprobe_json(restored)
    [video] = streams_of_type(probe, "video")
    assert video["pix_fmt"] == "yuv420p10le"  # Auto → 10 bits sous Restauration (D-h)
    assert _frames(restored) == _frames(src)
    assert len(streams_of_type(probe, "audio")) == 1
    assert any("--restore" in str(line) for line in state["progress"])
    reference = _luma(src)
    assert _grain(_luma(restored), reference) < 0.8 * _grain(_luma(plain), reference)


def test_restoration_with_interpolation_8_bits(tmp_path: Path) -> None:
    src = tmp_path / "src.mkv"
    _grainy_source(src)
    out, _ = _encode(
        tmp_path, src, "both.mkv", bit_depth="8",
        restoration=RestoreSettings(enabled=True, fast=True),
        interpolation=FrameInterpolationSettings(enabled=True, factor=2, quality="fast"),
    )
    [video] = streams_of_type(ffprobe_json(out), "video")
    assert video["pix_fmt"] == "yuv420p"
    assert _frames(out) == 2 * _frames(src)
    subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(out), "-map", "0:v:0", "-f", "null", "-"],
                   check=True, capture_output=True, timeout=60)


_MASTER_DISPLAY = "G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(10000000,1)"


def _hdr10_source(path: Path) -> None:
    """HEVC 10 bits PQ granuleux (MaxCLL / MaxFALL déclarés 1000,400) et une piste audio."""
    subprocess.run([
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"gradients=size={W}x{H}:rate={FPS}:speed=0.02:seed=5:c0=white:c1=black,noise=alls=24:allf=t",
        "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000",
        "-t", str(DURATION), "-pix_fmt", "yuv420p10le", "-c:v", "libx265", "-preset", "ultrafast",
        "-x265-params", "log-level=none:lossless=1:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:"
        f"master-display={_MASTER_DISPLAY}:max-cll=1000,400",
        "-c:a", "flac", "-shortest", str(path),
    ], check=True, capture_output=True, timeout=120)


def _light_levels(path: Path) -> tuple[tuple[int, int], tuple[int, int]]:
    """MaxCLL / MaxFALL de l'élément Colour (piste) et du SEI de la première image."""
    def find(side_data: list[dict]) -> tuple[int, int]:
        cll = next(s for s in side_data if s["side_data_type"] == "Content light level metadata")
        return int(cll["max_content"]), int(cll["max_average"])
    stream = streams_of_type(ffprobe_json(path), "video")[0]
    frame = json.loads(subprocess.run([
        "ffprobe", "-v", "error", "-read_intervals", "%+#1", "-select_streams", "v:0", "-show_frames", "-of", "json",
        str(path)], capture_output=True, text=True, check=True).stdout)["frames"][0]
    return find(stream.get("side_data_list", [])), find(frame.get("side_data_list", []))


def _measured(path: Path, *args: str) -> tuple[int, int]:
    """MaxCLL / MaxFALL (arrondis) des trames de ``path`` passées par le moteur avec ``args``, même définition."""
    stats = path.with_suffix(".jsonl")
    decode = subprocess.Popen(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v:0", "-vf", "format=yuv420p10le",
                               "-f", "yuv4mpegpipe", "-strict", "-1", "-"], stdout=subprocess.PIPE)
    subprocess.run([str(RIFE_BIN), "--factor", "1", "--matrix", "bt2020nc", "--range", "limited", "--chroma-loc", "left",
                    "--transfer", "pq", "--quiet", *args, "--frame-statistics", str(stats), "-o", os.devnull],
                   stdin=decode.stdout, check=True, timeout=120)
    decode.wait(timeout=60)
    frames = [json.loads(line) for line in stats.read_text().splitlines() if '"frame"' in line]
    return round(max(max(f["max_nits"]) for f in frames)), round(max(f["avg_nits"] for f in frames))


@pytest.mark.parametrize("codec", ["libx265", "hevc_nvenc", "nvencc_hevc"])
def test_restoration_hdr10_measures_light_levels(tmp_path: Path, codec: str) -> None:
    hardware = set(os.environ.get("MUXIVEO_TEST_HW", "").split(","))
    if codec == "nvencc_hevc" and ("nvencc" not in hardware or not shutil.which("nvencc")):
        pytest.skip("Activer MUXIVEO_TEST_HW=nvencc (NVEncC requis)")
    if codec == "hevc_nvenc" and "nvenc" not in hardware:
        pytest.skip("Activer MUXIVEO_TEST_HW=nvenc")
    if "frame-statistics" not in (rife_capabilities(str(RIFE_BIN)) or _NO_CAPS).options:
        pytest.skip("muxiveo-rife sans --frame-statistics")
    src = tmp_path / "hdr.mkv"
    _hdr10_source(src)
    out = tmp_path / f"restored-{codec}.mkv"
    cfg = EncodeConfig(
        source=src, output=out,
        video=VideoEncodeSettings(
            codec=codec, quality_mode=QualityMode.CRF if codec == "libx265" else QualityMode.CQ, crf=8, cq=12,
            preset={"libx265": "ultrafast", "hevc_nvenc": "p4", "nvencc_hevc": "P4"}[codec],
            source_color_transfer="smpte2084",
            inject_hdr_meta=True, master_display=_MASTER_DISPLAY, max_cll="1000,400",
            static_hdr_light_level_source="source", restoration=RestoreSettings(enabled=True, level="fort"),
        ),
        audio_tracks=[AudioTrackSettings(stream_index=1, codec="copy")],
        copy_subtitles=False, keep_chapters=False, duration_s=DURATION, work_dir=tmp_path / "work",
    )
    wf = EncodeWorkflow(ffmpeg_bin="ffmpeg", nvencc_bin=shutil.which("nvencc"), rife_bin=RIFE_BIN,
                        ram_buffer_enabled=False, ffmpeg_threads=2, generate_nfo=False)
    logs: list[str] = []
    wf.log_message.connect(lambda level, message: logs.append(f"{level}: {message}"))
    assert wf.validate(cfg) == []
    state = wait_task(wf.run(cfg), timeout=300.0)
    assert state["failed"] is None, f"Encode failed: {state['failed']}"
    text = "\n".join(logs + [str(line) for line in state["progress"]])
    # bruit synthétique très fort : MaxFALL déplacé de plus de 1 %, écart visible, mesure écrite
    assert "MaxCLL/MaxFALL exacts de l'image restaurée" in text and "écart visible" in text
    colour, sei = _light_levels(out)
    # Image restaurée mesurée avant l'encodage (comme pour le FEL) : Restauration déterministe, mêmes valeurs exactes.
    assert colour == sei == _measured(src, "--restore", "fort") != (1000, 400)
    assert _frames(out) == _frames(src)
