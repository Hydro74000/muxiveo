"""
tests/test_setup_and_config.py — Tests unitaires pour setup.py et core/config.py
"""

from __future__ import annotations

import os
from types import SimpleNamespace
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

    def test_default_remux_backend_is_ffmpeg(self, tmp_path):
        """Sans clé explicite, le backend remux par défaut est FFmpeg."""
        from core.config import AppConfig

        with patch("core.config.QSettings") as mock_qs:
            inst = MagicMock()
            inst.value.side_effect = lambda key, default=None: default
            mock_qs.return_value = inst
            with patch("core.config._app_data_dir", return_value=tmp_path), \
                 patch.dict(os.environ, {}, clear=False):
                cfg = AppConfig()

        assert cfg.remux_backend == "ffmpeg"

    def test_remux_backend_ini_accepts_mkvmerge(self, tmp_path):
        """config.ini [remux] backend=mkvmerge force le backend MKVToolNix."""
        import core.config as cfg_mod
        from core.config import AppConfig

        ini_path = tmp_path / "config.ini"
        ini_path.write_text("[remux]\nbackend = mkvmerge\n", encoding="utf-8")

        with patch("core.config.QSettings") as mock_qs:
            inst = MagicMock()
            inst.value.side_effect = lambda key, default=None: default
            mock_qs.return_value = inst
            with patch("core.config._app_data_dir", return_value=tmp_path), \
                 patch.object(cfg_mod, "_INI_PATH", ini_path):
                cfg = AppConfig()

        assert cfg.remux_backend == "mkvmerge"

    def test_audio_encoding_defaults_use_192_and_64(self, tmp_path):
        """Sans configuration explicite, l'audio utilise 192 kbps/canal et des paliers de 64."""
        from core.config import AppConfig

        with patch("core.config.QSettings") as mock_qs:
            inst = MagicMock()
            inst.value.side_effect = lambda key, default=None: default
            mock_qs.return_value = inst
            with patch("core.config._app_data_dir", return_value=tmp_path), \
                 patch.dict(os.environ, {}, clear=False):
                cfg = AppConfig()

        assert cfg.audio_default_bitrate_per_channel_kbps == 192
        assert cfg.audio_bitrate_step_per_channel_kbps == 64

    def test_audio_encoding_ini_overrides_defaults(self, tmp_path):
        """config.ini peut surcharger le bitrate/canal et le palier de combobox audio."""
        import core.config as cfg_mod
        from core.config import AppConfig

        ini_path = tmp_path / "config.ini"
        ini_path.write_text(
            "[audio_encoding]\n"
            "default_bitrate_per_channel_kbps = 160\n"
            "bitrate_step_per_channel_kbps = 48\n",
            encoding="utf-8",
        )

        with patch("core.config.QSettings") as mock_qs:
            inst = MagicMock()
            inst.value.side_effect = lambda key, default=None: default
            mock_qs.return_value = inst
            with patch("core.config._app_data_dir", return_value=tmp_path), \
                 patch.object(cfg_mod, "_INI_PATH", ini_path):
                cfg = AppConfig()

        assert cfg.audio_default_bitrate_per_channel_kbps == 160
        assert cfg.audio_bitrate_step_per_channel_kbps == 48

    def test_ffmpeg_threads_default_uses_cpu_count_times_0_75(self, tmp_path):
        """Sans valeur explicite, ffmpeg.threads vaut cores × 0,75 arrondi au supérieur."""
        from core.config import AppConfig

        with patch("core.config.QSettings") as mock_qs, \
             patch("core.config.os.cpu_count", return_value=8):
            inst = MagicMock()
            inst.value.side_effect = lambda key, default=None: default
            mock_qs.return_value = inst
            with patch("core.config._app_data_dir", return_value=tmp_path), \
                 patch.dict(os.environ, {}, clear=False):
                cfg = AppConfig()

        assert cfg.ffmpeg_threads == 6

    def test_ffmpeg_threads_ini_overrides_default(self, tmp_path):
        """config.ini [ffmpeg] threads surcharge le défaut calculé."""
        import core.config as cfg_mod
        from core.config import AppConfig

        ini_path = tmp_path / "config.ini"
        ini_path.write_text("[ffmpeg]\nthreads = 20\n", encoding="utf-8")

        with patch("core.config.QSettings") as mock_qs:
            inst = MagicMock()
            inst.value.side_effect = lambda key, default=None: default
            mock_qs.return_value = inst
            with patch("core.config._app_data_dir", return_value=tmp_path), \
                 patch.object(cfg_mod, "_INI_PATH", ini_path):
                cfg = AppConfig()

        assert cfg.ffmpeg_threads == 20

    def test_ffmpeg_threads_negative_ini_value_falls_back_to_default(self, tmp_path):
        """Une valeur négative revient au défaut calculé au lieu d'être passée telle quelle."""
        import core.config as cfg_mod
        from core.config import AppConfig

        ini_path = tmp_path / "config.ini"
        ini_path.write_text("[ffmpeg]\nthreads = -5\n", encoding="utf-8")

        with patch("core.config.QSettings") as mock_qs, \
             patch("core.config.os.cpu_count", return_value=4):
            inst = MagicMock()
            inst.value.side_effect = lambda key, default=None: default
            mock_qs.return_value = inst
            with patch("core.config._app_data_dir", return_value=tmp_path), \
                 patch.object(cfg_mod, "_INI_PATH", ini_path):
                cfg = AppConfig()

        assert cfg.ffmpeg_threads == 3


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


