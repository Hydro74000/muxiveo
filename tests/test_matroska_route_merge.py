"""Assembleur natif : une lecture par route, ordre de sortie indépendant de la disposition des sources."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from core.matroska import ebml
from core.matroska.assembly import (
    MatroskaAssemblyPlan,
    MatroskaAssemblyTrack,
    assembly_output_contract,
    compile_assembly_plan,
    merge_route_streams,
)
from core.matroska.ids import (
    CODEC_ID_ID,
    SIMPLE_TAG_ID,
    TAG_ID,
    TAG_NAME_ID,
    TAG_STRING_ID,
    TAG_TRACK_UID_ID,
    TAGS_ID,
    TARGETS_ID,
    TRACK_NUMBER_ID,
    TRACK_TYPE_ID,
    TRACK_UID_ID,
)
from core.matroska.mux_plan import MatroskaMuxPacket, MatroskaMuxPlan, MatroskaMuxTrack
from core.matroska.reader import MatroskaBlock, MatroskaReader, MatroskaTrack
from core.matroska.writer import MatroskaWriter, rewrite_tag_target_uids

_MS = 1_000_000


def _track(number: int, uid: int, kind: int, codec: str) -> MatroskaTrack:
    raw = b"".join((
        ebml.uint_element(TRACK_NUMBER_ID, number),
        ebml.uint_element(TRACK_UID_ID, uid),
        ebml.uint_element(TRACK_TYPE_ID, kind),
        ebml.ascii_element(CODEC_ID_ID, codec),
    ))
    return MatroskaTrack(number, uid, kind, codec, b"", "", "und", "", raw)


def _block(track: int, ms: int, payload: bytes, *, key: bool = True) -> MatroskaBlock:
    return MatroskaBlock(track, ms, 0x80 if key else 0, payload, timestamp_ns=ms * _MS)


def _write_source(path: Path, packets: list[MatroskaMuxPacket], *, tags: bytes = b"") -> None:
    """Source écrite dans l'ordre donné (flux paresseux : aucun réentrelacement par le writer)."""
    MatroskaWriter().write(MatroskaMuxPlan(
        path,
        (
            MatroskaMuxTrack(Path("v.mkv"), _track(1, 101, 1, "V_MPEG4/ISO/AVC"), 1, 101),
            MatroskaMuxTrack(Path("a.mkv"), _track(2, 102, 2, "A_AC3"), 2, 102),
        ),
        iter(packets),
        opaque_top_level=(tags,) if tags else (),
    ))


def _assemble(source: Path, output: Path, tracks: tuple[MatroskaAssemblyTrack, ...], **kwargs) -> Path:
    assembly = MatroskaAssemblyPlan(output=output, ordered_tracks=tracks, **kwargs)
    assembly = replace(assembly, expected_output_contract=assembly_output_contract(assembly))
    MatroskaWriter().write(compile_assembly_plan(assembly))
    return output


def _file_order(path: Path) -> list[tuple[int, int]]:
    return [(block.track_number, block.timestamp_ms) for block in MatroskaReader(path).blocks()]


def _max_lag_ms(order: list[tuple[int, int]]) -> int:
    highest, lag = 0, 0
    for _track_number, ms in order:
        highest = max(highest, ms)
        lag = max(lag, highest - ms)
    return lag


def test_badly_interleaved_source_is_reinterleaved(tmp_path: Path) -> None:
    """Audio écrit d'un bloc avant la vidéo (60 s d'avance) : sortie entrelacée par timestamp."""
    audio = [MatroskaMuxPacket(2, _block(2, ms, b"a"), i) for i, ms in enumerate(range(0, 60_000, 500))]
    video = [MatroskaMuxPacket(1, _block(1, ms, b"v"), i) for i, ms in enumerate(range(0, 60_000, 1000))]
    source = tmp_path / "bad.mkv"
    _write_source(source, audio + video)
    assert _max_lag_ms(_file_order(source)) >= 59_000

    output = _assemble(source, tmp_path / "out.mkv", (
        MatroskaAssemblyTrack(artifact=source, artifact_track_index=0, source_identity="s"),
        MatroskaAssemblyTrack(artifact=source, artifact_track_index=1, source_identity="s"),
    ))
    order = _file_order(output)
    assert _max_lag_ms(order) == 0
    assert [ms for track, ms in order if track == 1] == list(range(0, 60_000, 1000))
    assert [ms for track, ms in order if track == 2] == list(range(0, 60_000, 500))


