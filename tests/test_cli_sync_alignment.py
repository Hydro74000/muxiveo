"""Tests complets et poussés pour l'alignement CLI avec les dernières évolutions de synchronisation."""
from __future__ import annotations

import json
import shutil
from unittest.mock import MagicMock

import pytest

from core.config import AppConfig
from core.workflows.remux_models import RemuxConfig, SourceInput, TrackEntry
from core.workflows.sync_calibration import SyncCalibration, SyncSegment
from cli.commands import cmd_preview
from cli.constants import EXIT_OK
from cli.errors import CliError
from cli.hybrid import cmd_sync_scan, detect_stream_kind, perform_dynamic_sync, prepare_pair, HybridPair
from cli.logging import Logger
from cli.options import CommonOptions
from cli.parser import build_parser
from cli.profile import build_profile_remux_config
from cli.remux_config import build_remux_config


# =============================================================================
# 1. Tests Parser & Options
# =============================================================================

def test_cli_parser_sync_options_on_all_commands():
    parser = build_parser()

    # remux
    args = parser.parse_args(["remux", "-i", "a.mkv", "-o", "b.mkv", "--auto-sync", "--calibration", "c.json", "--detect-cuts", "--drift-threshold-ms", "30"])
    assert args.auto_sync is True
    assert args.calibration == "c.json"
    assert args.detect_cuts is True
    assert args.drift_threshold_ms == 30

    # preview
    args = parser.parse_args(["preview", "-i", "a.mkv", "--auto-sync", "--calibration", "c.json"])
    assert args.auto_sync is True
    assert args.calibration == "c.json"

    # profile apply
    args = parser.parse_args(["profile", "apply", "--profile", "p.json", "-i", "a.mkv", "-o", "b.mkv", "--auto-sync", "--calibration", "c.json"])
    assert args.auto_sync is True
    assert args.calibration == "c.json"

    # profile batch
    args = parser.parse_args(["profile", "batch", "--profile", "p.json", "--input-dir", "in", "--output-dir", "out", "--auto-sync"])
    assert args.auto_sync is True

    # sync-scan
    args = parser.parse_args(["sync-scan", "--ref", "ref.mkv", "--target", "tgt.srt", "--type", "subtitle", "--detect-cuts"])
    assert args.type == "subtitle"
    assert args.detect_cuts is True

    # shift-subs (mutually exclusive group offset vs calibration)
    args = parser.parse_args(["shift-subs", "-i", "in.srt", "-o", "out.srt", "--calibration", "c.json"])
    assert args.calibration == "c.json"


def test_detect_stream_kind(tmp_path):
    ffprobe = shutil.which("ffprobe") or "ffprobe"

    # Par extension
    assert detect_stream_kind(ffprobe, "subs.srt", "0") == "subtitle"
    assert detect_stream_kind(ffprobe, "subs.ass", "0") == "subtitle"
    assert detect_stream_kind(ffprobe, "subs.vtt", "0") == "subtitle"

    # Par spécificateur de flux
    assert detect_stream_kind(ffprobe, "video.mkv", "0:s:0") == "subtitle"
    assert detect_stream_kind(ffprobe, "video.mkv", "0:a:1") == "audio"
    assert detect_stream_kind(ffprobe, "video.mkv", "0:v:0") == "video"


# =============================================================================
# 2. Tests `sync-scan` avec sous-titres et audio
# =============================================================================

def test_cmd_sync_scan_subtitles_auto_and_output_json(tmp_path, capsys):
    ref_srt = tmp_path / "ref.srt"
    tgt_srt = tmp_path / "tgt.srt"
    out_json = tmp_path / "result.json"

    # Ref : répliques à 10s, 20s, 30s
    ref_srt.write_text(
        "1\n00:00:10,000 --> 00:00:13,000\nPremière réplique ref\n\n"
        "2\n00:00:20,000 --> 00:00:24,000\nDeuxième réplique ref\n\n"
        "3\n00:00:30,000 --> 00:00:35,000\nTroisième réplique ref\n",
        encoding="utf-8",
    )
    # Tgt : décalé de -600 ms (donc démarre à 9.4s, 19.4s, 29.4s) -> offset à appliquer = +600 ms
    tgt_srt.write_text(
        "1\n00:00:09,400 --> 00:00:12,400\nPremière réplique cible\n\n"
        "2\n00:00:19,400 --> 00:00:23,400\nDeuxième réplique cible\n\n"
        "3\n00:00:29,400 --> 00:00:34,400\nTroisième réplique cible\n",
        encoding="utf-8",
    )

    parser = build_parser()
    args = parser.parse_args([
        "sync-scan",
        "--ref", str(ref_srt),
        "--target", str(tgt_srt),
        "--type", "auto",
        "--output-json", str(out_json),
    ])

    config = AppConfig()
    logger = Logger(fmt="text")

    rc = cmd_sync_scan(args, config, logger)
    assert rc == EXIT_OK

    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip().splitlines()[-1])

    assert out_json.exists()
    file_payload = json.loads(out_json.read_text(encoding="utf-8"))
    assert payload == file_payload

    # Doit avoir détecté l'offset de +600 ms (tolérance ±50ms)
    assert len(payload["segments"]) == 1
    assert abs(payload["segments"][0]["shift_ms"] - 600.0) <= 50.0


