"""
tests/test_hdr10plus_options_l1.py — HDR10+ : options contradictoires refusées, repli L1 conservant la moyenne (N3,
N7 ; plan de remédiation 5.2.0, lot 11).
"""

from __future__ import annotations

import numpy as np
import pytest

from core.hdr10plus_generation import L1_HIGHLIGHT_EXPONENT, _l1_quantiles, l1_model
from core.hdr_inspection.patch import Hdr10PlusEdit
from core.service.catalog import CATALOG
from core.service.errors import ApiError
from core.service import call
from tests.test_cli_hdr_patch import _ctx, _measurement


def test_target_with_profile_a_or_recompute_is_refused_in_the_core():
    with pytest.raises(ValueError):
        Hdr10PlusEdit(targeted_display=400, drop_bezier=True)
    with pytest.raises(ValueError):
        Hdr10PlusEdit(targeted_display=400, recompute=(object(), object()))
    assert Hdr10PlusEdit(targeted_display=400).targeted_display == 400  # profil B : cible conservée
    assert Hdr10PlusEdit(targeted_display=0, drop_bezier=True).drop_bezier  # cible 0 : cohérente


@pytest.mark.parametrize("hdr10plus", [{"targeted_display_nits": 400, "profile_a": True},
                                       {"targeted_display_nits": 400, "recompute": True}])
def test_hdr_patch_refuses_the_contradiction(tmp_path, hdr10plus):
    """Reproduction du contre-audit : la cible 400 était annoncée par le plan puis écrite 0."""
    from core.hdr_inspection import InspectionCache

    source = tmp_path / "film.mkv"
    source.write_bytes(b"\x1a\x45\xdf\xa3")
    InspectionCache(tmp_path / "data" / "hdr_inspection").store_measurement(source, 0, _measurement(tmp_path))
    with pytest.raises(ApiError):
        call(CATALOG, "hdr.patch", {"source": str(source), "output": str(tmp_path / "fix.mkv"),
                                    "hdr10plus": hdr10plus, "measurement": "cached", "dry_run": True},
             _ctx(tmp_path))


def _model_mean(low: float, mean: float, high: float) -> float:
    lo, me, hi = (np.array([value]) for value in (low, mean, high))
    median, upper, lower = l1_model(lo, me, hi, L1_HIGHLIGHT_EXPONENT)
    positions = (np.arange(200_000) + 0.5) / 200_000  # intégrale fine du modèle
    return float(_l1_quantiles(lo, median, hi, positions, upper, lower).mean())


@pytest.mark.parametrize("low,mean,high", [
    (100, 150, 4000),     # contre-audit : image presque noire à point lumineux (moyenne < min + 12,5 % Δ)
    (100, 3500, 4000),    # Codex : image presque uniformément au pic (moyenne > min + 75 % Δ)
    (100, 100, 4000),     # moyenne = min
    (100, 4000, 4000),    # moyenne = max
    (2000, 2000, 2000),   # min = max : distribution dégénérée exacte
    (100, 1200, 4000),    # image ordinaire
])
def test_l1_model_preserves_the_pq_mean(low, mean, high):
    scale = 4095.0
    assert _model_mean(low / scale, mean / scale, high / scale) * scale == pytest.approx(mean, abs=2.0)


def test_ordinary_images_keep_the_previous_model():
    """Non-régression : moyenne dans la plage courante → médiane ajustée, exposants inchangés (k, 1)."""
    low, mean, high = (np.array([value / 4095.0]) for value in (100, 1200, 4000))
    median, upper, lower = l1_model(low, mean, high, L1_HIGHLIGHT_EXPONENT)
    tail = 1.0 / (2.0 * (L1_HIGHLIGHT_EXPONENT + 1.0))
    expected = (mean - low / 4.0 - high * tail) / (0.75 - tail)
    assert median == pytest.approx(expected) and upper[0] == L1_HIGHLIGHT_EXPONENT and lower[0] == 1.0
