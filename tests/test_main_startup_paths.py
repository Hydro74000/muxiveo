from __future__ import annotations

import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import main as main_mod


def test_startup_paths_from_argv_filters_only_existing_files(tmp_path) -> None:
    existing_a = tmp_path / "a.mkv"
    existing_b = tmp_path / "b.mp4"
    existing_a.write_text("", encoding="utf-8")
    existing_b.write_text("", encoding="utf-8")

    argv = [
        "mediarecode",
        str(existing_a),
        "--debug",
        str(tmp_path / "missing.mov"),
        str(existing_b),
    ]

    assert main_mod._startup_paths_from_argv(argv) == [existing_a, existing_b]


def test_main_routes_startup_file_to_main_window(tmp_path) -> None:
    startup_file = tmp_path / "movie.mkv"
    startup_file.write_text("", encoding="utf-8")

    class FakeQApplication:
        def __init__(self):
            self.setApplicationName = MagicMock()
            self.setApplicationVersion = MagicMock()
            self.setOrganizationName = MagicMock()
            self.setFont = MagicMock()
            self.exec = MagicMock(return_value=0)

        @staticmethod
        def instance():
            return fake_app

        @staticmethod
        def setAttribute(_attr):
            return None

    fake_app = FakeQApplication()

    fake_window = types.SimpleNamespace(
        show=MagicMock(),
        open_startup_paths=MagicMock(),
    )

    class FakeMainWindow:
        def __init__(self, config):
            self.config = config
            self._instance = fake_window

        def __getattr__(self, name):
            return getattr(fake_window, name)

    fake_ui_main_window = types.ModuleType("ui.main_window")
    fake_ui_main_window.MainWindow = FakeMainWindow

    scheduled: dict[str, object] = {}

    def fake_single_shot(delay_ms, callback):
        scheduled["delay_ms"] = delay_ms
        scheduled["callback"] = callback

    fake_config = types.SimpleNamespace(
        theme="dark",
        ui_scale_percent=100,
        language="eng",
    )

    with patch.object(main_mod, "QApplication", FakeQApplication), \
         patch.object(main_mod, "AppConfig", return_value=fake_config), \
         patch.object(main_mod, "DesignSystem", autospec=True) as mock_design, \
         patch.object(main_mod, "set_current_language") as mock_set_language, \
         patch.object(main_mod, "_prompt_work_dir_cleanup") as mock_cleanup, \
         patch.object(main_mod.QTimer, "singleShot", side_effect=fake_single_shot), \
         patch.dict("sys.modules", {"ui.main_window": fake_ui_main_window}), \
         patch.object(main_mod.sys, "argv", ["mediarecode", str(startup_file), "--verbose"]):
        mock_design.scale_factor.return_value = 1.0
        rc = main_mod.main()

    assert rc == 0
    mock_design.set_theme.assert_called_once_with("dark")
    mock_design.set_ui_scale.assert_called_once_with(100)
    mock_design.apply_to_application.assert_called_once_with(fake_app)
    mock_set_language.assert_called_once_with("eng")
    mock_cleanup.assert_called_once_with(fake_config)
    fake_app.setApplicationName.assert_called_once_with("Mediarecode")
    fake_app.setApplicationVersion.assert_called_once()
    fake_app.setOrganizationName.assert_called_once_with("mediarecode")
    fake_app.setFont.assert_called_once()
    fake_window.show.assert_called_once_with()
    assert scheduled["delay_ms"] == 0
    scheduled["callback"]()
    fake_window.open_startup_paths.assert_called_once_with([startup_file])
