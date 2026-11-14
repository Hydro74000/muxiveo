"""Ctrl+C sur un remux headless : annulation propre en vrai sous-processus (POSIX)."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from core.workdir import PROCESS_DIR_MARKER
from tests.integration._synth import make_av_container

pytestmark = pytest.mark.skipif(
    os.name == "nt" or shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe et signaux POSIX requis",
)

ROOT = Path(__file__).resolve().parents[2]


def _slow_ffmpeg(tmp_path: Path, mark: Path) -> Path:
    """FFmpeg qui bloque sur tout traitement (``-i``) après avoir posé ``mark`` ; le reste passe au vrai.

    ``exec sleep`` : un seul processus, comme le vrai FFmpeg (un enfant du shell garderait le pipe ouvert).
    """
    real = shutil.which("ffmpeg")
    script = tmp_path / "bin" / "ffmpeg"
    script.parent.mkdir()
    script.write_text(
        "#!/bin/sh\n"
        'case " $* " in *" -i "*) : > "' + str(mark) + '"; exec sleep 120;; esac\n'
        f'exec "{real}" "$@"\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def test_sigint_cancels_remux_and_cleans_work_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    src = tmp_path / "src.mkv"
    make_av_container(src)
    out = tmp_path / "out" / "sortie.mkv"
    out.parent.mkdir()
    work = tmp_path / "work"
    mark = tmp_path / "ffmpeg-started"
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "main.py"), "--cli", "remux", "-i", str(src), "-o", str(out),
         "--mux-backend", "ffmpeg", "--no-nfo", "--ffmpeg", str(_slow_ffmpeg(tmp_path, mark)),
         "--ffprobe", "ffprobe", "--mediainfo", "/bin/false", "--work-dir", str(work)],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        errors="replace",
    )
    try:
        deadline = time.monotonic() + 60
        while not mark.exists():
            assert proc.poll() is None, proc.communicate()[1]
            assert time.monotonic() < deadline, "FFmpeg jamais lancé"
            time.sleep(0.05)
        started = time.monotonic()
        proc.send_signal(signal.SIGINT)
        _stdout, stderr = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert time.monotonic() - started < 30, "annulation trop lente : le processus FFmpeg n'a pas été arrêté"
    assert proc.returncode == 6, stderr
    assert "Opération annulée." in stderr
    assert not out.exists()
    assert not list(out.parent.glob("*.partial")), list(out.parent.iterdir())
    leftovers = [path for path in work.rglob(PROCESS_DIR_MARKER)] if work.exists() else []
    assert leftovers == []


def test_sigint_during_batch_cancels_current_job_and_stops(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    sources = [tmp_path / "a.mkv", tmp_path / "b.mkv"]
    for src in sources:
        make_av_container(src)
    out = tmp_path / "out"
    out.mkdir()
    work = tmp_path / "work"
    mark = tmp_path / "ffmpeg-started"
    template = tmp_path / "template.json"
    template.write_text('{"version": 1}', encoding="utf-8")
    batch = tmp_path / "batch.json"
    batch.write_text(json.dumps({"jobs": [{"sources": [{"path": str(src)}], "output": str(out / src.name)}
                                          for src in sources]}), encoding="utf-8")
    summary = tmp_path / "summary.json"
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "main.py"), "--cli", "batch", "--template", str(template), "--batch", str(batch),
         "--summary", str(summary), "--continue-on-error", "--mux-backend", "ffmpeg", "--no-nfo",
         "--ffmpeg", str(_slow_ffmpeg(tmp_path, mark)), "--ffprobe", "ffprobe", "--mediainfo", "/bin/false",
         "--work-dir", str(work)],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        errors="replace",
    )
    try:
        deadline = time.monotonic() + 60
        while not mark.exists():
            assert proc.poll() is None, proc.communicate()[1]
            assert time.monotonic() < deadline, "FFmpeg jamais lancé"
            time.sleep(0.05)
        proc.send_signal(signal.SIGINT)
        _stdout, stderr = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 7, stderr
    payload = json.loads(summary.read_text(encoding="utf-8"))
    assert payload["cancelled"] is True and payload["total"] == 1
    assert [job["status"] for job in payload["jobs"]] == ["cancelled"]
    assert not list(out.iterdir())
    assert (list(work.rglob(PROCESS_DIR_MARKER)) if work.exists() else []) == []


def test_sigint_during_call_returns_a_cancelled_response(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    src = tmp_path / "src.mkv"
    make_av_container(src)
    out = tmp_path / "sortie.mkv"
    mark = tmp_path / "ffmpeg-started"
    request = tmp_path / "requete.json"
    request.write_text(json.dumps({"job": {"version": 1, "sources": ["src.mkv"], "output": "sortie.mkv",
                                           "mux_backend": "ffmpeg"}, "options": {"nfo": False}}), encoding="utf-8")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "main.py"), "--cli", "call", "media.run", "--request", str(request),
         "--ffmpeg", str(_slow_ffmpeg(tmp_path, mark)), "--ffprobe", "ffprobe", "--mediainfo", "/bin/false",
         "--work-dir", str(tmp_path / "work")],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        errors="replace",
    )
    try:
        deadline = time.monotonic() + 60
        while not mark.exists():
            assert proc.poll() is None, proc.communicate()
            assert time.monotonic() < deadline, "FFmpeg jamais lancé"
            time.sleep(0.05)
        proc.send_signal(signal.SIGINT)
        stdout, stderr = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 6, stderr
    response = json.loads(stdout)
    assert response["status"] == "cancelled" and response["error"]["code"] == "cancelled"
    states = [json.loads(line).get("state") for line in stderr.splitlines() if line.startswith("{")]
    assert states[-1] == "cancelled"
    assert not out.exists()


def test_sigint_during_the_stream_kind_probe_of_sync_scan_stops_it(tmp_path, monkeypatch):
    """Audit 13 : la sonde du type de flux passe par le port CLI ; Ctrl+C la tue au lieu d'attendre son délai."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    source = tmp_path / "src.mkv"
    make_av_container(source)
    mark = tmp_path / "ffprobe-started"
    ffprobe = tmp_path / "bin" / "ffprobe"
    ffprobe.parent.mkdir()
    ffprobe.write_text(f'#!/bin/sh\necho $$ > "{mark}"\nexec sleep 120\n', encoding="utf-8")
    ffprobe.chmod(0o755)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "main.py"), "--cli", "sync-scan", "--ref", str(source), "--target", str(source),
         "--stream-ref", "1", "--stream-target", "1", "--no-cadence-auto", "--ffprobe", str(ffprobe),
         "--mediainfo", "/bin/false", "--work-dir", str(tmp_path / "work")],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        errors="replace",
    )
    try:
        deadline = time.monotonic() + 60
        while not (mark.exists() and mark.read_text().strip()):
            assert proc.poll() is None, proc.communicate()[1]
            assert time.monotonic() < deadline, "sonde jamais lancée"
            time.sleep(0.05)
        started = time.monotonic()
        proc.send_signal(signal.SIGINT)
        _stdout, stderr = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert time.monotonic() - started < 8, "annulation attendue avant le délai de la sonde (10 s)"
    assert proc.returncode == 6, stderr
    _assert_probe_killed(int(mark.read_text()))