def test_cmd_sync_scan_explicit_subtitle_type(tmp_path, capsys):
    ref_srt = tmp_path / "ref.srt"
    tgt_srt = tmp_path / "tgt.srt"

    ref_srt.write_text("1\n00:00:05,000 --> 00:00:08,000\nDialogue 1\n", encoding="utf-8")
    tgt_srt.write_text("1\n00:00:05,250 --> 00:00:08,250\nDialogue 1\n", encoding="utf-8")

    parser = build_parser()
    args = parser.parse_args([
        "sync-scan",
        "--ref", str(ref_srt),
        "--target", str(tgt_srt),
        "--type", "subtitle",
    ])

    config = AppConfig()
    logger = Logger(fmt="text")

    rc = cmd_sync_scan(args, config, logger)
    assert rc == EXIT_OK
    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip().splitlines()[-1])
    assert abs(payload["segments"][0]["shift_ms"] - (-250.0)) <= 50.0


# =============================================================================
# 3. Tests `hybrid` avec donneur sous-titres uniquement
# =============================================================================

def test_prepare_pair_with_subtitle_only_donor(tmp_path, monkeypatch):
    ref_file = tmp_path / "S01E01.mkv"
    donor_srt = tmp_path / "S01E01.fre.srt"
    ref_file.touch()
    donor_srt.write_text("1\n00:00:02,000 --> 00:00:04,000\nSous-titre donneur\n", encoding="utf-8")

    pair = HybridPair(ref_file, donor_srt, 1, 1)

    parser = build_parser()
    args = parser.parse_args([
        "hybrid",
        "--ref", str(ref_file),
        "--donor", str(donor_srt),
        "-o", str(tmp_path / "output"),
        "--sync-mode", "container",
    ])
    config = AppConfig()
    logger = Logger(fmt="text")

    # Mocker inspect_sources pour retourner un fichier vidéo+audio+sub en référence et un subtitle en donneur
    ref_video = TrackEntry(0, "video", "HEVC", "1080p", "und", "", file_id="src0")
    ref_audio = TrackEntry(1, "audio", "E-AC-3", "5.1", "eng", "VO", file_id="src0")
    ref_sub = TrackEntry(2, "subtitle", "SubRip", "", "eng", "English", file_id="src0")

    tgt_sub = TrackEntry(0, "subtitle", "SubRip", "", "fre", "Français", file_id="src1")

    src0 = SourceInput(path=ref_file, file_index=0, tracks=[ref_video, ref_audio, ref_sub])
    src1 = SourceInput(path=donor_srt, file_index=1, tracks=[tgt_sub])

    monkeypatch.setattr(
        "cli.remux_config.inspect_sources",
        lambda job, conf, opts, log, cli_inputs=None: ([src0, src1], [MagicMock(), MagicMock()], [ref_video, ref_audio, ref_sub, tgt_sub]),
    )

    # Mocker SubtitleSyncScanner.scan
    mock_calib = SyncCalibration.linear(-450)
    monkeypatch.setattr(
        "core.workflows.subtitle_sync_scan.SubtitleSyncScanner.scan",
        lambda self, *a, **k: mock_calib,
    )

    result, calibration = prepare_pair(pair, args, config, logger)

    assert calibration == mock_calib
    assert result.sync_mode == "container"
    # Le sous-titre donneur doit avoir reçu le shift_ms de -450
    assert tgt_sub.time_shift_ms == -450


