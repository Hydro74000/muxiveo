#!/usr/bin/env python3
"""
Mediarecode — Setup & Dependency Installer
==========================================

Installs Python dependencies and external tools required by the application.

  Linux (Debian/Ubuntu)  → pip + apt packages + GitHub binaries
  Linux (Fedora/RHEL)    → pip + dnf packages + GitHub binaries
  macOS                  → pip + Homebrew packages + GitHub binaries
  Windows                → pip + winget packages + GitHub binaries (user-local)

Usage:
    python3 setup.py [--no-github] [--prefix PATH] [--dry-run]

Options:
    --no-github     Skip downloading dovi_tool / hdr10plus_tool from GitHub
    --prefix PATH   Installation prefix for GitHub binaries
                    Default: /usr/local on Linux/macOS,
                             <mediarecode folder>\\tools on Windows
    --dry-run       Print what would be done without executing anything
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import urllib.error
import zipfile
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Terminal colours (no external deps)
# ---------------------------------------------------------------------------

_USE_COLOR = sys.stdout.isatty() and platform.system() != "Windows"

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text

def ok(msg: str)     -> None: print(_c("32", f"  ✔  {msg}"))
def info(msg: str)   -> None: print(_c("36", f"  →  {msg}"))
def warn(msg: str)   -> None: print(_c("33", f"  ⚠  {msg}"))
def error(msg: str)  -> None: print(_c("31", f"  ✘  {msg}"), file=sys.stderr)
def title(msg: str)  -> None: print(_c("1;34", f"\n{'─'*60}\n  {msg}\n{'─'*60}"))
def step(msg: str)   -> None: print(_c("1;37", f"\n  ▸ {msg}"))

# ---------------------------------------------------------------------------
# OS detection
# ---------------------------------------------------------------------------

OS      = platform.system()           # "Linux" | "Windows" | "Darwin"
MACHINE = platform.machine().lower()  # "x86_64" | "aarch64" | "arm64" | "amd64"

def detect_linux_distro() -> str:
    """Return 'debian', 'fedora', or 'unknown'."""
    try:
        with open("/etc/os-release") as f:
            text = f.read().lower()
        if any(k in text for k in ("ubuntu", "debian", "linuxmint", "pop!_os", "kali", "raspbian")):
            return "debian"
        if any(k in text for k in ("fedora", "rhel", "centos", "rocky", "almalinux", "nobara")):
            return "fedora"
    except FileNotFoundError:
        pass
    if shutil.which("apt-get"):
        return "debian"
    if shutil.which("dnf") or shutil.which("yum"):
        return "fedora"
    return "unknown"

# ---------------------------------------------------------------------------
# Python requirements
# ---------------------------------------------------------------------------

PYTHON_PACKAGES = [
    "PySide6",
]

# ---------------------------------------------------------------------------
# External tools definition
# ---------------------------------------------------------------------------

# Tools available via system package managers
# format: { "executable": {"apt": "pkg", "dnf": "pkg", "brew": "pkg",
#                          "winget": "id", "desc": "..."} }
SYSTEM_TOOLS: dict[str, dict] = {
    "ffmpeg": {
        "apt":    "ffmpeg",
        "dnf":    "ffmpeg",
        "brew":   "ffmpeg",
        "winget": "Gyan.FFmpeg",
        "desc":   "Video/audio encoder and converter",
        "dnf_note": "Requires RPM Fusion (handled automatically)",
    },
    "ffprobe": {
        "apt":    "ffmpeg",
        "dnf":    "ffmpeg",
        "brew":   "ffmpeg",
        "winget": "Gyan.FFmpeg",
        "desc":   "Media file analyser (ships with ffmpeg)",
    },
    "mkvmerge": {
        "apt":    "mkvtoolnix",
        "dnf":    "mkvtoolnix",
        "brew":   "mkvtoolnix",
        "winget": "MKVToolNix.MKVToolNix",
        "desc":   "MKV container muxer",
    },
    "mkvextract": {
        "apt":    "mkvtoolnix",
        "dnf":    "mkvtoolnix",
        "brew":   "mkvtoolnix",
        "winget": "MKVToolNix.MKVToolNix",
        "desc":   "MKV track extractor (ships with mkvtoolnix)",
    },
    "mkvinfo": {
        "apt":    "mkvtoolnix",
        "dnf":    "mkvtoolnix",
        "brew":   "mkvtoolnix",
        "winget": "MKVToolNix.MKVToolNix",
        "desc":   "MKV info tool (ships with mkvtoolnix)",
    },
    "mkvpropedit": {
        "apt":    "mkvtoolnix",
        "dnf":    "mkvtoolnix",
        "brew":   "mkvtoolnix",
        "winget": "MKVToolNix.MKVToolNix",
        "desc":   "MKV metadata editor (ships with mkvtoolnix)",
    },
    "mediainfo": {
        "apt":    "mediainfo",
        "dnf":    "mediainfo",
        "brew":   "mediainfo",
        "winget": "MediaArea.MediaInfo.CLI",
        "desc":   "Media metadata tool",
    },
}

# Tools distributed as GitHub release binaries.
#
# asset_patterns keys: (OS, arch_key)
#   OS       — "Linux" | "Darwin" | "Windows"
#   arch_key — "x86_64" | "arm64"
#
# Each value:
#   suffix  — substring that uniquely identifies the asset filename
#   fmt     — "tar.gz" or "zip"
GITHUB_TOOLS: dict[str, dict] = {
    "dovi_tool": {
        "repo": "quietvoid/dovi_tool",
        "desc": "Dolby Vision RPU extraction and injection",
        "binary_name": {
            "Linux":   "dovi_tool",
            "Darwin":  "dovi_tool",
            "Windows": "dovi_tool.exe",
        },
        "asset_patterns": {
            ("Linux",   "x86_64"): {"suffix": "x86_64-unknown-linux-musl.tar.gz",  "fmt": "tar.gz"},
            ("Linux",   "arm64"):  {"suffix": "aarch64-unknown-linux-musl.tar.gz", "fmt": "tar.gz"},
            ("Darwin",  "x86_64"): {"suffix": "universal-macOS.zip",               "fmt": "zip"},
            ("Darwin",  "arm64"):  {"suffix": "universal-macOS.zip",               "fmt": "zip"},
            ("Windows", "x86_64"): {"suffix": "x86_64-pc-windows-msvc.zip",        "fmt": "zip"},
            ("Windows", "arm64"):  {"suffix": "aarch64-pc-windows-msvc.zip",       "fmt": "zip"},
        },
    },
    "hdr10plus_tool": {
        "repo": "quietvoid/hdr10plus_tool",
        "desc": "HDR10+ metadata extraction and injection",
        "binary_name": {
            "Linux":   "hdr10plus_tool",
            "Darwin":  "hdr10plus_tool",
            "Windows": "hdr10plus_tool.exe",
        },
        "asset_patterns": {
            ("Linux",   "x86_64"): {"suffix": "x86_64-unknown-linux-musl.tar.gz",  "fmt": "tar.gz"},
            ("Linux",   "arm64"):  {"suffix": "aarch64-unknown-linux-musl.tar.gz", "fmt": "tar.gz"},
            ("Darwin",  "x86_64"): {"suffix": "universal-macOS.zip",               "fmt": "zip"},
            ("Darwin",  "arm64"):  {"suffix": "universal-macOS.zip",               "fmt": "zip"},
            ("Windows", "x86_64"): {"suffix": "x86_64-pc-windows-msvc.zip",        "fmt": "zip"},
            ("Windows", "arm64"):  {"suffix": "aarch64-pc-windows-msvc.zip",       "fmt": "zip"},
        },
    },
}

# Tools with no automated install path (optional)
MANUAL_TOOLS: dict[str, dict] = {
    "eac3to": {
        "desc": "Advanced audio conversion (Windows only, optional)",
        "note": "Download from: https://forum.doom9.org/showthread.php?t=125966",
        "platforms": ["Windows"],
    },
}

# ---------------------------------------------------------------------------
# Command runner
# ---------------------------------------------------------------------------

def run(cmd: list[str], dry_run: bool = False, check: bool = True,
        capture: bool = False) -> Optional[subprocess.CompletedProcess]:
    """Run a shell command, optionally printing only (dry_run)."""
    display = " ".join(str(c) for c in cmd)
    info(f"$ {display}")
    if dry_run:
        return None
    result = subprocess.run(
        cmd,
        capture_output=capture,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        stderr = getattr(result, "stderr", "")
        raise RuntimeError(
            f"Command failed (exit {result.returncode}): {display}"
            + (f"\n{stderr.strip()}" if stderr else "")
        )
    return result

# ---------------------------------------------------------------------------
# Privilege helpers
# ---------------------------------------------------------------------------

def is_root() -> bool:
    return os.getuid() == 0 if hasattr(os, "getuid") else False

def sudo_prefix(dry_run: bool = False) -> list[str]:
    """Return ['sudo'] if not root and sudo is available, else [].
    In dry-run mode never raises — returns ['sudo'] as a placeholder."""
    if is_root():
        return []
    if shutil.which("sudo"):
        return ["sudo"]
    if dry_run:
        return ["sudo"]
    raise RuntimeError(
        "This step requires root privileges. "
        "Re-run as root or install sudo."
    )

# ---------------------------------------------------------------------------
# Architecture helpers
# ---------------------------------------------------------------------------

def _arch_key() -> str:
    """Normalise platform.machine() to 'x86_64' or 'arm64'."""
    m = MACHINE
    if m in ("x86_64", "amd64"):
        return "x86_64"
    if m in ("aarch64", "arm64"):
        return "arm64"
    raise RuntimeError(
        f"Unsupported architecture '{platform.machine()}'. "
        "Install dovi_tool and hdr10plus_tool manually."
    )

def _default_prefix() -> Path:
    """Return a sensible default install prefix for the current OS."""
    if OS == "Windows":
        # On Windows: GitHub binaries go to the 'tools' subfolder of the
        # mediarecode package directory (next to this setup.py file).
        return Path(__file__).parent / "tools"
    return Path("/usr/local")

# ---------------------------------------------------------------------------
# Step 1 — Python packages
# ---------------------------------------------------------------------------

def install_python_packages(dry_run: bool) -> None:
    title("Step 1 — Python packages")

    missing = []
    for pkg in PYTHON_PACKAGES:
        module = pkg.split("[")[0].lower().replace("-", "_")
        try:
            __import__(module)
            ok(f"{pkg} already installed")
        except ImportError:
            missing.append(pkg)

    if not missing:
        ok("All Python packages already satisfied")
        return

    step(f"Installing: {', '.join(missing)}")
    run(
        [sys.executable, "-m", "pip", "install", "--upgrade"] + missing,
        dry_run=dry_run,
    )
    ok("Python packages installed")

# ---------------------------------------------------------------------------
# Step 2a — System packages: apt (Debian/Ubuntu)
# ---------------------------------------------------------------------------

def _apt_packages_to_install(dry_run: bool) -> list[str]:
    already_seen: set[str] = set()
    to_install: list[str] = []
    for exe, meta in SYSTEM_TOOLS.items():
        pkg = meta["apt"]
        if pkg in already_seen:
            continue
        already_seen.add(pkg)
        if shutil.which(exe):
            ok(f"{exe} already present")
        else:
            to_install.append(pkg)
    return to_install

def install_apt(dry_run: bool) -> None:
    title("Step 2 — System packages (apt / Debian·Ubuntu)")
    sudo = sudo_prefix(dry_run)

    step("Refreshing package index")
    run(sudo + ["apt-get", "update", "-qq"], dry_run=dry_run)

    to_install = _apt_packages_to_install(dry_run)
    if not to_install:
        ok("All system packages already installed")
        return

    pkgs = sorted(set(to_install))
    step(f"Installing: {' '.join(pkgs)}")
    run(sudo + ["apt-get", "install", "-y"] + pkgs, dry_run=dry_run)
    ok("System packages installed")

# ---------------------------------------------------------------------------
# Step 2b — System packages: dnf (Fedora/RHEL)
# ---------------------------------------------------------------------------

def _ensure_rpmfusion(dry_run: bool, sudo: list[str]) -> None:
    """Enable RPM Fusion Free repo if not already enabled (required for ffmpeg on Fedora)."""
    result = run(
        ["dnf", "repolist", "--enabled"],
        dry_run=False, check=False, capture=True,
    )
    if result and "rpmfusion-free" in (result.stdout or "").lower():
        ok("RPM Fusion Free already enabled")
        return

    warn("RPM Fusion Free not found — enabling it (required for ffmpeg)")
    ver_result = run(
        ["rpm", "-E", "%fedora"],
        dry_run=False, check=False, capture=True,
    )
    fedora_ver = (ver_result.stdout or "").strip() if ver_result else "39"
    if not fedora_ver.isdigit():
        fedora_ver = "39"

    rpmfusion_url = (
        f"https://mirrors.rpmfusion.org/free/fedora/"
        f"rpmfusion-free-release-{fedora_ver}.noarch.rpm"
    )
    step(f"Installing RPM Fusion Free (Fedora {fedora_ver})")
    run(sudo + ["dnf", "install", "-y", rpmfusion_url], dry_run=dry_run)
    ok("RPM Fusion Free enabled")

def install_dnf(dry_run: bool) -> None:
    title("Step 2 — System packages (dnf / Fedora·RHEL)")
    sudo = sudo_prefix(dry_run)

    if not shutil.which("ffmpeg"):
        _ensure_rpmfusion(dry_run, sudo)

    already_seen: set[str] = set()
    to_install: list[str] = []
    for exe, meta in SYSTEM_TOOLS.items():
        pkg = meta["dnf"]
        if pkg in already_seen:
            continue
        already_seen.add(pkg)
        if shutil.which(exe):
            ok(f"{exe} already present")
        else:
            to_install.append(pkg)

    if not to_install:
        ok("All system packages already installed")
        return

    pkgs = sorted(set(to_install))
    step(f"Installing: {' '.join(pkgs)}")
    run(sudo + ["dnf", "install", "-y"] + pkgs, dry_run=dry_run)
    ok("System packages installed")

# ---------------------------------------------------------------------------
# Step 2c — System packages: Homebrew (macOS)
# ---------------------------------------------------------------------------

def install_brew(dry_run: bool) -> None:
    title("Step 2 — System packages (Homebrew / macOS)")

    brew = shutil.which("brew")
    if not brew:
        warn(
            "Homebrew not found — cannot auto-install system packages.\n"
            "   Install Homebrew from: https://brew.sh\n"
            "   Then re-run this script, or install tools manually (see links below)."
        )
        return

    already_seen: set[str] = set()
    to_install: list[str] = []
    for exe, meta in SYSTEM_TOOLS.items():
        pkg = meta.get("brew", "")
        if not pkg or pkg in already_seen:
            continue
        already_seen.add(pkg)
        if shutil.which(exe):
            ok(f"{exe} already present")
        else:
            to_install.append(pkg)

    if not to_install:
        ok("All system packages already installed")
        return

    pkgs = sorted(set(to_install))
    step(f"Installing via brew: {' '.join(pkgs)}")
    run([brew, "install"] + pkgs, dry_run=dry_run)
    ok("System packages installed")

# ---------------------------------------------------------------------------
# Step 2d — System packages: winget (Windows)
# ---------------------------------------------------------------------------

def install_winget(dry_run: bool) -> None:
    title("Step 2 — System packages (winget / Windows)")

    winget = shutil.which("winget")
    if not winget:
        warn(
            "winget not found — cannot auto-install system packages.\n"
            "   winget ships with Windows 10 (1809+) and Windows 11.\n"
            "   Install tools manually (see links below)."
        )
        return

    already_seen: set[str] = set()
    to_install: list[str] = []
    for exe, meta in SYSTEM_TOOLS.items():
        winget_id = meta.get("winget", "")
        if not winget_id or winget_id in already_seen:
            continue
        already_seen.add(winget_id)
        if shutil.which(exe):
            ok(f"{exe} already present")
        else:
            to_install.append(winget_id)

    if not to_install:
        ok("All system packages already installed")
        return

    for pkg_id in to_install:
        step(f"Installing via winget: {pkg_id}")
        run(
            [winget, "install", "--id", pkg_id, "--silent", "--accept-package-agreements",
             "--accept-source-agreements"],
            dry_run=dry_run,
        )
    ok("System packages installed")

# ---------------------------------------------------------------------------
# Step 3 — GitHub binary tools
# ---------------------------------------------------------------------------

def _github_latest_release(repo: str) -> dict:
    """Fetch latest release metadata from GitHub API."""
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    req = urllib.request.Request(url, headers={"User-Agent": "mediarecode-setup/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.URLError as e:
        raise RuntimeError(f"Cannot reach GitHub API for {repo}: {e}") from e

def _download_file(url: str, dest: Path) -> None:
    """Download url → dest with a simple progress indicator."""
    info(f"Downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "mediarecode-setup/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as out:
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            chunk_size = 65536
            while chunk := resp.read(chunk_size):
                out.write(chunk)
                downloaded += len(chunk)
                if total:
                    pct = downloaded * 100 // total
                    print(f"\r    {pct:3d}%  {downloaded//1024} KB / {total//1024} KB  ", end="", flush=True)
            print()
    except urllib.error.URLError as e:
        raise RuntimeError(f"Download failed: {url}\n  {e}") from e

def _find_asset(release: dict, suffix: str) -> Optional[str]:
    """Return the browser_download_url of the first asset whose name ends with suffix."""
    for asset in release.get("assets", []):
        if asset["name"].endswith(suffix):
            return asset["browser_download_url"]
    return None

def _extract_binary(archive_path: Path, binary_name: str, fmt: str, dest_dir: Path) -> Path:
    """Extract binary_name from archive (tar.gz or zip) into dest_dir."""
    if fmt == "tar.gz":
        with tarfile.open(archive_path, "r:gz") as tar:
            member = next(
                (m for m in tar.getmembers()
                 if Path(m.name).name == binary_name and m.isfile()),
                None,
            )
            if not member:
                raise RuntimeError(f"Binary '{binary_name}' not found inside archive")
            member.name = binary_name  # flatten path
            tar.extract(member, path=dest_dir)
    elif fmt == "zip":
        with zipfile.ZipFile(archive_path) as zf:
            entry = next(
                (e for e in zf.namelist() if Path(e).name == binary_name),
                None,
            )
            if not entry:
                raise RuntimeError(f"Binary '{binary_name}' not found inside archive")
            data = zf.read(entry)
            out = dest_dir / binary_name
            out.write_bytes(data)
    else:
        raise RuntimeError(f"Unknown archive format: {fmt}")

    extracted = dest_dir / binary_name
    extracted.chmod(extracted.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return extracted

def install_github_tools(prefix: Path, dry_run: bool) -> None:
    title("Step 3 — GitHub binary tools (dovi_tool, hdr10plus_tool)")

    arch = _arch_key()

    # On Windows: binaries go directly into prefix (mediarecode/tools/).
    # On Linux/macOS: binaries go into prefix/bin/ (/usr/local/bin/).
    if OS == "Windows":
        bin_dir = prefix
    else:
        bin_dir = prefix / "bin"

    # On Windows we install to a user-writable directory (no sudo needed).
    use_sudo = OS != "Windows"
    sudo = sudo_prefix(dry_run) if use_sudo else []

    path_reminder_shown = False

    for exe, meta in GITHUB_TOOLS.items():
        if shutil.which(exe):
            ok(f"{exe} already present ({shutil.which(exe)})")
            continue

        pattern_key = (OS, arch)
        pattern = meta["asset_patterns"].get(pattern_key)
        if not pattern:
            warn(
                f"No pre-built binary for {exe} on {OS}/{arch}. "
                f"Build from source: https://github.com/{meta['repo']}"
            )
            continue

        binary_name = meta["binary_name"].get(OS, meta["binary_name"].get("Linux"))
        step(f"Installing {exe}  ({meta['desc']})")
        info(f"Fetching latest release from github.com/{meta['repo']}")

        if dry_run:
            info(f"[dry-run] Would download and install {exe} to {bin_dir / binary_name}")
            ok(f"{exe} installed → {bin_dir / binary_name}")
            continue

        release = _github_latest_release(meta["repo"])
        tag = release.get("tag_name", "?")
        info(f"Latest release: {tag}")

        download_url = _find_asset(release, pattern["suffix"])
        if not download_url:
            available = [a["name"] for a in release.get("assets", [])]
            raise RuntimeError(
                f"No asset ending with '{pattern['suffix']}' found in release {tag}.\n"
                f"Available: {available}\n"
                f"Download manually from: https://github.com/{meta['repo']}/releases"
            )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            archive_path = tmp_path / f"{exe}_archive"
            _download_file(download_url, archive_path)
            extracted = _extract_binary(archive_path, binary_name, pattern["fmt"], tmp_path)

            dest = bin_dir / binary_name
            info(f"Installing to {dest}")

            if use_sudo and not is_root():
                run(sudo + ["install", "-m", "755", str(extracted), str(dest)])
            else:
                bin_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(extracted, dest)
                dest.chmod(dest.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

        ok(f"{exe} installed → {dest}")

        # Remind Windows users to add the directory to PATH once
        if OS == "Windows" and not path_reminder_shown:
            path_reminder_shown = True
            warn(
                f"Add the following directory to your PATH so Windows can find these tools:\n"
                f"    {bin_dir}\n"
                f"  (Settings → System → About → Advanced system settings → Environment Variables)"
            )

# ---------------------------------------------------------------------------
# Tool presence check (fallback / final verification)
# ---------------------------------------------------------------------------

def check_tools_presence() -> None:
    title("External tool check")

    all_tools = list(SYSTEM_TOOLS.keys()) + list(GITHUB_TOOLS.keys())
    seen: set[str] = set()
    missing: list[tuple[str, str]] = []

    for exe in all_tools:
        if exe in seen:
            continue
        seen.add(exe)

        path = shutil.which(exe)
        if path:
            ok(f"{exe:20s}  →  {path}")
        else:
            desc = (
                SYSTEM_TOOLS.get(exe, GITHUB_TOOLS.get(exe, {})).get("desc", "")
            )
            missing.append((exe, desc))

    if missing:
        print()
        warn("The following tools were NOT found in PATH:")
        print()

        install_hints = {
            "ffmpeg":         "https://ffmpeg.org/download.html",
            "ffprobe":        "https://ffmpeg.org/download.html  (ships with ffmpeg)",
            "mkvmerge":       "https://mkvtoolnix.download/",
            "mkvextract":     "https://mkvtoolnix.download/  (ships with mkvtoolnix)",
            "mkvinfo":        "https://mkvtoolnix.download/  (ships with mkvtoolnix)",
            "mkvpropedit":    "https://mkvtoolnix.download/  (ships with mkvtoolnix)",
            "mediainfo":      "https://mediaarea.net/en/MediaInfo/Download",
            "dovi_tool":      "https://github.com/quietvoid/dovi_tool/releases",
            "hdr10plus_tool": "https://github.com/quietvoid/hdr10plus_tool/releases",
        }

        max_len = max(len(e) for e, _ in missing)
        for exe, desc in missing:
            hint = install_hints.get(exe, "Install manually")
            print(_c("33", f"    {exe:{max_len}}  —  {desc}"))
            print(_c("2",  f"    {'':>{max_len}}     Download: {hint}"))
            print()

        print(_c("36",
            "  The application will start without these tools, but the\n"
            "  corresponding features will be unavailable or will fail.\n"
        ))
    else:
        ok("All external tools found in PATH")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    default_prefix = str(_default_prefix())
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--no-github",
        action="store_true",
        help="Skip downloading dovi_tool / hdr10plus_tool from GitHub",
    )
    p.add_argument(
        "--prefix",
        default=default_prefix,
        metavar="PATH",
        help=f"Install prefix for GitHub binaries (default: {default_prefix})",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done without executing anything",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    dry_run = args.dry_run
    prefix  = Path(args.prefix)

    print(_c("1;34", """
