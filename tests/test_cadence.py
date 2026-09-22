"""Tests unitaires pour la détection et la conversion de cadence (PAL 25 FPS ↔ Cinéma 23.976/24 FPS)."""
import math
from pathlib import Path
import pytest

from core.workflows.cadence import (
    CadenceAudioMethod,
    CadenceMismatch,
    CadenceType,
    build_cadence_audio_filter,
    detect_cadence_from_acoustic_samples,
    detect_cadence_from_metadata,
    parse_framerate,
)
from core.workflows.physical_sync import audio_filter, preparation_commands
from core.workflows.remux_models import RemuxConfig, RemuxError, SourceInput, TrackEntry
from core.workflows.subtitle_sync import parse_time, shift_text
from core.workflows.sync_calibration import SyncCalibration, SyncSegment


# ── 1. parse_framerate ────────────────────────────────────────────────────────

def test_parse_framerate_numbers():
    assert parse_framerate(25) == 25.0
    assert parse_framerate(23.976) == 23.976
    assert parse_framerate(0) is None
    assert parse_framerate(-1) is None
    assert parse_framerate(None) is None


def test_parse_framerate_fractions():
    val = parse_framerate("24000/1001")
    assert val is not None
    assert math.isclose(val, 23.976023976, rel_tol=1e-5)

    assert parse_framerate("25/1") == 25.0
    assert parse_framerate("24/1") == 24.0
    assert parse_framerate("0/1") is None
    assert parse_framerate("1/0") is None


def test_parse_framerate_strings_with_fps():
    assert parse_framerate("23.976 fps") == 23.976
    assert parse_framerate("25.000 FPS") == 25.0
    assert parse_framerate(" 24 fps ") == 24.0
    assert parse_framerate("invalid") is None
    assert parse_framerate("") is None


# ── 2. detect_cadence_from_metadata (Méthode A) ───────────────────────────────

def test_detect_cadence_pal_to_film_23976():
    mismatch = detect_cadence_from_metadata(master_fps_val="24000/1001", donor_fps_val="25/1")
    assert mismatch is not None
    assert mismatch.cadence_type == CadenceType.PAL_TO_FILM_23976
    assert mismatch.source_fps == 25.0
    assert mismatch.target_fps == 23.976
    assert math.isclose(mismatch.speed_factor, 24000.0 / 25000.0)
    assert mismatch.detection_method == "metadata"
    assert "25" in mismatch.description and "23.976" in mismatch.description


def test_detect_cadence_pal_to_film_24():
    mismatch = detect_cadence_from_metadata(master_fps_val=24.0, donor_fps_val=25.0)
    assert mismatch is not None
    assert mismatch.cadence_type == CadenceType.PAL_TO_FILM_24
    assert mismatch.source_fps == 25.0
    assert mismatch.target_fps == 24.0
    assert math.isclose(mismatch.speed_factor, 24.0 / 25.0)


def test_detect_cadence_film_to_pal():
    mismatch = detect_cadence_from_metadata(master_fps_val="25.0", donor_fps_val="23.976")
    assert mismatch is not None
    assert mismatch.cadence_type == CadenceType.FILM_23976_TO_PAL
    assert math.isclose(mismatch.speed_factor, 25000.0 / 24000.0)

    mismatch_24 = detect_cadence_from_metadata(master_fps_val=25.0, donor_fps_val=24.0)
    assert mismatch_24 is not None
    assert mismatch_24.cadence_type == CadenceType.FILM_24_TO_PAL
    assert math.isclose(mismatch_24.speed_factor, 25.0 / 24.0)


def test_detect_cadence_matching_or_missing_fps():
    # Même framerate : aucun décalage
    assert detect_cadence_from_metadata("23.976", "23.976") is None
    assert detect_cadence_from_metadata(25.0, 25.0) is None
    # Métadonnées manquantes
    assert detect_cadence_from_metadata(None, "25.0") is None
    assert detect_cadence_from_metadata("23.976", None) is None


# ── 3. detect_cadence_from_acoustic_samples (Méthode B) ───────────────────────

