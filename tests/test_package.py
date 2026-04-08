from __future__ import annotations

import subprocess
import struct
import zlib
from pathlib import Path
from unittest.mock import patch

import package as package_mod
import package_appimage as package_appimage_mod


def _png_bytes(width: int, height: int) -> bytes:
    def _chunk(name: bytes, data: bytes) -> bytes:
        payload = name + data
        return struct.pack(">I", len(data)) + payload + struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF)

    raw = b""
    for _ in range(height):
        raw += b"\x00" + (b"\xff\x00\x00" * width)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(raw))
        + _chunk(b"IEND", b"")
    )


def _ico_with_png_frames(frames: list[tuple[int, int, bytes]]) -> bytes:
    header = struct.pack("<HHH", 0, 1, len(frames))
    entries: list[bytes] = []
    payloads = b""
    offset = 6 + len(frames) * 16
    for width, height, png_data in frames:
        entries.append(
            struct.pack(
                "<BBBBHHII",
                0 if width >= 256 else width,
                0 if height >= 256 else height,
                0,
                0,
                1,
                32,
                len(png_data),
                offset,
            )
        )
        payloads += png_data
        offset += len(png_data)
    return header + b"".join(entries) + payloads


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


def test_ensure_wine_deps_pins_pyside6_and_verifies_runtime():
    with patch.object(package_mod, "_WIN_PYSIDE6_VER", "6.10.2"), \
         patch.object(package_mod, "_wine_pip") as mock_wine_pip, \
         patch.object(package_mod, "_ensure_wine_qt_icu_runtime") as mock_icu, \
         patch.object(package_mod, "_verify_wine_pyside6_runtime") as mock_verify:
        package_mod._ensure_wine_deps()

    mock_wine_pip.assert_called_once_with(
        "pyinstaller",
        "PySide6==6.10.2",
        "pymediainfo>=6.1.0",
    )
    mock_icu.assert_called_once_with()
    mock_verify.assert_called_once_with()


def test_verify_wine_pyside6_runtime_raises_with_missing_dlls(tmp_path):
    wine_python = tmp_path / "drive_c" / "Python311" / "python.exe"
    pyside_dir = wine_python.parent / "Lib" / "site-packages" / "PySide6"
    pyside_dir.mkdir(parents=True, exist_ok=True)
    (pyside_dir / "Qt6Core.dll").write_text("", encoding="utf-8")

    stderr = "\n".join(
        [
            "0044:err:module:import_dll Library icuuc.dll (which is needed by L\"C:\\\\Python311\\\\Lib\\\\site-packages\\\\PySide6\\\\Qt6Core.dll\") not found",
            "0044:err:module:import_dll Library Qt6Core.dll (which is needed by L\"C:\\\\Python311\\\\Lib\\\\site-packages\\\\PySide6\\\\QtCore.pyd\") not found",
            "ImportError: DLL load failed while importing QtCore: Module introuvable.",
        ]
    )

    with patch.object(package_mod, "_WIN_PY_EXE", wine_python), \
         patch.object(package_mod, "_wine_env", return_value={}), \
         patch.object(
             package_mod.subprocess,
             "run",
             return_value=subprocess.CompletedProcess(args=["wine"], returncode=1, stdout="", stderr=stderr),
         ):
        try:
            package_mod._verify_wine_pyside6_runtime()
        except RuntimeError as exc:
            text = str(exc)
        else:
            raise AssertionError("RuntimeError not raised")

    assert "PySide6.QtCore" in text
    assert "icuuc.dll" in text
    assert "Absentes du package PySide6 installé" in text


def test_sync_windows_icu_dlls_copies_versioned_files_and_aliases(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "icuuc72.dll").write_text("icuuc", encoding="utf-8")
    (source / "icuin72.dll").write_text("icuin", encoding="utf-8")
    (source / "icudt72.dll").write_text("icudt", encoding="utf-8")

    target = tmp_path / "dst"
    copied = package_mod._sync_windows_icu_dlls(target, sorted(source.glob("icu*.dll")))
    copied_names = {path.name for path in copied}

    assert "icuuc72.dll" in copied_names
    assert "icuin72.dll" in copied_names
    assert "icudt72.dll" in copied_names
    assert "icuuc.dll" in copied_names
    assert "icuin.dll" in copied_names
    assert "icudt.dll" in copied_names
    assert (target / "icuuc.dll").read_text(encoding="utf-8") == "icuuc"


def test_convert_ico_to_png_uses_largest_embedded_png_frame(tmp_path):
    ico_path = tmp_path / "icon.ico"
    ico_path.write_bytes(
        _ico_with_png_frames(
            [
                (16, 16, _png_bytes(16, 16)),
                (256, 256, _png_bytes(256, 256)),
            ]
        )
    )
    out_path = tmp_path / "icon.png"

    assert package_mod._convert_ico_to_png(ico_path, out_path) is True

    data = out_path.read_bytes()
    width, height = struct.unpack(">II", data[16:24])
    assert (width, height) == (256, 256)


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
         patch.object(package_mod, "_resolve_windows_icon_ico", return_value=ico_path), \
         patch.object(package_mod, "_convert_ico_to_png", side_effect=fake_convert):
        appdir = package_mod._build_appdir()

    assert (appdir / "Mediarecode.png").read_bytes() == b"png-from-ico"
    assert (appdir / "Mediarecode.ico").read_bytes() == b"ico"
    assert (appdir / ".DirIcon").is_symlink()
    assert (appdir / ".DirIcon").readlink() == Path("Mediarecode.png")


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


def test_package_appimage_build_appdir_prefers_icon_ico_when_available(tmp_path):
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    (bundle_dir / "mediarecode").write_text("", encoding="utf-8")
    ico_path = tmp_path / "icon.ico"
    ico_path.write_bytes(b"ico")

    def fake_convert(_src: Path, dest: Path) -> bool:
        dest.write_bytes(b"png-from-ico")
        return True

    with patch.object(package_appimage_mod, "ROOT", tmp_path), \
         patch.object(package_appimage_mod, "APPDIR", tmp_path / "Mediarecode.AppDir"), \
         patch.object(package_appimage_mod, "_convert_ico_to_png", side_effect=fake_convert):
        appdir = package_appimage_mod.build_appdir(bundle_dir)

    assert (appdir / "mediarecode.ico").read_bytes() == b"ico"
    assert (appdir / "mediarecode.png").read_bytes() == b"png-from-ico"
    assert (appdir / ".DirIcon").is_symlink()
    assert (appdir / ".DirIcon").readlink() == Path("mediarecode.png")


def test_package_versioned_output_path_uses_app_version_by_default():
    with patch.object(package_mod, "APP_VERSION", "9.9.9"):
        output = package_mod._versioned_output_path(
            Path("/tmp/Mediarecode-x86_64.AppImage"),
            None,
        )
    assert output.name == "Mediarecode-x86_64-9.9.9.AppImage"


def test_package_appimage_versioned_output_path_uses_app_version_by_default():
    with patch.object(package_appimage_mod, "APP_VERSION", "8.8.8"):
        output = package_appimage_mod._versioned_output_path(
            Path("/tmp/Mediarecode-x86_64_allinc.AppImage"),
            None,
        )
    assert output.name == "Mediarecode-x86_64_allinc-8.8.8.AppImage"
