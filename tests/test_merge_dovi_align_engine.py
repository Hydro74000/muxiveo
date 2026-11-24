"""Merge DoVi — alignement : moteur de correspondance sur films synthétiques (sans média)."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from core.workflows.merge_dovi_align.align import (
    AlignmentError,
    Film2Frames,
    _isotonic,
    align,
    coarse_offsets,
    detect_cuts,
)
from core.workflows.merge_dovi_align.probe import Signatures
from core.workflows.merge_dovi_align.signals import (
    Film2Metadata,
    MetadataTrack,
    combine,
    hdr10plus_track,
    rpu_track,
)

H, W = 96, 144          # cellules de vignette entières (16 × 24, 6 × 6 pixels)
BLACK = 64


class Scene:
    """Plan : image « open matte » lisse (96 lignes) qui glisse d'un pixel par image."""

    def __init__(self, rng: np.random.Generator, length: int, level: float | None = None, *,
                 black: bool = False) -> None:
        self.length, self.black = length, black
        low = rng.uniform(0, 1, size=(11, 26))
        big = np.kron(low, np.ones((10, 10)))  # 110 × 260 : marge pour le glissement d'un plan
        self.level = level if level is not None else float(rng.uniform(200, 700))
        self.texture = self.level + 220 * (big - big.mean())

    def frame(self, index: int) -> np.ndarray:
        if self.black:
            return np.full((H, W), BLACK, dtype=np.float64)
        return self.texture[2:H + 2, index:index + W]


def render(scenes: list[tuple[Scene, int, int]], bars: int, *, pillar: int = 0) -> Signatures:
    """Signatures d'une suite (plan, première image du plan, nombre d'images) ; barres haut/bas ``bars``."""
    vignettes, tops, bottoms, lefts, means, stds, peaks = [], [], [], [], [], [], []
    for scene, first, count in scenes:
        for index in range(first, first + count):
            picture = scene.frame(index).copy()
            if not scene.black:
                picture[:bars] = 0
                picture[H - bars:] = 0
                if pillar:
                    picture[:, :pillar] = 0
            cells = picture.reshape(16, 6, 24, 6).mean(axis=(1, 3))
            vignettes.append((np.clip(cells, 0, 1023) / 4).astype(np.uint8))
            visible = not scene.black
            tops.append(bars if visible else -1)
            bottoms.append(bars if visible else -1)
            lefts.append(pillar if visible else -1)
            means.append(float(cells.mean()))
            stds.append(float(cells.std()))
            peaks.append(float(cells.max()))
    n = len(vignettes)
    top = np.asarray(tops, dtype=np.int16)
    return Signatures(W, H, np.arange(n), top, np.asarray(bottoms, dtype=np.int16), np.asarray(lefts, dtype=np.int16),
                      np.asarray(lefts, dtype=np.int16), np.stack(vignettes), np.asarray(means, dtype=np.float32),
                      np.asarray(stds, dtype=np.float32), np.asarray(peaks, dtype=np.float32))


def metadata_for(scenes: list[tuple[Scene, int, int]]) -> Film2Metadata:
    """Métadonnées de Film 2 : une valeur par plan (coupe et point de changement à chaque début de plan)."""
    starts, luminance, position = [], [], 0
    for scene, _first, count in scenes:
        starts.append(position)
        luminance += [scene.level * 3.2 + 100.0] * count
        position += count
    cuts = np.asarray(starts, dtype=np.int64)
    return Film2Metadata(position, cuts, cuts, np.asarray(luminance, dtype=np.float32), ("rpu",))


def film(rng: np.random.Generator, shots: int = 60) -> list[Scene]:
    return [Scene(rng, int(rng.integers(20, 90))) for _ in range(shots)]


def whole(scenes: list[Scene]) -> list[tuple[Scene, int, int]]:
    return [(scene, 0, scene.length) for scene in scenes]