def _assert_probe_killed(pid: int) -> None:
    """La sonde lancée par le CLI est morte (jamais laissée orpheline) ; tuée ici sinon, puis échec."""
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    os.kill(pid, signal.SIGKILL)
    pytest.fail("sonde restée en vie après la fin du CLI")


@pytest.mark.skipif(not Path("/proc").exists(), reason="Linux")
def test_sigint_during_a_capabilities_probe_ends_the_cli_process(tmp_path, monkeypatch):
    """Audit 14 : Ctrl+C pendant une sonde de capacités → sonde tuée, processus CLI terminé aussitôt (code 6)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    mark = tmp_path / "ffmpeg-started"
    ffmpeg = tmp_path / "bin" / "ffmpeg"
    ffmpeg.parent.mkdir()
    ffmpeg.write_text(f'#!/bin/sh\ncase "$*" in *-filters*) echo $$ > "{mark}"; exec sleep 120;; esac\n',
                      encoding="utf-8")
    ffmpeg.chmod(0o755)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "main.py"), "--cli", "capabilities", "--section", "vulkan", "--ffmpeg", str(ffmpeg)],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        errors="replace",
    )
    try:
        deadline = time.monotonic() + 60
        while not (mark.exists() and mark.read_text().strip()):
            assert proc.poll() is None, proc.communicate()[1]
            assert time.monotonic() < deadline, "sonde jamais lancée"
            time.sleep(0.05)
        started = time.monotonic()
        proc.send_signal(signal.SIGINT)
        _stdout, stderr = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert time.monotonic() - started < 5, "processus CLI retenu par une sonde"
    assert proc.returncode == 6, stderr
    probe_pid = int(mark.read_text())
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:  # sonde tuée par le jeton, jamais laissée orpheline
        try:
            os.kill(probe_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        os.kill(probe_pid, signal.SIGKILL)
        pytest.fail("sonde de capacités restée en vie après la fin du CLI")


def test_sigint_during_a_cadence_resolution_of_calibration_edit_returns_6(tmp_path, monkeypatch):
    """Audit 15 : ``calibration edit --resolve-cadence`` : Ctrl+C pendant l'analyse → annulation (code 6), sonde tuée."""
    from core.workflows.cadence import CadenceMismatch, CadenceType
    from core.workflows.sync_calibration import SyncCalibration, SyncSegment

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    pal = CadenceMismatch(cadence_type=CadenceType.PAL_TO_FILM_23976, source_fps=25.0, target_fps=23.976,
                          speed_factor=24000.0 / 25025.0, speed_ratio="24000/25025")
    calibration = tmp_path / "calibration.json"
    calibration.write_text(json.dumps(SyncCalibration((SyncSegment(0, 0.0),), cadence_mismatch=pal,
                                                      cadence_audio_method="auto").to_dict()), encoding="utf-8")
    mark = tmp_path / "ffprobe-started"
    ffprobe = tmp_path / "bin" / "ffprobe"
    ffprobe.parent.mkdir()
    ffprobe.write_text(f'#!/bin/sh\necho $$ > "{mark}"\nexec sleep 120\n', encoding="utf-8")
    ffprobe.chmod(0o755)
    (tmp_path / "ref.mkv").write_bytes(b"x")
    (tmp_path / "tgt.mkv").write_bytes(b"x")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
           "PATH": f"{ffprobe.parent}{os.pathsep}{os.environ.get('PATH', '')}"}
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "main.py"), "--cli", "calibration", "edit", str(calibration),
         "--resolve-cadence", f"{tmp_path / 'ref.mkv'}:1", f"{tmp_path / 'tgt.mkv'}:1"],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        errors="replace",
    )
    try:
        deadline = time.monotonic() + 60
        while not (mark.exists() and mark.read_text().strip()):
            assert proc.poll() is None, proc.communicate()[1]
            assert time.monotonic() < deadline, "analyse jamais lancée"
            time.sleep(0.05)
        proc.send_signal(signal.SIGINT)
        _stdout, stderr = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 6, stderr
    _assert_probe_killed(int(mark.read_text()))  # sonde de l'analyse tuée
