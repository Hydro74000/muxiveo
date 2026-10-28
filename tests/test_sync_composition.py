"""Composition et réciproque des calibrations (chaînes de références du panneau Conteneur)."""
from __future__ import annotations

from fractions import Fraction

import numpy as np
import pytest

from core.workflows.cadence import CadenceMismatch, CadenceType
from core.workflows.sync_calibration import SyncCalibration, SyncSegment, compose_calibrations, invert_calibration

_PAL = CadenceMismatch(CadenceType.PAL_TO_FILM_23976, 25.0, 23.976, 24000.0 / 25025.0, speed_ratio="24000/25025")


def _placed(calibration: SyncCalibration, donor_ms: float) -> float:
    """Placement brut d'un instant (formule du segment qui le contient)."""
    index = max(i for i, s in enumerate(calibration.segments) if i == 0 or s.start_ms <= donor_ms)
    return donor_ms * calibration._stretch_ratio() + calibration.segments[index].shift_ms


def _shifts(calibration: SyncCalibration) -> list[tuple[float, float]]:
    return [(s.start_ms, s.shift_ms) for s in calibration.segments]


def test_constant_reference_offset_is_added():
    # Référence calée à +250 ms, cible mesurée à −320 ms face à son audio d'origine.
    assert _shifts(compose_calibrations(SyncCalibration.linear(-320), SyncCalibration.linear(250))) == [(0, -70)]


def test_relative_samples_kept_only_against_an_unmoved_reference():
    measured = SyncCalibration((SyncSegment(0, -320),), samples=({"start_ms": 0.0, "shift_ms": -320, "confidence": .9},))
    assert compose_calibrations(measured, SyncCalibration.linear(0)).samples == measured.samples
    assert compose_calibrations(measured, SyncCalibration.linear(250)).samples == ()


def test_reference_cut_creates_a_junction_in_the_target():
    reference = SyncCalibration((SyncSegment(0, 0), SyncSegment(60000, -1000)))
    composed = compose_calibrations(SyncCalibration.linear(100), reference)
    assert _shifts(composed) == [(0, 100), (59900, -900)]


def test_composition_matches_successive_placements():
    rng = np.random.default_rng(7)
    inner = SyncCalibration((SyncSegment(0, 120), SyncSegment(40000, -800), SyncSegment(90000, 1500)))
    outer = SyncCalibration((SyncSegment(0, -300), SyncSegment(30000, 2000), SyncSegment(100000, -50)))
    composed = compose_calibrations(inner, outer)
    junctions = [s.start_ms for s in composed.segments]
    for donor in rng.uniform(0, 150000, 400):
        if min(abs(donor - j) for j in junctions) < 5:
            continue
        assert _placed(composed, donor) == pytest.approx(_placed(outer, _placed(inner, donor)))


@pytest.mark.parametrize("inner_cadence, outer_cadence", [(True, False), (False, True)])
def test_single_cadence_is_kept(inner_cadence, outer_cadence):
    inner = SyncCalibration((SyncSegment(0, 100),), cadence_mismatch=_PAL if inner_cadence else None)
    outer = SyncCalibration((SyncSegment(0, 250), SyncSegment(50000, 0)),
                            cadence_mismatch=_PAL if outer_cadence else None)
    composed = compose_calibrations(inner, outer)
    assert composed.cadence_mismatch == _PAL
    for donor in (1000.0, 30000.0, 80000.0):
        assert _placed(composed, donor) == pytest.approx(_placed(outer, _placed(inner, donor)))


def test_reciprocal_cadences_cancel_and_others_multiply():
    inner = SyncCalibration((SyncSegment(0, 0),), cadence_mismatch=_PAL)
    assert compose_calibrations(inner, SyncCalibration((SyncSegment(0, 0),), cadence_mismatch=_PAL.inverse())
                                ).cadence_mismatch is None
    film = CadenceMismatch(CadenceType.CUSTOM, 23.976, 24.0, 1001.0 / 1000.0, speed_ratio="1001/1000")
    combined = compose_calibrations(inner, SyncCalibration((SyncSegment(0, 0),), cadence_mismatch=film))
    assert combined.cadence_mismatch is not None
    assert combined.cadence_mismatch.cadence_type == CadenceType.CUSTOM
    assert combined.cadence_mismatch.speed_factor == pytest.approx(_PAL.speed_factor * film.speed_factor)
    assert Fraction(combined.cadence_mismatch.speed_ratio) == Fraction(24000, 25025) * Fraction(1001, 1000)


