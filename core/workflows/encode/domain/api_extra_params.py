"""Paramètres libres de l'encodeur dans une requête de l'API : seules les options de l'encodeur passent.

Le CLI historique et la GUI gardent leurs paramètres libres. Une requête de l'API (``call``, service) ne peut rien
ajouter à la commande hors de l'encodeur choisi : ni entrée, sortie, format, protocole, filtre, progression, ni
chargeur indirect d'options. Les options de l'encodeur qui nomment un fichier restent permises (écrire là où l'OS le
permet relève du modèle de sécurité retenu, comme une sortie).

- Codecs à options FFmpeg (libx264, NVENC, AMF, QSV, VAAPI) : options privées de l'encodeur
  (``ffmpeg -h encoder=<codec>``) et options d'encodage vidéo du codec (section AVCodecContext de
  ``ffmpeg -h full``), sauf celles qu'une option de la ligne de commande FFmpeg sans argument masque : FFmpeg lit
  d'abord ses propres options, si bien que ``-stats chemin.mkv`` ajouterait une sortie.
- Codecs à chaîne de paramètres (libx265, libsvtav1) : la chaîne entière est la valeur d'un seul argument, aucune
  option FFmpeg ne peut s'y glisser ; seul le contrôle de syntaxe existant s'applique.
- NVEncC : options de ``--help`` hors entrées, sorties, journaux, sources, fichiers annexes, filtres ``--vpp-*`` et
  chargeurs indirects (``--option-file``, ``--avsdll``).
"""

from __future__ import annotations

import functools
import re
import shlex
import shutil
from dataclasses import dataclass

from core.workflows.encode.catalog import NVENCC_VIDEO_CODECS
from core.workflows.encode.domain.codecs import _PARAM_STRING_CODECS, ffmpeg_option_name, is_option_token

_HELP_TIMEOUT_S = 20.0
_AVOPTION_RE = re.compile(r"^  -(\S+)\s+<[^>]*>\s+([A-Z.]{11})\s")
_FFTOOLS_OPTION_RE = re.compile(r"^-([\w-]+)(?:\[[^\]]*\])?(\s+<[^>]*>)?\s{2,}")
_NVENCC_NAME_RE = re.compile(r"(?:^|[\s,])(-{1,2}[A-Za-z?][\w-]*)")
_NVENCC_NEGATABLE_RE = re.compile(r"--\(no-\)([\w-]+)")

#: Options NVEncC jamais acceptées d'une requête de l'API (noms longs canoniques ; les alias courts y sont ramenés) :
#: entrées et lecteurs, sorties et formats, positionnement dans la source, fichiers annexes, chargeurs indirects.
NVENCC_API_FORBIDDEN_FLAGS = frozenset({
    "--input", "--output", "--output-format", "--raw", "--y4m", "--y4m-timestamp", "--avhw", "--avsw", "--avs",
    "--vpy", "--vpy-mt", "--avi", "--fps", "--seek", "--seekto", "--trim", "--option-file", "--avsdll", "--vsdll",
    "--keyfile", "--tcfile-in", "--timecode", "--dolby-vision-rpu", "--dhdr10-info", "--metadata",
})
#: Familles NVEncC refusées : entrées, journaux, filtres, flux audio / sous-titres / données / pièces jointes ajoutés
#: à la sortie, options du multiplexeur, chapitres, sous-titres codés.
NVENCC_API_FORBIDDEN_PREFIXES = ("--input", "--log", "--vpp-", "--audio", "--sub-", "--data-", "--attachment",
                                 "--mux", "--chapter", "--caption")
#: Sources annexes (``--audio-source``, ``--sub-source``…).
NVENCC_API_FORBIDDEN_SUFFIXES = ("-source",)
#: Options standard de la ligne de commande FFmpeg admises comme réglages d'encodage (une valeur chacune) : qualité
#: constante (``-q`` / ``-qscale``), format de pixels, images clés forcées.
FFMPEG_STANDARD_ENCODE_OPTIONS = frozenset({"q", "pix_fmt", "force_key_frames"})


@functools.lru_cache(maxsize=64)
def _help_text(binary: str, *args: str) -> str:
    """Sortie d'une commande d'aide (vide si l'outil est absent ou en échec) ; mémorisée par binaire."""
    from core.subprocess_utils import run_probe

    try:
        result = run_probe([binary, *args], timeout=_HELP_TIMEOUT_S)
    except Exception:  # outil absent, délai, sonde refusée : aucune option connue (paramètres libres refusés)
        return ""
    return f"{result.stdout or ''}\n{result.stderr or ''}"


def _encoder_private_options(ffmpeg: str, codec: str) -> frozenset[str] | None:
    """Options privées de l'encodeur ; ``None`` si ce FFmpeg ne le connaît pas."""
    text = _help_text(ffmpeg, "-hide_banner", "-h", f"encoder={codec}")
    if f"Encoder {codec} " not in text:
        return None
    return frozenset(match.group(1) for line in text.splitlines() if (match := _AVOPTION_RE.match(line)))


@functools.lru_cache(maxsize=8)
def _codec_context_and_masked(ffmpeg: str) -> tuple[frozenset[str], frozenset[str]]:
    """Options d'encodage vidéo du codec (AVCodecContext, drapeaux ``E`` et ``V``) et options de la ligne de commande
    FFmpeg affichées sans argument (elles masquent une option d'encodeur homonyme)."""
    text = _help_text(ffmpeg, "-hide_banner", "-h", "full")
    context: set[str] = set()
    masked: set[str] = set()
    in_context = False
    for line in text.splitlines():
        if line.endswith("AVOptions:") and not line.startswith(" "):
            in_context = line.startswith("AVCodecContext AVOptions")
            continue
        fftools = _FFTOOLS_OPTION_RE.match(line)
        if fftools is not None:
            if fftools.group(2) is None:
                masked.add(fftools.group(1))
            continue
        option = _AVOPTION_RE.match(line)
        if in_context and option is not None and option.group(2)[0] == "E" and option.group(2)[3] == "V":
            context.add(option.group(1))
    return frozenset(context), frozenset(masked)


