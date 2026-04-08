"""
core/workflows/remux.py — Workflow de remuxage MKV sans réencodage.

Classes publiques :
    TrackEntry           — piste avec état d'inclusion et métadonnées éditables
    SourceInput          — un fichier source avec ses pistes associées
    RemuxConfig          — configuration complète d'un remuxage (multi-source)
    RemuxWorkflow        — construit et exécute la commande mkvmerge
    RemuxError           — exception levée par le workflow
    tracks_from_file_info — fabrique une liste de TrackEntry depuis un FileInfo

Conventions :
    - Jamais shell=True
    - pathlib.Path pour tous les chemins
    - mkvmerge uniquement (pas de ffmpeg)
    - Signaux Qt thread-safe (QueuedConnection) pour la communication vers l'UI
    - Hypothèse : les index ffprobe correspondent aux TID mkvmerge pour les MKV
"""

from __future__ import annotations

import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

from PySide6.QtCore import QObject, Signal
from core.lang_tags import Rfc5646LanguageTags as LangTags
from core.inspector import AttachmentInfo, ChapterEntry, FileInfo, HDRType, build_chapter_xml
from core.runner import TaskCancelledError, TaskSignals, ToolRunner
from core.subprocess_utils import subprocess_text_kwargs


def _cli_path(path: Path) -> str:
    """
    Normalise les chemins CLI avec des slashs pour garder des commandes stables
    entre Linux/macOS/Windows. mkvmerge et mkvpropedit les acceptent.
    """
    return path.as_posix()


# =============================================================================
# Modèle de piste
# =============================================================================

@dataclass
class TrackEntry:
    """
    Représente une piste avec son état d'inclusion et ses métadonnées éditables.

    mkv_tid est l'identifiant mkvmerge (= index ffprobe pour les fichiers MKV).
    file_id est l'identifiant UUID du SourceFile parent (géré par l'UI).
    orig_language / orig_title conservent les valeurs d'origine pour détecter
    les modifications et n'émettre des flags que si nécessaire.
    """

    mkv_tid:      int
    track_type:   str           # "video" | "audio" | "subtitle"
    codec:        str           # libellé court ("HEVC", "TRUEHD", ...)
    display_info: str           # résolution, canaux, flags... — lecture seule
    language:     str           # tag BCP-47 éditable ("fra", "eng", ...)
    title:        str           # titre de piste éditable
    enabled:      bool = True
    file_id:      str  = ""    # UUID du SourceFile parent (usage UI uniquement)

    orig_language: str = field(default="", repr=False)
    orig_title:    str = field(default="", repr=False)

    # Flags MKV éditables (transmis à mkvmerge uniquement si modifiés)
    flag_enabled:          bool = field(default=True,  repr=False)  # --track-enabled-flag
    flag_default:          bool = field(default=False, repr=False)  # --default-track-flag
    flag_forced:           bool = field(default=False, repr=False)  # --forced-track
    flag_hearing_impaired: bool = field(default=False, repr=False)  # --hearing-impaired-flag
    flag_visual_impaired:  bool = field(default=False, repr=False)  # --visual-impaired-flag
    flag_original:         bool = field(default=False, repr=False)  # --original-flag
    flag_commentary:       bool = field(default=False, repr=False)  # --commentary-flag

    orig_flag_enabled:          bool = field(default=True,  repr=False)
    orig_flag_default:          bool = field(default=False, repr=False)
    orig_flag_forced:           bool = field(default=False, repr=False)
    orig_flag_hearing_impaired: bool = field(default=False, repr=False)
    orig_flag_visual_impaired:  bool = field(default=False, repr=False)
    orig_flag_original:         bool = field(default=False, repr=False)
    orig_flag_commentary:       bool = field(default=False, repr=False)

    @property
    def flags_label(self) -> str:
        """Résumé court des flags actifs (pour la colonne Info du tableau)."""
        parts: list[str] = []
        if not self.flag_enabled:
            parts.append("désact.")
        if self.flag_default:
            parts.append("défaut")
        if self.flag_forced:
            parts.append("forcé")
        if self.flag_hearing_impaired:
            parts.append("malent.")
        if self.flag_visual_impaired:
            parts.append("malvoy.")
        if self.flag_original:
            parts.append("orig.")
        if self.flag_commentary:
            parts.append("comm.")
        return "  ·  ".join(parts)

    @property
    def full_info_label(self) -> str:
        """Info technique + flags actifs (affichage colonne Info)."""
        parts = [p for p in (self.display_info, self.flags_label) if p]
        return "  ·  ".join(parts)

    @property
    def type_label(self) -> str:
        """Lettre courte pour l'affichage dans le tableau."""
        match self.track_type:
            case "video":    return "V"
            case "audio":    return "A"
            case "subtitle": return "S"
            case _:          return "?"

    @property
    def type_long(self) -> str:
        """Libellé long du type de piste."""
        match self.track_type:
            case "video":    return "Vidéo"
            case "audio":    return "Audio"
            case "subtitle": return "Sous-titre"
            case _:          return self.track_type


