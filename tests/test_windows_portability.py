"""tests/test_windows_portability.py — Garde-fous de portabilité Windows (audit 2026-09-27)."""

from __future__ import annotations

from pathlib import Path, PureWindowsPath

import pytest

from cli.output_template import render_output_template
from cli.remux_config import resolve_final_output
from core.file_types import windows_filename_error
from core.workflows.remux_models import SourceInput


@pytest.mark.parametrize("name", ["a?.mkv", "a:b.mkv", "a|b.mkv", "a<b>.mkv", 'a"b.mkv', "a*.mkv", "fin. ", "CON.mkv", "lpt1.mkv"])
def test_windows_filename_error_rejects_invalid_names(name):
    assert windows_filename_error(name, platform="win32")


@pytest.mark.parametrize("name", ["Film été – test ✓.mkv", "Show.S01E01 [fr-FR].mkv", "console.mkv"])
def test_windows_filename_error_accepts_valid_names(name):
    assert windows_filename_error(name, platform="win32") is None


def test_windows_filename_error_is_noop_off_windows():
    assert windows_filename_error("a?.mkv", platform="linux") is None


def test_output_template_sanitizes_literal_forbidden_chars():
    assert render_output_template("{source_name} [x] ?*:<>|", {"source_name": "Film"}) == "Film [x].mkv"


def test_output_template_keeps_drive_letter():
    assert render_output_template(r"C:\out\{source_name}", {"source_name": "A"}) == r"C:\out\A.mkv"


def test_output_dir_is_base_for_relative_template(tmp_path):
    source = tmp_path / "Film.mkv"
    source.write_bytes(b"")
    job = {"output": str(tmp_path / "out") + "/", "output_template": "{source_name}.remux"}
    out = resolve_final_output(cli_output=None, job=job, sources=[SourceInput(path=source, file_index=0, tracks=[])], details=None)
    assert out == tmp_path / "out" / "Film.remux.mkv"


def test_windows_path_length_error_only_without_long_paths():
    from core.file_types import windows_path_length_error

    long_path = Path("C:/" + "d" * 300 + "/out.mkv")
    assert windows_path_length_error(long_path, platform="win32", long_paths=False)
    assert windows_path_length_error(long_path, platform="win32", long_paths=True) is None
    assert windows_path_length_error(Path("C:/court.mkv"), platform="win32", long_paths=False) is None
    assert windows_path_length_error(long_path, platform="linux") is None


@pytest.mark.parametrize("root", ["\\\\?\\C:\\", "\\\\?\\UNC\\server\\share\\"])
def test_windows_extended_paths_do_not_require_registry_opt_in(root, monkeypatch):
    import core.file_types as file_types

    path = PureWindowsPath(root).joinpath(*(["folder" * 8] * 6), "out.mkv")
    assert len(str(path)) >= 260

    def unexpected_registry_read():
        pytest.fail("Le namespace étendu ne dépend pas du registre")

    monkeypatch.setattr(file_types, "_windows_long_paths_enabled", unexpected_registry_read)
    assert file_types.windows_path_length_error(path, platform="win32") is None
    assert file_types.windows_path_length_error(path, platform="win32", long_paths=False) is None
    ordinary = PureWindowsPath("C:/").joinpath(*path.parts[1:])
    assert file_types.windows_path_length_error(ordinary, platform="win32", long_paths=False)


def test_repair_removes_qsettings_corrupted_windows_paths(tmp_path, monkeypatch):
    """Chemins « C:sers… » (ancien partage QSettings/config.ini) retirés, le reste conservé."""
    import core.config as config_mod

    monkeypatch.setattr(config_mod, "_is_windows", lambda: True)
    ini = tmp_path / "config.ini"
    ini.write_text(
        "# commentaire\n[tools]\nffprobe=C:sersockerppData\\ffprobe.EXE\nffmpeg = C:\\ffmpeg\\ffmpeg.exe\n"
        "[paths]\nwork_dir = C:tmpork\n[ui]\nlanguage = eng\n",
        encoding="utf-8",
    )
    removed = config_mod._repair_corrupted_windows_ini_paths(ini)

    assert removed == ["tools.ffprobe", "paths.work_dir"]
    text = ini.read_text(encoding="utf-8")
    assert "# commentaire" in text and "ffmpeg = C:\\ffmpeg\\ffmpeg.exe" in text and "language = eng" in text