def test_prepare_pair_with_subtitle_only_donor_physical_mode(tmp_path, monkeypatch):
    ref_file = tmp_path / "S01E01.mkv"
    donor_srt = tmp_path / "S01E01.fre.srt"
    ref_file.touch()
    donor_srt.touch()

    pair = HybridPair(ref_file, donor_srt, 1, 1)

    parser = build_parser()
    args = parser.parse_args([
        "hybrid",
        "--ref", str(ref_file),
        "--donor", str(donor_srt),
        "-o", str(tmp_path / "output"),
        "--sync-mode", "physical",
    ])
    config = AppConfig()
    logger = Logger(fmt="text")

    ref_audio = TrackEntry(1, "audio", "E-AC-3", "5.1", "eng", "VO", file_id="src0")
    tgt_sub = TrackEntry(0, "subtitle", "SubRip", "", "fre", "Français", file_id="src1")

    src0 = SourceInput(path=ref_file, file_index=0, tracks=[ref_audio])
    src1 = SourceInput(path=donor_srt, file_index=1, tracks=[tgt_sub])

    monkeypatch.setattr(
        "cli.remux_config.inspect_sources",
        lambda job, conf, opts, log, cli_inputs=None: ([src0, src1], [MagicMock(), MagicMock()], [ref_audio, tgt_sub]),
    )

    mock_calib = SyncCalibration((SyncSegment(0.0, -120.0), SyncSegment(30000.0, -550.0)), 0.95)
    monkeypatch.setattr(
        "core.workflows.subtitle_sync_scan.SubtitleSyncScanner.scan",
        lambda self, *a, **k: mock_calib,
    )

    result, calibration = prepare_pair(pair, args, config, logger)

    assert calibration == mock_calib
    assert result.sync_mode == "physical"
    assert "1" in result.sync_calibrations
    assert result.sync_calibrations["1"] == mock_calib.to_dict()


# =============================================================================
# 4. Tests `--calibration` sur remux et preview
# =============================================================================

def test_remux_with_explicit_calibration_file_container_mode(tmp_path, monkeypatch):
    calib_file = tmp_path / "my_calib.json"
    calib_file.write_text(json.dumps({
        "segments": [{"start_ms": 0.0, "shift_ms": 320.0}],
        "confidence": 1.0,
    }))

    ref_audio = TrackEntry(0, "audio", "FLAC", "2.0", "eng", "VO", file_id="src0")
    tgt_audio = TrackEntry(0, "audio", "E-AC-3", "5.1", "fre", "VF", file_id="src1")
    tgt_sub = TrackEntry(1, "subtitle", "SubRip", "", "fre", "VF", file_id="src1")

    src0 = SourceInput(path=tmp_path / "ref.mkv", file_index=0, tracks=[ref_audio])
    src1 = SourceInput(path=tmp_path / "donor.mkv", file_index=1, tracks=[tgt_audio, tgt_sub])

    monkeypatch.setattr(
        "cli.remux_config.inspect_sources",
        lambda job, conf, opts, log, cli_inputs=None: ([src0, src1], [MagicMock(), MagicMock()], [ref_audio, tgt_audio, tgt_sub]),
    )

    options = CommonOptions(calibration=str(calib_file), sync_mode="container", sync_subtitles="mirror")
    job = {
        "sources": [{"path": str(src0.path)}, {"path": str(src1.path)}],
        "output": str(tmp_path / "out.mkv"),
        "sync_mode": "container",
        "sync_subtitles": "mirror",
    }
    remux_conf = build_remux_config(job, AppConfig(), options, Logger(fmt="text"))

    # Vérifier que le décalage de +320 ms a été appliqué sur l'audio ET le sous-titre donneur
    assert tgt_audio.time_shift_ms == 320
    assert tgt_sub.time_shift_ms == 320
    # En mode conteneur, sync_calibrations doit être vide pour ne pas faire échouer le validateur de remux_plan
    assert remux_conf.sync_calibrations == {}


def test_remux_with_explicit_calibration_file_physical_mode(tmp_path, monkeypatch):
    calib_file = tmp_path / "cuts_calib.json"
    calib_dict = {
        "segments": [
            {"start_ms": 0.0, "shift_ms": 100.0},
            {"start_ms": 50000.0, "shift_ms": -200.0},
        ],
        "confidence": 0.9,
    }
    calib_file.write_text(json.dumps(calib_dict))

    ref_audio = TrackEntry(0, "audio", "FLAC", "2.0", "eng", "VO", file_id="src0")
    tgt_audio = TrackEntry(0, "audio", "E-AC-3", "5.1", "fre", "VF", file_id="src1")

    src0 = SourceInput(path=tmp_path / "ref.mkv", file_index=0, tracks=[ref_audio])
    src1 = SourceInput(path=tmp_path / "donor.mkv", file_index=1, tracks=[tgt_audio])

    monkeypatch.setattr(
        "cli.remux_config.inspect_sources",
        lambda job, conf, opts, log, cli_inputs=None: ([src0, src1], [MagicMock(), MagicMock()], [ref_audio, tgt_audio]),
    )

    options = CommonOptions(calibration=str(calib_file), sync_mode="physical")
    job = {
        "sources": [{"path": str(src0.path)}, {"path": str(src1.path)}],
        "output": str(tmp_path / "out.mkv"),
        "sync_mode": "physical",
    }
    remux_conf = build_remux_config(job, AppConfig(), options, Logger(fmt="text"))

    assert remux_conf.sync_mode == "physical"
    assert "1" in remux_conf.sync_calibrations
    assert remux_conf.sync_calibrations["1"]["segments"] == calib_dict["segments"]


