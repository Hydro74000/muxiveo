"""Jeton d'une opération headless sur le parcours réel : validation puis construction des commandes d'aperçu.

``checked_encode`` → ``EncodeWorkflow.validate`` → ``preview_encode`` → ``preview_command`` réels, sur un HEVC de
synthèse (FFmpeg / FFprobe réels) ; seul ``dovi_tool`` est un outil de test. Géométrie Dolby Vision + NVENC : la
sonde des bandes L5 tourne à la validation (sans résultat) puis à la construction des commandes, où le jeton
l'interrompt.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None or not Path("/bin/sh").exists(),
    reason="ffmpeg/ffprobe et shell POSIX requis",
)


def _hevc_source(path: Path) -> None:
    try:
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                        "testsrc2=size=1280x720:rate=24:duration=1", "-c:v", "libx265", "-x265-params",
                        "log-level=error", "-pix_fmt", "yuv420p10le", "-y", str(path)], check=True, timeout=120)
    except subprocess.CalledProcessError:
        pytest.skip("FFmpeg sans libx265")


def test_operation_token_interrupts_the_probe_run_while_building_preview_commands(qt_app, tmp_path):
    from core.jobs.encode_runtime import checked_encode, preview_encode
    from core.jobs.options import CommonOptions
    from core.subprocess_utils import ProbeCancelledError
    from core.workflows.encode.models import EncodeConfig, VideoEncodeSettings
    from core.workflows.encode.workflow import EncodeWorkflow

    source = tmp_path / "src.mkv"
    _hevc_source(source)
    armed, entered, calls = tmp_path / "lent", tmp_path / "entree", tmp_path / "appels"
    dovi_tool = tmp_path / "dovi_tool"
    # Sans témoin : échec immédiat (sonde sans résultat) ; témoin posé : entrée signalée puis attente longue.
    dovi_tool.write_text(f'#!/bin/sh\necho "$@" >> "{calls}"\n[ -e "{armed}" ] || exit 1\n'
                         f'touch "{entered}"\nexec sleep 30\n', encoding="utf-8")
    dovi_tool.chmod(0o755)
    wf = EncodeWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
    wf._bins["dovi_tool"] = str(dovi_tool)
    real_validate = wf.validate
    validated: list[list[str]] = []

    def validate_then_arm(config):  # validation réelle ; lenteur armée seulement après elle
        validated.append(real_validate(config))
        armed.touch()
        return validated[-1]

    wf.validate = validate_then_arm  # type: ignore[method-assign]
    video = VideoEncodeSettings(stream_index=0, source_path=source, codec="hevc_nvenc", copy_dv=True,
                                dovi_source_profile="8.1")
    config = EncodeConfig(source=source, output=tmp_path / "out.mkv", video=video, video_tracks=[video])
    cancel, previous = threading.Event(), threading.Event()
    wf.set_probe_cancel_event(previous)  # jeton antérieur : rétabli après l'opération
    checked, errors = checked_encode(config, wf, config=SimpleNamespace(tool_ffmpeg="ffmpeg"),
                                     options=CommonOptions(), accept_dovi_nvenc_crop=True, cancel_event=cancel)
    assert errors == [] and validated == [[]]
    assert calls.exists() and not entered.exists()  # sondes de la validation : sans résultat, jamais lentes
    cancelled_at: list[float] = []

    def cancel_on_entry() -> None:  # annulation déclenchée par l'entrée réelle dans la sonde d'aperçu
        deadline = time.monotonic() + 60
        while not entered.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        cancelled_at.append(time.monotonic())
        cancel.set()

    threading.Thread(target=cancel_on_entry, daemon=True).start()
    with pytest.raises(ProbeCancelledError):
        preview_encode(checked, wf, cancel)
    assert entered.exists() and cancelled_at
    assert time.monotonic() - cancelled_at[0] < 2  # sonde en cours tuée, pas attendue (30 s)
    assert wf._probe_cancel_event is previous
