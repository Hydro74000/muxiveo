"""Extraction headless réelle (FFmpeg sur médias de synthèse) : commande extract et appel de l'API."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.integration._synth import ffprobe_json

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe requis",
)

ROOT = Path(__file__).resolve().parents[2]
FONT = b"\x00\x01\x00\x00police de test"


def _cli(tmp_path: Path, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
           "XDG_CONFIG_HOME": str(tmp_path / "xdg-config")}
    return subprocess.run([sys.executable, str(ROOT / "main.py"), "--cli", *args, "--ffmpeg", "ffmpeg",
                           "--ffprobe", "ffprobe", "--mediainfo", "/bin/false"],
                          cwd=ROOT, env=env, input=stdin, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", check=False, timeout=300)


def _source(tmp_path: Path) -> Path:
    """MKV HEVC + FLAC (fra) + SRT + police jointe."""
    srt, font, src = tmp_path / "s.srt", tmp_path / "police.ttf", tmp_path / "src.mkv"
    srt.write_text("1\n00:00:00,000 --> 00:00:00,900\nbonjour\n", encoding="utf-8")
    font.write_bytes(FONT)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc=size=320x180:rate=25", "-f", "lavfi", "-i", "sine=frequency=500:sample_rate=48000",
                    "-i", str(srt), "-attach", str(font), "-metadata:s:t", "mimetype=font/ttf", "-t", "1",
                    "-c:v", "libx265", "-preset", "ultrafast", "-x265-params", "log-level=error",
                    "-c:a", "flac", "-metadata:s:a:0", "language=fra", "-c:s", "srt", "-shortest", "-y", str(src)],
                   check=True)
    return src


def test_extract_all_writes_elementary_streams_and_attachments(tmp_path):
    src = _source(tmp_path)
    out = tmp_path / "out"
    result = _cli(tmp_path, "extract", "-i", str(src), "--all", "--output-dir", str(out))
    assert result.returncode == 0, result.stderr
    names = sorted(path.name for path in out.iterdir())
    flac = next(out.glob("src.*.t1.flac"))  # langue au format de l'inspection (fr-FR)
    assert names == sorted(["police.ttf", flac.name, "src.und.t0.hevc", "src.und.t2.srt"])
    assert (out / "src.und.t0.hevc").read_bytes()[:4] == b"\x00\x00\x00\x01", "HEVC Annex B"
    assert (out / "police.ttf").read_bytes() == FONT
    assert "bonjour" in (out / "src.und.t2.srt").read_text(encoding="utf-8")
    assert ffprobe_json(flac)["streams"][0]["codec_name"] == "flac"
    again = _cli(tmp_path, "extract", "-i", str(src), "--all", "--output-dir", str(out))
    assert again.returncode == 5 and not list(out.glob("*.partial"))


def test_extract_matroska_container_and_refused_layer(tmp_path):
    src = _source(tmp_path)
    result = _cli(tmp_path, "extract", "-i", str(src), "--track", "a:0", "--format", "mkv",
                  "--output-dir", str(tmp_path), "--json")
    assert result.returncode == 0, result.stderr
    output = Path(json.loads(result.stdout)["outputs"][0]["path"])
    assert output.suffix == ".mka" and ffprobe_json(output)["streams"][0]["codec_name"] == "flac"
    refused = _cli(tmp_path, "extract", "-i", str(src), "--track", "v:0", "--layer", "rpu", "-o",
                   str(tmp_path / "x.rpu"))
    assert refused.returncode == 3 and not (tmp_path / "x.rpu").exists()


def test_call_media_extract_streams_artifacts(tmp_path):
    src = _source(tmp_path)
    request = {"source": str(src), "items": [{"track": "s:0", "output": str(tmp_path / "sub.srt")},
                                             {"attachment": "police.ttf", "output": str(tmp_path / "f.ttf")}]}
    result = _cli(tmp_path, "call", "media.extract", "--request", "-", stdin=json.dumps(request))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "succeeded"
    events = [json.loads(line) for line in result.stderr.splitlines() if line.startswith("{")]
    assert [e["path"] for e in events if e["type"] == "artifact"] == [str(tmp_path / "sub.srt"),
                                                                      str(tmp_path / "f.ttf")]
    assert (tmp_path / "f.ttf").read_bytes() == FONT


def test_background_extraction_used_by_the_container_panel(qt_app, tmp_path):
    from core.inspector import FileInspector
    from core.jobs.extract import ExtractTools, plan_attachment, plan_track, start_extraction
    from core.workflows.common.ffmpeg_runtime import ffmpeg_progress_args
    from tests.integration._synth import wait_task

    src = _source(tmp_path)
    info = FileInspector(ffprobe_bin="ffprobe", mediainfo_bin="/bin/false").inspect(src)
    targets = [(plan_track(info, "v:0"), tmp_path / "v.hevc"), (plan_attachment(info, "0"), tmp_path / "p.ttf")]
    state = wait_task(start_extraction(info, targets, tools=ExtractTools("ffmpeg"),
                                       progress_args=ffmpeg_progress_args()), timeout=60)
    assert state["failed"] is None and state["finished"] == f"{tmp_path / 'v.hevc'}\n{tmp_path / 'p.ttf'}"
    assert (tmp_path / "p.ttf").read_bytes() == FONT and (tmp_path / "v.hevc").stat().st_size > 0
    refused = wait_task(start_extraction(info, targets[:1], tools=ExtractTools("ffmpeg")), timeout=60)
    assert refused["failed"] is not None and "existante" in refused["failed"][0]
