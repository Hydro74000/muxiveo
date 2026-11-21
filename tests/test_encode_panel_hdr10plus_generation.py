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


def test_unchecking_generation_restores_the_hdr10plus_copy(qt_app):
    for codec in ("copy", "libx265"):
        panel = _panel((_PATH_A, DV_PLUS, 8, 1))
        try:
            _select_codec(panel, codec)
            _click(panel._generate_hdr10plus_cb)
            assert not panel._copy_hdr10plus_cb.isChecked()
            _click(panel._generate_hdr10plus_cb, False)
            assert panel._copy_hdr10plus_cb.isChecked(), codec
            _select_codec(panel, "libx265")
            assert panel._current_video_settings().copy_hdr10plus, codec  # métadonnées de la source gardées
        finally:
            panel.close()


@pytest.mark.parametrize("preset_generates", [True, False])
def test_loading_a_preset_in_apply_all_sets_the_common_intent(qt_app, monkeypatch, preset_generates):
    from core.workflows.encode.models import EncodePreset

    panel = _panel((_PATH_A, DV, 8, 1), (_PATH_B, DV, 8, 1))
    try:
        _select_codec(panel, "libx265")
        panel._on_apply_all_video_toggled(True)
        _click(panel._generate_hdr10plus_cb, not preset_generates)  # intention commune contraire au preset
        preset = EncodePreset(name="P", codec="libx265", generate_hdr10plus=preset_generates)
        monkeypatch.setattr(panel._profiles, "load_all", lambda: [preset])
        panel._profile_combo.addItem("P", "P")
        panel._profile_combo.setCurrentIndex(panel._profile_combo.findData("P"))
        panel._load_profile()
        config = panel.collect_config()
        assert config is not None
        assert [video.generate_hdr10plus for video in config.video_tracks] == [preset_generates] * 2
    finally:
        panel.close()


def test_fel_hdr10plus_choice_comes_back_with_the_copy(qt_app):
    panel = _panel((_PATH_A, DV_PLUS, 7, 6))
    try:
        _select_codec(panel, "libx265")
        if not panel._bake_fel_cb.isEnabled():
            pytest.skip("intégration FEL indisponible pour ce profil")
        _click(panel._bake_fel_cb)
        _click(panel._generate_hdr10plus_cb)
        assert panel._fel_hdr10plus_widget.isHidden()
        _click(panel._copy_hdr10plus_cb)
        assert not panel._fel_hdr10plus_widget.isHidden()
    finally:
        panel.close()


def test_old_extension_without_measure_announces_the_l1_estimate(qt_app, monkeypatch):
    from types import SimpleNamespace

    from core import hdr10plus_generation, plugins

    monkeypatch.setattr(plugins, "installed_plugin",
                        lambda spec, *a, **k: SimpleNamespace(path=Path("/x"), version="0.3.0", manifest={}))
    monkeypatch.setattr(hdr10plus_generation, "engine_generation_support",
                        lambda: hdr10plus_generation.EngineSupport(installed=True, frame_statistics=True))
    panel = _panel((_PATH_A, DV, 8, 1))
    try:
        _select_codec(panel, "libx265")
        _click(panel._generate_hdr10plus_cb)
        assert not panel._generate_hdr10plus_note.isHidden() and "L1" in panel._generate_hdr10plus_note.text()
    finally:
        panel.close()


def test_replaced_copy_comes_back_after_a_track_switch(qt_app):
    panel = _panel((_PATH_A, DV_PLUS, 8, 1), (_PATH_B, DV, 8, 1))
    try:
        _select_codec(panel, "libx265")
        assert panel._copy_hdr10plus_cb.isChecked()
        _click(panel._generate_hdr10plus_cb)
        panel._video_list.setCurrentRow(1)
        panel._video_list.setCurrentRow(0)
        _click(panel._generate_hdr10plus_cb, False)
        video = panel._current_video_settings()
        assert video.copy_hdr10plus and not video.generate_hdr10plus  # HDR10+ de la source gardé
    finally:
        panel.close()


def _fel_panel(monkeypatch, support):
    from types import SimpleNamespace

    from core import hdr10plus_generation, plugins

    installed = SimpleNamespace(path=Path("/x"), version="0.6.1", manifest={}) if support.installed else None
    monkeypatch.setattr(plugins, "installed_plugin", lambda spec, *a, **k: installed)
    monkeypatch.setattr(hdr10plus_generation, "engine_generation_support", lambda: support)
    panel = _panel((_PATH_A, DV, 7, 6))
    _select_codec(panel, "libx265")
    if not panel._bake_fel_cb.isEnabled():
        panel.close()
        pytest.skip("intégration FEL indisponible pour ce profil")
    return panel


