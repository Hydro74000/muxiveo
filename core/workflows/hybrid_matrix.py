"""Matrice d'hybridation multi-sources et plan d'assemblage intelligent.

Permet d'agréger plusieurs dossiers ou fichiers sources (vidéos, pistes audio isolées,
sous-titres externes) pour mixer une saison complète, même lorsque les donneurs
changent d'un épisode à l'autre (relais DVD/HDTV, épisodes manquants tolérés).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
import re
from typing import Any

from core.workflows.remux_models import RemuxConfig, SourceInput
from core.workflows.sync_calibration import SyncCalibration

# Extensions multimédia prises en charge
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".m2ts", ".webm"}
AUDIO_EXTENSIONS = {".mka", ".ac3", ".eac3", ".dts", ".dtshd", ".flac", ".aac", ".wav", ".m4a", ".mp3", ".opus"}
SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".sup"}
ALL_MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | AUDIO_EXTENSIONS | SUBTITLE_EXTENSIONS

# Expressions rationnelles pour extraire (saison, épisode)
_REGEX_SXX_EXX = re.compile(r"(?i)(?<![a-z0-9])s(\d{1,3})e(\d{1,3})(?!\d)")
_REGEX_NXN = re.compile(r"(?i)(?<![a-z0-9])(\d{1,2})x(\d{1,3})(?!\d)")
_REGEX_EP_STANDALONE = re.compile(r"(?i)(?<![a-z0-9])(?:ep|e)(\d{1,3})(?!\d)")
_REGEX_ANIME_NUM = re.compile(r"(?i)(?:[-_\s]|\[)(\d{2,3})(?:[-_\s\.]|\])")


def parse_episode_key(path: str | Path) -> tuple[int, int] | None:
    """Détecte le numéro de saison et d'épisode d'un fichier de manière tolérante."""
    name = Path(path).stem

    # 1. Format standard S01E02 ou s1e2
    match = _REGEX_SXX_EXX.search(name)
    if match:
        return int(match.group(1)), int(match.group(2))

    # 2. Format 1x02
    match = _REGEX_NXN.search(name)
    if match:
        return int(match.group(1)), int(match.group(2))

    # 3. Format E02 / EP02 (suppose saison 1 par défaut)
    match = _REGEX_EP_STANDALONE.search(name)
    if match:
        return 1, int(match.group(1))

    # 4. Format Anime numérotation absolue (ex: [Fansub] Titre - 05 [1080p])
    match = _REGEX_ANIME_NUM.search(name)
    if match:
        val = int(match.group(1))
        # Évite d'interpréter 720 ou 1080 comme un épisode
        if val not in {480, 576, 720, 1080, 2160}:
            return 1, val

    return None


class MatchingMode(str, Enum):
    EPISODE = "episode"  # Détection par numéros d'épisodes (SxxExx, 1x01...)
    FUZZY = "fuzzy"      # Détection par similarité de titre et année (films / anthologies)
    ORDER = "order"      # Tri naturel 1-à-1 (ordre alphabétique / numérique)
    MANUAL = "manual"    # Plan personnalisé manuel


_RELEASE_TAGS_REGEX = re.compile(
    r"(?i)\b(2160p|1080p|720p|480p|576p|uhd|4k|remux|bluray|bdrip|dvdrip|dvd|web-dl|webrip|web|hdtv|tvrip|"
    r"hevc|h264|h265|x264|x265|avc|dts-hd|dts|truehd|atmos|ac3|eac3|aac|flac|multi|truefrench|french|"
    r"vff|vfq|vfi|vo|vostfr|vost|subfrench|proper|repack|hdr|hdr10|hdr10plus|dv|dolby|vision|edition|director|cut|"
    r"extended|unrated|theatrical|imax|fr|en|jap)\b"
)
_YEAR_REGEX = re.compile(r"\b(19\d{2}|20\d{2})\b")


def clean_media_title(name: str) -> str:
    """Nettoie un nom de fichier pour isoler le titre et l'année sans les tags de release."""
    s = re.sub(r"\.[a-zA-Z0-9]{2,4}$", "", name)
    s = re.sub(r"[._\-+()\[\]{}]+", " ", s)
    s = _RELEASE_TAGS_REGEX.sub("", s)
    num_map = {
        "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
        "i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6",
        "vii": "7", "viii": "8", "ix": "9", "x": "10",
    }
    words = [num_map.get(w, w) for w in s.casefold().split()]
    return " ".join(words).strip()


