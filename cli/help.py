"""Aide du CLI : textes des options et des commandes, groupes d'options, page d'accueil par familles,
commande ``help`` (texte ou référence Markdown) et traduction (langue ``[ui] language``, repli anglais).

Les textes sources sont en français ; ils sont traduits au moment de l'affichage par ``translate_text``
(catalogue ``locales.json``). Les sorties des commandes ne changent pas : seule l'aide est traduite.
"""

from __future__ import annotations

import argparse
import functools
import re
from collections.abc import Iterator
from typing import Any

from core.i18n import translate_text

# ---------------------------------------------------------------------------
# Textes des options sans aide (communes à plusieurs commandes)
# ---------------------------------------------------------------------------

OPTION_HELP: dict[str, str] = {
    "--log-format": "Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements "
                    "versionnés sur stderr, réponse JSON sur stdout).",
    "--verbose": "Journal détaillé (sortie brute des outils).",
    "--sync-mode": "Synchro des sources : physical (pistes réécrites, coupures et cadence) ou container "
                   "(décalage du conteneur).",
    "--sync-subtitles": "Sous-titres : mirror (suivent la synchro de leur source) ou none.",
    "--clean-nfo": "Nettoyer le NFO (balises techniques retirées).",
    "--crossfade-ms": "Fondu aux raccords de synchro (ms, 0–1000).",
    "--auto-forced-subs": "Repérer les sous-titres forcés (peu d'événements) et les marquer.",
    "--forced-threshold": "Nombre d'événements sous lequel une piste de sous-titres est jugée forcée.",
    "--auto-sdh": "Repérer les sous-titres SDH (malentendants) et les marquer.",
    "-i": "Source (répétable).",
    "-o": "Sortie : fichier, ou dossier (avec / final).",
    "--output": "Fichier de sortie.",
    "--interpolation-preset": "Préréglage d'interpolation : fast, balanced, quality, ultra ou light.",
    "--nfo": "Écrire le NFO à côté de la sortie.",
    "--no-nfo": "Ne pas écrire de NFO.",
    "--writing-application": "Balise WritingApp de la sortie.",
    "--force": "Remplacer une sortie existante.",
    "--profile": "Profil décisionnel : chemin JSON ou nom d'un profil enregistré.",
    "--continue-on-error": "Continuer le lot après un échec.",
    "--json": "Résultat JSON sur stdout.",
    "--dry-run": "Vérifier et afficher le plan sans exécuter.",
    "--description": "Description enregistrée.",
    "--output-dir": "Dossier des sorties.",
    "--ref": "Fichier de référence (master : vidéo, chapitres).",
    "--donor": "Fichier donneur (audio, sous-titres).",
    "--ref-dir": "Dossier de référence (épisodes).",
    "--donor-dir": "Dossier donneur (épisodes).",
    "--config-template": "Écrire un exact-job minimal de la source au lieu de la fiche.",
    "--template": "Gabarit JSON appliqué à chaque job du lot.",
    "--input-dir": "Dossier de sources (répétable).",
    "--recursive": "Parcourir aussi les sous-dossiers.",
    "--include": "Motif de fichiers retenus (glob, répétable).",
    "--exclude": "Motif de fichiers écartés (glob, répétable).",
    "--summary": "Résumé JSON du lot écrit dans ce fichier.",
    "--target": "Fichier à caler.",
    "--stream-ref": "Flux de référence (spécificateur FFmpeg, ex. 0:a:0).",
    "--stream-target": "Flux à caler (spécificateur FFmpeg).",
    "--output-json": "Calibration écrite dans ce fichier JSON.",
    "--offset-ms": "Décalage à appliquer (ms).",
    "--calibration": "Fichier JSON de calibration explicite (remplace l'analyse).",
    "--report-json": "Rapport JSON des épisodes écrit dans ce fichier.",
    "--auto-tmdb": "Fiche TMDB : sans valeur, recherche d'après le nom ; avec un ID, cette fiche.",
    "--tmdb-apikey": "Clé API TMDB (défaut : réglage [metadata]).",
    "--no-cover": "Sans couverture TMDB.",
    "--tag": "Groupe de release ({group} du gabarit de nom).",
    "--kind": "Nature : film (movie), série (tv) ou les deux (all).",
    "--season": "Saison.",
    "--episode": "Épisode.",
}