def test_app_config_non_windows_detects_tool_from_absolute_candidates(tmp_path):
    import core.config as cfg_mod
    from core.config import AppConfig

    ini_path = tmp_path / "config.ini"
    ini_path.write_text("[tools]\n", encoding="utf-8")
    candidate = tmp_path / "usr-local" / "bin" / "dovi_tool"
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text("", encoding="utf-8")

    with patch("core.config.QSettings") as mock_qs, \
         patch("core.config.sys.platform", "linux"), \
         patch("core.config.shutil.which", return_value=None), \
         patch("core.config._app_data_dir", return_value=tmp_path), \
         patch.object(cfg_mod, "_INI_PATH", ini_path), \
         patch.object(cfg_mod, "_non_windows_tool_candidates", return_value=[candidate]):
        inst = MagicMock()
        inst.value.side_effect = lambda key, default=None: default
        mock_qs.return_value = inst
        cfg = AppConfig()

    assert cfg.tool_dovi_tool == str(candidate)


class TestToolVersionRegistry:
    """Tests unitaires du registre de versions d'outils externes."""

    def test_extract_major_supports_mkvmerge_style(self):
        from core.config import ToolVersionRegistry

        assert ToolVersionRegistry._extract_major("mkvmerge v98.0 ('Chonks') 64-bit") == 98

    def test_extract_major_supports_ffmpeg_style(self):
        from core.config import ToolVersionRegistry

        assert ToolVersionRegistry._extract_major("ffmpeg version 8.1-full_build-www.gyan.dev") == 8

    def test_probe_returns_empty_info_on_failure(self):
        from core.config import ToolVersionRegistry

        reg = ToolVersionRegistry({"mkvmerge": "mkvmerge"})
        with patch("core.config.subprocess.run", side_effect=FileNotFoundError):
            info = reg.get("mkvmerge")

        assert info.text is None
        assert info.major is None

    def test_get_uses_cache(self):
        from core.config import ToolVersionRegistry

        reg = ToolVersionRegistry({"mkvmerge": "mkvmerge"})
        with patch("core.config.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout="mkvmerge v98.0 ('Chonks')\n",
                stderr="",
                returncode=0,
            )
            first = reg.get("mkvmerge")
            second = reg.get("mkvmerge")

        assert first.major == 98
        assert second.major == 98
        assert mock_run.call_count == 1


