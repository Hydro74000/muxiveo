"""Panneau Encodage : case « Générer les métadonnées HDR10+ depuis le Dolby Vision »."""
from __future__ import annotations

from pathlib import Path

import pytest

from core.config import AppConfig
from core.inspector import HDRType
from ui.panels.encode_panel import EncodePanel
from tests.test_encode_panel_widgets import (  # noqa: F401 — fixtures autouse importées
    _COLOR,
    _PATH_A,
    _PATH_B,
    _file_info,
    _french_ui,
    _no_real_hardware_probe,
    _select_codec,
    _video_entry,
    _video_track,
)

DV = HDRType.DOLBY_VISION
DV_PLUS = HDRType.DOLBY_VISION_HDR10PLUS


def _panel(*sources: tuple[Path, HDRType, int | None, int | None]) -> EncodePanel:
    panel = EncodePanel(AppConfig())
    panel.set_output_provider(lambda: Path("/tmp/out.mkv"))
    tracks = []
    for index, (path, hdr, profile, compat) in enumerate(sources):
        video = _video_track(0, hdr)
        video.color_transfer = "smpte2084"
        video.dovi_profile, video.dovi_compat_id = profile, compat
        entry = _video_entry(index)
        entry.entry_id = f"t{index}"
        tracks.append((_file_info(path, [video], hdr_type=hdr), entry, _COLOR))
    panel.set_video_tracks(tracks)
    return panel


def _click(checkbox, checked: bool = True) -> None:
    if checkbox.isChecked() != checked:
        checkbox.click()


def test_offered_only_for_dolby_vision_and_hdr10plus_codecs(qt_app):
    panel = _panel((_PATH_A, DV, 8, 1))
    try:
        _select_codec(panel, "libx265")
        assert panel._generate_hdr10plus_cb.isEnabled() and not panel._generate_hdr10plus_cb.isChecked()
        _click(panel._generate_hdr10plus_cb)
        video = panel._current_video_settings()
        assert video.generate_hdr10plus and not video.copy_hdr10plus and video.dovi_source_profile == "p8_1"
        _select_codec(panel, "libsvtav1")  # sans HDR10+ : grisée, intention mémorisée
        assert not panel._generate_hdr10plus_cb.isEnabled() and not panel._generate_hdr10plus_cb.isChecked()
        assert not panel._current_video_settings().generate_hdr10plus
        _select_codec(panel, "libx265")
        assert panel._generate_hdr10plus_cb.isChecked()
    finally:
        panel.close()
    hdr10 = _panel((_PATH_A, HDRType.HDR10, None, None))
    try:
        _select_codec(hdr10, "libx265")
        assert not hdr10._generate_hdr10plus_cb.isEnabled()
    finally:
        hdr10.close()


def test_generation_and_copy_are_exclusive(qt_app):
    panel = _panel((_PATH_A, DV_PLUS, 8, 1))
    try:
        _select_codec(panel, "libx265")
        assert panel._copy_hdr10plus_cb.isChecked()
        _click(panel._generate_hdr10plus_cb)
        assert not panel._copy_hdr10plus_cb.isChecked()  # remplacement du HDR10+ de la source
        _click(panel._copy_hdr10plus_cb)
        assert not panel._generate_hdr10plus_cb.isChecked()
        video = panel._current_video_settings()
        assert video.copy_hdr10plus and not video.generate_hdr10plus
    finally:
        panel.close()


def test_copy_keeps_the_option_and_patches_the_stream(qt_app):
    panel = _panel((_PATH_A, DV_PLUS, 8, 1))
    try:
        _select_codec(panel, "copy")
        assert panel._generate_hdr10plus_cb.isEnabled() and not panel._copy_hdr10plus_cb.isEnabled()
        _click(panel._generate_hdr10plus_cb)
        assert not panel._copy_hdr10plus_cb.isChecked()
        config = panel.collect_config()
        assert config is not None
        video = config.video_tracks[0]
        assert video.codec == "copy" and video.generate_hdr10plus and not video.copy_hdr10plus and video.copy_dv
        state = panel._video_settings_by_entry_id["t0"]
        assert "10+" in panel._video_hdr_badges_from_state(state, source_video=panel._selected_video_track())
    finally:
        panel.close()


@pytest.mark.parametrize("profile,compat,codec,enabled", [
    (5, 0, "copy", False), (5, 0, "libx265", True), (8, 4, "libx265", False), (8, 2, "copy", False),
])
def test_base_layer_rules(qt_app, profile, compat, codec, enabled):
    panel = _panel((_PATH_A, DV, profile, compat))
    try:
        _select_codec(panel, codec)
        assert panel._generate_hdr10plus_cb.isEnabled() is enabled
        if not enabled:
            assert panel._generate_hdr10plus_cb.toolTip()
    finally:
        panel.close()


def test_tonemap_disables_generation(qt_app):
    panel = _panel((_PATH_A, DV, 8, 1))
    try:
        _select_codec(panel, "libx265")
        _click(panel._generate_hdr10plus_cb)
        _click(panel._tonemap_cb)
        assert not panel._generate_hdr10plus_cb.isEnabled()
        assert not panel._current_video_settings().generate_hdr10plus
    finally:
        panel.close()


def test_apply_all_spreads_the_intent_to_dolby_vision_sources_only(qt_app):
    panel = _panel((_PATH_A, DV, 8, 1), (_PATH_B, HDRType.HDR10PLUS, None, None))
    try:
        _select_codec(panel, "libx265")
        panel._on_apply_all_video_toggled(True)
        _click(panel._generate_hdr10plus_cb)
        config = panel.collect_config()
        assert config is not None
        by_path = {video.source_path: video for video in config.video_tracks}
        assert by_path[_PATH_A].generate_hdr10plus and not by_path[_PATH_A].copy_hdr10plus
        assert not by_path[_PATH_B].generate_hdr10plus and by_path[_PATH_B].copy_hdr10plus  # copie conservée
    finally:
        panel.close()


def test_state_survives_track_switch(qt_app):
    panel = _panel((_PATH_A, DV, 8, 1), (_PATH_B, DV, 8, 1))
    try:
        _select_codec(panel, "libx265")
        _click(panel._generate_hdr10plus_cb)
        panel._video_list.setCurrentRow(1)
        assert not panel._generate_hdr10plus_cb.isChecked()
        panel._video_list.setCurrentRow(0)
        assert panel._generate_hdr10plus_cb.isChecked()
    finally:
        panel.close()


def test_missing_extension_announces_the_l1_estimate(qt_app, monkeypatch):
    from core import plugins

    monkeypatch.setattr(plugins, "installed_plugin", lambda spec, *a, **k: None)
    panel = _panel((_PATH_A, DV, 8, 1))
    try:
        _select_codec(panel, "libx265")
        _click(panel._generate_hdr10plus_cb)
        assert not panel._generate_hdr10plus_note.isHidden() and "L1" in panel._generate_hdr10plus_note.text()
    finally:
        panel.close()
