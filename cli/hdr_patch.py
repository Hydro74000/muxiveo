"""Commande ``hdr-patch`` : correction des métadonnées HDR sans réencodage (opération ``hdr.patch``)."""

from __future__ import annotations

import argparse
import json
from typing import Any

from core.config import AppConfig
from core.jobs.constants import EXIT_ARGS, EXIT_OK
from core.jobs.errors import JobError
from core.service.context import CancelToken

from cli.constants import LOG_FORMATS
from cli.interrupts import interrupt_guard
from cli.logging import Logger
from cli.service_adapter import envelope, overrides_from_args, print_json, run_operation

_REALIGN = ("all", "static", "mdcv", "l6", "hdr10plus")


def _numbers(value: str, count: int, label: str, *, sep: str = ":") -> list[float]:
    parts = [part.strip().replace(",", ".") if sep != "," else part.strip() for part in value.split(sep)]
    try:
        numbers = [float(part) for part in parts]
    except ValueError as exc:
        raise JobError(f"{label} : {count} nombres séparés par « {sep} » attendus.", EXIT_ARGS) from exc
    if len(numbers) != count:
        raise JobError(f"{label} : {count} nombres séparés par « {sep} » attendus.", EXIT_ARGS)
    return numbers


def add_hdr_patch_parser(sub: Any) -> None:
    parser = sub.add_parser(
        "hdr-patch",
        help="Corriger les métadonnées HDR sans réencodage (onglet Édition du HDR Studio).",
        description="Écrit une copie corrigée d'un MKV ou d'un HEVC brut : HDR10 statique (MaxCLL/MaxFALL, MDCV), "
                    "RPU Dolby Vision (L6, L5, L11, écran du RPU, retrait CM v4.0), HDR10+ (retrait, TSDML, profil A, "
                    "plafond, recalcul depuis la mesure), ou propositions de réalignement d'après la mesure de "
                    "l'image. Les autres pistes, chapitres, pièces jointes et balises sont conservés.",
        epilog="Exemples :\n"
               "  hdr-patch -i film.mkv -o film.hdrfix.mkv --max-cll 1000 --max-fall 400\n"
               "  hdr-patch -i film.mkv -o fix.mkv --light-levels suggested --mdcv suggested --dry-run\n"
               "  hdr-patch -i film.mkv -o fix.mkv --mdcv 'Display P3:0.0001:1000' --l6 1000:0.0001:1000:400\n"
               "  hdr-patch -i film.mkv -o fix.mkv --realign all --measure fast\n"
               "  hdr-patch -i film.mkv -o fix.mkv --hdr10plus-recompute --measure full",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-i", "--input", required=True, metavar="FICHIER", help="Source (MKV ou HEVC brut).")
    parser.add_argument("-o", "--output", required=True, metavar="SORTIE", help="Fichier corrigé (autre que la source).")
    parser.add_argument("--hdr-track", type=int, default=None, metavar="N",
                        help="Index ffprobe de la piste vidéo (défaut : première).")
    static = parser.add_argument_group("HDR10 statique")
    static.add_argument("--max-cll", type=int, metavar="NITS", help="MaxCLL écrite (avec --max-fall).")
    static.add_argument("--max-fall", type=int, metavar="NITS", help="MaxFALL écrite (avec --max-cll).")
    static.add_argument("--light-levels", choices=("suggested",),
                        help="MaxCLL/MaxFALL proposés : mesure de l'image > HDR10+ > L1 (--max-cll/--max-fall "
                             "les remplacent un à un).")
    static.add_argument("--mdcv", metavar="suggested|PRIMAIRES:MIN:MAX",
                        help="Écran de mastering : « suggested » (L6 > écran du RPU > écran usuel) ou "
                             "« Display P3|BT.2020|BT.709:MIN:MAX » en nits.")
    rpu = parser.add_argument_group("Dolby Vision (RPU)")
    rpu.add_argument("--l6", metavar="MAX:MIN:CLL:FALL", help="L6 : mastering max. et min. (nits), MaxCLL, MaxFALL.")
    rpu.add_argument("--l5", metavar="G,D,H,B", help="Zone active L5 de toutes les images (pixels).")
    rpu.add_argument("--l11", metavar="TYPE[:POINT_BLANC[:ref]]",
                     help="L11 : type de contenu 0–4 (défaut, cinéma, jeu, sport, utilisateur), point blanc 0–15, "
                          "« ref » : mode de référence.")
    rpu.add_argument("--rpu-display", metavar="MIN:MAX", help="Écran de mastering du RPU (nits).")
    rpu.add_argument("--remove-cmv4", action="store_true", help="Retirer les métadonnées CM v4.0.")
    rpu.add_argument("--no-mastering-sync", dest="sync_mastering", action="store_false",
                     help="Ne pas garder identiques MDCV, mastering du L6 et écran du RPU (recopie par défaut).")
    hdr10plus = parser.add_argument_group("HDR10+")
    hdr10plus.add_argument("--hdr10plus-remove", action="store_true", help="Retirer le HDR10+.")
    hdr10plus.add_argument("--hdr10plus-target", type=int, metavar="NITS",
                           help="TargetedSystemDisplayMaximumLuminance de toutes les images.")
    hdr10plus.add_argument("--hdr10plus-profile-a", action="store_true", help="Retirer la courbe de Bézier (profil A).")
    hdr10plus.add_argument("--hdr10plus-cap", type=float, metavar="NITS",
                           help="Plafond des MaxScl, AverageRGB et percentiles.")
    hdr10plus.add_argument("--hdr10plus-recompute", action="store_true",
                           help="Recalculer le HDR10+ depuis la mesure complète de l'image (profil A).")
    measure = parser.add_argument_group("Mesure et réalignement")
    measure.add_argument("--realign", metavar="all|static,mdcv,l6,hdr10plus",
                         help="Retenir les propositions de réalignement d'après la mesure (valeurs explicites "
                              "prioritaires).")
    measure.add_argument("--measure", choices=("cached", "fast", "full"), default="cached",
                         help="Mesure de l'image : en cache seulement (défaut), rapide (images clés) ou complète.")
    measure.add_argument("--display-peak", type=float, metavar="NITS",
                         help="Écran simulé du réalignement (défaut : réglage [hdr] display_peak_nits).")
    parser.add_argument("--force", action="store_true", help="Remplacer la sortie si elle existe.")
    parser.add_argument("--dry-run", action="store_true", help="Afficher le plan résolu sans écrire.")
    parser.add_argument("--json", dest="json_output", action="store_true", help="Résultat JSON sur stdout.")
    parser.add_argument("--ffmpeg", help="Chemin ffmpeg override.")
    parser.add_argument("--ffprobe", help="Chemin ffprobe override.")
    parser.add_argument("--mediainfo", help="Chemin mediainfo override.")
    parser.add_argument("--work-dir", help="Répertoire de travail override.")
    parser.add_argument("--threads", type=int, help="Fils de la mesure de l'image.")
    parser.add_argument("--log-format", choices=LOG_FORMATS, default="text")
    parser.add_argument("--verbose", action="store_true", help="Journal détaillé.")
    parser.set_defaults(func=cmd_hdr_patch)


def build_request(args: argparse.Namespace) -> dict[str, Any]:
    request: dict[str, Any] = {"source": args.input, "output": args.output, "measurement": args.measure,
                               "sync_mastering": bool(args.sync_mastering), "force": bool(args.force),
                               "dry_run": bool(args.dry_run)}
    if args.hdr_track is not None:
        request["track"] = args.hdr_track
    if args.display_peak is not None:
        request["display_peak"] = args.display_peak
    if args.light_levels or args.max_cll is not None or args.max_fall is not None:
        levels: dict[str, Any] = {"mode": args.light_levels or "manual"}
        levels.update({key: value for key, value in (("max_cll", args.max_cll), ("max_fall", args.max_fall))
                       if value is not None})
        request["light_levels"] = levels
    if args.mdcv:
        if args.mdcv.strip().lower() == "suggested":
            request["mastering"] = {"mode": "suggested"}
        else:
            primaries, sep, rest = args.mdcv.partition(":")
            if not sep:
                raise JobError("--mdcv : « suggested » ou « PRIMAIRES:MIN:MAX » attendu.", EXIT_ARGS)
            low, high = _numbers(rest, 2, "--mdcv")
            request["mastering"] = {"primaries": primaries.strip(), "min_nits": low, "max_nits": high}
    rpu: dict[str, Any] = {}
    if args.l6:
        high, low, cll, fall = _numbers(args.l6, 4, "--l6")
        rpu["l6"] = {"max_mastering_nits": int(high), "min_mastering_nits": low, "max_cll": int(cll),
                     "max_fall": int(fall)}
    if args.l5:
        left, right, top, bottom = (int(v) for v in _numbers(args.l5, 4, "--l5", sep=","))
        rpu["active_area"] = {"left": left, "right": right, "top": top, "bottom": bottom}
    if args.l11:
        parts = args.l11.split(":")
        try:
            l11: dict[str, Any] = {"content_type": int(parts[0])}
            if len(parts) > 1 and parts[1]:
                l11["whitepoint"] = int(parts[1])
        except ValueError as exc:
            raise JobError("--l11 : TYPE[:POINT_BLANC[:ref]] attendu.", EXIT_ARGS) from exc
        if len(parts) > 2:
            l11["reference_mode"] = parts[2].strip().lower() in ("ref", "1", "true", "oui")
        rpu["l11"] = l11
    if args.rpu_display:
        low, high = _numbers(args.rpu_display, 2, "--rpu-display")
        rpu["source_display"] = {"min_nits": low, "max_nits": high}
    if args.remove_cmv4:
        rpu["remove_cmv4"] = True
    if rpu:
        request["rpu"] = rpu
    hdr10plus: dict[str, Any] = {}
    for key, value in (("remove", args.hdr10plus_remove), ("profile_a", args.hdr10plus_profile_a),
                       ("recompute", args.hdr10plus_recompute)):
        if value:
            hdr10plus[key] = True
    if args.hdr10plus_target is not None:
        hdr10plus["targeted_display_nits"] = args.hdr10plus_target
    if args.hdr10plus_cap is not None:
        hdr10plus["cap_nits"] = args.hdr10plus_cap
    if hdr10plus:
        request["hdr10plus"] = hdr10plus
    if args.realign:
        keys = [key.strip() for key in args.realign.split(",") if key.strip()]
        unknown = [key for key in keys if key not in _REALIGN]
        if unknown or not keys:
            raise JobError(f"--realign : valeurs parmi {', '.join(_REALIGN)}.", EXIT_ARGS)
        request["realign"] = keys
    return request


def cmd_hdr_patch(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    request = build_request(args)
    with interrupt_guard(CancelToken()) as cancel:
        result, ctx = run_operation("hdr.patch", request, config=config, logger=logger,
                                    overrides=overrides_from_args(args), cancel=cancel)
    if args.log_format == "events":
        print_json(envelope("hdr.patch", ctx.events.job, result=result))
    elif args.json_output or args.dry_run:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, allow_nan=False))
    else:
        logger.emit("info", f"Fichier corrigé écrit : {getattr(result, 'output', '')}")
    return EXIT_OK
