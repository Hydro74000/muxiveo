"""
core/workflows/encode/runtime/ram_buffer.py — Mesure de la RAM, multiplateforme.

Fonctions publiques (réserve de RAM des encodages vidéo parallèles) :
    total_ram_bytes()      — RAM physique totale (Linux/macOS/Windows)
    available_ram_bytes()  — RAM disponible
    macos_available_ram()  — détail macOS via vm_stat

Conventions :
    - Toutes les fonctions sont pures et thread-safe.
    - `Exception` capturée au niveau de l'appel OS pour ne jamais lever depuis ces helpers.
"""

from __future__ import annotations

import ctypes
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


__all__ = [
    "total_ram_bytes",
    "available_ram_bytes",
    "macos_available_ram",
]
