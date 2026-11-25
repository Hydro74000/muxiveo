"""Merge DoVi — alignement : moteur de correspondance sur films synthétiques (sans média)."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from core.workflows.merge_dovi_align import align as align_module
from core.workflows.merge_dovi_align.align import (
    MAX_STATES,
    AlignmentError,
    Film2Frames,
    _check_order,
    _Context,
    _frame_scan,
    _ShotModel,
    _isotonic,
    _refine_junction,
    align,
    candidate_states,
    coarse_offsets,
    detect_cuts,
    Segment,
)
from core.workflows.merge_dovi_align.features import active_band
from core.workflows.merge_dovi_align.process import ProcessHooks
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
                 black: bool = False, contrast: float = 220.0) -> None:
        self.length, self.black = length, black
        low = rng.uniform(0, 1, size=(11, 30))
        big = np.kron(low, np.ones((10, 10)))  # 110 × 300 : marge pour le glissement d'un plan
        self.level = level if level is not None else float(rng.uniform(200, 700))
        self.texture = self.level + contrast * (big - big.mean())

    def frame(self, index: int, dx: int = 0) -> np.ndarray:
        if self.black:
            return np.full((H, W), BLACK, dtype=np.float64)
        return self.texture[2:H + 2, 20 + index + dx:20 + index + dx + W]


def render(scenes: list[tuple[Scene, int, int]], bars: int, *, pillar: int = 0, bottom: int | None = None,
           dx: int = 0, dx_per_scene: dict[int, int] | None = None) -> Signatures:
    """Signatures d'une suite (plan, première image du plan, nombre d'images) ; barres haut ``bars`` et bas
    ``bottom`` (``bars`` par défaut), translation horizontale ``dx`` (pixels), par plan si demandé."""
    bottom = bars if bottom is None else bottom
    vignettes, tops, bottoms, lefts, means, stds, peaks = [], [], [], [], [], [], []
    for number, (scene, first, count) in enumerate(scenes):
        shift = (dx_per_scene or {}).get(number, dx)
        for index in range(first, first + count):
            picture = scene.frame(index, shift).copy()
            if not scene.black:
                picture[:bars] = 0
                picture[H - bottom:] = 0
                if pillar:
                    picture[:, :pillar] = 0
            cells = picture.reshape(16, 6, 24, 6).mean(axis=(1, 3))
            vignettes.append((np.clip(cells, 0, 1023) / 4).astype(np.uint8))
            visible = not scene.black
            tops.append(bars if visible else -1)
            bottoms.append(bottom if visible else -1)
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
    starts, luminance, peaks, position = [], [], [], 0
    for scene, first, count in scenes:
        starts.append(position)
        luminance += [scene.level * 3.2 + 100.0] * count
        peak = 100.0 if scene.black else float(scene.frame(first).max())
        peaks += [peak * 2.5 + 300.0] * count
        position += count
    cuts = np.asarray(starts, dtype=np.int64)
    return Film2Metadata(position, cuts, cuts, np.asarray(luminance, dtype=np.float32), ("rpu",),
                         np.asarray(peaks, dtype=np.float32))


def film(rng: np.random.Generator, shots: int = 60) -> list[Scene]:
    return [Scene(rng, int(rng.integers(20, 90))) for _ in range(shots)]


def whole(scenes: list[Scene]) -> list[tuple[Scene, int, int]]:
    return [(scene, 0, scene.length) for scene in scenes]


def run(film1: list[tuple[Scene, int, int]], film2: list[tuple[Scene, int, int]], *, bars1: int = 4,
        bars2: int = 18, windowed: bool = True, frame_rate: float = 2.0, sig2: Signatures | None = None,
        budget: float = 0.25, **kwargs):
    sig1 = render(film1, bars1)
    sig2 = sig2 if sig2 is not None else render(film2, bars2)
    meta = kwargs.pop("metadata", None) or metadata_for(film2)
    full_calls = []

    def full():
        full_calls.append(True)
        return sig2

    source = Film2Frames(meta.frames, window=(lambda a, n: sig2.slice(a, a + n)) if windowed else None,
                         full_loader=full, budget=budget)
    result = align(sig1, meta, source, frame_rate=frame_rate, **kwargs)
    return result, source, full_calls


def test_identity_is_verified_without_edit() -> None:
    scenes = whole(film(np.random.default_rng(1)))
    result, _source, full = run(scenes, scenes, bars2=4, frame_rate=0.5)
    assert result.identity and not result.removed and not full


def test_constant_offset_with_black_head_and_tail_like_the_imax_case() -> None:
    rng = np.random.default_rng(2)
    content = whole(film(rng))
    head, tail = Scene(rng, 16, black=True), Scene(rng, 24, black=True)
    film2 = [(head, 0, 16), *content, (tail, 0, 24)]
    result, source, full = run(content, film2, frame_rate=0.5)
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
    assert all(16 in offsets.tolist() for offsets, _votes in candidates if offsets.size)
    # Décalage négatif plus grand que le début du bloc (Film 1 avec des images en plus en tête), bloc unique.
    later = cuts[cuts >= 40] - 40
    single = coarse_offsets(cuts, len(sig), later, len(sig), block=len(sig))
    assert single[0][0].tolist()[0] == -40


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

    def frame(self, index: int, dx: int = 0) -> np.ndarray:
        weight = (index + 1) / (self.length + 1)
        return (1 - weight) * self.first.frame(index, dx) + weight * self.second.frame(index, dx)


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
    assert result.stats["points_by_image"] >= 20  # points du fondu confirmés un par un par l'image


def test_film1_at_half_resolution_aligns_by_picture_coordinates() -> None:
    rng = np.random.default_rng(16)
    scenes = whole(film(rng))
    sig1 = render(scenes, 4)
    half = Signatures(W // 2, H // 2, sig1.ranks, sig1.top // 2, sig1.bottom // 2, sig1.left, sig1.right,
                      sig1.vignettes, sig1.mean, sig1.std, sig1.peak)
    sig2 = render(scenes, 18)
    meta = metadata_for(scenes)
    result = align(half, meta, Film2Frames(meta.frames, window=lambda a, n: sig2.slice(a, a + n)), frame_rate=0.5)
    assert result.identity



# --- Relecture du lot 2 ---------------------------------------------------------------------------


def test_reversed_order_never_maps_film2_backwards() -> None:
    rng = np.random.default_rng(20)
    scenes = film(rng)
    film1 = whole(scenes[30:] + scenes[:30])
    try:
        result, _source, _full = run(film1, whole(scenes))
    except AlignmentError:
        return
    _check_order(list(result.segments))
    assert any(not gap.black for gap in result.uncovered)  # la seconde moitié reste sans correspondance


def test_coarse_candidates_skip_ineligible_peaks() -> None:
    # Bloc : coupes 10, 20, 30, 40. Film 2 : trois coupes exactes à +500, et des coupes voisines (±1) à +7.
    cuts1 = np.asarray([0, 10, 20, 30, 40])
    cuts2 = np.asarray([510, 520, 530, 18, 26, 36, 48])
    out = coarse_offsets(cuts1, 60, cuts2, 700, block=60)
    assert 500 in out[0][0].tolist()


def test_junction_with_insertion_larger_than_window_is_refused() -> None:
    rng = np.random.default_rng(21)
    scenes = whole(film(rng, 20))
    sig = render(scenes, 4)
    ctx = _Context(sig, Film2Frames(len(sig), full=sig), active_band(sig), 0.0, 0.0, ProcessHooks())
    with pytest.raises(AlignmentError):
        _refine_junction(ctx, 100, 140, 300, 0)


def test_position_needs_a_strict_lead_except_on_still_content() -> None:
    rng = np.random.default_rng(22)
    moving = Scene(rng, 60)

    class Still(Scene):
        def frame(self, index: int, dx: int = 0) -> np.ndarray:
            return super().frame(0, dx)

    still = Still(rng, 60)
    sig = render([(moving, 0, 60), (still, 0, 60)], 4)
    source = Film2Frames(len(sig), full=sig)
    ctx = _Context(sig, source, active_band(sig), 0.0, 0.0, ProcessHooks())
    assert ctx.confirm_position(30, 0)           # mouvement : décalage 0 strictement meilleur
    assert not ctx.confirm_position(30, 1)       # décalage d'une image : refusé
    assert ctx.confirm_position(90, 1)           # plan fixe : position sans effet


def test_one_sided_information_is_a_disagreement() -> None:
    rng = np.random.default_rng(23)
    scenes = whole(film(rng, 10))
    sig1 = render(scenes, 4)
    black = [(Scene(rng, sum(c for _s, _f, c in scenes), black=True), 0, sum(c for _s, _f, c in scenes))]
    sig2 = render(black, 4)
    ctx = _Context(sig1, Film2Frames(len(sig2), full=sig2), active_band(sig1), 0.0, 0.0, ProcessHooks())
    assert ctx.confirm_content(np.arange(10, 16), 0) == "fail"
    assert not ctx.confirm_position(12, 0)


@pytest.mark.usefixtures("french_messages")
def test_full_pass_checks_every_change_point_individually() -> None:
    """Deux images de Film 2 échangées autour d'un point de changement interne à un plan en mouvement
    rapide (trop court pour une découpe) : refus du point, pas d'acceptation globale."""
    rng = np.random.default_rng(24)

    class Fast(Scene):
        def frame(self, index: int, dx: int = 0) -> np.ndarray:
            return super().frame(3 * index, dx)

    scenes: list[Scene] = [Fast(rng, int(rng.integers(24, 40))) for _ in range(40)]
    film1 = whole(scenes)
    sig1 = render(film1, 4)
    point = sum(scene.length for scene in scenes[:10]) + scenes[10].length // 2
    order = np.arange(len(sig1))
    order[point - 1], order[point] = point, point - 1
    swapped = align_module.take(render(film1, 4), order)
    meta = metadata_for(film1)
    meta = type(meta)(meta.frames, np.unique(np.append(meta.change_points, point)), meta.reference_cuts,
                      meta.luminance, meta.kinds, meta.peak)
    with pytest.raises(AlignmentError, match="point de changement"):
        align(sig1, meta, Film2Frames(meta.frames, full_loader=lambda: swapped), frame_rate=2.0,
              force_full_film2=True)
    # Sans l'échange, la même vérification exhaustive accepte la carte identité.
    result = align(sig1, meta, Film2Frames(meta.frames, full_loader=lambda: render(film1, 4)), frame_rate=2.0,
                   force_full_film2=True)
    assert result.identity


