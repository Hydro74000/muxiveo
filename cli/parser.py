"""Argparse construction for muxiveo --cli."""

from __future__ import annotations

import argparse

from cli.hdr import add_hdr_options
from cli.commands import cmd_batch, cmd_capabilities, cmd_inspect, cmd_preview, cmd_profile, cmd_remux, cmd_run, cmd_schema, cmd_tools, cmd_validate, cmd_version
from cli.constants import LOG_FORMATS
from cli.encode_options import add_encode_options
from cli.admin import add_admin_parsers
from cli.calibration import add_calibration_parsers
from cli.extract import add_extract_parser
from cli.help import add_help_parser
from cli.help import complete as complete_help
from cli.library import add_library_parsers, add_profile_management_parsers
from cli.hdr_patch import add_hdr_patch_parser
from cli.plugins import add_plugins_parser
from cli.encode_commands import cmd_encode_preset
from cli.service_adapter import cmd_call, cmd_operations
from core.version import APP_EXECUTABLE_NAME, APP_NAME


class _ExplicitStore(argparse.Action):
    """Valeur saisie, mémorisée comme explicite (``explicit_options``) : distingue une option fournie égale au défaut
    d'une option absente (``hybrid`` : la recette prime seulement sur une option absente)."""

    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)
        explicit = set(getattr(namespace, "explicit_options", ()) or ())
        explicit.add(self.dest)
        namespace.explicit_options = explicit


def _add_sync_options(parser):
    parser.add_argument("--sync-mode", choices=("physical", "container"), default=None, action=_ExplicitStore)
    parser.add_argument("--sync-subtitles", choices=("mirror", "none"), default=None, action=_ExplicitStore)
    parser.add_argument("--clean-nfo", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--remove-closed-captions", action=argparse.BooleanOptionalAction, default=None,
                        help="Retirer les sous-titres embarqués EIA-608/708 détectés, sans réencodage")
    parser.add_argument("--crossfade-ms", type=int, default=None, action=_ExplicitStore)
    parser.add_argument("--auto-forced-subs", action="store_true")
    parser.add_argument("--forced-threshold", type=int, default=50)
    parser.add_argument("--auto-sdh", action="store_true")
    parser.add_argument("--auto-sync", action="store_true", help="Analyser et recalibrer automatiquement les sources dynamiquement.")
    parser.add_argument("--auto-sync-all", action="store_true",
                        help="Synchroniser chaque source ≥ 1 face à la source 0 (sources sans bloc `sync` du job).")
    parser.add_argument("--calibration", help="Fichier JSON de calibration explicite (outrepasse l'analyse dynamique).")
    parser.add_argument("--detect-cuts", action="store_true", help="Détecter les coupures et ruptures temporelles (multi-segments).")
    parser.add_argument(
        "--quick-sync-check",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Avec --detect-cuts : pas d'analyse continue si les mesures ponctuelles concordent "
             "(confiance ≥ 0,90). Défaut : [sync] quick_audio_check.",
    )
    parser.add_argument(
        "--reject-coherent-sync",
        action="store_true",
        help="Avec --detect-cuts : refuser une synchro cohérente en partie non vérifiée (zones sans "
             "correspondance entourées du même décalage). Défaut : acceptée avec un avertissement.",
    )
    parser.add_argument("--drift-threshold-ms", type=int, default=25, help="Seuil de dérive en ms pour détecter une coupure.")
    parser.add_argument(
        "--cadence-auto",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Détection et conversion automatique de cadence PAL <-> Cinéma.",
    )
    parser.add_argument(
        "--cadence-method",
        choices=("auto", "atempo", "asetrate"),
        default="auto",
        help="Méthode de conversion audio pour cadence : auto (analyse acoustique du pitch, défaut), atempo (tonalité préservée) ou asetrate (hauteur naturelle / pitch shift).",
    )
    parser.add_argument("--export-workflow", help="Sauvegarder le workflow exact sans exécuter.")


