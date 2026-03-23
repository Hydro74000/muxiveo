"""
core/workflows/encode.py — Workflow d'encodage vidéo/audio via ffmpeg.

Classes publiques :
    VideoEncodeSettings  — paramètres d'encodage vidéo (codec, qualité, HDR)
    AudioTrackSettings   — paramètres par piste audio
    EncodeConfig         — configuration complète d'un encodage
    EncodePreset         — profil sauvegardable en JSON
    HardwareEncoderDetector — détecte les encodeurs matériels disponibles à l'exécution (probe runtime)
    ProfileManager       — sauvegarde/charge les profils JSON
    EncodeWorkflow       — construit et exécute la commande ffmpeg
    EncodeError          — exception levée par le workflow

Conventions :
    - Jamais shell=True
    - pathlib.Path pour tous les chemins
    - ffmpeg uniquement (pas de mkvmerge)
    - Signaux Qt thread-safe pour la communication vers l'UI
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
import shutil
import tempfile
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from core.runner import TaskCancelledError, TaskSignals, ToolRunner


# =============================================================================
# Enums et constantes
# =============================================================================

class QualityMode(str, Enum):
    CRF     = "crf"
    BITRATE = "bitrate"
    SIZE    = "size"

    def label(self) -> str:
        return {"crf": "CRF", "bitrate": "Débit (kbps)", "size": "Taille cible (Mo)"}[self.value]


SOFTWARE_VIDEO_CODECS: list[tuple[str, str]] = [
    ("libx265",   "x265 — HEVC (logiciel)"),
    ("libx264",   "x264 — H.264 (logiciel)"),
    ("libsvtav1", "SVT-AV1 (logiciel)"),
]

HARDWARE_VIDEO_CODECS: list[tuple[str, str]] = [
    ("hevc_nvenc", "NVENC — HEVC (NVIDIA)"),
    ("hevc_amf",   "AMF — HEVC (AMD)"),
    ("hevc_qsv",   "QSV — HEVC (Intel)"),
    ("h264_nvenc", "NVENC — H.264 (NVIDIA)"),
    ("h264_amf",   "AMF — H.264 (AMD)"),
    ("h264_qsv",   "QSV — H.264 (Intel)"),
]

AUDIO_CODECS: list[tuple[str, str]] = [
    ("copy",  "Copie (sans réencodage)"),
    ("aac",   "AAC"),
    ("eac3",  "EAC-3 (Dolby Digital+)"),
    ("flac",  "FLAC (sans perte)"),
]

X265_PRESETS   = ["ultrafast", "superfast", "veryfast", "faster", "fast",
                  "medium", "slow", "slower", "veryslow", "placebo"]
X264_PRESETS   = X265_PRESETS
SVTAV1_PRESETS = [str(i) for i in range(13)]   # 0 = qualité max, 12 = vitesse max
NVENC_PRESETS  = ["p1", "p2", "p3", "p4", "p5", "p6", "p7",
                  "slow", "medium", "fast", "hp", "hq"]

TONEMAP_ALGORITHMS = ["hable", "mobius", "reinhard", "gamma", "linear", "clip"]


def presets_for_codec(codec: str) -> list[str]:
    """Retourne la liste de presets appropriée pour le codec donné."""
    if codec == "libsvtav1":
        return SVTAV1_PRESETS
    if codec in ("hevc_nvenc", "h264_nvenc"):
        return NVENC_PRESETS
    if codec in ("hevc_amf", "hevc_qsv", "h264_amf", "h264_qsv"):
        return []   # pas de preset standardisé
    return X265_PRESETS   # libx265, libx264


# =============================================================================
# Dataclasses
# =============================================================================

@dataclass
class VideoEncodeSettings:
    """Paramètres d'encodage vidéo."""
    codec:            str          = "libx265"
    quality_mode:     QualityMode  = QualityMode.CRF
    crf:              int          = 18
    bitrate_kbps:     int          = 5000
    target_size_mb:   int          = 4000
    preset:           str          = "slow"
    extra_params:     str          = ""    # x265-params / svtav1-params passthrough
    # HDR statique
    inject_hdr_meta:  bool         = False
    master_display:   str          = ""   # ex. "G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(40000000,50)"
    max_cll:          str          = ""   # ex. "1000,400"
    # Tone mapping
    tonemap_to_sdr:   bool         = False
    tonemap_algorithm: str         = "hable"


