"""Réécriture de synchro du Remux : règles partagées par les backends FFmpeg et natif (sans FFmpeg)."""
from __future__ import annotations

from pathlib import Path

import pytest

from core.workflows.common.sync_rewrite import SyncRewritePreparedInput
from core.workflows.remux_mapping import resolve_mapped_tracks
from core.workflows.remux_models import RemuxConfig, RemuxError, SourceInput, TrackEntry
from core.workflows.remux_plan import native_capability_reasons
from core.workflows.remux_sync_rewrite import (
    MaterializedRewrite,
    NativeRewrite,
    SyncRewriteIntent,
    SyncRewriteSettings,
    ffmpeg_input_origin,
    finalize_native_rewrite_shifts,
    materialize_sync_rewrites,
    plan_sync_rewrites,
    rewrite_input_offsets,
)
from core.workflows.sync_calibration import SyncCalibration


def _config(tmp_path: Path, *, offset: int = 700, calibration: dict | None = None) -> RemuxConfig:
    video = tmp_path / "video.mkv"
    foreign = tmp_path / "foreign.mkv"
    video.write_bytes(b"")
    foreign.write_bytes(b"")
    v = TrackEntry(0, "video", "HEVC", "", "und", "")
    a = TrackEntry(0, "audio", "EAC3", "5.1 · 640 kb/s", "fre", "", time_shift_ms=offset)
    s = TrackEntry(1, "subtitle", "SubRip", "", "fre", "", time_shift_ms=offset)
    return RemuxConfig(
        sources=[SourceInput(video, 0, [v]), SourceInput(foreign, 1, [a, s])],
        output=tmp_path / "out.mkv",
        track_order=[(0, 0, v.entry_id), (1, 0, a.entry_id), (1, 1, s.entry_id)],
        sync_calibrations={"1": calibration} if calibration else {},
    )