def test_detect_cadence_acoustic_pal_to_film():
    # Pente théorique PAL 25 -> 23.976 : ~ +0.041666 à +0.04271
    # Dérive sur 30 minutes (1800 s = 1 800 000 ms)
    # y(t) = 150 + 0.04167 * t
    samples = []
    times_s = [0, 300, 600, 900, 1200, 1500, 1800]
    for ts in times_s:
        t_ms = ts * 1000.0
        shift = 150.0 + 0.04167 * t_ms
        samples.append({"start_ms": t_ms, "shift_ms": shift, "confidence": 0.85})

    mismatch = detect_cadence_from_acoustic_samples(samples)
    assert mismatch is not None
    assert mismatch.cadence_type == CadenceType.PAL_TO_FILM_23976
    assert mismatch.detection_method == "acoustic_slope"
    assert mismatch.confidence >= 0.95


def test_detect_cadence_acoustic_film_to_pal():
    # Pente inverse 23.976 -> PAL 25 : ~ -0.040
    samples = []
    times_s = [0, 200, 400, 600, 800, 1000]
    for ts in times_s:
        t_ms = ts * 1000.0
        shift = 50.0 - 0.040 * t_ms
        samples.append({"start_ms": t_ms, "shift_ms": shift, "confidence": 0.90})

    mismatch = detect_cadence_from_acoustic_samples(samples)
    assert mismatch is not None
    assert mismatch.cadence_type == CadenceType.FILM_23976_TO_PAL
    assert mismatch.detection_method == "acoustic_slope"


def test_detect_cadence_acoustic_no_drift_or_cut_jump():
    # Dérive constante (synchronisation normale)
    samples_flat = [
        {"start_ms": ts * 1000.0, "shift_ms": 120.0, "confidence": 0.9}
        for ts in [0, 200, 400, 600, 800, 1000]
    ]
    assert detect_cadence_from_acoustic_samples(samples_flat) is None

    # Coupure publicitaire (saut abrupt à 500s de +3000 ms) : pas une dérive linéaire
    samples_cut = [
        {"start_ms": 0.0, "shift_ms": 100.0, "confidence": 0.9},
        {"start_ms": 200000.0, "shift_ms": 100.0, "confidence": 0.9},
        {"start_ms": 400000.0, "shift_ms": 100.0, "confidence": 0.9},
        {"start_ms": 600000.0, "shift_ms": 3100.0, "confidence": 0.9},
        {"start_ms": 800000.0, "shift_ms": 3100.0, "confidence": 0.9},
        {"start_ms": 1000000.0, "shift_ms": 3100.0, "confidence": 0.9},
    ]
    assert detect_cadence_from_acoustic_samples(samples_cut) is None


# ── 4. build_cadence_audio_filter ─────────────────────────────────────────────

def test_build_cadence_audio_filter_atempo():
    mismatch = CadenceMismatch(
        cadence_type=CadenceType.PAL_TO_FILM_23976,
        source_fps=25.0,
        target_fps=23.976,
        speed_factor=24000.0 / 25000.0,
    )
    f_atempo = build_cadence_audio_filter(mismatch, CadenceAudioMethod.ATEMPO)
    assert f_atempo == "atempo=24000/25000"

    mismatch_24 = CadenceMismatch(
        cadence_type=CadenceType.PAL_TO_FILM_24,
        source_fps=25.0,
        target_fps=24.0,
        speed_factor=24.0 / 25.0,
    )
    assert build_cadence_audio_filter(mismatch_24, "atempo") == "atempo=24/25"


def test_build_cadence_audio_filter_asetrate():
    mismatch = CadenceMismatch(
        cadence_type=CadenceType.PAL_TO_FILM_23976,
        source_fps=25.0,
        target_fps=23.976,
        speed_factor=24000.0 / 25000.0,
    )
    f_asetrate = build_cadence_audio_filter(mismatch, CadenceAudioMethod.ASETRATE, sample_rate=48000)
    # 48000 * (24000/25000) = 46080
    assert f_asetrate == "asetrate=46080,aresample=48000"


# ── 5. SyncCalibration serialization & time stretch ───────────────────────────

def test_sync_calibration_roundtrip_with_cadence():
    mismatch = CadenceMismatch(
        cadence_type=CadenceType.PAL_TO_FILM_23976,
        source_fps=25.0,
        target_fps=23.976,
        speed_factor=24000.0 / 25000.0,
        description="PAL 25 → 23.976 FPS (+4,1 %)",
    )
    calib = SyncCalibration(
        segments=(SyncSegment(0.0, 150.0),),
        confidence=0.98,
        cadence_mismatch=mismatch,
        cadence_audio_method="atempo",
    )

    data = calib.to_dict()
    assert "cadence_mismatch" in data
    assert data["cadence_mismatch"]["cadence_type"] == "pal_to_film_23976"
    assert data["cadence_audio_method"] == "atempo"

    restored = SyncCalibration.from_dict(data)
    assert restored.cadence_mismatch is not None
    assert restored.cadence_mismatch.cadence_type == CadenceType.PAL_TO_FILM_23976
    assert restored.cadence_audio_method == "atempo"
    assert any("PAL 25 → 23.976" in line for line in restored.summary_lines())


