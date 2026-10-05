"""Intégration : toute option émise pour un encodeur ffmpeg existe dans ce build (lot 4).

Chaque mode de débit du catalogue (``VIDEO_RATE_CONTROLS``) est construit puis
confronté à ``ffmpeg -h encoder=<codec>`` et aux options génériques
(AVCodecContext). Un encodeur absent du build est ignoré.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from functools import cache

import pytest

from core.workflows.encode import QualityMode, VideoEncodeSettings
from core.workflows.encode.catalog import VIDEO_RATE_CONTROLS, default_preset_for_codec, supports_10bit
from core.workflows.encode.domain import EncodeCodecDomainCallbacks, video_codec_args

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg requis")

# Options de l'outil ffmpeg (pas des AVOptions d'encodeur).
_FFTOOLS_OPTIONS = frozenset({
    "c", "vaapi_device", "init_hw_device", "filter_hw_device", "hwaccel", "hwaccel_device",
    "hwaccel_output_format", "pix_fmt", "vf", "pass", "passlogfile", "x265-params", "svtav1-params",
})
_OPTION_RE = re.compile(r"^\s{2}-([\w-]+)\s", re.MULTILINE)
_CB = EncodeCodecDomainCallbacks(platform="linux", vaapi_device="/dev/dri/renderD128")


def _ffmpeg_help(*args: str) -> str:
    return subprocess.run(
        ["ffmpeg", "-hide_banner", "-h", *args], capture_output=True, text=True, check=False,
    ).stdout


@cache
def _generic_options() -> frozenset[str]:
    full = _ffmpeg_help("full")
    start = full.find("AVCodecContext AVOptions:")
    end = full.find("\n\n", start)
    return frozenset(_OPTION_RE.findall(full[start:end])) if start >= 0 else frozenset()


@cache
def _encoders() -> str:
    return subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True, check=False,
    ).stdout


def _emitted_options(args: list[str]) -> set[str]:
    return {
        token.lstrip("-").split(":", 1)[0]
        for token in args
        if token.startswith("-") and not re.fullmatch(r"-\d+(\.\d+)?", token)
    }


_CASES = [
    (codec, spec.rc_id)
    for codec, controls in VIDEO_RATE_CONTROLS.items()
    if not codec.startswith("nvencc_")
    for spec in controls
]


@pytest.mark.parametrize(("codec", "rate_control"), _CASES)
def test_rate_control_options_exist_in_encoder(codec: str, rate_control: str) -> None:
    if not re.search(rf"\s{re.escape(codec)}\s", _encoders()):
        pytest.skip(f"{codec} absent de ce build ffmpeg")
    known = frozenset(_OPTION_RE.findall(_ffmpeg_help(f"encoder={codec}"))) | _generic_options()
    for force_10bit in (False, True) if supports_10bit(codec) else (False,):
        video = VideoEncodeSettings(
            codec=codec,
            rate_control=rate_control,
            quality_mode=QualityMode.SIZE if rate_control == "size" else QualityMode.CRF,
            preset=default_preset_for_codec(codec),
            force_10bit=force_10bit,
        )
        unknown = _emitted_options(video_codec_args(video, 8000, callbacks=_CB)) - known - _FFTOOLS_OPTIONS
        assert not unknown, f"{codec} / {rate_control} (10 bits={force_10bit}) : options inconnues {sorted(unknown)}"