def test_intents_cover_shifted_audio_and_subtitles(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _tracks, intents = plan_sync_rewrites(config, resolve_mapped_tracks(config), SyncRewriteSettings(enabled=True))
    assert [(intent.mapped_track.track.track_type, intent.offset_ms) for intent in intents] == [
        ("audio", 700), ("subtitle", 700),
    ]


def test_calibration_without_rewrite_is_refused_for_both_backends(tmp_path: Path) -> None:
    calibration = SyncCalibration.from_dict({"segments": [
        {"start_ms": 0, "shift_ms": 100}, {"start_ms": 10_000, "shift_ms": 900},
    ]}).to_dict()
    config = _config(tmp_path, offset=0, calibration=calibration)
    with pytest.raises(RemuxError, match="réécriture sync réelle est requise"):
        plan_sync_rewrites(config, resolve_mapped_tracks(config), SyncRewriteSettings(enabled=False))
    _tracks, intents = plan_sync_rewrites(config, resolve_mapped_tracks(config), SyncRewriteSettings(enabled=True))
    assert intents and all(intent.required for intent in intents if intent.mapped_track.track.is_audio)


def test_required_rewrite_refused_by_the_probe_is_an_error(tmp_path: Path) -> None:
    config = _config(tmp_path)
    mapped = resolve_mapped_tracks(config)[1]
    calibration = SyncCalibration.linear(0)

    class Refusing:
        def maybe_materialize(self, **_kwargs):
            return None

    optional = SyncRewriteIntent(1, mapped, 700, None)
    assert materialize_sync_rewrites(config, [optional], service=Refusing(), tmp_dir=tmp_path,  # type: ignore[arg-type]
                                     first_input_idx=2) == []
    required = SyncRewriteIntent(1, mapped, 0, calibration)
    with pytest.raises(RemuxError, match="non applicable"):
        materialize_sync_rewrites(config, [required], service=Refusing(), tmp_dir=tmp_path,  # type: ignore[arg-type]
                                  first_input_idx=2)


def test_native_blockers_for_rewrites_are_gone(tmp_path: Path) -> None:
    config = _config(tmp_path)
    tracks = [
        source if source.file_index == 0 else SourceInput(
            source.path, source.file_index,
            [track.__class__(**{**track.__dict__, "sync_rewrite_label": "Sync réelle · audio réencodé"})
             for track in source.tracks],
        )
        for source in config.sources
    ]
    reasons = native_capability_reasons(RemuxConfig(
        sources=tracks, output=config.output, track_order=config.track_order,
    ))
    assert not any("synchronisation" in reason for reason in reasons)


@pytest.mark.parametrize("format_name,retained,expected", [
    ("matroska,webm", [1], 5.0),
    ("mpegts", [1], 606.39),
    ("mpegts", [], 601.39),
    ("mpeg", [0, 1], 601.39),
])
def test_ffmpeg_input_origin_follows_correct_input_start_times(format_name, retained, expected) -> None:
    payload = {
        "format": {"start_time": "601.39" if "mpeg" in format_name else "5.0", "format_name": format_name},
        "streams": [{"index": 0, "start_time": "601.39"}, {"index": 1, "start_time": "606.39"}],
    }
    assert ffmpeg_input_origin(payload, retained) == pytest.approx(expected)


def _materialized(path: Path, track_type: str, source: Path) -> MaterializedRewrite:
    mapped = type("Mapped", (), {"source_path": source, "track": TrackEntry(0, track_type, "EAC3", "", "fre", "")})()
    return MaterializedRewrite(
        SyncRewriteIntent(0, mapped, 700, None),  # type: ignore[arg-type]
        SyncRewritePreparedInput(path, 2, track_type, "eac3", "Sync réelle", ffmpeg_timestamp_offset_s=0.005,
                                 source_timestamp_offset_s=-0.011),
    )


def test_input_offsets_follow_the_historical_ffmpeg_rule(tmp_path: Path) -> None:
    source, other = tmp_path / "foreign.mkv", tmp_path / "video.mkv"
    artifact = tmp_path / "rewrite.mka"
    probed: list[Path] = []
    offsets = rewrite_input_offsets(
        {}, [_materialized(artifact, "audio", source)], [other, source],
        lambda path: probed.append(path) or 0.021,
    )
    assert offsets == {artifact: 0.005, source: -0.011, other: 0.021}
    assert probed == [other]
    # Sous-titres seuls : aucun pré-roll sondé.
    assert rewrite_input_offsets({}, [_materialized(artifact, "subtitle", source)], [other], lambda _p: 1.0) == {
        artifact: 0.005,
    }


def test_final_shift_is_relative_minus_native_origin(tmp_path: Path) -> None:
    config = RemuxConfig(
        sources=[SourceInput(tmp_path / "a.mka", 5, [TrackEntry(0, "audio", "EAC3", "", "fre", "")])],
        output=tmp_path / "out.mkv", track_order=[(5, 0)],
    )
    rewrite = NativeRewrite(5, 1, tmp_path / "foreign.ts", relative_s=0.016, ffmpeg_origin_s=600.0)
    assert finalize_native_rewrite_shifts(config, [rewrite], lambda _r: 0.0).sources[0].tracks[0].time_shift_ms == 16
    measured = finalize_native_rewrite_shifts(config, [rewrite], lambda _r: -0.004)
    assert measured.sources[0].tracks[0].time_shift_ms == 20
    # Source d'origine sans route native : placée comme FFmpeg place sa source.
    orphan = finalize_native_rewrite_shifts(config, [rewrite], lambda _r: None)
    assert orphan.sources[0].tracks[0].time_shift_ms == round((0.016 - 600.0) * 1000)


@pytest.mark.parametrize("source,target,preserve,advanced,offset,expected", [
    ("E-AC-3", "AC3", False, False, 700, "ac3"),       # simple : directement la variante
    ("E-AC-3", "E-AC-3", True, False, 700, "eac3"),    # sans variante : codec de la source
    ("AC3", "FLAC", False, False, 700, None),          # cible hors réécriture simple : non réécrite
    ("FLAC", "AC3", False, True, 700, "flac"),         # sans perte : FLAC, variante ensuite
    ("Vorbis", "FLAC", False, True, 700, "flac"),      # avec perte → FLAC demandé : produit directement
    ("MP3", "AAC", False, True, 700, "aac"),           # avec perte → AAC : un seul encodage avec perte
    ("MP3", "Opus", False, True, 700, "flac"),         # cible hors contrat : intermédiaire FLAC
    ("MP3", "AAC", False, False, 700, None),           # audio avancé désactivé
    ("DTS", "DTS", True, True, -300, "dts"),           # copie / coupe
    ("DTS", "DTS", True, True, 300, None),             # copie / coupe : retard positif refusé
])
def test_planned_rewrite_codec_matches_the_service_rule(source, target, preserve, advanced, offset, expected) -> None:
    from core.workflows.common.sync_rewrite import planned_rewrite_codec

    assert planned_rewrite_codec(
        source, target, preserve_source=preserve, advanced_audio=advanced, offset_ms=offset,
    ) == expected