@dataclass
class AudioTrackSettings:
    """Paramètres d'encodage pour une piste audio."""
    stream_index:        int         # index global ffprobe
    codec:               str = "copy"
    bitrate_kbps:        int = 384
    extract_truehd_core: bool = False   # strip Atmos via BSF truehd_core


@dataclass
class EncodeConfig:
    """Configuration complète d'un encodage."""
    source:           Path
    output:           Path
    video:            VideoEncodeSettings
    audio_tracks:     list[AudioTrackSettings]
    copy_subtitles:   bool         = True
    duration_s:       float | None = None   # requis pour le mode taille cible
    # Passthrough métadonnées dynamiques (HEVC uniquement)
    copy_dv:          bool         = False  # injecter RPU Dolby Vision via dovi_tool
    copy_hdr10plus:   bool         = False  # injecter HDR10+ SEI via hdr10plus_tool
    dovi_profile:     str          = "0"    # flag -m dovi_tool : "0"=conserver, "2"=normaliser P8.1
    work_dir:         Path | None  = None   # dossier de travail (passlog, fichiers temp)


@dataclass
class EncodePreset:
    """Profil d'encodage sauvegardable en JSON."""
    name:                       str  = "Nouveau profil"
    description:                str  = ""
    codec:                      str  = "libx265"
    quality_mode:               str  = QualityMode.CRF.value
    crf:                        int  = 18
    bitrate_kbps:               int  = 5000
    target_size_mb:             int  = 4000
    preset:                     str  = "slow"
    extra_params:               str  = ""
    inject_hdr_meta:            bool = False
    master_display:             str  = ""
    max_cll:                    str  = ""
    tonemap_to_sdr:             bool = False
    tonemap_algorithm:          str  = "hable"
    default_audio_codec:        str  = "copy"
    default_audio_bitrate_kbps: int  = 384

    def to_video_settings(self) -> VideoEncodeSettings:
        return VideoEncodeSettings(
            codec=self.codec,
            quality_mode=QualityMode(self.quality_mode),
            crf=self.crf,
            bitrate_kbps=self.bitrate_kbps,
            target_size_mb=self.target_size_mb,
            preset=self.preset,
            extra_params=self.extra_params,
            inject_hdr_meta=self.inject_hdr_meta,
            master_display=self.master_display,
            max_cll=self.max_cll,
            tonemap_to_sdr=self.tonemap_to_sdr,
            tonemap_algorithm=self.tonemap_algorithm,
        )


# =============================================================================
# Détection des encodeurs matériels
# =============================================================================

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


# =============================================================================
# Gestionnaire de profils JSON
# =============================================================================

class ProfileManager:
    """
    Sauvegarde et charge les profils EncodePreset en JSON.

    Dossier : <app_data_dir>/encode_profiles/
    """

    _FIELDS = EncodePreset.__dataclass_fields__

    def __init__(self, profiles_dir: Path) -> None:
        self._dir = profiles_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    def save(self, preset: EncodePreset) -> None:
        safe = re.sub(r"[^\w\-]", "_", preset.name)
        path = self._dir / f"{safe}.json"
        data = {f: getattr(preset, f) for f in self._FIELDS}
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def load_all(self) -> list[EncodePreset]:
        presets: list[EncodePreset] = []
        for f in sorted(self._dir.glob("*.json")):
            try:
                raw = json.loads(f.read_text(encoding="utf-8"))
                presets.append(EncodePreset(**{k: v for k, v in raw.items() if k in self._FIELDS}))
            except Exception:
                pass
        return presets

    def delete(self, name: str) -> None:
        safe = re.sub(r"[^\w\-]", "_", name)
        (self._dir / f"{safe}.json").unlink(missing_ok=True)

    def names(self) -> list[str]:
        return [p.name for p in self.load_all()]


# =============================================================================
# Exception
# =============================================================================

class EncodeError(RuntimeError):
    """Erreur levée lors de la validation ou de l'exécution d'un encodage."""


# =============================================================================
# Workflow
# =============================================================================

