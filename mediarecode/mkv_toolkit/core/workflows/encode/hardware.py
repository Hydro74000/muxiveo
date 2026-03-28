"""
core/workflows/encode/hardware.py — Runtime detection of available hardware encoders.

Public:
    HardwareEncoderDetector
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from core.workflows.encode.models import HARDWARE_VIDEO_CODECS


class HardwareEncoderDetector:
    """
    Détecte les encodeurs matériels réellement utilisables à l'exécution.

    Un encodeur peut être compilé dans ffmpeg (visible dans `-encoders`) mais
    inutilisable si le driver GPU est absent (ex: libcuda.so.1 manquant pour NVENC).
    On probe chaque encodeur avec une frame nulle pour confirmer sa disponibilité.

    Synchrone et thread-safe — à exécuter dans un ThreadPoolExecutor.
    """

    def detect(self, ffmpeg_bin: str = "ffmpeg") -> set[str]:
        """Retourne l'ensemble des identifiants d'encodeurs matériels disponibles."""
        # Étape 1 : filtre rapide — encodeurs compilés dans ce ffmpeg
        try:
            result = subprocess.run(
                [ffmpeg_bin, "-encoders"],
                capture_output=True,
                text=True,
                check=False,
            )
            compiled = {
                codec_id
                for codec_id, _ in HARDWARE_VIDEO_CODECS
                if re.search(rf"\b{re.escape(codec_id)}\b", result.stdout)
            }
        except FileNotFoundError:
            return set()

        if not compiled:
            return set()

        # Étape 2 : probe runtime selon la famille d'encodeur
        #
        # NVENC : on passe par nvidia-smi plutôt qu'un encode test.
        #   ffmpeg charge libcuda.so.1 dynamiquement ; dans un container
        #   (distrobox / Flatpak) la lib n'est souvent pas dans LD_LIBRARY_PATH
        #   même si le GPU est accessible. nvidia-smi communique directement
        #   avec le kernel driver (/dev/nvidiactl) sans dépendance CUDA.
        #
        # AMF / QSV : encode probe avec format=yuv420p (les HW encoders
        #   refusent rgb24 produit par nullsrc).

        _NVENC = {"hevc_nvenc", "h264_nvenc"}
        available: set[str] = set()

        nvenc_compiled = compiled & _NVENC
        if nvenc_compiled and self._nvidia_ok():
            available |= nvenc_compiled

        for codec_id in compiled - _NVENC:
            probe = subprocess.run(
                [
                    ffmpeg_bin,
                    "-f", "lavfi", "-i", "nullsrc=s=64x64:r=1:d=0.04",
                    "-vf", "format=yuv420p",
                    "-frames:v", "1",
                    "-c:v", codec_id,
                    "-f", "null", "-",
                    "-loglevel", "error",
                ],
                capture_output=True,
                check=False,
            )
            if probe.returncode == 0:
                available.add(codec_id)

        return available

    @staticmethod
    def _nvidia_ok() -> bool:
        """Retourne True si un GPU NVIDIA est accessible (via nvidia-smi ou /dev/nvidia0)."""
        try:
            r = subprocess.run(
                ["nvidia-smi", "-L"],
                capture_output=True,
                check=False,
                timeout=5,
            )
            if r.returncode == 0:
                return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        # Fallback : présence du device node kernel
        return Path("/dev/nvidia0").exists()