#: Arguments positionnels, par commande (chemin de sous-commandes) puis destination.
POSITIONAL_HELP: dict[tuple[str, str], str] = {
    ("inspect", "input"): "Sources à inspecter.",
    ("encode-preset show", "name"): "Nom du preset.",
    ("encode-preset save", "name"): "Nom du preset.",
    ("encode-preset export", "name"): "Nom du preset.",
    ("encode-preset delete", "name"): "Nom du preset.",
    ("encode-preset import", "file"): "Fichier JSON du preset.",
    ("encode-preset export", "file"): "Fichier JSON écrit.",
    ("profile show", "name"): "Nom du profil.",
    ("profile delete", "name"): "Nom du profil.",
    ("profile save", "file"): "Fichier JSON du profil (decision-profile v1).",
    ("calibration show", "file"): "Fichier JSON de calibration.",
    ("calibration edit", "file"): "Fichier JSON de calibration.",
    ("calibration invert", "file"): "Fichier JSON de calibration.",
    ("tmdb search", "query"): "Titre recherché.",
    ("tmdb details", "tmdb_id"): "Identifiant TMDB.",
    ("config show", "section"): "Section (paths, tools, ffmpeg, hdr, ui…).",
    ("config get", "key"): "Réglage section.clé (voir config show).",
    ("config set", "key"): "Réglage section.clé (voir config show).",
    ("config unset", "key"): "Réglage section.clé.",
    ("config reset", "key"): "Réglage section.clé.",
    ("config set", "value"): "Nouvelle valeur (booléens : true / false).",
}

# ---------------------------------------------------------------------------
# Commandes : description et exemples
# ---------------------------------------------------------------------------

#: Familles de la page d'accueil (``--cli -h``), dans l'ordre.
FAMILIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Média", ("inspect", "validate", "preview", "remux", "run", "batch", "extract", "bluray-titles")),
    ("Encodage", ("encode", "encode-preset", "capabilities")),
    ("HDR", ("hdr-patch",)),
    ("Synchro", ("sync-scan", "shift-subs", "calibration", "sync-waveform")),
    ("Hybrid", ("hybrid",)),
    ("Profils et métadonnées", ("profile", "tmdb")),
    ("Système", ("version", "tools", "config", "workdir", "plugins", "nvencc", "schema", "help")),
    ("API", ("call", "operations", "serve")),
)

