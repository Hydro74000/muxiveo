from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import package as package_mod


def test_pyinstaller_frontend_flag_uses_windowed_on_windows():
    assert package_mod._pyinstaller_frontend_flag("Windows") == "--windowed"


def test_pyinstaller_frontend_flag_keeps_console_on_linux():
    assert package_mod._pyinstaller_frontend_flag("Linux") == "--console"


def test_nsis_bundle_glob_uses_posix_separator_on_linux():
    with patch.object(package_mod, "OS", "Linux"):
        assert package_mod._nsis_bundle_glob(Path("/tmp/mediarecode-win")) == "/tmp/mediarecode-win/*"


def test_nsis_bundle_glob_uses_windows_separator_on_windows():
    with patch.object(package_mod, "OS", "Windows"):
        assert package_mod._nsis_bundle_glob(Path(r"C:\tmp\mediarecode")) == r"C:\tmp\mediarecode\*"


def test_build_pyinstaller_uses_windowed_on_native_windows(tmp_path):
    commands: list[list[str]] = []

    def fake_run(cmd, cwd=None):
        commands.append(cmd)
        exe_path = tmp_path / "dist" / "mediarecode" / "mediarecode.exe"
        exe_path.parent.mkdir(parents=True, exist_ok=True)
        exe_path.write_text("", encoding="utf-8")

    version_file = tmp_path / "version.txt"
    version_file.write_text("", encoding="utf-8")

    with patch.object(package_mod, "ROOT", tmp_path), \
         patch.object(package_mod, "OS", "Windows"), \
         patch.object(package_mod, "DATA_FILES", []), \
         patch.object(package_mod, "_ensure_windows_runtime_dlls_available"), \
         patch.object(package_mod, "_add_windows_ctypes_to_pyinstaller_native"), \
         patch.object(package_mod, "_add_windows_sqlite_to_pyinstaller_native"), \
         patch.object(package_mod, "_add_windows_ssl_to_pyinstaller_native"), \
         patch.object(package_mod, "_write_windows_version_file", return_value=version_file), \
         patch.object(package_mod, "_resolve_windows_icon_ico", return_value=None), \
         patch.object(package_mod, "_verify_windows_runtime_bundle"), \
         patch.object(package_mod, "_run", side_effect=fake_run):
        package_mod._build_pyinstaller(onefile=False)

    assert commands
    assert "--windowed" in commands[0]
    assert "--console" not in commands[0]


def _prepare_linux_bundle(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "dist" / "mediarecode"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / "mediarecode").write_text("", encoding="utf-8")


def test_build_appdir_prefers_icon_ico_when_available(tmp_path):
    _prepare_linux_bundle(tmp_path)
    ico_path = tmp_path / "icon.ico"
    ico_path.write_bytes(b"ico")
    png_path = tmp_path / "icon.png"
    png_path.write_bytes(b"png-fallback")

    def fake_convert(_src: Path, dest: Path) -> bool:
        dest.write_bytes(b"png-from-ico")
        return True

    with patch.object(package_mod, "ROOT", tmp_path), \
         patch.object(package_mod, "ICON_ICO", ico_path), \
         patch.object(package_mod, "ICON_PNG", png_path), \
         patch.object(package_mod, "_convert_ico_to_png", side_effect=fake_convert):
        appdir = package_mod._build_appdir()

    assert (appdir / "Mediarecode.png").read_bytes() == b"png-from-ico"


def test_build_appdir_falls_back_to_icon_png_if_ico_conversion_fails(tmp_path):
    _prepare_linux_bundle(tmp_path)
    ico_path = tmp_path / "icon.ico"
    ico_path.write_bytes(b"ico")
    png_path = tmp_path / "icon.png"
    png_path.write_bytes(b"png-fallback")

    with patch.object(package_mod, "ROOT", tmp_path), \
         patch.object(package_mod, "ICON_ICO", ico_path), \
         patch.object(package_mod, "ICON_PNG", png_path), \
         patch.object(package_mod, "_convert_ico_to_png", return_value=False):
        appdir = package_mod._build_appdir()

    assert (appdir / "Mediarecode.png").read_bytes() == b"png-fallback"