class TestAppConfigToolVersionPropagation:
    """Tests de propagation des versions d'outils via AppConfig."""

    def _mock_qsettings(self):
        inst = MagicMock()
        inst.value.side_effect = lambda key, default=None: default
        return inst

    def test_tool_major_version_and_text_are_available(self, tmp_path):
        import core.config as cfg_mod
        from core.config import AppConfig

        ini_path = tmp_path / "config.ini"
        ini_path.write_text("[tools]\n", encoding="utf-8")

        def _fake_run(cmd, **_kwargs):
            exe_name = Path(str(cmd[0])).name.lower()
            if exe_name in {"mkvmerge", "mkvmerge.exe"}:
                return MagicMock(
                    stdout="mkvmerge v98.0 ('Chonks')\n",
                    stderr="",
                    returncode=0,
                )
            return MagicMock(stdout="", stderr="", returncode=1)

        with patch("core.config.QSettings") as mock_qs, \
             patch("core.config.sys.platform", "linux"), \
             patch("core.config.shutil.which", return_value=None), \
             patch("core.config.subprocess.run", side_effect=_fake_run) as mock_run, \
             patch("core.config._app_data_dir", return_value=tmp_path), \
             patch.object(cfg_mod, "_INI_PATH", ini_path), \
             patch.object(cfg_mod, "_non_windows_tool_candidates", return_value=[]):
            mock_qs.return_value = self._mock_qsettings()
            cfg = AppConfig()
            assert cfg.tool_major_version("mkvmerge") == 98
            assert cfg.tool_version_text("mkvmerge") == "mkvmerge v98.0 ('Chonks')"
            called_cmds = [call.args[0] for call in mock_run.call_args_list]
            assert any(args[-1] == "--version" for args in called_cmds)

    def test_refresh_tool_versions_reloads_updated_command_map(self, tmp_path):
        import core.config as cfg_mod
        from core.config import AppConfig

        ini_path = tmp_path / "config.ini"
        ini_path.write_text("[tools]\n", encoding="utf-8")

        def _fake_run(cmd, **_kwargs):
            exe = cmd[0]
            outputs = {
                "mkvmerge": "mkvmerge v97.0 ('Glass')",
                "custom-mkvmerge": "mkvmerge v98.1 ('Chonks')",
            }
            text = outputs.get(exe, "")
            return MagicMock(stdout=f"{text}\n" if text else "", stderr="", returncode=0 if text else 1)

        with patch("core.config.QSettings") as mock_qs, \
             patch("core.config.sys.platform", "linux"), \
             patch("core.config.shutil.which", return_value=None), \
             patch("core.config.subprocess.run", side_effect=_fake_run), \
             patch("core.config._app_data_dir", return_value=tmp_path), \
             patch.object(cfg_mod, "_INI_PATH", ini_path), \
             patch.object(cfg_mod, "_non_windows_tool_candidates", return_value=[]):
            mock_qs.return_value = self._mock_qsettings()
            cfg = AppConfig()
            first_major = cfg.tool_major_version("mkvmerge")
            cfg.tool_mkvmerge = "custom-mkvmerge"
            cfg.refresh_tool_versions()
            second_major = cfg.tool_major_version("mkvmerge")

        assert first_major == 97
        assert second_major == 98

