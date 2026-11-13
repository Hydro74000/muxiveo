"""Encodage headless réel (x265 sur médias de synthèse) : commande encode, aperçu, lot, appel de l'API."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.integration._synth import ffprobe_json, make_av_container, streams_of_type

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe requis",
)

ROOT = Path(__file__).resolve().parents[2]
FAST = ["--video-codec", "libx265", "--crf", "32", "--encoder-preset", "ultrafast", "--no-nfo"]


def _cli(tmp_path: Path, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
           "XDG_CONFIG_HOME": str(tmp_path / "xdg-config")}
    return subprocess.run([sys.executable, str(ROOT / "main.py"), "--cli", *args, "--ffmpeg", "ffmpeg",
                           "--ffprobe", "ffprobe", "--mediainfo", "/bin/false", "--work-dir", str(tmp_path / "work")],
                          cwd=ROOT, env=env, input=stdin, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", check=False, timeout=600)


def _codecs(path: Path, kind: str) -> list[str]:
    return [stream["codec_name"] for stream in streams_of_type(ffprobe_json(path), kind)]


def test_encode_command_reencodes_video_and_audio(tmp_path):
    src, out = tmp_path / "src.mkv", tmp_path / "out.mkv"
    make_av_container(src, duration=1.0)
    result = _cli(tmp_path, "encode", "-i", str(src), "-o", str(out), *FAST, "--audio-codec", "0:1=eac3")
    assert result.returncode == 0, result.stderr
    assert _codecs(out, "video") == ["hevc"] and _codecs(out, "audio") == ["eac3"]
    assert "frame=" not in result.stderr, "sortie brute de FFmpeg seulement avec --verbose"


def test_encode_dry_run_prints_the_encoder_command(tmp_path):
    src = tmp_path / "src.mkv"
    make_av_container(src, duration=1.0)
    result = _cli(tmp_path, "encode", "-i", str(src), "-o", str(tmp_path / "out.mkv"), *FAST, "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "libx265" in result.stdout and not (tmp_path / "out.mkv").exists()


def test_validate_json_publishes_the_resolved_plan(tmp_path):
    src = tmp_path / "src.mkv"
    make_av_container(src, duration=1.0)
    result = _cli(tmp_path, "validate", "-i", str(src), "-o", str(tmp_path / "out.mkv"), *FAST, "--json")
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)["encode"]
    assert plan["pure_copy"] is False and plan["video"][0]["codec"] == "libx265" and plan["video"][0]["crf"] == 32


def test_batch_template_with_encode_block(tmp_path):
    sources = [tmp_path / "a.mkv", tmp_path / "b.mkv"]
    for src in sources:
        make_av_container(src, duration=1.0)
    template = tmp_path / "template.json"
    template.write_text(json.dumps({"version": 1, "encode": {"video": {"default": {
        "codec": "libx265", "crf": 32, "encoder_preset": "ultrafast"}}}}), encoding="utf-8")
    batch = tmp_path / "batch.json"
    out = tmp_path / "out"
    out.mkdir()  # sorties explicites : dossier existant (créé seulement pour les sorties générées)
    batch.write_text(json.dumps({"jobs": [{"sources": [{"path": str(src)}], "output": str(out / src.name)}
                                          for src in sources]}), encoding="utf-8")
    result = _cli(tmp_path, "batch", "--template", str(template), "--batch", str(batch), "--no-nfo")
    assert result.returncode == 0, result.stderr
    assert all(_codecs(out / src.name, "video") == ["hevc"] for src in sources)


def test_call_media_run_with_encode_block_streams_progress(tmp_path):
    src = tmp_path / "src.mkv"
    make_av_container(src, duration=2.0)
    request = {"job": {"version": 1, "sources": [str(src)], "output": str(tmp_path / "out.mkv"),
                       "encode": {"video": {"default": {"codec": "libx265", "crf": 32,
                                                        "encoder_preset": "ultrafast"}}}},
               "options": {"nfo": False}}
    result = _cli(tmp_path, "call", "media.run", "--request", "-", stdin=json.dumps(request))
    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout)
    assert response["status"] == "succeeded" and response["result"]["status"] == "completed"
    events = [json.loads(line) for line in result.stderr.splitlines() if line.startswith("{")]
    assert any(e["type"] == "artifact" and e.get("kind") == "output" for e in events)
    assert _codecs(tmp_path / "out.mkv", "video") == ["hevc"]