#: Description et exemples des commandes (chemin de sous-commandes).
COMMANDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "inspect": ("Inspecter une ou plusieurs sources : pistes, HDR, chapitres, pièces jointes (JSON). "
                "--config-template écrit un exact-job de départ ; --hdr ouvre l'inspection du HDR Studio.",
                ("inspect film.mkv", "inspect film.mkv --config-template --with-encode > job.json",
                 "inspect film.mkv --hdr measure --hdr-advice --hdr-report rapport.html")),
    "validate": ("Valider un job (ou un profil) sans rien exécuter ; --json publie le plan résolu (pistes, "
                 "synchro, encodage).", ("validate --config job.json --json", "validate --profile Films -i film.mkv")),
    "preview": ("Afficher les commandes prévues (FFmpeg, encodeur) sans les exécuter.",
                ("preview --config job.json", "preview -i film.mkv -o sortie.mkv --video-codec libx265 --crf 18")),
    "remux": ("Remuxer des sources selon le job ou les options (sélection des pistes, synchro, métadonnées).",
              ("remux -i film.mkv -o sortie.mkv", "remux --config job.json --save gabarit.json")),
    "run": ("Exécuter un job JSON (exact-job ou contrat v1), remux ou encodage selon son bloc `encode`.",
            ("run --config job.json", "run --config job.json --dry-run")),
    "encode": ("Encoder : job + bloc `encode` ou options d'encodage ; aperçus réels avec --preview-image / "
               "--preview-video.",
               ("encode -i film.mkv -o film.x265.mkv --video-codec libx265 --crf 18",
                "encode -i film.mkv -o sortie.mkv --encode-preset \"Films 4K\" --preview-image 600")),
    "batch": ("Appliquer un gabarit (ou un profil) à un lot de jobs ou de fichiers ; collisions refusées avant "
              "le premier job.", ("batch --template gabarit.json --input-dir /films --output-dir /sorties",
                                  "batch --template gabarit.json --batch jobs.json --continue-on-error --summary bilan.json")),
    "extract": ("Extraire des pistes, couches Dolby Vision (EL, BL, RPU), HDR10+ ou pièces jointes.",
                ("extract -i film.mkv --track a:0 --output-dir pistes/",
                 "extract -i film.mkv --track v:0 --layer el -o film.el.hevc")),
    "bluray-titles": ("Lister les titres (playlists) d'un Blu-ray, du plus long au plus court.",
                      ("bluray-titles /disques/FILM --min-duration 600",)),
    "encode-preset": ("Presets d'encodage enregistrés (communs avec l'interface).",
                      ("encode-preset list", "encode-preset save \"x265 rapide\" --video-codec libx265 --crf 22")),
    "encode-preset list": ("Lister les presets d'encodage.", ("encode-preset list",)),
    "encode-preset show": ("Afficher un preset d'encodage (JSON).", ("encode-preset show \"x265 rapide\"",)),
    "encode-preset save": ("Enregistrer un preset d'après les options d'encodage (réglages d'encodeur seulement).",
                           ("encode-preset save \"x265 rapide\" --video-codec libx265 --crf 22 --encoder-preset fast",)),
    "encode-preset import": ("Importer un preset depuis un fichier JSON.", ("encode-preset import preset.json",)),
    "encode-preset export": ("Exporter un preset dans un fichier JSON.",
                             ("encode-preset export \"x265 rapide\" preset.json",)),
    "encode-preset delete": ("Supprimer un preset d'encodage.", ("encode-preset delete \"x265 rapide\"",)),
    "capabilities": ("Capacités de la machine : encodeurs, préréglages, Vulkan, NVOF, TensorRT, moteur vidéo MVO.",
                     ("capabilities", "capabilities --section encoders")),
    "sync-scan": ("Mesurer le calage d'une piste (audio ou sous-titres) face à une référence ; calibration JSON.",
                  ("sync-scan --ref uhd.mkv --target vf.mkv --output-json calib.json",
                   "sync-scan --ref uhd.mkv --target vf.mkv --detect-cuts")),
    "shift-subs": ("Recaler un fichier de sous-titres texte (SRT, ASS) : décalage fixe ou calibration.",
                   ("shift-subs -i vf.srt -o vf.cale.srt --offset-ms 1250",
                    "shift-subs -i vf.srt -o vf.cale.srt --calibration calib.json")),
    "calibration": ("Calibrations de synchro : lire, éditer (Synchro Studio), composer, inverser.",
                    ("calibration show calib.json", "calibration edit calib.json --cut 600000-602500 -o calib2.json")),
    "call": ("Appeler une opération de la couche de service avec une requête JSON stricte.",
             ("call media.inspect --request requete.json", "call media.extract --request - < requete.json",
              "call config.set --allow-privileged --request reglage.json")),
    "calibration show": ("Décrire une calibration : segments, coupes, plages de sortie, cadence.",
                         ("calibration show calib.json",)),
    "calibration compose": ("Composer deux calibrations : mesure face à une référence elle-même synchronisée.",
                            ("calibration compose mesure.json position.json -o synchro.json",)),
    "calibration invert": ("Inverser une calibration (référence face à la cible).",
                           ("calibration invert synchro.json -o inverse.json",)),
    "sync-waveform": ("Forme d'onde d'une piste audio sur une plage (pics par tranche, JSON).",
                      ("sync-waveform -i film.mkv --stream 0:a:0 --start 1:00:00 --duration 30",)),
    "hybrid": ("Hybrider une paire ou une saison sur le moteur du Hybrid Studio : vidéo du master, audio et "
               "sous-titres des donneurs calés ; plan, exact-jobs, recette, encodage par épisode.",
               ("hybrid --ref-dir /uhd --donor-dir /vf -o /sorties",
                "hybrid --source master=/uhd --source donor-audio=/vf -o /sorties --plan --export-jobs /jobs")),
    "profile": ("Profils décisionnels : valider, prévisualiser, appliquer, lots ; profils enregistrés.",
                ("profile list", "profile apply --profile Films -i film.mkv -o sortie.mkv")),
    "profile validate": ("Valider un profil décisionnel v1.", ("profile validate --profile profil.json",)),
    "profile preview": ("Prévisualiser un profil sur des sources (pistes retenues, nom, commandes).",
                        ("profile preview --profile Films -i film.mkv --json",)),
    "profile apply": ("Appliquer un profil et remuxer (ou encoder avec les options d'encodage).",
                      ("profile apply --profile Films -i film.mkv -o sortie.mkv",)),
    "profile batch": ("Appliquer un profil à un lot de fichiers.",
                      ("profile batch --profile Films --input-dir /films --output-dir /sorties",)),
    "profile capture": ("Capturer un profil d'après un job (comme « Capturer » du panneau Conteneur).",
                        ("profile capture -i film.mkv --name \"Films VF\" --save",
                         "profile capture --config job.json --name Séries --no-order")),
    "profile list": ("Lister les profils enregistrés (ceux de l'interface).", ("profile list",)),
    "profile show": ("Afficher un profil enregistré (JSON).", ("profile show Films",)),
    "profile save": ("Enregistrer un profil depuis un fichier JSON (validé).", ("profile save profil.json --force",)),
    "profile delete": ("Supprimer un profil enregistré.", ("profile delete Ancien",)),
    "tmdb": ("TMDB : rechercher un film ou une série, lire une fiche.",
             ("tmdb search \"Le Bureau des légendes\" --kind tv", "tmdb details 2316 --kind tv --season 2 --episode 1")),
    "tmdb search": ("Rechercher sur TMDB (25 résultats au plus).", ("tmdb search Dune --year 2021",)),
    "tmdb details": ("Fiche TMDB complète, titre de conteneur et balises MKV.", ("tmdb details 438631 --json",)),
    "version": ("Version de Muxiveo ; --check compare à la dernière release du canal (code 8 si mise à jour) ; "
                "--install --yes l'installe.", ("version --check", "version --install --yes")),
    "tools": ("Chemins des outils résolus par Muxiveo (JSON).", ("tools",)),
    "config": ("Configuration : réglages du panneau Paramètres et des extensions.",
               ("config show ui", "config set ui.theme light")),
    "config path": ("Emplacements de config.ini et des réglages de l'interface.", ("config path",)),
    "config show": ("Réglages, valeur effective et provenance (ini, settings, default).", ("config show tools",)),
    "config get": ("Un réglage.", ("config get ffmpeg.threads",)),
    "config set": ("Écrire un réglage dans config.ini (valeur validée).", ("config set hdr.display_peak_nits 800",)),
    "config unset": ("Retirer la surcharge config.ini d'un réglage.", ("config unset ui.theme",)),
    "config reset": ("Revenir à la valeur par défaut.", ("config reset ui.theme",)),
    "workdir": ("Dossier de travail : éléments abandonnés, nettoyage.", ("workdir list", "workdir clean --dry-run")),
    "workdir list": ("Éléments nettoyables (jobs actifs exclus).", ("workdir list",)),
    "workdir clean": ("Nettoyer le dossier de travail (comme « Nettoyer » au démarrage).", ("workdir clean",)),
    "plugins": ("Extensions : moteur vidéo MVO (mvo-rife), accélération TensorRT (mvo-rife-trt).",
                ("plugins list", "plugins install mvo-rife")),
    "plugins list": ("État des extensions.", ("plugins list",)),
    "plugins install": ("Installer une extension (TensorRT : --accept-license).",
                        ("plugins install mvo-rife-trt --accept-license",)),
    "plugins update": ("Mettre à jour une extension, ou toutes celles installées.", ("plugins update",)),
    "plugins remove": ("Supprimer une extension.", ("plugins remove mvo-rife-trt",)),
    "nvencc": ("NVEncC : état, installation, mise à jour, suppression de la copie utilisateur.",
               ("nvencc status --check", "nvencc install")),
    "nvencc status": ("Présence, version et provenance de NVEncC.", ("nvencc status --check",)),
    "nvencc install": ("Installer NVEncC (apt / dnf, sinon dossier utilisateur).", ("nvencc install",)),
    "nvencc update": ("Mettre à jour NVEncC si une version plus récente existe.", ("nvencc update",)),
    "nvencc remove": ("Supprimer la copie utilisateur de NVEncC.", ("nvencc remove",)),
    "schema": ("Schéma JSON du contrat (jobs, exact-job, profils) ou du catalogue de l'API.",
               ("schema --version 1", "schema --version api")),
    "operations": ("Opérations de la couche de service ; --describe publie leur schéma.",
                   ("operations", "operations --describe media.extract")),
    "serve": ("Worker JSON-RPC sur stdio : un client local (extension, outil) soumet des jobs, suit leurs événements, "
              "les annule.",
              ("serve", "serve --max-jobs 2 --max-analyses 2", "serve --allow-privileged")),
    "help": ("Aide d'une commande, de toutes (--all) ou référence Markdown (--format md).",
             ("help encode", "help profile apply", "help --all", "help --format md > reference.md")),
}

