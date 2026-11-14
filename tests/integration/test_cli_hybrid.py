"""Hybridation headless sur le moteur du Hybrid Studio (médias de synthèse) : plan, export, exécution, recette."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.integration._synth import ffprobe_json

pytestmark = pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("ffmpeg", "ffprobe")), reason="FFmpeg et ffprobe requis")

ROOT = Path(__file__).resolve().parents[2]


def _episode(path: Path, *, language: str, delay_ms: int, subtitle: bool, video: bool = True) -> None:
    volume = "volume=eval=frame:volume='0.15+0.6*abs(sin(t*1.7)*sin(t*0.29))'"
    inputs = ["-f", "lavfi", "-i", "color=c=black:s=64x64:r=25:d=30",
              "-f", "lavfi", "-i", "anoisesrc=d=30:c=pink:r=48000:a=0.5:seed=11"]
    maps = ["-map", "0:v", "-map", "1:a"]
    if subtitle:
        srt = path.parent.parent / f"{path.stem}.srt"  # hors du dossier de saison (sinon apparié comme média)
        srt.write_text("1\n00:00:05,000 --> 00:00:06,000\nbonjour\n", encoding="utf-8")
        inputs += ["-i", str(srt)]
        maps += ["-map", "2:s"]
    subprocess.run(["ffmpeg", "-v", "error", "-y", *inputs, *maps,
                    "-af", f"{volume},adelay={delay_ms}|{delay_ms},atrim=0:30",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "flac",
                    *(["-c:s", "srt", "-metadata:s:s:0", f"language={language}"] if subtitle else []),
                    "-metadata:s:a:0", f"language={language}", str(path)],
                   check=True, capture_output=True, timeout=120)


@pytest.fixture(scope="module")
def season(tmp_path_factory):
    root = tmp_path_factory.mktemp("hybrid")
    master, donor = root / "uhd", root / "vf"
    master.mkdir()
    donor.mkdir()
    for number in (1, 2):
        _episode(master / f"Serie.S01E0{number}.2160p.mkv", language="eng", delay_ms=0, subtitle=False)
        _episode(donor / f"Serie.S01E0{number}.FRENCH.1080p.mkv", language="fre", delay_ms=300, subtitle=True)
    return master, donor


def _cli(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
           "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"), "XDG_DATA_HOME": str(tmp_path / "xdg-data")}
    return subprocess.run([sys.executable, str(ROOT / "main.py"), "--cli", "hybrid", *args, "--ffmpeg", "ffmpeg",
                           "--ffprobe", "ffprobe", "--mediainfo", "/bin/false", "--work-dir", str(tmp_path / "work")],
                          cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          check=False, timeout=600)


def test_plan_export_and_recipe(season, tmp_path):
    master, donor = season
    result = _cli(tmp_path, "--source", f"master={master}", "--source", f"donor={donor}", "-o", str(tmp_path / "out"),
                  "--plan", "--export-jobs", str(tmp_path / "jobs"), "--save-recipe", str(tmp_path / "r.json"),
                  "--sync", "manual", "--shift-ms", "-300")
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert [episode["episode"] for episode in plan["episodes"]] == ["S01E01", "S01E02"]
    tracks = plan["episodes"][0]["tracks"]
    assert {(t["source"], t["type"]) for t in tracks} == {(0, "video"), (0, "audio"), (1, "audio"), (1, "subtitle")}
    assert all(t["time_shift_ms"] == -300 for t in tracks if t["source"] == 1)
    assert len(list((tmp_path / "jobs").glob("*.exact-job.json"))) == 2
    from core.jobs.schema import unknown_job_keys

    for exported in (tmp_path / "jobs").glob("*.exact-job.json"):  # exact-jobs exportés : conformes au contrat
        assert unknown_job_keys(json.loads(exported.read_text(encoding="utf-8"))) == []
    recipe = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
    assert recipe["format"] == "muxiveo-hybrid-recipe" and recipe["sync_strategy"] == "manual"
    # Recette relue : mêmes choix ; --matching order et paires manuelles donnent le même appariement.
    again = _cli(tmp_path, "--ref-dir", str(master), "--donor-dir", str(donor), "-o", str(tmp_path / "out"),
                 "--plan", "--recipe", str(tmp_path / "r.json"), "--matching", "order")
    assert again.returncode == 0, again.stderr
    assert json.loads(again.stdout)["episodes"][1]["tracks"] == plan["episodes"][1]["tracks"]
    assert json.loads(again.stdout)["episodes"][1]["episode"] == "Serie.S01E02.2160p"  # clé de l'ordre : le nom
    pairs = _cli(tmp_path, "--pair", f"{master / 'Serie.S01E01.2160p.mkv'}::{donor / 'Serie.S01E01.FRENCH.1080p.mkv'}",
                 "-o", str(tmp_path / "out"), "--plan", "--sync", "none")
    assert pairs.returncode == 0, pairs.stderr
    assert [e["episode"] for e in json.loads(pairs.stdout)["episodes"]] == ["S01E01"]


def test_run_with_automatic_sync(season, tmp_path):
    master, donor = season
    out = tmp_path / "out"
    result = _cli(tmp_path, "--ref-dir", str(master), "--donor-dir", str(donor), "-o", str(out),
                  "--report-json", str(tmp_path / "report.json"))
    assert result.returncode == 0, result.stderr
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))["episodes"]
    assert [entry["status"] for entry in report] == ["ok", "ok"]
    assert all(abs(entry["calibration"]["segments"][0]["shift_ms"] + 300) <= 10 for entry in report)
    produced = sorted(out.glob("*.mkv"))
    assert len(produced) == 2
    streams = ffprobe_json(produced[0])["streams"]
    assert [s["codec_type"] for s in streams] == ["video", "audio", "audio", "subtitle"]
    assert [s.get("tags", {}).get("language") for s in streams if s["codec_type"] == "audio"] == ["fre", "eng"]
    again = _cli(tmp_path, "--ref-dir", str(master), "--donor-dir", str(donor), "-o", str(out))
    assert again.returncode != 0, "sorties existantes : refus sans --force"


def test_episode_encoding_with_encode_options(season, tmp_path):
    master, donor = season
    out = tmp_path / "enc"
    result = _cli(tmp_path, "--pair", f"{master / 'Serie.S01E02.2160p.mkv'}::{donor / 'Serie.S01E02.FRENCH.1080p.mkv'}",
                  "-o", str(out), "--sync", "manual", "--shift-ms", "-300", "--video-codec", "libx265", "--crf", "32",
                  "--encoder-preset", "ultrafast")
    assert result.returncode == 0, result.stderr
    produced = list(out.glob("*.mkv"))
    assert len(produced) == 1
    assert [s["codec_name"] for s in ffprobe_json(produced[0])["streams"] if s["codec_type"] == "video"] == ["hevc"]