def run(film1: list[tuple[Scene, int, int]], film2: list[tuple[Scene, int, int]], *, bars1: int = 4,
        bars2: int = 18, windowed: bool = True, frame_rate: float = 2.0, **kwargs):
    sig1 = render(film1, bars1)
    sig2 = render(film2, bars2)
    meta = kwargs.pop("metadata", None) or metadata_for(film2)
    source = Film2Frames(meta.frames, window=(lambda a, n: sig2.slice(a, a + n)) if windowed else None)
    full_calls = []

    def full():
        full_calls.append(True)
        return sig2

    result = align(sig1, meta, source, frame_rate=frame_rate, full_film2=full, **kwargs)
    return result, source, full_calls


def test_identity_is_verified_without_edit() -> None:
    scenes = whole(film(np.random.default_rng(1)))
    result, _source, full = run(scenes, scenes, bars2=4)
    assert result.identity and not result.removed and not full


def test_constant_offset_with_black_head_and_tail_like_the_imax_case() -> None:
    rng = np.random.default_rng(2)
    content = whole(film(rng))
    head, tail = Scene(rng, 16, black=True), Scene(rng, 24, black=True)
    film2 = [(head, 0, 16), *content, (tail, 0, 24)]
    result, source, full = run(content, film2)
    n1 = sum(count for _s, _f, count in content)
    assert [(seg.film1_start, seg.length, seg.offset) for seg in result.segments] == [(0, n1, 16)]
    assert result.removed == ((0, 16), (n1 + 16, 24)) and not result.uncovered
    assert not full and 0 < source.decoded < 0.25 * result.film2_frames


def test_extra_black_frames_in_film1_are_uncovered_and_classified() -> None:
    rng = np.random.default_rng(3)
    content = whole(film(rng))
    film1 = [(Scene(rng, 10, black=True), 0, 10), *content]
    result, _source, _full = run(film1, content)
    assert result.uncovered and result.uncovered[0].start == 0 and result.uncovered[0].length == 10
    assert result.uncovered[0].black and not result.uncovered[0].source_black


def test_shot_missing_from_film1_removes_its_metadata() -> None:
    rng = np.random.default_rng(4)
    scenes = film(rng)
    film2 = whole(scenes)
    film1 = whole(scenes[:30] + scenes[31:])
    result, _source, _full = run(film1, film2)
    start = sum(scene.length for scene in scenes[:30])
    assert result.removed == ((start, scenes[30].length),)
    assert [seg.offset for seg in result.segments] == [0, scenes[30].length]


def test_shot_only_in_film1_is_uncovered_content() -> None:
    rng = np.random.default_rng(5)
    scenes = film(rng)
    extra = Scene(rng, 40)
    film1 = whole(scenes[:25] + [extra] + scenes[25:])
    result, _source, _full = run(film1, whole(scenes))
    start = sum(scene.length for scene in scenes[:25])
    assert len(result.uncovered) == 1
    gap = result.uncovered[0]
    assert (gap.start, gap.length, gap.black) == (start, 40, False)
    assert start in result.expected_cuts.tolist() and start + 40 in result.expected_cuts.tolist()


def test_compensated_ruptures_inside_shots_are_found() -> None:
    """Deux images en moins dans un plan, deux en plus dans un autre : comptes égaux, coupes décalées entre les deux."""
    rng = np.random.default_rng(6)
    scenes = film(rng)
    film2 = whole(scenes)
    film1 = [(scene, 0, scene.length) for scene in scenes]
    a, b = 12, 20
    film1[a] = (scenes[a], 0, scenes[a].length - 2)                    # plan raccourci de 2 images (fin)
    film1[b] = (scenes[b], 0, scenes[b].length + 2)                    # plan prolongé de 2 images
    result, _source, _full = run(film1, film2)
    assert sum(count for _s, _f, count in film1) == result.film2_frames
    offsets = [seg.offset for seg in result.segments]
    assert 2 in offsets and offsets[0] == 0 and offsets[-1] == 0
    # Les 2 images de fin du plan a sont retirées de Film 2 ; les 2 images prolongées du plan b n'ont pas de
    # correspondance (images de Film 1 en plus).
    end_a = sum(scene.length for scene in scenes[:a + 1])
    assert (end_a - 2, 2) in result.removed
    assert any(gap.length == 2 for gap in result.uncovered)