def test_sync_calibration_intervals_time_stretch():
    mismatch = CadenceMismatch(
        cadence_type=CadenceType.PAL_TO_FILM_23976,
        source_fps=25.0,
        target_fps=23.976,
        speed_factor=24000.0 / 25000.0,  # stretch ratio = 25000 / 24000 = 1.0416667
    )
    calib = SyncCalibration(
        segments=(SyncSegment(0.0, 100.0),),
        cadence_mismatch=mismatch,
    )

    # Intervalle de 60s à 65s dans le donneur PAL (60 000 ms -> 65 000 ms)
    # Après étirement : 60 000 * 25/24 = 62 500 ms + shift 100 = 62 600 ms
    # 65 000 * 25/24 = 67 708.33 ms + shift 100 = 67 808.33 ms
    intervals = list(calib.intervals(60_000.0, 65_000.0))
    assert len(intervals) == 1
    start, end = intervals[0]
    assert math.isclose(start, 62_600.0, abs_tol=1.0)
    assert math.isclose(end, 67_808.33, abs_tol=1.0)


# ── 6. Subtitle synchronization with cadence stretch ──────────────────────────

def test_subtitle_shift_with_cadence_mismatch():
    srt_text = """1
00:01:00,000 --> 00:01:05,000
Bonjour tout le monde !
"""
    mismatch = CadenceMismatch(
        cadence_type=CadenceType.PAL_TO_FILM_23976,
        source_fps=25.0,
        target_fps=23.976,
        speed_factor=24000.0 / 25000.0,
    )
    calib = SyncCalibration(
        segments=(SyncSegment(0.0, 200.0),),
        cadence_mismatch=mismatch,
    )

    result = shift_text(srt_text, calib, suffix=".srt")
    assert "Bonjour tout le monde !" in result
    # 60s * (25/24) = 62.5s + 0.2s = 62.7s -> 00:01:02,700
    assert "00:01:02,700" in result


# ── 7. physical_sync filter generation with cadence ───────────────────────────

def test_physical_sync_audio_filter_generation():
    mismatch = CadenceMismatch(
        cadence_type=CadenceType.PAL_TO_FILM_23976,
        source_fps=25.0,
        target_fps=23.976,
        speed_factor=24000.0 / 25000.0,
    )
    calib = SyncCalibration(
        segments=(SyncSegment(0.0, 0.0),),
        cadence_mismatch=mismatch,
        cadence_audio_method="atempo",
    )
    graph = audio_filter(calib, crossfade_ms=80)
    assert "atempo=24000/25000" in graph
    assert "[cadence_in]" in graph


def test_physical_sync_commands_forces_reencode_on_cadence():
    mismatch = CadenceMismatch(
        cadence_type=CadenceType.PAL_TO_FILM_23976,
        source_fps=25.0,
        target_fps=23.976,
        speed_factor=24000.0 / 25000.0,
    )
    calib = SyncCalibration(
        segments=(SyncSegment(0.0, 0.0),),  # Décalage 0 ms : sans cadence ce serait copié
        cadence_mismatch=mismatch,
    )
    track = TrackEntry(
        mkv_tid=1,
        track_type="audio",
        codec="EAC3",
        display_info="6 canaux 640 kbps",
        language="fre",
        title="VF PAL",
    )
    config = RemuxConfig(
        sources=[
            SourceInput(Path("dummy_master.mkv"), 0, []),
            SourceInput(Path("dummy_donor.mkv"), 1, [track]),
        ],
        output=Path("out.mkv"),
        track_order=[(1, 1, track.entry_id)],
        sync_mode="physical",
        sync_calibrations={"1": calib.to_dict()},
    )
    cmds = preparation_commands(config, Path("/tmp"), "ffmpeg")
    assert len(cmds) == 1
    _, _, cmd, _ = cmds[0]
    # Doit utiliser -filter_complex avec réencodage (pas -c:a copy)
    assert "-filter_complex" in cmd
    assert "-c:a" in cmd
    assert "copy" not in cmd


# ── 8. audio_sync_scan with acoustic slope detection fallback ─────────────────