def test_b_frames_keep_decode_order(tmp_path: Path) -> None:
    decode_order = [0, 120, 40, 80, 240, 160, 200]
    video = [MatroskaMuxPacket(1, _block(1, ms, f"v{ms}".encode(), key=i == 0), i)
             for i, ms in enumerate(decode_order)]
    audio = [MatroskaMuxPacket(2, _block(2, ms, b"a"), i) for i, ms in enumerate(range(0, 280, 20))]
    source = tmp_path / "bframes.mkv"
    _write_source(source, video + audio)
    output = _assemble(source, tmp_path / "out.mkv", (
        MatroskaAssemblyTrack(artifact=source, artifact_track_index=0, source_identity="s"),
        MatroskaAssemblyTrack(artifact=source, artifact_track_index=1, source_identity="s"),
    ))
    order = _file_order(output)
    assert [ms for track, ms in order if track == 1] == decode_order
    # Écart borné par la profondeur de réordonnancement des B-frames.
    assert _max_lag_ms(order) <= 80


def _track_tag(uid: int, value: str) -> bytes:
    return ebml.element(TAGS_ID, ebml.element(TAG_ID, b"".join((
        ebml.element(TARGETS_ID, ebml.uint_element(TAG_TRACK_UID_ID, uid)),
        ebml.element(SIMPLE_TAG_ID, ebml.string_element(TAG_NAME_ID, "COMMENT")
                     + ebml.string_element(TAG_STRING_ID, value)),
    ))))


def test_duplicated_track_with_divergent_offsets(tmp_path: Path) -> None:
    audio = [MatroskaMuxPacket(2, _block(2, ms, b"a"), i) for i, ms in enumerate(range(0, 120_000, 1000))]
    video = [MatroskaMuxPacket(1, _block(1, ms, b"v"), i) for i, ms in enumerate(range(0, 120_000, 1000))]
    source = tmp_path / "dup.mkv"
    _write_source(source, sorted(audio + video, key=lambda p: p.block.timestamp_ms),
                  tags=_track_tag(102, "audio"))
    source_uid = MatroskaReader(source).tracks()[1].uid
    assert source_uid == 102
    output = _assemble(source, tmp_path / "out.mkv", (
        MatroskaAssemblyTrack(artifact=source, artifact_track_index=0, source_identity="s"),
        MatroskaAssemblyTrack(artifact=source, artifact_track_index=1, source_identity="s"),
        MatroskaAssemblyTrack(artifact=source, artifact_track_index=1, source_identity="s",
                              time_shift_ms=60_000, provenance="copie"),
    ), tag_copy_sources=(source,))
    order = _file_order(output)
    assert _max_lag_ms(order) == 0
    assert [ms for track, ms in order if track == 3] == list(range(60_000, 180_000, 1000))
    # Le tag de la piste source suit ses deux copies.
    reader = MatroskaReader(output)
    output_uids = {track.number: track.uid for track in reader.tracks()}
    raw_tags = b"".join(reader.raw_top_level(TAGS_ID))
    for number in (2, 3):
        assert ebml.uint_element(TAG_TRACK_UID_ID, output_uids[number]) in raw_tags


def test_negative_offset_drops_packets_before_zero(tmp_path: Path) -> None:
    audio = [MatroskaMuxPacket(2, _block(2, ms, b"a"), i) for i, ms in enumerate(range(0, 5_000, 1000))]
    video = [MatroskaMuxPacket(1, _block(1, ms, b"v"), i) for i, ms in enumerate(range(0, 5_000, 1000))]
    source = tmp_path / "neg.mkv"
    _write_source(source, sorted(audio + video, key=lambda p: p.block.timestamp_ms))
    output = _assemble(source, tmp_path / "out.mkv", (
        MatroskaAssemblyTrack(artifact=source, artifact_track_index=0, source_identity="s"),
        MatroskaAssemblyTrack(artifact=source, artifact_track_index=1, source_identity="s", time_shift_ms=-2_000),
    ))
    assert [ms for track, ms in _file_order(output) if track == 2] == [0, 1000, 2000]