def extract_media_year(name: str) -> int | None:
    """Extrait l'année (1920-2099) d'un nom de média si présente."""
    matches = list(_YEAR_REGEX.finditer(name))
    if matches:
        return int(matches[-1].group(1))
    return None


def natural_sort_key(name: str | Path) -> list:
    """Clé de tri naturel prenant en compte les nombres dans les chaînes."""
    text = str(Path(name).name)
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", text)]


def calculate_title_similarity(name1: str, name2: str) -> float:
    """Calcule un score de similarité entre 0.0 et 1.0 entre deux noms de fichiers."""
    import difflib
    c1 = clean_media_title(name1)
    c2 = clean_media_title(name2)
    if not c1 or not c2:
        return 0.0
    if c1 == c2:
        return 1.0

    ratio = difflib.SequenceMatcher(None, c1, c2).ratio()

    # Bonus / malus d'année
    y1 = extract_media_year(name1)
    y2 = extract_media_year(name2)
    if y1 and y2:
        if y1 == y2:
            ratio = min(1.0, ratio + 0.15)
        else:
            ratio = max(0.0, ratio - 0.4)

    # Token overlap bonus (Jaccard)
    t1 = set(c1.split())
    t2 = set(c2.split())
    if t1 and t2:
        jaccard = len(t1 & t2) / len(t1 | t2)
        ratio = 0.5 * ratio + 0.5 * jaccard

    return ratio


class SourceRole(str, Enum):
    MASTER = "master"            # Vidéo de référence, chapitres et VO
    DONOR = "donor"              # Donneur généraliste (audio et/ou sous-titres)
    DONOR_AUDIO = "donor_audio"  # Donneur spécifique pour l'audio (ex: VF)
    DONOR_SUBTITLE = "donor_sub" # Donneur spécifique pour les sous-titres (ex: ST FR)
    AUTO = "auto"                # Détection automatique selon le type de fichier


@dataclass
class MatrixSource:
    """Définition d'une source dans la matrice (dossier ou fichier unique)."""
    source_id: str
    path: Path
    role: SourceRole = SourceRole.AUTO
    label: str = ""
    priority: int = 0  # Priorité plus haute = utilisé en priorité pour un même rôle

    def __post_init__(self):
        self.path = Path(self.path).expanduser().resolve()
        if not self.label:
            self.label = self.path.name or str(self.path)

    @property
    def is_dir(self) -> bool:
        return self.path.is_dir()

    def discover_files(self) -> dict[tuple[int, int], Path]:
        """Retourne un dictionnaire {(saison, episode): chemin} des fichiers compatibles."""
        indexed: dict[tuple[int, int], Path] = {}
        if not self.path.exists():
            return indexed

        if self.path.is_file():
            if self.path.suffix.lower() in ALL_MEDIA_EXTENSIONS:
                key = parse_episode_key(self.path)
                if key:
                    indexed[key] = self.path
            return indexed

        for item in sorted(self.path.iterdir(), key=lambda p: p.name.casefold()):
            if not item.is_file():
                continue
            if item.suffix.lower() not in ALL_MEDIA_EXTENSIONS:
                continue
            key = parse_episode_key(item)
            if key and key not in indexed:
                indexed[key] = item.resolve()

        return indexed

    def discover_all_files(self) -> list[Path]:
        """Retourne tous les fichiers médias compatibles de la source, triés naturellement."""
        if not self.path.exists():
            return []
        if self.path.is_file():
            if self.path.suffix.lower() in ALL_MEDIA_EXTENSIONS:
                return [self.path]
            return []
        files = [p.resolve() for p in self.path.iterdir() if p.is_file() and p.suffix.lower() in ALL_MEDIA_EXTENSIONS]
        return sorted(files, key=natural_sort_key)