def test_remux_with_cuts_calibration_in_container_mode_raises(tmp_path, monkeypatch):
    calib_file = tmp_path / "cuts_calib.json"
    calib_file.write_text(json.dumps({
        "segments": [{"start_ms": 0.0, "shift_ms": 0.0}, {"start_ms": 5000.0, "shift_ms": 100.0}],
    }))

    ref_audio = TrackEntry(0, "audio", "FLAC", "2.0", "eng", "VO", file_id="src0")
    tgt_audio = TrackEntry(0, "audio", "E-AC-3", "5.1", "fre", "VF", file_id="src1")

    src0 = SourceInput(path=tmp_path / "ref.mkv", file_index=0, tracks=[ref_audio])
    src1 = SourceInput(path=tmp_path / "donor.mkv", file_index=1, tracks=[tgt_audio])

    monkeypatch.setattr(
        "cli.remux_config.inspect_sources",
        lambda job, conf, opts, log, cli_inputs=None: ([src0, src1], [MagicMock(), MagicMock()], [ref_audio, tgt_audio]),
    )

    options = CommonOptions(calibration=str(calib_file), sync_mode="container")
    job = {"sources": [{"path": str(src0.path)}, {"path": str(src1.path)}], "output": str(tmp_path / "out.mkv"), "sync_mode": "container"}
    with pytest.raises(CliError, match="Le mode container ne prend pas en charge les coupures"):
        build_remux_config(job, AppConfig(), options, Logger(fmt="text"))


# =============================================================================
# 5. Réserve 3 : Profil avec flag auto-sync et réanalyse dynamique à chaque fois
# =============================================================================

def test_profile_dynamic_sync_reanalyzes_for_each_different_source_pair(tmp_path, monkeypatch):
    """Vérifie que la synchronisation sur un profil est un flag et recalcule dynamiquement
    la calibration pour chaque paire de sources, au lieu d'utiliser une calibration figée."""

    # Profil avec flag auto_sync: True
    profile = {
        "version": 1,
        "kind": "decision-profile",
        "name": "Auto Sync Hybrid Profile",
        "auto_sync": True,
        "rules": [
            {
                "id": "keep_all",
                "match": {"field": "track_type", "op": "in", "value": ["video", "audio", "subtitle"]},
                "actions": [{"type": "set_enabled", "value": True}],
            }
        ],
    }

    # Paire 1
    src1_ref = tmp_path / "pair1_ref.mkv"
    src1_donor = tmp_path / "pair1_donor.mkv"
    src1_ref.touch()
    src1_donor.touch()

    # Paire 2
    src2_ref = tmp_path / "pair2_ref.mkv"
    src2_donor = tmp_path / "pair2_donor.mkv"
    src2_ref.touch()
    src2_donor.touch()

    # Simuler deux calibrations complètement différentes retournées dynamiquement
    calib_pair1 = SyncCalibration.linear(-350.0)
    calib_pair2 = SyncCalibration.linear(820.0)

    scan_calls = []

    def mock_perform_dynamic_sync(sources, tracks, config, options, logger):
        scan_calls.append([s.path.name for s in sources])
        if "pair1_ref.mkv" in sources[0].path.name:
            return calib_pair1
        elif "pair2_ref.mkv" in sources[0].path.name:
            return calib_pair2
        return None

    monkeypatch.setattr("cli.hybrid.perform_dynamic_sync", mock_perform_dynamic_sync)

    # Exécution Paire 1
    t1_ref_v = TrackEntry(0, "video", "HEVC", "1080p", "und", "", file_id="src0")
    t1_ref_a = TrackEntry(1, "audio", "FLAC", "2.0", "eng", "VO", file_id="src0")
    t1_donor_a = TrackEntry(0, "audio", "E-AC-3", "5.1", "fre", "VF", file_id="src1")
    s1_0 = SourceInput(path=src1_ref, file_index=0, tracks=[t1_ref_v, t1_ref_a])
    s1_1 = SourceInput(path=src1_donor, file_index=1, tracks=[t1_donor_a])

    monkeypatch.setattr(
        "cli.profile.inspect_sources",
        lambda job, conf, opts, log, cli_inputs=None: ([s1_0, s1_1], [MagicMock(), MagicMock()], [t1_ref_v, t1_ref_a, t1_donor_a]),
    )

    conf1, _ = build_profile_remux_config(
        profile,
        cli_inputs=[str(src1_ref), str(src1_donor)],
        cli_output=str(tmp_path / "out1.mkv"),
        config=AppConfig(),
        options=CommonOptions(sync_mode="physical"),
        logger=Logger(fmt="text"),
    )

    # Exécution Paire 2
    t2_ref_v = TrackEntry(0, "video", "AVC", "1080p", "und", "", file_id="src0")
    t2_ref_a = TrackEntry(1, "audio", "DTS", "5.1", "eng", "VO", file_id="src0")
    t2_donor_a = TrackEntry(0, "audio", "AAC", "2.0", "fre", "VF", file_id="src1")
    s2_0 = SourceInput(path=src2_ref, file_index=0, tracks=[t2_ref_v, t2_ref_a])
    s2_1 = SourceInput(path=src2_donor, file_index=1, tracks=[t2_donor_a])

    monkeypatch.setattr(
        "cli.profile.inspect_sources",
        lambda job, conf, opts, log, cli_inputs=None: ([s2_0, s2_1], [MagicMock(), MagicMock()], [t2_ref_v, t2_ref_a, t2_donor_a]),
    )

    conf2, _ = build_profile_remux_config(
        profile,
        cli_inputs=[str(src2_ref), str(src2_donor)],
        cli_output=str(tmp_path / "out2.mkv"),
        config=AppConfig(),
        options=CommonOptions(sync_mode="physical"),
        logger=Logger(fmt="text"),
    )

    # 1. Vérifier que la synchro dynamique a été appelée DEUX fois
    assert len(scan_calls) == 2
    assert scan_calls[0] == ["pair1_ref.mkv", "pair1_donor.mkv"]
    assert scan_calls[1] == ["pair2_ref.mkv", "pair2_donor.mkv"]

    # 2. Vérifier que chaque configuration a reçu sa PROPRE calibration distincte
    assert conf1.sync_calibrations["1"]["segments"][0]["shift_ms"] == -350.0
    assert conf2.sync_calibrations["1"]["segments"][0]["shift_ms"] == 820.0


