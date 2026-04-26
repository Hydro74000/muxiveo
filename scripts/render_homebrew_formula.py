#!/usr/bin/env python3
"""
Render the Homebrew formula used to publish Mediarecode for Linux and macOS.
"""

from __future__ import annotations

import argparse
import base64
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.file_types import build_desktop_mime_type_string


_DESKTOP_MIME_TYPES = build_desktop_mime_type_string()


def _extract_largest_png_from_ico(path: Path) -> bytes:
    data = path.read_bytes()
    if len(data) < 6:
        raise ValueError(f"Invalid ICO file: {path}")

    _reserved, icon_type, count = struct.unpack_from("<HHH", data, 0)
    if icon_type != 1 or count <= 0:
        raise ValueError(f"Unsupported ICO file: {path}")

    best_payload: bytes | None = None
    best_area = -1
    for index in range(count):
        offset = 6 + index * 16
        if offset + 16 > len(data):
            break
        width, height, _colors, _reserved, _planes, _bpp, size, image_offset = struct.unpack_from(
            "<BBBBHHII",
            data,
            offset,
        )
        width = 256 if width == 0 else width
        height = 256 if height == 0 else height
        if image_offset + size > len(data):
            continue
        payload = data[image_offset:image_offset + size]
        if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
            continue
        area = width * height
        if area > best_area:
            best_area = area
            best_payload = payload

    if best_payload is None:
        raise ValueError(f"No embedded PNG frame found in {path}")
    return best_payload


_LINUX_ICON_PNG_BASE64 = base64.b64encode(
    _extract_largest_png_from_ico(ROOT / "icon.ico")
).decode("ascii")