def test_cadence_inverse_round_trips():
    inverse = _PAL.inverse()
    assert inverse.cadence_type == CadenceType.FILM_23976_TO_PAL
    assert (inverse.source_fps, inverse.target_fps) == (23.976, 25.0)
    assert Fraction(inverse.speed_ratio) == Fraction(25025, 24000)
    assert inverse.time_stretch_ratio == pytest.approx(_PAL.speed_factor)
    back = inverse.inverse()
    assert back.cadence_type == _PAL.cadence_type and Fraction(back.speed_ratio) == Fraction(_PAL.speed_ratio)


def test_inverse_restores_played_instants():
    calibration = SyncCalibration((SyncSegment(0, 500), SyncSegment(40000, -1500), SyncSegment(80000, 3000)),
                                  cadence_mismatch=_PAL)
    inverse = invert_calibration(calibration)
    ranges = calibration.output_ranges()
    for donor in np.linspace(0, 120000, 241):
        output = _placed(calibration, donor)
        played = next((i for start, stop, i in ranges if start + 5 < output < stop - 5), None)
        segment = max(i for i, s in enumerate(calibration.segments) if i == 0 or s.start_ms <= donor)
        if played != segment:
            continue  # partie coupée ou bordure : pas d'antécédent unique
        assert _placed(inverse, output) == pytest.approx(donor)
    # Composée avec sa réciproque, une synchro constante revient à l'identité.
    assert _shifts(compose_calibrations(SyncCalibration.linear(-70), invert_calibration(SyncCalibration.linear(-70)))) \
        == [(0, 0)]


def _segments(calibration: SyncCalibration) -> list[tuple[float, float]]:
    return [(round(s.start_ms, 3), round(s.shift_ms, 3)) for s in calibration.segments]


def test_studio_round_trip_through_an_added_silence_is_identity():
    # Audit : référence avec 1 s de silence ajoutée à 60 s, cible indépendante.
    reference = SyncCalibration((SyncSegment(0, 0), SyncSegment(60000, 1000)))
    shown = compose_calibrations(SyncCalibration.linear(0), invert_calibration(reference))
    assert _segments(compose_calibrations(shown, reference)) == [(0, 0)]


def _random_calibration(rng, cadence):
    starts = sorted(rng.choice(np.arange(5000, 200000, 500), rng.integers(0, 4), replace=False).tolist())
    shifts = rng.integers(-3000, 3000, len(starts) + 1)
    return SyncCalibration(tuple(SyncSegment(float(a), float(b)) for a, b in zip([0, *starts], shifts)),
                           cadence_mismatch=_PAL if cadence else None)


@pytest.mark.parametrize("seed", range(40))
def test_round_trip_through_any_reference_keeps_the_sync(seed):
    """Validé sans modification, l'affichage relatif du studio redonne la synchro de départ."""
    rng = np.random.default_rng(seed)
    current = _random_calibration(rng, cadence=seed % 3 == 1)
    reference = _random_calibration(rng, cadence=seed % 3 == 2)
    restored = compose_calibrations(compose_calibrations(current, invert_calibration(reference)), reference)
    assert _segments(restored) == _segments(current)
    assert restored._stretch_ratio() == pytest.approx(current._stretch_ratio())


def test_requested_cadence_mode_is_serialised_composed_and_inverted():
    from core.workflows.cadence import detect_cadence_from_metadata

    pal = detect_cadence_from_metadata("24000/1001", "25/1")
    auto = SyncCalibration((SyncSegment(0, 300.0),), cadence_mismatch=pal, cadence_audio_method="atempo",
                           cadence_method_auto=True)
    explicit = SyncCalibration((SyncSegment(0, 300.0),), cadence_mismatch=pal, cadence_audio_method="atempo")
    assert SyncCalibration.from_dict(auto.to_dict()).cadence_method_requested == "auto"
    assert SyncCalibration.from_dict(explicit.to_dict()).cadence_method_requested == "atempo"
    assert "cadence_method_auto" not in explicit.to_dict()
    # Sans conversion, le mode demandé n'est pas sérialisé.
    assert "cadence_method_auto" not in SyncCalibration((SyncSegment(0, 250.0),), cadence_method_auto=True).to_dict()
    undecided = SyncCalibration((SyncSegment(0, 0.0),), cadence_mismatch=pal, cadence_audio_method="auto")
    assert undecided.cadence_method_requested == "auto"
    position = SyncCalibration.linear(250)
    assert compose_calibrations(auto, position).cadence_method_auto
    assert not compose_calibrations(explicit, position).cadence_method_auto
    assert invert_calibration(auto).cadence_method_auto
