"""Panneau Encodage : ligne Débruitage (MVO-R2 par défaut, NLMeans au choix) — disponibilité de MVO-R2 selon le
moteur, options affichées selon le filtre, niveaux, mode rapide, animation, état par piste, profils, badges."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from core.config import AppConfig
from core.inspector import HDRType
from core.workflows.encode import EncodePreset, ProfileManager, RestoreSettings, VideoFilterSettings
from core.workflows.encode.domain.codecs import resolve_output_bit_depth
from core.workflows.encode.interpolation import RifeCapabilities
from tests.test_encode_panel_widgets import _COLOR, _PATH_A, _file_info, _select_codec, _video_entry, _video_track
from ui.panels.encode_panel.panel import EncodePanel


def _caps(models=("r2", "r2q"), options=("restore",)) -> RifeCapabilities:
    return RifeCapabilities((1, 12, 0), 1, frozenset(options), frozenset(), "ok", frozenset(models))


@pytest.fixture
def panel(qt_app, monkeypatch):
    _ = qt_app
    widget = EncodePanel(AppConfig())
    monkeypatch.setattr(widget, "_interpolation_tool_available", lambda: True)
    _select_codec(widget, "libx265")  # codec copy : carte des filtres désactivée
    yield widget
    widget.close()


def _sync(panel: EncodePanel, capabilities: RifeCapabilities | None) -> None:
    with patch("ui.panels.encode_panel.panel.rife_capabilities", return_value=capabilities):
        panel._sync_interpolation_availability()


def _r2_item_enabled(panel: EncodePanel) -> bool:
    model = panel._denoise_filter_combo.model()
    return model.item(panel._denoise_filter_combo.findData("mvo-r2")).isEnabled()


def _tip(panel: EncodePanel, value: str) -> str:
    combo = panel._denoise_filter_combo
    return str(combo.itemData(combo.findData(value), 3))  # Qt.ItemDataRole.ToolTipRole


def test_mvo_r2_is_the_default_filter_when_available(panel):
    _sync(panel, _caps())
    assert panel._denoise_filter() == "mvo-r2" and _r2_item_enabled(panel)
    panel._denoise_cb.setChecked(True)
    assert panel._current_restore_settings().enabled and not panel._current_filter_settings().nlmeans_enabled


def test_unavailable_engine_falls_back_to_nlmeans_without_substitution(panel, monkeypatch):
    _sync(panel, _caps())
    panel._denoise_cb.setChecked(True)
    _sync(panel, _caps(options=()))
    assert not _r2_item_enabled(panel) and "mettre à jour" in _tip(panel, "mvo-r2")
    assert panel._denoise_filter() == "nlmeans" and not panel._denoise_cb.isChecked()  # jamais NLMeans à la place
    monkeypatch.setattr(panel, "_interpolation_tool_available", lambda: False)
    _sync(panel, _caps())
    assert not _r2_item_enabled(panel) and "non installé" in _tip(panel, "mvo-r2")



def test_user_choice_of_nlmeans_survives_engine_changes(panel):
    _sync(panel, _caps(options=()))
    combo = panel._denoise_filter_combo
    combo.activated.emit(combo.findData("nlmeans"))  # choix de l'utilisateur
    _sync(panel, _caps())
    assert panel._denoise_filter() == "nlmeans"

def test_options_follow_the_selected_filter(panel):
    _sync(panel, _caps())
    r2 = (panel._restore_level_combo, panel._restore_mode_combo, panel._restore_animation_combo)
    nlmeans = (panel._nlmeans_strength_combo, panel._nlmeans_profile_combo)
    assert all(not w.isHidden() for w in r2) and all(w.isHidden() for w in nlmeans)
    panel._denoise_cb.setChecked(True)
    panel._set_combo_data(panel._denoise_filter_combo, "nlmeans")
    assert all(w.isHidden() for w in r2) and all(not w.isHidden() for w in nlmeans)
    filters = panel._current_filter_settings()
    assert filters.nlmeans_enabled and filters.denoise_filter == "nlmeans"
    assert not panel._current_restore_settings().enabled


def test_fast_mode_needs_its_graphs(panel):
    _sync(panel, _caps(models=("r2",)))
    panel._apply_denoise_settings(VideoFilterSettings(), RestoreSettings(enabled=True, fast=True))
    assert not panel._restore_mode_combo.isEnabled()
    assert panel._current_restore_settings().fast is False
    _sync(panel, _caps())
    assert panel._restore_mode_combo.isEnabled() and panel._current_restore_settings().fast is True


def test_settings_roundtrip_and_animation_only_for_auto(panel):
    _sync(panel, _caps())
    settings = RestoreSettings(enabled=True, level="fort", fast=True, animation="off")
    panel._apply_denoise_settings(VideoFilterSettings(), settings)
    assert panel._current_restore_settings() == settings
    assert not panel._restore_animation_combo.isEnabled()
    panel._restore_level_combo.setCurrentIndex(panel._restore_level_combo.findData("auto"))
    assert panel._restore_animation_combo.isEnabled()
    panel._denoise_cb.setChecked(False)
    assert not panel._current_restore_settings().enabled


def test_apply_restores_the_effective_filter(panel):
    _sync(panel, _caps())
    panel._apply_denoise_settings(VideoFilterSettings(nlmeans_enabled=True, nlmeans_strength="strong"), RestoreSettings())
    assert panel._denoise_filter() == "nlmeans" and panel._denoise_cb.isChecked()
    assert panel._current_filter_settings().nlmeans_strength == "strong"
    # Filtre mémorisé, case décochée.
    panel._apply_denoise_settings(VideoFilterSettings(denoise_filter="nlmeans"), RestoreSettings())
    assert panel._denoise_filter() == "nlmeans" and not panel._denoise_cb.isChecked()
    panel._apply_denoise_settings(VideoFilterSettings(), RestoreSettings())
    assert panel._denoise_filter() == "mvo-r2" and not panel._denoise_cb.isChecked()


def test_badge_and_modified_state(panel):
    badges = panel._video_filter_badges_from_state({"restoration": RestoreSettings(enabled=True, level="fort", fast=True)})
    assert "MVO-R2 Fort Rapide" in badges
    assert not any("MVO-R2" in badge for badge in panel._video_filter_badges_from_state({"restoration": RestoreSettings()}))


def test_track_state_reaches_the_workflow_settings(panel):
    _sync(panel, _caps())
    entry = _video_entry(0)
    entry.entry_id = "video-restore"
    panel.set_video_tracks([(_file_info(_PATH_A, [_video_track(0, HDRType.NONE, bit_depth=8)]), entry, _COLOR)])
    _select_codec(panel, "libx265")
    panel._denoise_cb.setChecked(True)
    video = panel._current_video_settings()
    assert video.restores() and not video.filters.nlmeans_enabled
    assert resolve_output_bit_depth(video) == 10  # Auto → 10 bits sous MVO-R2
    assert RestoreSettings.from_value(panel._video_settings_by_entry_id["video-restore"]["restoration"]).enabled


def test_profile_roundtrip(panel, tmp_path):
    _sync(panel, _caps())
    panel._profiles = ProfileManager(tmp_path / "profiles")
    panel._profiles.save(EncodePreset(name="grain", codec="libx265",
                                      restoration=RestoreSettings(enabled=True, level="leger", animation="on")))
    panel._profiles.save(EncodePreset(name="nlm", codec="libx265",
                                      filters=VideoFilterSettings(nlmeans_enabled=True, denoise_filter="nlmeans")))
    panel._refresh_profiles(select="grain")
    panel._load_profile()
    assert panel._current_restore_settings() == RestoreSettings(enabled=True, level="leger", animation="on")
    panel._refresh_profiles(select="nlm")
    panel._load_profile()
    assert panel._denoise_filter() == "nlmeans" and panel._current_filter_settings().nlmeans_enabled
    assert not panel._current_restore_settings().enabled
