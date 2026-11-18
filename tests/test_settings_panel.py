"""
tests/test_settings_panel.py — Régressions ciblées pour le panneau des réglages.
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock, patch

from core.i18n import translate_text


def _mock_qsettings():
    inst = MagicMock()
    inst.value.side_effect = lambda key, default=None: default
    return inst


def _field_widget(panel: object, section: str, key: str) -> Any:
    return cast(Any, panel).widget_for(section, key)


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
    combo = _field_widget(panel, "ui", "language")
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
        combo = _field_widget(panel, "ui", "language")
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
        combo = _field_widget(panel, "ui", "startup_panel")
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
        checkbox = _field_widget(panel, "ui", "startup_menu_compact")
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
        checkbox = _field_widget(panel, "ui", "startup_logs_expanded")
        checkbox.setChecked(True)
        panel._on_save_clicked()

        assert "startup_logs_expanded = true" in ini_path.read_text(encoding="utf-8")
        assert cfg.startup_logs_expanded is True


def test_settings_panel_writes_enable_file_logging_to_ini(tmp_path, qt_app):
    import core.config as cfg_mod
    from core.config import AppConfig
    from ui.panels.settings_panel import SettingsPanel

    ini_path = tmp_path / "config.ini"
    ini_path.write_text("[ui]\nenable_file_logging = false\n", encoding="utf-8")

    with patch("core.config.QSettings") as mock_qs, \
         patch("core.config._app_data_dir", return_value=tmp_path), \
         patch.object(cfg_mod, "_INI_PATH", ini_path):
        mock_qs.return_value = _mock_qsettings()
        cfg = AppConfig()

        panel = SettingsPanel(cfg)
        checkbox = _field_widget(panel, "ui", "enable_file_logging")
        checkbox.setChecked(True)
        panel._on_save_clicked()

        assert "enable_file_logging = true" in ini_path.read_text(encoding="utf-8")
        assert cfg.enable_file_logging is True


def test_settings_panel_writes_file_logging_level_to_ini(tmp_path, qt_app):
    import core.config as cfg_mod
    from core.config import AppConfig
    from ui.panels.settings_panel import SettingsPanel

    ini_path = tmp_path / "config.ini"
    ini_path.write_text("[ui]\nfile_logging_level = standard\n", encoding="utf-8")

    with patch("core.config.QSettings") as mock_qs, \
         patch("core.config._app_data_dir", return_value=tmp_path), \
         patch.object(cfg_mod, "_INI_PATH", ini_path):
        mock_qs.return_value = _mock_qsettings()
        cfg = AppConfig()

        panel = SettingsPanel(cfg)
        combo = _field_widget(panel, "ui", "file_logging_level")
        index = combo.findData("verbose")
        assert index >= 0
        combo.setCurrentIndex(index)
        panel._on_save_clicked()

        assert "file_logging_level = verbose" in ini_path.read_text(encoding="utf-8")
        assert cfg.file_logging_level == "verbose"


def test_settings_panel_disables_file_logging_level_when_file_logging_is_off(tmp_path, qt_app):
    import core.config as cfg_mod
    from core.config import AppConfig
    from ui.panels.settings_panel import SettingsPanel

    ini_path = tmp_path / "config.ini"
    ini_path.write_text("[ui]\nenable_file_logging = false\nfile_logging_level = verbose\n", encoding="utf-8")

    with patch("core.config.QSettings") as mock_qs, \
         patch("core.config._app_data_dir", return_value=tmp_path), \
         patch.object(cfg_mod, "_INI_PATH", ini_path):
        mock_qs.return_value = _mock_qsettings()
        cfg = AppConfig()

        panel = SettingsPanel(cfg)
        checkbox = _field_widget(panel, "ui", "enable_file_logging")
        combo = _field_widget(panel, "ui", "file_logging_level")
        assert combo.isEnabled() is False

        checkbox.setChecked(True)
        assert combo.isEnabled() is True


def test_settings_panel_reload_resyncs_file_logging_level_state(tmp_path, qt_app):
    import core.config as cfg_mod
    from core.config import AppConfig
    from ui.panels.settings_panel import SettingsPanel

    ini_path = tmp_path / "config.ini"
    ini_path.write_text("[ui]\nenable_file_logging = true\nfile_logging_level = verbose\n", encoding="utf-8")

    with patch("core.config.QSettings") as mock_qs, \
         patch("core.config._app_data_dir", return_value=tmp_path), \
         patch.object(cfg_mod, "_INI_PATH", ini_path):
        mock_qs.return_value = _mock_qsettings()
        cfg = AppConfig()

        panel = SettingsPanel(cfg)
        checkbox = _field_widget(panel, "ui", "enable_file_logging")
        combo = _field_widget(panel, "ui", "file_logging_level")

        assert checkbox.isChecked() is True
        assert combo.isEnabled() is True

        ini_path.write_text("[ui]\nenable_file_logging = false\nfile_logging_level = verbose\n", encoding="utf-8")
        panel._on_reload_clicked()

        assert checkbox.isChecked() is False
        assert combo.isEnabled() is False


def test_settings_panel_prefills_verbose_log_dir_full_path(tmp_path, qt_app):
    import core.config as cfg_mod
    from core.config import AppConfig
    from ui.panels.settings_panel import SettingsPanel

    ini_path = tmp_path / "config.ini"
    ini_path.write_text("", encoding="utf-8")

    with patch("core.config.QSettings") as mock_qs, \
         patch("core.config._app_data_dir", return_value=tmp_path), \
         patch.object(cfg_mod, "_INI_PATH", ini_path):
        mock_qs.return_value = _mock_qsettings()
        cfg = AppConfig()
        panel = SettingsPanel(cfg)

        edit = _field_widget(panel, "ui", "verbose_log_dir")
        assert edit.text() == str(tmp_path / "logs")


def test_settings_panel_writes_verbose_log_dir_to_ini(tmp_path, qt_app):
    import core.config as cfg_mod
    from core.config import AppConfig
    from ui.panels.settings_panel import SettingsPanel

    ini_path = tmp_path / "config.ini"
    target = tmp_path / "custom_logs"
    ini_path.write_text("[ui]\nverbose_log_dir = /tmp/old_logs\n", encoding="utf-8")

    with patch("core.config.QSettings") as mock_qs, \
         patch("core.config._app_data_dir", return_value=tmp_path), \
         patch.object(cfg_mod, "_INI_PATH", ini_path):
        mock_qs.return_value = _mock_qsettings()
        cfg = AppConfig()

        panel = SettingsPanel(cfg)
        edit = _field_widget(panel, "ui", "verbose_log_dir")
        edit.setText(str(target))
        panel._on_save_clicked()

        assert f"verbose_log_dir = {target}" in ini_path.read_text(encoding="utf-8")
        assert cfg.verbose_log_dir == target


def test_settings_panel_rejects_empty_verbose_log_dir_when_logging_enabled(tmp_path, qt_app):
    import core.config as cfg_mod
    from core.config import AppConfig
    from ui.panels.settings_panel import SettingsPanel

    ini_path = tmp_path / "config.ini"
    ini_path.write_text("[ui]\nenable_file_logging = true\nverbose_log_dir = /tmp/logs\n", encoding="utf-8")

    with patch("core.config.QSettings") as mock_qs, \
         patch("core.config._app_data_dir", return_value=tmp_path), \
         patch.object(cfg_mod, "_INI_PATH", ini_path):
        mock_qs.return_value = _mock_qsettings()
        cfg = AppConfig()

        panel = SettingsPanel(cfg)
        checkbox = _field_widget(panel, "ui", "enable_file_logging")
        checkbox.setChecked(True)
        edit = _field_widget(panel, "ui", "verbose_log_dir")
        edit.setText("")
        with patch("ui.panels.settings_panel.QMessageBox.warning") as warn:
            panel._on_save_clicked()

        warn.assert_called_once()
        assert "verbose_log_dir = /tmp/logs" in ini_path.read_text(encoding="utf-8")


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
        spin = _field_widget(panel, "ui", "ui_scale_percent")
        spin.setValue(125)
        panel._on_save_clicked()

        assert "ui_scale_percent = 130" in ini_path.read_text(encoding="utf-8")
        assert cfg.ui_scale_percent == 130


def test_settings_panel_writes_sync_rewrite_enabled_to_ini(tmp_path, qt_app):
    import core.config as cfg_mod
    from core.config import AppConfig
    from ui.panels.settings_panel import SettingsPanel

    ini_path = tmp_path / "config.ini"
    ini_path.write_text("[sync]\nrewrite_enabled = false\n", encoding="utf-8")

    with patch("core.config.QSettings") as mock_qs, \
         patch("core.config._app_data_dir", return_value=tmp_path), \
         patch.object(cfg_mod, "_INI_PATH", ini_path):
        mock_qs.return_value = _mock_qsettings()
        cfg = AppConfig()

        panel = SettingsPanel(cfg)
        checkbox = _field_widget(panel, "sync", "rewrite_enabled")
        assert checkbox.isChecked() is False
        checkbox.setChecked(True)
        panel._on_save_clicked()

        assert "rewrite_enabled = true" in ini_path.read_text(encoding="utf-8")
        assert cfg.sync_rewrite_enabled is True


def test_settings_panel_writes_advanced_audio_sync_rewrite_and_tooltip(tmp_path, qt_app):
    import core.config as cfg_mod
    from core.config import AppConfig
    from ui.panels.settings_panel import SettingsPanel

    ini_path = tmp_path / "config.ini"
    ini_path.write_text("[sync]\nadvanced_audio_rewrite_enabled = false\n", encoding="utf-8")

    with patch("core.config.QSettings") as mock_qs, \
         patch("core.config._app_data_dir", return_value=tmp_path), \
         patch.object(cfg_mod, "_INI_PATH", ini_path):
        mock_qs.return_value = _mock_qsettings()
        cfg = AppConfig()

        panel = SettingsPanel(cfg)
        checkbox = _field_widget(panel, "sync", "advanced_audio_rewrite_enabled")
        assert checkbox.isChecked() is False
        assert "TrueHD/MLP" in checkbox.toolTip()
        assert "Atmos/DTS:X" in checkbox.toolTip()
        checkbox.setChecked(True)
        panel._on_save_clicked()

        content = ini_path.read_text(encoding="utf-8")
        assert "advanced_audio_rewrite_enabled = true" in content
        assert cfg.sync_advanced_audio_rewrite_enabled is True


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
        # Section enregistrée seulement si modifiée : l'écriture purge les clés historiques.
        slider = _field_widget(panel, "audio_encoding", "aac_bitrate_per_channel_kbps")
        slider.setValue(0 if slider.value() else 1)
        assert panel.dirty_sections() == ["audio_encoding"]
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
        spin = _field_widget(panel, "ffmpeg", "threads")
        spin.setValue(18)
        panel._on_save_clicked()

        assert "threads = 18" in ini_path.read_text(encoding="utf-8")
        assert cfg.ffmpeg_threads == 18


def test_settings_panel_writes_x265_frame_threads_to_ini(tmp_path, qt_app):
    import core.config as cfg_mod
    from core.config import AppConfig
    from ui.panels.settings_panel import SettingsPanel

    ini_path = tmp_path / "config.ini"
    ini_path.write_text("[ffmpeg]\nx265_frame_threads = 40\n", encoding="utf-8")

    with patch("core.config.QSettings") as mock_qs, \
         patch("core.config._app_data_dir", return_value=tmp_path), \
         patch.object(cfg_mod, "_INI_PATH", ini_path):
        mock_qs.return_value = _mock_qsettings()
        cfg = AppConfig()
        assert cfg.x265_frame_threads == 16  # borné : x265 refuse plus de 16 images en parallèle

        panel = SettingsPanel(cfg)
        spin = _field_widget(panel, "ffmpeg", "x265_frame_threads")
        assert spin.maximum() == 16
        spin.setValue(6)
        panel._on_save_clicked()

        assert "x265_frame_threads = 6" in ini_path.read_text(encoding="utf-8")
        assert cfg.x265_frame_threads == 6


def test_settings_panel_writes_max_parallel_video_encodes_to_ini(tmp_path, qt_app):
    import core.config as cfg_mod
    from core.config import AppConfig
    from ui.panels.settings_panel import SettingsPanel

    ini_path = tmp_path / "config.ini"
    ini_path.write_text("[encoding]\nmax_parallel_video_encodes = 1\n", encoding="utf-8")

    with patch("core.config.QSettings") as mock_qs, \
         patch("core.config._app_data_dir", return_value=tmp_path), \
         patch.object(cfg_mod, "_INI_PATH", ini_path):
        mock_qs.return_value = _mock_qsettings()
        cfg = AppConfig()

        panel = SettingsPanel(cfg)
        spin = _field_widget(panel, "encoding", "max_parallel_video_encodes")
        spin.setValue(3)
        panel._on_save_clicked()

        assert "max_parallel_video_encodes = 3" in ini_path.read_text(encoding="utf-8")
        assert cfg.max_parallel_video_encodes == 3


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
    restarts: list[bool] = []
    panel.restart_requested.connect(lambda: restarts.append(True))
    with patch.object(cfg, "rerun_setup") as mock_rerun, \
         patch.object(cfg, "restart_application", return_value=True) as mock_restart, \
         patch("ui.panels.settings_panel.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
        panel._on_rerun_setup_clicked()

    mock_rerun.assert_called_once_with()
    # Redémarrage confié à la fenêtre (accord de fermeture, puis fermeture de cette instance).
    assert restarts == [True]
    mock_restart.assert_not_called()
    assert panel._status_label is not None
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
    assert panel._status_label is not None
    assert "boom" in panel._status_label.text()


def _panel_with_ini(tmp_path, content: str):
    import core.config as cfg_mod
    from core.config import AppConfig
    from ui.panels.settings_panel import SettingsPanel

    ini_path = tmp_path / "config.ini"
    ini_path.write_text(content, encoding="utf-8")
    patches = (
        patch("core.config.QSettings", return_value=_mock_qsettings()),
        patch("core.config._app_data_dir", return_value=tmp_path),
        patch.object(cfg_mod, "_INI_PATH", ini_path),
    )
    for item in patches:
        item.start()
    cfg = AppConfig()
    panel = SettingsPanel(cfg)
    return panel, cfg, ini_path, patches


def _stop(patches) -> None:
    for item in patches:
        item.stop()


def test_settings_panel_one_tab_per_section(tmp_path, qt_app):
    from core.config import INI_FIELD_GROUPS

    panel, _cfg, _ini, patches = _panel_with_ini(tmp_path, "[ui]\nlanguage = fra\n")
    try:
        tabs = panel._tabs
        assert tabs.count() == len(INI_FIELD_GROUPS)
        assert panel._tab_sections[0] == "ui"
        assert sorted(panel._tab_sections) == sorted(group["section"] for group in INI_FIELD_GROUPS)
        assert not panel.has_unsaved_changes()
        assert panel._save_btn is not None and not panel._save_btn.isEnabled()
    finally:
        _stop(patches)


def test_settings_panel_edit_marks_tab_and_reverting_clears_it(tmp_path, qt_app):
    panel, _cfg, _ini, patches = _panel_with_ini(tmp_path, "[ui]\nstartup_menu_compact = false\n")
    try:
        checkbox = _field_widget(panel, "ui", "startup_menu_compact")
        checkbox.setChecked(True)
        assert panel.dirty_sections() == ["ui"]
        assert panel._save_btn.isEnabled()
        assert not panel._tabs.tabIcon(0).isNull()
        checkbox.setChecked(False)
        assert not panel.has_unsaved_changes()
        assert panel._tabs.tabIcon(0).isNull()
    finally:
        _stop(patches)


def test_settings_panel_tab_change_save_writes_section(tmp_path, qt_app):
    panel, cfg, ini_path, patches = _panel_with_ini(tmp_path, "[ui]\nstartup_menu_compact = false\n")
    try:
        _field_widget(panel, "ui", "startup_menu_compact").setChecked(True)
        with patch.object(panel, "_ask_pending_changes", return_value="save") as ask:
            panel._tabs.setCurrentIndex(1)
        ask.assert_called_once_with(["ui"])
        assert panel._tabs.currentIndex() == 1
        assert "startup_menu_compact = true" in ini_path.read_text(encoding="utf-8")
        assert cfg.startup_menu_compact is True
        assert not panel.has_unsaved_changes()
    finally:
        _stop(patches)


def test_settings_panel_tab_change_discard_restores_values(tmp_path, qt_app):
    panel, _cfg, ini_path, patches = _panel_with_ini(tmp_path, "[ui]\nstartup_menu_compact = false\n")
    try:
        checkbox = _field_widget(panel, "ui", "startup_menu_compact")
        checkbox.setChecked(True)
        with patch.object(panel, "_ask_pending_changes", return_value="discard"):
            panel._tabs.setCurrentIndex(2)
        assert panel._tabs.currentIndex() == 2
        assert not checkbox.isChecked()
        assert not panel.has_unsaved_changes()
        assert "startup_menu_compact = true" not in ini_path.read_text(encoding="utf-8")
    finally:
        _stop(patches)


def test_settings_panel_tab_change_cancel_stays_on_tab(tmp_path, qt_app):
    panel, _cfg, _ini, patches = _panel_with_ini(tmp_path, "[ui]\nstartup_menu_compact = false\n")
    try:
        checkbox = _field_widget(panel, "ui", "startup_menu_compact")
        checkbox.setChecked(True)
        with patch.object(panel, "_ask_pending_changes", return_value="cancel"):
            panel._tabs.setCurrentIndex(1)
            assert panel._tabs.currentIndex() == 0
            assert checkbox.isChecked()
            assert not panel.confirm_leave()
        # Onglet suivant sans modification : aucune question.
        checkbox.setChecked(False)
        with patch.object(panel, "_ask_pending_changes") as ask:
            panel._tabs.setCurrentIndex(1)
            assert panel.confirm_leave()
        ask.assert_not_called()
        assert panel._tabs.currentIndex() == 1
    finally:
        _stop(patches)


def test_settings_panel_save_refused_by_validation_keeps_tab(tmp_path, qt_app):
    panel, _cfg, _ini, patches = _panel_with_ini(tmp_path, "[ui]\nenable_file_logging = false\n")
    try:
        _field_widget(panel, "ui", "enable_file_logging").setChecked(True)
        _field_widget(panel, "ui", "verbose_log_dir").setText("")
        with patch.object(panel, "_ask_pending_changes", return_value="save"), \
             patch("ui.panels.settings_panel.QMessageBox.warning"):
            panel._tabs.setCurrentIndex(1)
        assert panel._tabs.currentIndex() == 0
        assert panel.has_unsaved_changes()
    finally:
        _stop(patches)


def test_settings_panel_refresh_field_is_not_a_pending_change(tmp_path, qt_app):
    panel, cfg, _ini, patches = _panel_with_ini(tmp_path, "[ui]\nupdate_popup = true\n")
    try:
        cfg.update_popup = False
        panel.refresh_field("ui", "update_popup")
        assert not _field_widget(panel, "ui", "update_popup").isChecked()
        assert not panel.has_unsaved_changes()
    finally:
        _stop(patches)


def test_settings_panel_keeps_values_the_fields_cannot_show(tmp_path, qt_app):
    """Échelle hors palier, débit hors palier, option inconnue : conservés tant qu'on n'y touche pas."""
    panel, cfg, ini_path, patches = _panel_with_ini(
        tmp_path,
        "[ui]\nui_scale_percent = 105\n"
        "[audio_encoding]\naac_bitrate_per_channel_kbps = 100\n",
    )
    try:
        assert not panel.has_unsaved_changes()
        scale = _field_widget(panel, "ui", "ui_scale_percent")
        assert scale.value() == 110
        assert panel._slider_value_labels[("ui", "ui_scale_percent")].text() == "105%"
        aac = _field_widget(panel, "audio_encoding", "aac_bitrate_per_channel_kbps")
        assert panel._slider_value_labels[("audio_encoding", "aac_bitrate_per_channel_kbps")].text() == "100 kbps"
        channel_field = next(f for f in panel._group_by_section["ui"]["fields"] if f["key"] == "update_channel")
        panel._apply_value("ui", channel_field, "nightly")
        assert _field_widget(panel, "ui", "update_channel").currentIndex() == -1
        assert panel._field_value("ui", channel_field) == "nightly"
        panel._load_field("ui", channel_field)

        # Autre champ de la section enregistré : les valeurs non affichables restent intactes.
        _field_widget(panel, "ui", "startup_menu_compact").setChecked(True)
        panel._on_save_clicked()
        text = ini_path.read_text(encoding="utf-8")
        assert "ui_scale_percent = 105" in text
        assert not panel.has_unsaved_changes()

        # Curseur déplacé puis ramené sur 96 : 96 devient la valeur, à enregistrer.
        steps = aac.property("steps")
        aac.setValue(steps.index(112))
        aac.setValue(steps.index(96))
        assert panel.dirty_sections() == ["audio_encoding"]
        panel._on_save_clicked()
        assert "aac_bitrate_per_channel_kbps = 96" in ini_path.read_text(encoding="utf-8")
    finally:
        _stop(patches)


def test_settings_panel_discard_restores_value_the_field_cannot_show(tmp_path, qt_app):
    panel, _cfg, _ini, patches = _panel_with_ini(tmp_path, "[audio_encoding]\naac_bitrate_per_channel_kbps = 100\n")
    try:
        aac = _field_widget(panel, "audio_encoding", "aac_bitrate_per_channel_kbps")
        aac.setValue(aac.property("steps").index(128))
        assert panel.dirty_sections() == ["audio_encoding"]
        panel._discard_sections(["audio_encoding"])
        assert not panel.has_unsaved_changes()
        assert panel._section_values("audio_encoding")["aac_bitrate_per_channel_kbps"] == "100"
    finally:
        _stop(patches)


def test_settings_panel_write_error_keeps_tab_and_state(tmp_path, qt_app):
    panel, _cfg, _ini, patches = _panel_with_ini(tmp_path, "[ui]\nstartup_menu_compact = false\n")
    try:
        _field_widget(panel, "ui", "startup_menu_compact").setChecked(True)
        with patch.object(panel, "_ask_pending_changes", return_value="save"), \
             patch("ui.panels.settings_panel.write_ini_settings", side_effect=PermissionError("lecture seule")), \
             patch("ui.panels.settings_panel.QMessageBox.warning") as warning:
            panel._tabs.setCurrentIndex(1)
        warning.assert_called_once()
        assert panel._tabs.currentIndex() == 0 == panel._current_tab
        assert panel.dirty_sections() == ["ui"]
        assert "lecture seule" in panel._status_label.text()
    finally:
        _stop(patches)


def test_settings_panel_question_is_not_reentrant(tmp_path, qt_app):
    """Fermeture ou navigation demandée pendant la question : refusée sans seconde question."""
    panel, _cfg, _ini, patches = _panel_with_ini(tmp_path, "[ui]\nstartup_menu_compact = false\n")
    try:
        _field_widget(panel, "ui", "startup_menu_compact").setChecked(True)
        nested: list[bool] = []

        def ask(sections):
            nested.append(panel.confirm_leave())
            return "discard"

        with patch.object(panel, "_ask_pending_changes", side_effect=ask) as asked:
            assert panel.confirm_leave()
        assert nested == [False]
        asked.assert_called_once()
    finally:
        _stop(patches)


def test_settings_panel_status_survives_translation_pass(tmp_path, qt_app):
    from core.i18n import apply_translations

    panel, _cfg, _ini, patches = _panel_with_ini(tmp_path, "[ui]\nstartup_menu_compact = false\n")
    try:
        _field_widget(panel, "ui", "startup_menu_compact").setChecked(True)
        panel._on_save_clicked()
        apply_translations(panel)
        _field_widget(panel, "metadata", "generate_nfo").setChecked(not _field_widget(panel, "metadata", "generate_nfo").isChecked())
        panel._on_save_clicked()
        apply_translations(panel)
        assert translate_text("Métadonnées") in panel._status_label.text()
    finally:
        _stop(patches)


def test_settings_panel_rerun_setup_asks_about_pending_changes_first(tmp_path, qt_app):
    panel, cfg, _ini, patches = _panel_with_ini(tmp_path, "[ui]\nstartup_menu_compact = false\n")
    try:
        _field_widget(panel, "ui", "startup_menu_compact").setChecked(True)
        with patch.object(panel, "_ask_pending_changes", return_value="cancel"), \
             patch.object(cfg, "rerun_setup") as rerun:
            panel._on_rerun_setup_clicked()
        rerun.assert_not_called()
        assert panel.has_unsaved_changes()
    finally:
        _stop(patches)


def test_settings_panel_slider_label_follows_reloaded_value(tmp_path, qt_app):
    """Même position de curseur pour une autre valeur : le libellé suit la valeur rechargée."""
    panel, _cfg, _ini, patches = _panel_with_ini(
        tmp_path, "[ui]\nui_scale_percent = 105\n[audio_encoding]\naac_bitrate_per_channel_kbps = 100\n",
    )
    try:
        aac_field = panel._group_by_section["audio_encoding"]["fields"][0]
        scale_field = next(f for f in panel._group_by_section["ui"]["fields"] if f["key"] == "ui_scale_percent")
        aac_label = panel._slider_value_labels[("audio_encoding", aac_field["key"])]
        scale_label = panel._slider_value_labels[("ui", "ui_scale_percent")]
        assert (aac_label.text(), scale_label.text()) == ("100 kbps", "105%")
        panel._apply_value("audio_encoding", aac_field, 96)
        panel._apply_value("ui", scale_field, 110)
        assert (aac_label.text(), scale_label.text()) == ("96 kbps", "110%")
        assert panel._field_value("audio_encoding", aac_field) == "96"
    finally:
        _stop(patches)


def test_settings_panel_is_leaving_only_during_confirm_leave(tmp_path, qt_app):
    """Question posée avant une autre action : MainWindow n'y propose pas de redémarrage."""
    panel, _cfg, _ini, patches = _panel_with_ini(tmp_path, "[ui]\nstartup_menu_compact = false\n")
    try:
        checkbox = _field_widget(panel, "ui", "startup_menu_compact")
        seen: list[bool] = []

        def ask(sections):
            panel.confirm_leave()          # demande imbriquée : refusée, état extérieur intact
            seen.append(panel.is_leaving())
            return "discard"

        checkbox.setChecked(True)
        with patch.object(panel, "_ask_pending_changes", side_effect=ask):
            assert panel.confirm_leave()
            checkbox.setChecked(True)
            panel._tabs.setCurrentIndex(1)  # changement d'onglet : pas une sortie de page
        assert seen == [True, False]
        assert not panel.is_leaving()
    finally:
        _stop(patches)


