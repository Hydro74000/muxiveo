"""Liste blanche des paramètres libres face au FFmpeg réel : options d'encodeur acceptées, commande sans ajout."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from core.workflows.encode.domain.api_extra_params import api_extra_params_errors, ffmpeg_encoder_option_names

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg absent")


def test_real_ffmpeg_whitelist_and_command(tmp_path):
    allowed = ffmpeg_encoder_option_names("ffmpeg", "libx264")
    if allowed is None:
        pytest.skip("libx264 absent de ce FFmpeg")
    assert {"rc-lookahead", "x264-params", "g", "b", "passlogfile"} <= allowed
    assert not {"stats", "i", "f", "progress", "y", "filter_complex"} & allowed
    sample = "-rc-lookahead 10 -g 12 -x264-params aq-mode=1"
    assert api_extra_params_errors("libx264", sample, ffmpeg="ffmpeg", nvencc="nvencc") == []
    output = tmp_path / "out.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i", "testsrc2=s=160x90:r=10:d=0.1",
                    "-c:v", "libx264", *sample.split(), str(output)], check=True, cwd=tmp_path)
    assert [path.name for path in tmp_path.iterdir()] == ["out.mkv"]  # aucune sortie supplémentaire
