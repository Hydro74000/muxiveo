"""
tests/test_matroska_chapter_tracks.py — Fidélité Matroska (N5, N12 ; plan de remédiation 5.2.0, lot 12) :
``ChapterTrack`` remappé, chapitre sans cible retiré (décision DF) sans rejet de la sortie, ``SegmentUID`` distinct
pour deux plans ne différant que d'un décalage, UID des pistes lues en flux stables.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from core.matroska import ebml
from core.matroska.assembly import (
    MatroskaAssemblyPlan,
    MatroskaAssemblyTrack,
    assembly_output_contract,
    compile_assembly_plan,
)
from core.matroska.chapter_tracks import remap_chapter_tracks
from core.matroska.ids import CHAPTERS_ID
from core.matroska.mux_plan import MatroskaMuxPacket, MatroskaMuxPlan, MatroskaMuxTrack
from core.matroska.reader import MatroskaBlock, MatroskaReader, payload_children
from core.matroska.writer import MatroskaWriter
from tests.test_matroska_remediation import _track

ATOM, UID, START, TRACK, TRACK_UID = (bytes.fromhex(h) for h in ("b6", "73c4", "91", "8f", "89"))
EDITION, CHAPTERS = bytes.fromhex("45b9"), bytes.fromhex("1043a770")


def _atom(uid: int, *, targets: tuple[int, ...] = (), children: tuple[bytes, ...] = ()) -> bytes:
    body = ebml.uint_element(UID, uid) + ebml.uint_element(START, uid * 1_000_000_000)
    if targets:
        body += ebml.element(TRACK, b"".join(ebml.uint_element(TRACK_UID, value) for value in targets))
    return ebml.element(ATOM, body + b"".join(children))


def _chapters(*atoms: bytes) -> bytes:
    return ebml.element(EDITION, ebml.uint_element(bytes.fromhex("45bc"), 7) + b"".join(atoms))


def _uids(element: bytes) -> list[tuple[int, list[int]]]:
    found: list[tuple[int, list[int]]] = []

    def walk(payload: bytes) -> None:
        for element_id, value in payload_children(payload):
            if element_id == ATOM:
                uid, targets = 0, []
                for child_id, child in payload_children(value):
                    if child_id == UID:
                        uid = int.from_bytes(child, "big")
                    elif child_id == TRACK:
                        targets = [int.from_bytes(v, "big") for i, v in payload_children(child) if i == TRACK_UID]
                found.append((uid, targets))
                walk(value)
            elif element_id == EDITION:
                walk(value)

    walk(MatroskaReader._raw_payload(element))
    return found


def test_chapter_tracks_are_remapped_and_orphan_chapters_removed():
    payload = _chapters(
        _atom(1, targets=(11,)),                                 # piste dupliquée : deux UID de sortie
        _atom(2, targets=(22,), children=(_atom(3, targets=(33,)),)),  # cible absente : retiré avec son enfant
        _atom(4, children=(_atom(5, targets=(99,)),)),          # parent sans cible gardé, enfant orphelin retiré
        _atom(6),                                                # sans ChapterTrack : inchangé
    )
    rewritten, removed = remap_chapter_tracks(payload, {11: [101, 102]})
    assert removed == 3
    assert _uids(rewritten) == [(1, [101, 102]), (4, []), (6, [])]


def test_all_chapters_removed_yields_no_element():
    rewritten, removed = remap_chapter_tracks(_chapters(_atom(1, targets=(5,))), {})
    assert rewritten == b"" and removed == 1


def _source(path: Path, *, chapters: bytes = b"") -> Path:
    video = _track(1, 10, "V_MPEG4/ISO/AVC", 1)
    MatroskaWriter().write(MatroskaMuxPlan(
        path, (MatroskaMuxTrack(path, video, 1, 10),),
        (MatroskaMuxPacket(1, MatroskaBlock(1, 0, 0x80, b"video")),),
        opaque_top_level=(ebml.element(CHAPTERS_ID, chapters),) if chapters else (),
    ))
    return path


def _plan(source: Path, output: Path, **track_kwargs) -> MatroskaAssemblyPlan:
    return MatroskaAssemblyPlan(output=output, chapter_source=source, ordered_tracks=(
        MatroskaAssemblyTrack(artifact=source, artifact_track_index=0, source_identity="test", **track_kwargs),))


def test_copied_chapters_follow_the_output_track_and_the_contract(tmp_path):
    source = _source(tmp_path / "src.mkv", chapters=_chapters(_atom(1, targets=(10,)), _atom(2, targets=(77,))))
    assembly = _plan(source, tmp_path / "out.mkv")
    contract = assembly_output_contract(assembly)
    assert contract.expects_chapters
    plan = compile_assembly_plan(replace(assembly, expected_output_contract=contract))
    assert plan.chapters_removed == 1
    MatroskaWriter().write(plan)
    output_uid = MatroskaReader(tmp_path / "out.mkv").tracks()[0].uid
    chapters = MatroskaReader(tmp_path / "out.mkv").raw_top_level(CHAPTERS_ID)
    assert _uids(chapters[0]) == [(1, [output_uid])]  # UID de sortie, jamais celui de la source


def test_every_chapter_removed_never_makes_the_output_rejected(tmp_path):
    source = _source(tmp_path / "src.mkv", chapters=_chapters(_atom(1, targets=(77,))))
    assembly = _plan(source, tmp_path / "out.mkv")
    precomputed = replace(assembly_output_contract(assembly), expects_chapters=True)  # contrat calculé sur la source
    contract = assembly_output_contract(replace(assembly, expected_output_contract=precomputed))
    assert contract.expects_chapters is False  # fixé sur les chapitres réellement écrits
    plan = compile_assembly_plan(replace(assembly, expected_output_contract=contract))
    assert not any(item.startswith(CHAPTERS_ID) for item in plan.opaque_top_level)


def test_segment_uid_changes_with_a_track_offset(tmp_path):
    source = _source(tmp_path / "src.mkv")

    def segment_uid(offset: int) -> int:
        assembly = _plan(source, tmp_path / "out.mkv", time_shift_ms=offset)
        return compile_assembly_plan(replace(assembly, expected_output_contract=assembly_output_contract(assembly))
                                     ).segment_uid

    assert segment_uid(0) == segment_uid(0)  # même plan : stable
    assert segment_uid(0) != segment_uid(125)


def test_streamed_track_uid_never_depends_on_the_producer_uid(tmp_path):
    """Piste lue en flux : deux lectures d'une même source (UID du producteur FFmpeg différents) → même UID."""
    first, second = _source(tmp_path / "a.mkv"), tmp_path / "b.mkv"
    video = _track(1, 999, "V_MPEG4/ISO/AVC", 1)  # autre UID de producteur
    MatroskaWriter().write(MatroskaMuxPlan(second, (MatroskaMuxTrack(second, video, 1, 999),),
                                           (MatroskaMuxPacket(1, MatroskaBlock(1, 0, 0x80, b"video")),)))

    def output_uid(artifact: Path) -> int:
        assembly = MatroskaAssemblyPlan(output=tmp_path / "out.mkv", ordered_tracks=(
            MatroskaAssemblyTrack(artifact=artifact, artifact_track_index=0, source_identity="film.mp4"),))
        assembly = replace(assembly, expected_output_contract=assembly_output_contract(assembly))
        return compile_assembly_plan(assembly, readers={artifact: MatroskaReader(artifact)},
                                     stream_keys={(artifact, 0): "flux:1"}).tracks[0].output_uid

    assert output_uid(first) == output_uid(second)