# =============================================================================
# Source d'entrée
# =============================================================================

@dataclass
class SourceInput:
    """
    Un fichier source avec toutes ses pistes (activées ou non).

    file_index est l'indice 0-based de ce fichier dans RemuxConfig.sources ;
    il est utilisé dans --track-order pour référencer les pistes.
    """

    path:                    Path
    file_index:              int                    # 0-based, --track-order
    tracks:                  list[TrackEntry]       # toutes les pistes (enabled ou non)
    # Attachements sélectionnés (vide = aucun).
    # Chaque AttachmentInfo.local_index + 1 = ID mkvmerge.
    # AttachmentInfo.index = stream index ffprobe pour ffmpeg -map.
    selected_attachments:    list[AttachmentInfo]   = field(default_factory=list)
    attachment_count:        int                    = 0   # total dans le fichier source
    copy_tags:               bool                   = False


# =============================================================================
# Configuration de remuxage
# =============================================================================

@dataclass
class RemuxConfig:
    """
    Configuration complète d'un remuxage multi-source.

    sources           : liste ordonnée des fichiers source (chacun avec ses pistes).
    track_order       : liste de (file_index, mkv_tid) dans l'ordre désiré en sortie.
                        Seules les pistes présentes dans track_order sont incluses.
    extra_attachments : fichiers externes à attacher en plus (--attach-file).
    """

    sources:             list[SourceInput]
    output:              Path
    track_order:         list[tuple[int, int]]   # (file_index, mkv_tid) ordonnés
    keep_chapters:       bool          = True
    #: None  → mkvmerge copie les chapitres des sources (comportement par défaut).
    #: list  → on passe un XML de chapitres à mkvmerge (--chapters) et on supprime
    #:         les chapitres des sources (--no-chapters per source).
    chapter_overrides:   list | None   = None  # list[ChapterEntry] | None
    extra_attachments:   list          = field(default_factory=list)  # list[Path]
    work_dir:            Path | None   = None
    file_title:          str           = ""      # balise Title du segment de sortie
    #: Balises MKV globales à écrire dans le fichier de sortie via mkvpropedit.
    #: None  → comportement par défaut (mkvmerge copie les balises des sources).
    #: dict  → supprime les balises des sources (--no-global-tags) et écrit ce dict.
    #: {}    → supprime toutes les balises (--no-global-tags, rien n'est écrit).
    tag_overrides:       dict[str, str] | None = None


# =============================================================================
# Exception
# =============================================================================

class RemuxError(RuntimeError):
    """Erreur levée lors de la validation ou de l'exécution du remuxage."""


# =============================================================================
# Fabrique depuis FileInfo
# =============================================================================