class TestWindowsControlledFolderAccessSetup:
    """Tests de la proposition d'allowlist Windows Security (Controlled Folder Access)."""

    def test_windows_cfa_candidate_apps_include_bundle_and_writer_tools(self, tmp_path):
        import setup as setup_mod

        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir()
        app_exe = bundle_dir / "mediarecode.exe"
        app_exe.write_text("", encoding="utf-8")

        ffmpeg = tmp_path / "ffmpeg.exe"
        mkvmerge = tmp_path / "mkvmerge.exe"
        mkvpropedit = tmp_path / "mkvpropedit.exe"
        for exe in (ffmpeg, mkvmerge, mkvpropedit):
            exe.write_text("", encoding="utf-8")

        ini_path = tmp_path / "config.ini"
        ini_path.write_text(
            "[tools]\n"
            f"ffmpeg = {ffmpeg}\n"
            f"mkvmerge = {mkvmerge}\n"
            f"mkvpropedit = {mkvpropedit}\n",
            encoding="utf-8",
        )

        with patch.object(setup_mod, "OS", "Windows"), \
             patch.object(setup_mod, "_config_ini_path", return_value=ini_path), \
             patch.object(setup_mod.sys, "executable", str(app_exe)), \
             patch.object(setup_mod.sys, "frozen", True, create=True):
            paths = setup_mod._windows_cfa_candidate_apps(tmp_path)

        assert paths == [app_exe, ffmpeg, mkvmerge, mkvpropedit]

    def test_offer_windows_cfa_setup_skips_when_disabled(self, tmp_path):
        import setup as setup_mod

        with patch.object(setup_mod, "OS", "Windows"), \
             patch.object(setup_mod, "_windows_controlled_folder_access_state", return_value=0), \
             patch.object(setup_mod, "_windows_cfa_candidate_apps") as mock_candidates, \
             patch.object(setup_mod, "_windows_apply_controlled_folder_access_allowlist") as mock_apply:
            setup_mod.offer_windows_controlled_folder_access_setup(tmp_path, dry_run=False)

        mock_candidates.assert_not_called()
        mock_apply.assert_not_called()

    def test_offer_windows_cfa_setup_prompts_and_applies(self, tmp_path):
        import setup as setup_mod

        ffmpeg = tmp_path / "ffmpeg.exe"
        ffmpeg.write_text("", encoding="utf-8")

        with patch.object(setup_mod, "OS", "Windows"), \
             patch.object(setup_mod, "_windows_controlled_folder_access_state", return_value=1), \
             patch.object(setup_mod, "_windows_cfa_candidate_apps", return_value=[ffmpeg]), \
             patch.object(setup_mod, "_windows_yes_no", return_value=True) as mock_prompt, \
             patch.object(
                 setup_mod,
                 "_windows_apply_controlled_folder_access_allowlist",
                 return_value={
                     "status": "updated",
                     "added": [str(ffmpeg)],
                     "skipped": [],
                     "message": "",
                 },
             ) as mock_apply:
            setup_mod.offer_windows_controlled_folder_access_setup(tmp_path, dry_run=False)

        mock_prompt.assert_called_once()
        mock_apply.assert_called_once_with([ffmpeg])
        prompt_text = mock_prompt.call_args.args[0]
        assert "Videos" in prompt_text
        assert "Documents" in prompt_text
        assert "Without this exception" in prompt_text
        assert "ffmpeg" in prompt_text


def test_setup_initializes_ui_language_in_config_ini(tmp_path):
    """setup.py initialise ui.language depuis la langue système quand la clé est absente."""
    import setup as setup_mod

    ini_path = tmp_path / "config.ini"
    ini_path.write_text("[ui]\n", encoding="utf-8")

    with patch.object(setup_mod, "_config_ini_path", return_value=ini_path), \
         patch.object(setup_mod, "_system_language_code", return_value="fra"), \
         patch.object(setup_mod, "_ask_language_dialog", return_value=None):
        setup_mod.initialize_config_ini_language(dry_run=False, force=False)

    assert "language = fra" in ini_path.read_text(encoding="utf-8")


def test_setup_language_dialog_uses_in_process_qt_when_frozen():
    import setup as setup_mod

    languages = [("eng", "English"), ("fra", "Français")]
    with patch.object(setup_mod.sys, "frozen", True, create=True), \
         patch.object(setup_mod, "_ask_language_dialog_qt_in_process", return_value="fra") as mock_in_process, \
         patch.object(setup_mod.subprocess, "run") as mock_run:
        selected = setup_mod._ask_language_dialog(languages)

    assert selected == "fra"
    mock_in_process.assert_called_once_with(languages)
    mock_run.assert_not_called()


def test_setup_language_dialog_ignores_non_windows_popup(tmp_path):
    import setup as setup_mod

    ini_path = tmp_path / "config.ini"
    ini_path.write_text("[ui]\n", encoding="utf-8")

    with patch.object(setup_mod, "OS", "Linux"), \
         patch.object(setup_mod, "_config_ini_path", return_value=ini_path), \
         patch.object(setup_mod, "_system_language_code", return_value="eng"), \
         patch.object(setup_mod, "_ask_language_dialog") as mock_dialog:
        setup_mod.initialize_config_ini_language(dry_run=False, force=False)

    mock_dialog.assert_not_called()
    assert "language = eng" in ini_path.read_text(encoding="utf-8")