# ---------------------------------------------------------------------------
# Groupes d'options
# ---------------------------------------------------------------------------

GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Sources", ("-i", "--config", "--template", "--batch", "--input-dir", "--recursive", "--include", "--exclude",
                 "--ref", "--donor", "--ref-dir", "--donor-dir", "--profile")),
    ("Sortie", ("-o", "--output-dir", "--output-template", "--output-all", "--force", "--dry-run", "--json",
                "--export-workflow", "--save", "--nfo", "--no-nfo", "--clean-nfo", "--writing-application",
                "--mux-backend", "--summary", "--report-json", "--continue-on-error", "--purge-temp")),
    ("Pistes", ("--auto-forced-subs", "--forced-threshold", "--auto-sdh", "--remove-closed-captions")),
    ("Synchro", ("--sync-mode", "--sync-subtitles", "--crossfade-ms", "--auto-sync", "--auto-sync-all", "--calibration",
                 "--detect-cuts", "--quick-sync-check", "--reject-coherent-sync", "--drift-threshold-ms",
                 "--cadence-auto", "--cadence-method")),
    ("TMDB", ("--auto-tmdb", "--tmdb", "--tmdb-id", "--tmdb-apikey", "--no-cover", "--no-attach", "--tag")),
    ("Journal et exécution", ("--log-format", "--verbose", "--ffmpeg", "--ffprobe", "--mediainfo", "--work-dir",
                              "--threads", "--keep-failed-work-dir")),
)
_GROUP_OF = {option: title for title, options in GROUPS for option in options}
_SUMMARY_RE = re.compile(r"^(  \S+ {2,})(\S.*)$")