def test_same_length_permuted_shots_with_different_luminance_are_refused() -> None:
    rng = np.random.default_rng(7)
    scenes = [Scene(rng, 40, level=200.0 + 10 * k) for k in range(80)]
    swapped = list(scenes)
    swapped[41], swapped[42] = Scene(rng, 40, level=650.0), Scene(rng, 40, level=230.0)
    with pytest.raises(AlignmentError):
        run(whole(swapped), whole(scenes), frame_rate=0.5)


def test_film2_without_index_is_read_entirely() -> None:
    rng = np.random.default_rng(8)
    scenes = whole(film(rng))
    result, _source, full = run(scenes, scenes, bars2=4, windowed=False)
    assert result.identity and full and result.stats["film2_full"]


def test_metadata_changing_on_every_frame_switches_to_full_pass() -> None:
    rng = np.random.default_rng(9)
    scenes = whole(film(rng))
    meta = metadata_for(scenes)
    every = Film2Metadata(meta.frames, np.arange(meta.frames), meta.reference_cuts, meta.luminance, ("rpu",))
    result, _source, full = run(scenes, scenes, bars2=4, metadata=every)
    assert result.identity and full


def test_hdr10plus_without_usable_cuts_reads_film2_entirely() -> None:
    rng = np.random.default_rng(10)
    scenes = whole(film(rng))
    meta = metadata_for(scenes)
    blind = Film2Metadata(meta.frames, meta.change_points, np.zeros(0, dtype=np.int64), meta.luminance, ("hdr10plus",))
    result, _source, full = run(scenes, scenes, bars2=4, metadata=blind)
    assert result.identity and full


def test_unrelated_content_is_refused() -> None:
    first, second = whole(film(np.random.default_rng(11))), whole(film(np.random.default_rng(12)))
    with pytest.raises(AlignmentError):
        run(first, second)


def test_forced_film2_image_check() -> None:
    rng = np.random.default_rng(13)
    scenes = whole(film(rng))
    result, _source, full = run(scenes, scenes, bars2=4, force_full_film2=True)
    assert result.identity and full


def test_cut_detection_and_coarse_offsets() -> None:
    rng = np.random.default_rng(14)
    scenes = whole(film(rng, 30))
    sig = render(scenes, 4)
    cuts = detect_cuts(sig)
    starts = np.cumsum([0] + [count for _s, _f, count in scenes[:-1]])
    assert np.array_equal(cuts, starts)
    candidates = coarse_offsets(cuts, len(sig), cuts + 16, len(sig) + 40, block=600)
    assert all(16 in block.tolist() for block in candidates if block.size)
    # Décalage négatif plus grand que le début du bloc (Film 1 avec des images en plus en tête), bloc unique.
    later = cuts[cuts >= 40] - 40
    single = coarse_offsets(cuts, len(sig), later, len(sig), block=len(sig))
    assert single[0].tolist()[0] == -40


def test_isotonic_fit_is_monotone() -> None:
    centers, values = _isotonic(np.asarray([1.0, 2.0, 3.0, 4.0]), np.asarray([1.0, 3.0, 2.0, 4.0]))
    assert np.all(np.diff(values) >= 0) and values.tolist() == [1.0, 2.5, 4.0]


# --- Repères des métadonnées ----------------------------------------------------------------------


def test_rpu_change_points_from_nal_bytes(tmp_path: Path) -> None:
    nals = [b"\x7c\x01AAAA", b"\x7c\x01AAAA", b"\x7c\x01BBBB", b"\x7c\x01BBBB", b"\x7c\x01AAAA"]
    path = tmp_path / "rpu.bin"
    path.write_bytes(b"".join(b"\x00\x00\x00\x01" + nal for nal in nals))
    track = rpu_track(path, np.asarray([0, 2]), np.asarray([10, 10, 20, 20, 10]))
    assert track.frames == 5 and track.change_points.tolist() == [0, 2, 4]
    assert track.reference_cuts.tolist() == [0, 2] and track.luminance.tolist() == [10, 10, 20, 20, 10]


