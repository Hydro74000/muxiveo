from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_render_homebrew_formula_contains_platform_blocks(tmp_path):
    output = tmp_path / "mediarecode.rb"
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
    assert 'class Mediarecode < Formula' in text
    assert 'on_linux do' in text
    assert 'on_macos do' in text
    assert 'depends_on "ffmpeg"' in text
    assert 'depends_on "mediainfo"' in text
    assert 'libexec.install Dir["*.AppImage"].first => "Mediarecode.AppImage"' in text
    assert 'prefix.install "Mediarecode.app"' in text
    assert 'resource "dovi_tool" do' in text
    assert 'resource "hdr10plus_tool" do' in text