def _streamed(tmp_path: Path, name: str, producer_uid: int) -> Path:
    path = tmp_path / name
    video = _track(1, producer_uid, "V_MPEG4/ISO/AVC", 1)
    MatroskaWriter().write(MatroskaMuxPlan(path, (MatroskaMuxTrack(path, video, 1, producer_uid),),
                                           (MatroskaMuxPacket(1, MatroskaBlock(1, 0, 0x80, b"video")),)))
    return path


def _compiled(tmp_path: Path, artifact: Path, keys: dict, **track_kwargs):
    assembly = MatroskaAssemblyPlan(output=tmp_path / "out.mkv", ordered_tracks=(
        MatroskaAssemblyTrack(artifact=artifact, artifact_track_index=0, source_identity="film.mp4",
                              **track_kwargs),))
    assembly = replace(assembly, expected_output_contract=assembly_output_contract(assembly))
    return compile_assembly_plan(assembly, readers={artifact: MatroskaReader(artifact)}, stream_keys=keys)


def test_two_streams_of_a_source_each_read_alone_never_share_their_uids(tmp_path):
    """Relecture Codex 19 : deux flux d'un même MP4, chacun seul (position 0) avec les mêmes métadonnées → UID de
    piste et SegmentUID distincts (clé = index du flux d'origine, jamais la position dans le producteur)."""
    first, second = _streamed(tmp_path, "a.mkv", 5), _streamed(tmp_path, "b.mkv", 5)
    one = _compiled(tmp_path, first, {(first, 0): "flux:1"})
    two = _compiled(tmp_path, second, {(second, 0): "flux:2"})
    assert one.tracks[0].output_uid != two.tracks[0].output_uid and one.segment_uid != two.segment_uid