def test_fel_without_extension_announces_the_l1_estimate(qt_app, monkeypatch):
    from core.hdr10plus_generation import EngineSupport

    panel = _fel_panel(monkeypatch, EngineSupport())
    try:
        _click(panel._bake_fel_cb)
        _click(panel._generate_hdr10plus_cb)
        assert not panel._generate_hdr10plus_note.isHidden() and "L1" in panel._generate_hdr10plus_note.text()
    finally:
        panel.close()


def test_fel_without_frame_statistics_or_measure_announces_the_l1_estimate(qt_app, monkeypatch):
    from core.hdr10plus_generation import EngineSupport

    panel = _fel_panel(monkeypatch, EngineSupport(installed=True))
    try:
        _click(panel._bake_fel_cb)
        _click(panel._generate_hdr10plus_cb)
        assert not panel._generate_hdr10plus_note.isHidden() and "L1" in panel._generate_hdr10plus_note.text()
    finally:
        panel.close()


def test_fel_click_refreshes_the_announced_method(qt_app, monkeypatch):
    from core.hdr10plus_generation import EngineSupport

    # Extension 0.3.0 : statistiques FEL, aucune mesure préalable.
    panel = _fel_panel(monkeypatch, EngineSupport(installed=True, frame_statistics=True))
    try:
        _click(panel._bake_fel_cb)
        _click(panel._generate_hdr10plus_cb)
        assert panel._generate_hdr10plus_note.isHidden()  # statistiques de l'image FEL
        _click(panel._bake_fel_cb, False)
        assert not panel._generate_hdr10plus_note.isHidden() and "L1" in panel._generate_hdr10plus_note.text()
        _click(panel._bake_fel_cb)
        assert panel._generate_hdr10plus_note.isHidden()
    finally:
        panel.close()


def test_copy_comes_back_after_apply_all_from_copy(qt_app):
    panel = _panel((_PATH_A, DV, 8, 1), (_PATH_B, DV_PLUS, 8, 1))
    try:
        _select_codec(panel, "copy")
        panel._on_apply_all_video_toggled(True)
        _select_codec(panel, "libx265")
        _click(panel._generate_hdr10plus_cb)
        _click(panel._generate_hdr10plus_cb, False)
        config = panel.collect_config()
        assert config is not None
        second = config.video_tracks[1]
        assert second.copy_hdr10plus and not second.generate_hdr10plus  # HDR10+ de la source gardé
    finally:
        panel.close()


@pytest.mark.parametrize("preset_generates", [True, False])
def test_copy_comes_back_after_a_preset(qt_app, monkeypatch, preset_generates):
    from core.workflows.encode.models import EncodePreset

    panel = _panel((_PATH_A, DV_PLUS, 8, 1))
    try:
        _select_codec(panel, "libx265")
        if not preset_generates:
            _click(panel._generate_hdr10plus_cb)  # génération active avant le preset qui la retire
        preset = EncodePreset(name="P", codec="libx265", generate_hdr10plus=preset_generates)
        monkeypatch.setattr(panel._profiles, "load_all", lambda: [preset])
        panel._profile_combo.addItem("P", "P")
        panel._profile_combo.setCurrentIndex(panel._profile_combo.findData("P"))
        panel._load_profile()
        if preset_generates:
            assert panel._generate_hdr10plus_cb.isChecked() and not panel._copy_hdr10plus_cb.isChecked()
            _click(panel._generate_hdr10plus_cb, False)
        video = panel._current_video_settings()
        assert video.copy_hdr10plus and not video.generate_hdr10plus
        assert panel._copy_hdr10plus_cb.isChecked()
    finally:
        panel.close()


def test_method_note_follows_the_fel_choice_of_each_track(qt_app, monkeypatch):
    from types import SimpleNamespace

    from core import hdr10plus_generation, plugins
    from core.hdr10plus_generation import EngineSupport

    installed = SimpleNamespace(path=Path("/x"), version="0.3.0", manifest={})
    monkeypatch.setattr(plugins, "installed_plugin", lambda spec, *a, **k: installed)
    monkeypatch.setattr(hdr10plus_generation, "engine_generation_support",
                        lambda: EngineSupport(installed=True, frame_statistics=True))
    panel = _panel((_PATH_A, DV, 7, 6), (_PATH_B, DV, 7, 6))
    try:
        for row, fel in ((1, False), (0, True)):
            panel._video_list.setCurrentRow(row)
            _select_codec(panel, "libx265")
            if not panel._bake_fel_cb.isEnabled():
                pytest.skip("intégration FEL indisponible pour ce profil")
            _click(panel._bake_fel_cb, fel)
            _click(panel._generate_hdr10plus_cb)
        assert panel._generate_hdr10plus_note.isHidden()  # A : statistiques de l'image FEL
        panel._video_list.setCurrentRow(1)
        assert "L1" in panel._generate_hdr10plus_note.text() and not panel._generate_hdr10plus_note.isHidden()
        panel._video_list.setCurrentRow(0)
        assert panel._bake_fel_cb.isChecked() and panel._generate_hdr10plus_note.isHidden()
    finally:
        panel.close()