def test_audio_sync_scanner_detects_acoustic_cadence(monkeypatch):
    from core.workflows.audio_sync import AudioSyncTrack
    from core.workflows.audio_sync_scan import AudioSyncScanner

    scanner = AudioSyncScanner("ffmpeg", "ffprobe")
    # Simuler duration = 1800s (30 min)
    monkeypatch.setattr(scanner, "duration", lambda track: 1800.0)

    # Simuler measure retournant une dérive PAL (+0.04167 ms/ms = ~41.67 ms/s)
    # y = 200.0 + 0.04167 * (p * 1000)
    def fake_measure(ref, don, p, win, cadence_filter=None):
        shift = 200.0 + 0.04167 * (p * 1000.0)
        return shift, 0.92

    monkeypatch.setattr(scanner, "measure", fake_measure)

    logs = []
    calib = scanner.scan(
        AudioSyncTrack(Path("dummy_ref.mkv"), 1),
        AudioSyncTrack(Path("dummy_don.mkv"), 1),
        detect_cuts=False,
        drift_threshold_ms=25,
        log=lambda msg: logs.append(msg),
    )

    assert calib.cadence_mismatch is not None
    assert calib.cadence_mismatch.cadence_type == CadenceType.PAL_TO_FILM_23976
    assert calib.cadence_mismatch.detection_method == "acoustic_slope"
    assert any("Différence de cadence détectée acoustiquement" in line for line in logs)


# ── 9. prepare_matrix_episode with metadata cadence detection ─────────────────

def test_prepare_matrix_episode_with_cadence_auto_apply(monkeypatch, tmp_path):
    from core.workflows.hybrid_matrix import (
        HybridRecipe,
        MatrixEpisode,
        MatrixSource,
        SourceRole,
        prepare_matrix_episode,
    )
    from core.workflows.remux_models import RemuxConfig, SourceInput, TrackEntry

    master_path = tmp_path / "Ep01_master.mkv"
    donor_path = tmp_path / "Ep01_donor.mkv"
    master_path.touch()
    donor_path.touch()

    # Master: 23.976 fps
    master_v = TrackEntry(0, "video", "HEVC", "1920×1080  23.976 fps", "eng", "")
    master_a = TrackEntry(1, "audio", "EAC3", "6 canaux 640 kbps", "eng", "")
    # Donneur: 25.0 fps
    donor_v = TrackEntry(0, "video", "AVC", "1920×1080  25.000 fps", "fre", "")
    donor_a = TrackEntry(1, "audio", "AC3", "6 canaux 448 kbps", "fre", "")

    mock_remux_config = RemuxConfig(
        sources=[
            SourceInput(master_path, 0, [master_v, master_a]),
            SourceInput(donor_path, 1, [donor_v, donor_a]),
        ],
        output=tmp_path / "out.mkv",
        track_order=[],
        sync_mode="container",  # Même si la recette était container, la cadence doit forcer physical
    )

    import cli.remux_config
    monkeypatch.setattr(cli.remux_config, "build_remux_config", lambda *args, **kwargs: mock_remux_config)

    # Mock audio scanner
    from core.workflows.audio_sync_scan import AudioSyncScanner
    dummy_calib = SyncCalibration(
        (SyncSegment(0.0, 50.0),),
        confidence=0.99,
        cadence_mismatch=CadenceMismatch(
            CadenceType.PAL_TO_FILM_23976, 25.0, 23.976, 24000.0 / 25000.0
        ),
    )
    monkeypatch.setattr(AudioSyncScanner, "scan", lambda self, *args, **kwargs: dummy_calib)

    class DummyLogger:
        def emit(self, level, msg):
            pass

    recipe = HybridRecipe(cadence_auto_apply=True, sync_mode="container")
    donor_src = MatrixSource("donor", donor_path, SourceRole.DONOR)
    ep = MatrixEpisode(season=1, episode=1, master_file=master_path, donor_files=[(donor_src, donor_path)])

    class DummyConfig:
        tool_ffmpeg = "ffmpeg"
        tool_ffprobe = "ffprobe"

    cfg, cal = prepare_matrix_episode(
        ep,
        recipe,
        tmp_path / "out",
        DummyConfig(),
        None,
        DummyLogger(),
    )

    assert cal is not None
    assert cal.cadence_mismatch is not None
    assert cal.cadence_mismatch.cadence_type == CadenceType.PAL_TO_FILM_23976
    # La présence de cadence doit avoir basculé sync_mode en "physical"
    assert cfg.sync_mode == "physical"
    assert ep.cadence_mismatch is not None