# =============================================================================
# 6. Tests approfondis : coupures, audio vs sub, contrat et preview
# =============================================================================

def test_cmd_sync_scan_subtitles_cuts_detection(tmp_path, capsys, monkeypatch):
    ref_srt = tmp_path / "ref.srt"
    tgt_srt = tmp_path / "tgt.srt"
    ref_srt.write_text("1\n00:00:01,000 --> 00:00:03,000\nHello\n", encoding="utf-8")
    tgt_srt.write_text("1\n00:00:01,000 --> 00:00:03,000\nHello\n", encoding="utf-8")

    expected_calib = SyncCalibration(
        (SyncSegment(0.0, -100.0), SyncSegment(120000.0, -400.0)),
        confidence=0.92,
    )
    monkeypatch.setattr(
        "core.workflows.subtitle_sync_scan.SubtitleSyncScanner.scan",
        lambda self, *a, **k: expected_calib,
    )

    parser = build_parser()
    args = parser.parse_args([
        "sync-scan",
        "--ref", str(ref_srt),
        "--target", str(tgt_srt),
        "--type", "auto",
        "--detect-cuts",
    ])
    rc = cmd_sync_scan(args, AppConfig(), Logger(fmt="text"))
    assert rc == EXIT_OK

    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip().splitlines()[-1])
    assert len(payload["segments"]) == 2
    assert payload["segments"][0]["shift_ms"] == -100.0
    assert payload["segments"][1]["shift_ms"] == -400.0


def test_perform_dynamic_sync_audio_vs_subtitles(tmp_path, monkeypatch):
    ref_file = tmp_path / "ref.mkv"
    donor_file = tmp_path / "donor.srt"
    ref_file.touch()
    donor_file.touch()

    ref_audio = TrackEntry(0, "audio", "E-AC-3", "5.1", "eng", "VO", file_id="src0")
    tgt_sub = TrackEntry(0, "subtitle", "SubRip", "", "fre", "VF", file_id="src1")

    src0 = SourceInput(path=ref_file, file_index=0, tracks=[ref_audio])
    src1 = SourceInput(path=donor_file, file_index=1, tracks=[tgt_sub])

    mock_calib = SyncCalibration.linear(-800.0)
    monkeypatch.setattr(
        "core.workflows.subtitle_sync_scan.SubtitleSyncScanner.scan",
        lambda self, ref_path, ref_tid, tgt_path, tgt_tid, is_ref_sub, detect_cuts, log: mock_calib,
    )

    logger = Logger(fmt="text")
    options = CommonOptions(auto_sync=True)
    calib = perform_dynamic_sync([src0, src1], [ref_audio, tgt_sub], AppConfig(), options, logger)

    assert calib == mock_calib
    assert calib.segments[0].shift_ms == -800.0