def test_too_few_calibration_shots_force_the_full_pass() -> None:
    rng = np.random.default_rng(25)
    scenes = whole(film(rng, 40))
    result, source, full = run(scenes, scenes, bars2=4, frame_rate=24.0)
    assert full and "étalonnable" in source.full_reason and result.identity
    assert result.stats["exhaustive"]  # parcours repris entièrement sur la passe complète


def test_window_budget_is_global() -> None:
    rng = np.random.default_rng(26)
    scenes = whole(film(rng))
    result, source, full = run(scenes, scenes, bars2=4, budget=0.01)
    assert full and "%" in source.full_reason and result.identity
    assert source.decoded <= 0.01 * result.film2_frames


def test_peaks_disagreeing_with_metadata_are_refused() -> None:
    rng = np.random.default_rng(27)
    scenes = [Scene(rng, 40, level=200.0 + 6 * k) for k in range(90)]
    film2 = whole(scenes)
    flat = Scene(rng, 40, level=scenes[45].level, contrast=20.0)   # même moyenne, pic bien plus bas
    film1 = whole(scenes[:45] + [flat] + scenes[46:])
    with pytest.raises(AlignmentError):
        run(film1, film2, bars2=4, frame_rate=0.5)


def test_horizontal_translation_is_estimated_and_variable_framing_refused() -> None:
    rng = np.random.default_rng(28)
    scenes = whole(film(rng))
    result, _source, _full = run(scenes, scenes, bars2=4, sig2=render(scenes, 4, dx=3))
    assert result.identity and abs(result.stats["dx"]) >= 0.01
    varying = {number: (6 if number % 2 else -6) for number in range(len(scenes))}
    with pytest.raises(AlignmentError):
        run(scenes, scenes, bars2=4, sig2=render(scenes, 4, dx_per_scene=varying))


