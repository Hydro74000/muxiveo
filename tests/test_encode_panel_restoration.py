"""Panneau Encodage : réglages de la Restauration (disponibilité selon le moteur, niveaux, mode rapide, animation,
état par piste, profils, badges)."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from core.config import AppConfig
from core.inspector import HDRType
from core.workflows.encode import EncodePreset, ProfileManager, RestoreSettings
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


def test_availability_follows_engine_capabilities(panel, monkeypatch):
    _sync(panel, _caps())
    assert panel._restore_tool_flag()
    _sync(panel, _caps(options=()))
    assert not panel._restore_tool_flag() and "mettre à jour" in panel._restore_cb.toolTip()
    monkeypatch.setattr(panel, "_interpolation_tool_available", lambda: False)
    _sync(panel, _caps())
    assert not panel._restore_tool_flag() and "non installée" in panel._restore_cb.toolTip()


def test_fast_mode_needs_its_graphs(panel):
    _sync(panel, _caps(models=("r2",)))
    panel._apply_restore_settings(RestoreSettings(enabled=True, fast=True))
    assert not panel._restore_mode_combo.isEnabled()
    assert panel._current_restore_settings().fast is False
    _sync(panel, _caps())
    assert panel._restore_mode_combo.isEnabled() and panel._current_restore_settings().fast is True


def test_settings_roundtrip_and_animation_only_for_auto(panel):
    _sync(panel, _caps())
    panel._apply_restore_settings(RestoreSettings(enabled=True, level="fort", fast=True, animation="off"))
    assert panel._current_restore_settings() == RestoreSettings(enabled=True, level="fort", fast=True, animation="off")
    assert not panel._restore_animation_combo.isEnabled()
    panel._restore_level_combo.setCurrentIndex(panel._restore_level_combo.findData("auto"))
    assert panel._restore_animation_combo.isEnabled()
    panel._restore_cb.setChecked(False)
    assert not panel._restore_level_combo.isEnabled() and not panel._current_restore_settings().enabled


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
    panel._restore_cb.setChecked(True)
    video = panel._current_video_settings()
    assert video.restores() and resolve_output_bit_depth(video) == 10  # Auto → 10 bits sous Restauration
    assert RestoreSettings.from_value(panel._video_settings_by_entry_id["video-restore"]["restoration"]).enabled


def test_profile_roundtrip(panel, tmp_path):
    _sync(panel, _caps())
    panel._profiles = ProfileManager(tmp_path / "profiles")
    panel._profiles.save(EncodePreset(name="grain", codec="libx265",
                                      restoration=RestoreSettings(enabled=True, level="leger", animation="on")))
    panel._refresh_profiles(select="grain")
    panel._load_profile()
    assert panel._current_restore_settings() == RestoreSettings(enabled=True, level="leger", animation="on")
