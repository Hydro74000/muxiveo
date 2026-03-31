"""
core/inspector.py — Inspection et analyse de fichiers vidéo MKV/MP4.

Classes publiques :
    VideoTrack      — dataclass modélisant une piste vidéo
    AudioTrack      — dataclass modélisant une piste audio
    SubtitleTrack   — dataclass modélisant une piste de sous-titres
    ChapterTrack    — dataclass modélisant les chapitres
    FileInfo        — agrégat complet d'un fichier inspecté
    HDRType         — enum des formats HDR détectés
    FileInspector   — moteur d'inspection via ffprobe + mediainfo

Conventions :
    - Jamais shell=True
    - pathlib.Path pour tous les chemins
    - ffprobe pour l'inventaire des pistes (JSON)
    - mediainfo pour le frame count et les métadonnées HDR complémentaires
    - Toutes les méthodes publiques sont thread-safe (pas d'état mutable partagé)
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any

from core.lang_tags import Rfc5646LanguageTags


# =============================================================================
# Tags MKV standard (spec Matroska officielle)
# =============================================================================

#: Noms de balises MKV reconnus par la spec officielle Matroska.
#: Sert à distinguer les tags standard des tags propriétaires (préfixe _, etc.)
#: lors de l'affichage et de l'édition dans l'UI.
STANDARD_MKV_TAGS: frozenset[str] = frozenset({
    "TITLE", "SUBTITLE", "URL", "SYNOPSIS", "DESCRIPTION",
    "KEYWORDS", "SUMMARY", "COMMENT", "COLLECTION",
    "SEASON", "EPISODE", "PART_NUMBER",
    "DATE_RECORDED", "DATE_TAGGED", "DATE_RELEASED",
    "DATE_ENCODED", "DATE_WRITTEN", "DATE_PURCHASED",
    "ENCODER", "ENCODER_SETTINGS", "ORIGINAL",
    "DIRECTOR", "CAST", "GENRE", "MOOD",
    "RATING", "COUNTRY", "LANGUAGE",
    "LAW_RATING", "ICRA",
    "TOTAL_PARTS", "PART_OFFSET",
    "SORT_WITH", "INSTRUMENTS",
    "EMAIL", "PHONE", "FAX", "ADDRESS",
    "MEASURE", "TUNING",
    "REPLAY_GAIN_GAIN", "REPLAY_GAIN_PEAK",
    "POPULARITY_METER", "PLAY_COUNTER", "RATING",
    "SOURCE", "SOURCE_ID", "BPS", "DURATION",
    "NUMBER_OF_FRAMES", "NUMBER_OF_BYTES",
})


# =============================================================================
# Enum HDR
# =============================================================================

class HDRType(Enum):
    """
    Type de métadonnées HDR détecté dans le flux vidéo principal.

    Ordre de priorité (du plus riche au plus pauvre) :
        DOLBY_VISION_HDR10PLUS > DOLBY_VISION > HDR10PLUS > HDR10 > NONE
    """
    NONE                  = auto()
    HDR10                 = auto()
    HDR10PLUS             = auto()
    DOLBY_VISION          = auto()
    DOLBY_VISION_HDR10PLUS = auto()

    def label(self) -> str:
        """Libellé court pour l'affichage dans l'UI."""
        return {
            HDRType.NONE:                   "SDR",
            HDRType.HDR10:                  "HDR10",
            HDRType.HDR10PLUS:              "HDR10+",
            HDRType.DOLBY_VISION:           "Dolby Vision",
            HDRType.DOLBY_VISION_HDR10PLUS: "Dolby Vision + HDR10+",
        }[self]


# =============================================================================
# Dataclasses de pistes
# =============================================================================

@dataclass
class VideoTrack:
    """Piste vidéo extraite depuis ffprobe."""
    index:           int
    codec:           str          # "hevc", "h264", "av1"…
    codec_long:      str          # "H.265 / HEVC (High Efficiency Video Coding)"
    width:           int | None
    height:          int | None
    frame_rate:      str | None   # "23.976025" ou "24000/1001"
    bit_depth:       int | None   # 8, 10, 12
    color_space:     str | None   # "yuv420p10le"…
    color_primaries: str | None   # "bt2020"
    color_transfer:  str | None   # "smpte2084" (PQ), "arib-std-b67" (HLG)
    color_matrix:    str | None   # "bt2020nc"
    hdr_type:        HDRType      = HDRType.NONE
    language:        str | None   = None
    title:           str | None   = None
    duration_s:      float | None = None
    bit_rate:        int | None   = None   # bps
    raw:             dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def resolution(self) -> str:
        if self.width and self.height:
            return f"{self.width}×{self.height}"
        return "?"

    @property
    def is_hdr(self) -> bool:
        return self.hdr_type != HDRType.NONE


@dataclass
class AudioTrack:
    """Piste audio extraite depuis ffprobe."""
    index:        int
    codec:        str          # "truehd", "eac3", "dts", "aac"…
    codec_long:   str
    channels:     int | None   # 2, 6, 8
    channel_layout: str | None # "stereo", "5.1(side)", "7.1"
    sample_rate:  int | None   # Hz
    bit_rate:     int | None   # bps
    language:     str | None
    title:        str | None
    duration_s:   float | None = None
    raw:          dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def channels_label(self) -> str:
        """Libellé court des canaux : '7.1', '5.1', 'Stereo'…"""
        if self.channel_layout:
            return self.channel_layout
        match self.channels:
            case 8: return "7.1"
            case 6: return "5.1"
            case 2: return "Stereo"
            case 1: return "Mono"
            case _: return str(self.channels) if self.channels else "?"

    @property
    def atmos_flag(self) -> bool:
        """True si la piste contient une couche Atmos (TrueHD Atmos ou E-AC-3 JOC)."""
        profile    = (self.raw.get("profile") or "").lower()
        title      = (self.title       or "").lower()
        codec_long = self.codec_long.lower()
        return (
            "atmos"  in profile    or
            "atmos"  in title      or
            "atmos"  in codec_long or
            "joc"    in profile    or
            "joc"    in codec_long
        )

    @property
    def dtsx_flag(self) -> bool:
        """True si la piste est DTS:X (XLL X)."""
        if self.codec.lower() != "dts":
            return False
        profile    = (self.raw.get("profile") or "").lower()
        title      = (self.title       or "").lower()
        codec_long = self.codec_long.lower()
        return (
            "dts-x"  in profile    or
            "dts:x"  in profile    or
            "dtsx"   in profile    or
            "dts-x"  in title      or
            "dts:x"  in title      or
            "xll x"  in codec_long or
            "dts-x"  in codec_long
        )


@dataclass
class SubtitleTrack:
    """Piste de sous-titres extraite depuis ffprobe."""
    index:    int
    codec:    str        # "subrip", "ass", "hdmv_pgs_subtitle", "dvd_subtitle"…
    language: str | None
    title:    str | None
    forced:   bool = False
    default:  bool = False
    raw:      dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class ChapterInfo:
    """Informations sur les chapitres du fichier."""
    count:  int
    titles: list[str] = field(default_factory=list)


@dataclass
class AttachmentInfo:
    """
    Pièce jointe MKV (cover art, police de sous-titres, etc.).

    index       : index global ffprobe (utilisé pour -map dans ffmpeg).
    local_index : position 0-based parmi les attachements du fichier
                  (mkvmerge ID = local_index + 1).
    filename    : nom du fichier tel que stocké dans le MKV.
    mimetype    : type MIME (ex. "image/jpeg", "application/x-truetype-font").
    size_bytes  : taille en octets (None si non disponible via ffprobe).
    """
    index:       int
    local_index: int
    filename:    str
    mimetype:    str
    size_bytes:  int | None = None


@dataclass
class FileInfo:
    """
    Résultat complet de l'inspection d'un fichier.

    Agrège toutes les pistes et métadonnées issues de ffprobe et mediainfo.
    """
    path:       Path
    format:     str          # "matroska,webm", "mov,mp4,m4a,3gp,3g2,mj2"…
    duration_s: float | None
    size_bytes: int | None
    bit_rate:   int | None

    video_tracks:    list[VideoTrack]    = field(default_factory=list)
    audio_tracks:    list[AudioTrack]    = field(default_factory=list)
    subtitle_tracks: list[SubtitleTrack] = field(default_factory=list)
    attachments:     list[AttachmentInfo] = field(default_factory=list)
    chapters:        ChapterInfo | None  = None

    frame_count:  int | None        = None   # via mediainfo
    tag_count:    int               = 0      # nombre de balises MKV globales (via mkvmerge --identify)
    hdr_type:     HDRType           = HDRType.NONE  # du flux vidéo principal
    title:        str               = ""     # titre de segment (balise Title du conteneur)
    #: Balises MKV globales du conteneur (clés en MAJUSCULES, hors TITLE).
    #: Inclut les tags standard et propriétaires (ex. _PROGRAM_LABEL).
    #: Peuplé depuis ffprobe format.tags lors de l'inspection.
    global_tags:  dict[str, str]    = field(default_factory=dict)

    @property
    def primary_video(self) -> VideoTrack | None:
        return self.video_tracks[0] if self.video_tracks else None

    @property
    def size_human(self) -> str:
        if self.size_bytes is None:
            return "?"
        for unit, threshold in [("Go", 1 << 30), ("Mo", 1 << 20), ("Ko", 1 << 10)]:
            if self.size_bytes >= threshold:
                return f"{self.size_bytes / threshold:.2f} {unit}"
        return f"{self.size_bytes} o"

    @property
    def duration_human(self) -> str:
        if self.duration_s is None:
            return "?"
        h = int(self.duration_s // 3600)
        m = int((self.duration_s % 3600) // 60)
        s = int(self.duration_s % 60)
        return f"{h:02d}:{m:02d}:{s:02d}"


# =============================================================================
# Exceptions
# =============================================================================

class InspectionError(RuntimeError):
    """Échec de l'inspection d'un fichier."""
    def __init__(self, path: Path, reason: str) -> None:
        self.path   = path
        self.reason = reason
        super().__init__(f"Inspection échouée pour {path.name} : {reason}")


# =============================================================================
# FileInspector
# =============================================================================

class FileInspector:
    """
    Analyse un fichier MKV/MP4 via ffprobe et mediainfo.

    Toutes les méthodes sont synchrones et thread-safe.
    Pour une utilisation asynchrone depuis l'UI Qt, encapsuler dans
    un worker (QThread ou ThreadPoolExecutor).

    Usage :
        inspector = FileInspector()
        info = inspector.inspect(Path("/films/movie.mkv"))
        print(info.primary_video.resolution)
        print(info.hdr_type.label())
        print(info.frame_count)
    """

    # ------------------------------------------------------------------
    # Constructeur
    # ------------------------------------------------------------------

    def __init__(
        self,
        ffprobe_bin:   str = "ffprobe",
        mediainfo_bin: str = "mediainfo",
        mkvmerge_bin:  str = "mkvmerge",
    ) -> None:
        self._ffprobe   = ffprobe_bin
        self._mediainfo = mediainfo_bin
        self._mkvmerge  = mkvmerge_bin

    # ------------------------------------------------------------------
    # API publique
    # ------------------------------------------------------------------

    def inspect(self, path: Path) -> FileInfo:
        """
        Inspecte un fichier et retourne un FileInfo complet.

        Lève :
            InspectionError : si le fichier est illisible ou si ffprobe échoue.
        """
        if not path.is_file():
            raise InspectionError(path, "fichier introuvable")

        raw = self._run_ffprobe(path)
        info = self._parse_ffprobe(path, raw)

        # Enrichissement via mediainfo (non bloquant si absent)
        try:
            info.frame_count = self.get_frame_count(path)
        except Exception:
            pass  # mediainfo absent ou fichier non supporté — on continue

        # Enrichissement MKV via mkvmerge --identify (tag count + language_ietf)
        if "matroska" in info.format or "webm" in info.format:
            try:
                tag_count, ietf_langs = self._get_mkvmerge_track_data(path)
                info.tag_count = tag_count
                # Remplace les codes ISO 639-2 de ffprobe par les balises IETF
                # de mkvmerge, qui sont plus précises (ex : "en-US", "fr-FR").
                for track in (*info.video_tracks, *info.audio_tracks, *info.subtitle_tracks):
                    lang = ietf_langs.get(track.index)
                    if lang is not None:
                        track.language = lang if lang != "und" else None
            except Exception:
                pass  # mkvmerge absent ou erreur non bloquante
        else:
            # Pour les formats non-MKV (MP4, TS…), ffprobe renvoie de l'ISO 639-2.
            # On convertit en IETF BCP 47 pour garder un format homogène.
            for track in (*info.video_tracks, *info.audio_tracks, *info.subtitle_tracks):
                if not track.language:
                    continue
                converted = Rfc5646LanguageTags.from_iso639_2(track.language)
                if converted and converted != "und":
                    track.language = converted
                elif converted == "und" or not converted:
                    track.language = None

        # HDR du flux vidéo principal — réutilise le raw déjà parsé (évite 2e appel ffprobe)
        if info.primary_video:
            info.hdr_type = self._detect_hdr_from_raw(path, raw)
            info.primary_video.hdr_type = info.hdr_type

        return info

    def get_frame_count(self, path: Path) -> int | None:
        """
        Retourne le frame count via ``mediainfo --Inform=Video;%FrameCount%``.

        Retourne None si mediainfo est absent ou si la valeur est illisible.
        """
        try:
            result = subprocess.run(
                [self._mediainfo, "--Inform=Video;%FrameCount%", str(path)],
                capture_output=True,
                text=True,
                check=False,
                # shell=True JAMAIS
            )
            raw = result.stdout.strip()
            if re.fullmatch(r"\d+", raw):
                return int(raw)
        except FileNotFoundError:
            pass  # mediainfo absent
        return None

    def detect_hdr_type(self, path: Path) -> HDRType:
        """
        Détecte le type HDR du flux vidéo principal.

        Stratégie de détection (par ordre de priorité) :
            1. Dolby Vision + HDR10+ : color_transfer PQ + DoVi NAL + HDR10+ SEI
            2. Dolby Vision seul      : présence NAL units DoVi (via mediainfo)
            3. HDR10+                 : présence SEI MDCV + MaxSCL (via mediainfo)
            4. HDR10                  : color_transfer=smpte2084 + master_display
            5. NONE / SDR

        Retourne HDRType.NONE si la détection échoue.
        """
        try:
            raw = self._run_ffprobe(path)
        except InspectionError:
            return HDRType.NONE
        return self._detect_hdr_from_raw(path, raw)

    def _detect_hdr_from_raw(self, path: Path, raw: dict[str, Any]) -> HDRType:
        """
        Détecte le type HDR depuis un dict ffprobe déjà parsé.

        Utilisé en interne par inspect() pour éviter un second appel ffprobe.
        """
        video_streams = [s for s in raw.get("streams", []) if s.get("codec_type") == "video"]
        if not video_streams:
            return HDRType.NONE

        vs = video_streams[0]
        transfer   = vs.get("color_transfer", "")
        side_data  = vs.get("side_data_list", [])

        has_pq           = transfer in ("smpte2084", "smpte2084le")
        has_hlg          = transfer == "arib-std-b67"
        has_master_disp  = any(sd.get("side_data_type") == "Mastering display metadata" for sd in side_data)
        has_cll          = any(sd.get("side_data_type") == "Content light level metadata" for sd in side_data)
        has_dovi         = any(sd.get("side_data_type") == "DOVI configuration record" for sd in side_data)
        has_hdr10plus    = any(sd.get("side_data_type") == "HDR Dynamic Metadata SMPTE2094-40 (HDR10+)" for sd in side_data)

        # Fallback mediainfo pour DoVi et HDR10+ (ffprobe peut manquer certains streams)
        if not has_dovi or not has_hdr10plus:
            mi_dovi, mi_hdr10plus = self._mediainfo_hdr_flags(path)
            has_dovi      = has_dovi      or mi_dovi
            has_hdr10plus = has_hdr10plus or mi_hdr10plus

        # Priorité décroissante
        if has_dovi and has_hdr10plus:
            return HDRType.DOLBY_VISION_HDR10PLUS
        if has_dovi:
            return HDRType.DOLBY_VISION
        if has_hdr10plus:
            return HDRType.HDR10PLUS
        if has_pq and (has_master_disp or has_cll):
            return HDRType.HDR10
        if has_pq or has_hlg:
            # PQ/HLG sans métadonnées statiques : HDR10 incomplet mais présent
            return HDRType.HDR10

        return HDRType.NONE

    # ------------------------------------------------------------------
    # Appels externes
    # ------------------------------------------------------------------

    def _run_ffprobe(self, path: Path) -> dict[str, Any]:
        """Lance ffprobe et retourne le JSON parsé."""
        cmd = [
            self._ffprobe,
            "-v", "quiet",
            "-print_format", "json",
            "-show_streams",
            "-show_format",
            "-show_chapters",
            str(path),
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                # shell=True JAMAIS
            )
        except FileNotFoundError:
            raise InspectionError(path, "ffprobe introuvable dans PATH")

        if result.returncode != 0:
            stderr = result.stderr.strip()[-500:]
            raise InspectionError(path, f"ffprobe a échoué (code {result.returncode}) : {stderr}")

        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise InspectionError(path, f"Sortie ffprobe non parseable : {exc}")

    def _mediainfo_hdr_flags(self, path: Path) -> tuple[bool, bool]:
        """
        Retourne (has_dovi, has_hdr10plus) via mediainfo.

        Utilise deux appels --Inform ciblés pour minimiser la latence.
        Retourne (False, False) si mediainfo est absent.
        """
        try:
            # Dolby Vision : champ HDR_Format contient "Dolby Vision"
            r_dovi = subprocess.run(
                [self._mediainfo, "--Inform=Video;%HDR_Format%", str(path)],
                capture_output=True, text=True, check=False,
            )
            has_dovi = "dolby vision" in r_dovi.stdout.lower()

            # HDR10+ : champ HDR_Format_Compatibility contient "HDR10+"
            r_hdr10p = subprocess.run(
                [self._mediainfo, "--Inform=Video;%HDR_Format_Compatibility%", str(path)],
                capture_output=True, text=True, check=False,
            )
            has_hdr10plus = "hdr10+" in r_hdr10p.stdout.lower()

            return has_dovi, has_hdr10plus

        except FileNotFoundError:
            return False, False

    # ------------------------------------------------------------------
    # Parsing ffprobe
    # ------------------------------------------------------------------

    def _parse_ffprobe(self, path: Path, raw: dict[str, Any]) -> FileInfo:
        """Convertit la sortie brute ffprobe en FileInfo structuré."""
        fmt = raw.get("format", {})

        raw_tags = fmt.get("tags", {})
        # Normalise les clés en MAJUSCULES et exclut TITLE (déjà dans .title)
        global_tags: dict[str, str] = {
            k.upper(): str(v)
            for k, v in raw_tags.items()
            if k.upper() != "TITLE" and str(v).strip()
        }

        info = FileInfo(
            path        = path,
            format      = fmt.get("format_name", "?"),
            duration_s  = _float_or_none(fmt.get("duration")),
            size_bytes  = _int_or_none(fmt.get("size")),
            bit_rate    = _int_or_none(fmt.get("bit_rate")),
            title       = raw_tags.get("title", "") or raw_tags.get("TITLE", ""),
            global_tags = global_tags,
        )

        att_local_idx = 0
        for stream in raw.get("streams", []):
            codec_type = stream.get("codec_type", "")
            match codec_type:
                case "video":
                    # Les images de couverture (cover art) sont reportées par ffprobe
                    # avec codec_type="video" mais disposition.attached_pic=1.
                    # On les traite comme des attachements.
                    if stream.get("disposition", {}).get("attached_pic", 0):
                        info.attachments.append(
                            self._parse_attachment(stream, att_local_idx)
                        )
                        att_local_idx += 1
                    else:
                        info.video_tracks.append(self._parse_video(stream))
                case "audio":
                    info.audio_tracks.append(self._parse_audio(stream))
                case "subtitle":
                    info.subtitle_tracks.append(self._parse_subtitle(stream))
                case "attachment":
                    info.attachments.append(
                        self._parse_attachment(stream, att_local_idx)
                    )
                    att_local_idx += 1

        chapters = raw.get("chapters", [])
        if chapters:
            info.chapters = ChapterInfo(
                count  = len(chapters),
                titles = [c.get("tags", {}).get("title", "") for c in chapters],
            )

        return info

    def _parse_video(self, s: dict[str, Any]) -> VideoTrack:
        tags = s.get("tags", {})

        # Bit depth depuis pix_fmt (ex. "yuv420p10le" → 10)
        bit_depth: int | None = None
        pix_fmt = s.get("pix_fmt", "")
        if m := re.search(r"(\d+)(?:le|be)?$", pix_fmt):
            bd = int(m.group(1))
            bit_depth = bd if bd in (8, 10, 12, 16) else None

        # Frame rate : avg_frame_rate ou r_frame_rate
        frame_rate = s.get("avg_frame_rate") or s.get("r_frame_rate")
        if frame_rate in ("0/0", "0", None):
            frame_rate = None

        return VideoTrack(
            index           = s.get("index", 0),
            codec           = s.get("codec_name", "?"),
            codec_long      = s.get("codec_long_name", ""),
            width           = _int_or_none(s.get("width")),
            height          = _int_or_none(s.get("height")),
            frame_rate      = frame_rate,
            bit_depth       = bit_depth,
            color_space     = s.get("pix_fmt"),
            color_primaries = s.get("color_primaries"),
            color_transfer  = s.get("color_transfer"),
            color_matrix    = s.get("color_space"),
            language        = tags.get("language"),
            title           = tags.get("title"),
            duration_s      = _float_or_none(s.get("duration")),
            bit_rate        = _int_or_none(s.get("bit_rate")),
            raw             = s,
        )

    def _parse_audio(self, s: dict[str, Any]) -> AudioTrack:
        tags = s.get("tags", {})
        return AudioTrack(
            index          = s.get("index", 0),
            codec          = s.get("codec_name", "?"),
            codec_long     = s.get("codec_long_name", ""),
            channels       = _int_or_none(s.get("channels")),
            channel_layout = s.get("channel_layout"),
            sample_rate    = _int_or_none(s.get("sample_rate")),
            bit_rate       = _int_or_none(s.get("bit_rate")),
            language       = tags.get("language"),
            title          = tags.get("title"),
            duration_s     = _float_or_none(s.get("duration")),
            raw            = s,
        )

    def _parse_subtitle(self, s: dict[str, Any]) -> SubtitleTrack:
        tags        = s.get("tags", {})
        disposition = s.get("disposition", {})
        return SubtitleTrack(
            index    = s.get("index", 0),
            codec    = s.get("codec_name", "?"),
            language = tags.get("language"),
            title    = tags.get("title"),
            forced   = bool(disposition.get("forced", 0)),
            default  = bool(disposition.get("default", 0)),
            raw      = s,
        )

    def _parse_attachment(self, s: dict[str, Any], local_index: int) -> "AttachmentInfo":
        tags = s.get("tags", {})
        return AttachmentInfo(
            index       = s.get("index", 0),
            local_index = local_index,
            filename    = tags.get("filename", "attachment"),
            mimetype    = tags.get("mimetype", "application/octet-stream"),
            size_bytes  = _int_or_none(s.get("size")),
        )

    def _get_mkvmerge_track_data(
        self, path: Path
    ) -> tuple[int, dict[int, str]]:
        """
        Appelle ``mkvmerge --identify --identification-format json`` et retourne :
          - le nombre de balises MKV globales (int)
          - un dict {track_id: language_ietf} pour chaque piste

        Retourne (0, {}) si mkvmerge est absent ou si la sortie ne peut pas
        être parsée.
        """
        try:
            result = subprocess.run(
                [
                    self._mkvmerge,
                    "--identify", "--identification-format", "json",
                    str(path),
                ],
                capture_output=True, text=True, check=False, timeout=15,
            )
            if result.returncode not in (0, 1):   # 1 = warnings non bloquants
                return 0, {}
            data = json.loads(result.stdout)
            tag_count = sum(
                entry.get("num_entries", 0)
                for entry in data.get("global_tags", [])
            )
            lang_map: dict[int, str] = {}
            for track in data.get("tracks", []):
                tid = track.get("id")
                lang = track.get("properties", {}).get("language_ietf")
                if tid is not None and lang:
                    lang_map[tid] = lang
            return tag_count, lang_map
        except (FileNotFoundError, json.JSONDecodeError, Exception):
            return 0, {}


# =============================================================================
# Helpers
# =============================================================================

def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
