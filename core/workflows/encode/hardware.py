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
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from core.subprocess_utils import subprocess_text_kwargs, subprocess_windows_no_window_kwargs
from core.workflows.encode.catalog import (
    AMF_VIDEO_CODECS as _AMF_CODECS,
    HARDWARE_VIDEO_CODECS,
    NVENC_VIDEO_CODECS as _NVENC_CODECS,
    QSV_VIDEO_CODECS as _QSV_CODECS,
    SOFTWARE_VIDEO_CODECS,
    VAAPI_VIDEO_CODECS as _VAAPI_CODECS,
)
from core.workflows.encode.hw_devices import (
    select_linux_hwaccel_device,
    select_windows_hwaccel_device,
)
from core.workflows.encode.runtime.nvencc import (
    NVENCC_VIDEO_CODECS as _NVENCC_CODECS,
    detect_nvencc_available,
)


_NULLSRC = "nullsrc=s=256x256:r=25:d=0.1"   # ≥ 1 frame garantie (25fps × 0.1s)
_GENERIC_HW_FILTER = "format=nv12"
_VAAPI_FILTER = "format=nv12,hwupload"


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

    def __init__(self) -> None:
        # Cache par instance pour éviter les subprocess redondants quand
        # detect() et detect_software() sont appelés à la suite.
        self._cache_lock = threading.RLock()
        self._encoders_output_cache: dict[str, str] = {}
        self._system_ffmpeg_cache: str | None = None
        self._system_ffmpeg_cached = False
        self._vaapi_device_cache: str | None = None
        self._vaapi_device_cached = False
        self._qsv_device_cache: dict[str, str | None] = {}

    @staticmethod
    def _resolve_ffmpeg(ffmpeg_bin: str) -> str:
        """
        Retourne la commande ffmpeg à exécuter sans normaliser les alias.

        En tests (et dans certains environnements packagés), convertir
        systématiquement `ffmpeg` en chemin absolu casse le contrat attendu
        des appels subprocess mockés. On conserve donc l'argument tel qu'il
        est fourni par la configuration.
        """
        return ffmpeg_bin

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
        with self._cache_lock:
            if resolved in self._encoders_output_cache:
                return self._encoders_output_cache[resolved]
        try:
            result = subprocess.run(
                [resolved, "-hide_banner", "-encoders"],
                capture_output=True,
                check=False,
                **subprocess_text_kwargs(),
            )
        except (FileNotFoundError, OSError):
            output = ""
        else:
            output = "\n".join(part for part in (result.stdout, result.stderr) if part)
        with self._cache_lock:
            self._encoders_output_cache[resolved] = output
        return output

    def _system_ffmpeg(self) -> str | None:
        """Retourne le ffmpeg système (AppImage) avec cache par instance."""
        with self._cache_lock:
            if self._system_ffmpeg_cached:
                return self._system_ffmpeg_cache
        detected = self._find_system_ffmpeg()
        with self._cache_lock:
            self._system_ffmpeg_cache = detected
            self._system_ffmpeg_cached = True
        return detected

    def _cached_vaapi_device(self) -> str | None:
        """Retourne le render node VAAPI avec cache par instance."""
        with self._cache_lock:
            if self._vaapi_device_cached:
                return self._vaapi_device_cache
        device = self._vaapi_device()
        with self._cache_lock:
            self._vaapi_device_cache = device
            self._vaapi_device_cached = True
        return device

    def _cached_qsv_device(self, ffmpeg_bin: str) -> str | None:
        """Retourne le device QSV avec cache par instance et binaire ffmpeg."""
        resolved = self._resolve_ffmpeg(ffmpeg_bin)
        with self._cache_lock:
            if resolved in self._qsv_device_cache:
                return self._qsv_device_cache[resolved]
        device = self._qsv_device(ffmpeg_bin)
        with self._cache_lock:
            self._qsv_device_cache[resolved] = device
        return device

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
        system_ff = self._system_ffmpeg()
        if system_ff:
            sys_output = self._get_encoders_output(system_ff)
            if sys_output:
                for codec_id, _ in SOFTWARE_VIDEO_CODECS:
                    if re.search(rf"\b{re.escape(codec_id)}\b", sys_output):
                        available.add(codec_id)

        return available

    def detect(
        self,
        ffmpeg_bin: str = "ffmpeg",
        *,
        nvencc_bin: str | None = None,
    ) -> tuple[set[str], str]:
        """
        Retourne (encodeurs_hw_disponibles, chemin_ffmpeg_utilisé).

        Le chemin retourné est celui du ffmpeg effectivement utilisé pour les
        probes — il peut différer du ffmpeg_bin fourni si ce dernier ne compile
        pas les encodeurs HW (typique dans un AppImage avec ffmpeg statique).
        Ce chemin doit être utilisé pour l'encodage HW effectif.

        Si ``nvencc_bin`` est fourni *et* que NVENC ffmpeg est disponible, on
        ajoute les codecs NVEncC supportés par le GPU (parsing
        ``NVEncC --check-features``). NVEncC n'est jamais exposé sans NVENC.
        """
        ff, compiled = self._compiled_hw(ffmpeg_bin)
        if not compiled:
            return set(), ffmpeg_bin

        resolved = self._resolve_ffmpeg(ff)
        available: set[str] = set()
        nvenc_compiled = compiled & _NVENC_CODECS
        nvenc_available: set[str] = set()

        if nvenc_compiled:
            nvenc_available = self._detect_nvenc(resolved, nvenc_compiled)
            available |= nvenc_available

        available |= self._probe_codecs(resolved, compiled - _NVENC_CODECS)

        if nvencc_bin and nvenc_available:
            _, nvencc_codecs = detect_nvencc_available(nvencc_bin)
            available |= nvencc_codecs & _NVENCC_CODECS

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
        system_ff = self._system_ffmpeg()
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
                **subprocess_windows_no_window_kwargs(),
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return False
        return probe.returncode == 0

    def _detect_nvenc(
        self,
        ffmpeg_bin: str,
        compiled: set[str],
    ) -> set[str]:
        """
        Garde la logique historique Linux/macOS pour NVIDIA.

        Sous Linux (containers, distrobox, Flatpak), FFmpeg peut echouer a
        charger `libcuda.so.1` pendant le probe alors que le GPU NVIDIA est bien
        accessible. Dans ces environnements, `nvidia-smi` ou `/dev/nvidia0`
        restent donc le signal prioritaire. Sous Windows, on conserve un probe
        FFmpeg reel pour valider NVENC.
        """
        # Les tests et certains contextes appellent explicitement un binaire
        # Windows (.exe) depuis un host non-win32 ; on force alors la logique
        # "probe FFmpeg réel" sans dépendre de nvidia-smi.
        if sys.platform == "win32" or ffmpeg_bin.lower().endswith(".exe"):
            return self._probe_codecs(ffmpeg_bin, compiled)

        if self._nvidia_ok():
            return set(compiled)

        return self._probe_codecs(ffmpeg_bin, compiled)

    def _probe_codecs(
        self,
        ffmpeg_bin: str,
        codec_ids: set[str],
    ) -> set[str]:
        """Probe une liste de codecs et retourne ceux qui sont utilisables."""
        jobs: list[tuple[str, list[str]]] = []
        for codec_id in codec_ids:
            cmd = self._probe_command(ffmpeg_bin, codec_id)
            if cmd is None:
                continue
            jobs.append((codec_id, cmd))

        available: set[str] = set()
        if not jobs:
            return available

        if len(jobs) == 1:
            codec_id, cmd = jobs[0]
            if self._probe_encoder(cmd):
                available.add(codec_id)
            return available

        # Les probes sont indépendants ; on les lance en parallèle pour réduire
        # le temps mural quand plusieurs codecs HW sont compilés.
        max_workers = min(4, len(jobs))
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="hw-probe") as pool:
            futures = {pool.submit(self._probe_encoder, cmd): codec_id for codec_id, cmd in jobs}
            for future in as_completed(futures):
                codec_id = futures[future]
                try:
                    ok = future.result()
                except Exception:
                    ok = False
                if ok:
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
                **subprocess_windows_no_window_kwargs(),
            )
            if result.returncode == 0:
                return True
            return False
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return Path("/dev/nvidia0").exists()

    def _probe_command(
        self,
        ffmpeg_bin: str,
        codec_id: str,
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
            vaapi_device = self._cached_vaapi_device()
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

        if codec_id in _QSV_CODECS:
            qsv_device = self._cached_qsv_device(ffmpeg_bin)
            if qsv_device is None:
                return None
            return [
                *base_cmd,
                "-qsv_device", qsv_device,
                "-f", "lavfi", "-i", _NULLSRC,
                "-vf", _GENERIC_HW_FILTER,
                "-frames:v", "1",
                "-c:v", codec_id,
                "-f", "null", "-",
            ]

        if codec_id in _AMF_CODECS and self._is_windows_runtime(ffmpeg_bin):
            amf_device = self._amf_device(ffmpeg_bin, codec_id)
            if amf_device is None:
                return None
            device_name = f"mre_amf_probe{amf_device}"
            return [
                *base_cmd,
                "-init_hw_device", f"d3d11va={device_name}:{amf_device}",
                "-filter_hw_device", device_name,
                "-f", "lavfi", "-i", _NULLSRC,
                "-vf", _VAAPI_FILTER,
                "-frames:v", "1",
                "-c:v", codec_id,
                "-f", "null", "-",
            ]

        if codec_id in _NVENC_CODECS and self._is_windows_runtime(ffmpeg_bin):
            nvenc_device = self._nvenc_device(ffmpeg_bin, codec_id)
            if nvenc_device is None:
                return None
            return [
                *base_cmd,
                "-f", "lavfi", "-i", _NULLSRC,
                "-vf", _GENERIC_HW_FILTER,
                "-frames:v", "1",
                "-c:v", codec_id,
                "-gpu", nvenc_device,
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
    def _is_windows_runtime(ffmpeg_bin: str) -> bool:
        return sys.platform == "win32" or ffmpeg_bin.lower().endswith(".exe")

    def _vaapi_device(self) -> str | None:
        """Retourne le render node Linux ciblé pour VAAPI, ou None."""
        return select_linux_hwaccel_device("hevc_vaapi")

    def _qsv_device(self, ffmpeg_bin: str, codec_id: str = "hevc_qsv") -> str | None:
        """Retourne le device QSV ciblé selon l'OS, ou None."""
        if self._is_windows_runtime(ffmpeg_bin):
            return select_windows_hwaccel_device(codec_id, ffmpeg_bin=self._resolve_ffmpeg(ffmpeg_bin))
        return select_linux_hwaccel_device("hevc_qsv")

    def _amf_device(self, ffmpeg_bin: str, codec_id: str = "hevc_amf") -> str | None:
        if not self._is_windows_runtime(ffmpeg_bin):
            return None
        return select_windows_hwaccel_device(codec_id, ffmpeg_bin=self._resolve_ffmpeg(ffmpeg_bin))

    def _nvenc_device(self, ffmpeg_bin: str, codec_id: str = "hevc_nvenc") -> str | None:
        if not self._is_windows_runtime(ffmpeg_bin):
            return None
        return select_windows_hwaccel_device(codec_id, ffmpeg_bin=self._resolve_ffmpeg(ffmpeg_bin))
