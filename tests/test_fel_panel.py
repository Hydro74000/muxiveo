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


def test_device_combo_dynamic_selection_and_track_state(qt_app, monkeypatch):
    from core.fel.engine import FelEngine
    from tests.test_fel_devices import NVIDIA, SECOND
    from unittest.mock import Mock
    monkeypatch.setattr(EncodePanel, "_detect_hw_encoders", lambda self: None)
    monkeypatch.setattr(EncodePanel, "_rebuild_preview", lambda self: None)
    monkeypatch.setattr(EncodePanel, "_schedule_static_hdr_estimate", lambda *a, **k: None)
    engine = Mock()
    engine.devices.return_value = (NVIDIA, SECOND)
    monkeypatch.setattr(FelEngine, "installed", lambda: engine)
    panel = EncodePanel(AppConfig())
    try:
        panel.set_video_tracks([(_file_info(_PATH_A), _video_entry(), _COLOR)])
        combo = panel._fel_device_combo
        combo.refresh_devices()
        assert [combo.itemData(i) for i in range(combo.count())] == ["auto", NVIDIA.uuid, SECOND.uuid, "cpu"]
        assert not combo.isEnabled()
        panel._set_combo_data(panel._codec_combo, "libx265")
        panel._bake_fel_cb.click()
        assert combo.isEnabled()
        combo.setCurrentIndex(combo.findData(SECOND.uuid))
        combo.activated.emit(combo.currentIndex())
        state = panel._current_video_state()
        assert state["fel_device"] == SECOND.uuid
        panel._set_combo_data(panel._codec_combo, "copy")
        assert not combo.isEnabled()
        panel._apply_video_state(state)
        assert panel._current_video_settings().fel_device == SECOND.uuid
        engine.devices.return_value = (NVIDIA,)
        combo.refresh_devices()
        assert combo.currentData() == SECOND.uuid
        from core.i18n import translate_text
        assert combo.currentText() == translate_text("GPU indisponible ({id})", id=SECOND.uuid[:8])
        combo.setCurrentIndex(combo.findData("cpu"))
        combo.activated.emit(combo.currentIndex())
        assert panel._current_video_settings().fel_device == "cpu"
    finally:
        panel.close()


def test_precision_combo_modes_tooltips_and_track_state(qt_app, monkeypatch):
    """Trois précisions : Lent par défaut, infobulles, état par piste et GPU sans double signalé."""
    from core.fel.engine import FelEngine
    from core.i18n import translate_text
    from PySide6.QtCore import Qt
    from tests.test_fel_devices import ARC, RTX64
    from unittest.mock import Mock
    monkeypatch.setattr(EncodePanel, "_detect_hw_encoders", lambda self: None)
    monkeypatch.setattr(EncodePanel, "_rebuild_preview", lambda self: None)
    monkeypatch.setattr(EncodePanel, "_schedule_static_hdr_estimate", lambda *a, **k: None)
    engine = Mock()
    engine.capabilities = {"device_features": ["float64"]}
    engine.devices.return_value = (RTX64, ARC)
    monkeypatch.setattr(FelEngine, "installed", lambda: engine)
    panel = EncodePanel(AppConfig())
    try:
        panel.set_video_tracks([(_file_info(_PATH_A), _video_entry(), _COLOR)])
        combo = panel._fel_precision_combo
        assert [combo.itemData(i) for i in range(combo.count())] == ["exact", "standard", "fast"]
        assert combo.choice() == "exact" and translate_text("Lent (recommandé)") == combo.currentText()
        assert "1/64" in combo.itemData(1, Qt.ItemDataRole.ToolTipRole)
        assert "⚠" in combo.itemData(2, Qt.ItemDataRole.ToolTipRole)
        assert combo.itemData(2, Qt.ItemDataRole.ForegroundRole) is not None
        panel._set_combo_data(panel._codec_combo, "libx265")
        panel._bake_fel_cb.click()
        assert combo.isEnabled()
        combo.setCurrentIndex(combo.findData("standard"))
        combo.activated.emit(combo.currentIndex())
        assert combo.toolTip() == combo.itemData(1, Qt.ItemDataRole.ToolTipRole)
        state = panel._current_video_state()
        assert state["fel_precision"] == "standard"
        panel._apply_video_state({**state, "fel_precision": "fast"})
        assert panel._current_video_settings().fel_precision == "fast"
        panel._apply_video_state(state)
        assert panel._current_video_settings().fel_precision == "standard"
        devices = panel._fel_device_combo
        devices.refresh_devices()
        labels = {devices.itemData(i): devices.itemText(i) for i in range(devices.count())}
        assert translate_text("sans double précision : CPU en Lent et Standard") in labels[ARC.uuid]
        assert "double" not in labels[RTX64.uuid]
    finally:
        panel.close()