@functools.lru_cache(maxsize=1)
def help_language() -> str:
    """Langue de l'aide : réglage ``[ui] language``, repli anglais."""
    try:
        from core.config import AppConfig

        return str(AppConfig().language or "eng")
    except Exception:  # configuration illisible : aide en anglais
        return "eng"


def _t(text: str, language: str | None = None) -> str:
    return translate_text(text, language=language or help_language()) if text else text


class HelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Aide traduite, valeur par défaut affichée, descriptions et exemples gardant leurs retours à la ligne."""

    language: str | None = None

    def __init__(self, prog: str, **kwargs: Any) -> None:
        kwargs.setdefault("max_help_position", 32)
        kwargs.setdefault("width", 110)
        super().__init__(prog, **kwargs)

    def _get_help_string(self, action: argparse.Action) -> str:
        source = (action.help or "").replace("%%", "%")
        text = _t(source, self.language).replace("%", "%%")
        default = action.default
        # Valeur par défaut ajoutée sauf si l'aide la décrit déjà.
        if (default not in (None, False, "", [], argparse.SUPPRESS) and "défaut" not in source.lower()
                and action.option_strings and not isinstance(action, argparse._StoreConstAction)):
            text += " " + _t("(défaut : %(default)s)", self.language)
        return text

    def start_section(self, heading: str | None) -> None:
        super().start_section(_t(heading or "", self.language) if heading else heading)

    def add_text(self, text: str | None) -> None:
        if text and text is not argparse.SUPPRESS:
            text = "\n".join(self._line(line) for line in text.split("\n"))
        super().add_text(text)

    def _line(self, line: str) -> str:
        """Ligne traduite ; « nom   résumé » (page d'accueil) : résumé traduit ; exemples inchangés."""
        if not line:
            return line
        if not line.startswith("  "):
            return _t(line, self.language)
        match = _SUMMARY_RE.match(line)
        return match.group(1) + _t(match.group(2), self.language) if match else line


def _subcommands(parser: argparse.ArgumentParser) -> Iterator[tuple[str, argparse.ArgumentParser]]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            yield from action.choices.items()


def walk(parser: argparse.ArgumentParser, path: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...],
                                                                                           argparse.ArgumentParser]]:
    """Toutes les commandes (chemin, parseur), en profondeur."""
    for name, sub in _subcommands(parser):
        yield (*path, name), sub
        yield from walk(sub, (*path, name))


def _examples(examples: tuple[str, ...]) -> str:
    return "Exemples :\n" + "\n".join(f"  muxiveo --cli {line}" for line in examples)