def test_rewritten_track_with_a_streamed_origin_has_a_stable_uid(tmp_path):
    """Piste réécrite dont l'origine est un en-tête de producteur : UID indépendant de l'UID de ce producteur."""
    rewritten = _source(tmp_path / "rewritten.mkv")

    def output_uid(origin_uid: int, name: str) -> int:
        origin = _streamed(tmp_path, name, origin_uid)
        return _compiled(tmp_path, rewritten, {(origin, 0): "flux:3"}, origin=(origin, 0)).tracks[0].output_uid

    assert output_uid(111, "o1.mkv") == output_uid(222, "o2.mkv")


def test_segment_uid_follows_every_written_flag(tmp_path):
    from core.matroska.assembly import MatroskaTrackFlags

    source = _source(tmp_path / "src.mkv")

    def segment_uid(**flags) -> int:
        return _compiled(tmp_path, source, {}, flags=MatroskaTrackFlags(**flags)).segment_uid

    assert segment_uid() == segment_uid()
    assert segment_uid() != segment_uid(hearing_impaired=True)
    assert segment_uid() != segment_uid(commentary=True)


def test_stream_artifacts_record_the_origin_stream_of_each_route(tmp_path):
    from core.workflows.common.native_stream_inputs import StreamArtifacts, StreamTrackSpec

    opened: list[object] = []

    class _Inputs:
        def open(self, spec, *, duration_ns=None, header_only=False):
            opened.append(spec)
            return type("R", (), {"path": tmp_path / f"header{len(opened)}.mkv"})()

    streams = StreamArtifacts(_Inputs())  # type: ignore[arg-type]
    mp4 = tmp_path / "film.mp4"
    streams.declare([StreamTrackSpec(mp4, 0, "video", "h264"), StreamTrackSpec(mp4, 2, "audio", "aac")])
    header, position = streams.route(StreamTrackSpec(mp4, 2, "audio", "aac"))
    assert streams.stream_keys[(header, position)] == "flux:2"
    assert streams.stream_keys[streams.route(StreamTrackSpec(mp4, 0, "video", "h264"))] == "flux:0"
    only = streams.metadata(StreamTrackSpec(mp4, 3, "subtitle", "mov_text"))
    assert streams.stream_keys[only] == "flux:3"


# ── Drapeaux d'édition et de chapitre (N6, décision DD) ───────────────────────


def _flagged_source(path: Path, *, ordered: bool = False, extra_chapter: bytes = b"") -> Path:
    child = ebml.element(ATOM, ebml.uint_element(UID, 3) + ebml.uint_element(START, 1_000_000_000)
                         + ebml.uint_element(bytes.fromhex("98"), 1))                    # enfant masqué
    parent = ebml.element(ATOM, ebml.uint_element(UID, 1) + ebml.uint_element(START, 0)
                          + ebml.uint_element(bytes.fromhex("4598"), 0) + child)         # parent inactif
    edition = (ebml.uint_element(bytes.fromhex("45bc"), 7) + ebml.uint_element(bytes.fromhex("45bd"), 1)
               + ebml.uint_element(bytes.fromhex("45db"), 1)
               + (ebml.uint_element(bytes.fromhex("45dd"), 1) if ordered else b"") + parent + extra_chapter)
    return _source(path, chapters=ebml.element(EDITION, edition))