def test_rewrite_tags_duplicates_for_each_output_uid() -> None:
    rewritten = rewrite_tag_target_uids(_track_tag(7, "x"), track_uids={7: [70, 71]}, attachment_uids={})
    assert ebml.uint_element(TAG_TRACK_UID_ID, 70) in rewritten
    assert ebml.uint_element(TAG_TRACK_UID_ID, 71) in rewritten
    single = rewrite_tag_target_uids(_track_tag(7, "x"), track_uids={7: 70}, attachment_uids={})
    assert ebml.uint_element(TAG_TRACK_UID_ID, 70) in single


def test_merge_closes_every_route_when_interrupted() -> None:
    closed: list[str] = []

    def route(name: str):
        try:
            for ms in range(0, 10_000, 1000):
                yield MatroskaMuxPacket(1, _block(1, ms, name.encode()), ms)
        finally:
            closed.append(name)

    merged = merge_route_streams([route("a"), route("b")])
    next(merged)
    merged.close()  # type: ignore[attr-defined]
    assert sorted(closed) == ["a", "b"]


# ---------------------------------------------------------------------------
# Lecture partagée : une passe, files bornées, sous-titres clairsemés par les Cues
# ---------------------------------------------------------------------------

def _subtitle_track(number: int, uid: int) -> MatroskaTrack:
    return _track(number, uid, 17, "S_TEXT/UTF8")


def _write_av_subtitles(path: Path, *, subtitle_times: list[int], duration_ms: int = 600_000) -> None:
    """Vidéo + audio denses, sous-titres clairsemés ; Cues et statistiques écrits par le writer natif."""
    packets = [MatroskaMuxPacket(1, _block(1, ms, b"v" * 2000), i) for i, ms in enumerate(range(0, duration_ms, 1000))]
    packets += [MatroskaMuxPacket(2, _block(2, ms, b"a" * 500), i) for i, ms in enumerate(range(0, duration_ms, 500))]
    packets += [MatroskaMuxPacket(3, MatroskaBlock(3, ms, 0x80, f"s{ms}".encode(), timestamp_ns=ms * _MS,
                                                   duration_ms=1000, duration_ns=1000 * _MS), i)
                for i, ms in enumerate(subtitle_times)]
    MatroskaWriter().write(MatroskaMuxPlan(
        path,
        (
            MatroskaMuxTrack(Path("v.mkv"), _track(1, 101, 1, "V_MPEG4/ISO/AVC"), 1, 101),
            MatroskaMuxTrack(Path("a.mkv"), _track(2, 102, 2, "A_AC3"), 2, 102),
            MatroskaMuxTrack(Path("s.mkv"), _subtitle_track(3, 103), 3, 103),
        ),
        tuple(packets),
        regenerate_statistics=True,
    ))


def _all_tracks(source: Path) -> tuple[MatroskaAssemblyTrack, ...]:
    return tuple(MatroskaAssemblyTrack(artifact=source, artifact_track_index=i, source_identity="s") for i in range(3))


def test_sparse_subtitles_never_force_a_second_read(tmp_path: Path, monkeypatch) -> None:
    from core.matroska import assembly

    source = tmp_path / "sparse.mkv"
    _write_av_subtitles(source, subtitle_times=[5_000, 300_000, 590_000])
    detached: list[int] = []
    original = assembly._SharedArtifactRead._detach
    monkeypatch.setattr(assembly._SharedArtifactRead, "_detach",
                        lambda self, route: (detached.append(route.track_number), original(self, route))[1])
    # Budget minuscule : attendre le prochain sous-titre dans la passe partagée déborderait.
    monkeypatch.setattr(assembly, "SHARED_READ_QUEUE_BYTES", 64 * 1024)
    order = _file_order(_assemble(source, tmp_path / "out.mkv", _all_tracks(source)))
    assert detached == []
    assert [ms for track, ms in order if track == 3] == [5_000, 300_000, 590_000]
    assert _max_lag_ms(order) == 0