def tracks_from_file_info(info: FileInfo, file_id: str = "") -> list[TrackEntry]:
    """
    Construit la liste des TrackEntry depuis un FileInfo inspecté.

    L'ordre est : pistes vidéo, puis audio, puis sous-titres.
    file_id permet d'associer chaque piste à un SourceFile de l'UI.
    """
    entries: list[TrackEntry] = []

    def _flags_from_disp(raw: dict) -> dict:
        disp = raw.get("disposition", {})
        return dict(
            flag_default          = bool(disp.get("default",          0)),
            flag_forced           = bool(disp.get("forced",           0)),
            flag_hearing_impaired = bool(disp.get("hearing_impaired", 0)),
            flag_visual_impaired  = bool(disp.get("visual_impaired",  0)),
            flag_original         = bool(disp.get("original",         0)),
            flag_commentary       = bool(disp.get("comment",          0)),
            orig_flag_default          = bool(disp.get("default",          0)),
            orig_flag_forced           = bool(disp.get("forced",           0)),
            orig_flag_hearing_impaired = bool(disp.get("hearing_impaired", 0)),
            orig_flag_visual_impaired  = bool(disp.get("visual_impaired",  0)),
            orig_flag_original         = bool(disp.get("original",         0)),
            orig_flag_commentary       = bool(disp.get("comment",          0)),
        )

    for v in info.video_tracks:
        parts: list[str] = [v.resolution]
        if v.hdr_type != HDRType.NONE:
            parts.append(v.hdr_type.label())
        if v.frame_rate:
            fr = v.frame_rate
            if "/" in fr:
                try:
                    num, den = fr.split("/")
                    fps = round(int(num) / int(den), 3)
                    fr = f"{fps} fps"
                except (ValueError, ZeroDivisionError):
                    pass
            else:
                fr = f"{fr} fps"
            parts.append(fr)
        entries.append(TrackEntry(
            mkv_tid=v.index,
            track_type="video",
            codec=v.codec.upper(),
            display_info="  ".join(parts),
            language=v.language or "",
            title=v.title or "",
            orig_language=v.language or "",
            orig_title=v.title or "",
            file_id=file_id,
            **_flags_from_disp(v.raw),
        ))

    for a in info.audio_tracks:
        parts = [a.channels_label]
        if a.bit_rate:
            parts.append(f"{a.bit_rate // 1000} kbps")
        if a.atmos_flag:
            parts.append("Atmos")
        elif a.dtsx_flag:
            parts.append("DTS:X")
        entries.append(TrackEntry(
            mkv_tid=a.index,
            track_type="audio",
            codec=a.codec.upper(),
            display_info="  ".join(parts),
            language=a.language or "",
            title=a.title or "",
            orig_language=a.language or "",
            orig_title=a.title or "",
            file_id=file_id,
            **_flags_from_disp(a.raw),
        ))

    for s in info.subtitle_tracks:
        disp_flags = _flags_from_disp(s.raw)
        # SubtitleTrack.forced / .default are the authoritative source;
        # override whatever raw["disposition"] may (or may not) contain.
        disp_flags["flag_forced"]      = s.forced
        disp_flags["orig_flag_forced"] = s.forced
        disp_flags["flag_default"]      = s.default
        disp_flags["orig_flag_default"] = s.default
        entries.append(TrackEntry(
            mkv_tid=s.index,
            track_type="subtitle",
            codec=s.codec.upper(),
            display_info="",
            language=s.language or "",
            title=s.title or "",
            orig_language=s.language or "",
            orig_title=s.title or "",
            file_id=file_id,
            **disp_flags,
        ))

    return entries


# =============================================================================
# Workflow
# =============================================================================