def _regroup(parser: argparse.ArgumentParser) -> None:
    """Options du groupe par défaut rangées par thème (Sources, Sortie, Synchro…), dans l'ordre de ``GROUPS``."""
    default = next((group for group in parser._action_groups if group.title in ("options", "optional arguments")),
                   None)
    if default is None:
        return
    groups: dict[str, Any] = {}
    for action in list(default._group_actions):
        title = next((_GROUP_OF[option] for option in action.option_strings if option in _GROUP_OF), None)
        if title is None:
            continue
        if title not in groups:
            groups[title] = parser.add_argument_group(title)
        default._group_actions.remove(action)
        groups[title]._group_actions.append(action)
    # Positionnels et options générales, puis Sources, Sortie, Pistes, Synchro, groupes propres à la commande
    # (Encodage, HDR…), TMDB, Journal et exécution.
    order = [title for title, _options in GROUPS]
    first = [group for group in parser._action_groups if group.title in ("positional arguments", default.title)]

    def rank(group: Any) -> tuple[int, int]:
        title = str(group.title)
        if title in order:
            position = order.index(title)
            return (position if position < order.index("TMDB") else position + 1, 0)
        return (order.index("TMDB"), parser._action_groups.index(group))

    rest = sorted((group for group in parser._action_groups if group not in first), key=rank)
    parser._action_groups[:] = first + rest