def test_incomplete_subtitle_cues_fail_explicitly(tmp_path: Path, monkeypatch) -> None:
    import pytest

    from core.matroska import assembly

    source = tmp_path / "cues.mkv"
    _write_av_subtitles(source, subtitle_times=[5_000, 300_000, 590_000])
    real_index = MatroskaReader.cue_block_index

    def partial_index(self, numbers):
        index = real_index(self, numbers)
        return {number: dict(sorted(values.items())[:-1]) for number, values in index.items()}

    monkeypatch.setattr(MatroskaReader, "cue_block_index", partial_index)
    monkeypatch.setattr(assembly, "_statistics_frame_counts", lambda _reader: {103: 2})
    with pytest.raises(ValueError, match="Cues.*incomplet"):
        _assemble(source, tmp_path / "out.mkv", _all_tracks(source))
    assert not (tmp_path / "out.mkv").exists()


def test_subtitle_cues_unused_when_statistics_disagree(tmp_path: Path, monkeypatch) -> None:
    from core.matroska import assembly

    source = tmp_path / "stats.mkv"
    _write_av_subtitles(source, subtitle_times=[5_000, 10_000], duration_ms=20_000)
    monkeypatch.setattr(assembly, "_statistics_frame_counts", lambda _reader: {103: 99})
    order = _file_order(_assemble(source, tmp_path / "out.mkv", _all_tracks(source)))
    assert [ms for track, ms in order if track == 3] == [5_000, 10_000]


def test_indexed_subtitles_stored_late_are_written_in_presentation_order(tmp_path: Path) -> None:
    """Cas réel (UHD, PGS) : la source range 308 s après 313 s ; la sortie les écrit à leur place."""
    from core.matroska.copy_check import block_fingerprints
    from core.matroska.validation import interleave_warnings, scan_interleave

    source = tmp_path / "late.mkv"
    _write_av_subtitles(source, subtitle_times=[5_000, 313_000, 308_000, 309_000, 400_000])
    stored = [ms for track, ms in _file_order(source) if track == 3]
    assert stored == [5_000, 313_000, 308_000, 309_000, 400_000]  # source mal rangée, Cues complets
    output = _assemble(source, tmp_path / "out.mkv", _all_tracks(source))
    order = _file_order(output)
    assert [ms for track, ms in order if track == 3] == [5_000, 308_000, 309_000, 313_000, 400_000]
    assert _max_lag_ms(order) == 0
    assert not interleave_warnings(scan_interleave(output))
    # Copie fidèle : mêmes blocs de sous-titres, seul leur rang change.
    assert block_fingerprints(output) == block_fingerprints(source)


def test_indexed_blocks_follow_the_given_order(tmp_path: Path) -> None:
    source = tmp_path / "index.mkv"
    _write_av_subtitles(source, subtitle_times=[5_000, 313_000, 308_000])
    reader = MatroskaReader(source)
    index = reader.cue_block_index({3})[3]
    assert sorted(index.values()) == [5_000, 308_000, 313_000]  # CueTime = horodatage (échelle 1 ms)
    assert reader.cue_block_positions({3}) == {3: set(index)}
    by_time = sorted(index, key=lambda position: (index[position], position))
    assert [block.timestamp_ms for block in reader.indexed_blocks(by_time, ordered=True)] == [5_000, 308_000, 313_000]
    assert [block.timestamp_ms for block in reader.indexed_blocks(by_time)] == [5_000, 313_000, 308_000]