def test_build_remux_config_auto_sync_flag_runs_dynamic_sync(tmp_path, monkeypatch):
    ref_audio = TrackEntry(0, "audio", "FLAC", "2.0", "eng", "VO", file_id="src0")
    tgt_audio = TrackEntry(0, "audio", "E-AC-3", "5.1", "fre", "VF", file_id="src1")

    src0 = SourceInput(path=tmp_path / "ref.mkv", file_index=0, tracks=[ref_audio])
    src1 = SourceInput(path=tmp_path / "donor.mkv", file_index=1, tracks=[tgt_audio])

    monkeypatch.setattr(
        "cli.remux_config.inspect_sources",
        lambda job, conf, opts, log, cli_inputs=None: ([src0, src1], [MagicMock(), MagicMock()], [ref_audio, tgt_audio]),
    )

    mock_calib = SyncCalibration.linear(150.0)
    monkeypatch.setattr(
        "cli.hybrid.perform_dynamic_sync",
        lambda sources, tracks, config, options, logger: mock_calib,
    )

    # 1. En mode container : décalage direct sur la piste donneuse
    options = CommonOptions(auto_sync=True, sync_mode="container")
    job = {"sources": [{"path": str(src0.path)}, {"path": str(src1.path)}], "output": str(tmp_path / "out.mkv")}
    remux_conf = build_remux_config(job, AppConfig(), options, Logger(fmt="text"))
    assert tgt_audio.time_shift_ms == 150
    assert remux_conf.sync_calibrations == {}

    # 2. En mode physical : calibration stockée dans sync_calibrations
    options_phys = CommonOptions(auto_sync=True, sync_mode="physical")
    job_phys = {"sources": [{"path": str(src0.path)}, {"path": str(src1.path)}], "output": str(tmp_path / "out.mkv"), "sync_mode": "physical"}
    remux_conf_phys = build_remux_config(job_phys, AppConfig(), options_phys, Logger(fmt="text"))
    assert "1" in remux_conf_phys.sync_calibrations
    assert remux_conf_phys.sync_calibrations["1"]["segments"][0]["shift_ms"] == 150.0


def test_contract_validation_auto_sync_and_cuts():
    from cli.contract import validate_job_contract

    # Valide
    job_ok = {
        "version": 1,
        "sources": [{"path": "a.mkv"}],
        "output": "b.mkv",
        "auto_sync": True,
        "detect_cuts": True,
        "drift_threshold_ms": 30,
    }
    validate_job_contract(job_ok, require_version=True)

    # Erreurs de types
    job_err1 = {"version": 1, "sources": [{"path": "a.mkv"}], "auto_sync": "yes"}
    with pytest.raises(Exception, match="auto_sync: attendu boolean"):
        validate_job_contract(job_err1)

    job_err2 = {"version": 1, "sources": [{"path": "a.mkv"}], "drift_threshold_ms": -5}
    with pytest.raises(Exception, match="drift_threshold_ms: attendu entier > 0"):
        validate_job_contract(job_err2)


def test_cmd_preview_with_calibration_output_json(tmp_path, capsys, monkeypatch):
    calib_file = tmp_path / "calib.json"
    calib_file.write_text(json.dumps({
        "version": 1, "kind": "sync-calibration", "timebase": "donor-ms",
        "segments": [{"start_ms": 0.0, "shift_ms": -125.0}],
        "confidence": 1.0,
    }))

    ref_audio = TrackEntry(0, "audio", "FLAC", "2.0", "eng", "VO", file_id="src0")
    tgt_audio = TrackEntry(0, "audio", "E-AC-3", "5.1", "fre", "VF", file_id="src1")

    src0 = SourceInput(path=tmp_path / "ref.mkv", file_index=0, tracks=[ref_audio])
    src1 = SourceInput(path=tmp_path / "donor.mkv", file_index=1, tracks=[tgt_audio])
    src0.path.touch()
    src1.path.touch()

    monkeypatch.setattr(
        "cli.remux_config.inspect_sources",
        lambda job, conf, opts, log, cli_inputs=None: ([src0, src1], [MagicMock(), MagicMock()], [ref_audio, tgt_audio]),
    )
    orig_which = shutil.which
    monkeypatch.setattr("shutil.which", lambda cmd: orig_which(cmd) or f"/mock/bin/{cmd}")

    parser = build_parser()
    args = parser.parse_args([
        "preview",
        "-i", str(src0.path),
        "-i", str(src1.path),
        "-o", str(tmp_path / "out.mkv"),
        "--calibration", str(calib_file),
        "--sync-mode", "physical",
        "--json",
    ])
    rc = cmd_preview(args, AppConfig(), Logger(fmt="text"))
    assert rc == EXIT_OK

    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip())
    assert payload["valid"] is True
    assert "sync_calibrations" in payload
    assert "1" in payload["sync_calibrations"]
    assert payload["sync_calibrations"]["1"]["segments"][0]["shift_ms"] == -125.0


