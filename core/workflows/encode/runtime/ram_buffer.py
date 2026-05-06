"""
core/workflows/encode/runtime/ram_buffer.py — Helpers RAM/SHM cross-platform.

Fonctions publiques :
    total_ram_bytes()      — RAM physique totale (Linux/macOS/Windows)
    available_ram_bytes()  — RAM disponible
    macos_available_ram()  — détail macOS via vm_stat
    ram_buffer_dir()       — répertoire RAM-backed (/dev/shm) ou None
    shm_path()             — résout un chemin RAM ou disque selon seuils

Conventions :
    - Toutes les fonctions sont pures et thread-safe.
    - Aucun état partagé : `shm_path` reçoit explicitement les paramètres de configuration.
    - `Exception` capturée au niveau de l'appel OS pour ne jamais lever depuis ces helpers.
"""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
import sys
from pathlib import Path

from core.subprocess_utils import subprocess_text_kwargs


def total_ram_bytes() -> int:
    """
    RAM physique totale en octets.
    Linux : /proc/meminfo · macOS : sysctl hw.memsize · Windows : GlobalMemoryStatusEx.
    Retourne 0 si la valeur ne peut pas être lue.
    """
    try:
        if sys.platform == "linux":
            text = Path("/proc/meminfo").read_text(encoding="ascii")
            m = re.search(r"MemTotal:\s+(\d+)\s+kB", text)
            return int(m.group(1)) * 1024 if m else 0
        if sys.platform == "darwin":
            r = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True, check=False, timeout=5, **subprocess_text_kwargs(),
            )
            v = r.stdout.strip()
            return int(v) if r.returncode == 0 and v.isdigit() else 0
        if sys.platform == "win32":
            return _win_mem_status().ullTotalPhys
    except Exception:
        pass
    return 0


def available_ram_bytes() -> int:
    """RAM disponible en octets, équivalent MemAvailable sur Linux. 0 si non déterminable."""
    try:
        if sys.platform == "linux":
            text = Path("/proc/meminfo").read_text(encoding="ascii")
            m = re.search(r"MemAvailable:\s+(\d+)\s+kB", text)
            return int(m.group(1)) * 1024 if m else 0
        if sys.platform == "darwin":
            return macos_available_ram()
        if sys.platform == "win32":
            return _win_mem_status().ullAvailPhys
    except Exception:
        pass
    return 0


def macos_available_ram() -> int:
    """RAM disponible sur macOS via vm_stat (free + inactive + speculative + purgeable)."""
    r = subprocess.run(
        ["vm_stat"], capture_output=True, check=False, timeout=5, **subprocess_text_kwargs()
    )
    if r.returncode != 0:
        return 0
    page_m = re.search(r"page size of (\d+) bytes", r.stdout)
    page = int(page_m.group(1)) if page_m else 4096
    pages = 0
    for field in ("Pages free", "Pages inactive", "Pages speculative", "Pages purgeable"):
        m = re.search(rf"{re.escape(field)}:\s*(\d+)", r.stdout)
        if m:
            pages += int(m.group(1))
    return pages * page


def _win_mem_status():
    """Structure MEMORYSTATUSEX remplie (Windows uniquement)."""
    class _MEMSTATEX(ctypes.Structure):
        _fields_ = [
            ("dwLength",                ctypes.c_ulong),
            ("dwMemoryLoad",            ctypes.c_ulong),
            ("ullTotalPhys",            ctypes.c_ulonglong),
            ("ullAvailPhys",            ctypes.c_ulonglong),
            ("ullTotalPageFile",        ctypes.c_ulonglong),
            ("ullAvailPageFile",        ctypes.c_ulonglong),
            ("ullTotalVirtual",         ctypes.c_ulonglong),
            ("ullAvailVirtual",         ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]
    stat = _MEMSTATEX()
    stat.dwLength = ctypes.sizeof(stat)
    windll = getattr(ctypes, "windll")
    windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
    return stat


def ram_buffer_dir() -> Path | None:
    """
    Répertoire RAM-backed disponible, ou None.

    · Linux  : /dev/shm (tmpfs kernel)
    · macOS  : /dev/shm (writable sur macOS ≥ 10.15)
    · Windows: aucun équivalent standard → None
    """
    if sys.platform in ("linux", "darwin"):
        shm = Path("/dev/shm")
        if shm.is_dir() and os.access(shm, os.W_OK):
            return shm
    return None


def shm_path(
    tmp: Path,
    name: str,
    file_size: int,
    *,
    enabled: bool,
    threshold_pct: int,
) -> Path:
    """
    Résout un chemin RAM si les conditions sont réunies, sinon un chemin dans `tmp`.

    Conditions cumulatives :
      1. `enabled` True
      2. Un répertoire RAM existe (ram_buffer_dir())
      3. RAM disponible après chargement ≥ threshold_pct % de la RAM totale
         formule : available - file_size ≥ total × threshold_pct / 100
    """
    if not enabled:
        return tmp / name
    ram_dir = ram_buffer_dir()
    if ram_dir is None:
        return tmp / name
    total = total_ram_bytes()
    available = available_ram_bytes()
    if total <= 0 or available <= 0:
        return tmp / name
    min_free_after = int(total * threshold_pct / 100)
    if available - file_size >= min_free_after:
        return ram_dir / name
    return tmp / name


__all__ = [
    "total_ram_bytes",
    "available_ram_bytes",
    "macos_available_ram",
    "ram_buffer_dir",
    "shm_path",
]
