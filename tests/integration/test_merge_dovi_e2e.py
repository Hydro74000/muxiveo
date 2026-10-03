"""Merge DoVi de bout en bout sur médias synthétiques (HEVC 10 bits PQ + RPU généré).

Ignoré sans dovi_tool / hdr10plus_tool / mediainfo ou sans encodeur HEVC 10 bits
(libx265, sinon hevc_nvenc).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from core.workflows.encode.runtime.frame_count_guard import MetadataAdjustment
from core.workflows.merge_dovi import DoviProfile, MergeDoviWorkflow, WorkflowError, WorkflowStep

_PQ_VUI = "hevc_metadata=colour_primaries=9:transfer_characteristics=16:matrix_coefficients=9"
_FRAMES = 48


def _hevc10_encoder() -> list[str] | None:
    for args in (
        ["-c:v", "libx265", "-preset", "ultrafast", "-x265-params", "log-level=none", "-pix_fmt", "yuv420p10le"],
        ["-c:v", "hevc_nvenc", "-profile:v", "main10", "-pix_fmt", "p010le"],
    ):
        probe = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=320x180:d=0.2",
             "-frames:v", "2", *args, "-f", "null", "-"],
            capture_output=True,
        )
        if probe.returncode == 0:
            return [*args, "-g", "24", "-bsf:v", _PQ_VUI]
    return None


_TOOLS = ("ffmpeg", "ffprobe", "mediainfo", "dovi_tool", "hdr10plus_tool")
_ENCODER = _hevc10_encoder() if all(shutil.which(t) for t in _TOOLS) else None
pytestmark = pytest.mark.skipif(_ENCODER is None, reason="outils DoVi ou encodeur HEVC 10 bits absents")


def _encoder() -> list[str]:
    assert _ENCODER is not None
    return _ENCODER


def _run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 0, f"{' '.join(cmd)}\n{result.stderr}"


def _film1(root: Path) -> Path:
    path = root / "film1.mkv"
    _run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=s=320x180:r=24000/1001",
        "-f", "lavfi", "-i", "sine=f=440:d=3",
        "-frames:v", str(_FRAMES), *_encoder(), "-c:a", "aac", "-shortest", str(path),
    ])
    return path


def _film2_dovi(root: Path, frames: int) -> Path:
    base = root / f"film2_base_{frames}.hevc"
    _run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=s=320x180:r=24000/1001",
        "-frames:v", str(frames), *_encoder(), "-f", "hevc", str(base),
    ])
    config = root / f"gen_{frames}.json"
    config.write_text(json.dumps({
        "cm_version": "V40", "length": frames,
        "level6": {"max_display_mastering_luminance": 1000, "min_display_mastering_luminance": 1,
                   "max_content_light_level": 1000, "max_frame_average_light_level": 400},
    }), encoding="utf-8")
    rpu = root / f"rpu_{frames}.bin"
    _run(["dovi_tool", "generate", "-j", str(config), "-o", str(rpu)])
    film2 = root / f"film2_{frames}.hevc"
    _run(["dovi_tool", "-m", "2", "inject-rpu", "-i", str(base), "-r", str(rpu), "-o", str(film2)])
    return film2


def _run_workflow(qt_app, root: Path, film1: Path, film2: Path, adjustment: MetadataAdjustment, *, override=None) -> dict:
    wf = MergeDoviWorkflow()
    wf.set_validation_override(override)
    result: dict = {}
    wf.workflow_finished.connect(lambda out: result.update(status="finished", out=out))
    wf.workflow_failed.connect(lambda step, msg: result.update(status="failed", step=step.name, msg=msg))
    wf.workflow_cancelled.connect(lambda: result.update(status="cancelled"))
    wf.start(film1, film2, root / "work", root / "out", dovi_profile=DoviProfile.P8_1,
             metadata_adjustment=adjustment)
    deadline = 240.0
    start = time.monotonic()
    while not result and time.monotonic() - start < deadline:
        qt_app.processEvents()
        time.sleep(0.05)
    qt_app.processEvents()
    return result


def _rpu_frames(path: Path, tmp: Path) -> int:
    rpu = tmp / "check_rpu.bin"
    _run(["dovi_tool", "extract-rpu", "-i", str(path), "-o", str(rpu)])
    info = subprocess.run(["dovi_tool", "info", "-i", str(rpu), "--summary"], capture_output=True, text=True)
    return int(next(line.split(":")[1] for line in info.stdout.splitlines() if "Frames" in line))


def test_merge_dovi_identical_counts_end_to_end(qt_app, tmp_path: Path) -> None:
    film1 = _film1(tmp_path)
    film2 = _film2_dovi(tmp_path, _FRAMES)
    result = _run_workflow(qt_app, tmp_path, film1, film2, MetadataAdjustment.EXACT)

    assert result.get("status") == "finished", result
    output = Path(result["out"])
    assert _rpu_frames(output, tmp_path) == _FRAMES
    # Dossier process supprimé, seul le marqueur de racine subsiste.
    assert [p.name for p in (tmp_path / "work").iterdir()] == [".muxiveo-workdir"]


def test_merge_dovi_surplus_needs_explicit_trim_tail(qt_app, tmp_path: Path) -> None:
    film1 = _film1(tmp_path)
    film2 = _film2_dovi(tmp_path, _FRAMES + 2)

    refused = _run_workflow(qt_app, tmp_path / "exact", film1, film2, MetadataAdjustment.EXACT)
    # Film 2 HEVC brut : son compte exact vient du RPU extrait (CHECK_METADATA).
    assert refused.get("status") == "failed" and refused["step"] == "CHECK_METADATA", refused
    assert "politique exacte" in refused["msg"]
    assert [p.name for p in (tmp_path / "exact" / "work").iterdir()] == [".muxiveo-workdir"]

    trimmed = _run_workflow(qt_app, tmp_path / "trim", film1, film2, MetadataAdjustment.TRIM_TAIL)
    assert trimmed.get("status") == "finished", trimmed
    assert _rpu_frames(Path(trimmed["out"]), tmp_path) == _FRAMES


def test_merge_dovi_shorter_film2_is_refused(qt_app, tmp_path: Path) -> None:
    film1 = _film1(tmp_path)
    film2 = _film2_dovi(tmp_path, _FRAMES - 2)
    result = _run_workflow(qt_app, tmp_path, film1, film2, MetadataAdjustment.TRIM_TAIL)
    assert result.get("status") == "failed", result
    assert "aucune trame" in result["msg"]


def test_merge_dovi_counts_packets_despite_plausible_stale_statistics(qt_app, tmp_path: Path) -> None:
    film1 = _film1(tmp_path)
    stale = tmp_path / "film1_stale.mkv"
    _run(["ffmpeg", "-v", "error", "-y", "-i", str(film1), "-map", "0", "-c", "copy",
          "-metadata:s:v:0", f"NUMBER_OF_FRAMES={_FRAMES + 1}", str(stale)])
    film2 = _film2_dovi(tmp_path, _FRAMES)
    result = _run_workflow(qt_app, tmp_path, stale, film2, MetadataAdjustment.EXACT)
    assert result.get("status") == "finished", result
    assert _rpu_frames(Path(result["out"]), tmp_path) == _FRAMES


@pytest.mark.parametrize("accepted", [True, False])
def test_merge_dovi_final_rejection_requires_explicit_override(qt_app, tmp_path, monkeypatch, accepted):
    film1 = _film1(tmp_path)
    film2 = _film2_dovi(tmp_path, _FRAMES)
    output = MergeDoviWorkflow.output_path_for(film1, tmp_path / "out")
    output.parent.mkdir()
    output.write_bytes(b"previous result")
    requests = []

    def reject(*_args, **_kwargs):
        raise WorkflowError(WorkflowStep.VERIFY, "rejet simulé du flux final")

    def decide(candidate, message, cancelled):
        assert candidate.is_file()
        assert output.read_bytes() == b"previous result"
        requests.append(message)
        return accepted

    monkeypatch.setattr(MergeDoviWorkflow, "_step_verify", reject)
    result = _run_workflow(qt_app, tmp_path, film1, film2, MetadataAdjustment.EXACT, override=decide)
    assert len(requests) == 1
    if accepted:
        assert result.get("status") == "finished", result
        assert _rpu_frames(output, tmp_path) == _FRAMES
    else:
        assert result.get("status") == "failed" and result["step"] == "VERIFY", result
        assert output.read_bytes() == b"previous result"
    assert not any(p.is_dir() for p in (tmp_path / "work").iterdir())
