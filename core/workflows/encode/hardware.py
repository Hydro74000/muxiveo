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
        # VAAPI : nécessite l'init du device (-vaapi_device) et un upload
        #   explicite vers le GPU (format=nv12,hwupload). La taille minimale
        #   acceptée par le driver est >130x130 — on utilise 256x256.
        #
        # AMF / QSV : encode probe avec format=yuv420p (les HW encoders
        #   refusent rgb24 produit par nullsrc). Taille 256x256 par cohérence.

        _NVENC = {"hevc_nvenc", "h264_nvenc"}
        _VAAPI = {"hevc_vaapi", "h264_vaapi"}
        available: set[str] = set()

        nvenc_compiled = compiled & _NVENC
        if nvenc_compiled and self._nvidia_ok():
            available |= nvenc_compiled

        vaapi_device = self._vaapi_device()

        for codec_id in compiled - _NVENC:
            if codec_id in _VAAPI:
                if vaapi_device is None:
                    continue
                cmd = [
                    ffmpeg_bin,
                    "-vaapi_device", vaapi_device,
                    "-f", "lavfi", "-i", "nullsrc=s=256x256:r=1:d=0.04",
                    "-vf", "format=nv12,hwupload",
                    "-frames:v", "1",
                    "-c:v", codec_id,
                    "-f", "null", "-",
                    "-loglevel", "error",
                ]
            else:
                cmd = [
                    ffmpeg_bin,
                    "-f", "lavfi", "-i", "nullsrc=s=256x256:r=1:d=0.04",
                    "-vf", "format=yuv420p",
                    "-frames:v", "1",
                    "-c:v", codec_id,
                    "-f", "null", "-",
                    "-loglevel", "error",
                ]
            probe = subprocess.run(cmd, capture_output=True, check=False)
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

    @staticmethod
    def _vaapi_device() -> str | None:
        """Retourne le chemin du premier device VAAPI disponible, ou None."""
        for i in range(8):
            node = Path(f"/dev/dri/renderD{128 + i}")
            if node.exists():
                return str(node)
        return None
