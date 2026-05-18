from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_render_homebrew_formula_contains_platform_blocks(tmp_path):
    output = tmp_path / "Muxiveo.rb"
    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "render_homebrew_formula.py"),
        "--version",
        "2.0.0",
        "--linux-url",
        "https://example.test/linux.AppImage",
        "--linux-sha256",
        "a" * 64,
        "--macos-url",
        "https://example.test/macos.tar.gz",
        "--macos-sha256",
        "b" * 64,
        "--dovi-tool-macos-url",
        "https://example.test/dovi.zip",
        "--dovi-tool-macos-sha256",
        "c" * 64,
        "--hdr10plus-tool-macos-url",
        "https://example.test/hdr10plus.zip",
        "--hdr10plus-tool-macos-sha256",
        "d" * 64,
        "--output",
        str(output),
    ]
    subprocess.run(cmd, check=True)

    text = output.read_text(encoding="utf-8")
    assert 'class Muxiveo < Formula' in text
    assert 'on_linux do' in text
    assert 'on_macos do' in text
    assert 'depends_on "ffmpeg"' in text
    assert 'depends_on "mediainfo"' in text
    assert 'def install_setup_brew_helper' in text
    assert 'def install_uninstall_shortcuts_script' in text
    assert 'def run_setup_brew(*args)' in text
    assert 'def post_install' in text
    assert '(libexec/"setup_brew.py").write <<~PY' in text
    assert 'python3 is required for Muxiveo Homebrew integration' in text
    assert 'exec "${PYTHON_BIN}" "#{opt_libexec}/setup_brew.py" cleanup' in text
    assert 'run_setup_brew("post-install", "--platform", "linux"' in text
    assert 'run_setup_brew("post-install", "--platform", "macos"' in text
    assert 'def install_linux_shortcut' in text
    assert 'def install_macos_link' in text
    assert 'def cleanup_shortcuts' in text
    assert "f\"Exec={opt_bin / 'muxiveo'} %F" in text
    assert "f\"TryExec={opt_bin / 'muxiveo'}" in text
    assert 'Icon=Muxiveo' in text
    assert 'user_icon = data_home / "icons" / "hicolor" / "256x256" / "apps" / "Muxiveo.png"' in text
    assert 'desktop-file-validate' in text
    assert 'gtk-update-icon-cache' in text
    assert 'xdg-desktop-menu' in text
    assert '~/.local/state/Muxiveo/setup_brew.log' in text
    assert 'import tempfile' in text
    assert 'import traceback' in text
    assert 'def _safe_log(message: str) -> None:' in text
    assert 'fatal-error={exc!r}' in text
    assert 'bin.install_symlink libexec/"muxiveo-uninstall-shortcuts"' in text
    assert 'muxiveo-uninstall-shortcuts' in text
    assert 'brew postinstall Muxiveo' in text
    assert 'ICON_PNG_BASE64 =' in text
    assert 'libexec.install Dir["*.AppImage"].first => "Muxiveo.AppImage"' in text
    assert 'prefix.install "Muxiveo.app"' in text
    assert '(libexec/"muxiveo").write <<~EOS' in text
    assert 'bin.install_symlink libexec/"muxiveo"' in text
    assert 'resource "dovi_tool" do' in text
    assert 'resource "hdr10plus_tool" do' in text
    assert 'setup_brew.py" post-install --platform linux' in text
