"""Texte des sous-titres : index Matroska (Cues) d'abord, une seule passe FFmpeg sinon."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core.matroska.ebml import element, string_element, uint_element
from core.matroska.ids import EBML_HEADER_ID, SEGMENT_ID
from core.matroska.reader import MatroskaReader
from core.workflows import subtitle_text
from core.workflows.preparation_cache import PreparationCache
from core.workflows.subtitle_text import SubtitleTextRequest, extract_subtitle_texts, subtitle_text_summaries

_ID = {name: bytes.fromhex(value) for name, value in {
    "tracks": "1654ae6b", "entry": "ae", "number": "d7", "uid": "73c5", "type": "83", "codec": "86",
    "cluster": "1f43b675", "timestamp": "e7", "simple_block": "a3", "block_group": "a0", "block": "a1",
    "cues": "1c53bb6b", "point": "bb", "time": "b3", "positions": "b7", "track": "f7",
    "cluster_position": "f1", "relative_position": "f0",
}.items()}
_TRACKS = ((1, 1, "V_MPEGH/ISO/HEVC"), (2, 0x11, "S_TEXT/UTF8"), (3, 0x11, "S_TEXT/ASS"))
_CLUSTERS = (
    ((1, b"video0"), (2, b"Bonjour"), (2, b"[Music] la la")),
    ((1, b"video1"), (3, b"0,0,Default,,0,0,0,,{\\i1}Salut{\\i0}\\Nici"), (2, b"Fin")),
)


def _mkv(tmp_path: Path, *, indexed=frozenset({2, 3}), skip=(), grouped=False, relative=True) -> Path:
    """MKV minimal : Tracks, Cues (avant les Clusters, lus sans SeekHead), Clusters."""
    tracks = element(_ID["tracks"], b"".join(element(_ID["entry"], b"".join((
        uint_element(_ID["number"], number), uint_element(_ID["uid"], number),
        uint_element(_ID["type"], kind), string_element(_ID["codec"], codec),
    ))) for number, kind, codec in _TRACKS))
    cues = clusters = b""
    for _ in range(8):  # Point fixe : la taille des Cues décale les Clusters.
        offset, clusters, points = len(tracks) + len(cues), b"", []
        for cluster_blocks in _CLUSTERS:
            body = uint_element(_ID["timestamp"], 0)
            for number, payload in cluster_blocks:
                block = bytes([0x80 | number]) + b"\x00\x00\x80" + payload
                if number in indexed and (number, payload) not in skip:
                    points.append((number, offset, len(body)))
                body += (element(_ID["block_group"], element(_ID["block"], block)) if grouped
                         else element(_ID["simple_block"], block))
            cluster = element(_ID["cluster"], body)
            clusters += cluster
            offset += len(cluster)
        rebuilt = element(_ID["cues"], b"".join(element(_ID["point"], uint_element(_ID["time"], 0) + element(
            _ID["positions"], uint_element(_ID["track"], number) + uint_element(_ID["cluster_position"], position)
            + (uint_element(_ID["relative_position"], inner) if relative else b""),
        )) for number, position, inner in points))
        if rebuilt == cues:
            break
        cues = rebuilt
    path = tmp_path / "subs.mkv"
    path.write_bytes(element(EBML_HEADER_ID, b"") + element(SEGMENT_ID, tracks + cues + clusters))
    return path


def _requests(*, utf8: int | None = 3, ass: int | None = 1):
    return [SubtitleTextRequest(1, "subrip", utf8), SubtitleTextRequest(2, "ass", ass)]


@pytest.fixture
def ffmpeg_calls(monkeypatch):
    calls = []

    def run_probe(command, **_kwargs):
        maps = [index for index, value in enumerate(command) if value == "-map"]
        calls.append([command[index + 1] for index in maps])
        for index in maps:
            Path(command[index + 6]).write_text("1\n00:00:01,000 --> 00:00:02,000\n(ffmpeg)\n")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subtitle_text, "run_probe", run_probe)
    return calls


@pytest.mark.parametrize("grouped", [False, True])
def test_indexed_tracks_are_read_from_cues_without_ffmpeg(tmp_path, ffmpeg_calls, grouped):
    texts = extract_subtitle_texts(_mkv(tmp_path, grouped=grouped), _requests(), ffmpeg="ffmpeg")
    assert not ffmpeg_calls
    assert texts[1].count == 3 and texts[1].text == "Bonjour\n\n[Music] la la\n\nFin"
    # ASS : champs d'en-tête retirés, balises i/b/u comme dans le SRT produit par FFmpeg.
    assert texts[2].count == 1 and texts[2].text == "<i>Salut</i>\nici"


def test_unverifiable_or_incomplete_index_goes_to_a_single_ffmpeg_pass(tmp_path, ffmpeg_calls):
    path = _mkv(tmp_path, skip={(2, b"Fin")})
    # Piste 2 : Cues partiels ; piste 3 : statistiques absentes. Les deux en une passe.
    texts = extract_subtitle_texts(path, _requests(ass=None), ffmpeg="ffmpeg")
    assert ffmpeg_calls == [["0:1", "0:2"]]
    assert texts[1].count == texts[2].count == 1


def test_index_without_relative_position_is_not_used(tmp_path, ffmpeg_calls):
    path = _mkv(tmp_path, relative=False)
    assert MatroskaReader(path).cue_block_positions({2, 3}) is None
    extract_subtitle_texts(path, _requests(), ffmpeg="ffmpeg")
    assert ffmpeg_calls == [["0:1", "0:2"]]


def test_codec_mismatch_or_foreign_file_uses_ffmpeg(tmp_path, ffmpeg_calls):
    path = _mkv(tmp_path)
    extract_subtitle_texts(path, [SubtitleTextRequest(1, "ass", 3)], ffmpeg="ffmpeg")
    other = tmp_path / "subs.mp4"
    other.write_bytes(b"\x00" * 64)
    extract_subtitle_texts(other, [SubtitleTextRequest(2, "subrip", 1)], ffmpeg="ffmpeg")
    assert ffmpeg_calls == [["0:1"], ["0:2"]]


def test_summaries_are_cached_until_the_file_changes(tmp_path, monkeypatch):
    path = _mkv(tmp_path)
    reads = []
    original = subtitle_text.extract_subtitle_texts
    monkeypatch.setattr(subtitle_text, "extract_subtitle_texts",
                        lambda *args, **kwargs: reads.append(args[1]) or original(*args, **kwargs))
    cache = PreparationCache()
    first = subtitle_text_summaries(path, _requests(), ffmpeg="ffmpeg", cache=cache)
    assert first[1].count == 3 and first[1].sound_description == "[Music]"
    assert subtitle_text_summaries(path, _requests(), ffmpeg="ffmpeg", cache=cache) == first
    assert len(reads) == 1
    path.write_bytes(path.read_bytes() + b"\xec\x80")  # Void ajouté : identité changée.
    subtitle_text_summaries(path, _requests(), ffmpeg="ffmpeg", cache=cache)
    assert len(reads) == 2