def test_asymmetric_bars_and_pillarbox() -> None:
    rng = np.random.default_rng(29)
    scenes = whole(film(rng))
    sig2 = render(scenes, 6, bottom=20, pillar=8)
    result, _source, _full = run(scenes, scenes, sig2=sig2)
    assert result.identity


def test_dynamic_programming_states_are_capped() -> None:
    per_block = [(np.asarray([k * 1000, k * 1000 + 300, k * 1000 + 600]), np.asarray([9, 5, 4])) for k in range(60)]
    assert candidate_states(per_block).size <= MAX_STATES


def test_cancellation_is_checked_before_any_work() -> None:
    rng = np.random.default_rng(30)
    scenes = whole(film(rng, 10))
    calls: list[int] = []
    sig = render(scenes, 4)

    def window(a: int, n: int) -> Signatures:
        calls.append(a)
        return sig.slice(a, a + n)

    with pytest.raises(InterruptedError):
        align(sig, metadata_for(scenes), Film2Frames(len(sig), window=window), frame_rate=2.0,
              hooks=ProcessHooks(cancelled=lambda: True))
    assert not calls


def test_segments_order_check() -> None:
    with pytest.raises(AlignmentError):
        _check_order([Segment(0, 100, 500), Segment(100, 50, 400)])



def test_relaxed_max_marginals_match_brute_force() -> None:
    import itertools

    bounds = np.asarray([0, 10, 25, 40, 55])
    film1_cut = np.zeros(56, dtype=bool)
    film1_cut[bounds[:-1]] = True
    cuts2 = np.asarray([0, 10, 25, 47, 62])
    candidates = np.asarray([0, 7])
    model = _ShotModel(bounds, film1_cut, cuts2, 80, candidates, ProcessHooks())
    path, _score = model.solve()
    alternatives = model.alternatives(path)
    emissions = [model.emission(k, None) for k in range(model.shots)]
    for k in range(model.shots):
        best = -np.inf
        for states in itertools.product(range(model.states), repeat=model.shots):
            if states[k] == path[k]:
                continue
            total = sum(emissions[q][states[q]] for q in range(model.shots))
            total -= align_module.CHANGE_PENALTY * sum(a != b for a, b in zip(states, states[1:]))
            best = max(best, total)
        assert alternatives[k] == pytest.approx(best)