class RemuxWorkflow(QObject):
    """
    Construit et exécute un remuxage MKV via mkvmerge (support multi-source).

    Usage :
        wf = RemuxWorkflow(mkvmerge_bin="mkvmerge")
        cmd = wf.build_command(config)          # list[str]
        preview = wf.preview_command(config)    # str multi-lignes lisible
        errors = wf.validate(config)            # list[str] — vide si valide
        signals = wf.run(config)                # TaskSignals

    Signaux :
        log_message(level: str, message: str)
            Émis par run() pour informer l'UI.
    """

    log_message = Signal(str, str)

    def __init__(
        self,
        mkvmerge_bin: str = "mkvmerge",
        mkvpropedit_bin: str = "mkvpropedit",
        parent: QObject | None = None,
        *,
        writing_application: str = "",
    ) -> None:
        super().__init__(parent)
        self._mkvmerge = mkvmerge_bin
        self._mkvpropedit = mkvpropedit_bin
        self._runner = ToolRunner(max_workers=1, parent=self)
        self._writing_application = writing_application.strip()

    def set_writing_application(self, writing_application: str) -> None:
        """Met à jour la valeur du tag Multiplexing Application."""
        self._writing_application = writing_application.strip()

    # ------------------------------------------------------------------
    # Construction de la commande
    # ------------------------------------------------------------------

    def build_command(
        self,
        config: RemuxConfig,
        chapters_file: "Path | None" = None,
        *,
        emit_metadata_logs: bool = False,
    ) -> list[str]:
        """
        Construit la liste d'arguments mkvmerge pour un remuxage multi-source.

        Structure :
            mkvmerge -o OUTPUT
              [--chapters FILE]
              [--track-order FI:TID,...]
              [per-source flags] SOURCE0
              [per-source flags] SOURCE1
              ...

        chapters_file : chemin vers un fichier XML de chapitres à passer à
                        --chapters.  Fourni par run() depuis un fichier temporaire ;
                        pour l'aperçu (preview_command) il vaut None et on affiche
                        un placeholder.
        """
        cmd: list[str] = [self._mkvmerge, "-o", _cli_path(config.output)]

        # --- Titre du segment de sortie (toujours appliqué, même vide) ---
        cmd.extend(["--title", config.file_title])

        # --- Chapitres personnalisés (avant les sources, option globale) ---
        if config.chapter_overrides is not None:
            if chapters_file is not None:
                cmd.extend(["--chapters", _cli_path(chapters_file)])
            else:
                cmd.extend(["--chapters", "<chapitres.xml>"])

        # --- Ordre global des pistes (avant les sources) ---
        if config.track_order:
            order = ",".join(f"{fi}:{tid}" for fi, tid in config.track_order)
            cmd.extend(["--track-order", order])

        # Ensemble des (file_index, mkv_tid) activées pour lookup rapide
        enabled_set: set[tuple[int, int]] = set(config.track_order)

        # --- Pièces jointes supplémentaires (ajout manuel) — avant les sources ---
        # --attach-file est une option globale : doit précéder les fichiers source.
        for att_path in config.extra_attachments:
            # Fichiers nommés "cover.*" → forcer l'attachment-name à "cover"
            if att_path.stem.lower() == "cover":
                cmd.extend(["--attachment-name", "cover"])
            cmd.extend(["--attach-file", _cli_path(att_path)])

        for src in config.sources:
            fi = src.file_index

            # Pistes activées de CE fichier
            enabled_here   = [t for t in src.tracks if (fi, t.mkv_tid) in enabled_set]
            enabled_tids   = {t.mkv_tid for t in enabled_here}

            videos_all = [t for t in src.tracks if t.track_type == "video"]
            audios_all = [t for t in src.tracks if t.track_type == "audio"]
            subs_all   = [t for t in src.tracks if t.track_type == "subtitle"]
            videos_on  = [t for t in enabled_here if t.track_type == "video"]
            audios_on  = [t for t in enabled_here if t.track_type == "audio"]
            subs_on    = [t for t in enabled_here if t.track_type == "subtitle"]

            # --- Inclusion/exclusion par type ---
            # Émet --no-xxx uniquement si la source POSSÈDE des pistes de ce type
            # (évite d'émettre --no-video pour une source sans vidéo)
            if videos_all and not videos_on:
                cmd.append("--no-video")
            elif len(videos_on) < len(videos_all):
                cmd.extend(["--video-tracks", ",".join(str(t.mkv_tid) for t in videos_on)])

            if audios_all and not audios_on:
                cmd.append("--no-audio")
            elif len(audios_on) < len(audios_all):
                cmd.extend(["--audio-tracks", ",".join(str(t.mkv_tid) for t in audios_on)])

            if subs_all and not subs_on:
                cmd.append("--no-subtitles")
            elif len(subs_on) < len(subs_all):
                cmd.extend(["--subtitle-tracks", ",".join(str(t.mkv_tid) for t in subs_on)])

            # --- Options conteneur ---
            # Supprime les chapitres de cette source si :
            #   a) l'utilisateur veut garder ses chapitres personnalisés (chapter_overrides),
            #   b) ou si keep_chapters est False.
            if config.chapter_overrides is not None or not config.keep_chapters:
                cmd.append("--no-chapters")

            # --- Balises globales ---
            # Si tag_overrides est défini : on supprime les balises de cette source
            # (elles seront remplacées via mkvpropedit après le remuxage).
            # Si copy_tags=False et pas d'overrides : suppression explicite.
            if config.tag_overrides is not None or not src.copy_tags:
                cmd.append("--no-global-tags")

            # --- Attachements (sélection per-source) ---
            # attachment_count > 0 = la source possède des attachements
            if src.attachment_count > 0:
                sel = src.selected_attachments
                if not sel:
                    cmd.append("--no-attachments")
                elif len(sel) < src.attachment_count:
                    ids = ",".join(
                        str(a.local_index + 1)
                        for a in sorted(sel, key=lambda a: a.local_index)
                    )
                    cmd.extend(["--attachments", ids])
                # sinon : tous sélectionnés → mkvmerge les copie par défaut

            # --- Métadonnées de pistes (seulement si modifiées) ---
            for t in src.tracks:
                if t.mkv_tid not in enabled_tids:
                    continue
                if t.title != t.orig_title:
                    cmd.extend(["--track-name", f"{t.mkv_tid}:{t.title}"])
                lang     = (t.language      or "").strip()
                orig_lang = (t.orig_language or "").strip()
                if lang != orig_lang:
                    # Langue modifiée ou vidée → émettre --language-ietf
                    emit_lang = lang if lang else "und"
                    cmd.extend(["--language", f"{t.mkv_tid}:{LangTags.to_iso639_2(emit_lang)}"])
                    cmd.extend(["--language-ietf", f"{t.mkv_tid}:{emit_lang}"])
                    if emit_metadata_logs:
                        self.log_message.emit(
                            "INFO",
                            f"Lang set for track {t.mkv_tid} to {emit_lang} "
                            f"(ISO639-2: {LangTags.to_iso639_2(emit_lang)}) in workflow",
                        )
                # Flags MKV
                if t.flag_enabled != t.orig_flag_enabled:
                    cmd.extend(["--track-enabled-flag",  f"{t.mkv_tid}:{'1' if t.flag_enabled else '0'}"])
                if t.flag_default != t.orig_flag_default:
                    cmd.extend(["--default-track-flag",  f"{t.mkv_tid}:{'1' if t.flag_default else '0'}"])
                if t.flag_forced != t.orig_flag_forced:
                    cmd.extend(["--forced-track",        f"{t.mkv_tid}:{'1' if t.flag_forced else '0'}"])
                if t.flag_hearing_impaired != t.orig_flag_hearing_impaired:
                    cmd.extend(["--hearing-impaired-flag", f"{t.mkv_tid}:{'1' if t.flag_hearing_impaired else '0'}"])
                if t.flag_visual_impaired != t.orig_flag_visual_impaired:
                    cmd.extend(["--visual-impaired-flag", f"{t.mkv_tid}:{'1' if t.flag_visual_impaired else '0'}"])
                if t.flag_original != t.orig_flag_original:
                    cmd.extend(["--original-flag",        f"{t.mkv_tid}:{'1' if t.flag_original else '0'}"])
                if t.flag_commentary != t.orig_flag_commentary:
                    cmd.extend(["--commentary-flag",      f"{t.mkv_tid}:{'1' if t.flag_commentary else '0'}"])

            cmd.append(_cli_path(src.path))

        return cmd

    def preview_command(self, config: RemuxConfig) -> str:
        """
        Retourne la commande sous forme lisible, une option/valeur par ligne.
        """
        parts = self.build_command(config)
        if not parts:
            return ""

        lines: list[str] = [parts[0]]
        i = 1
        while i < len(parts):
            p = parts[i]
            if p.startswith("-") and i + 1 < len(parts) and not parts[i + 1].startswith("-"):
                lines.append(f"    {p} {parts[i + 1]}")
                i += 2
            else:
                lines.append(f"    {p}")
                i += 1

        return " \\\n".join(lines)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate(self, config: RemuxConfig) -> list[str]:
        """
        Valide la configuration avant exécution.

        Retourne une liste d'erreurs lisibles, ou [] si tout est valide.
        """
        errors: list[str] = []

        if not config.sources:
            errors.append("Aucun fichier source.")
            return errors

        for src in config.sources:
            if not src.path.is_file():
                errors.append(f"Fichier source introuvable : {src.path}")
            if src.path == config.output:
                errors.append(f"Le fichier de sortie doit être différent de la source : {src.path.name}")

        output_dir = config.output.parent
        if not output_dir.exists():
            errors.append(f"Dossier de sortie inexistant : {output_dir}")
        elif not self._is_dir_writable(output_dir):
            errors.append(
                "Dossier de sortie non inscriptible : "
                f"{output_dir} (vérifiez les protections Windows sur les dossiers Bibliothèques)."
            )

        if not config.track_order:
            errors.append("Aucune piste sélectionnée.")

        return errors

    @staticmethod
    def _is_dir_writable(path: Path) -> bool:
        """
        Vérifie qu'un fichier temporaire peut être créé dans ``path``.

        Sous Windows, certains dossiers protégés (Documents/Vidéos, etc.) peuvent
        exister mais refuser la création de nouveaux fichiers.
        """
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=path,
                prefix="mrecode_write_probe_",
                delete=True,
            ):
                pass
            return True
        except OSError:
            return False

    # ------------------------------------------------------------------
    # Exécution
    # ------------------------------------------------------------------

    def _write_chapter_xml(self, entries: list) -> Path:
        """Écrit un XML Matroska Chapters dans un fichier temporaire et retourne son chemin."""
        xml_content = build_chapter_xml(entries)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".xml", delete=False, encoding="utf-8"
        ) as f:
            f.write(xml_content)
            return Path(f.name)

    def _apply_tags_inplace(self, output: Path, tags: dict[str, str]) -> None:
        """
        Écrit les balises MKV globales depuis un dict via mkvpropedit (in-place).

        Construit un fichier XML Matroska Tags temporaire et l'applique avec
        ``mkvpropedit --tags all:<xmlfile>``. Supprime le fichier temp après usage.
        """
        if not tags:
            return

        simples = "\n".join(
            f"    <Simple><Name>{_xml_escape(k)}</Name>"
            f"<String>{_xml_escape(v)}</String></Simple>"
            for k, v in tags.items()
            if v.strip()
        )
        xml_content = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<Tags>\n"
            "  <Tag>\n"
            "    <Targets />\n"
            f"{simples}\n"
            "  </Tag>\n"
            "</Tags>\n"
        )

        if not self._mkvpropedit:
            self.log_message.emit("WARN", "mkvpropedit non configuré — balises non appliquées.")
            return

        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".xml", delete=False, encoding="utf-8"
            ) as f:
                f.write(xml_content)
                tmp_path = Path(f.name)

            cmd = [self._mkvpropedit, _cli_path(output), "--tags", f"all:{_cli_path(tmp_path)}"]
            self.log_message.emit("INFO", "$ " + " ".join(str(c) for c in cmd))
            r = subprocess.run(
                cmd, capture_output=True, check=False, timeout=60, **subprocess_text_kwargs()
            )
            if r.returncode != 0:
                self.log_message.emit("WARN", f"mkvpropedit (balises) : {(r.stderr or '').strip()}")
        except FileNotFoundError:
            self.log_message.emit("WARN", "mkvpropedit introuvable — balises non appliquées.")
        finally:
            if tmp_path is not None:
                try:
                    tmp_path.unlink()
                except Exception:
                    pass

    def _set_writing_app_inplace(self, output: Path) -> None:
        """Écrit le tag Multiplexing Application dans les infos de segment via mkvpropedit."""
        if not self._mkvpropedit:
            self.log_message.emit("WARN", "mkvpropedit non configuré — writing-app non appliqué.")
            return
        if not self._writing_application:
            self.log_message.emit("WARN", "Writing-app non configuré — balise non appliquée.")
            return
        cmd = [
            self._mkvpropedit, _cli_path(output),
            "--edit", "info",
            "--set", f"muxing-application={self._writing_application}",
        ]
        try:
            self.log_message.emit("INFO", "$ " + " ".join(str(c) for c in cmd))
            r = subprocess.run(
                cmd, capture_output=True, check=False, timeout=30, **subprocess_text_kwargs()
            )
            if r.returncode != 0:
                self.log_message.emit("WARN", f"mkvpropedit (writing-app) : {(r.stderr or '').strip()}")
        except FileNotFoundError:
            self.log_message.emit("WARN", "mkvpropedit introuvable — writing-app non appliqué.")

    def run(self, config: RemuxConfig) -> TaskSignals:
        """
        Lance le remuxage dans un thread secondaire via ToolRunner,
        puis applique le tag Writing Application via mkvpropedit.

        Les signaux du TaskSignals retourné permettent de suivre la progression.
        """
        errors = self.validate(config)
        if errors:
            raise RemuxError("\n".join(errors))

        self.log_message.emit("INFO", f"Remuxage → {config.output.name}")
        cwd = config.work_dir or config.sources[0].path.parent
        if config.work_dir:
            config.work_dir.mkdir(parents=True, exist_ok=True)

        signals = TaskSignals()
        executor = ThreadPoolExecutor(max_workers=1)

        def _task() -> None:
            chapters_file: Path | None = None
            try:
                if config.chapter_overrides is not None:
                    chapters_file = self._write_chapter_xml(config.chapter_overrides)
                cmd = self.build_command(
                    config,
                    chapters_file=chapters_file,
                    emit_metadata_logs=True,
                )
                output = self._runner._run_cmd(
                    cmd, cwd=cwd, label="mkvmerge",
                    progress_cb=lambda line: signals.progress.emit(line),
                    signals=signals,
                )
                if config.tag_overrides:
                    self._apply_tags_inplace(config.output, config.tag_overrides)
                self._set_writing_app_inplace(config.output)
                signals.finished.emit(output)
            except TaskCancelledError:
                signals.cancelled.emit()
            except Exception as exc:
                signals.failed.emit(str(exc), exc)
            finally:
                if chapters_file is not None:
                    try:
                        chapters_file.unlink()
                    except Exception:
                        pass
                executor.shutdown(wait=False)

        executor.submit(_task)
        return signals
