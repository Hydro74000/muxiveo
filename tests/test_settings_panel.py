"""
tests/test_settings_panel.py — Régressions ciblées pour le panneau des réglages.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from core.i18n import translate_text


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


def test_settings_panel_writes_startup_logs_expanded_to_ini(tmp_path, qt_app):
    import core.config as cfg_mod
    from core.config import AppConfig
    from ui.panels.settings_panel import SettingsPanel

    ini_path = tmp_path / "config.ini"
    ini_path.write_text("[ui]\nstartup_logs_expanded = false\n", encoding="utf-8")

    with patch("core.config.QSettings") as mock_qs, \
         patch("core.config._app_data_dir", return_value=tmp_path), \
         patch.object(cfg_mod, "_INI_PATH", ini_path):
        mock_qs.return_value = _mock_qsettings()
        cfg = AppConfig()

        panel = SettingsPanel(cfg)
        checkbox = panel.widget_for("ui", "startup_logs_expanded")
        checkbox.setChecked(True)
        panel._on_save_clicked()

        assert "startup_logs_expanded = true" in ini_path.read_text(encoding="utf-8")
        assert cfg.startup_logs_expanded is True


def test_settings_panel_writes_ui_scale_percent_to_ini(tmp_path, qt_app):
    import core.config as cfg_mod
    from core.config import AppConfig
    from ui.panels.settings_panel import SettingsPanel

    ini_path = tmp_path / "config.ini"
    ini_path.write_text("[ui]\nui_scale_percent = 100\n", encoding="utf-8")

    with patch("core.config.QSettings") as mock_qs, \
         patch("core.config._app_data_dir", return_value=tmp_path), \
         patch.object(cfg_mod, "_INI_PATH", ini_path):
        mock_qs.return_value = _mock_qsettings()
        cfg = AppConfig()

        panel = SettingsPanel(cfg)
        spin = panel.widget_for("ui", "ui_scale_percent")
        spin.setValue(125)
        panel._on_save_clicked()

        assert "ui_scale_percent = 130" in ini_path.read_text(encoding="utf-8")
        assert cfg.ui_scale_percent == 130


def test_settings_panel_does_not_expose_legacy_audio_encoding_settings(tmp_path, qt_app):
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
        assert ("audio_encoding", "default_bitrate_per_channel_kbps") not in panel._field_widgets
        assert ("audio_encoding", "bitrate_step_per_channel_kbps") not in panel._field_widgets
        panel._on_save_clicked()

        ini_text = ini_path.read_text(encoding="utf-8")
        assert "default_bitrate_per_channel_kbps" not in ini_text
        assert "bitrate_step_per_channel_kbps" not in ini_text
        assert not hasattr(cfg, "audio_default_bitrate_per_channel_kbps")
        assert not hasattr(cfg, "audio_bitrate_step_per_channel_kbps")


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


def test_settings_panel_rerun_setup_restarts_app_on_confirmation(tmp_path, qt_app):
    import core.config as cfg_mod
    from core.config import AppConfig
    from ui.panels.settings_panel import SettingsPanel
    from PySide6.QtWidgets import QMessageBox

    ini_path = tmp_path / "config.ini"
    ini_path.write_text("[ui]\nlanguage = eng\n", encoding="utf-8")

    with patch("core.config.QSettings") as mock_qs, \
         patch("core.config._app_data_dir", return_value=tmp_path), \
         patch.object(cfg_mod, "_INI_PATH", ini_path):
        mock_qs.return_value = _mock_qsettings()
        cfg = AppConfig()

    panel = SettingsPanel(cfg)
    with patch.object(cfg, "rerun_setup") as mock_rerun, \
         patch.object(cfg, "restart_application", return_value=True) as mock_restart, \
         patch("ui.panels.settings_panel.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
        panel._on_rerun_setup_clicked()

    mock_rerun.assert_called_once_with()
    mock_restart.assert_called_once_with()
    assert panel._status_label.text() == translate_text(
        "Setup relancé avec succès. Un redémarrage de l'application est recommandé."
    )


def test_settings_panel_rerun_setup_shows_error_message(tmp_path, qt_app):
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
    with patch.object(cfg, "rerun_setup", side_effect=RuntimeError("boom")), \
         patch("ui.panels.settings_panel.QMessageBox.warning") as mock_warning:
        panel._on_rerun_setup_clicked()

    mock_warning.assert_called_once()
    assert "boom" in panel._status_label.text()
