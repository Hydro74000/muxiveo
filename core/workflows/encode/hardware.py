"""
core/workflows/encode/hardware.py - Runtime detection of available hardware encoders.

Public:
    HardwareEncoderDetector
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from core.subprocess_utils import subprocess_text_kwargs
from core.workflows.encode.models import HARDWARE_VIDEO_CODECS, SOFTWARE_VIDEO_CODECS


_NULLSRC = "nullsrc=s=256x256:r=25:d=0.1"   # ≥ 1 frame garantie (25fps × 0.1s)
_GENERIC_HW_FILTER = "format=nv12"
_VAAPI_FILTER = "format=nv12,hwupload"
_NVENC_CODECS = {"hevc_nvenc", "h264_nvenc", "av1_nvenc"}
_VAAPI_CODECS = {"hevc_vaapi", "h264_vaapi", "av1_vaapi"}


class HardwareEncoderDetector:
    """
    Detecte les encodeurs materiels reellement utilisables a l'execution.

    Un encodeur peut etre compile dans ffmpeg (visible dans `-encoders`) mais
    inutilisable si le driver GPU ou le runtime associe est absent. On probe
    donc chaque encodeur avec une frame nulle pour confirmer sa disponibilite.

    Dans un AppImage, le ffmpeg embarqué peut ne pas inclure les encodeurs
    materiels (ex: johnvansickle static build). Dans ce cas, le ffmpeg systeme
    est utilise automatiquement pour la detection et l'encodage HW.

    Synchrone et thread-safe - a executer dans un ThreadPoolExecutor.
    """

    @staticmethod
    def _resolve_ffmpeg(ffmpeg_bin: str) -> str:
        """Résout le chemin absolu de ffmpeg (critique dans AppImage/PyInstaller)."""
        import shutil
        return shutil.which(ffmpeg_bin) or ffmpeg_bin

    @staticmethod
    def _find_system_ffmpeg() -> str | None:
        """
        Dans un AppImage, retourne le ffmpeg système en ignorant les répertoires
        embarqués dans l'AppImage ($APPDIR).

        Le ffmpeg système a potentiellement des encodeurs HW (NVENC, VAAPI, QSV)
        que le ffmpeg statique embarqué ne compile pas.

        Hors AppImage (dev, Windows, macOS), retourne None car le ffmpeg fourni
        est déjà le bon.
        """
        import shutil
        appdir = os.environ.get("APPDIR", "")
        if not appdir:
            return None
        path_dirs = os.environ.get("PATH", "").split(os.pathsep)
        filtered = os.pathsep.join(
            d for d in path_dirs if d and not d.startswith(appdir)
        )
        return shutil.which("ffmpeg", path=filtered) or None

    def _get_encoders_output(self, ffmpeg_bin: str) -> str:
        """Retourne la sortie de `ffmpeg -encoders`, chaîne vide si échec."""
        resolved = self._resolve_ffmpeg(ffmpeg_bin)
        try:
            result = subprocess.run(
                [resolved, "-hide_banner", "-encoders"],
                capture_output=True,
                check=False,
                **subprocess_text_kwargs(),
            )
        except (FileNotFoundError, OSError):
            return ""
        return "\n".join(part for part in (result.stdout, result.stderr) if part)

    def detect_software(self, ffmpeg_bin: str = "ffmpeg") -> set[str]:
        """
        Retourne les codecs SOFTWARE disponibles.

        Vérifie le ffmpeg fourni (généralement le ffmpeg embarqué) ET le ffmpeg
        système (dans un AppImage, le ffmpeg statique peut manquer libsvtav1).
        Retourne l'union des codecs trouvés.
        """
        available: set[str] = set()

        # Codecs du ffmpeg principal
        output = self._get_encoders_output(ffmpeg_bin)
        if output:
            for codec_id, _ in SOFTWARE_VIDEO_CODECS:
                if re.search(rf"\b{re.escape(codec_id)}\b", output):
                    available.add(codec_id)

        # Dans un AppImage : complète avec le ffmpeg système si différent
        system_ff = self._find_system_ffmpeg()
        if system_ff:
            sys_output = self._get_encoders_output(system_ff)
            if sys_output:
                for codec_id, _ in SOFTWARE_VIDEO_CODECS:
                    if re.search(rf"\b{re.escape(codec_id)}\b", sys_output):
                        available.add(codec_id)

        return available

    def detect(self, ffmpeg_bin: str = "ffmpeg") -> tuple[set[str], str]:
        """
        Retourne (encodeurs_hw_disponibles, chemin_ffmpeg_utilisé).

        Le chemin retourné est celui du ffmpeg effectivement utilisé pour les
        probes — il peut différer du ffmpeg_bin fourni si ce dernier ne compile
        pas les encodeurs HW (typique dans un AppImage avec ffmpeg statique).
        Ce chemin doit être utilisé pour l'encodage HW effectif.
        """
        ff, compiled = self._compiled_hw(ffmpeg_bin)
        if not compiled:
            return set(), ffmpeg_bin

        resolved = self._resolve_ffmpeg(ff)
        vaapi_device = self._vaapi_device()
        available: set[str] = set()
        nvenc_compiled = compiled & _NVENC_CODECS

        if nvenc_compiled:
            available |= self._detect_nvenc(resolved, nvenc_compiled, vaapi_device)

        for codec_id in compiled - _NVENC_CODECS:
            cmd = self._probe_command(resolved, codec_id, vaapi_device)
            if cmd is None:
                continue
            if self._probe_encoder(cmd):
                available.add(codec_id)

        return available, ff

    def _compiled_hw(self, ffmpeg_bin: str) -> tuple[str, set[str]]:
        """
        Retourne (chemin_ffmpeg, codecs_HW_compilés).

        Si le ffmpeg fourni ne compile aucun codec HW et qu'on est dans un
        AppImage, essaie le ffmpeg système comme fallback.
        """
        output = self._get_encoders_output(ffmpeg_bin)
        compiled = self._parse_hw_codecs(output)
        if compiled:
            return ffmpeg_bin, compiled

        # Fallback : ffmpeg système (AppImage uniquement)
        system_ff = self._find_system_ffmpeg()
        if system_ff:
            sys_output = self._get_encoders_output(system_ff)
            sys_compiled = self._parse_hw_codecs(sys_output)
            if sys_compiled:
                return system_ff, sys_compiled

        return ffmpeg_bin, set()

    @staticmethod
    def _parse_hw_codecs(encoders_output: str) -> set[str]:
        if not encoders_output:
            return set()
        return {
            codec_id
            for codec_id, _ in HARDWARE_VIDEO_CODECS
            if re.search(rf"\b{re.escape(codec_id)}\b", encoders_output)
        }

    @staticmethod
    def _probe_encoder(cmd: list[str]) -> bool:
        """Retourne True si la commande de probe FFmpeg aboutit."""
        try:
            probe = subprocess.run(
                cmd,
                capture_output=True,
                check=False,
                timeout=5,
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