def test_reader_exposes_simple_flags_and_references(tmp_path):
    (edition,) = MatroskaReader(_flagged_source(tmp_path / "a.mkv")).chapter_editions()
    assert edition.hidden and edition.default and not edition.ordered
    parent = edition.chapters[0]
    assert parent.enabled is False and parent.children[0].hidden is True


def test_projection_copies_simple_flags_and_writer_writes_them(tmp_path):
    from core.matroska.writer import build_chapters_element
    from core.workflows.merge_dovi_align.chapters import ProjectedChapter, edition_flags, unsupported_reason

    editions = MatroskaReader(_flagged_source(tmp_path / "a.mkv")).chapter_editions()
    assert unsupported_reason(editions) is None and edition_flags(editions) == (True, True)
    chapters = (ProjectedChapter(0.0, None, "Un", "fra", 1, (("Un", "fra"),),
                                 (ProjectedChapter(1.0, None, "Deux", "fra", 3, (("Deux", "fra"),), hidden=True),),
                                 enabled=False),)
    element = build_chapters_element(list(chapters), edition_hidden=True, edition_default=True)
    output = _source(tmp_path / "out.mkv", chapters=MatroskaReader._raw_payload(element))
    (written,) = MatroskaReader(output).chapter_editions()
    assert written.hidden and written.default
    assert written.chapters[0].enabled is False and written.chapters[0].children[0].hidden is True


def test_unprojectable_editions_are_reported(tmp_path):
    from core.workflows.merge_dovi_align.chapters import unsupported_reason

    ordered = MatroskaReader(_flagged_source(tmp_path / "o.mkv", ordered=True)).chapter_editions()
    assert unsupported_reason(ordered)
    linked = ebml.element(ATOM, ebml.uint_element(UID, 9) + ebml.uint_element(START, 0)
                          + ebml.element(bytes.fromhex("6e67"), b"\x01" * 16))
    assert unsupported_reason(MatroskaReader(_flagged_source(tmp_path / "l.mkv", extra_chapter=linked)
                                             ).chapter_editions())
    targeted = _atom(8, targets=(10,))
    assert unsupported_reason(MatroskaReader(_flagged_source(tmp_path / "t.mkv", extra_chapter=targeted)
                                             ).chapter_editions())


def _with_crc(master_id: bytes, body: bytes) -> bytes:
    """Master précédé d'un CRC-32 valide (EBML : CRC IEEE, petit-boutiste, des enfants qui suivent)."""
    import zlib

    return ebml.element(master_id, ebml.element(bytes.fromhex("bf"), zlib.crc32(body).to_bytes(4, "little")) + body)


def _invalid_crcs(payload: bytes, masters: frozenset[bytes]) -> list[bytes]:
    import zlib

    bad: list[bytes] = []
    for element_id, value in payload_children(payload):
        if element_id not in masters:
            continue
        children = list(payload_children(value))
        if children and children[0][0] == bytes.fromhex("bf"):
            rest = value[len(ebml.element(bytes.fromhex("bf"), children[0][1])):]
            if zlib.crc32(rest).to_bytes(4, "little") != children[0][1]:
                bad.append(element_id)
        bad += _invalid_crcs(value, masters)
    return bad


def test_remapped_chapters_never_keep_a_stale_crc():
    """Relecture Codex 19 : un master réassemblé ne garde jamais le CRC-32 de la source (devenu invalide)."""
    atom_body = (ebml.uint_element(UID, 1) + ebml.uint_element(START, 0)
                 + ebml.element(TRACK, ebml.uint_element(TRACK_UID, 11)))
    edition = _with_crc(EDITION, ebml.uint_element(bytes.fromhex("45bc"), 7) + _with_crc(ATOM, atom_body))
    payload = MatroskaReader._raw_payload(_with_crc(CHAPTERS, edition))
    remapped, removed = remap_chapter_tracks(payload, {11: [99]})
    assert removed == 0 and _uids(remapped) == [(1, [99])]
    assert _invalid_crcs(MatroskaReader._raw_payload(remapped), frozenset({EDITION, ATOM, TRACK})) == []
