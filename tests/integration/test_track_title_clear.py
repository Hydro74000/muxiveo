"""Titre et langue de piste vidés (profil) : effacés dans la sortie réelle, variantes audio comprises, remux et
encodage."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.integration._synth import ffprobe_json, make_av_container

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe requis",
)

ROOT = Path(__file__).resolve().parents[2]
ENCODE = ["--video-codec", "libx265", "--crf", "32", "--encoder-preset", "ultrafast"]


def _titled_source(tmp_path: Path) -> Path:
    plain, src = tmp_path / "plain.mkv", tmp_path / "src.mkv"
    make_av_container(plain, duration=1.0)
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(plain), "-map", "0", "-c", "copy",
         "-metadata:s:v:0", "title=BEN.THE.MEN", "-metadata:s:a:0", "title=Audio original",
         "-metadata:s:a:0", "language=fre", "-y", str(src)],
        check=True,
    )
    return src


def _profile(tmp_path: Path) -> Path:
    """Titres effacés, puis variante AC3 créée depuis l'audio (clone après effacement)."""
    path = tmp_path / "clear.json"
    path.write_text(json.dumps({"version": 1, "kind": "decision-profile", "name": "Effacer", "rules": [
        {"id": "video", "priority": 30, "scope": "all",
         "match": {"all": [{"field": "type", "op": "is", "value": "video"}]},
         "actions": [{"type": "set_title", "value": ""}]},
        {"id": "audio", "priority": 20, "scope": "all",
         "match": {"all": [{"field": "type", "op": "is", "value": "audio"}]},
         "actions": [{"type": "set_title", "value": ""}, {"type": "set_language", "value": ""},
                     {"type": "create_audio_variant", "codec": "ac3"}]},
    ]}), encoding="utf-8")
    return path


def _cli(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
           "XDG_CONFIG_HOME": str(tmp_path / "xdg-config")}
    return subprocess.run([sys.executable, str(ROOT / "main.py"), "--cli", *args, "--ffmpeg", "ffmpeg",
                           "--ffprobe", "ffprobe", "--mediainfo", "/bin/false", "--work-dir", str(tmp_path / "work")],
                          cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", check=False, timeout=600)


@pytest.mark.parametrize("backend", ["native", "ffmpeg"])
@pytest.mark.parametrize("encode", [False, True], ids=["remux", "encode"])
def test_cleared_titles_stay_empty_in_output(tmp_path, backend, encode):
    src, out = _titled_source(tmp_path), tmp_path / "out.mkv"
    result = _cli(tmp_path, "profile", "apply", "--profile", str(_profile(tmp_path)), "-i", str(src),
                  "-o", str(out), "--mux-backend", backend, *(ENCODE if encode else []))
    assert result.returncode == 0, result.stderr
    streams = ffprobe_json(out)["streams"]
    assert [stream["codec_type"] for stream in streams] == ["video", "audio", "audio"]
    assert [stream["codec_name"] for stream in streams[1:]] == ["aac", "ac3"]
    assert [stream.get("tags", {}).get("title", "") for stream in streams] == ["", "", ""]
    assert [stream.get("tags", {}).get("language", "und") for stream in streams[1:]] == ["und", "und"]
