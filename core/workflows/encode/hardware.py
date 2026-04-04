"""
core/workflows/encode/hardware.py - Runtime detection of available hardware encoders.

Public:
    HardwareEncoderDetector
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from core.subprocess_utils import subprocess_text_kwargs
from core.workflows.encode.models import HARDWARE_VIDEO_CODECS


_NULLSRC = "nullsrc=s=256x256:r=1:d=0.04"
_GENERIC_HW_FILTER = "format=nv12"
_VAAPI_FILTER = "format=nv12,hwupload"
_NVENC_CODECS = {"hevc_nvenc", "h264_nvenc"}
_VAAPI_CODECS = {"hevc_vaapi", "h264_vaapi"}


class HardwareEncoderDetector:
    """
    Detecte les encodeurs materiels reellement utilisables a l'execution.

    Un encodeur peut etre compile dans ffmpeg (visible dans `-encoders`) mais
    inutilisable si le driver GPU ou le runtime associe est absent. On probe
    donc chaque encodeur avec une frame nulle pour confirmer sa disponibilite.

    Synchrone et thread-safe - a executer dans un ThreadPoolExecutor.
    """

    def detect(self, ffmpeg_bin: str = "ffmpeg") -> set[str]:
        """Retourne l'ensemble des identifiants d'encodeurs materiels disponibles."""
        try:
            result = subprocess.run(
                [ffmpeg_bin, "-hide_banner", "-encoders"],
                capture_output=True,
                check=False,
                **subprocess_text_kwargs(),
            )
        except FileNotFoundError:
            return set()

        encoders_output = "\n".join(
            part for part in (result.stdout, result.stderr) if part
        )
        compiled = {
            codec_id
            for codec_id, _ in HARDWARE_VIDEO_CODECS
            if re.search(rf"\b{re.escape(codec_id)}\b", encoders_output)
        }
        if not compiled:
            return set()

        vaapi_device = self._vaapi_device()
        available: set[str] = set()
        nvenc_compiled = compiled & _NVENC_CODECS

        if nvenc_compiled:
            available |= self._detect_nvenc(ffmpeg_bin, nvenc_compiled, vaapi_device)

        for codec_id in compiled - _NVENC_CODECS:
            cmd = self._probe_command(ffmpeg_bin, codec_id, vaapi_device)
            if cmd is None:
                continue
            if self._probe_encoder(cmd):
                available.add(codec_id)

        return available

    @staticmethod
    def _probe_encoder(cmd: list[str]) -> bool:
        """Retourne True si la commande de probe FFmpeg aboutit."""
        try:
            probe = subprocess.run(
                cmd,
                capture_output=True,
                check=False,
                timeout=15,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return False
        return probe.returncode == 0

    def _detect_nvenc(
        self,
        ffmpeg_bin: str,
        compiled: set[str],
        vaapi_device: str | None,
    ) -> set[str]:
        """
        Garde la logique historique Linux/macOS pour NVIDIA.

        Sous Linux (containers, distrobox, Flatpak), FFmpeg peut echouer a
        charger `libcuda.so.1` pendant le probe alors que le GPU NVIDIA est bien
        accessible. Dans ces environnements, `nvidia-smi` ou `/dev/nvidia0`
        restent donc le signal prioritaire. Sous Windows, on conserve un probe
        FFmpeg reel pour valider NVENC.
        """
        if sys.platform == "win32":
            return self._probe_codecs(ffmpeg_bin, compiled, vaapi_device)

        if self._nvidia_ok():
            return set(compiled)

        return self._probe_codecs(ffmpeg_bin, compiled, vaapi_device)

    def _probe_codecs(
        self,
        ffmpeg_bin: str,
        codec_ids: set[str],
        vaapi_device: str | None,
    ) -> set[str]:
        """Probe une liste de codecs et retourne ceux qui sont utilisables."""
        available: set[str] = set()
        for codec_id in codec_ids:
            cmd = self._probe_command(ffmpeg_bin, codec_id, vaapi_device)
            if cmd is None:
                continue
            if self._probe_encoder(cmd):
                available.add(codec_id)
        return available

    @staticmethod
    def _nvidia_ok() -> bool:
        """Retourne True si un GPU NVIDIA est accessible via nvidia-smi ou /dev/nvidia0."""
        try:
            result = subprocess.run(
                ["nvidia-smi", "-L"],
                capture_output=True,
                check=False,
                timeout=5,
            )
            if result.returncode == 0:
                return True
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass
        return Path("/dev/nvidia0").exists()

    def _probe_command(
        self,
        ffmpeg_bin: str,
        codec_id: str,
        vaapi_device: str | None,
    ) -> list[str] | None:
        """
        Construit la commande de probe adaptee a la famille d'encodeur.

        VAAPI reste un cas special car il faut initialiser explicitement le
        device et uploader la frame de test vers le GPU. Les autres encodeurs
        materiels (NVENC, AMF, QSV) sont verifies avec un probe FFmpeg reel.
        """
        base_cmd = [
            ffmpeg_bin,
            "-hide_banner",
            "-loglevel", "error",
        ]

        if codec_id in _VAAPI_CODECS:
            if vaapi_device is None:
                return None
            return [
                *base_cmd,
                "-vaapi_device", vaapi_device,
                "-f", "lavfi", "-i", _NULLSRC,
                "-vf", _VAAPI_FILTER,
                "-frames:v", "1",
                "-c:v", codec_id,
                "-f", "null", "-",
            ]

        return [
            *base_cmd,
            "-f", "lavfi", "-i", _NULLSRC,
            "-vf", _GENERIC_HW_FILTER,
            "-frames:v", "1",
            "-c:v", codec_id,
            "-f", "null", "-",
        ]

    @staticmethod
    def _vaapi_device() -> str | None:
        """Retourne le chemin du premier device VAAPI disponible, ou None."""
        for i in range(8):
            node = Path(f"/dev/dri/renderD{128 + i}")
            if node.exists():
                return str(node)
        return None
