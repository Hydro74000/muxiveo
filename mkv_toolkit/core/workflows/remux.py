"""
core/workflows/remux.py — Workflow de remuxage MKV sans réencodage.

Classes publiques :
    TrackEntry           — piste avec état d'inclusion et métadonnées éditables
    RemuxConfig          — configuration complète d'un remuxage
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

from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from core.inspector import FileInfo, HDRType
from core.runner import TaskSignals, ToolRunner


# =============================================================================
# Modèle de piste
# =============================================================================

@dataclass
class TrackEntry:
    """
    Représente une piste avec son état d'inclusion et ses métadonnées éditables.

    mkv_tid est l'identifiant mkvmerge (= index ffprobe pour les fichiers MKV).
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

    orig_language: str = field(default="", repr=False)
    orig_title:    str = field(default="", repr=False)

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
# Configuration de remuxage
# =============================================================================

@dataclass
class RemuxConfig:
    """
    Configuration complète d'un remuxage.

    tracks : liste ordonnée des pistes — l'ordre définit --track-order dans
             la commande mkvmerge finale.
    """

    source:           Path
    output:           Path
    tracks:           list[TrackEntry]
    keep_chapters:    bool = True
    keep_attachments: bool = True   # cover.jpg et autres pièces jointes


# =============================================================================
# Exception
# =============================================================================

class RemuxError(RuntimeError):
    """Erreur levée lors de la validation ou de l'exécution du remuxage."""


# =============================================================================
# Fabrique depuis FileInfo
# =============================================================================

def tracks_from_file_info(info: FileInfo) -> list[TrackEntry]:
    """
    Construit la liste des TrackEntry depuis un FileInfo inspecté.

    L'ordre est : pistes vidéo, puis audio, puis sous-titres.
    """
    entries: list[TrackEntry] = []

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
        ))

    for a in info.audio_tracks:
        parts = [a.channels_label]
        if a.sample_rate:
            parts.append(f"{a.sample_rate // 1000} kHz")
        if a.bit_rate:
            parts.append(f"{a.bit_rate // 1000} kbps")
        entries.append(TrackEntry(
            mkv_tid=a.index,
            track_type="audio",
            codec=a.codec.upper(),
            display_info="  ".join(parts),
            language=a.language or "",
            title=a.title or "",
            orig_language=a.language or "",
            orig_title=a.title or "",
        ))

    for s in info.subtitle_tracks:
        flags: list[str] = []
        if s.forced:  flags.append("forcé")
        if s.default: flags.append("défaut")
        entries.append(TrackEntry(
            mkv_tid=s.index,
            track_type="subtitle",
            codec=s.codec.upper(),
            display_info=", ".join(flags),
            language=s.language or "",
            title=s.title or "",
            orig_language=s.language or "",
            orig_title=s.title or "",
        ))

    return entries


# =============================================================================
# Workflow
# =============================================================================

class RemuxWorkflow(QObject):
    """
    Construit et exécute un remuxage MKV via mkvmerge.

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
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._mkvmerge = mkvmerge_bin
        self._runner = ToolRunner(max_workers=1, parent=self)

    # ------------------------------------------------------------------
    # Construction de la commande
    # ------------------------------------------------------------------

    def build_command(self, config: RemuxConfig) -> list[str]:
        """
        Construit la liste d'arguments mkvmerge correspondant à la configuration.

        La commande suit cette structure :
            mkvmerge -o OUTPUT
              [--no-video|--video-tracks TIDs]
              [--no-audio|--audio-tracks TIDs]
              [--no-subtitles|--subtitle-tracks TIDs]
              [--no-chapters]  [--no-attachments]
              [--track-name TID:name ...]
              [--language TID:lang ...]
              [--track-order 0:TID,...]
              SOURCE
        """
        cmd: list[str] = [self._mkvmerge, "-o", str(config.output)]

        enabled    = [t for t in config.tracks if t.enabled]
        videos_all = [t for t in config.tracks if t.track_type == "video"]
        audios_all = [t for t in config.tracks if t.track_type == "audio"]
        subs_all   = [t for t in config.tracks if t.track_type == "subtitle"]

        videos_on  = [t for t in enabled if t.track_type == "video"]
        audios_on  = [t for t in enabled if t.track_type == "audio"]
        subs_on    = [t for t in enabled if t.track_type == "subtitle"]

        # --- Inclusion/exclusion par type ---
        if not videos_on:
            cmd.append("--no-video")
        elif len(videos_on) < len(videos_all):
            cmd.extend(["--video-tracks", ",".join(str(t.mkv_tid) for t in videos_on)])

        if not audios_on:
            cmd.append("--no-audio")
        elif len(audios_on) < len(audios_all):
            cmd.extend(["--audio-tracks", ",".join(str(t.mkv_tid) for t in audios_on)])

        if not subs_on:
            cmd.append("--no-subtitles")
        elif len(subs_on) < len(subs_all):
            cmd.extend(["--subtitle-tracks", ",".join(str(t.mkv_tid) for t in subs_on)])

        # --- Options conteneur ---
        if not config.keep_chapters:
            cmd.append("--no-chapters")
        if not config.keep_attachments:
            cmd.append("--no-attachments")

        # --- Métadonnées de pistes (seulement si modifiées) ---
        for t in enabled:
            if t.title != t.orig_title:
                cmd.extend(["--track-name", f"{t.mkv_tid}:{t.title}"])
            if t.language != t.orig_language:
                cmd.extend(["--language", f"{t.mkv_tid}:{t.language}"])

        # --- Ordre des pistes ---
        if enabled:
            order = ",".join(f"0:{t.mkv_tid}" for t in enabled)
            cmd.extend(["--track-order", order])

        cmd.append(str(config.source))
        return cmd

    def preview_command(self, config: RemuxConfig) -> str:
        """
        Retourne la commande sous forme lisible, une option/valeur par ligne.

        Exemple :
            mkvmerge \\
                -o /output/film_remux.mkv \\
                --no-subtitles \\
                --track-order 0:0,0:1 \\
                /source/film.mkv
        """
        parts = self.build_command(config)
        if not parts:
            return ""

        lines: list[str] = [parts[0]]
        i = 1
        while i < len(parts):
            p = parts[i]
            # Flag avec valeur : le prochain token ne commence pas par "-"
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

        if not config.source.is_file():
            errors.append(f"Fichier source introuvable : {config.source}")
        if not config.output.parent.exists():
            errors.append(f"Dossier de sortie inexistant : {config.output.parent}")
        if config.source == config.output:
            errors.append("Le fichier de sortie doit être différent du fichier source.")
        if not any(t.enabled for t in config.tracks):
            errors.append("Aucune piste sélectionnée.")

        return errors

    # ------------------------------------------------------------------
    # Exécution
    # ------------------------------------------------------------------

    def run(self, config: RemuxConfig) -> TaskSignals:
        """
        Lance le remuxage dans un thread secondaire via ToolRunner.

        Les signaux du TaskSignals retourné permettent de suivre la progression.
        """
        errors = self.validate(config)
        if errors:
            raise RemuxError("\n".join(errors))

        self.log_message.emit("INFO", f"Remuxage → {config.output.name}")
        cmd = self.build_command(config)
        return self._runner.run(cmd, cwd=config.source.parent, label="mkvmerge")