@dataclass
class MatrixEpisode:
    """Représente un élément ou épisode assemblé à partir des différentes sources."""
    season: int = 0
    episode: int = 0
    item_key: str = ""
    master_file: Path | None = None
    donor_files: list[tuple[MatrixSource, Path]] = field(default_factory=list)
    calibration: SyncCalibration | None = None
    shift_ms: float = 0.0
    confidence: float = 1.0
    segments_count: int = 1
    cadence_mismatch: Any = None
    status: str = "pending"  # "ready", "partial", "error", "done"
    status_message: str = ""

    @property
    def key(self) -> tuple[int, int] | str:
        if self.item_key:
            return self.item_key
        return self.season, self.episode

    @property
    def display_name(self) -> str:
        if self.item_key:
            return self.item_key
        if self.season > 0 or self.episode > 0:
            return f"S{self.season:02d}E{self.episode:02d}"
        if self.master_file:
            return self.master_file.stem
        return "Élément"

    @property
    def is_complete(self) -> bool:
        return self.master_file is not None and len(self.donor_files) > 0

    @property
    def all_paths(self) -> list[Path]:
        paths: list[Path] = []
        if self.master_file:
            paths.append(self.master_file)
        for _, path in self.donor_files:
            if path not in paths:
                paths.append(path)
        return paths


@dataclass
class HybridRecipe:
    """Recette d'assemblage déterminant quelles pistes conserver, synchroniser et ordonner."""
    keep_master_video: bool = True
    keep_master_chapters: bool = True
    keep_master_audio_langs: list[str] = field(default_factory=lambda: ["eng", "jpn", "orig"])
    donor_audio_langs: list[str] = field(default_factory=lambda: ["fre", "fra"])
    donor_sub_langs: list[str] = field(default_factory=lambda: ["fre", "fra"])
    exclude_languages: list[str] = field(default_factory=list)
    sync_mode: str = "physical"  # "physical" ou "container"
    sync_subtitles: str = "mirror"  # "mirror" ou "none"
    crossfade_ms: int = 80
    cadence_auto_apply: bool = True
    cadence_audio_method: str = "auto"
    output_tag: str = "Hybrid"

    @classmethod
    def from_witness_config(cls, remux_config: RemuxConfig, master_index: int = 0) -> HybridRecipe:
        """Déduit automatiquement la recette à partir de la configuration choisie sur un épisode témoin."""
        recipe = cls()
        if not remux_config.sources:
            return recipe

        master_source = remux_config.sources[master_index] if len(remux_config.sources) > master_index else None
        donor_sources = [s for i, s in enumerate(remux_config.sources) if i != master_index]

        recipe.sync_mode = getattr(remux_config, "sync_mode", "physical") or "physical"
        recipe.sync_subtitles = getattr(remux_config, "sync_subtitles", "mirror") or "mirror"
        recipe.crossfade_ms = getattr(remux_config, "crossfade_ms", 80) or 80
        recipe.cadence_auto_apply = getattr(remux_config, "cadence_auto_apply", True)
        recipe.cadence_audio_method = getattr(remux_config, "cadence_audio_method", "auto") or "auto"

        # Pistes du master retenues
        if master_source:
            recipe.keep_master_video = any(t.track_type == "video" and t.enabled for t in master_source.tracks)
            recipe.keep_master_audio_langs = [
                t.language for t in master_source.tracks if t.track_type == "audio" and t.enabled and t.language
            ]

        # Pistes des donneurs retenues
        recipe.donor_audio_langs = []
        recipe.donor_sub_langs = []
        for donor in donor_sources:
            for t in donor.tracks:
                if t.enabled and t.language:
                    if t.track_type == "audio" and t.language not in recipe.donor_audio_langs:
                        recipe.donor_audio_langs.append(t.language)
                    elif t.track_type == "subtitle" and t.language not in recipe.donor_sub_langs:
                        recipe.donor_sub_langs.append(t.language)

        return recipe


