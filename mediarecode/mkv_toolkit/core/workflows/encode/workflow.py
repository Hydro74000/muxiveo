"""
core/workflows/encode/workflow.py — FFmpeg encode workflow with optional HDR metadata injection.

Public:
    EncodeWorkflow
"""

from __future__ import annotations

import ctypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from core.lang_tags import Rfc5646LanguageTags as LangTags
from core.runner import TaskCancelledError, TaskSignals, ToolRunner
from core.workflows.encode.models import (
    EncodeConfig, EncodeError, QualityMode,
    VideoEncodeSettings, AudioTrackSettings,
)


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
        mkvextract_bin:            str  = "mkvextract",
        mkvpropedit_bin:           str  = "mkvpropedit",
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
            "mkvextract":     mkvextract_bin,
            "mkvpropedit":    mkvpropedit_bin,
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
        # ── Collecte des sources d'entrée ────────────────────────────────────
        # L'ordre détermine les indices -i : source principale = 0, extras = 1+
        all_sources: list[Path] = [config.source]
        for a in config.audio_tracks:
            sp = a.source_path or config.source
            if sp not in all_sources:
                all_sources.append(sp)
        for src_path, _idx in config.subtitle_tracks:
            if src_path not in all_sources:
                all_sources.append(src_path)
        for src_path, _idx in config.attachment_streams:
            if src_path not in all_sources:
                all_sources.append(src_path)
        source_idx: dict[Path, int] = {p: i for i, p in enumerate(all_sources)}

        cmd: list[str] = [self._ffmpeg, "-hide_banner", "-y"]
        for src in all_sources:
            cmd.extend(["-i", str(src)])

        vf = self._build_vf(config.video)
        if vf:
            cmd.extend(["-vf", vf])

        cmd.extend(["-map", "0:v:0"])
        cmd.extend(self._video_codec_args(config.video, config.video.bitrate_kbps))

        # Passthrough métadonnées container et stream vidéo (codec COPY uniquement).
        if config.video.codec == "copy":
            cmd.extend(["-map_metadata", "0", "-map_metadata:s:v:0", "0:s:v:0"])

        if config.video.inject_hdr_meta and not config.video.tonemap_to_sdr:
            cmd.extend(self._hdr_meta_args(config.video))

        for i, a in enumerate(config.audio_tracks):
            inp = source_idx.get(a.source_path or config.source, 0)
            cmd.extend(["-map", f"{inp}:{a.stream_index}"])
            cmd.extend(self._audio_codec_args(i, a))

        # Sous-titres : soit liste explicite (multi-sources), soit map générique source 0
        if config.subtitle_tracks:
            for src_path, stream_idx in config.subtitle_tracks:
                inp = source_idx.get(src_path, 0)
                cmd.extend(["-map", f"{inp}:{stream_idx}"])
            cmd.extend(["-c:s", "copy"])
        elif config.copy_subtitles:
            for inp_i in range(len(all_sources)):
                cmd.extend(["-map", f"{inp_i}:s?"])
            cmd.extend(["-c:s", "copy"])

        if config.attachment_streams:
            for src_path, stream_idx in config.attachment_streams:
                inp = source_idx.get(src_path, 0)
                cmd.extend(["-map", f"{inp}:{stream_idx}"])
            cmd.extend(["-c:t", "copy"])

        if config.keep_chapters:
            cmd.extend(["-map_chapters", "0"])

        cmd.append(str(config.output))
        return cmd

    def _build_two_pass(self, config: EncodeConfig) -> list[list[str]]:
        bitrate = self._size_to_bitrate_kbps(config)
        vf = self._build_vf(config.video)

        # Sources d'entrée (multi-sources pour audio, sous-titres, attachements)
        all_sources: list[Path] = [config.source]
        for a in config.audio_tracks:
            sp = a.source_path or config.source
            if sp not in all_sources:
                all_sources.append(sp)
        for src_path, _idx in config.subtitle_tracks:
            if src_path not in all_sources:
                all_sources.append(src_path)
        for src_path, _idx in config.attachment_streams:
            if src_path not in all_sources:
                all_sources.append(src_path)
        source_idx: dict[Path, int] = {p: i for i, p in enumerate(all_sources)}

        def _base() -> list[str]:
            c = [self._ffmpeg, "-hide_banner", "-y"]
            for src in all_sources:
                c.extend(["-i", str(src)])
            if vf:
                c.extend(["-vf", vf])
            c.extend(["-map", "0:v:0"])
            c.extend(self._video_codec_args_bitrate(config.video, bitrate))
            return c

        pass1 = _base() + ["-pass", "1", "-an", "-f", "null", "/dev/null"]

        pass2 = _base() + ["-pass", "2"]
        if config.video.codec == "copy":
            pass2.extend(["-map_metadata", "0", "-map_metadata:s:v:0", "0:s:v:0"])
        if config.video.inject_hdr_meta and not config.video.tonemap_to_sdr:
            pass2.extend(self._hdr_meta_args(config.video))
        for i, a in enumerate(config.audio_tracks):
            inp = source_idx.get(a.source_path or config.source, 0)
            pass2.extend(["-map", f"{inp}:{a.stream_index}"])
            pass2.extend(self._audio_codec_args(i, a))
        if config.subtitle_tracks:
            for src_path, stream_idx in config.subtitle_tracks:
                inp = source_idx.get(src_path, 0)
                pass2.extend(["-map", f"{inp}:{stream_idx}"])
            pass2.extend(["-c:s", "copy"])
        elif config.copy_subtitles:
            for inp_i in range(len(all_sources)):
                pass2.extend(["-map", f"{inp_i}:s?"])
            pass2.extend(["-c:s", "copy"])
        if config.attachment_streams:
            for src_path, stream_idx in config.attachment_streams:
                inp = source_idx.get(src_path, 0)
                pass2.extend(["-map", f"{inp}:{stream_idx}"])
            pass2.extend(["-c:t", "copy"])

        if config.keep_chapters:
            pass2.extend(["-map_chapters", "0"])
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
                x265 = self._x265_params(v)
                if x265:
                    args.extend(["-x265-params", x265])
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
                x265 = self._x265_params(v)
                if x265:
                    args.extend(["-x265-params", x265])
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

    def _x265_params(self, v: VideoEncodeSettings) -> str:
        """
        Construit la valeur de -x265-params en fusionnant extra_params et les
        métadonnées HDR10 statiques (master-display, max-cll) si inject_hdr_meta est actif.

        Retourne une chaîne vide si aucun paramètre n'est à passer.
        """
        parts: list[str] = []
        if v.extra_params:
            parts.append(v.extra_params.strip(":"))
        if v.inject_hdr_meta and not v.tonemap_to_sdr:
            if v.master_display:
                parts.append(f"master-display={v.master_display}")
            if v.max_cll:
                parts.append(f"max-cll={v.max_cll}")
        return ":".join(p for p in parts if p)

    def _hdr_meta_args(self, v: VideoEncodeSettings) -> list[str]:
        """
        Flags de couleur container-level + métadonnées SEI selon le codec.

        Couleur (valides pour tout codec HEVC/AV1 re-encodé) :
            -color_primaries bt2020  -color_trc smpte2084  -colorspace bt2020nc

        master_display / max_cll par codec :
            libx265          → injectés via -x265-params (dans _video_codec_args_crf/bitrate)
            hevc_nvenc        → options privées du codec (-master_display / -max_cll)
            hevc_amf, hevc_qsv, libsvtav1 → pas de mécanisme standardisé → ignorés
            copy, h264_*, libx264 → couleur non applicable / pas de HDR10 → rien
        """
        if v.codec in ("copy", "libx264", "h264_nvenc", "h264_amf", "h264_qsv"):
            return []
        args = ["-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc"]
        if v.codec == "hevc_nvenc":
            if v.master_display:
                args.extend(["-master_display", v.master_display])
            if v.max_cll:
                args.extend(["-max_cll", v.max_cll])
        # libx265 : master_display/max_cll déjà fusionnés dans -x265-params
        # hevc_amf, hevc_qsv, libsvtav1 : couleur only, SEI non géré
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

    # ------------------------------------------------------------------
    # Commandes spécialisées pour _run_with_metadata_inject
    # ------------------------------------------------------------------

    def _build_video_only_cmd(self, config: EncodeConfig, output_hevc: Path) -> list[str]:
        """
        ffmpeg : vidéo seule, sortie HEVC brut (-f hevc, sans container).
        Pas d'audio ni de subs. Utilisé pour encoder directement vers un
        flux HEVC injectable, sans passer par un MKV intermédiaire.
        """
        cmd = [self._ffmpeg, "-hide_banner", "-y", "-i", str(config.source)]
        vf = self._build_vf(config.video)
        if vf:
            cmd.extend(["-vf", vf])
        cmd.extend(["-map", "0:v:0"])
        cmd.extend(self._video_codec_args(config.video, config.video.bitrate_kbps))
        if config.video.inject_hdr_meta and not config.video.tonemap_to_sdr:
            cmd.extend(self._hdr_meta_args(config.video))
        cmd.extend(["-an", "-f", "hevc", str(output_hevc)])
        return cmd

    def _build_video_only_two_pass(
        self, config: EncodeConfig, output_hevc: Path
    ) -> list[list[str]]:
        """
        Deux passes ffmpeg : vidéo seule, sortie HEVC brut.
        Utilisé en mode SIZE pour l'étape vidéo de _run_with_metadata_inject.
        """
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
        pass2.extend(["-an", "-f", "hevc", str(output_hevc)])
        return [pass1, pass2]

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
            if config.video.codec == "copy":
                # Codec COPY : les NAL units DoVi/HDR10+ sont déjà dans le bitstream source.
                # Extraction + réinjection inutiles sans réencodage — remux direct avec passthrough.
                self.log_message.emit(
                    "INFO",
                    "Codec COPY : injection DoVi/HDR10+ ignorée — "
                    "métadonnées préservées par passthrough ffmpeg.",
                )
            else:
                return self._run_with_metadata_inject(config)

        cwd = config.work_dir or config.source.parent
        if config.work_dir:
            config.work_dir.mkdir(parents=True, exist_ok=True)

        has_tags        = bool(config.tag_sources)
        has_meta_edits  = bool(config.track_meta_edits)
        needs_postproc  = has_tags or has_meta_edits

        if config.video.quality_mode == QualityMode.SIZE:
            cmds = self._build_two_pass(config)
            post = (lambda s: self._postproc(config, s)) if needs_postproc else None
            return self._run_two_pass(cmds, cwd=cwd, post_fn=post)

        cmd = self._build_single_pass(config)
        if needs_postproc:
            return self._run_single_with_postproc(cmd, config, cwd)
        return self._runner.run(cmd, cwd=cwd, label="ffmpeg")

    def _run_two_pass(
        self,
        cmds: list[list[str]],
        cwd: Path | None,
        signals: TaskSignals | None = None,
        post_fn=None,   # Callable[[TaskSignals], None] | None
    ) -> TaskSignals:
        """Exécute deux commandes ffmpeg séquentiellement, retourne un TaskSignals commun.

        post_fn : si fourni, appelé après la passe 2 avant signals.finished (ex. injection tags).
        """
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
                if post_fn is not None:
                    post_fn(signals)
                signals.finished.emit(output)
            except TaskCancelledError:
                signals.cancelled.emit()
            except Exception as exc:
                signals.failed.emit(str(exc), exc)
            finally:
                executor.shutdown(wait=False)

        executor.submit(_task)
        return signals

    def _inject_tags_inplace(
        self,
        output: Path,
        tag_sources: list,
        signals: "TaskSignals | None" = None,
    ) -> None:
        """
        Injecte les balises MKV (<Tags>) depuis les sources vers output.mkv, sans fichier
        vidéo intermédiaire.

        Processus :
          1. mkvextract <source> tags  → XML vers stdout (quelques Ko)
          2. Écriture dans un NamedTemporaryFile XML (supprimé immédiatement après)
          3. mkvpropedit <output> --tags all:<xml>  → modification in-place

        Les balises de sources multiples sont injectées séquentiellement ;
        chaque appel remplace les balises existantes (dernier appel gagne).
        """
        if not tag_sources:
            return

        mkvextract_bin  = self._bins["mkvextract"]
        mkvpropedit_bin = self._bins["mkvpropedit"]

        if signals:
            signals.progress.emit("Injection balises MKV…")

        for src in tag_sources:
            # ── Extraction XML depuis la source ─────────────────────────────
            result = subprocess.run(
                [mkvextract_bin, str(src), "tags"],
                capture_output=True, text=True, timeout=30,
            )
            tags_xml = result.stdout.strip()
            if not tags_xml or result.returncode != 0:
                self.log_message.emit(
                    "WARN",
                    f"Aucune balise dans {Path(src).name} "
                    f"(mkvextract code={result.returncode})",
                )
                continue

            # ── Fichier XML temporaire (quelques Ko, supprimé immédiatement) ─
            with tempfile.NamedTemporaryFile(
                suffix=".xml", mode="w", encoding="utf-8",
                delete=False, prefix="mkv_tags_",
            ) as f:
                f.write(tags_xml)
                xml_path = Path(f.name)

            try:
                tags_cmd = [mkvpropedit_bin, str(output), "--tags", f"all:{xml_path}"]
                self.log_message.emit("INFO", "$ " + " ".join(tags_cmd))
                r = subprocess.run(tags_cmd, capture_output=True, text=True, timeout=60)
                if r.returncode != 0:
                    self.log_message.emit("WARN", f"mkvpropedit: {r.stderr.strip()}")
                else:
                    self.log_message.emit(
                        "INFO", f"Balises MKV injectées depuis {Path(src).name}"
                    )
            finally:
                xml_path.unlink(missing_ok=True)

    def _set_writing_app_inplace(self, output: Path) -> None:
        """Écrit le tag Multiplexing Application dans les infos de segment via mkvpropedit."""
        mkvpropedit_bin = self._bins["mkvpropedit"]
        cmd = [
            mkvpropedit_bin, str(output),
            "--edit", "info",
            "--set", "muxing-application=MediaRecode v1.0 by Hydro74000 - VibeCode Proof of Concept",
        ]
        try:
            self.log_message.emit("INFO", "$ " + " ".join(cmd))
            r = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=30)
            if r.returncode != 0:
                self.log_message.emit("WARN", f"mkvpropedit (writing-app) : {r.stderr.strip()}")
        except FileNotFoundError:
            self.log_message.emit("WARN", "mkvpropedit introuvable — writing-app non appliqué.")

    def _postproc(self, config: EncodeConfig, signals: "TaskSignals | None" = None) -> None:
        """Post-traitements in-place : balises MKV + métadonnées de pistes + writing-app."""
        self._inject_tags_inplace(config.output, config.tag_sources, signals)
        self._apply_track_meta_edits_inplace(config.output, config.track_meta_edits)
        self._set_writing_app_inplace(config.output)

    def _apply_track_meta_edits_inplace(self, output: Path, edits: list) -> None:
        """
        Applique les éditions de langue/titre de pistes via mkvpropedit.

        Chaque TrackMetaEdit.track_order est le numéro 1-based de la piste dans
        le fichier de sortie (sélecteur ``@N`` de mkvpropedit).
        """
        if not edits:
            return
        mkvpropedit_bin = self._bins["mkvpropedit"]
        cmd: list[str] = [mkvpropedit_bin, str(output)]
        for edit in edits:
            cmd.extend(["--edit", f"track:@{edit.track_order}"])
            if edit.language:
                cmd.extend(["--set", f"language={LangTags.to_iso639_2(edit.language)}"])
                cmd.extend(["--set", f"language-ietf={edit.language}"])
                self.log_message.emit("INFO", "Lang set for track " + str(edit.track_order) + " to " + edit.language + " (ISO639-2: " + LangTags.to_iso639_2(edit.language) + ") in workflow")
            if edit.title is not None:
                cmd.extend(["--set", f"name={edit.title}"])
        try:
            self.log_message.emit("INFO", "$ " + " ".join(cmd))
            r = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=30)
            if r.returncode != 0:
                self.log_message.emit("WARN", f"mkvpropedit (métadonnées) : {r.stderr.strip()}")
        except FileNotFoundError:
            self.log_message.emit("WARN", "mkvpropedit introuvable — métadonnées de pistes non appliquées.")

    def _run_single_with_postproc(
        self, cmd: list[str], config: EncodeConfig, cwd: Path
    ) -> TaskSignals:
        """
        Exécute une passe ffmpeg unique puis les post-traitements (balises + métadonnées pistes).
        """
        signals = TaskSignals()
        executor = ThreadPoolExecutor(max_workers=1)

        def _task() -> None:
            try:
                output = self._runner._run_cmd(
                    cmd, cwd=cwd, label="ffmpeg", signals=signals,
                    progress_cb=lambda line: signals.progress.emit(line),
                )
                self._postproc(config, signals)
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
        Appelé uniquement quand copy_dv ou copy_hdr10plus est actif ET codec ≠ copy.

        Gestion des fichiers de travail :
          • Pas de encoded.mkv : la vidéo est encodée directement en HEVC brut (enc.hevc).
          • L'audio copy est pris directement depuis la source via mkvmerge — aucun
            traitement. Un fichier audio_subs.mkv n'est créé que si un réencodage audio
            ou une extraction TrueHD core (BSF) est nécessaire.
          • La source n'est jamais modifiée.
          • Chaque HEVC intermédiaire est alloué via _shm_path() : en RAM (/dev/shm)
            si RAM libre > seuil, sinon sur disque.
          • Chaque intermédiaire HEVC est supprimé immédiatement dès que l'étape
            suivante l'a consommé.
          • Les fichiers /dev/shm sont explicitement supprimés dans le finally.

        Ordre d'injection (contrainte HDR10+ avant DV) :
          HDR10+ en premier : hdr10plus_tool ne tolère pas les NAL RPU DV existants.
          dovi_tool préserve tous les types de NAL.

        Étapes :
          1. Extraction HEVC source brut → src.hevc (RAM ou disque)
          2. Extraction RPU DoVi (dovi_tool extract-rpu) si copy_dv
          3. Extraction HDR10+ (hdr10plus_tool extract) si copy_hdr10plus
          4. Suppression immédiate de src.hevc
          5. Encodage vidéo seule → enc.hevc (HEVC brut, sans container)
             Mode SIZE : deux passes (analyse + encodage direct en .hevc)
          6. Injection HDR10+ si applicable → nouveau current_hevc, ancien supprimé
          7. Injection RPU DV si applicable → nouveau current_hevc, ancien supprimé
          8. Reconstitution finale via ffmpeg (une seule commande, depuis la source) :
             ffmpeg -i current_hevc -i source
               -map 0:v:0 -c:v copy            (vidéo injectée)
               -map 1:stream_idx [codec args]   (audio depuis source, copy ou réencodage)
               -map 1:s? -c:s copy              (subs depuis source)
               output.mkv
             Pas de mkvmerge, pas de fichier audio intermédiaire.
             La source n'est jamais modifiée.
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

                # ── 4. Libération de src.hevc ────────────────────────────
                # Libéré avant l'encodage pour maximiser l'espace disque/RAM.
                _free(src_hevc)

                # ── 5a. Encodage vidéo → enc.hevc brut ──────────────────
                # Encodage direct en HEVC sans container ni audio.
                # Élimine encoded.mkv et l'étape d'extraction HEVC redondante.
                # La taille estimée = taille source (approximation conservative).
                enc_hevc = _alloc("enc.hevc", src_size_est)
                signals.progress.emit("Encodage vidéo…")
                if config.video.quality_mode == QualityMode.SIZE:
                    v_cmds = self._build_video_only_two_pass(config, enc_hevc)
                    self.log_message.emit("INFO", "Passe 1/2 (analyse)…")
                    _run(v_cmds[0])
                    _check()
                    self.log_message.emit("INFO", "Passe 2/2 (encodage)…")
                    _run(v_cmds[1])
                else:
                    _run(self._build_video_only_cmd(config, enc_hevc))
                _check()
                current_hevc = enc_hevc

                # ── 6. Injection HDR10+ ──────────────────────────────────
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

                # ── 7. Injection RPU DV ──────────────────────────────────
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

                # ── 8. Reconstitution finale ─────────────────────────────
                # ffmpeg multi-input :
                #   input 0 = HEVC injecté, input 1 = source principale,
                #   input 2+ = sources audio / sous-titres / attachements supplémentaires.
                signals.progress.emit("Reconstitution finale…")
                extra_sources: list[Path] = []
                for a in config.audio_tracks:
                    sp = a.source_path or config.source
                    if sp != config.source and sp not in extra_sources:
                        extra_sources.append(sp)
                for src_path, _idx in config.subtitle_tracks:
                    if src_path != config.source and src_path not in extra_sources:
                        extra_sources.append(src_path)
                for src_path, _idx in config.attachment_streams:
                    if src_path != config.source and src_path not in extra_sources:
                        extra_sources.append(src_path)

                # Indice ffmpeg : 0=HEVC, 1=source, 2+=extras
                def _inp(src_path: Path) -> int:
                    if src_path == config.source:
                        return 1
                    return 2 + extra_sources.index(src_path)

                recon_cmd = [self._ffmpeg, "-hide_banner", "-y",
                             "-i", str(current_hevc),
                             "-i", str(config.source)]
                for sp in extra_sources:
                    recon_cmd.extend(["-i", str(sp)])
                recon_cmd.extend(["-map", "0:v:0", "-c:v", "copy"])

                for i, a in enumerate(config.audio_tracks):
                    inp = _inp(a.source_path or config.source)
                    recon_cmd.extend(["-map", f"{inp}:{a.stream_index}"])
                    recon_cmd.extend(self._audio_codec_args(i, a))

                if config.subtitle_tracks:
                    for src_path, stream_idx in config.subtitle_tracks:
                        inp = _inp(src_path)
                        recon_cmd.extend(["-map", f"{inp}:{stream_idx}"])
                    recon_cmd.extend(["-c:s", "copy"])
                elif config.copy_subtitles:
                    n_inputs = 2 + len(extra_sources)
                    for inp_i in range(1, n_inputs):   # skip input 0 (HEVC brut, pas de subs)
                        recon_cmd.extend(["-map", f"{inp_i}:s?"])
                    recon_cmd.extend(["-c:s", "copy"])

                if config.attachment_streams:
                    for src_path, stream_idx in config.attachment_streams:
                        inp = _inp(src_path)
                        recon_cmd.extend(["-map", f"{inp}:{stream_idx}"])
                    recon_cmd.extend(["-c:t", "copy"])

                if config.keep_chapters:
                    recon_cmd.extend(["-map_chapters", "1"])   # chapitres depuis source

                recon_cmd.append(str(config.output))
                _run(recon_cmd)

                # ── 9. Post-traitements (balises MKV + métadonnées pistes) ────
                self._postproc(config, signals)

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