def render_formula(
    *,
    version: str,
    linux_url: str,
    linux_sha256: str,
    macos_url: str,
    macos_sha256: str,
    dovi_tool_macos_url: str,
    dovi_tool_macos_sha256: str,
    hdr10plus_tool_macos_url: str,
    hdr10plus_tool_macos_sha256: str,
    homepage: str = "https://github.com/Hydro74000/mediarecode",
) -> str:
    return f"""require "base64"

class Mediarecode < Formula
  desc "GUI video workflow tool for remuxing, encoding, Dolby Vision and HDR10+"
  homepage "{homepage}"
  version "{version}"
  license "MIT"

  on_linux do
    url "{linux_url}"
    sha256 "{linux_sha256}"
  end

  on_macos do
    url "{macos_url}"
    sha256 "{macos_sha256}"

    depends_on "ffmpeg"
    depends_on "mediainfo"

    resource "dovi_tool" do
      url "{dovi_tool_macos_url}"
      sha256 "{dovi_tool_macos_sha256}"
    end

    resource "hdr10plus_tool" do
      url "{hdr10plus_tool_macos_url}"
      sha256 "{hdr10plus_tool_macos_sha256}"
    end
  end

  def install_linux_desktop_entry
    desktop_dir = Pathname.new(File.expand_path("~/.local/share/applications"))
    desktop_dir.mkpath
    (desktop_dir/"mediarecode.desktop").write <<~EOS
      [Desktop Entry]
      Name=Mediarecode
      Comment=MKV/MP4 workflow - DoVi, HDR10+, encoding
      Exec=#{{opt_bin}}/mediarecode %F
      Icon=#{{opt_share}}/icons/hicolor/256x256/apps/mediarecode.png
      Type=Application
      Categories=AudioVideo;Video;
      MimeType={_DESKTOP_MIME_TYPES}
      Terminal=false
      StartupNotify=true
    EOS
  end

  def install_linux_icon
    icon_dir = share/"icons/hicolor/256x256/apps"
    icon_dir.mkpath
    (icon_dir/"mediarecode.png").binwrite(Base64.decode64("{_LINUX_ICON_PNG_BASE64}"))
  end

  def install_macos_app_link
    apps_dir = Pathname.new(File.expand_path("~/Applications"))
    apps_dir.mkpath
    app_link = apps_dir/"Mediarecode.app"
    return if app_link.exist?

    app_link.make_symlink(opt_prefix/"Mediarecode.app")
  rescue StandardError
    nil
  end

  def install_uninstall_shortcuts_script
    (libexec/"mediarecode-uninstall-shortcuts").write <<~EOS
      #!/bin/bash
      set -euo pipefail
      rm -f "${{XDG_DATA_HOME:-$HOME/.local/share}}/applications/mediarecode.desktop"
      rm -f "$HOME/Applications/Mediarecode.app"
    EOS
    chmod 0755, libexec/"mediarecode-uninstall-shortcuts"
    bin.install_symlink libexec/"mediarecode-uninstall-shortcuts"
  end

  def install
    if OS.mac?
      prefix.install "Mediarecode.app"
      (libexec/"tools").mkpath
      install_uninstall_shortcuts_script

      resource("dovi_tool").stage do
        libexec.install Dir["**/dovi_tool"].first => "tools/dovi_tool"
      end

      resource("hdr10plus_tool").stage do
        libexec.install Dir["**/hdr10plus_tool"].first => "tools/hdr10plus_tool"
      end

      chmod 0755, libexec/"tools/dovi_tool"
      chmod 0755, libexec/"tools/hdr10plus_tool"
      install_macos_app_link

      (libexec/"mediarecode").write <<~EOS
        #!/bin/bash
        set -euo pipefail
        CONFIG_DIR="${{XDG_CONFIG_HOME:-$HOME/.config}}/mediarecode"
        CONFIG_FILE="${{CONFIG_DIR}}/config.ini"
        APPS_DIR="${{HOME}}/Applications"
        APP_LINK="${{APPS_DIR}}/Mediarecode.app"
        mkdir -p "${{CONFIG_DIR}}"
        mkdir -p "${{APPS_DIR}}"
        if [ ! -f "${{CONFIG_FILE}}" ]; then
          cat > "${{CONFIG_FILE}}" <<'CFG'
# Mediarecode - configuration locale
# Fichier cree par le wrapper Homebrew pour eviter le setup interactif.
CFG
        fi
        if [ ! -e "${{APP_LINK}}" ]; then
          ln -s "#{{opt_prefix}}/Mediarecode.app" "${{APP_LINK}}" 2>/dev/null || true
        fi
        export PATH="#{{opt_libexec}}/tools:#{{HOMEBREW_PREFIX}}/bin:$PATH"
        exec "#{{opt_prefix}}/Mediarecode.app/Contents/MacOS/Mediarecode" "$@"
      EOS
      chmod 0755, libexec/"mediarecode"
      bin.install_symlink libexec/"mediarecode"
    else
      libexec.install Dir["*.AppImage"].first => "Mediarecode.AppImage"
      chmod 0755, libexec/"Mediarecode.AppImage"
      install_uninstall_shortcuts_script
      install_linux_icon
      install_linux_desktop_entry

      (libexec/"mediarecode").write <<~EOS
        #!/bin/bash
        set -euo pipefail
        CONFIG_DIR="${{XDG_CONFIG_HOME:-$HOME/.config}}/mediarecode"
        CONFIG_FILE="${{CONFIG_DIR}}/config.ini"
        DESKTOP_DIR="${{XDG_DATA_HOME:-$HOME/.local/share}}/applications"
        DESKTOP_FILE="${{DESKTOP_DIR}}/mediarecode.desktop"
        mkdir -p "${{CONFIG_DIR}}"
        mkdir -p "${{DESKTOP_DIR}}"
        if [ ! -f "${{CONFIG_FILE}}" ]; then
          cat > "${{CONFIG_FILE}}" <<'CFG'
# Mediarecode - configuration locale
# Fichier cree par le wrapper Homebrew.
CFG
        fi
        if [ ! -f "${{DESKTOP_FILE}}" ]; then
          cat > "${{DESKTOP_FILE}}" <<'DESKTOP'
[Desktop Entry]
Name=Mediarecode
Comment=MKV/MP4 workflow - DoVi, HDR10+, encoding
Exec=#{{opt_bin}}/mediarecode %F
Icon=#{{opt_share}}/icons/hicolor/256x256/apps/mediarecode.png
Type=Application
Categories=AudioVideo;Video;
MimeType={_DESKTOP_MIME_TYPES}
Terminal=false
StartupNotify=true
DESKTOP
        fi
        if [ ! -e /dev/fuse ]; then
          export APPIMAGE_EXTRACT_AND_RUN=1
        fi
        exec "#{{opt_libexec}}/Mediarecode.AppImage" "$@"
      EOS
      chmod 0755, libexec/"mediarecode"
      bin.install_symlink libexec/"mediarecode"
    end
  end

  def caveats
    <<~EOS
      User session shortcuts are installed outside the Homebrew prefix.
      Before `brew uninstall mediarecode`, run:
        mediarecode-uninstall-shortcuts
    EOS
  end

  test do
    if OS.mac?
      assert_predicate prefix/"Mediarecode.app", :exist?
      assert_predicate bin/"mediarecode", :exist?
    else
      assert_predicate libexec/"Mediarecode.AppImage", :exist?
      assert_predicate bin/"mediarecode", :exist?
      assert_predicate share/"icons/hicolor/256x256/apps/mediarecode.png", :exist?
    end
  end
end
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render the Mediarecode Homebrew formula.")
    parser.add_argument("--version", required=True)
    parser.add_argument("--linux-url", required=True)
    parser.add_argument("--linux-sha256", required=True)
    parser.add_argument("--macos-url", required=True)
    parser.add_argument("--macos-sha256", required=True)
    parser.add_argument("--dovi-tool-macos-url", required=True)
    parser.add_argument("--dovi-tool-macos-sha256", required=True)
    parser.add_argument("--hdr10plus-tool-macos-url", required=True)
    parser.add_argument("--hdr10plus-tool-macos-sha256", required=True)
    parser.add_argument("--homepage", default="https://github.com/Hydro74000/mediarecode")
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        render_formula(
            version=args.version,
            linux_url=args.linux_url,
            linux_sha256=args.linux_sha256,
            macos_url=args.macos_url,
            macos_sha256=args.macos_sha256,
            dovi_tool_macos_url=args.dovi_tool_macos_url,
            dovi_tool_macos_sha256=args.dovi_tool_macos_sha256,
            hdr10plus_tool_macos_url=args.hdr10plus_tool_macos_url,
            hdr10plus_tool_macos_sha256=args.hdr10plus_tool_macos_sha256,
            homepage=args.homepage,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
