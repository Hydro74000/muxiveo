"""
tests/test_setup_and_config.py — Tests unitaires pour setup.py et core/config.py
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _qt_app(qt_app):
    """Assure qu'une QApplication existe pour les accès Qt/QSettings."""
    return qt_app


class TestAppConfigRamBuffer:
    """Tests des clés INI ram_buffer_enabled / ram_buffer_threshold_pct."""

    def test_defaults_enabled_true_threshold_15(self, tmp_path):
        """Valeurs par défaut : enabled=True, threshold=15."""
        from core.config import AppConfig

        with patch("core.config.QSettings") as mock_qs:
            inst = MagicMock()
            inst.value.side_effect = lambda key, default=None: default
            mock_qs.return_value = inst
            with patch("core.config._app_data_dir", return_value=tmp_path), \
                 patch.dict(os.environ, {}, clear=False):
                cfg = AppConfig()

        assert cfg.ram_buffer_enabled is True
        assert cfg.ram_buffer_threshold_pct == 15

    def test_ini_disables_ram_buffer(self, tmp_path):
        """config.ini ram_buffer_enabled=false désactive le buffer."""
        import core.config as cfg_mod
        from core.config import AppConfig

        ini_path = tmp_path / "config.ini"
        ini_path.write_text("[encoding]\nram_buffer_enabled = false\n")

        with patch("core.config.QSettings") as mock_qs:
            inst = MagicMock()
            inst.value.side_effect = lambda key, default=None: default
            mock_qs.return_value = inst
            with patch("core.config._app_data_dir", return_value=tmp_path), \
                 patch.object(cfg_mod, "_INI_PATH", ini_path):
                cfg = AppConfig()

        assert cfg.ram_buffer_enabled is False

    def test_ini_sets_threshold(self, tmp_path):
        """config.ini ram_buffer_threshold_pct=25 fixe le seuil à 25."""
        import core.config as cfg_mod
        from core.config import AppConfig

        ini_path = tmp_path / "config.ini"
        ini_path.write_text("[encoding]\nram_buffer_threshold_pct = 25\n")

        with patch("core.config.QSettings") as mock_qs:
            inst = MagicMock()
            inst.value.side_effect = lambda key, default=None: default
            mock_qs.return_value = inst
            with patch("core.config._app_data_dir", return_value=tmp_path), \
                 patch.object(cfg_mod, "_INI_PATH", ini_path):
                cfg = AppConfig()

        assert cfg.ram_buffer_threshold_pct == 25

    def test_explicit_blank_ini_uses_default_instead_of_qsettings(self, tmp_path):
        """Une clé présente mais vide revient au défaut documenté, pas à QSettings."""
        import core.config as cfg_mod
        from core.config import AppConfig

        ini_path = tmp_path / "config.ini"
        ini_path.write_text("[paths]\nwork_dir =\n", encoding="utf-8")
        default_work_dir = tmp_path / "default-work"

        with patch("core.config.QSettings") as mock_qs:
            inst = MagicMock()
            inst.value.side_effect = lambda key, default=None: "/tmp/qsettings-work" if key == "paths/work_dir" else default
            mock_qs.return_value = inst
            with patch("core.config._app_data_dir", return_value=tmp_path), \
                 patch("core.config._default_work_dir", return_value=default_work_dir), \
                 patch.object(cfg_mod, "_INI_PATH", ini_path):
                cfg = AppConfig()

        assert cfg.work_dir == default_work_dir

    def test_ini_language_accepts_iso639_2_alias(self, tmp_path):
        """Le code UI peut utiliser un alias ISO639-2, normalisé vers le code canonique."""
        import core.config as cfg_mod
        from core.config import AppConfig

        ini_path = tmp_path / "config.ini"
        ini_path.write_text("[ui]\nlanguage = fre\n", encoding="utf-8")

        with patch("core.config.QSettings") as mock_qs:
            inst = MagicMock()
            inst.value.side_effect = lambda key, default=None: default
            mock_qs.return_value = inst
            with patch("core.config._app_data_dir", return_value=tmp_path), \
                 patch.object(cfg_mod, "_INI_PATH", ini_path):
                cfg = AppConfig()

        assert cfg.language == "fra"

    def test_default_startup_panel_is_dashboard(self, tmp_path):
        """Sans clé explicite, le panneau de démarrage est le dashboard."""
        from core.config import AppConfig

        with patch("core.config.QSettings") as mock_qs:
            inst = MagicMock()
            inst.value.side_effect = lambda key, default=None: default
            mock_qs.return_value = inst
            with patch("core.config._app_data_dir", return_value=tmp_path), \
                 patch.dict(os.environ, {}, clear=False):
                cfg = AppConfig()

        assert cfg.startup_panel == "dashboard"

    def test_invalid_startup_panel_falls_back_to_dashboard(self, tmp_path):
        """Une valeur startup_panel invalide revient à dashboard."""
        import core.config as cfg_mod
        from core.config import AppConfig

        ini_path = tmp_path / "config.ini"
        ini_path.write_text("[ui]\nstartup_panel = unknown\n", encoding="utf-8")

        with patch("core.config.QSettings") as mock_qs:
            inst = MagicMock()
            inst.value.side_effect = lambda key, default=None: default
            mock_qs.return_value = inst
            with patch("core.config._app_data_dir", return_value=tmp_path), \
                 patch.object(cfg_mod, "_INI_PATH", ini_path):
                cfg = AppConfig()

        assert cfg.startup_panel == "dashboard"


