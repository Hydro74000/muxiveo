"""Décalages et hauteurs projetés par piste, du plan Hybrid à l'assemblage."""
from dataclasses import replace

import pytest

from core.workflows.cadence import detect_cadence_from_metadata
from core.workflows.common.sync_rewrite import SyncRewriteService
from core.workflows.common.track_types import TrackOffset
from core.workflows.encode.models import EncodeConfig, EncodeError, VideoEncodeSettings
from core.workflows.encode.planning.offsets import track_time_offset_calibration_lookup, track_time_offset_lookup
from core.workflows.encode.remux_bridge import merge_remux_into_encode_config
from core.workflows.physical_sync import preparation_commands, track_calibration
from core.workflows.remux_mapping import resolve_mapped_tracks
from core.workflows.remux_models import RemuxConfig, SourceInput, TrackEntry
from core.workflows.sync_calibration import SyncCalibration, SyncSegment


@pytest.mark.parametrize("offset", [-2000, 0, 2000])
@pytest.mark.parametrize("cuts,cadence", [(False, False), (True, False), (False, True), (True, True)])
def test_projection_preserves_subtitle_times_and_audio_pitch(offset, cuts, cadence):
    segments = (SyncSegment(0, offset), SyncSegment(10000, offset + 400)) if cuts else (SyncSegment(0, offset),)
    calibration = SyncCalibration(segments, cadence_pitch_factor=0.96,
                                  cadence_mismatch=detect_cadence_from_metadata("24000/1001", "25/1") if cadence else None,
                                  cadence_audio_method="asetrate")
    subtitle = calibration.for_track("subtitle")
    assert calibration.for_track("audio") is calibration
    assert subtitle.audio_pitch_factor == 1.0
    assert subtitle.segments == calibration.segments
    for position in (0, 5000, 12000):
        assert subtitle.output_time_ms(position) == pytest.approx(calibration.output_time_ms(position))
    assert calibration.cadence_pitch_factor == 0.96


@pytest.mark.parametrize("per_track", [False, True])
@pytest.mark.parametrize("offset", [-2000, 2000])
def test_bridge_and_native_planning_keep_the_offset_of_a_pitch_calibration(tmp_path, per_track, offset):
    source = tmp_path / "donor.mkv"
    pitch = SyncCalibration((SyncSegment(0, offset),), cadence_pitch_factor=0.96)
    subtitle = TrackEntry(1, "subtitle", "subrip", "", "fra", "",
                          sync_calibration=pitch.to_dict() if per_track else None)
    remux = RemuxConfig([SourceInput(source, 0, [subtitle])], tmp_path / "out.mkv",
                        [(0, 1, subtitle.entry_id)], sync_mode="physical", sync_subtitles="mirror",
                        sync_calibrations={"0": pitch.to_dict()})
    encode = EncodeConfig(source, remux.output, video=VideoEncodeSettings())
    merged = merge_remux_into_encode_config(encode, remux)
    assert len(merged.track_time_offsets) == 1
    track_offset = merged.track_time_offsets[0]
    assert track_offset.offset_ms == offset and track_offset.calibration is None
    assert subtitle.time_shift_ms == 0
    # Un document sauvegardé avec le payload original suit la même projection au rechargement.
    restored = replace(merged, track_time_offsets=[TrackOffset("subtitle", source, 1, 0, calibration=pitch.to_dict())])
    assert track_time_offset_lookup(restored)[("subtitle", source, 1)] == offset
    assert track_time_offset_calibration_lookup(restored) == {}


def test_individual_subtitle_offset_overrides_source_calibration(tmp_path):
    pitch = SyncCalibration((SyncSegment(0, 2000),), cadence_pitch_factor=0.96)
    track = TrackEntry(1, "subtitle", "subrip", "", "fra", "",
                       sync_calibration=SyncCalibration.linear(-400).to_dict())
    config = RemuxConfig([SourceInput(tmp_path / "in.mkv", 0, [track])], tmp_path / "out.mkv",
                         [(0, 1, track.entry_id)], sync_subtitles="mirror", sync_calibrations={"0": pitch.to_dict()})
    mapped = resolve_mapped_tracks(config)[0]
    assert mapped.track.time_shift_ms == -400
    assert track.time_shift_ms == 0
    assert track_calibration(config, mapped).segments[0].shift_ms == -400


