"""Lot Hybrid réel piloté par la commande globale, de la préparation au MKV."""
import shutil
import subprocess
import time

import pytest
from PySide6.QtCore import QCoreApplication

from core.matroska.reader import MatroskaReader
from tests.test_unified_operations import window as window


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg requis")
def test_global_action_executes_a_complete_hybrid_batch(window, tmp_path):
    reference, donor, output = (tmp_path / name for name in ("reference", "donor", "output"))
    reference.mkdir()
    donor.mkdir()
    for episode in (1, 2):
        for directory, language in ((reference, "eng"), (donor, "fra")):
            subprocess.run([
                "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=s=64x64:r=24:d=0.5",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=0.5",
                "-c:v", "ffv1", "-c:a", "eac3", "-metadata:s:a", f"language={language}",
                str(directory / f"Show.S01E{episode:02}.mkv"),
            ], check=True, capture_output=True, timeout=15)
    panel = window._hybrid_panel
    panel.reference.setText(str(reference))
    panel.donor.setText(str(donor))
    panel.output.setText(str(output))
    panel.sync_strategy.setCurrentIndex(panel.sync_strategy.findData("manual"))
    panel.manual_shift.setValue(125)
    window._stack.setCurrentIndex(4)
    window._refresh_operation_bar()
    states = []
    panel.op_state_changed.connect(lambda state, label: states.append((state, label)))
    window._run_btn.click()
    assert window._running
    deadline = time.monotonic() + 30
    while window._running and time.monotonic() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.01)
    assert not window._running, states
    assert states[-1][0] == "finished", states
    assert sum(state == "started" for state, _ in states) == 1  # pas de déverrouillage entre préparation et lot
    assert len(panel.jobs) == 2
    for config, _ in panel.jobs.values():
        assert config.output.exists()
        reader = MatroskaReader(config.output)
        french = next(track for track in reader.tracks() if track.language in {"fre", "fra"})
        assert next(reader.blocks(track_numbers={french.number})).timestamp_ms == 0
    window._log_panel._flush_pending()
    text = window._log_panel._text.toPlainText()
    assert "HYBRID" in text
    assert "S01E01" in text and "S01E02" in text
    assert "Synchronisation physique" in text
    assert "Hybridation terminée" in text
