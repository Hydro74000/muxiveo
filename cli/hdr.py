"""Options ``inspect --hdr`` du CLI (opération ``hdr.inspect`` : ``core/service/ops/hdr.py``)."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from typing import Any

from cli.constants import EXIT_OK, EXIT_VALIDATION
from cli.interrupts import interrupt_guard
from cli.logging import Logger
from cli.options import common_options
from cli.service_adapter import envelope, overrides_from_options, print_json, run_operation
from core.config import AppConfig
from core.inspector import FileInspector as FileInspector  # remplaçable (tests)
from core.service.context import CancelToken
from core.service.ops.hdr import FAIL_ON as _FAIL_ON
from core.service.ops.hdr import FEL_ENCODERS as FEL_ENCODERS
from core.service.ops.hdr import FEL_MIN as _FEL_MIN
from core.service.ops.hdr import LEVELS as LEVELS
from core.service.ops.hdr import HdrInspectResult
from core.service.ops.hdr import dynamic_report as _dynamic  # remplaçable (tests)
from core.service.ops.hdr import fel_analysis as _fel_analysis  # noqa: F401 - compatibilité
from core.service.ops.hdr import measure_image as _measure  # noqa: F401 - compatibilité


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("valeur strictement positive attendue")
    return number


def _survival_spec(value: str) -> tuple[str, int]:
    """``ENCODEUR:KBPS`` (ex. ``libx265:35000``)."""
    encoder, _sep, bitrate = value.partition(":")
    if encoder not in FEL_ENCODERS or not bitrate.isdigit() or int(bitrate) <= 0:
        raise argparse.ArgumentTypeError(
            f"ENCODEUR:KBPS attendu, encodeur parmi {', '.join(FEL_ENCODERS)} (ex. libx265:35000)")
    return encoder, int(bitrate)


def add_hdr_options(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("HDR (HDR Studio)")
    group.add_argument("--hdr", choices=LEVELS,
                       help="Inspection HDR : en-têtes (static), + RPU / HDR10+ (dynamic), + mesure de chaque image "
                            "(measure, extension mvo-fel ≥ 0.4.0). Sortie JSON.")
    group.add_argument("--hdr-track", type=int, default=None,
                       help="Index ffprobe de la piste vidéo (défaut : première piste vidéo).")
    group.add_argument("--hdr-report", default="",
                       help="Rapport écrit dans ce fichier : HTML (.html) ou JSON (autre extension).")
    group.add_argument("--hdr-compare", action="store_true",
                       help="Compare la seconde source à la première (deux entrées).")
    group.add_argument("--hdr-offset", type=int, default=None,
                       help="Décalage imposé de B en images (défaut : alignement automatique).")
    group.add_argument("--hdr-fail-on", choices=tuple(_FAIL_ON), default=None,
                       help="Code de sortie 3 si un contrôle atteint ce niveau.")
    group.add_argument("--hdr-fast", action="store_true",
                       help="Avec --hdr measure : mesure rapide (images clés, extension mvo-fel ≥ 0.5.0) ; estimation.")
    group.add_argument("--hdr-no-cache", action="store_true",
                       help="Relit le fichier au lieu du résultat en cache (RPU, HDR10+, mesure).")
    group.add_argument("--hdr-fel", action="store_true",
                       help="FEL Analyzer : apport de la couche FEL d'une source Dolby Vision profil 7 (lit les "
                            "métadonnées dynamiques ; extension mvo-fel ≥ 0.4.0).")
    group.add_argument("--hdr-fel-interval", type=_positive_int, default=30, metavar="SECONDES",
                       help="Avec --hdr-fel : une image mesurée toutes les SECONDES (défaut 30 ; 60 rapide, 10 fin).")
    group.add_argument("--hdr-fel-survival", type=_survival_spec, default=None, metavar="ENCODEUR:KBPS",
                       help="Avec --hdr-fel : test de survie à l'encodage (ex. libx265:35000).")
    group.add_argument("--hdr-fel-min", choices=tuple(_FEL_MIN), default=None,
                       help="Avec --hdr-fel : code de sortie 3 si le verdict est inférieur (moderate, useful).")
    group.add_argument("--hdr-fel-moments", default=None, metavar="DOSSIER",
                       help="Avec --hdr-fel : moments les plus marqués en PNG pleine définition (sans FEL, avec FEL, "
                            "carte de l'écart).")
    group.add_argument("--hdr-fel-exposure", type=float, default=0.0, metavar="EV",
                       help="Avec --hdr-fel-moments : exposition des PNG (EV, -6 à 6 ; défaut 0).")
    group.add_argument("--hdr-advice", action="store_true",
                       help="Recommandations (clé `advice`) : MaxCLL/MaxFALL et MDCV proposés, simulation du tone "
                            "mapping d'une TV, réalignement d'après la mesure de l'image.")
    group.add_argument("--hdr-display-peak", type=float, default=None, metavar="NITS",
                       help="Avec --hdr-advice : pic de l'écran simulé (défaut : réglage [hdr] display_peak_nits).")
    group.add_argument("--hdr-tv-model", choices=("maxcll", "mdcv", "dynamic"), default="maxcll",
                       help="Avec --hdr-advice : TV suivant la MaxCLL (défaut), le MDCV seul, ou dynamique (référence).")


def cmd_inspect_hdr(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    """``inspect --hdr`` / ``--hdr-fel`` : opération ``hdr.inspect`` ; Ctrl+C annule mesure et FEL Analyzer."""
    payload: dict = {
        "inputs": list(args.input),
        "compare": bool(args.hdr_compare),
        "fast": bool(args.hdr_fast),
        "no_cache": bool(args.hdr_no_cache),
        "fel": bool(args.hdr_fel),
        "fel_interval": int(args.hdr_fel_interval),
    }
    if getattr(args, "hdr_advice", False):
        payload.update(advice=True, tv_model=args.hdr_tv_model)
        if args.hdr_display_peak is not None:
            payload["display_peak"] = args.hdr_display_peak
    if getattr(args, "hdr_fel_moments", None):
        payload.update(fel_moments_dir=args.hdr_fel_moments, fel_exposure=float(args.hdr_fel_exposure))
    for key, value in (("level", args.hdr), ("track", args.hdr_track), ("report", args.hdr_report or None),
                       ("offset", args.hdr_offset), ("fail_on", args.hdr_fail_on), ("fel_min", args.hdr_fel_min)):
        if value is not None:
            payload[key] = value
    if args.hdr_fel_survival is not None:
        encoder, bitrate = args.hdr_fel_survival
        payload["fel_survival"] = {"encoder": encoder, "bitrate_kbps": bitrate}
    ports: dict[str, Callable[..., Any]] = {
        "file_inspector": lambda **kwargs: FileInspector(**kwargs),
        "dynamic_report": lambda *a, **k: _dynamic(*a, **k),
    }
    with interrupt_guard(CancelToken()) as cancel:
        result, ctx = run_operation("hdr.inspect", payload, config=config, logger=logger,
                                    overrides=overrides_from_options(common_options(args)), ports=ports,
                                    cancel=cancel)
    assert isinstance(result, HdrInspectResult)
    if args.log_format == "events":
        print_json(envelope("hdr.inspect", ctx.events.job, result=result))
    else:
        output: dict = {"hdr": list(result.hdr)}
        if result.comparison is not None:
            output["comparison"] = result.comparison
        print(json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False))
    if result.fel_below_min or result.fail_on_triggered:
        return EXIT_VALIDATION
    return EXIT_OK