def test_setup_language_dialog_skips_when_language_already_defined_on_windows(tmp_path):
    import setup as setup_mod

    ini_path = tmp_path / "config.ini"
    ini_path.write_text("[ui]\nlanguage = fra\n", encoding="utf-8")

    with patch.object(setup_mod, "OS", "Windows"), \
         patch.object(setup_mod, "_config_ini_path", return_value=ini_path), \
         patch.object(setup_mod, "_ask_language_dialog") as mock_dialog, \
         patch.object(setup_mod, "_system_language_code") as mock_detect:
        setup_mod.initialize_config_ini_language(dry_run=False, force=False)

    mock_dialog.assert_not_called()
    mock_detect.assert_not_called()
    assert "language = fra" in ini_path.read_text(encoding="utf-8")


def test_setup_windows_no_window_kwargs_disabled_when_console_is_visible():
    import setup as setup_mod

    fake_windll = SimpleNamespace(kernel32=SimpleNamespace(GetConsoleWindow=lambda: 1))
    with patch.object(setup_mod, "OS", "Windows"), \
         patch.object(setup_mod.sys, "frozen", True, create=True), \
         patch.object(setup_mod.ctypes, "windll", fake_windll, create=True):
        kwargs = setup_mod._windows_no_window_subprocess_kwargs()

    assert kwargs == {}


def test_setup_windows_no_window_kwargs_disabled_in_cli_mode():
    import setup as setup_mod

    fake_windll = SimpleNamespace(kernel32=SimpleNamespace(GetConsoleWindow=lambda: 0))
    with patch.object(setup_mod, "OS", "Windows"), \
         patch.object(setup_mod.sys, "frozen", False, create=True), \
         patch.object(setup_mod.ctypes, "windll", fake_windll, create=True):
        kwargs = setup_mod._windows_no_window_subprocess_kwargs()

    assert kwargs == {}


def test_setup_config_ini_path_uses_xdg_on_non_windows(tmp_path):
    import setup as setup_mod

    xdg_dir = tmp_path / "xdg"
    with patch.object(setup_mod, "OS", "Linux"), \
         patch.dict(os.environ, {"XDG_CONFIG_HOME": str(xdg_dir)}, clear=False):
        path = setup_mod._config_ini_path()

    assert path == xdg_dir / "mediarecode" / "config.ini"


def test_setup_detect_non_windows_tool_path_reads_ini_value(tmp_path):
    import setup as setup_mod

    tool_path = tmp_path / "custom" / "dovi_tool"
    tool_path.parent.mkdir(parents=True, exist_ok=True)
    tool_path.write_text("", encoding="utf-8")
    ini_path = tmp_path / "config.ini"
    ini_path.write_text(f"[tools]\ndovi_tool = {tool_path}\n", encoding="utf-8")

    with patch.object(setup_mod, "OS", "Linux"), \
         patch.object(setup_mod, "_config_ini_path", return_value=ini_path), \
         patch.object(setup_mod.shutil, "which", return_value=None):
        resolved = setup_mod._detect_non_windows_tool_path("dovi_tool", tmp_path / "prefix")

    assert resolved == str(tool_path)


def test_setup_detect_non_windows_tool_path_uses_prefix_bin(tmp_path):
    import setup as setup_mod

    prefix = tmp_path / "prefix"
    tool_path = prefix / "bin" / "hdr10plus_tool"
    tool_path.parent.mkdir(parents=True, exist_ok=True)
    tool_path.write_text("", encoding="utf-8")
    ini_path = tmp_path / "config.ini"
    ini_path.write_text("[tools]\n", encoding="utf-8")

    with patch.object(setup_mod, "OS", "Linux"), \
         patch.object(setup_mod, "_config_ini_path", return_value=ini_path), \
         patch.object(setup_mod.shutil, "which", return_value=None):
        resolved = setup_mod._detect_non_windows_tool_path("hdr10plus_tool", prefix)

    assert resolved == str(tool_path)