class EncodeWorkflow(QObject):
    """
    Construit et exécute un encodage ffmpeg.

    Usage :
        wf = EncodeWorkflow(ffmpeg_bin="ffmpeg")
        cmd  = wf.build_command_single(config)   # list[str] — aperçu
        cmds = wf.build_command(config)           # list[str] ou list[list[str]]
        errors = wf.validate(config)
        signals = wf.run(config)

    Signaux :
        log_message(level, message)
    """

    log_message = Signal(str, str)

    def __init__(
        self,
        ffmpeg_bin:                str  = "ffmpeg",
        dovi_tool_bin:             str  = "dovi_tool",
        hdr10plus_bin:             str  = "hdr10plus_tool",
        mkvmerge_bin:              str  = "mkvmerge",
        ram_buffer_enabled:        bool = True,
        ram_buffer_threshold_pct:  int  = 15,
        parent: QObject | None         = None,
    ) -> None:
        super().__init__(parent)
        self._ffmpeg = ffmpeg_bin
        self._bins: dict[str, str] = {
            "dovi_tool":      dovi_tool_bin,
            "hdr10plus_tool": hdr10plus_bin,
            "mkvmerge":       mkvmerge_bin,
        }
        self._runner = ToolRunner(max_workers=1, parent=self)
        self._ram_buffer_enabled       = ram_buffer_enabled
        self._ram_buffer_threshold_pct = max(0, min(ram_buffer_threshold_pct, 90))

    # ------------------------------------------------------------------
    # Construction de la commande
    # ------------------------------------------------------------------

    def build_command(self, config: EncodeConfig) -> list[str] | list[list[str]]:
        """
        Retourne une commande (list[str]) ou deux commandes pour la double passe (list[list[str]]).
        """
        if config.video.quality_mode == QualityMode.SIZE:
            return self._build_two_pass(config)
        return self._build_single_pass(config)

    def build_command_single(self, config: EncodeConfig) -> list[str]:
        """Toujours une seule commande — pour l'aperçu UI."""
        if config.video.quality_mode == QualityMode.SIZE:
            return self._build_two_pass(config)[1]   # passe 2
        return self._build_single_pass(config)

    def _build_single_pass(self, config: EncodeConfig) -> list[str]:
        cmd: list[str] = [self._ffmpeg, "-hide_banner", "-y", "-i", str(config.source)]

        vf = self._build_vf(config.video)
        if vf:
            cmd.extend(["-vf", vf])

        cmd.extend(["-map", "0:v:0"])
        cmd.extend(self._video_codec_args(config.video, config.video.bitrate_kbps))

        if config.video.inject_hdr_meta and not config.video.tonemap_to_sdr:
            cmd.extend(self._hdr_meta_args(config.video))

        for i, a in enumerate(config.audio_tracks):
            cmd.extend(["-map", f"0:{a.stream_index}"])
            cmd.extend(self._audio_codec_args(i, a))

        if config.copy_subtitles:
            cmd.extend(["-map", "0:s?", "-c:s", "copy"])

        cmd.append(str(config.output))
        return cmd

    def _build_two_pass(self, config: EncodeConfig) -> list[list[str]]:
        bitrate = self._size_to_bitrate_kbps(config)
        vf = self._build_vf(config.video)

        def _base() -> list[str]:
            c = [self._ffmpeg, "-hide_banner", "-y", "-i", str(config.source)]
            if vf:
                c.extend(["-vf", vf])
            c.extend(["-map", "0:v:0"])
            c.extend(self._video_codec_args_bitrate(config.video, bitrate))
            return c

        pass1 = _base() + ["-pass", "1", "-an", "-f", "null", "/dev/null"]

        pass2 = _base() + ["-pass", "2"]
        if config.video.inject_hdr_meta and not config.video.tonemap_to_sdr:
            pass2.extend(self._hdr_meta_args(config.video))
        for i, a in enumerate(config.audio_tracks):
            pass2.extend(["-map", f"0:{a.stream_index}"])
            pass2.extend(self._audio_codec_args(i, a))
        if config.copy_subtitles:
            pass2.extend(["-map", "0:s?", "-c:s", "copy"])
        pass2.append(str(config.output))

        return [pass1, pass2]

    # ------------------------------------------------------------------
    # Arguments par codec
    # ------------------------------------------------------------------

    def _video_codec_args(self, v: VideoEncodeSettings, bitrate_kbps: int) -> list[str]:
        if v.quality_mode == QualityMode.CRF:
            return self._video_codec_args_crf(v)
        return self._video_codec_args_bitrate(v, bitrate_kbps)

    def _video_codec_args_crf(self, v: VideoEncodeSettings) -> list[str]:
        match v.codec:
            case "copy":
                return ["-c:v", "copy"]
            case "libx265":
                args = ["-c:v", "libx265", "-crf", str(v.crf), "-preset", v.preset]
                if v.extra_params:
                    args.extend(["-x265-params", v.extra_params])
                return args
            case "libx264":
                args = ["-c:v", "libx264", "-crf", str(v.crf), "-preset", v.preset]
                return args
            case "libsvtav1":
                args = ["-c:v", "libsvtav1", "-crf", str(v.crf), "-preset", v.preset]
                if v.extra_params:
                    args.extend(["-svtav1-params", v.extra_params])
                return args
            case "hevc_nvenc":
                return ["-c:v", "hevc_nvenc", "-rc:v", "vbr", "-cq:v", str(v.crf), "-preset:v", v.preset]
            case "hevc_amf":
                return ["-c:v", "hevc_amf", "-rc", "cqp", "-qp_p", str(v.crf), "-qp_i", str(v.crf)]
            case "hevc_qsv":
                return ["-c:v", "hevc_qsv", "-global_quality", str(v.crf), "-look_ahead", "1"]
            case "h264_nvenc":
                return ["-c:v", "h264_nvenc", "-rc:v", "vbr", "-cq:v", str(v.crf), "-preset:v", v.preset]
            case "h264_amf":
                return ["-c:v", "h264_amf", "-rc", "cqp", "-qp_p", str(v.crf), "-qp_i", str(v.crf)]
            case "h264_qsv":
                return ["-c:v", "h264_qsv", "-global_quality", str(v.crf)]
            case _:
                return ["-c:v", v.codec, "-crf", str(v.crf)]

    def _video_codec_args_bitrate(self, v: VideoEncodeSettings, bitrate_kbps: int) -> list[str]:
        match v.codec:
            case "copy":
                return ["-c:v", "copy"]
            case "libx265":
                args = ["-c:v", "libx265", "-b:v", f"{bitrate_kbps}k", "-preset", v.preset]
                if v.extra_params:
                    args.extend(["-x265-params", v.extra_params])
                return args
            case "libx264":
                return ["-c:v", "libx264", "-b:v", f"{bitrate_kbps}k", "-preset", v.preset]
            case "libsvtav1":
                return ["-c:v", "libsvtav1", "-b:v", f"{bitrate_kbps}k", "-preset", v.preset]
            case "hevc_nvenc":
                return ["-c:v", "hevc_nvenc", "-b:v", f"{bitrate_kbps}k", "-preset:v", v.preset]
            case "hevc_amf":
                return ["-c:v", "hevc_amf", "-b:v", f"{bitrate_kbps}k"]
            case "hevc_qsv":
                return ["-c:v", "hevc_qsv", "-b:v", f"{bitrate_kbps}k"]
            case _:
                return ["-c:v", v.codec, "-b:v", f"{bitrate_kbps}k"]

    def _build_vf(self, v: VideoEncodeSettings) -> str:
        """Filtre vidéo pour le tone mapping HDR→SDR (BT.2020 PQ → BT.709)."""
        if not v.tonemap_to_sdr:
            return ""
        algo = v.tonemap_algorithm or "hable"
        return (
            "zscale=transfer=linear:npl=100,"
            "format=gbrpf32le,"
            "zscale=primaries=bt709,"
            f"tonemap=tonemap={algo}:desat=0,"
            "zscale=transfer=bt709:matrix=bt709:range=tv,"
            "format=yuv420p"
        )

    def _hdr_meta_args(self, v: VideoEncodeSettings) -> list[str]:
        """Flags de couleur + métadonnées statiques HDR10 (ST 2086 / MaxCLL)."""
        args = ["-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc"]
        if v.master_display:
            args.extend(["-master_display", v.master_display])
        if v.max_cll:
            args.extend(["-max_cll", v.max_cll])
        return args

    def _audio_codec_args(self, out_idx: int, a: AudioTrackSettings) -> list[str]:
        args: list[str] = []
        # BSF TrueHD core extraction (supprime la couche Atmos)
        if a.extract_truehd_core:
            args.extend([f"-bsf:a:{out_idx}", "truehd_core"])
        match a.codec:
            case "copy":
                args.extend([f"-c:a:{out_idx}", "copy"])
            case "aac":
                args.extend([f"-c:a:{out_idx}", "aac", f"-b:a:{out_idx}", f"{a.bitrate_kbps}k"])
            case "eac3":
                args.extend([f"-c:a:{out_idx}", "eac3", f"-b:a:{out_idx}", f"{a.bitrate_kbps}k"])
            case "flac":
                args.extend([f"-c:a:{out_idx}", "flac"])
            case _:
                args.extend([f"-c:a:{out_idx}", a.codec])
        return args

    def _size_to_bitrate_kbps(self, config: EncodeConfig) -> int:
        duration = config.duration_s or 3600.0
        total_bits = config.video.target_size_mb * 8 * 1024 * 1024
        audio_bps = sum(
            a.bitrate_kbps * 1000
            for a in config.audio_tracks
            if a.codec not in ("copy", "flac")
        )
        video_bits = total_bits - audio_bps * duration
        return max(500, int(video_bits / duration / 1000))

    # ------------------------------------------------------------------
    # Helpers RAM / buffer — cross-platform (Linux · macOS · Windows)
    # ------------------------------------------------------------------

    @staticmethod
    def _total_ram_bytes() -> int:
        """
        Retourne la RAM physique totale en octets.
        Linux : /proc/meminfo · macOS : sysctl hw.memsize · Windows : ctypes GlobalMemoryStatusEx.
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
                    capture_output=True, text=True, check=False, timeout=5,
                )
                v = r.stdout.strip()
                return int(v) if r.returncode == 0 and v.isdigit() else 0
            if sys.platform == "win32":
                return EncodeWorkflow._win_mem_status().ullTotalPhys
        except Exception:
            pass
        return 0

    @staticmethod
    def _available_ram_bytes() -> int:
        """
        Retourne la RAM disponible en octets (MemAvailable sur Linux, équivalent sur macOS/Windows).
        Retourne 0 si non déterminable.
        """
        try:
            if sys.platform == "linux":
                text = Path("/proc/meminfo").read_text(encoding="ascii")
                m = re.search(r"MemAvailable:\s+(\d+)\s+kB", text)
                return int(m.group(1)) * 1024 if m else 0
            if sys.platform == "darwin":
                return EncodeWorkflow._macos_available_ram()
            if sys.platform == "win32":
                return EncodeWorkflow._win_mem_status().ullAvailPhys
        except Exception:
            pass
        return 0

    @staticmethod
    def _macos_available_ram() -> int:
        """RAM disponible sur macOS via vm_stat (free + inactive + speculative + purgeable)."""
        r = subprocess.run(
            ["vm_stat"], capture_output=True, text=True, check=False, timeout=5
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

    @staticmethod
    def _win_mem_status():
        """Retourne une structure MEMORYSTATUSEX remplie (Windows uniquement)."""
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
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))  # type: ignore[attr-defined]
        return stat

    @staticmethod
    def _ram_buffer_dir() -> Path | None:
        """
        Retourne le répertoire RAM-backed disponible sur cette plateforme, ou None.

        · Linux  : /dev/shm (tmpfs kernel, taille = RAM physique)
        · macOS  : /dev/shm (POSIX shm namespace, writable sur macOS ≥ 10.15)
        · Windows: aucun équivalent standard → None (buffer sur disque uniquement)
        """
        if sys.platform in ("linux", "darwin"):
            shm = Path("/dev/shm")
            if shm.is_dir() and os.access(shm, os.W_OK):
                return shm
        return None

    def _shm_path(self, tmp: Path, name: str, file_size: int) -> Path:
        """
        Retourne un chemin dans le répertoire RAM si les conditions sont réunies,
        sinon un chemin dans tmp (disque).

        Conditions (toutes requises) :
          1. ram_buffer_enabled = True (configuration)
          2. Un répertoire RAM existe sur cette plateforme (_ram_buffer_dir())
          3. RAM disponible après chargement ≥ threshold_pct % de la RAM totale
             formule : available_before - file_size ≥ total_ram × threshold_pct / 100

        La décision est réévaluée à chaque appel (RAM dynamique).
        """
        if not self._ram_buffer_enabled:
            return tmp / name
        ram_dir = EncodeWorkflow._ram_buffer_dir()
        if ram_dir is None:
            return tmp / name
        total     = EncodeWorkflow._total_ram_bytes()
        available = EncodeWorkflow._available_ram_bytes()
        if total <= 0 or available <= 0:
            return tmp / name
        min_free_after = int(total * self._ram_buffer_threshold_pct / 100)
        if available - file_size >= min_free_after:
            return ram_dir / name
        return tmp / name

    # ------------------------------------------------------------------
    # Aperçu lisible
    # ------------------------------------------------------------------

    def preview_command(self, config: EncodeConfig) -> str:
        cmd = self.build_command_single(config)
        if not cmd:
            return ""
        prefix = "# Mode taille cible : passe 1 omise de cet aperçu\n" \
                 if config.video.quality_mode == QualityMode.SIZE else ""
        lines = [cmd[0]]
        i = 1
        while i < len(cmd):
            p = cmd[i]
            if p.startswith("-") and i + 1 < len(cmd) and not cmd[i + 1].startswith("-"):
                lines.append(f"    {p} {cmd[i + 1]}")
                i += 2
            else:
                lines.append(f"    {p}")
                i += 1
        return prefix + " \\\n".join(lines)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate(self, config: EncodeConfig) -> list[str]:
        errors: list[str] = []
        if not config.source.is_file():
            errors.append(f"Fichier source introuvable : {config.source}")
        if not config.output.parent.exists():
            errors.append(f"Dossier de sortie inexistant : {config.output.parent}")
        if config.source == config.output:
            errors.append("Le fichier de sortie doit être différent du fichier source.")
        if config.video.quality_mode == QualityMode.SIZE and not (config.duration_s or 0) > 0:
            errors.append("Durée du fichier source inconnue — mode taille cible impossible.")
        if config.video.inject_hdr_meta and not config.video.tonemap_to_sdr:
            if config.video.master_display and not re.match(
                r"^G\(\d+,\d+\)B\(\d+,\d+\)R\(\d+,\d+\)WP\(\d+,\d+\)L\(\d+,\d+\)$",
                config.video.master_display.strip(),
            ):
                errors.append(
                    "Format master_display invalide. "
                    "Attendu : G(x,y)B(x,y)R(x,y)WP(x,y)L(max,min)"
                )
            if config.video.max_cll and not re.match(r"^\d+,\d+$", config.video.max_cll.strip()):
                errors.append("Format MaxCLL invalide. Attendu : MaxCLL,MaxFALL  ex. 1000,400")
        return errors

    # ------------------------------------------------------------------
    # Exécution
    # ------------------------------------------------------------------

    def run(self, config: EncodeConfig) -> TaskSignals:
        """
        Lance l'encodage dans un thread secondaire.

        Le mode taille cible exécute deux passes séquentiellement
        dans le même thread et retourne un unique TaskSignals.
        """
        errors = self.validate(config)
        if errors:
            raise EncodeError("\n".join(errors))

        self.log_message.emit("INFO", f"Encodage → {config.output.name}")

        if config.copy_dv or config.copy_hdr10plus:
            return self._run_with_metadata_inject(config)

        cwd = config.work_dir or config.source.parent
        if config.work_dir:
            config.work_dir.mkdir(parents=True, exist_ok=True)

        if config.video.quality_mode == QualityMode.SIZE:
            cmds = self._build_two_pass(config)
            return self._run_two_pass(cmds, cwd=cwd)

        cmd = self._build_single_pass(config)
        return self._runner.run(cmd, cwd=cwd, label="ffmpeg")

    def _run_two_pass(
        self,
        cmds: list[list[str]],
        cwd: Path | None,
        signals: TaskSignals | None = None,
    ) -> TaskSignals:
        """Exécute deux commandes ffmpeg séquentiellement, retourne un TaskSignals commun."""
        if signals is None:
            signals = TaskSignals()
        executor = ThreadPoolExecutor(max_workers=1)

        def _task() -> None:
            try:
                self.log_message.emit("INFO", "Passe 1/2 (analyse)…")
                self._runner._run_cmd(
                    cmds[0], cwd=cwd, label="ffmpeg-pass1",
                    progress_cb=lambda line: signals.progress.emit(line),
                    signals=signals,
                )
                self.log_message.emit("INFO", "Passe 2/2 (encodage)…")
                output = self._runner._run_cmd(
                    cmds[1], cwd=cwd, label="ffmpeg-pass2",
                    progress_cb=lambda line: signals.progress.emit(line),
                    signals=signals,
                )
                signals.finished.emit(output)
            except TaskCancelledError:
                signals.cancelled.emit()
            except Exception as exc:
                signals.failed.emit(str(exc), exc)
            finally:
                executor.shutdown(wait=False)

        executor.submit(_task)
        return signals

    def _run_with_metadata_inject(self, config: EncodeConfig) -> TaskSignals:
        """
        Workflow d'encodage avec injection DV RPU / HDR10+ en post-traitement.

        Gestion des fichiers de travail — max 2 gros fichiers simultanément :
          • src.hevc est supprimé dès que RPU et HDR10+ sont extraits (avant l'encodage).
          • encoded.mkv reste jusqu'au remuxage final (nécessaire pour audio/subs).
          • Chaque HEVC intermédiaire est alloué via _shm_path() : en RAM (/dev/shm)
            si RAM libre > taille × 1.10, sinon sur disque.
          • Chaque intermédiaire HEVC est supprimé immédiatement dès que l'étape
            suivante l'a consommé.
          • Les fichiers /dev/shm sont explicitement supprimés dans le finally.

        Ordre d'injection (inchangé — contrainte HDR10+ avant DV) :
          HDR10+ en premier : hdr10plus_tool ne tolère pas les NAL RPU DV existants.
          dovi_tool préserve tous les types de NAL.

        Étapes :
          1. Extraction HEVC source brut → src.hevc (RAM ou disque)
          2. Extraction RPU DoVi (dovi_tool extract-rpu) si copy_dv
          3. Extraction HDR10+ (hdr10plus_tool extract) si copy_hdr10plus
          4. Suppression immédiate de src.hevc
          5. Encodage vers encoded.mkv
          6. Extraction HEVC encodé → current_hevc (RAM si possible)
          7. Injection HDR10+ si applicable → nouveau current_hevc, ancien supprimé
          8. Injection RPU DV si applicable → nouveau current_hevc, ancien supprimé
          9. Remuxage final (mkvmerge : current_hevc + audio/subs de encoded.mkv)
        """
        signals = TaskSignals()
        executor = ThreadPoolExecutor(max_workers=1)

        def _task() -> None:
            work = config.work_dir
            if work:
                work.mkdir(parents=True, exist_ok=True)
            tmp_dir = tempfile.mkdtemp(
                prefix="mkv_toolkit_encode_",
                dir=str(work) if work else None,
            )
            tmp = Path(tmp_dir)
            # Fichiers alloués hors du répertoire de travail tmp (ex. /dev/shm)
            # → nettoyage explicite dans le finally car shutil.rmtree ne les couvre pas.
            ext_files: list[Path] = []
            _ram_dir = EncodeWorkflow._ram_buffer_dir()

            def _run(cmd: list[str]) -> str:
                return self._runner._run_cmd(
                    cmd, signals=signals, cwd=tmp,
                    progress_cb=lambda line: signals.progress.emit(line),
                )

            def _check() -> None:
                if signals._cancel_event.is_set():
                    raise TaskCancelledError()

            def _alloc(name: str, ref_size: int) -> Path:
                """
                Alloue un chemin de travail HEVC.
                Si le buffer RAM est actif et la RAM suffisante → répertoire RAM.
                Sinon → tmp (disque).
                Le chemin est enregistré dans ext_files s'il est hors de tmp.
                """
                p = self._shm_path(tmp, name, ref_size)
                if p.parent != tmp:   # hors tmp = répertoire RAM ou autre
                    ext_files.append(p)
                return p

            def _free(path: Path) -> None:
                """
                Supprime immédiatement un fichier intermédiaire.
                Le retire de ext_files si présent.
                Silencieux sur toute erreur OS.
                """
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
                try:
                    ext_files.remove(path)
                except ValueError:
                    pass   # chemin disque, pas dans ext_files

            try:
                # ── 1. HEVC source brut ──────────────────────────────────
                # Taille estimée = taille du fichier source (approximation conservative)
                src_size_est = config.source.stat().st_size
                src_hevc = _alloc("src.hevc", src_size_est)
                signals.progress.emit("Extraction HEVC source…")
                _run([
                    self._ffmpeg, "-hide_banner", "-y",
                    "-i", str(config.source),
                    "-map", "0:v:0", "-c:v", "copy", "-f", "hevc", str(src_hevc),
                ])
                _check()

                # ── 2. RPU Dolby Vision ──────────────────────────────────
                rpu_bin = tmp / "rpu.bin"
                if config.copy_dv:
                    signals.progress.emit("Extraction RPU Dolby Vision…")
                    _run([
                        self._bins["dovi_tool"], "extract-rpu",
                        "-i", str(src_hevc), "-o", str(rpu_bin),
                    ])
                    _check()

                # ── 3. HDR10+ ────────────────────────────────────────────
                hdr10p_json = tmp / "hdr10p.json"
                if config.copy_hdr10plus:
                    signals.progress.emit("Extraction métadonnées HDR10+…")
                    _run([
                        self._bins["hdr10plus_tool"], "extract",
                        str(src_hevc), "-o", str(hdr10p_json),
                    ])
                    _check()

                # ── 4. Suppression immédiate de src.hevc ─────────────────
                # src.hevc n'est plus nécessaire : RPU et HDR10+ sont extraits.
                # Libérer l'espace avant l'encodage (étape 5).
                _free(src_hevc)

                # ── 5. Encodage vers MKV temporaire ─────────────────────
                encoded_mkv = tmp / "encoded.mkv"
                temp_cfg = replace(
                    config, output=encoded_mkv,
                    copy_dv=False, copy_hdr10plus=False,
                )
                if config.video.quality_mode == QualityMode.SIZE:
                    cmds = self._build_two_pass(temp_cfg)
                    self.log_message.emit("INFO", "Passe 1/2 (analyse)…")
                    _run(cmds[0])
                    _check()
                    self.log_message.emit("INFO", "Passe 2/2 (encodage)…")
                    _run(cmds[1])
                else:
                    _run(self._build_single_pass(temp_cfg))
                _check()

                # ── 6. HEVC encodé brut ──────────────────────────────────
                # Sur disque à ce stade : encoded.mkv uniquement.
                # On alloue enc.hevc en RAM si possible pour ne pas dépasser
                # 2 gros fichiers disque (encoded.mkv + current_hevc).
                enc_size_est = encoded_mkv.stat().st_size
                current_hevc = _alloc("enc.hevc", enc_size_est)
                signals.progress.emit("Extraction HEVC encodé…")
                _run([
                    self._ffmpeg, "-hide_banner", "-y",
                    "-i", str(encoded_mkv),
                    "-map", "0:v:0", "-c:v", "copy", "-f", "hevc", str(current_hevc),
                ])
                _check()

                # ── 7. Injection HDR10+ ──────────────────────────────────
                # HDR10+ avant DV : hdr10plus_tool ne tolère pas les NAL RPU DV.
                if config.copy_hdr10plus and hdr10p_json.exists():
                    cur_size = current_hevc.stat().st_size
                    out_hdr10p = _alloc("enc_hdr10p.hevc", cur_size)
                    signals.progress.emit("Injection métadonnées HDR10+…")
                    _run([
                        self._bins["hdr10plus_tool"], "inject",
                        "-i", str(current_hevc),
                        "-j", str(hdr10p_json),
                        "-o", str(out_hdr10p),
                    ])
                    _free(current_hevc)   # libère enc.hevc immédiatement
                    current_hevc = out_hdr10p
                    _check()

                # ── 8. Injection RPU DV ──────────────────────────────────
                if config.copy_dv and rpu_bin.exists():
                    cur_size = current_hevc.stat().st_size
                    out_dv = _alloc("enc_dv.hevc", cur_size)
                    signals.progress.emit("Injection RPU Dolby Vision…")
                    _run([
                        self._bins["dovi_tool"],
                        "-m", config.dovi_profile,
                        "inject-rpu",
                        "-i", str(current_hevc),
                        "-r", str(rpu_bin),
                        "-o", str(out_dv),
                    ])
                    _free(current_hevc)   # libère enc_hdr10p.hevc (ou enc.hevc)
                    current_hevc = out_dv
                    _check()

                # ── 9. Remuxage final ────────────────────────────────────
                signals.progress.emit("Remuxage final…")
                _run([
                    self._bins["mkvmerge"],
                    "-o", str(config.output),
                    str(current_hevc),              # piste vidéo avec métadonnées
                    "--no-video", str(encoded_mkv), # audio + sous-titres uniquement
                ])
                signals.finished.emit(f"Encodage terminé → {config.output.name}")

            except TaskCancelledError:
                signals.cancelled.emit()
            except Exception as exc:
                signals.failed.emit(str(exc), exc)
            finally:
                executor.shutdown(wait=False)
                # Supprimer les fichiers hors tmp (ex. /dev/shm) non couverts par rmtree.
                # Itère sur une copie : _free() mute ext_files.
                for p in list(ext_files):
                    try:
                        p.unlink(missing_ok=True)
                    except OSError:
                        pass
                shutil.rmtree(tmp_dir, ignore_errors=True)

        executor.submit(_task)
        return signals
