"""Synchro en chaîne d'un job (``sources[i].sync``, ``--auto-sync-all``, ``tracks[].sync``) sur médias de synthèse."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.jobs.options import CommonOptions
from core.jobs.remux_config import build_remux_config

pytestmark = pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("ffmpeg", "ffprobe")), reason="FFmpeg et ffprobe requis")


def _source(path: Path, delay_ms: int) -> Path:
    """40 s : bruit rose modulé (corrélable), retardé de ``delay_ms`` ; vidéo noire ; sous-titre."""
    srt = path.with_suffix(".srt")
    srt.write_text("1\n00:00:05,000 --> 00:00:06,000\nbonjour\n", encoding="utf-8")
    volume = "volume=eval=frame:volume='0.15+0.6*abs(sin(t*1.7)*sin(t*0.29))'"
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=64x64:r=25:d=40",
        "-f", "lavfi", "-i", "anoisesrc=d=40:c=pink:r=48000:a=0.5:seed=7", "-i", str(srt),
        "-map", "0:v", "-map", "1:a", "-map", "2:s",
        "-af", f"{volume},adelay={delay_ms}|{delay_ms},atrim=0:40",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "flac", "-c:s", "srt",
        "-metadata:s:a:0", "language=fre", str(path),
    ], check=True, capture_output=True, timeout=120)
    return path


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    folder = tmp_path_factory.mktemp("sync-links")
    return [_source(folder / f"{name}.mkv", delay) for name, delay in (("ref", 0), ("b", 400), ("c", 700))]


class _Logger:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def emit(self, _level: str, message: str, **_fields: object) -> None:
        self.lines.append(message)


def _config(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(tool_ffmpeg="ffmpeg", tool_ffprobe="ffprobe", tool_mediainfo="/bin/false",
                           sync_cadence_auto_apply=True, sync_cadence_audio_method="auto", sync_quick_audio_check=True,
                           output_dir=tmp_path, work_dir=tmp_path / "work", app_data_dir=tmp_path / "data",
                           ensure_work_dir=lambda: tmp_path / "work", release_group_tag_enabled=False,
                           release_group_tag="", generate_nfo=False, tmdb_api_key="", tmdb_bearer_token="",
                           ffmpeg_threads=2, sync_rewrite_enabled=True, matroska_mux_backend="auto")


def _shifts(job: dict, tmp_path: Path, **options: object) -> tuple[dict[int, int], _Logger]:
    logger = _Logger()
    config = build_remux_config(job, _config(tmp_path), CommonOptions(mediainfo="/bin/false", **options),  # type: ignore[arg-type]
                                logger)  # type: ignore[arg-type]
    shifts = {source.file_index: next(t.time_shift_ms for t in source.tracks if t.track_type == "audio")
              for source in config.sources}
    return shifts, logger


def test_chained_references_follow_their_reference(media, tmp_path):
    ref, b, c = media
    job = {"version": 1, "output": str(tmp_path / "out.mkv"),
           "sources": [{"path": str(ref)}, {"path": str(b), "sync": {"reference": 0}},
                       {"path": str(c), "sync": {"reference": 1}}]}
    chained, logger = _shifts(job, tmp_path)
    assert chained[1] == pytest.approx(-400, abs=10) and chained[2] == pytest.approx(-700, abs=10)
    assert any("suit sa position" in line for line in logger.lines)
    direct, _ = _shifts({**job, "sources": [{"path": str(ref)}, {"path": str(b)}, {"path": str(c)}]}, tmp_path,
                        auto_sync_all=True)
    assert direct[2] == pytest.approx(chained[2], abs=10)
    given, _ = _shifts({**job, "sources": [{"path": str(ref)}, {"path": str(b), "sync": {"offset_ms": -400}},
                                           {"path": str(c), "sync": {"reference": 1, "offset_ms": -300}}]}, tmp_path)
    assert given == {0: 0, 1: -400, 2: -700}


def test_loops_are_refused(media, tmp_path):
    ref, b, _c = media
    from core.jobs.errors import JobError

    job = {"version": 1, "output": str(tmp_path / "out.mkv"),
           "sources": [{"path": str(ref), "sync": {"reference": 1, "offset_ms": 10}},
                       {"path": str(b), "sync": {"reference": 0, "offset_ms": 10}}]}
    with pytest.raises(JobError, match="boucle"):
        _shifts(job, tmp_path)


def test_track_only_sync_moves_the_track_alone(media, tmp_path):
    ref, b, _c = media
    job = {"version": 1, "output": str(tmp_path / "out.mkv"), "sync_subtitles": "none",
           "sources": [{"path": str(ref)}, {"path": str(b)}],
           "tracks": [{"source": 1, "mkv_tid": 2, "sync": {"reference": 0, "offset_ms": 250}}]}
    logger = _Logger()
    config = build_remux_config(job, _config(tmp_path), CommonOptions(mediainfo="/bin/false"),  # type: ignore[arg-type]
                                logger)  # type: ignore[arg-type]
    donor = config.sources[1]
    subtitle = next(t for t in donor.tracks if t.track_type == "subtitle")
    audio = next(t for t in donor.tracks if t.track_type == "audio")
    assert subtitle.time_shift_ms == 250 and subtitle.sync_calibration is not None and audio.time_shift_ms == 0