def test_settings_panel_save_writes_only_changed_keys(tmp_path, qt_app):
    """Clé normalisée au chargement (250 → 200) et clés sans valeur dans config.ini : non réécrites."""
    panel, cfg, ini_path, patches = _panel_with_ini(tmp_path, "[ui]\nui_scale_percent = 250\n")
    try:
        assert cfg.ui_scale_percent == 200
        _field_widget(panel, "ui", "startup_menu_compact").setChecked(True)
        panel._on_save_clicked()
        text = ini_path.read_text(encoding="utf-8")
        assert "ui_scale_percent = 250" in text
        assert "startup_menu_compact = true" in text
        assert "theme" not in text and "language" not in text
        assert not panel.has_unsaved_changes()
    finally:
        _stop(patches)


def test_settings_panel_reload_asks_about_pending_changes_and_applies(tmp_path, qt_app):
    panel, _cfg, ini_path, patches = _panel_with_ini(tmp_path, "[ui]\nstartup_menu_compact = false\n")
    try:
        saved: list[bool] = []
        panel.settings_saved.connect(lambda: saved.append(True))
        checkbox = _field_widget(panel, "ui", "startup_menu_compact")
        checkbox.setChecked(True)
        with patch.object(panel, "_ask_pending_changes", return_value="cancel"):
            panel._on_reload_clicked()
        assert checkbox.isChecked() and saved == []

        ini_path.write_text("[ui]\nstartup_menu_compact = false\nlog_max_lines = 1234\n", encoding="utf-8")
        with patch.object(panel, "_ask_pending_changes", return_value="discard"):
            panel._on_reload_clicked()
        assert not checkbox.isChecked()
        assert _field_widget(panel, "ui", "log_max_lines").value() == 1234
        assert saved == [True]
        assert not panel.has_unsaved_changes()
    finally:
        _stop(patches)


def test_settings_panel_refuses_value_config_ini_cannot_store(tmp_path, qt_app):
    """Dossier « Films #1 » : relu tronqué par config.ini → refus explicite, saisie conservée."""
    panel, _cfg, ini_path, patches = _panel_with_ini(tmp_path, "[paths]\n")
    try:
        _field_widget(panel, "paths", "output_dir").setText(str(tmp_path / "Films #1"))
        with patch("ui.panels.settings_panel.QMessageBox.warning") as warning:
            panel._on_save_clicked()
        warning.assert_called_once()
        assert panel.dirty_sections() == ["paths"]
        assert "Films" not in ini_path.read_text(encoding="utf-8")
    finally:
        _stop(patches)