# =============================================================================
# 9. Tests Détection & Conversion de Cadence en CLI (Auto + Manuel)
# =============================================================================

def test_cli_parser_cadence_options():
    parser = build_parser()

    # Defaults
    args = parser.parse_args(["hybrid", "--ref", "r.mkv", "--donor", "d.mkv", "-o", "out"])
    assert args.cadence_auto is True
    assert args.cadence_method == "auto"

    # Désactivation auto
    args = parser.parse_args(["hybrid", "--ref", "r.mkv", "--donor", "d.mkv", "-o", "out", "--no-cadence-auto"])
    assert args.cadence_auto is False

    # Méthode explicite asetrate
    args = parser.parse_args(["hybrid", "--ref", "r.mkv", "--donor", "d.mkv", "-o", "out", "--cadence-method", "asetrate"])
    assert args.cadence_method == "asetrate"

    # Méthode explicite atempo
    args = parser.parse_args(["hybrid", "--ref", "r.mkv", "--donor", "d.mkv", "-o", "out", "--cadence-method", "atempo"])
    assert args.cadence_method == "atempo"

    # sync-scan options
    args = parser.parse_args(["sync-scan", "--ref", "r.mkv", "--target", "t.mkv", "--cadence-method", "asetrate"])
    assert args.cadence_auto is True
    assert args.cadence_method == "asetrate"


def test_prepare_pair_auto_cadence_detection(tmp_path, monkeypatch):
    ref_file = tmp_path / "ref.mkv"
    donor_file = tmp_path / "donor.mkv"
    ref_file.touch()
    donor_file.touch()

    ref_video = TrackEntry(0, "video", "HEVC", "1080p", "und", "", frame_rate="24000/1001", file_id="src0")
    ref_audio = TrackEntry(1, "audio", "EAC3", "5.1", "eng", "", file_id="src0")
    donor_video = TrackEntry(0, "video", "H264", "1080p", "und", "", frame_rate="25/1", file_id="src1")
    donor_audio = TrackEntry(1, "audio", "AC3", "5.1", "fre", "", file_id="src1")

    src0 = SourceInput(path=ref_file, file_index=0, tracks=[ref_video, ref_audio])
    src1 = SourceInput(path=donor_file, file_index=1, tracks=[donor_video, donor_audio])

    fake_config = RemuxConfig(
        sources=[src0, src1],
        output=tmp_path / "out.mkv",
        track_order=[(0, 0, "src0"), (0, 1, "src0"), (1, 0, "src1"), (1, 1, "src1")],
        sync_mode="container",  # volontairement container pour tester l'upgrade auto
    )

    monkeypatch.setattr("cli.hybrid.build_remux_config", lambda job, conf, opts, log: fake_config)

    scanned_args = {}

    def fake_scan(ref_t, don_t, **kwargs):
        scanned_args.update(kwargs)
        return SyncCalibration(
            segments=(SyncSegment(0, -42.0),),
            confidence=0.95,
            cadence_mismatch=kwargs.get("cadence_mismatch"),
            cadence_audio_method=kwargs.get("cadence_audio_method", "atempo"),
        )

    mock_scanner = MagicMock()
    mock_scanner.scan = fake_scan
    monkeypatch.setattr("cli.hybrid.scanner", lambda args, conf: mock_scanner)

    parser = build_parser()
    args = parser.parse_args([
        "hybrid",
        "--ref", str(ref_file),
        "--donor", str(donor_file),
        "-o", str(tmp_path / "out"),
        "--cadence-method", "asetrate",
    ])

    pair = HybridPair(ref_file, donor_file, 1, 2)
    result, calib = prepare_pair(pair, args, AppConfig(), Logger(fmt="text"))

    # Vérification que le mismatch de cadence a bien été détecté et passé au scanner
    assert scanned_args["cadence_mismatch"] is not None
    assert scanned_args["cadence_mismatch"].speed_ratio == "24000/25025"
    assert scanned_args["cadence_audio_method"] == "asetrate"

    # Vérification que sync_mode a automatiquement été promu à physical
    assert result.sync_mode == "physical"
    assert "1" in result.sync_calibrations
    assert result.sync_calibrations["1"]["cadence_mismatch"]["speed_ratio"] == "24000/25025"
    assert result.sync_calibrations["1"]["cadence_audio_method"] == "asetrate"