def _add_base_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", help="Fichier JSON job/template.")
    parser.add_argument("--ffmpeg", help="Chemin ffmpeg override.")
    parser.add_argument("--ffprobe", help="Chemin ffprobe override.")
    parser.add_argument("--mediainfo", help="Chemin mediainfo override.")
    parser.add_argument("--work-dir", help="Répertoire de travail override.")
    parser.add_argument("--threads", type=int, help="Nombre de threads ffmpeg.")
    parser.add_argument(
        "--mux-backend", choices=("auto", "native", "ffmpeg"), default=None,
        help="Backend MKV : auto (défaut), native ou ffmpeg. Prioritaire sur le job.",
    )
    parser.add_argument(
        "--output-template",
        dest="output_template",
        default="",
        help=(
            "Template du nom de sortie. Placeholders disponibles : "
            "{source_name},{title},{year},{season},{episode},{episode_title},"
            "{season_episode},{group}, keywords pistes comme {audio-lang:best} "
            "(+ {season_num},{episode_num} pour formats numériques). Extension "
            ".mkv ajoutée si absente."
        ),
    )
    parser.add_argument(
        "--output-all",
        action="store_true",
        help="Force les keywords pistes de --output-template en mode all (toutes les valeurs finales trouvées).",
    )
    parser.add_argument("--log-format", choices=LOG_FORMATS, default="text")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Affiche la sortie ffmpeg en direct (progression, codecs, timing). Par défaut, seule la progression des étapes de workflow est affichée.",
    )


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    _add_sync_options(parser)
    _add_base_options(parser)


