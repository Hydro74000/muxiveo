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
    assert combo.currentData() == "fra"
    assert "=" not in combo.currentText()
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
        index = combo.findData("fra")
        assert index >= 0
        combo.setCurrentIndex(index)
        panel._on_save_clicked()

        assert "language = fra" in ini_path.read_text(encoding="utf-8")
        assert cfg.language == "fra"


def test_settings_panel_writes_selected_startup_panel_to_ini(tmp_path, qt_app):
    import core.config as cfg_mod
    from core.config import AppConfig
    from ui.panels.settings_panel import SettingsPanel

    ini_path = tmp_path / "config.ini"
    ini_path.write_text("[ui]\nstartup_panel = dashboard\n", encoding="utf-8")

    with patch("core.config.QSettings") as mock_qs, \
         patch("core.config._app_data_dir", return_value=tmp_path), \
         patch.object(cfg_mod, "_INI_PATH", ini_path):
        mock_qs.return_value = _mock_qsettings()
        cfg = AppConfig()

        panel = SettingsPanel(cfg)
        combo = panel.widget_for("ui", "startup_panel")
        index = combo.findData("encoding")
        assert index >= 0
        combo.setCurrentIndex(index)
        panel._on_save_clicked()

        assert "startup_panel = encoding" in ini_path.read_text(encoding="utf-8")
        assert cfg.startup_panel == "encoding"


def test_settings_panel_writes_startup_menu_compact_to_ini(tmp_path, qt_app):
    import core.config as cfg_mod
    from core.config import AppConfig
    from ui.panels.settings_panel import SettingsPanel

    ini_path = tmp_path / "config.ini"
    ini_path.write_text("[ui]\nstartup_menu_compact = false\n", encoding="utf-8")

    with patch("core.config.QSettings") as mock_qs, \
         patch("core.config._app_data_dir", return_value=tmp_path), \
         patch.object(cfg_mod, "_INI_PATH", ini_path):
        mock_qs.return_value = _mock_qsettings()
        cfg = AppConfig()

        panel = SettingsPanel(cfg)
        checkbox = panel.widget_for("ui", "startup_menu_compact")
        checkbox.setChecked(True)
        panel._on_save_clicked()

        assert "startup_menu_compact = true" in ini_path.read_text(encoding="utf-8")
        assert cfg.startup_menu_compact is True


def test_settings_panel_writes_audio_encoding_values_to_ini(tmp_path, qt_app):
    import core.config as cfg_mod
    from core.config import AppConfig
    from ui.panels.settings_panel import SettingsPanel

    ini_path = tmp_path / "config.ini"
    ini_path.write_text(
        "[audio_encoding]\n"
        "default_bitrate_per_channel_kbps = 192\n"
        "bitrate_step_per_channel_kbps = 64\n",
        encoding="utf-8",
    )

    with patch("core.config.QSettings") as mock_qs, \
         patch("core.config._app_data_dir", return_value=tmp_path), \
         patch.object(cfg_mod, "_INI_PATH", ini_path):
        mock_qs.return_value = _mock_qsettings()
        cfg = AppConfig()

        panel = SettingsPanel(cfg)
        default_spin = panel.widget_for("audio_encoding", "default_bitrate_per_channel_kbps")
        step_spin = panel.widget_for("audio_encoding", "bitrate_step_per_channel_kbps")
        default_spin.setValue(160)
        step_spin.setValue(48)
        panel._on_save_clicked()

        ini_text = ini_path.read_text(encoding="utf-8")
        assert "default_bitrate_per_channel_kbps = 160" in ini_text
        assert "bitrate_step_per_channel_kbps = 48" in ini_text
        assert cfg.audio_default_bitrate_per_channel_kbps == 160
        assert cfg.audio_bitrate_step_per_channel_kbps == 48


def test_settings_panel_writes_ffmpeg_threads_to_ini(tmp_path, qt_app):
    import core.config as cfg_mod
    from core.config import AppConfig
    from ui.panels.settings_panel import SettingsPanel

    ini_path = tmp_path / "config.ini"
    ini_path.write_text("[ffmpeg]\nthreads = 12\n", encoding="utf-8")

    with patch("core.config.QSettings") as mock_qs, \
         patch("core.config._app_data_dir", return_value=tmp_path), \
         patch.object(cfg_mod, "_INI_PATH", ini_path):
        mock_qs.return_value = _mock_qsettings()
        cfg = AppConfig()

        panel = SettingsPanel(cfg)
        spin = panel.widget_for("ffmpeg", "threads")
        spin.setValue(18)
        panel._on_save_clicked()

        assert "threads = 18" in ini_path.read_text(encoding="utf-8")
        assert cfg.ffmpeg_threads == 18
