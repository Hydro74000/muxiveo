"""Intégration : conversion Dolby Vision P5 → HDR10 par NVEncC (outils réels, lot 5).

Épingle le comportement de NVEncC 9.36 : sortie en plage pleine pour un RPU P5,
ramenée en plage limitée par ``--vpp-tweak``. Si une version de NVEncC change ce
comportement, le noir ne sort plus à 64 et ce test échoue (la sonde du workflow
écarterait alors la voie native au profit du pipe FFmpeg).
"""

from __future__ import annotations

import shutil

import pytest

from core.workflows.encode.runtime.nvencc import build_nvencc_command
from core.workflows.encode.runtime.nvencc_p5 import nvencc_p5_features, run_nvencc_p5_probe

_TOOLS = ("nvencc", "ffmpeg", "dovi_tool")

pytestmark = pytest.mark.skipif(
    not all(shutil.which(tool) for tool in _TOOLS) or not nvencc_p5_features("nvencc"),
    reason="NVEncC (libdovi + libplacebo), FFmpeg et dovi_tool requis",
)


def test_nvencc_native_p5_conversion_levels(tmp_path):
    result = run_nvencc_p5_probe(
        nvencc_bin="nvencc",
        ffmpeg_bin="ffmpeg",
        dovi_tool_bin="dovi_tool",
        work_dir=tmp_path,
        build_command=lambda nvencc, video, output, source: build_nvencc_command(
            nvencc, video, output, input_path=source, input_reader="avhw",
        ),
        has_ffmpeg_libplacebo=True,
    )
    assert result.ok, result.reason