def test_setup_install_github_tools_updates_non_windows_config_ini(tmp_path):
    import setup as setup_mod

    prefix = tmp_path / "prefix"
    ini_path = tmp_path / "config.ini"
    fake_tools = {
        "dovi_tool": {
            "repo": "quietvoid/dovi_tool",
            "desc": "Dolby Vision RPU extraction and injection",
            "binary_name": {"Linux": "dovi_tool"},
            "asset_patterns": {("Linux", "x86_64"): {"suffix": ".tar.gz", "fmt": "tar.gz"}},
        }
    }

    def fake_extract(_archive_path, binary_name, _fmt, dest_dir):
        extracted = dest_dir / binary_name
        extracted.write_text("", encoding="utf-8")
        return extracted

    with patch.object(setup_mod, "OS", "Linux"), \
         patch.object(setup_mod, "GITHUB_TOOLS", fake_tools), \
         patch.object(setup_mod, "_arch_key", return_value="x86_64"), \
         patch.object(setup_mod.shutil, "which", return_value=None), \
         patch.object(setup_mod, "is_root", return_value=True), \
         patch.object(setup_mod, "_github_latest_release", return_value={"tag_name": "v1.0.0"}), \
         patch.object(setup_mod, "_find_asset", return_value="https://example.invalid/dovi_tool.tar.gz"), \
         patch.object(setup_mod, "_download_file"), \
         patch.object(setup_mod, "_extract_binary", side_effect=fake_extract), \
         patch.object(setup_mod, "_config_ini_path", return_value=ini_path), \
         patch.object(setup_mod, "_update_ini_tools_section") as mock_update:
        setup_mod.install_github_tools(prefix, dry_run=False, force=False)

    mock_update.assert_called_once_with(
        ini_path,
        {"dovi_tool": str(prefix / "bin" / "dovi_tool")},
        dry_run=False,
    )


def test_setup_install_github_tools_creates_prefix_bin_with_sudo(tmp_path):
    import setup as setup_mod

    prefix = tmp_path / "prefix"
    fake_tools = {
        "dovi_tool": {
            "repo": "quietvoid/dovi_tool",
            "desc": "Dolby Vision RPU extraction and injection",
            "binary_name": {"Linux": "dovi_tool"},
            "asset_patterns": {("Linux", "x86_64"): {"suffix": ".tar.gz", "fmt": "tar.gz"}},
        }
    }

    def fake_extract(_archive_path, binary_name, _fmt, dest_dir):
        extracted = dest_dir / binary_name
        extracted.write_text("", encoding="utf-8")
        return extracted

    with patch.object(setup_mod, "OS", "Linux"), \
         patch.object(setup_mod, "GITHUB_TOOLS", fake_tools), \
         patch.object(setup_mod, "_arch_key", return_value="x86_64"), \
         patch.object(setup_mod.shutil, "which", return_value=None), \
         patch.object(setup_mod, "is_root", return_value=False), \
         patch.object(setup_mod, "sudo_prefix", return_value=["sudo"]), \
         patch.object(setup_mod, "_github_latest_release", return_value={"tag_name": "v1.0.0"}), \
         patch.object(setup_mod, "_find_asset", return_value="https://example.invalid/dovi_tool.tar.gz"), \
         patch.object(setup_mod, "_download_file"), \
         patch.object(setup_mod, "_extract_binary", side_effect=fake_extract), \
         patch.object(setup_mod, "_config_ini_path", return_value=tmp_path / "config.ini"), \
         patch.object(setup_mod, "_update_ini_tools_section"), \
         patch.object(setup_mod, "run") as mock_run:
        setup_mod.install_github_tools(prefix, dry_run=False, force=False)

    commands = [call.args[0] for call in mock_run.call_args_list]
    assert ["sudo", "mkdir", "-p", str(prefix / "bin")] in commands
