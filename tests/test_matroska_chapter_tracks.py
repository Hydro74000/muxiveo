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
        return compile_assembly_plan(assembly, readers={artifact: MatroskaReader(artifact)}).tracks[0].output_uid

    assert output_uid(first) == output_uid(second)