class TestAppConfigWindowsToolAutodetect:
    """Tests de détection et persistance auto des outils Windows dans config.ini."""

    def _mock_qsettings(self):
        inst = MagicMock()
        inst.value.side_effect = lambda key, default=None: default
        return inst

    def test_windows_autodetects_repo_tool_and_updates_ini(self, tmp_path):
        """Un binaire local dans tools/ est détecté et écrit dans config.ini."""
        import core.config as cfg_mod
        from core.config import AppConfig

        ini_path = tmp_path / "config.ini"
        ini_path.write_text("[tools]\n", encoding="utf-8")
        tool_path = tmp_path / "tools" / "dovi_tool.exe"
        tool_path.parent.mkdir(parents=True, exist_ok=True)
        tool_path.write_text("", encoding="utf-8")

        with patch("core.config.QSettings") as mock_qs, \
             patch("core.config.sys.platform", "win32"), \
             patch("core.config.shutil.which", return_value=None), \
             patch("core.config._app_data_dir", return_value=tmp_path), \
             patch.object(cfg_mod, "_INI_PATH", ini_path):
            mock_qs.return_value = self._mock_qsettings()
            cfg = AppConfig()

        assert cfg.tool_dovi_tool == str(tool_path)
        assert f"dovi_tool = {tool_path}" in ini_path.read_text(encoding="utf-8")

    def test_windows_autodetects_winget_tool_and_updates_ini(self, tmp_path):
        """Un binaire installé via winget est détecté et écrit dans config.ini."""
        import core.config as cfg_mod
        from core.config import AppConfig

        ini_path = tmp_path / "config.ini"
        ini_path.write_text("[tools]\n", encoding="utf-8")
        ffmpeg_path = (
            tmp_path / "localapp" / "Microsoft" / "WinGet" / "Packages"
            / "Gyan.FFmpeg_1.0.0_x64__test" / "ffmpeg-7.1-full_build" / "bin" / "ffmpeg.exe"
        )
        ffmpeg_path.parent.mkdir(parents=True, exist_ok=True)
        ffmpeg_path.write_text("", encoding="utf-8")

        with patch("core.config.QSettings") as mock_qs, \
             patch("core.config.sys.platform", "win32"), \
             patch("core.config.shutil.which", return_value=None), \
             patch("core.config._app_data_dir", return_value=tmp_path), \
             patch.object(cfg_mod, "_INI_PATH", ini_path), \
             patch.dict(os.environ, {"LOCALAPPDATA": str(tmp_path / "localapp")}, clear=False):
            mock_qs.return_value = self._mock_qsettings()
            cfg = AppConfig()

        assert cfg.tool_ffmpeg == str(ffmpeg_path)
        assert f"ffmpeg = {ffmpeg_path}" in ini_path.read_text(encoding="utf-8")

    def test_windows_keeps_explicit_ini_tool_value(self, tmp_path):
        """Une valeur explicite dans config.ini reste prioritaire sur l'autodetect."""
        import core.config as cfg_mod
        from core.config import AppConfig

        explicit = r"C:\custom\ffmpeg.exe"
        ini_path = tmp_path / "config.ini"
        ini_path.write_text(f"[tools]\nffmpeg = {explicit}\n", encoding="utf-8")
        detected_path = tmp_path / "tools" / "ffmpeg.exe"
        detected_path.parent.mkdir(parents=True, exist_ok=True)
        detected_path.write_text("", encoding="utf-8")

        with patch("core.config.QSettings") as mock_qs, \
             patch("core.config.sys.platform", "win32"), \
             patch("core.config.shutil.which", return_value=None), \
             patch("core.config._app_data_dir", return_value=tmp_path), \
             patch.object(cfg_mod, "_INI_PATH", ini_path):
            mock_qs.return_value = self._mock_qsettings()
            cfg = AppConfig()

        assert cfg.tool_ffmpeg == explicit
        assert ini_path.read_text(encoding="utf-8").count("ffmpeg =") == 1


def test_setup_initializes_ui_language_in_config_ini(tmp_path):
    """setup.py initialise ui.language depuis la langue système quand la clé est absente."""
    import setup as setup_mod

    ini_path = tmp_path / "config.ini"
    ini_path.write_text("[ui]\n", encoding="utf-8")

    with patch.object(setup_mod, "_config_ini_path", return_value=ini_path), \
         patch.object(setup_mod, "_system_language_code", return_value="fra"):
        setup_mod.initialize_config_ini_language(dry_run=False, force=False)

    assert "language = fra" in ini_path.read_text(encoding="utf-8")