╔══════════════════════════════════════════╗
║          Mediarecode — Setup             ║
╚══════════════════════════════════════════╝
"""))

    info(f"Python  : {sys.version.split()[0]}")
    info(f"Platform: {OS} / {platform.machine()}")
    if dry_run:
        warn("DRY-RUN mode — no changes will be made")

    # ── Python packages (all platforms) ──────────────────────────────────
    try:
        install_python_packages(dry_run)
    except Exception as e:
        error(f"Python packages: {e}")
        sys.exit(1)

    # ── Platform-specific ─────────────────────────────────────────────────
    if OS == "Linux":
        distro = detect_linux_distro()
        info(f"Distro family: {distro}")

        if distro == "debian":
            try:
                install_apt(dry_run)
            except Exception as e:
                error(f"apt install failed: {e}")
                sys.exit(1)

        elif distro == "fedora":
            try:
                install_dnf(dry_run)
            except Exception as e:
                error(f"dnf install failed: {e}")
                sys.exit(1)

        else:
            warn(
                "Unrecognised Linux distribution — cannot auto-install system packages.\n"
                "   Install manually: ffmpeg  mkvtoolnix  mediainfo"
            )

        if not args.no_github:
            try:
                install_github_tools(prefix, dry_run)
            except Exception as e:
                error(f"GitHub tool installation failed: {e}")
                warn("Install dovi_tool and hdr10plus_tool manually:")
                warn("  → https://github.com/quietvoid/dovi_tool/releases")
                warn("  → https://github.com/quietvoid/hdr10plus_tool/releases")
        else:
            info("Skipping GitHub tools (--no-github)")

        title("Final tool verification")
        check_tools_presence()

    elif OS == "Darwin":
        try:
            install_brew(dry_run)
        except Exception as e:
            error(f"Homebrew install failed: {e}")

        if not args.no_github:
            try:
                install_github_tools(prefix, dry_run)
            except Exception as e:
                error(f"GitHub tool installation failed: {e}")
                warn("Install dovi_tool and hdr10plus_tool manually:")
                warn("  → https://github.com/quietvoid/dovi_tool/releases")
                warn("  → https://github.com/quietvoid/hdr10plus_tool/releases")
        else:
            info("Skipping GitHub tools (--no-github)")

        title("Final tool verification")
        check_tools_presence()

    elif OS == "Windows":
        try:
            install_winget(dry_run)
        except Exception as e:
            error(f"winget install failed: {e}")

        if not args.no_github:
            try:
                install_github_tools(prefix, dry_run)
            except Exception as e:
                error(f"GitHub tool installation failed: {e}")
                warn("Install dovi_tool and hdr10plus_tool manually:")
                warn("  → https://github.com/quietvoid/dovi_tool/releases")
                warn("  → https://github.com/quietvoid/hdr10plus_tool/releases")
        else:
            info("Skipping GitHub tools (--no-github)")

        title("Final tool verification")
        check_tools_presence()

    else:
        warn(f"Unknown platform '{OS}' — skipping system package installation")
        check_tools_presence()

    # ── Done ──────────────────────────────────────────────────────────────
    title("Setup complete")
    ok("Run the application with:")
    print(_c("1;37", "\n    python3 main.py\n"))


if __name__ == "__main__":
    main()