def complete(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Complète l'aide de toutes les commandes : textes manquants, descriptions, exemples, groupes, formateur."""
    parser.formatter_class = HelpFormatter
    for path, sub in walk(parser):
        name = " ".join(path)
        sub.formatter_class = HelpFormatter
        entry = COMMANDS.get(name)
        if entry:
            description, examples = entry
            if not sub.description:
                sub.description = description
            if not sub.epilog and examples:
                sub.epilog = _examples(examples)
        for action in sub._actions:
            if action.help is None and not isinstance(action, argparse._SubParsersAction):
                key = action.option_strings[0] if action.option_strings else ""
                action.help = OPTION_HELP.get(key) or POSITIONAL_HELP.get((name, action.dest))
        _regroup(sub)
    _home_page(parser)
    return parser


def _home_page(parser: argparse.ArgumentParser) -> None:
    """Page d'accueil : commandes par famille, avec leur résumé."""
    commands = dict(_subcommands(parser))
    summaries: dict[str, str] = {}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            summaries = {choice.dest: str(choice.help or "") for choice in action._choices_actions}
            action.help = "Commande (liste ci-dessous)."
            action._choices_actions = []
            action.metavar = "COMMANDE"
    lines = ["Commandes (muxiveo --cli help COMMANDE pour le détail) :"]
    for family, names in FAMILIES:
        lines.append("")
        lines.append(f"{family} :")
        for name in names:
            if name in commands:
                lines.append(f"  {name:<16}{summaries.get(name, '')}")
    parser.epilog = "\n".join(lines)


# ---------------------------------------------------------------------------
# Commande help et référence Markdown
# ---------------------------------------------------------------------------


def _parser_for(root: argparse.ArgumentParser, path: list[str]) -> argparse.ArgumentParser:
    current = root
    for name in path:
        found = dict(_subcommands(current)).get(name)
        if found is None:
            from core.jobs.constants import EXIT_ARGS
            from core.jobs.errors import JobError

            raise JobError(f"Commande inconnue : {' '.join(path)} (muxiveo --cli help).", EXIT_ARGS)
        current = found
    return current


def _markdown_usage(parser: argparse.ArgumentParser, width: int = 110) -> str:
    """Usage replié par Muxiveo, identique quelle que soit la version de Python.

    Le découpage d'argparse change selon les versions (3.12 sépare « -o » de « OUTPUT ») : l'usage est
    remis sur une ligne puis replié entre groupes complets (« [...] », « (...) »).
    """
    words = parser.format_usage().replace("usage: ", "", 1).split()
    prog = len(parser.prog.split())
    groups: list[str] = [" ".join(words[:prog])]
    current: list[str] = []
    depth = 0
    for word in words[prog:]:
        current.append(word)
        depth += word.count("[") + word.count("(") - word.count("]") - word.count(")")
        if depth <= 0:
            groups.append(" ".join(current))
            current, depth = [], 0
    if current:
        groups.append(" ".join(current))
    indent = " " * (len(groups[0]) + 1)
    lines = [groups[0]]
    for group in groups[1:]:
        if len(lines[-1]) + 1 + len(group) <= width:
            lines[-1] += " " + group
        else:
            lines.append(indent + group)
    return "\n".join(lines)


def render_markdown(root: argparse.ArgumentParser, language: str = "fra") -> str:
    """Référence Markdown de toutes les commandes et du catalogue de l'API (source de docs/cli/reference.md)."""
    from core.service.catalog import CATALOG

    def t(text: str) -> str:
        return translate_text(text, language=language) if text else text

    lines = ["# " + t("Référence du CLI Muxiveo"), "",
             t("Document généré par `muxiveo --cli help --format md` : ne pas modifier à la main."), ""]
    for family, names in FAMILIES:
        lines += ["## " + t(family), ""]
        for name in names:
            parser = dict(_subcommands(root)).get(name)
            if parser is None:
                continue
            for path, sub in [((name,), parser), *walk(parser, (name,))]:
                if any(isinstance(action, argparse._SubParsersAction) for action in sub._actions) \
                        and len(path) == 1 and not sub.description:
                    continue
                lines += [f"### `{' '.join(path)}`", ""]
                if sub.description:
                    lines += [t(sub.description), ""]
                lines += ["```text", _markdown_usage(sub), "```", ""]
                rows = []
                for action in sub._actions:
                    if isinstance(action, (argparse._HelpAction, argparse._SubParsersAction)):
                        continue
                    label = ", ".join(f"`{option}`" for option in action.option_strings) or f"`{action.dest}`"
                    help_text = t(action.help or "").replace("|", "\\|").replace("\n", " ")
                    default = action.default
                    if (default not in (None, False, "", [], argparse.SUPPRESS) and action.option_strings
                            and "défaut" not in (action.help or "").lower()):
                        help_text += f" ({t('défaut')} : `{default}`)"
                    if action.choices and not isinstance(action.choices, dict):
                        help_text += " — " + ", ".join(f"`{choice}`" for choice in action.choices)
                    rows.append(f"| {label} | {help_text} |")
                if rows:
                    lines += [f"| {t('Option')} | {t('Effet')} |", "|---|---|", *rows, ""]
                if sub.epilog and sub.epilog.startswith("Exemples"):
                    lines += [f"**{t('Exemples')}**", "", "```bash",
                              *[line.strip() for line in sub.epilog.splitlines()[1:] if line.strip()], "```", ""]
    lines += ["## " + t("Opérations de la couche de service"), "",
              t("Appelables par `muxiveo --cli call OPÉRATION --request requête.json` ; schémas : "
                "`operations --describe OPÉRATION`."), "",
              f"| {t('Opération')} | {t('Rôle')} | {t('Nature')} |", "|---|---|---|"]
    for spec in CATALOG:
        nature = ", ".join(word for flag, word in ((spec.long_running, t("longue")), (spec.mutating, t("écrit")),
                                                   (spec.uses_gpu, "GPU")) if flag) or "—"
        lines.append(f"| `{spec.name}` | {t(spec.summary).replace('|', '/')} | {nature} |")
    return "\n".join(lines).rstrip() + "\n"


def add_help_parser(sub: Any) -> None:
    parser = sub.add_parser("help", help="Aide d'une commande, de toutes, ou référence Markdown.")
    parser.add_argument("topic", nargs="*", metavar="COMMANDE", help="Commande (et sous-commande), ex. profile apply.")
    parser.add_argument("--all", dest="all_commands", action="store_true", help="Aide de toutes les commandes.")
    parser.add_argument("--format", dest="help_format", choices=("text", "md"), default="text",
                        help="text (terminal) ou md (référence Markdown, docs/cli/reference.md).")
    parser.add_argument("--language", default=None, help="Langue de l'aide (fra, eng ; défaut : [ui] language).")
    parser.add_argument("--log-format", default="text", help=argparse.SUPPRESS)
    parser.set_defaults(func=cmd_help)


def cmd_help(args: argparse.Namespace, _config: Any, _logger: Any) -> int:
    from cli.parser import build_parser

    root = build_parser()
    language = args.language or help_language()
    HelpFormatter.language = language
    if args.help_format == "md":
        print(render_markdown(root, language=args.language or "fra"), end="")
        return 0
    if args.all_commands:
        for path, sub in walk(root):
            print("=" * 100)
            print(sub.format_help())
        return 0
    print(_parser_for(root, list(args.topic)).format_help())
    return 0
