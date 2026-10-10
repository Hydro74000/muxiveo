"""Activation FEL explicite par piste, indépendance HDR et profils."""
import pytest

from core.config import AppConfig
from tests.test_encode_panel_dovi_geometry import _file_info, _video_entry, _PATH_A, _COLOR
from ui.panels.encode_panel.panel import EncodePanel


@pytest.mark.parametrize("checked", [False, True])
@pytest.mark.parametrize("profile", [5, 7])
def test_default_disabled_manual_and_copy(qt_app, monkeypatch, checked, profile):
    monkeypatch.setattr(EncodePanel, "_detect_hw_encoders", lambda self: None)
    monkeypatch.setattr(EncodePanel, "_rebuild_preview", lambda self: None)
    monkeypatch.setattr(EncodePanel, "_schedule_static_hdr_estimate", lambda *a, **k: None)
    panel = EncodePanel(AppConfig())
    try:
        info = _file_info(_PATH_A)
        info.video_tracks[0].dovi_profile = profile
        panel.set_video_tracks([(info, _video_entry(), _COLOR)])
        assert not panel._bake_fel_cb.isChecked()
        assert panel._bake_fel_choice is None
        assert not panel._bake_fel_cb.isEnabled()
        panel._set_combo_data(panel._codec_combo, "libx265")
        assert panel._bake_fel_cb.isEnabled()
        panel._bake_fel_cb.click()
        assert panel._bake_fel_choice is True
        if not checked:
            panel._bake_fel_cb.click()
        assert panel._bake_fel_choice is checked
        panel._copy_dv_cb.setChecked(False)
        panel._tonemap_cb.setChecked(True)
        panel._set_combo_data(panel._codec_combo, "copy")
        panel._set_combo_data(panel._codec_combo, "libx265")
        assert panel._bake_fel_cb.isChecked() is checked
        state = panel._current_video_state()
        assert state["bake_dovi_fel"] is checked
        panel._apply_video_state(state)
        assert panel._bake_fel_choice is checked
        assert panel._current_video_settings().bake_dovi_fel is checked
    finally:
        panel.close()


def test_profile_changes_keep_default_disabled(qt_app, monkeypatch):
    monkeypatch.setattr(EncodePanel, "_detect_hw_encoders", lambda self: None)
    monkeypatch.setattr(EncodePanel, "_rebuild_preview", lambda self: None)
    monkeypatch.setattr(EncodePanel, "_schedule_static_hdr_estimate", lambda *a, **k: None)
    panel = EncodePanel(AppConfig())
    try:
        info = _file_info(_PATH_A)
        info.video_tracks[0].dovi_profile = 8
        panel.set_video_tracks([(info, _video_entry(), _COLOR)])
        assert not panel._bake_fel_cb.isChecked()
        info.video_tracks[0].dovi_profile = 7
        panel._sync_dovi_profile_options()
        assert not panel._bake_fel_cb.isChecked()
        assert panel._bake_fel_choice is None
    finally:
        panel.close()