def ffmpeg_encoder_option_names(ffmpeg: str, codec: str) -> frozenset[str] | None:
    """Options acceptées pour ``codec`` (chacune prend une valeur) ; ``None`` si aucun FFmpeg disponible ne connaît
    l'encodeur."""
    for binary in dict.fromkeys((ffmpeg, shutil.which("ffmpeg") or ffmpeg)):  # FFmpeg configuré, puis système
        private = _encoder_private_options(binary, codec)
        if private is not None:
            context, masked = _codec_context_and_masked(binary)
            return ((private | context) - masked) | FFMPEG_STANDARD_ENCODE_OPTIONS
    return None


@dataclass(frozen=True)
class NvenccOption:
    """Option NVEncC : nom canonique (long) et valeur obligatoire."""

    name: str
    takes_value: bool


def nvencc_options(nvencc: str) -> dict[str, NvenccOption]:
    """Options de ``NVEncC --help`` par nom ou alias (``-f`` → ``--output-format`` ; ``--(no-)aq`` : ``--aq`` et
    ``--no-aq``). Valeur obligatoire : argument hors crochets dans l'aide (``[<int>]`` facultatif : aucune)."""
    options: dict[str, NvenccOption] = {}
    for line in _help_text(nvencc, "--help").splitlines():
        if not line.lstrip().startswith("-"):
            continue
        spec = re.split(r"\s{2,}", line.strip(), maxsplit=1)[0]
        negatable = _NVENCC_NEGATABLE_RE.findall(spec)
        rest = _NVENCC_NEGATABLE_RE.sub("", spec)
        names = _NVENCC_NAME_RE.findall(rest)
        takes_value = "<" in re.sub(r"\[[^\]]*\]", "", rest)
        for base in negatable:
            for name in (f"--{base}", f"--no-{base}"):
                options[name] = NvenccOption(name, False)
        if names:
            canonical = next((name for name in names if name.startswith("--")), names[0])
            for name in names:
                options[name] = NvenccOption(canonical, takes_value)
    return options


def nvencc_option_names(nvencc: str) -> frozenset[str]:
    """Noms et alias listés par ``NVEncC --help``."""
    return frozenset(nvencc_options(nvencc))


def _nvencc_forbidden(name: str) -> bool:
    return (name in NVENCC_API_FORBIDDEN_FLAGS or name.startswith(NVENCC_API_FORBIDDEN_PREFIXES)
            or name.endswith(NVENCC_API_FORBIDDEN_SUFFIXES))


def api_extra_params_errors(codec: str, extra_params: str, *, ffmpeg: str, nvencc: str) -> list[str]:
    """Écarts des paramètres libres d'une requête de l'API à la liste blanche de l'encodeur (vide : acceptés)."""
    raw = (extra_params or "").strip()
    codec = str(codec or "").strip().lower()
    if not raw or codec == "copy" or codec in _PARAM_STRING_CODECS:
        return []
    try:
        tokens = shlex.split(raw)
    except ValueError as exc:
        return [f"paramètres libres illisibles ({exc})"]
    if codec in NVENCC_VIDEO_CODECS:
        return _nvencc_errors(codec, tokens, nvencc_options(nvencc))
    allowed = ffmpeg_encoder_option_names(ffmpeg, codec)
    if not allowed:
        return [f"options de l'encodeur {codec} illisibles : paramètres libres refusés"]
    errors: list[str] = []
    index = 0
    while index < len(tokens):  # options d'encodeur FFmpeg : chacune consomme sa valeur, même « -cgop »
        token = tokens[index]
        if not is_option_token(token):
            errors.append(f"« {token} » n'est précédé d'aucune option")
            index += 1
            continue
        if token.startswith("-/") or ffmpeg_option_name(token) not in allowed:
            errors.append(f"option « {token} » hors des options de l'encodeur {codec}")
        if index + 1 >= len(tokens):
            errors.append(f"option « {token} » sans valeur")
        index += 2
    return errors


def _nvencc_errors(codec: str, tokens: list[str], options: dict[str, NvenccOption]) -> list[str]:
    if not options:
        return [f"options de l'encodeur {codec} illisibles : paramètres libres refusés"]
    errors: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        name, inline_value = token.split("=", 1) if token.startswith("--") and "=" in token else (token, None)
        option = options.get(name) if is_option_token(token) else None
        if option is None:
            errors.append(f"« {token} » hors des options de l'encodeur {codec}" if is_option_token(token)
                          else f"« {token} » n'est précédé d'aucune option")
            index += 1
            continue
        if _nvencc_forbidden(option.name):
            errors.append(f"option « {token} » hors des options de l'encodeur {codec}")
        consumes = option.takes_value and inline_value is None
        if consumes and index + 1 >= len(tokens):
            errors.append(f"option « {token} » sans valeur")
        index += 2 if consumes else 1
    return errors


__all__ = ["FFMPEG_STANDARD_ENCODE_OPTIONS", "NVENCC_API_FORBIDDEN_FLAGS", "NvenccOption", "api_extra_params_errors",
           "ffmpeg_encoder_option_names", "nvencc_option_names", "nvencc_options"]
