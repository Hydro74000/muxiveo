"""
tests/test_settings_panel.py — Régressions ciblées pour le panneau des réglages.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch


def _mock_qsettings():
    inst = MagicMock()
    inst.value.side_effect = lambda key, default=None: default
    return inst


def test_settings_panel_language_combo_shows_full_language_name(tmp_path, qt_app):
    import core.config as cfg_mod
    from core.config import AppConfig
    from ui.panels.settings_panel import SettingsPanel

    ini_path = tmp_path / "config.ini"
    ini_path.write_text("[ui]\nlanguage = fre\n", encoding="utf-8")

    with patch("core.config.QSettings") as mock_qs, \
         patch("core.config._app_data_dir", return_value=tmp_path), \
         patch.object(cfg_mod, "_INI_PATH", ini_path):
        mock_qs.return_value = _mock_qsettings()
        cfg = AppConfig()

    panel = SettingsPanel(cfg)
    combo = panel.widget_for("ui", "language")
    assert combo.currentData() == "fre"
    assert "Français" in combo.currentText() or "French" in combo.currentText()


def test_settings_panel_writes_selected_language_to_ini(tmp_path, qt_app):
    import core.config as cfg_mod
    from core.config import AppConfig
    from ui.panels.settings_panel import SettingsPanel

    ini_path = tmp_path / "config.ini"
    ini_path.write_text("[ui]\nlanguage = eng\n", encoding="utf-8")

    with patch("core.config.QSettings") as mock_qs, \
         patch("core.config._app_data_dir", return_value=tmp_path), \
         patch.object(cfg_mod, "_INI_PATH", ini_path):
        mock_qs.return_value = _mock_qsettings()
        cfg = AppConfig()

        panel = SettingsPanel(cfg)
        combo = panel.widget_for("ui", "language")
        index = combo.findData("fre")
        assert index >= 0
        combo.setCurrentIndex(index)
        panel._on_save_clicked()

        assert "language = fre" in ini_path.read_text(encoding="utf-8")
        assert cfg.language == "fre"
