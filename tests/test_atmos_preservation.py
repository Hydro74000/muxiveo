"""Atmos doit rester copié lors des opérations de synchronisation implicites."""
from pathlib import Path

import pytest

from core.workflows.common.sync_rewrite import SyncRewriteService
from core.workflows.physical_sync import preparation_commands
from core.workflows.remux_models import RemuxConfig, RemuxError, SourceInput, TrackEntry
from core.workflows.sync_calibration import SyncCalibration, SyncSegment


@pytest.mark.parametrize("codec,display_info,title", [
    ("EAC3-JOC", "5.1 640 kbps", ""),
    ("E-AC-3 JOC", "5.1 640 kbps", ""),
    ("EAC3", "5.1 JOC", ""),
    ("EAC3", "5.1 640 kbps", "English Atmos"),
    ("TRUEHD Atmos", "7.1", ""),
])
@pytest.mark.parametrize("offset_ms", [-100, 100])
def test_physical_sync_rejects_object_audio_markers(tmp_path, codec, display_info, title, offset_ms):
    track = TrackEntry(0, "audio", codec, display_info, "fra", title, time_shift_ms=offset_ms)
    config = RemuxConfig(
        [SourceInput(tmp_path / "source.mkv", 0, [track])],
        tmp_path / "out.mkv", [(0, 0)], sync_mode="physical",
    )
    with pytest.raises(RemuxError, match="immersif"):
        preparation_commands(config, tmp_path, "ffmpeg")


@pytest.mark.parametrize("codec,profile", [
    ("truehd", "Dolby TrueHD + Dolby Atmos"),
    ("eac3", "Dolby Digital Plus + Dolby Atmos"),
    ("eac3", "E-AC-3 JOC"),
])
@pytest.mark.parametrize("advanced", [False, True])
@pytest.mark.parametrize("offset_ms", [-120, 120])
def test_sync_rewrite_never_reencodes_atmos_detected_from_profile(
    tmp_path, monkeypatch, codec, profile, advanced, offset_ms,
):
    service = SyncRewriteService(ffmpeg_bin="ffmpeg", advanced_audio_enabled=advanced)
    # Aucun indice dans le titre : la décision doit utiliser le profil sondé.
    monkeypatch.setattr(service, "_probe_stream", lambda *_args: {
        "codec_name": codec, "profile": profile, "channels": 8, "tags": {},
    })
    commands = []

    def run(command, destination, *_args, **_kwargs):
        commands.append(command)
        Path(destination).write_bytes(b"prepared")

    monkeypatch.setattr(service, "_run_checked", run)
    prepared = service.maybe_materialize(
        source_path=tmp_path / "source.mkv", stream_index=1, track_type="audio",
        codec=codec, offset_ms=offset_ms, tmp_dir=tmp_path, input_idx=2,
    )
    if advanced and offset_ms < 0:
        assert prepared is not None
        assert len(commands) == 1
        command = commands[0]
        assert command[command.index("-c:a") + 1] == "copy"
        assert "-af" not in command and "-filter_complex" not in command
        assert "truehd_core" not in command
    else:
        assert prepared is None
        assert commands == []


@pytest.mark.parametrize("codec,profile", [
    ("truehd", "Dolby TrueHD + Dolby Atmos"),
    ("eac3", "E-AC-3 JOC"),
])
def test_segment_calibration_cannot_reencode_atmos(tmp_path, monkeypatch, codec, profile):
    service = SyncRewriteService(ffmpeg_bin="ffmpeg", advanced_audio_enabled=True)
    monkeypatch.setattr(service, "_probe_stream", lambda *_args: {
        "codec_name": codec, "profile": profile, "channels": 8,
    })

    def forbidden(*_args, **_kwargs):
        pytest.fail("Une calibration Atmos ne doit lancer aucune transformation audio")

    monkeypatch.setattr(service, "_run_checked", forbidden)
    with pytest.raises(RemuxError, match="multi-segments"):
        service.maybe_materialize(
            source_path=tmp_path / "source.mkv", stream_index=1, track_type="audio",
            codec=codec, offset_ms=120, tmp_dir=tmp_path, input_idx=2,
            calibration=SyncCalibration((SyncSegment(0, 120), SyncSegment(1000, 0))),
        )
