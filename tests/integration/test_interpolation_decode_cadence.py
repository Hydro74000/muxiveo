"""Intégration : cadence du décodage y4m remis à RIFE après YADIF « une image par champ » (D19).

Le y4m ne porte pas d'horodatage : l'étage de décodage doit livrer un flux régulier.
Images synthétiques 16 × 16 (lavfi, CPU seul) dont le luma porte l'indice source,
pour vérifier comptes, durée représentée et provenance des répétitions.
"""

from __future__ import annotations

import io
import shutil
import subprocess
from fractions import Fraction

import pytest

from core.workflows.encode import FrameInterpolationSettings, VideoEncodeSettings, VideoFilterSettings
from core.workflows.encode.domain import build_vf
from core.workflows.encode.interpolation import (
    build_decode_stage,
    decode_normalization_rate,
    interpolation_source_from_probe,
)

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg requis")


def _source_graph(kind: str, rate: Fraction, *, holes: bool = False) -> str:
    """100 images (alternance progressive / TFF, ou uniformes) ; à trous : PTS d'origine conservés."""
    base = f"nullsrc=s=16x16:r={rate}:d=5,geq=lum='16+N':cb=128:cr=128"
    if kind == "mixed":
        graph = (
            base + ",split[p][i];"
            "[p]select='not(mod(n,2))',setfield=prog[p0];"
            "[i]select='mod(n,2)',setfield=tff[i0];"
            f"[p0][i0]interleave=duration=longest,fps={rate}"
        )
    else:
        graph = base + (",setfield=tff" if kind == "interlaced" else ",setfield=prog")
    graph += ",trim=end_frame=100"
    if holes:
        graph += ",select='not(eq(n,20)+eq(n,60))'"
    return graph


def _video(mode: str, deint: str) -> VideoEncodeSettings:
    return VideoEncodeSettings(
        stream_index=0,
        codec="libx265",
        filters=VideoFilterSettings(yadif_enabled=True, yadif_mode=mode, yadif_parity="auto", yadif_deint=deint),
        interpolation=FrameInterpolationSettings(enabled=True, factor=2),
    )


def _decode(graph: str, vf: str) -> tuple[list[int], Fraction]:
    """Étage de décodage réel (``-fps_mode passthrough``, y4m) : indices source et cadence de l'en-tête."""
    command = build_decode_stage("ffmpeg", graph, stream_index=0, pre_input_args=("-f", "lavfi"), vf=vf)
    stream = io.BytesIO(subprocess.run(command, capture_output=True, check=True, timeout=60).stdout)
    tokens = stream.readline().decode().split()
    rate = Fraction(next(t[1:] for t in tokens if t.startswith("F")).replace(":", "/"))
    ids: list[int] = []
    while marker := stream.readline():
        assert marker == b"FRAME\n"
        ids.append(stream.read(16 * 16 * 3 // 2)[0] - 16)
    return ids, rate


def _decode_vf(video: VideoEncodeSettings, r_rate: str, avg_rate: str) -> str:
    info = interpolation_source_from_probe({"r_frame_rate": r_rate, "avg_frame_rate": avg_rate})
    rate = decode_normalization_rate(info, video)
    return ",".join(part for part in (build_vf(video), f"fps={rate}" if rate else "") if part)


@pytest.mark.parametrize("kind,rate", [
    ("mixed", Fraction(25)),
    ("progressive", Fraction(25)),
    ("interlaced", Fraction(25)),
    ("mixed", Fraction(30000, 1001)),
])
def test_interlaced_field_deinterlace_keeps_duration_and_pairs(kind, rate):
    # Avant correctif : alternance P/TFF → 150 images à 2 × cadence (3 s au lieu de 4).
    vf = _decode_vf(_video("send_field", "interlaced"), str(rate), str(rate))
    ids, out_rate = _decode(_source_graph(kind, rate), vf)
    assert out_rate == 2 * rate
    assert Fraction(len(ids)) / out_rate == Fraction(100) / rate
    assert ids == [i for i in range(100) for _ in range(2)]


def test_vfr_field_deinterlace_keeps_the_doubling():
    # Avant correctif : fps= à la cadence source après YADIF → 100 images à 25 i/s, une trame sur deux jetée.
    vf = _decode_vf(_video("send_field", "all"), "25/1", "49/2")
    assert vf.count("fps=") == 1
    ids, out_rate = _decode(_source_graph("interlaced", Fraction(25), holes=True), vf)
    assert out_rate == 50
    assert len(ids) == 200  # durée de 4 s conservée (98 images source, PTS d'origine)
    assert set(ids) == set(range(100)) - {20, 60}


@pytest.mark.parametrize("mode,deint,count,out_rate", [
    ("send_frame", "interlaced", 100, 25),
    ("send_field", "all", 200, 50),
])
def test_regular_deinterlace_unchanged(mode, deint, count, out_rate):
    vf = _decode_vf(_video(mode, deint), "25/1", "25/1")
    assert "fps=" not in vf
    ids, rate = _decode(_source_graph("mixed", Fraction(25)), vf)
    assert (len(ids), rate) == (count, out_rate)