def test_hdr10plus_change_points_ignore_admin_fields_but_see_bezier(tmp_path: Path) -> None:
    base = {"LuminanceParameters": {"AverageRGB": 1000, "MaxScl": [1, 2, 3]}, "NumberOfWindows": 1}
    entries = []
    for index in range(30):
        entry = json.loads(json.dumps(base))
        entry.update({"SceneFrameIndex": index % 15, "SceneId": index // 15, "SequenceFrameIndex": index})
        if index >= 20:
            entry["BezierCurveData"] = {"KneePointX": 5}
        entries.append(entry)
    path = tmp_path / "h.json"
    path.write_text(json.dumps({"SceneInfo": entries}), encoding="utf-8")
    track = hdr10plus_track(path)
    assert track.change_points.tolist() == [0, 20]
    assert track.reference_cuts.tolist() == [0, 15]
    assert track.luminance.size == 30 and track.luminance[0] > 0


def test_hdr10plus_scenes_recomputed_every_frame_are_not_cuts(tmp_path: Path) -> None:
    entries = [{"LuminanceParameters": {"AverageRGB": 100 + index}, "SceneFrameIndex": 0, "SceneId": index}
               for index in range(40)]
    path = tmp_path / "h.json"
    path.write_text(json.dumps({"SceneInfo": entries}), encoding="utf-8")
    assert hdr10plus_track(path).reference_cuts.size == 0


def test_combine_prefers_rpu_and_unites_change_points() -> None:
    a = MetadataTrack("hdr10plus", 10, np.asarray([0, 5]), np.zeros(0, dtype=np.int64), np.ones(10, np.float32))
    b = MetadataTrack("rpu", 10, np.asarray([0, 3]), np.asarray([0, 3]), np.full(10, 2.0, np.float32))
    merged = combine([a, b])
    assert merged.change_points.tolist() == [0, 3, 5] and merged.reference_cuts.tolist() == [0, 3]
    assert merged.luminance[0] == 2.0 and merged.kinds == ("rpu", "hdr10plus")
    with pytest.raises(ValueError):
        combine([a, MetadataTrack("rpu", 11, np.asarray([0]), np.asarray([0]), np.zeros(0, np.float32))])


class Fade(Scene):
    """Fondu enchaîné entre deux plans (une image différente à chaque rang)."""

    def __init__(self, first: Scene, second: Scene, length: int) -> None:
        self.length, self.black, self.level = length, False, (first.level + second.level) / 2
        self.first, self.second = first, second

    def frame(self, index: int) -> np.ndarray:
        weight = (index + 1) / (self.length + 1)
        return (1 - weight) * self.first.frame(index) + weight * self.second.frame(index)


def test_fade_with_metadata_changing_on_each_frame_is_confirmed_by_image() -> None:
    rng = np.random.default_rng(15)
    scenes = film(rng)
    fade = Fade(scenes[20], scenes[21], 24)
    sequence = whole(scenes[:21]) + [(fade, 0, 24)] + whole(scenes[21:])
    meta = metadata_for(sequence)
    fade_start = sum(scene.length for scene in scenes[:21])
    points = np.unique(np.concatenate([meta.change_points, np.arange(fade_start, fade_start + 24)]))
    meta = Film2Metadata(meta.frames, points, meta.reference_cuts, meta.luminance, ("rpu",))
    head = Scene(rng, 12, black=True)
    film2 = [(head, 0, 12), *sequence]
    shifted = Film2Metadata(meta.frames + 12, np.concatenate([[0], points + 12]),
                            np.concatenate([[0], meta.reference_cuts + 12]),
                            np.concatenate([np.full(12, 50.0, np.float32), meta.luminance]), ("rpu",))
    result, _source, _full = run(sequence, film2, metadata=shifted)
    assert [(seg.offset, seg.length) for seg in result.segments] == [(12, meta.frames)]
    assert result.stats["image_windows"] > 0


def test_film1_at_half_resolution_aligns_by_picture_coordinates() -> None:
    rng = np.random.default_rng(16)
    scenes = whole(film(rng))
    sig1 = render(scenes, 4)
    half = Signatures(W // 2, H // 2, sig1.ranks, sig1.top // 2, sig1.bottom // 2, sig1.left, sig1.right,
                      sig1.vignettes, sig1.mean, sig1.std, sig1.peak)
    sig2 = render(scenes, 18)
    meta = metadata_for(scenes)
    result = align(half, meta, Film2Frames(meta.frames, window=lambda a, n: sig2.slice(a, a + n)), frame_rate=2.0)
    assert result.identity
