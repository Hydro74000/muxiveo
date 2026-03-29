#!/usr/bin/env python3
"""
MKV/MP4 Toolkit — Setup & Dependency Installer
===============================================

Installs Python dependencies and external tools required by the application.

  Linux (Debian/Ubuntu)  → pip + apt packages + GitHub binaries
  Linux (Fedora/RHEL)    → pip + dnf packages + GitHub binaries
  Windows / macOS        → pip only, then reports missing external tools

Usage:
    python3 setup.py [--no-github] [--prefix /usr/local] [--dry-run]

Options:
    --no-github     Skip downloading dovi_tool / hdr10plus_tool from GitHub
    --prefix PATH   Installation prefix for GitHub binaries (default: /usr/local)
    --dry-run       Print what would be done without executing
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
MACHINE = platform.machine().lower()  # "x86_64" | "aarch64" | "arm64" ...

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
    # Fallback: check available package managers
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
# format: { "executable": {"apt": "pkg", "dnf": "pkg", "desc": "..."} }
SYSTEM_TOOLS: dict[str, dict] = {
    "ffmpeg": {
        "apt": "ffmpeg",
        "dnf": "ffmpeg",
        "desc": "Video/audio encoder and converter",
        "dnf_note": "Requires RPM Fusion (handled automatically)",
    },
    "ffprobe": {
        "apt": "ffmpeg",       # ships with ffmpeg
        "dnf": "ffmpeg",
        "desc": "Media file analyser (ships with ffmpeg)",
    },
    "mkvmerge": {
        "apt": "mkvtoolnix",
        "dnf": "mkvtoolnix",
        "desc": "MKV container muxer",
    },
    "mkvextract": {
        "apt": "mkvtoolnix",
        "dnf": "mkvtoolnix",
        "desc": "MKV track extractor (ships with mkvtoolnix)",
    },
    "mkvinfo": {
        "apt": "mkvtoolnix",
        "dnf": "mkvtoolnix",
        "desc": "MKV info tool (ships with mkvtoolnix)",
    },
    "mediainfo": {
        "apt": "mediainfo",
        "dnf": "mediainfo",
        "desc": "Media metadata tool",
    },
}

# Tools only available as GitHub release binaries
# format: { "executable": { "repo": "owner/repo", "asset_patterns": {...} } }
GITHUB_TOOLS: dict[str, dict] = {
    "dovi_tool": {
        "repo": "quietvoid/dovi_tool",
        "desc": "Dolby Vision RPU extraction and injection",
        "asset_patterns": {
            "x86_64":  "dovi_tool-x86_64-unknown-linux-gnu.tar.gz",
            "aarch64": "dovi_tool-aarch64-unknown-linux-gnu.tar.gz",
        },
        "binary_name": "dovi_tool",
    },
    "hdr10plus_tool": {
        "repo": "quietvoid/hdr10plus_tool",
        "desc": "HDR10+ metadata extraction and injection",
        "asset_patterns": {
            "x86_64":  "hdr10plus_tool-x86_64-unknown-linux-gnu.tar.gz",
            "aarch64": "hdr10plus_tool-aarch64-unknown-linux-gnu.tar.gz",
        },
        "binary_name": "hdr10plus_tool",
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
        return ["sudo"]   # placeholder only, nothing will actually run
    raise RuntimeError(
        "This step requires root privileges. "
        "Re-run as root or install sudo."
    )

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
# Step 2 — System packages (Linux only)
# ---------------------------------------------------------------------------

def _apt_packages_to_install(dry_run: bool) -> list[str]:
    """Return the list of apt package names that need to be installed."""
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

    # Deduplicate
    pkgs = sorted(set(to_install))
    step(f"Installing: {' '.join(pkgs)}")
    run(
        sudo + ["apt-get", "install", "-y"] + pkgs,
        dry_run=dry_run,
    )
    ok("System packages installed")

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
    # Detect Fedora version
    ver_result = run(
        ["rpm", "-E", "%fedora"],
        dry_run=False, check=False, capture=True,
    )
    fedora_ver = (ver_result.stdout or "").strip() if ver_result else "39"
    if not fedora_ver.isdigit():
        fedora_ver = "39"  # safe fallback

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

    # ffmpeg needs RPM Fusion
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
    run(
        sudo + ["dnf", "install", "-y"] + pkgs,
        dry_run=dry_run,
    )
    ok("System packages installed")

# ---------------------------------------------------------------------------
# Step 3 — GitHub binary tools (Linux only)
# ---------------------------------------------------------------------------

def _github_latest_release(repo: str) -> dict:
    """Fetch latest release metadata from GitHub API."""
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    req = urllib.request.Request(url, headers={"User-Agent": "mkv-toolkit-setup/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.URLError as e:
        raise RuntimeError(f"Cannot reach GitHub API for {repo}: {e}") from e

def _download_file(url: str, dest: Path) -> None:
    """Download url → dest with a simple progress indicator."""
    info(f"Downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "mkv-toolkit-setup/1.0"})
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

def _arch_key() -> str:
    """Map platform.machine() to our asset_patterns key."""
    m = MACHINE
    if m in ("x86_64", "amd64"):
        return "x86_64"
    if m in ("aarch64", "arm64"):
        return "aarch64"
    raise RuntimeError(
        f"Unsupported architecture '{platform.machine()}'. "
        "Install dovi_tool and hdr10plus_tool manually."
    )

def install_github_tools(prefix: Path, dry_run: bool) -> None:
    title("Step 3 — GitHub binary tools (dovi_tool, hdr10plus_tool)")

    bin_dir = prefix / "bin"
    sudo = sudo_prefix(dry_run)
    arch = _arch_key()

    for exe, meta in GITHUB_TOOLS.items():
        if shutil.which(exe):
            ok(f"{exe} already present ({shutil.which(exe)})")
            continue

        if arch not in meta["asset_patterns"]:
            warn(
                f"No pre-built binary for {exe} on {platform.machine()}. "
                "Build from source: https://github.com/" + meta["repo"]
            )
            continue

        step(f"Installing {exe}  ({meta['desc']})")

        # Fetch release info
        info(f"Fetching latest release from github.com/{meta['repo']}")
        if not dry_run:
            release = _github_latest_release(meta["repo"])
            tag = release.get("tag_name", "?")
            info(f"Latest release: {tag}")

            asset_name = meta["asset_patterns"][arch]
            download_url = None
            for asset in release.get("assets", []):
                if asset["name"] == asset_name:
                    download_url = asset["browser_download_url"]
                    break

            if not download_url:
                # Fallback: list available assets for debug
                available = [a["name"] for a in release.get("assets", [])]
                raise RuntimeError(
                    f"Asset '{asset_name}' not found in release {tag}.\n"
                    f"Available: {available}\n"
                    f"Download manually from: https://github.com/{meta['repo']}/releases"
                )

            with tempfile.TemporaryDirectory() as tmp:
                archive_path = Path(tmp) / asset_name
                _download_file(download_url, archive_path)

                # Extract binary from tarball
                with tarfile.open(archive_path, "r:gz") as tar:
                    binary_name = meta["binary_name"]
                    # Find the binary inside the archive (may be nested)
                    binary_member = None
                    for member in tar.getmembers():
                        if Path(member.name).name == binary_name and member.isfile():
                            binary_member = member
                            break
                    if not binary_member:
                        raise RuntimeError(
                            f"Binary '{binary_name}' not found inside {asset_name}"
                        )
                    binary_member.name = binary_name  # flatten path
                    tar.extract(binary_member, path=tmp)

                extracted = Path(tmp) / binary_name
                extracted.chmod(extracted.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

                dest = bin_dir / binary_name
                info(f"Installing to {dest}")
                if not is_root():
                    run(sudo + ["install", "-m", "755", str(extracted), str(dest)])
                else:
                    bin_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(extracted, dest)
                    dest.chmod(dest.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        else:
            info(f"[dry-run] Would download and install {exe} to {bin_dir}/{exe}")

        ok(f"{exe} installed → {bin_dir / exe}")

# ---------------------------------------------------------------------------
# Tool presence check (Windows / macOS)
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
        default="/usr/local",
        metavar="PATH",
        help="Install prefix for GitHub binaries (default: /usr/local)",
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
║       MKV/MP4 Toolkit — Setup            ║
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

        # Final check
        title("Final tool verification")
        check_tools_presence()

    elif OS in ("Windows", "Darwin"):
        label = "Windows" if OS == "Windows" else "macOS"
        title(f"Platform: {label}")
        warn(
            f"Automatic system package installation is not supported on {label}.\n"
            "   Python dependencies have been installed above.\n"
            "   Please install external tools manually (see links below)."
        )
        check_tools_presence()

    else:
        warn(f"Unknown platform '{OS}' — skipping system package installation")
        check_tools_presence()

    # ── Done ──────────────────────────────────────────────────────────────
    title("Setup complete")
    ok("Run the application with:")
    print(_c("1;37", "\n    python3 mkv_toolkit/main.py\n"))


if __name__ == "__main__":
    main()