class HybridMatrix:
    """Gestionnaire d'appariement multi-sources pour une saison ou une collection de films."""

    def __init__(self, matching_mode: MatchingMode = MatchingMode.EPISODE) -> None:
        self.sources: list[MatrixSource] = []
        self.recipe: HybridRecipe = HybridRecipe()
        self.matching_mode: MatchingMode = matching_mode

    def add_source(
        self,
        path: str | Path,
        role: SourceRole = SourceRole.AUTO,
        label: str = "",
        priority: int = 0,
    ) -> MatrixSource:
        source_id = f"src_{len(self.sources) + 1}"
        source = MatrixSource(source_id=source_id, path=Path(path), role=role, label=label, priority=priority)
        self.sources.append(source)
        return source

    def clear(self) -> None:
        self.sources.clear()

    def get_all_donor_candidates(self) -> list[tuple[MatrixSource, Path]]:
        """Retourne la liste de tous les fichiers donneurs disponibles dans les sources donneuses."""
        candidates: list[tuple[MatrixSource, Path]] = []
        master_src = next((s for s in self.sources if s.role == SourceRole.MASTER), None)
        donor_srcs = [s for s in self.sources if s != master_src]
        for src in donor_srcs:
            for p in src.discover_all_files():
                candidates.append((src, p))
        return candidates

    def get_all_master_candidates(self) -> list[Path]:
        """Retourne la liste de tous les fichiers master disponibles."""
        master_src = next((s for s in self.sources if s.role == SourceRole.MASTER), None)
        if not master_src and self.sources:
            master_src = self.sources[0]
        if master_src:
            return master_src.discover_all_files()
        return []

    def scan(self) -> list[MatrixEpisode]:
        """Scanne toutes les sources et génère la liste des éléments appariés selon le mode configuré."""
        if not self.sources:
            return []

        # Identifier la source Master (la première marquée MASTER, ou la première source enregistrée)
        master_src = next((s for s in self.sources if s.role == SourceRole.MASTER), None)
        if not master_src:
            master_src = self.sources[0]

        donor_srcs = [s for s in self.sources if s != master_src]

        if self.matching_mode == MatchingMode.FUZZY:
            return self._scan_by_fuzzy(master_src, donor_srcs)
        elif self.matching_mode == MatchingMode.ORDER:
            return self._scan_by_order(master_src, donor_srcs)
        elif self.matching_mode == MatchingMode.MANUAL:
            return self._scan_manual(master_src, donor_srcs)
        else:
            return self._scan_by_episode(master_src, donor_srcs)

    def _scan_by_episode(self, master_src: MatrixSource, donor_srcs: list[MatrixSource]) -> list[MatrixEpisode]:
        """Mode 1 : Appariement tolérant basé sur la numérotation des saisons/épisodes (SxxExx)."""
        master_index = master_src.discover_files()
        donors_indexes: list[tuple[MatrixSource, dict[tuple[int, int], Path]]] = [
            (src, src.discover_files()) for src in donor_srcs
        ]

        all_keys: set[tuple[int, int]] = set(master_index.keys())
        for _, d_index in donors_indexes:
            all_keys.update(d_index.keys())

        episodes: list[MatrixEpisode] = []
        for key in sorted(all_keys):
            season, ep = key
            master_file = master_index.get(key)
            donors_for_ep: list[tuple[MatrixSource, Path]] = []

            sorted_donors = sorted(donors_indexes, key=lambda pair: pair[0].priority, reverse=True)
            for d_src, d_index in sorted_donors:
                if key in d_index:
                    donors_for_ep.append((d_src, d_index[key]))

            if master_file and donors_for_ep:
                status = "ready"
                msg = f"Prêt ({len(donors_for_ep)} donneur{'s' if len(donors_for_ep) > 1 else ''})"
            elif not master_file:
                status = "partial"
                msg = "Fichier master manquant"
            else:
                status = "partial"
                msg = "Donneur manquant"

            episodes.append(
                MatrixEpisode(
                    season=season,
                    episode=ep,
                    master_file=master_file,
                    donor_files=donors_for_ep,
                    status=status,
                    status_message=msg,
                )
            )

        return episodes

    def _scan_by_fuzzy(self, master_src: MatrixSource, donor_srcs: list[MatrixSource]) -> list[MatrixEpisode]:
        """Mode 2 : Appariement intelligent par similarité de titre et année (films / collections)."""
        master_files = master_src.discover_all_files()
        donors_files_by_src: list[tuple[MatrixSource, list[Path]]] = [
            (src, src.discover_all_files()) for src in donor_srcs
        ]

        if not master_files and not any(files for _, files in donors_files_by_src):
            return []

        # Association pour chaque source donneuse indépendamment
        # master_idx -> list of (donor_src, donor_path)
        matched_donors: dict[int, list[tuple[MatrixSource, Path]]] = {i: [] for i in range(len(master_files))}
        unused_donors_by_src: dict[MatrixSource, list[Path]] = {}

        for d_src, d_files in donors_files_by_src:
            if not d_files:
                continue

            scores: list[tuple[float, int, int]] = []
            for m_idx, m_path in enumerate(master_files):
                for d_idx, d_path in enumerate(d_files):
                    score = calculate_title_similarity(m_path.name, d_path.name)
                    scores.append((score, m_idx, d_idx))

            scores.sort(key=lambda item: item[0], reverse=True)
            used_m: set[int] = set()
            used_d: set[int] = set()

            threshold = 0.15 if (len(master_files) == 1 and len(d_files) == 1) else 0.30
            for score, m_idx, d_idx in scores:
                if m_idx not in used_m and d_idx not in used_d:
                    if score >= threshold:
                        used_m.add(m_idx)
                        used_d.add(d_idx)
                        matched_donors[m_idx].append((d_src, d_files[d_idx]))

            unused_d = [d_files[d_idx] for d_idx in range(len(d_files)) if d_idx not in used_d]
            if unused_d:
                unused_donors_by_src[d_src] = unused_d

        episodes: list[MatrixEpisode] = []

        # 1. Éléments avec master
        for m_idx, m_path in enumerate(master_files):
            donors_for_ep = matched_donors.get(m_idx, [])
            if donors_for_ep:
                status = "ready"
                msg = f"Prêt ({len(donors_for_ep)} donneur{'s' if len(donors_for_ep) > 1 else ''})"
            else:
                status = "partial"
                msg = "Donneur manquant"

            parsed = parse_episode_key(m_path)
            season = parsed[0] if parsed else 0
            ep_num = parsed[1] if parsed else 0

            episodes.append(
                MatrixEpisode(
                    season=season,
                    episode=ep_num,
                    item_key=m_path.stem,
                    master_file=m_path,
                    donor_files=donors_for_ep,
                    status=status,
                    status_message=msg,
                )
            )

        # 2. Donneurs orphelins (fichiers donneurs sans master correspondant)
        for d_src, u_files in unused_donors_by_src.items():
            for d_path in u_files:
                parsed = parse_episode_key(d_path)
                season = parsed[0] if parsed else 0
                ep_num = parsed[1] if parsed else 0
                episodes.append(
                    MatrixEpisode(
                        season=season,
                        episode=ep_num,
                        item_key=d_path.stem,
                        master_file=None,
                        donor_files=[(d_src, d_path)],
                        status="partial",
                        status_message="Fichier master manquant",
                    )
                )

        return episodes

    def _scan_by_order(self, master_src: MatrixSource, donor_srcs: list[MatrixSource]) -> list[MatrixEpisode]:
        """Mode 3 : Appariement séquentiel 1-à-1 selon l'ordre naturel des fichiers."""
        master_files = master_src.discover_all_files()
        donors_files_by_src: list[tuple[MatrixSource, list[Path]]] = [
            (src, src.discover_all_files()) for src in donor_srcs
        ]

        max_len = len(master_files)
        for _, d_files in donors_files_by_src:
            max_len = max(max_len, len(d_files))

        if max_len == 0:
            return []

        episodes: list[MatrixEpisode] = []
        for i in range(max_len):
            m_path = master_files[i] if i < len(master_files) else None
            donors_for_ep: list[tuple[MatrixSource, Path]] = []
            for d_src, d_files in donors_files_by_src:
                if i < len(d_files):
                    donors_for_ep.append((d_src, d_files[i]))

            if m_path and donors_for_ep:
                status = "ready"
                msg = f"Prêt ({len(donors_for_ep)} donneur{'s' if len(donors_for_ep) > 1 else ''})"
            elif not m_path:
                status = "partial"
                msg = "Fichier master manquant"
            else:
                status = "partial"
                msg = "Donneur manquant"

            parsed = parse_episode_key(m_path) if m_path else (parse_episode_key(donors_for_ep[0][1]) if donors_for_ep else None)
            season = parsed[0] if parsed else 0
            ep_num = parsed[1] if parsed else 0
            item_key = m_path.stem if m_path else (donors_for_ep[0][1].stem if donors_for_ep else f"Item {i + 1}")

            episodes.append(
                MatrixEpisode(
                    season=season,
                    episode=ep_num,
                    item_key=item_key,
                    master_file=m_path,
                    donor_files=donors_for_ep,
                    status=status,
                    status_message=msg,
                )
            )

        return episodes

    def _scan_manual(self, master_src: MatrixSource, donor_srcs: list[MatrixSource]) -> list[MatrixEpisode]:
        """Mode 4 : Plan personnalisé - initialisé par similarité ou ordre pour être ajusté manuellement."""
        episodes = self._scan_by_fuzzy(master_src, donor_srcs)
        if not episodes:
            episodes = self._scan_by_order(master_src, donor_srcs)
        return episodes