@pytest.mark.parametrize("offset,expected", [(2000, "00:00:12,000"), (-2000, "00:00:08,000")])
def test_subtitle_rewrite_keeps_pitch_calibration_offset(tmp_path, monkeypatch, offset, expected):
    """Seul le transport FFmpeg est simulé ; les événements SRT sont réellement réécrits."""
    service = SyncRewriteService(ffmpeg_bin="ffmpeg")
    rewritten = []

    def run(command, destination, *_args, **_kwargs):
        if destination.suffix == ".srt":
            destination.write_text("1\n00:00:10,000 --> 00:00:11,000\nTexte\n", encoding="utf-8")
        else:
            rewritten.append(next(path for path in tmp_path.glob("*_shifted.srt")).read_text(encoding="utf-8"))
            destination.write_bytes(b"mks")

    monkeypatch.setattr(service, "_run_checked", run)
    prepared = service.maybe_materialize(source_path=tmp_path / "in.mkv", stream_index=1,
                                         track_type="subtitle", codec="subrip", offset_ms=0,
                                         tmp_dir=tmp_path, input_idx=0,
                                         calibration=SyncCalibration((SyncSegment(0, offset),), cadence_pitch_factor=0.96))
    assert prepared is not None and expected in rewritten[0]


def test_pitch_only_with_no_offset_does_not_rewrite_bitmap_subtitles(tmp_path, monkeypatch):
    service = SyncRewriteService(ffmpeg_bin="ffmpeg")
    monkeypatch.setattr(service, "_run_checked", lambda *_a, **_kw: pytest.fail("Aucune transformation temporelle"))
    assert service.maybe_materialize(source_path=tmp_path / "in.mkv", stream_index=1,
                                     track_type="subtitle", codec="hdmv_pgs_subtitle", offset_ms=0,
                                     tmp_dir=tmp_path, input_idx=0,
                                     calibration=SyncCalibration((SyncSegment(0, 0),), cadence_pitch_factor=0.96)) is None


@pytest.mark.parametrize("payload", [{"segments": []}, {"bad": 1}])
@pytest.mark.parametrize("lookup", [track_time_offset_lookup, track_time_offset_calibration_lookup])
def test_invalid_calibration_is_refused_instead_of_reduced_to_an_offset(tmp_path, payload, lookup):
    source = tmp_path / "in.mkv"
    config = EncodeConfig(source, tmp_path / "out.mkv", video=VideoEncodeSettings(),
                          track_time_offsets=[TrackOffset("subtitle", source, 1, 500, calibration=payload)])
    with pytest.raises(EncodeError, match="Calibration de synchronisation invalide"):
        lookup(config)


@pytest.mark.parametrize("mode", ["physical", "container"])
def test_disabled_subtitle_mirror_does_not_inherit_source_offset(tmp_path, mode):
    source = tmp_path / "in.mkv"
    subtitle = TrackEntry(1, "subtitle", "hdmv_pgs_subtitle", "", "fra", "")
    config = RemuxConfig([SourceInput(source, 0, [subtitle])], tmp_path / "out.mkv",
                         [(0, 1, subtitle.entry_id)], sync_mode=mode, sync_subtitles="none",
                         sync_calibrations={"0": SyncCalibration((SyncSegment(0, 2000),), cadence_pitch_factor=0.96).to_dict()})
    mapped = resolve_mapped_tracks(config)[0]
    assert mapped.track.time_shift_ms == 0 and track_calibration(config, mapped) == SyncCalibration.linear(0)
    assert not preparation_commands(config, tmp_path, "ffmpeg")
    merged = merge_remux_into_encode_config(EncodeConfig(source, config.output, video=VideoEncodeSettings()), config)
    assert not merged.track_time_offsets
