"""FEL Analyzer : la session de capture (plugin ≥ 0.6.0) livre les mêmes octets que la capture par couche.

Média FEL réel et plugin : MUXIVEO_TEST_FEL_SOURCE (MKV profil 7 FEL), MUXIVEO_TEST_FEL_LIBRARY.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import numpy as np
import pytest

SOURCE = os.environ.get("MUXIVEO_TEST_FEL_SOURCE", "")
LIBRARY = os.environ.get("MUXIVEO_TEST_FEL_LIBRARY", "")

pytestmark = pytest.mark.skipif(not (SOURCE and LIBRARY and Path(SOURCE).is_file() and Path(LIBRARY).is_file()
                                     and shutil.which("ffmpeg")),
                                reason="Média FEL local et plugin requis (MUXIVEO_TEST_FEL_SOURCE/LIBRARY)")


@pytest.mark.parametrize("device", ["cpu", "auto"])
def test_session_capture_matches_layer_capture(device):
    from core.fel.engine import CaptureSession, FelEngine, capture_supported
    from core.hdr_inspection.fel_analyzer import SessionCapture, close_capture, plugin_capture
    from core.inspector import FileInspector

    engine = FelEngine(Path(LIBRARY))
    if not capture_supported(engine):
        pytest.skip("plugin antérieur à 0.6.0")
    info = FileInspector().inspect(Path(SOURCE))
    video = info.video_tracks[0]
    duration = info.duration_s or 2.0
    width, height = video.width or 3840, video.height or 2160

    class LayerOnly:  # même bibliothèque, capacité de session masquée : capture par couche d'origine
        lib, path = engine.lib, engine.path
        capabilities = {k: v for k, v in engine.capabilities.items() if k != "capture"}

    legacy = plugin_capture(LayerOnly(), Path(SOURCE), video.index, ffmpeg_bin="ffmpeg", width=width,
                            height=height, frame_rate="24000/1001")
    session = SessionCapture(CaptureSession(engine, Path(SOURCE), video.index, frame_rate="24000/1001",
                                            device=device), width, height)
    try:
        # Positionnements dans le désordre (retour en arrière compris), deux images avant la fin.
        for time_s in (0.2 * duration, 0.6 * duration, 0.35 * duration):
            reconstructed, mapped = session.pair(time_s, 2)
            for layer, frames in (("reconstructed", reconstructed), ("mapped", mapped)):
                for got, want in zip(frames, legacy(layer, time_s, 2)):
                    for plane in ("y", "cb", "cr"):
                        assert np.array_equal(getattr(got, plane), getattr(want, plane)), (device, time_s, layer)
    finally:
        close_capture(session)
