from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import launcher


class TestLauncherWindowsControlledFolderAccess:
    def test_restart_current_app_uses_sys_executable_when_frozen(self):
        with patch.object(launcher.sys, "executable", r"C:\Apps\Mediarecode\mediarecode.exe"), \
             patch.object(launcher.sys, "frozen", True, create=True), \
             patch("launcher.subprocess.Popen") as mock_popen:
            assert launcher._restart_current_app() is True

        mock_popen.assert_called_once_with(
            [r"C:\Apps\Mediarecode\mediarecode.exe"]
        )

    def test_first_time_setup_restarts_after_cfa_update(self, tmp_path):
        fake_setup = SimpleNamespace(
            _default_prefix=lambda: tmp_path / "tools",
            install_winget=MagicMock(),
            install_github_tools=MagicMock(),
            autofill_windows_config_ini=MagicMock(),
            check_tools_presence=MagicMock(),
            offer_windows_controlled_folder_access_setup=MagicMock(
                return_value={"status": "updated", "added": [], "skipped": [], "message": ""}
            ),
            initialize_config_ini_language=MagicMock(),
            install_python_packages=MagicMock(),
        )

        with patch("platform.system", return_value="Windows"), \
             patch.object(launcher, "_is_allinc", return_value=False), \
             patch.dict(sys.modules, {"setup": fake_setup}), \
             patch.object(launcher, "_restart_current_app", return_value=True) as mock_restart:
            rc = launcher._run_first_time_setup(tmp_path)

        assert rc == 0
        mock_restart.assert_called_once_with()