def test_queue_budget_detaches_the_loaded_route_and_keeps_order(tmp_path: Path, monkeypatch) -> None:
    from core.matroska import assembly

    audio = [MatroskaMuxPacket(2, _block(2, ms, b"a" * 1000), i) for i, ms in enumerate(range(0, 60_000, 500))]
    video = [MatroskaMuxPacket(1, _block(1, ms, b"v" * 1000), i) for i, ms in enumerate(range(0, 60_000, 1000))]
    source = tmp_path / "bad.mkv"
    _write_source(source, audio + video)
    detached: list[int] = []
    original = assembly._SharedArtifactRead._detach
    monkeypatch.setattr(assembly._SharedArtifactRead, "_detach",
                        lambda self, route: (detached.append(route.track_number), original(self, route))[1])
    monkeypatch.setattr(assembly, "SHARED_READ_QUEUE_BYTES", 8 * 1024)
    order = _file_order(_assemble(source, tmp_path / "out.mkv", (
        MatroskaAssemblyTrack(artifact=source, artifact_track_index=0, source_identity="s"),
        MatroskaAssemblyTrack(artifact=source, artifact_track_index=1, source_identity="s"),
    )))
    assert detached == [2]  # l'audio écrit d'un bloc en avance passe sur sa propre lecture
    assert _max_lag_ms(order) == 0
    assert [ms for track, ms in order if track == 2] == list(range(0, 60_000, 500))


def test_incomplete_cues_detected_even_when_every_shared_route_is_detached(tmp_path: Path, monkeypatch) -> None:
    import pytest

    from core.matroska import assembly

    source = tmp_path / "detached.mkv"
    _write_av_subtitles(source, subtitle_times=[5_000, 300_000, 590_000])
    real_index = MatroskaReader.cue_block_index
    monkeypatch.setattr(MatroskaReader, "cue_block_index", lambda self, numbers: {
        number: dict(sorted(values.items())[:-1]) for number, values in real_index(self, numbers).items()
    })
    monkeypatch.setattr(assembly, "_statistics_frame_counts", lambda _reader: {103: 2})
    # Budget nul : vidéo et audio quittent la passe partagée dès les premiers blocs.
    monkeypatch.setattr(assembly, "SHARED_READ_QUEUE_BYTES", 0)
    with pytest.raises(ValueError, match="Cues.*incomplet"):
        _assemble(source, tmp_path / "out.mkv", _all_tracks(source))
    assert not (tmp_path / "out.mkv").exists()


def test_cue_pointing_to_another_track_is_refused(tmp_path: Path, monkeypatch) -> None:
    import pytest

    from core.matroska import assembly

    source = tmp_path / "wrong.mkv"
    _write_av_subtitles(source, subtitle_times=[5_000, 300_000, 590_000])
    real_index = MatroskaReader.cue_block_index

    def video_positions(self, numbers):
        video = dict(sorted(real_index(self, {1})[1].items())[:3])
        return {number: video for number in numbers}

    monkeypatch.setattr(MatroskaReader, "cue_block_index", video_positions)
    monkeypatch.setattr(assembly, "_statistics_frame_counts", lambda _reader: {103: 3})
    with pytest.raises(ValueError, match="désigne un bloc de la piste #1"):
        _assemble(source, tmp_path / "out.mkv", _all_tracks(source))


def test_dropped_packets_are_not_queued(tmp_path: Path, monkeypatch) -> None:
    from core.matroska import assembly

    audio = [MatroskaMuxPacket(2, _block(2, ms, b"a"), i) for i, ms in enumerate(range(0, 120_000, 20))]
    video = [MatroskaMuxPacket(1, _block(1, ms, b"v"), i) for i, ms in enumerate(range(0, 120_000, 1000))]
    source = tmp_path / "neg.mkv"
    _write_source(source, sorted(audio + video, key=lambda p: p.block.timestamp_ms))
    longest: list[int] = []
    original_pull = assembly._SharedArtifactRead._pull

    def spy(self):
        result = original_pull(self)
        longest.append(max((len(route.queue) for route in self._routes), default=0))
        return result

    monkeypatch.setattr(assembly._SharedArtifactRead, "_pull", spy)
    output = _assemble(source, tmp_path / "out.mkv", (
        MatroskaAssemblyTrack(artifact=source, artifact_track_index=0, source_identity="s"),
        MatroskaAssemblyTrack(artifact=source, artifact_track_index=1, source_identity="s", time_shift_ms=-100_000),
    ))
    # 5 000 paquets audio écartés (avant zéro) : aucun n'occupe la file.
    assert max(longest) < 100
    assert [ms for track, ms in _file_order(output) if track == 2] == list(range(0, 20_000, 20))