def _extract_source_video_fps(source_input: SourceInput) -> str | float | None:
    for track in source_input.tracks:
        if track.track_type == "video":
            if getattr(track, "frame_rate", ""):
                return track.frame_rate
            if track.display_info:
                match = re.search(r"([\d\.]+)\s*fps", track.display_info, re.I)
                if match:
                    return match.group(1)
    return None


def prepare_matrix_episode(
    episode: MatrixEpisode,
    recipe: HybridRecipe,
    output_dir: Path | str,
    config: Any,
    options: Any,
    logger: Any,
    detect_cuts: bool = True,
    drift_threshold_ms: int = 25,
) -> tuple[RemuxConfig, SyncCalibration | None]:
    """Construit la configuration remux et effectue la calibration synchro pour un épisode multi-sources."""
    from cli.remux_config import build_remux_config
    from core.workflows.audio_sync import AudioSyncTrack
    from core.workflows.audio_sync_scan import AudioSyncScanner
    from core.workflows.subtitle_sync_scan import SubtitleSyncScanner

    if not episode.master_file:
        raise ValueError(f"Épisode {episode.display_name} sans fichier master.")
    if not episode.donor_files:
        raise ValueError(f"Épisode {episode.display_name} sans donneur.")

    outdir = Path(output_dir).expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    out_name = f"{episode.master_file.stem}.{recipe.output_tag}.mkv"
    output_path = outdir / out_name

    source_paths = [str(episode.master_file)] + [str(path) for _, path in episode.donor_files]
    job_payload = {
        "sources": source_paths,
        "output": str(output_path),
        "mux_backend": "native",
        "sync_mode": recipe.sync_mode,
        "sync_subtitles": recipe.sync_subtitles,
        "crossfade_ms": recipe.crossfade_ms,
        "clean_nfo": True,
        "chapters": {"source_index": 0},
        "_allow_missing_output_dir": True,
    }

    remux_config = build_remux_config(job_payload, config, options, logger)

    # 1. Source 0 : Master (Vidéo, VO, Chapitres)
    master_source = remux_config.sources[0]
    for track in master_source.tracks:
        if track.track_type == "video":
            track.enabled = recipe.keep_master_video
        elif track.track_type == "audio":
            if recipe.keep_master_audio_langs:
                track.enabled = (track.language in recipe.keep_master_audio_langs) or not any(
                    t.language in recipe.keep_master_audio_langs for t in master_source.tracks if t.track_type == "audio"
                )
        elif track.track_type == "subtitle":
            track.enabled = track.language not in recipe.donor_sub_langs

    ref_audio = next((t for t in master_source.tracks if t.track_type == "audio"), None)
    ref_sub = next((t for t in master_source.tracks if t.track_type == "subtitle"), None)

    # 2. Sources 1..N : Donneurs (Audio VF, Sous-titres)
    calibrations: dict[str, dict] = {}
    primary_calibration: SyncCalibration | None = None

    ffmpeg_bin = getattr(options, "ffmpeg", None) or config.tool_ffmpeg
    ffprobe_bin = getattr(options, "ffprobe", None) or config.tool_ffprobe
    audio_scanner = AudioSyncScanner(ffmpeg_bin, ffprobe_bin)
    sub_scanner = SubtitleSyncScanner(ffmpeg_bin, ffprobe_bin)

    for donor_idx, donor_source in enumerate(remux_config.sources[1:], start=1):
        donor_calib: SyncCalibration | None = None

        for track in donor_source.tracks:
            if track.track_type == "video":
                track.enabled = False
            elif track.track_type == "audio":
                if recipe.donor_audio_langs:
                    track.enabled = track.language in recipe.donor_audio_langs or not any(
                        t.language in recipe.donor_audio_langs for t in donor_source.tracks if t.track_type == "audio"
                    )
                else:
                    track.enabled = True
            elif track.track_type == "subtitle":
                if recipe.donor_sub_langs:
                    track.enabled = track.language in recipe.donor_sub_langs
                else:
                    track.enabled = True

        # Détection de cadence A (métadonnées vidéo entre master et donneur)
        cadence_mismatch = None
        cadence_auto_apply = getattr(recipe, "cadence_auto_apply", True)
        cadence_audio_method = getattr(recipe, "cadence_audio_method", "auto") or "auto"

        m_fps = _extract_source_video_fps(master_source)
        d_fps = _extract_source_video_fps(donor_source)
        if m_fps and d_fps:
            from core.workflows.cadence import detect_cadence_from_metadata
            cadence_mismatch = detect_cadence_from_metadata(m_fps, d_fps)
            if cadence_mismatch:
                logger.emit(
                    "info",
                    f"[{episode.display_name}] Différence de cadence vidéo détectée (Méthode A) : {cadence_mismatch.description}"
                )

        # Calibration du donneur contre le master
        target_audio = next((t for t in donor_source.tracks if t.track_type == "audio" and t.enabled), None)
        target_sub = next((t for t in donor_source.tracks if t.track_type == "subtitle" and t.enabled), None)

        if target_audio and ref_audio:
            logger.emit("info", f"[{episode.display_name}] Synchronisation audio donneur #{donor_idx}…")
            donor_calib = audio_scanner.scan(
                AudioSyncTrack(master_source.path, ref_audio.mkv_tid),
                AudioSyncTrack(donor_source.path, target_audio.mkv_tid),
                detect_cuts=detect_cuts,
                drift_threshold_ms=drift_threshold_ms,
                cadence_mismatch=cadence_mismatch if cadence_auto_apply else None,
                cadence_audio_method=cadence_audio_method,
                log=lambda msg: logger.emit("info", f"[{episode.display_name}] {msg}"),
            )
        elif target_sub:
            logger.emit("info", f"[{episode.display_name}] Synchronisation sous-titre donneur #{donor_idx}…")
            if ref_sub:
                donor_calib = sub_scanner.scan(
                    master_source.path, ref_sub.mkv_tid,
                    donor_source.path, target_sub.mkv_tid,
                    is_ref_sub=True,
                    detect_cuts=detect_cuts,
                    log=lambda msg: logger.emit("info", f"[{episode.display_name}] {msg}"),
                )
            elif ref_audio:
                donor_calib = sub_scanner.scan(
                    master_source.path, ref_audio.mkv_tid,
                    donor_source.path, target_sub.mkv_tid,
                    is_ref_sub=False,
                    detect_cuts=False,
                    log=lambda msg: logger.emit("info", f"[{episode.display_name}] {msg}"),
                )

        if donor_calib:
            calibrations[str(donor_idx)] = donor_calib.to_dict()
            if primary_calibration is None:
                primary_calibration = donor_calib
            has_cadence = bool(donor_calib.cadence_mismatch and getattr(donor_calib.cadence_mismatch, "cadence_type", None) not in (None, "none"))
            if has_cadence and cadence_auto_apply:
                remux_config.sync_mode = "physical"
            elif recipe.sync_mode != "physical" and len(donor_calib.segments) == 1:
                for t in donor_source.tracks:
                    if t.enabled:
                        t.time_shift_ms = round(donor_calib.segments[0].shift_ms)

    remux_config.sync_calibrations = calibrations

    # 3. Établir l'ordre des pistes harmonisé
    final_order: list[tuple[int, int, str]] = []
    # Master Video d'abord
    for t in master_source.tracks:
        if t.track_type == "video" and t.enabled:
            final_order.append((0, t.mkv_tid, t.entry_id))

    # Donor Audio
    for d_idx, d_src in enumerate(remux_config.sources[1:], start=1):
        for t in d_src.tracks:
            if t.track_type == "audio" and t.enabled:
                final_order.append((d_idx, t.mkv_tid, t.entry_id))

    # Master Audio
    for t in master_source.tracks:
        if t.track_type == "audio" and t.enabled:
            final_order.append((0, t.mkv_tid, t.entry_id))

    # Donor Subtitles
    for d_idx, d_src in enumerate(remux_config.sources[1:], start=1):
        for t in d_src.tracks:
            if t.track_type == "subtitle" and t.enabled:
                final_order.append((d_idx, t.mkv_tid, t.entry_id))

    # Master Subtitles
    for t in master_source.tracks:
        if t.track_type == "subtitle" and t.enabled:
            final_order.append((0, t.mkv_tid, t.entry_id))

    remux_config.track_order = final_order
    episode.calibration = primary_calibration
    if primary_calibration:
        episode.shift_ms = primary_calibration.segments[0].shift_ms
        episode.segments_count = len(primary_calibration.segments)
        episode.confidence = primary_calibration.confidence
        episode.cadence_mismatch = primary_calibration.cadence_mismatch

    return remux_config, primary_calibration
