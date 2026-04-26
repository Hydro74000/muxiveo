#!/usr/bin/env python3
"""
Render the Homebrew formula used to publish Mediarecode for Linux and macOS.
"""

from __future__ import annotations

import argparse
from pathlib import Path


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
    return f"""class Mediarecode < Formula
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

  def install
    if OS.mac?
      prefix.install "Mediarecode.app"
      (libexec/"tools").mkpath

      resource("dovi_tool").stage do
        libexec.install Dir["**/dovi_tool"].first => "tools/dovi_tool"
      end

      resource("hdr10plus_tool").stage do
        libexec.install Dir["**/hdr10plus_tool"].first => "tools/hdr10plus_tool"
      end

      chmod 0755, libexec/"tools/dovi_tool"
      chmod 0755, libexec/"tools/hdr10plus_tool"

      (libexec/"mediarecode").write <<~EOS
        #!/bin/bash
        set -euo pipefail
        CONFIG_DIR="${{XDG_CONFIG_HOME:-$HOME/.config}}/mediarecode"
        CONFIG_FILE="${{CONFIG_DIR}}/config.ini"
        mkdir -p "${{CONFIG_DIR}}"
        if [ ! -f "${{CONFIG_FILE}}" ]; then
          cat > "${{CONFIG_FILE}}" <<'CFG'
# Mediarecode - configuration locale
# Fichier cree par le wrapper Homebrew pour eviter le setup interactif.
CFG
        fi
        export PATH="#{{opt_libexec}}/tools:#{{HOMEBREW_PREFIX}}/bin:$PATH"
        exec "#{{opt_prefix}}/Mediarecode.app/Contents/MacOS/Mediarecode" "$@"
      EOS
      chmod 0755, libexec/"mediarecode"
      bin.install_symlink libexec/"mediarecode"
    else
      libexec.install Dir["*.AppImage"].first => "Mediarecode.AppImage"
      chmod 0755, libexec/"Mediarecode.AppImage"

      (libexec/"mediarecode").write <<~EOS
        #!/bin/bash
        set -euo pipefail
        CONFIG_DIR="${{XDG_CONFIG_HOME:-$HOME/.config}}/mediarecode"
        CONFIG_FILE="${{CONFIG_DIR}}/config.ini"
        mkdir -p "${{CONFIG_DIR}}"
        if [ ! -f "${{CONFIG_FILE}}" ]; then
          cat > "${{CONFIG_FILE}}" <<'CFG'
# Mediarecode - configuration locale
# Fichier cree par le wrapper Homebrew.
CFG
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

  test do
    if OS.mac?
      assert_predicate prefix/"Mediarecode.app", :exist?
      assert_predicate bin/"mediarecode", :exist?
    else
      assert_predicate libexec/"Mediarecode.AppImage", :exist?
      assert_predicate bin/"mediarecode", :exist?
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