def test_unmatched_shots_never_resume_backwards_in_film2() -> None:
    rng = np.random.default_rng(31)
    for _trial in range(20):
        lengths = rng.integers(8, 30, size=30)
        bounds = np.concatenate([[0], np.cumsum(lengths)])
        film1_cut = np.zeros(int(bounds[-1]) + 1, dtype=bool)
        film1_cut[bounds[:-1]] = True
        # Film 2 répète une partie du film : un chemin sans contrainte relirait Film 2 à rebours.
        part = bounds[:-1]
        cuts2 = np.unique(np.concatenate([part + 300, part[10:20] + 300 - 260]))
        candidates = np.arange(-400, 401, 20)
        model = _ShotModel(bounds, film1_cut, cuts2, 2000, candidates, ProcessHooks())
        offsets = model.offsets(model.solve()[0])
        end = -np.inf
        for k, d in enumerate(offsets.tolist()):
            if d == align_module._NONE:
                continue
            start = bounds[k] + d
            assert start >= end - align_module.MAX_INSERT
            end = bounds[k + 1] + d


@pytest.mark.usefixtures("french_messages")
def test_missing_peak_metadata_forces_image_confirmation() -> None:
    rng = np.random.default_rng(32)
    scenes = whole(film(rng))
    meta = metadata_for(scenes)
    no_peak = Film2Metadata(meta.frames, meta.change_points, meta.reference_cuts, meta.luminance, meta.kinds)
    logs: list[str] = []
    result, _source, _full = run(scenes, scenes, bars2=4, frame_rate=0.5, metadata=no_peak, log=logs.append)
    assert result.identity and any("Pic non décrit" in line for line in logs)


def test_frame_scan_finds_a_short_gap_across_block_boundaries() -> None:
    """Images 4095–4097 de Film 1 retrouvées deux rangs plus tôt dans Film 2 (à cheval sur la frontière des
    blocs de calcul, 4096) : écart local retenu d'un seul tenant."""
    rng = np.random.default_rng(33)

    class Noise(Scene):
        def frame(self, index: int, dx: int = 0) -> np.ndarray:
            return 300 + 200 * np.random.default_rng(index).uniform(-1, 1, size=(H, W))

    scene = Noise(rng, 4200)
    sig1 = render([(scene, 0, 4200)], 4)
    order = np.arange(4200)
    order[4093:4098] = np.arange(4095, 4100)
    sig2 = align_module.take(sig1, order)
    ctx = _Context(sig1, Film2Frames(4200, full=sig2), active_band(sig1), 0.0, 0.0, ProcessHooks())
    assert (4095, 4098, -2) in _frame_scan(ctx, Segment(0, 4200, 0))


def test_constrained_solver_matches_brute_force_with_unmatched_resumes() -> None:
    import itertools

    rng = np.random.default_rng(34)
    for _trial in range(12):
        lengths = rng.integers(10, 40, size=6)
        bounds = np.concatenate([[0], np.cumsum(lengths)])
        film1_cut = np.zeros(int(bounds[-1]) + 1, dtype=bool)
        film1_cut[bounds[:-1]] = True
        candidates = np.asarray([0, 150, 450])
        cuts2 = np.unique(rng.choice(np.arange(0, 700), size=12, replace=False))
        cuts2 = np.unique(np.concatenate([cuts2, bounds[rng.integers(0, 6, size=3)] + candidates[rng.integers(0, 3)]]))
        model = _ShotModel(bounds, film1_cut, cuts2, 700, candidates, ProcessHooks())
        _path, total = model.solve()
        emissions = [model.emission(k, None) for k in range(model.shots)]
        best = -np.inf
        for states in itertools.product(range(model.states), repeat=model.shots):
            score = emissions[0][states[0]]
            feasible = True
            position = -np.inf      # fin dans Film 2 du dernier plan placé
            previous = states[0]
            if previous != model.none:
                position = bounds[1] + candidates[previous]
            for k in range(1, model.shots):
                state = states[k]
                if state != previous:
                    score -= align_module.CHANGE_PENALTY
                    if state != model.none:
                        start = bounds[k] + candidates[state]
                        if previous == model.none:
                            feasible &= start >= position
                        else:
                            feasible &= candidates[previous] <= candidates[state] + align_module.MAX_INSERT
                score += emissions[k][state]
                if state != model.none:
                    position = bounds[k + 1] + candidates[state]
                previous = state
            if feasible:
                best = max(best, score)
        assert total == pytest.approx(best)