def test_windows_qsettings_never_targets_config_ini(tmp_path, monkeypatch):
    import core.config as config_mod

    monkeypatch.setenv("MUXIVEO_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(config_mod, "_INI_PATH", tmp_path / "config.ini")
    assert config_mod._windows_settings_path() == tmp_path / "muxiveo.conf"
    assert config_mod._windows_settings_path() != config_mod._INI_PATH


@pytest.mark.parametrize("original", [r"C:\tools\dovi_tool.exe", r"C:\temp\Muxiveo\dovi_tool.exe"])
def test_repair_handles_real_qsettings_corruption_with_drive_separator(tmp_path, monkeypatch, original):
    from PySide6.QtCore import QSettings
    import core.config as config_mod

    ini = tmp_path / "config.ini"
    ini.write_text(f"[tools]\ndovi_tool={original}\n", encoding="utf-8")
    settings = QSettings(str(ini), QSettings.Format.IniFormat)
    settings.setValue("ui/last_update_check", 123)
    settings.sync()
    corrupted = ini.read_text(encoding="utf-8")
    assert original not in corrupted and "dovi_tool=C:\\" in corrupted
    del settings

    replacement = tmp_path / "dovi_tool.exe"
    replacement.write_bytes(b"tool")
    monkeypatch.setattr(config_mod, "_is_windows", lambda: True)
    monkeypatch.setattr(config_mod, "_detect_windows_tool_path", lambda *_args: str(replacement))
    assert config_mod._repair_corrupted_windows_ini_paths(ini) == ["tools.dovi_tool"]
    assert "dovi_tool=" not in ini.read_text(encoding="utf-8")
    assert config_mod._repair_corrupted_windows_ini_paths(ini) == []
    monkeypatch.setattr(config_mod, "_INI_PATH", ini)
    monkeypatch.setattr(config_mod, "_appimage_tools_dir", lambda: None)
    config = config_mod.AppConfig.__new__(config_mod.AppConfig)
    config._ini = config_mod._load_ini()
    config._settings = QSettings(str(tmp_path / "muxiveo.conf"), QSettings.Format.IniFormat)
    config._detected_ini_tools = {}
    assert config._resolve_tool_value("dovi_tool", "tools/dovi_tool", "dovi_tool") == str(replacement)
    assert config._detected_ini_tools == {"dovi_tool": str(replacement)}


@pytest.mark.parametrize("legacy,replacement_available", [(False, True), (True, False)])
def test_repair_preserves_ambiguous_missing_paths_without_evidence(tmp_path, monkeypatch, legacy, replacement_available):
    import core.config as config_mod

    ini = tmp_path / "config.ini"
    original = "[tools]\ndovi_tool=C:\\custom\\tool.exe\n[paths]\noutput_dir=D:\\offline\\Films\n"
    if legacy:
        original += "[ui]\nlast_update_check=123\n"
    ini.write_text(original, encoding="utf-8")
    replacement = tmp_path / "dovi_tool.exe"
    if replacement_available:
        replacement.write_bytes(b"tool")
    monkeypatch.setattr(config_mod, "_is_windows", lambda: True)
    monkeypatch.setattr(config_mod, "_detect_windows_tool_path", lambda *_args: str(replacement))
    assert config_mod._repair_corrupted_windows_ini_paths(ini) == []
    assert ini.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("comment", ["", " # version personnalisée", " # 100% personnalisé"])
def test_repair_preserves_existing_tool_with_inline_comment(tmp_path, monkeypatch, comment):
    import core.config as config_mod

    original_path = r"C:\custom\dovi_tool#1.exe"
    ini = tmp_path / "config.ini"
    original = f"[tools]\ndovi_tool={original_path}{comment}\n[ui]\nlast_update_check=123\n"
    ini.write_text(original, encoding="utf-8")
    replacement = tmp_path / "dovi_tool.exe"
    replacement.write_bytes(b"tool")
    exists = Path.exists
    monkeypatch.setattr(Path, "exists", lambda path: str(path) == original_path or exists(path))
    monkeypatch.setattr(config_mod, "_is_windows", lambda: True)
    monkeypatch.setattr(config_mod, "_detect_windows_tool_path", lambda *_args: str(replacement))

    assert config_mod._repair_corrupted_windows_ini_paths(ini) == []
    assert ini.read_text(encoding="utf-8") == original