def _add_tmdb_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--auto-tmdb", action="store_true", help="Chercher TMDB automatiquement et prendre le premier résultat.")
    parser.add_argument("--tmdb", action="store_true", help="Alias historique de --auto-tmdb.")
    parser.add_argument("--tmdb-id", type=int, help="ID TMDB explicite.")
    parser.add_argument("--tmdb-apikey", dest="tmdb_apikey", default="", help=f"Clé API TMDB v3 (surcharge la config {APP_NAME} et le JSON job).")
    parser.add_argument("--no-cover", action="store_true", help="Ne pas ajouter la cover TMDB.")
    parser.add_argument("--no-attach", action="store_true", help="Ne pas inclure d'attachments ni de cover TMDB.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"{APP_EXECUTABLE_NAME} --cli", description=f"{APP_NAME} en ligne de commande (sans interface).")
    sub = parser.add_subparsers(dest="command", required=True)

    inspect = sub.add_parser("inspect", help="Inspecter une ou plusieurs sources.")
    _add_common_options(inspect)
    inspect.add_argument("input", nargs="+")
    inspect.add_argument("--config-template", action="store_true")
    inspect.add_argument("--with-encode", action="store_true",
                         help="Avec --config-template : bloc `encode` de départ (copie, HDR de la source).")
    inspect.add_argument("--output")
    add_hdr_options(inspect)
    inspect.set_defaults(func=cmd_inspect)

    schema = sub.add_parser("schema", help="Afficher le schéma JSON du contrat CLI.")
    schema.add_argument("--output", help="Ecrire le schéma JSON dans un fichier.")
    schema.add_argument("--version", dest="schema_version", choices=("1", "exact-job", "decision-profile", "all", "api"), default="1",
                        help="Contrat à publier ; « api » : catalogue des opérations de la couche de service.")
    schema.add_argument("--log-format", choices=LOG_FORMATS, default="text")
    schema.set_defaults(func=cmd_schema)

    tools = sub.add_parser("tools", help="Afficher les chemins d'outils résolus par Muxiveo.")
    tools.add_argument("--log-format", choices=LOG_FORMATS, default="text")
    tools.set_defaults(func=cmd_tools)

    capabilities = sub.add_parser("capabilities", help="Capacités de la machine (encodeurs, Vulkan, NVOF, TensorRT…).")
    capabilities.add_argument("--section", action="append", choices=("encoders", "presets", "vulkan", "gpu", "rife"),
                              help="Section à sonder ; répétable (défaut : toutes).")
    capabilities.add_argument("--ffmpeg", help="Chemin ffmpeg pour cette sonde.")
    capabilities.add_argument("--log-format", choices=LOG_FORMATS, default="text")
    capabilities.set_defaults(func=cmd_capabilities)

    version = sub.add_parser("version", help="Afficher la version de Muxiveo.")
    version.add_argument("--check", action="store_true", help="Comparer avec la dernière release GitHub (code retour 8 si mise à jour disponible).")
    version.add_argument("--channel", choices=("stable", "unstable"), help="Canal de mise à jour (défaut : réglage [ui] update_channel).")
    version.add_argument("--install", action="store_true",
                         help="Installer la dernière version du canal (installation intégrée ; sans relancer l'interface).")
    version.add_argument("--yes", action="store_true", help="Avec --install : confirmer l'installation.")
    version.add_argument("--log-format", choices=LOG_FORMATS, default="text")
    version.set_defaults(func=cmd_version)

    add_plugins_parser(sub)
    add_extract_parser(sub)
    add_hdr_patch_parser(sub)
    add_calibration_parsers(sub)
    add_library_parsers(sub)
    add_admin_parsers(sub)

    presets = sub.add_parser("encode-preset", help="Presets d'encodage enregistrés (communs avec l'interface).")
    preset_sub = presets.add_subparsers(dest="preset_command", required=True)
    for action, help_text in (
        ("list", "Lister les presets."),
        ("show", "Afficher un preset (JSON)."),
        ("save", "Enregistrer un preset d'après les options d'encodage."),
        ("import", "Importer un preset depuis un fichier JSON."),
        ("export", "Exporter un preset dans un fichier JSON."),
        ("delete", "Supprimer un preset."),
    ):
        action_parser = preset_sub.add_parser(action, help=help_text)
        if action in ("show", "save", "export", "delete"):
            action_parser.add_argument("name", metavar="NOM")
        if action in ("import", "export"):
            action_parser.add_argument("file", metavar="FICHIER")
        if action == "import":
            action_parser.add_argument("--name", dest="import_name", help="Nom enregistré (défaut : celui du fichier).")
        if action == "save":
            action_parser.add_argument("--description", default="")
            add_encode_options(action_parser)
        if action in ("save", "import", "export"):
            action_parser.add_argument("--force", action="store_true", help="Remplacer l'existant.")
        action_parser.add_argument("--log-format", choices=LOG_FORMATS, default="text")
        action_parser.set_defaults(func=cmd_encode_preset)

    call = sub.add_parser(
        "call",
        help="Appeler une opération de la couche de service (requête JSON, réponse JSON).",
        description="Exécute une opération du catalogue (voir `operations`) avec une requête JSON stricte. "
                    "Réponse sur stdout ({status, result | error}), événements versionnés sur stderr.",
    )
    call.add_argument("operation", metavar="OPÉRATION", help="Nom de l'opération (ex. media.inspect).")
    call.add_argument("--request", metavar="FICHIER|-",
                      help="Requête JSON : fichier (chemins relatifs à son dossier) ou - pour stdin. Défaut : {}.")
    call.add_argument("--ffmpeg", help="Chemin ffmpeg pour cet appel.")
    call.add_argument("--ffprobe", help="Chemin ffprobe pour cet appel.")
    call.add_argument("--mediainfo", help="Chemin mediainfo pour cet appel.")
    call.add_argument("--work-dir", help="Dossier de travail pour cet appel.")
    call.add_argument("--threads", type=int, help="Threads ffmpeg pour cet appel.")
    call.set_defaults(func=cmd_call, log_format="events")

    operations = sub.add_parser("operations", help="Lister les opérations de la couche de service.")
    operations.add_argument("--describe", metavar="OPÉRATION",
                            help="Afficher le schéma de requête et de résultat, les erreurs et les métadonnées.")
    operations.add_argument("--json", dest="json_output", action="store_true", help="Liste au format JSON.")
    operations.add_argument("--log-format", choices=LOG_FORMATS, default="text")
    operations.set_defaults(func=cmd_operations)

    for name, help_text, func in (
        ("validate", "Valider un job (ou un profil) sans exécuter.", cmd_validate),
        ("preview", "Afficher les commandes prévues (FFmpeg, encodeur).", cmd_preview),
        ("remux", "Remuxer des sources (pistes, synchro, métadonnées).", cmd_remux),
        ("run", "Exécuter un job JSON (remux ou encodage).", cmd_run),
        ("encode", "Encoder (job remux + bloc `encode` ou options d'encodage).", cmd_run),
    ):
        p = sub.add_parser(name, help=help_text)
        _add_common_options(p)
        p.add_argument("--profile", help="Profil décisionnel à appliquer aux entrées.")
        p.add_argument("-i", "--input", action="append")
        p.add_argument("-o", "--output")
        _add_tmdb_options(p)
        p.add_argument("--force", action="store_true", help="Autoriser l'écrasement de la sortie.")
        p.add_argument("--dry-run", action="store_true", help="Valider et afficher la commande sans exécuter.")
        p.add_argument("--nfo", dest="nfo", action="store_true", default=None)
        p.add_argument("--no-nfo", dest="nfo", action="store_false")
        p.add_argument("--writing-application", default="")
        add_encode_options(p)
        if name in {"validate", "preview"}:
            p.add_argument("--json", dest="json_output", action="store_true", help="Sortie JSON structurée.")
        if name == "remux":
            p.add_argument("--save", help="Sauvegarder les options/règles en template réutilisable.")
        if name == "encode":
            preview = p.add_argument_group("Aperçu (au lieu d'encoder)")
            preview.add_argument("--preview-image", action="append", type=float, metavar="SECONDES",
                                 help="Image(s) encodée(s) à cet instant ; répétable.")
            preview.add_argument("--preview-video", metavar="SECONDES:DURÉE",
                                 help="Extrait vidéo encodé (ex. 600:10 ; durée 5–30 s).")
            preview.add_argument("--random-scene", action="store_true", help="Instant d'aperçu tiré au hasard.")
            preview.add_argument("--preview-dir", metavar="DOSSIER",
                                 help="Dossier des aperçus (défaut : celui de la sortie).")
        p.set_defaults(func=func)

    batch = sub.add_parser("batch", help="Appliquer un gabarit ou un profil à un lot.")
    _add_common_options(batch)
    batch.add_argument("--template")
    batch.add_argument("--profile", help="Profil décisionnel à appliquer au lot.")
    batch.add_argument("--batch", help="JSON contenant `jobs` ou `inputs`.")
    batch.add_argument("-i", "--input", action="append", help="Entrée batch simple; répétable.")
    batch.add_argument("--input-dir", action="append", help="Dossier à scanner pour créer un job par vidéo; répétable.")
    batch.add_argument("--recursive", action="store_true", help="Scanner récursivement les dossiers fournis avec --input-dir.")
    batch.add_argument("--include", action="append", help="Glob de fichiers à inclure lors du scan de dossiers; répétable.")
    batch.add_argument("--exclude", action="append", help="Glob de fichiers à exclure lors du scan de dossiers; répétable.")
    batch.add_argument("--output-dir")
    _add_tmdb_options(batch)
    batch.add_argument("--force", action="store_true")
    batch.add_argument("--dry-run", action="store_true", help="Valider et afficher les commandes sans exécuter.")
    batch.add_argument("--continue-on-error", action="store_true")
    batch.add_argument("--summary", help="Ecrire un rapport JSON final.")
    batch.add_argument("--nfo", dest="nfo", action="store_true", default=None)
    batch.add_argument("--no-nfo", dest="nfo", action="store_false")
    batch.add_argument("--writing-application", default="")
    add_encode_options(batch)
    batch.set_defaults(func=cmd_batch)

    profile = sub.add_parser("profile", help="Valider, prévisualiser ou appliquer un profil décisionnel.")
    profile_sub = profile.add_subparsers(dest="profile_command", required=True)

    profile_validate = profile_sub.add_parser("validate", help="Valider un decision-profile v1.")
    _add_common_options(profile_validate)
    profile_validate.add_argument("--profile", required=True)
    profile_validate.add_argument("--json", dest="json_output", action="store_true")
    profile_validate.set_defaults(func=cmd_profile)

    profile_preview = profile_sub.add_parser("preview", help="Prévisualiser un profil sur une ou plusieurs sources.")
    _add_common_options(profile_preview)
    profile_preview.add_argument("--profile", required=True)
    profile_preview.add_argument("-i", "--input", action="append", required=True)
    profile_preview.add_argument("-o", "--output")
    _add_tmdb_options(profile_preview)
    profile_preview.add_argument("--json", dest="json_output", action="store_true")
    add_encode_options(profile_preview)
    profile_preview.set_defaults(func=cmd_profile)

    profile_apply = profile_sub.add_parser("apply", help="Appliquer un profil et remuxer.")
    _add_common_options(profile_apply)
    profile_apply.add_argument("--profile", required=True)
    profile_apply.add_argument("-i", "--input", action="append", required=True)
    profile_apply.add_argument("-o", "--output", required=True)
    _add_tmdb_options(profile_apply)
    profile_apply.add_argument("--force", action="store_true")
    profile_apply.add_argument("--dry-run", action="store_true")
    add_encode_options(profile_apply)
    profile_apply.set_defaults(func=cmd_profile)

    profile_batch = profile_sub.add_parser("batch", help="Appliquer un profil à un lot de fichiers.")
    _add_common_options(profile_batch)
    profile_batch.add_argument("--profile", required=True)
    profile_batch.add_argument("-i", "--input", action="append")
    profile_batch.add_argument("--input-dir", action="append")
    profile_batch.add_argument("--recursive", action="store_true")
    profile_batch.add_argument("--include", action="append")
    profile_batch.add_argument("--exclude", action="append")
    profile_batch.add_argument("--output-dir", required=True)
    _add_tmdb_options(profile_batch)
    profile_batch.add_argument("--dry-run", action="store_true")
    profile_batch.add_argument("--force", action="store_true")
    profile_batch.add_argument("--continue-on-error", action="store_true")
    profile_batch.add_argument("--summary")
    add_encode_options(profile_batch)
    profile_batch.set_defaults(func=cmd_profile)
    add_profile_management_parsers(profile_sub, _add_common_options)
    from cli.hybrid import cmd_hybrid, cmd_sync_scan, cmd_shift_subs
    scan = sub.add_parser("sync-scan", help="Analyser le calage acoustique ou par sous-titres.")
    _add_common_options(scan)
    scan.add_argument("--ref", required=True)
    scan.add_argument("--target", required=True)
    scan.add_argument("--stream-ref", default="0:a:0")
    scan.add_argument("--stream-target", default="0:a:0")
    scan.add_argument("--type", choices=("auto", "audio", "subtitle"), default="auto", help="Type d'analyse : auto (détection par extension/flux), audio ou subtitle.")
    scan.add_argument("--output-json")
    scan.set_defaults(func=cmd_sync_scan)
    subs = sub.add_parser("shift-subs", help="Recaler des sous-titres texte.")
    _add_base_options(subs)
    subs.add_argument("-i", "--input", required=True)
    subs.add_argument("-o", "--output", required=True)
    calibration = subs.add_mutually_exclusive_group(required=True)
    calibration.add_argument("--offset-ms", type=float)
    calibration.add_argument("--calibration")
    subs.add_argument("--force", action="store_true")
    subs.set_defaults(func=cmd_shift_subs)
    hybrid = sub.add_parser("hybrid", help="Hybrider une paire ou une saison (moteur du Hybrid Studio).")
    _add_common_options(hybrid)
    for option in ("ref", "donor", "ref-dir", "donor-dir", "profile", "report-json"):
        hybrid.add_argument("--" + option)
    hybrid.add_argument("-o", "--output-dir", required=True)
    hybrid.add_argument("--auto-tmdb", type=int, nargs="?", const=0)
    hybrid.add_argument("--tmdb-apikey", default="")
    hybrid.add_argument("--no-cover", action="store_true")
    hybrid.add_argument("--tag", default="MVO")
    hybrid.add_argument("--purge-temp", action="store_true", help="Compatibilité : nettoyage toujours activé.")
    hybrid.add_argument("--dry-run", action="store_true")
    hybrid.add_argument("--force", action="store_true")
    hybrid.add_argument("--continue-on-error", action="store_true")
    studio = hybrid.add_argument_group("Moteur du Hybrid Studio (sources, appariement, recette)")
    studio.add_argument("--source", action="append", default=[], metavar="RÔLE=CHEMIN",
                        help="Source et son rôle : master, donor, donor-audio, donor-sub ; fichier ou dossier ; "
                             "répétable (--ref/--donor/--ref-dir/--donor-dir en sont des alias).")
    studio.add_argument("--matching", choices=("episode", "fuzzy", "order"), default="episode",
                        help="Appariement des épisodes : numéros SxxEyy (défaut), similarité des titres, ou ordre.")
    studio.add_argument("--pair", action="append", default=[], metavar="MASTER::DONNEUR",
                        help="Paire manuelle (MASTER::DONNEUR[::DONNEUR…]) ; répétable, exclut --source.")
    studio.add_argument("--recipe", metavar="FICHIER", help="Recette exportée par le Hybrid Studio (JSON).")
    studio.add_argument("--keep-master-audio", metavar="LANGUES", help="Langues audio du master gardées (fre,eng…).")
    studio.add_argument("--donor-audio-langs", metavar="LANGUES", help="Langues audio prises aux donneurs.")
    studio.add_argument("--donor-sub-langs", metavar="LANGUES", help="Langues de sous-titres prises aux donneurs.")
    studio.add_argument("--exclude-langs", metavar="LANGUES", help="Langues jamais retenues.")
    studio.add_argument("--sync", choices=("auto", "manual", "none"), default=None,
                        help="Synchro des donneurs : analyse (défaut), décalage manuel (--shift-ms) ou aucune.")
    studio.add_argument("--shift-ms", type=int, default=None, help="Décalage manuel des donneurs (avec --sync manual).")
    studio.add_argument("--plan", action="store_true",
                        help="Préparer sans exécuter : appariement, pistes, calibrations et sorties en JSON.")
    studio.add_argument("--export-jobs", metavar="DOSSIER",
                        help="Écrire les exact-jobs finalisés (à corriger puis lancer par run / batch).")
    studio.add_argument("--save-recipe", metavar="FICHIER", help="Écrire la recette effective (JSON).")
    add_encode_options(hybrid)
    hybrid.set_defaults(func=cmd_hybrid, sync_mode="physical", sync_subtitles="mirror", clean_nfo=True, crossfade_ms=80)
    add_help_parser(sub)
    return complete_help(parser)
