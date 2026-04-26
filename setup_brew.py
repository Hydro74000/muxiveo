#!/usr/bin/env python3
"""
Mediarecode Homebrew post-install helper.

This script manages user-scoped desktop integration that does not belong inside
the Homebrew prefix:
  - Linux desktop shortcut in ~/.local/share/applications
  - Linux icon in <prefix>/share/icons/hicolor/256x256/apps
  - macOS app link in ~/Applications
  - desktop database refresh for KDE/GNOME when available
"""

from __future__ import annotations

import argparse
import base64
import os
import shutil
import subprocess
from pathlib import Path


ICON_PNG_BASE64 = "__MEDIARECODE_ICON_PNG_BASE64__"
DESKTOP_MIME_TYPES = "__MEDIARECODE_DESKTOP_MIME_TYPES__"


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_binary(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _linux_desktop_file(opt_bin: Path, opt_share: Path) -> str:
    icon_path = opt_share / "icons" / "hicolor" / "256x256" / "apps" / "mediarecode.png"
    return (
        "[Desktop Entry]\n"
        "Name=Mediarecode\n"
        "Comment=MKV/MP4 workflow - DoVi, HDR10+, encoding\n"
        f"Exec={opt_bin / 'mediarecode'} %F\n"
        f"Icon={icon_path}\n"
        "Type=Application\n"
        "Categories=AudioVideo;Video;\n"
        f"MimeType={DESKTOP_MIME_TYPES}\n"
        "Terminal=false\n"
        "StartupNotify=true\n"
    )


def install_linux_shortcut(opt_bin: Path, opt_share: Path) -> None:
    icon_dir = opt_share / "icons" / "hicolor" / "256x256" / "apps"
    icon_path = icon_dir / "mediarecode.png"
    icon_bytes = base64.b64decode(ICON_PNG_BASE64)
    _write_binary(icon_path, icon_bytes)

    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    desktop_file = data_home / "applications" / "mediarecode.desktop"
    _write_text(desktop_file, _linux_desktop_file(opt_bin, opt_share))

    for command in ("update-desktop-database", "kbuildsycoca6", "kbuildsycoca5"):
        executable = shutil.which(command)
        if not executable:
            continue
        args = [executable]
        if command == "update-desktop-database":
            args.append(str(desktop_file.parent))
        subprocess.run(args, check=False)


def install_macos_link(opt_prefix: Path) -> None:
    apps_dir = Path.home() / "Applications"
    apps_dir.mkdir(parents=True, exist_ok=True)
    app_link = apps_dir / "Mediarecode.app"
    app_target = opt_prefix / "Mediarecode.app"
    if app_link.is_symlink() or app_link.exists():
        return
    try:
        app_link.symlink_to(app_target)
    except OSError:
        return


def cleanup_shortcuts() -> None:
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    desktop_file = data_home / "applications" / "mediarecode.desktop"
    app_link = Path.home() / "Applications" / "Mediarecode.app"
    for path in (desktop_file, app_link):
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            continue


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mediarecode Homebrew setup helper.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    post_install = subparsers.add_parser("post-install")
    post_install.add_argument("--platform", choices=("linux", "macos"), required=True)
    post_install.add_argument("--opt-bin", required=True)
    post_install.add_argument("--opt-share", required=True)
    post_install.add_argument("--opt-prefix", required=True)

    subparsers.add_parser("cleanup")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "cleanup":
        cleanup_shortcuts()
        return 0

    opt_bin = Path(args.opt_bin)
    opt_share = Path(args.opt_share)
    opt_prefix = Path(args.opt_prefix)

    if args.platform == "linux":
        install_linux_shortcut(opt_bin, opt_share)
    else:
        install_macos_link(opt_prefix)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