def test_perform_dynamic_sync_cadence_detection(tmp_path):
    ref_video = TrackEntry(0, "video", "HEVC", "1080p", "und", "", frame_rate="24000/1001", file_id="src0")
    ref_audio = TrackEntry(1, "audio", "AAC", "2.0", "fra", "", file_id="src0")
    donor_video = TrackEntry(0, "video", "H264", "1080p", "und", "", frame_rate="25/1", file_id="src1")
    donor_audio = TrackEntry(1, "audio", "AAC", "2.0", "fra", "", file_id="src1")

    ref_path = tmp_path / "ref.mkv"
    donor_path = tmp_path / "donor.mkv"
    ref_path.touch()
    donor_path.touch()

    src0 = SourceInput(path=ref_path, file_index=0, tracks=[ref_video, ref_audio])
    src1 = SourceInput(path=donor_path, file_index=1, tracks=[donor_video, donor_audio])

    captured_kwargs = {}

    class MockAudioScanner:
        def __init__(self, *a, **kw): pass
        def scan(self, ref_track, tgt_track, **kwargs):
            captured_kwargs.update(kwargs)
            return SyncCalibration(
                segments=(SyncSegment(0, 50.0),),
                confidence=0.98,
                cadence_mismatch=kwargs.get("cadence_mismatch"),
                cadence_audio_method=kwargs.get("cadence_audio_method", "atempo"),
            )

    import cli.hybrid
    orig_scanner = cli.hybrid.AudioSyncScanner
    cli.hybrid.AudioSyncScanner = MockAudioScanner
    try:
        parser = build_parser()
        args = parser.parse_args(["remux", "-i", str(ref_path), "-o", str(tmp_path / "out.mkv"), "--auto-sync", "--cadence-method", "atempo"])
        options = CommonOptions.from_namespace(args)

        calib = perform_dynamic_sync([src0, src1], [ref_video, ref_audio, donor_video, donor_audio], AppConfig(), options, Logger(fmt="text"))

        assert calib is not None
        assert captured_kwargs["cadence_mismatch"] is not None
        assert captured_kwargs["cadence_mismatch"].speed_ratio == "24000/25025"
        assert captured_kwargs["cadence_audio_method"] == "atempo"
        assert calib.cadence_mismatch is not None
    finally:
        cli.hybrid.AudioSyncScanner = orig_scanner


def test_cmd_sync_scan_audio_with_cadence_detection(tmp_path, monkeypatch, capsys):
    ref_file = tmp_path / "ref.mkv"
    tgt_file = tmp_path / "tgt.mkv"
    ref_file.touch()
    tgt_file.touch()

    class MockInspector:
        def __init__(self, *a, **kw): pass
        def inspect(self, p):
            mock_info = MagicMock()
            if p == ref_file:
                v = MagicMock()
                v.frame_rate = "24000/1001"
                mock_info.video_tracks = [v]
            else:
                v = MagicMock()
                v.frame_rate = "25/1"
                mock_info.video_tracks = [v]
            return mock_info

    monkeypatch.setattr("core.inspector.FileInspector", MockInspector)
    monkeypatch.setattr("cli.hybrid.detect_stream_kind", lambda *a, **kw: "audio")

    def fake_scan(ref_t, don_t, **kwargs):
        return SyncCalibration(
            segments=(SyncSegment(0, 100.0),),
            confidence=0.99,
            cadence_mismatch=kwargs.get("cadence_mismatch"),
            cadence_audio_method=kwargs.get("cadence_audio_method", "atempo"),
        )

    mock_scanner = MagicMock()
    mock_scanner.scan = fake_scan
    monkeypatch.setattr("cli.hybrid.scanner", lambda args, conf: mock_scanner)

    out_json = tmp_path / "calib.json"
    parser = build_parser()
    args = parser.parse_args([
        "sync-scan",
        "--ref", str(ref_file),
        "--target", str(tgt_file),
        "--output-json", str(out_json),
        "--cadence-method", "asetrate",
    ])

    rc = cmd_sync_scan(args, AppConfig(), Logger(fmt="text"))
    assert rc == EXIT_OK

    data = json.loads(out_json.read_text(encoding="utf-8"))
    assert "cadence_mismatch" in data
    assert data["cadence_mismatch"]["speed_ratio"] == "24000/25025"
    assert data["cadence_audio_method"] == "asetrate"



