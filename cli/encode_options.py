"""Options courtes d'encodage du CLI : traduites en bloc ``encode`` du job (priorité la plus haute)."""

from __future__ import annotations

import argparse
import json
import re
from typing import Any

from core.jobs.constants import EXIT_ARGS
from core.jobs.errors import JobError
from core.jobs.batch import merge_encode_block


def add_encode_options(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("Encodage (bloc `encode` du job)")
    group.add_argument("--encode-preset", metavar="NOM|FICHIER", help="Preset d'encodage enregistré ou fichier JSON.")
    group.add_argument("--video-codec", metavar="CODEC", help="Codec vidéo (copy, libx265, hevc_nvenc…).")
    group.add_argument("--rate-control", metavar="MODE", help="Mode de débit du codec (crf, vbr_cq, cbr…).")
    group.add_argument("--crf", type=int, help="Qualité constante des codecs logiciels.")
    group.add_argument("--cq", type=int, help="Qualité constante des codecs matériels.")
    group.add_argument("--bitrate", type=int, metavar="KBPS", help="Débit vidéo (kbit/s).")
    group.add_argument("--target-size", type=int, metavar="MIO", help="Taille du fichier visée (mode taille).")
    group.add_argument("--encoder-preset", metavar="PRESET", help="Préréglage de l'encodeur (slow, p5…).")
    group.add_argument("--bit-depth", choices=("auto", "8", "10"), help="Profondeur de sortie.")
    group.add_argument("--crop", metavar="auto|HAUT,BAS,GAUCHE,DROITE",
                       help="Recadrage : auto (bandes noires mesurées) ou valeurs en pixels.")
    group.add_argument("--resize", metavar="PRESET|LxH|N%", help="Redimensionnement (1080p, 1920x1080, 50%%).")
    group.add_argument("--deinterlace", action="store_true", help="Désentrelacement YADIF.")
    group.add_argument("--denoise", metavar="mvo-r2[:NIVEAU]|nlmeans[:FORCE]|off",
                       help="Débruitage : MVO-R2 (auto, leger, moyen, fort) ou NLMeans ; off le désactive.")
    group.add_argument("--interpolate", metavar="xN|CADENCE",
                       help="Interpolation MVO-RIFE : facteur (x2, x2.5…) ou cadence cible (60, 60000/1001).")
    group.add_argument("--interpolation-preset", choices=("fast", "balanced", "quality", "ultra", "light"))
    group.add_argument("--tonemap", nargs="?", const="hable", metavar="ALGO", help="Tone mapping vers SDR.")
    group.add_argument("--dv", choices=("copy", "off"), help="Dolby Vision de la source : copié ou retiré.")
    group.add_argument("--hdr10plus", choices=("copy", "generate", "off"),
                       help="HDR10+ : copié de la source, généré depuis son Dolby Vision (remplace celui de la "
                            "source) ou retiré.")
    group.add_argument("--bake-fel", action="store_true", help="Intégrer la couche Dolby Vision FEL (mvo-fel).")
    group.add_argument("--audio-codec", action="append", metavar="SOURCE:FLUX=CODEC[:KBPS]",
                       help="Réencoder une piste audio (ex. 0:1=eac3:640) ; répétable.")
    group.add_argument("--video-track", metavar="SOURCE:FLUX",
                       help="Options vidéo ci-dessus appliquées à cette piste seulement (défaut : toutes).")
    group.add_argument("--accept-dovi-nvenc-crop", action="store_true",
                       help="Accepter le recadrage aligné sur 32 imposé par Dolby Vision + NVENC.")
    group.add_argument("--keep-failed-work-dir", action="store_true",
                       help="Encodage en échec : conserver le dossier de travail (fichiers produits).")
    group.add_argument("--video-set", action="append", metavar="CLÉ.POINTÉE=VALEUR",
                       help="Champ vidéo du bloc encode (ex. restoration.mode=fast) ; valeur JSON ou texte.")


def _selector(text: str, option: str) -> dict[str, int]:
    match = re.fullmatch(r"(\d+):(\d+)", text.strip())
    if match is None:
        raise JobError(f"{option} : SOURCE:FLUX attendu (ex. 0:1), reçu {text!r}.", EXIT_ARGS)
    return {"source": int(match.group(1)), "stream": int(match.group(2))}


def _set_path(target: dict[str, Any], dotted: str, value: Any) -> None:
    keys = [key for key in dotted.split(".") if key]
    if not keys:
        raise JobError(f"--video-set : clé vide ({dotted!r}).", EXIT_ARGS)
    node = target
    for key in keys[:-1]:
        node = node.setdefault(key, {})
        if not isinstance(node, dict):
            raise JobError(f"--video-set : {dotted} traverse une valeur simple.", EXIT_ARGS)
    node[keys[-1]] = value


def _video_spec(args: argparse.Namespace) -> dict[str, Any]:
    spec: dict[str, Any] = {}
    for option, key in (("video_codec", "codec"), ("rate_control", "rate_control"), ("crf", "crf"), ("cq", "cq"),
                        ("bitrate", "bitrate_kbps"), ("encoder_preset", "encoder_preset"),
                        ("bit_depth", "bit_depth")):
        value = getattr(args, option, None)
        if value is not None:
            spec[key] = value
    crop = getattr(args, "crop", None)
    if crop:
        if crop.strip().lower() == "auto":
            spec["crop"] = {"auto": True}
        else:
            parts = [part.strip() for part in crop.split(",")]
            if len(parts) != 4 or not all(part.isdigit() for part in parts):
                raise JobError("--crop : auto ou HAUT,BAS,GAUCHE,DROITE (pixels) attendu.", EXIT_ARGS)
            top, bottom, left, right = (int(part) for part in parts)
            spec["crop"] = {"unit": "px", "top": top, "bottom": bottom, "left": left, "right": right}
    resize = getattr(args, "resize", None)
    if resize:
        size = re.fullmatch(r"(\d+)\s*[x×]\s*(\d+)", resize.strip())
        if size:
            spec["resize"] = {"mode": "size", "width": int(size.group(1)), "height": int(size.group(2))}
        elif resize.strip().endswith("%") and resize.strip()[:-1].isdigit():
            spec["resize"] = {"mode": "percent", "percent": int(resize.strip()[:-1])}
        else:
            spec["resize"] = {"mode": "preset", "preset": resize.strip()}
    if getattr(args, "deinterlace", False):
        spec.setdefault("filters", {})["yadif_enabled"] = True
    denoise = getattr(args, "denoise", None)
    if denoise:
        kind, _sep, level = denoise.strip().lower().partition(":")
        if kind == "off":
            spec.setdefault("filters", {})["nlmeans_enabled"] = False
            spec["restoration"] = {"enabled": False}
        elif kind == "mvo-r2":
            spec.setdefault("filters", {})["denoise_filter"] = "mvo-r2"
            spec["restoration"] = {"enabled": True, **({"level": level} if level else {})}
        elif kind == "nlmeans":
            spec.setdefault("filters", {}).update(denoise_filter="nlmeans", nlmeans_enabled=True,
                                                  **({"nlmeans_strength": level} if level else {}))
            spec["restoration"] = {"enabled": False}
        else:
            raise JobError("--denoise : mvo-r2[:NIVEAU], nlmeans[:FORCE] ou off attendu.", EXIT_ARGS)
    interpolate = getattr(args, "interpolate", None)
    if interpolate:
        text = interpolate.strip().lower()
        if text.startswith("x"):
            try:
                spec["interpolation"] = {"factor": float(text[1:])}
            except ValueError as exc:
                raise JobError("--interpolate : facteur xN attendu (ex. x2).", EXIT_ARGS) from exc
        else:
            spec["interpolation"] = {"target_fps": text}
    preset = getattr(args, "interpolation_preset", None)
    if preset:
        spec.setdefault("interpolation", {})["quality"] = preset
    tonemap = getattr(args, "tonemap", None)
    if tonemap:
        spec["tonemap"] = {"enabled": True, "algorithm": tonemap}
    if getattr(args, "dv", None):
        spec["dolby_vision"] = {"mode": args.dv}
    if getattr(args, "hdr10plus", None):
        spec["hdr10plus"] = {"mode": args.hdr10plus}
    if getattr(args, "bake_fel", False):
        spec["fel"] = {"bake": True}
    for item in getattr(args, "video_set", None) or []:
        key, sep, raw = item.partition("=")
        if not sep:
            raise JobError(f"--video-set : CLÉ=VALEUR attendu, reçu {item!r}.", EXIT_ARGS)
        parsed: Any
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = raw
        _set_path(spec, key.strip(), parsed)
    return spec


def encode_block_from_args(args: argparse.Namespace) -> dict[str, Any] | None:
    """Bloc ``encode`` des options courtes ; ``None`` si aucune n'est donnée."""
    block: dict[str, Any] = {}
    if getattr(args, "encode_preset", None):
        block["preset"] = args.encode_preset
    if getattr(args, "target_size", None) is not None:
        block["target_size_mb"] = args.target_size
    if getattr(args, "accept_dovi_nvenc_crop", False):
        block["accept_dovi_nvenc_crop"] = True
    spec = _video_spec(args)
    if spec:
        track = getattr(args, "video_track", None)
        if track:
            block["video"] = {"tracks": [{"select": _selector(track, "--video-track"), **spec}]}
        else:
            block["video"] = {"default": spec}
    elif getattr(args, "video_track", None):
        raise JobError("--video-track : aucune option vidéo à appliquer.", EXIT_ARGS)
    audio = []
    for item in getattr(args, "audio_codec", None) or []:
        selector, sep, rest = item.partition("=")
        if not sep:
            raise JobError(f"--audio-codec : SOURCE:FLUX=CODEC[:KBPS] attendu, reçu {item!r}.", EXIT_ARGS)
        codec, _sep, kbps = rest.partition(":")
        entry: dict[str, Any] = {"select": _selector(selector, "--audio-codec"), "codec": codec.strip()}
        if kbps:
            if not kbps.strip().isdigit():
                raise JobError(f"--audio-codec : débit entier attendu, reçu {kbps!r}.", EXIT_ARGS)
            entry["bitrate_kbps"] = int(kbps)
        audio.append(entry)
    if audio:
        block["audio"] = audio
    return block or None


def apply_encode_options(job: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    """Job avec les options courtes fusionnées dans son bloc ``encode`` (elles priment)."""
    block = encode_block_from_args(args)
    return job if block is None else merge_encode_block(job, block)
