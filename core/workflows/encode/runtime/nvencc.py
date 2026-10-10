"""
core/workflows/encode/runtime/nvencc.py — Intégration NVEncC (rigaya).

Public:
    NVENCC_VIDEO_CODECS     — frozenset des identifiants codec NVEncC
    NVENCC_CODEC_FLAG       — mapping codec → valeur du flag ``-c`` NVEncC
    NVENCC_OUTPUT_EXT       — mapping codec → extension du bitstream brut
    is_nvencc_codec(codec)  — True si ``codec`` est géré par NVEncC
    nvencc_binary_name()    — nom du binaire NVEncC selon plateforme
    detect_nvencc_available(nvencc_bin) — (available, supported_codecs)

    build_decode_pipe_cmd(...)    — phase 1 : ffmpeg → yuv4mpegpipe sur stdout
    build_nvencc_command(...)     — phase 2 : NVEncC lit le pipe Y4M → vidéo encodée
    build_remux_cmd(...)          — phase 3 : ffmpeg remux audio/subs/chapters
    build_nvencc_pipeline(...)    — agrégateur retournant les 3 commandes

Le pipeline est ``ffmpeg | NVEncC → ffmpeg`` : phase 1 et 2 communiquent via
``Popen.stdout = Popen.stdin`` à l'exécution (pattern ``merge_dovi.py``).
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
import shutil
from functools import lru_cache
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from core.bluray import append_ffmpeg_input_args
from core.workflows.encode.catalog import hdr_capabilities
from core.subprocess_utils import subprocess_text_kwargs
from core.workflows.encode.models import (
    VideoCropSettings,
    VideoEncodeSettings,
    VideoFilterSettings,
    VideoResizeSettings,
)
from core.workflows.encode.domain.codecs import (
    ExtraParamsReport,
    build_vf as _build_ffmpeg_vf,
    classify_user_args,
    option_values,
    output_hdr_transfer,
    rate_control_values,
    resolve_output_bit_depth,
    filtered_bit_depth,
    filtered_pix_fmt,
    transfer_kind,
    nlmeans_settings,
    supports_output_10bit,
    option_items,
    resolve_resize_dimensions,
)


NVENCC_VIDEO_CODECS: frozenset[str] = frozenset({
    "nvencc_hevc",
    "nvencc_h264",
    "nvencc_av1",
})

# Vues dérivées de la table de compatibilité HDR du catalogue.
NVENCC_DYNAMIC_HDR_CODECS: frozenset[str] = frozenset(
    codec for codec in NVENCC_VIDEO_CODECS if hdr_capabilities(codec).dynamic
)

NVENCC_MANUAL_STATIC_HDR_CODECS: frozenset[str] = frozenset(
    codec for codec in NVENCC_VIDEO_CODECS if hdr_capabilities(codec).manual_static
)

NVENCC_WORKFLOW_OWNED_FLAGS: frozenset[str] = frozenset({
    "--master-display",
    "--max-cll",
    "--dhdr10-info",
    "--dolby-vision-profile",
    "--dolby-vision-rpu",
    "--dolby-vision-rpu-prm",
    "--avhw",
    "--avsw",
    "--colormatrix",
    "--colorprim",
    "--transfer",
    "--chromaloc",
    "--vpp-colorspace",
    "--vpp-libplacebo-tonemapping",
    "--vpp-libplacebo-tonemapping-lut",
    "--crop",
    "--output-res",
    "--vpp-resize",
    "--vpp-yadif",
    "--vpp-nlmeans",
})

# Contrôle de débit : posé par le mode qualité de l'UI, une saisie le remplace
# (dernière option de débit appliquée par NVEncC).
NVENCC_RATE_CONTROL_OPTIONS: frozenset[str] = frozenset({
    "cqp", "vbr", "cbr", "qvbr", "vbrhq", "cbrhq", "vbr-quality",
})
# Entrée / sortie du pipeline : retirées si la commande les pose.
_NVENCC_PIPELINE_OPTIONS = frozenset({
    "codec", "input", "output", "input-format", "y4m", "fps", "avsync", "video-streamid",
})
# Réglages de l'onglet Video : une saisie les remplace (avertissement).
_NVENCC_UI_OPTIONS = frozenset({"preset", "output-depth", "profile", "tier"})

# Alias NVEncC courts → nom long, pour repérer une option déjà posée par le workflow.
_NVENCC_OPTION_ALIASES = {"u": "preset", "c": "codec", "i": "input", "o": "output"}


def nvencc_option_name(token: str) -> str:
    """Nom canonique d'une option NVEncC (``-u`` → ``preset``, ``--x=y`` → ``x``)."""
    name = token.lstrip("-").split("=", 1)[0]
    return _NVENCC_OPTION_ALIASES.get(name, name)


NVENCC_QP_TRIPLET_FLAGS: frozenset[str] = frozenset({
    "--cqp",
    "--qp-init",
    "--qp-min",
    "--qp-max",
})

# Valeur du flag NVEncC ``-c <codec>`` à passer pour chaque identifiant interne.
NVENCC_CODEC_FLAG: dict[str, str] = {
    "nvencc_hevc": "hevc",
    "nvencc_h264": "h264",
    "nvencc_av1": "av1",
}

# Extension du fichier intermédiaire produit par NVEncC.
# Note importante : on utilise des conteneurs (.mkv) plutôt que des bitstreams
# bruts (.hevc/.h264/.ivf). Le pipe ``ffmpeg yuv4mpegpipe → NVEncC`` ne propage
# pas les timestamps, et ffmpeg refuse de muxer un bitstream sans timestamps
# (erreur "Can't write packet with unknown timestamp"). Faire écrire NVEncC dans
# un MKV résout ce problème : NVEncC utilise libavformat en interne et
# reconstruit les timestamps depuis l'input ``--y4m``.
NVENCC_OUTPUT_EXT: dict[str, str] = {
    "nvencc_hevc": ".mkv",
    "nvencc_h264": ".mkv",
    "nvencc_av1": ".mkv",
}


def is_nvencc_codec(codec: str | None) -> bool:
    """Retourne True si ``codec`` est l'un des identifiants NVEncC."""
    if not codec:
        return False
    return str(codec).strip().lower() in NVENCC_VIDEO_CODECS


def nvencc_supports_dynamic_hdr(codec: str | None) -> bool:
    return is_nvencc_codec(codec) and hdr_capabilities(codec).dynamic


def nvencc_supports_manual_static_hdr(codec: str | None) -> bool:
    return is_nvencc_codec(codec) and hdr_capabilities(codec).manual_static


def nvencc_binary_name() -> str:
    """Nom du binaire NVEncC attendu sur le PATH selon la plateforme.

    - Windows : ``NVEncC64.exe`` (archive .7z rigaya).
    - Linux   : ``nvencc`` (lowercase — c'est le nom posé par les paquets
      .deb/.rpm rigaya, et le binaire produit par ``make``).
    """
    if sys.platform == "win32":
        return "NVEncC64.exe"
    return "nvencc"


# ---------------------------------------------------------------------------
# Détection à l'exécution
# ---------------------------------------------------------------------------

# Codecs d'encodage NVENC réellement exposés par le GPU. Seules deux zones de
# sortie sont fiables : la liste suivant « Avaliable Codec(s) » de ``--check-hw``
# (faute d'orthographe d'origine chez rigaya) et les lignes « Codec: … » de
# ``--check-features``. La bannière de version (« reader: … [H.264/AVC, …, AV1] »)
# liste les *décodeurs* d'entrée : la parcourir donnerait de faux positifs (AV1).
_FEATURE_TOKEN_BY_CODEC: dict[str, tuple[str, ...]] = {
    "nvencc_h264": ("H.264/AVC", "H.264", "AVC"),
    "nvencc_hevc": ("H.265/HEVC", "H.265", "HEVC"),
    "nvencc_av1": ("AV1",),
}
_CHECK_HW_HEADER_RE = re.compile(r"^\s*(?:avaliable|available)\s+codec\(s\)\s*:?\s*$", re.IGNORECASE)
_FEATURES_CODEC_RE = re.compile(r"^\s*Codec\s*:\s*(.+?)\s*$", re.IGNORECASE)


def _run_nvencc_probe(nvencc_bin: str, option: str) -> str | None:
    """Exécute une sonde NVEncC ; None si l'outil ou le GPU/pilote NVIDIA est inutilisable."""
    try:
        proc = subprocess.run(
            [nvencc_bin, option],
            capture_output=True,
            check=False,
            timeout=15,
            **subprocess_text_kwargs(),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    # Sans GPU NVIDIA / pilote compatible, NVEncC échoue avec un code non nul.
    if proc.returncode != 0:
        return None
    return (proc.stdout or "") + "\n" + (proc.stderr or "")


def _codec_from_label(label: str) -> str | None:
    for codec_id, tokens in _FEATURE_TOKEN_BY_CODEC.items():
        if any(re.search(rf"\b{re.escape(token)}\b", label, re.IGNORECASE) for token in tokens):
            return codec_id
    return None


def _parse_supported_codecs(output: str) -> set[str]:
    """Codecs d'encodage listés par ``--check-hw`` (prioritaire) ou ``--check-features``."""
    if not output:
        return set()
    lines = output.splitlines()
    supported: set[str] = set()
    for i, line in enumerate(lines):
        if not _CHECK_HW_HEADER_RE.match(line):
            continue
        # Une ligne par codec jusqu'à la première ligne vide ou non reconnue.
        for entry in lines[i + 1:]:
            codec_id = _codec_from_label(entry) if entry.strip() else None
            if codec_id is None:
                break
            supported.add(codec_id)
        return supported
    for line in lines:
        match = _FEATURES_CODEC_RE.match(line)
        if match and (codec_id := _codec_from_label(match.group(1))) is not None:
            supported.add(codec_id)
    return supported


def detect_nvencc_available(nvencc_bin: str | None) -> tuple[bool, set[str]]:
    """
    Détecte si NVEncC est utilisable et liste les codecs supportés par le GPU.

    Sonde ``--check-hw`` (même option sous Windows et Linux ; NVEncC n'existe
    pas sous macOS) : code 0 uniquement si un GPU NVIDIA et un pilote
    compatibles répondent, suivi de la liste des encodeurs disponibles.
    ``--check-features`` sert de repli si ce format venait à changer.

    Indépendant de NVENC ffmpeg : NVEncC embarque sa propre négociation d'API
    et peut fonctionner quand le ffmpeg fourni exige un pilote plus récent.

    Returns:
        (available, codecs) — codecs ⊂ ``NVENCC_VIDEO_CODECS``.
    """
    if not nvencc_bin:
        return False, set()
    supported: set[str] = set()
    for option in ("--check-hw", "--check-features"):
        output = _run_nvencc_probe(nvencc_bin, option)
        if output is None:
            # Échec d'exécution (binaire absent, pas de GPU) : inutile d'insister.
            return False, set()
        supported = _parse_supported_codecs(output) & NVENCC_VIDEO_CODECS
        if supported:
            break
    return bool(supported), supported


def _parse_10bit_codecs(output: str) -> frozenset[str]:
    """Lit les profondeurs dans NVEnc, sans confondre sections encodeur et NVDec."""
    codecs: set[str] = set()
    current: str | None = None
    for line in output.splitlines():
        if re.search(r"\bNVDec\b", line, re.IGNORECASE):
            break
        match = _FEATURES_CODEC_RE.match(line)
        if match:
            current = _codec_from_label(match.group(1))
        elif current and re.match(r"\s*10\s*bit\s+depth\s+(?:[:=]\s*)?yes\b", line, re.IGNORECASE):
            codecs.add(current)
    return frozenset(codecs)


@lru_cache(maxsize=16)
def _cached_10bit_codecs(binary: str, size: int, mtime_ns: int) -> frozenset[str]:
    return _parse_10bit_codecs(_run_nvencc_probe(binary, "--check-features") or "")


def detect_nvencc_10bit_codecs(nvencc_bin: str | None) -> frozenset[str]:
    """Sonde conservatrice, cache invalidé lors d'un changement de binaire."""
    if not nvencc_bin:
        return frozenset()
    binary = shutil.which(str(nvencc_bin)) or str(nvencc_bin)
    try:
        path = Path(binary).resolve()
        stat = path.stat()
    except OSError:
        return frozenset()
    return _cached_10bit_codecs(str(path), stat.st_size, stat.st_mtime_ns)


# ---------------------------------------------------------------------------
# Construction du pipeline ffmpeg → NVEncC → ffmpeg
# ---------------------------------------------------------------------------

def build_decode_pipe_cmd(
    ffmpeg_bin: str,
    source: Path | str,
    *,
    stream_index: int = 0,
    extra_input_args: list[str] | None = None,
    vf: str | None = None,
    frame_exact: bool = False,
    nut: bool = False,
) -> list[str]:
    """Phase 1 : images décodées vers stdout (NUT horodaté ou yuv4mpegpipe).

    Le ``-vf`` optionnel sert uniquement aux préfiltrages portables que
    NVEncC ne couvre pas nativement dans l'application.
    ``frame_exact`` : métadonnées indexées par trame (RPU) fournies à NVEncC,
    aucune trame dupliquée ni supprimée (``-fps_mode passthrough``).
    """
    cmd: list[str] = [str(ffmpeg_bin), "-hide_banner", "-loglevel", "error", "-y"]
    if extra_input_args:
        cmd.extend(extra_input_args)
    append_ffmpeg_input_args(cmd, source)
    cmd.extend([
        "-map", f"0:{int(stream_index)}",
    ])
    if vf:
        cmd.extend(["-vf", str(vf)])
    if frame_exact or nut:
        cmd.extend(["-fps_mode", "passthrough"])
    if nut:
        # Transport sans compression : aucun encodage intermédiaire avec pertes.
        cmd.extend(["-c:v", "rawvideo", "-f", "nut", "-"])
    else:
        cmd.extend(["-f", "yuv4mpegpipe", "-strict", "-1", "-"])
    return cmd


def nvencc_requires_ffmpeg_filter_pipe(video: VideoEncodeSettings) -> bool:
    """True when NVEncC must read a y4m pipe (FFmpeg prefilters or RIFE interpolation)."""
    return nvencc_requires_ffmpeg_prefilter(video) or video.interpolates()


def nvencc_requires_ffmpeg_prefilter(video: VideoEncodeSettings) -> bool:
    """True when portable FFmpeg prefiltering is required before NVEncC."""
    filters = video.filters
    crop = video.crop
    resize = video.resize
    return bool(
        (filters.deblock_enabled or filters.chroma_smooth_enabled)
        or (crop.is_active() and crop.unit == "percent")
        or (resize.is_active() and str(resize.mode or "").strip().lower() == "percent")
    )


def nvencc_ffmpeg_filter_vf(video: VideoEncodeSettings) -> str:
    return _build_ffmpeg_vf(video)


def nvencc_pipe_encode_video(video: VideoEncodeSettings) -> VideoEncodeSettings:
    """Return settings already filtered by FFmpeg, keeping encode/HDR knobs.

    Tone mapping fait par FFmpeg : les trames sont SDR, aucune signalisation
    HDR10 statique (VUI PQ, master display, MaxCLL) ne doit suivre.
    """
    sdr = bool(video.tonemap_to_sdr)
    return replace(
        video,
        bit_depth=str(resolve_output_bit_depth(video)),
        source_bit_depth=filtered_bit_depth(video),
        source_pix_fmt=filtered_pix_fmt(video),
        resize=VideoResizeSettings(),
        crop=VideoCropSettings(),
        filters=VideoFilterSettings(),
        tonemap_to_sdr=False,
        # La source de l'encodeur est désormais la sortie du filtre FFmpeg.
        source_color_transfer="bt709" if sdr else "smpte2084" if video.p5_to_hdr10 else video.source_color_transfer,
        p5_to_hdr10=False,
        inject_hdr_meta=False if sdr else video.inject_hdr_meta,
        master_display="" if sdr else video.master_display,
        max_cll="" if sdr else video.max_cll,
    )


def _rate_control_args(video: VideoEncodeSettings) -> list[str]:
    """Mode de débit NVEncC (catalog.VIDEO_RATE_CONTROLS).

    Taille cible : ``bitrate_kbps`` calculé en amont, VBR plafonné.
    """
    spec, quality = rate_control_values(video)
    rc_id = spec.rc_id if spec is not None else "qvbr"
    bitrate = int(video.bitrate_kbps)
    if rc_id == "cqp":
        high = spec.quality_range[1] if spec is not None else 51
        return ["--cqp", f"{quality}:{min(high, quality + 2)}:{min(high, quality + 4)}"]
    if rc_id == "qvbr":
        return ["--qvbr", str(quality)]
    if rc_id == "vbr_quality":
        return ["--vbr", str(bitrate), "--vbr-quality", str(quality)]
    if rc_id == "cbr":
        return ["--cbr", str(bitrate)]
    if rc_id == "size":
        return ["--vbr", str(bitrate), "--max-bitrate", str(int(bitrate * 1.5))]
    return ["--vbr", str(bitrate)]


def _output_depth_args(video: VideoEncodeSettings) -> list[str]:
    """NVEncC ne conserve pas la profondeur d'entrée : cible toujours explicite."""
    return ["--output-depth", str(resolve_output_bit_depth(video))]


def _hdr_static_args(video: VideoEncodeSettings) -> list[str]:
    """Métadonnées HDR statiques (master display + MaxCLL/MaxFALL)."""
    args: list[str] = []
    if (
        not getattr(video, "inject_hdr_meta", False)
        or getattr(video, "tonemap_to_sdr", False)
        or not nvencc_supports_manual_static_hdr(video.codec)
    ):
        return args
    md = (video.master_display or "").strip()
    if md:
        args.extend(["--master-display", md])
    cll = (video.max_cll or "").strip()
    if cll:
        args.extend(["--max-cll", cll])
    return args


def map_nvencc_dovi_profile(profile: str | None) -> str | None:
    """Mappe la sémantique UI legacy vers l'option NVEncC attendue.

    NVEncC attend les profils Dolby Vision sous leur forme décimale
    (ex. ``8.1``), alors que notre UI et certains chemins legacy manipulent
    aussi des alias compacts ou symboliques (ex. ``2`` pour "normaliser en
    P8.1"). On normalise donc ici vers la représentation textuelle acceptée
    par NVEncC.
    """
    value = str(profile or "").strip().lower()
    if value in {"", "0", "copy"}:
        return "copy"

    aliases = {
        "2": "8.1",
        "8": "8.1",
        "81": "8.1",
        "82": "8.2",
        "84": "8.4",
        "50": "5.0",
        "100": "10.0",
        "101": "10.1",
        "102": "10.2",
        "104": "10.4",
    }
    if value in aliases:
        return aliases[value]

    if value in {"5.0", "8.1", "8.2", "8.4", "10.0", "10.1", "10.2", "10.4"}:
        return value
    return value


def _dovi_profile_for_codec(codec: str, profile: str | None) -> str | None:
    """Dolby Vision en AV1 = profil 10.x : un profil 8.x y produit un RPU invalide."""
    if codec == "nvencc_av1" and profile and profile.startswith("8."):
        return "10." + profile.split(".", 1)[1]
    return profile


# Source P5 convertie par NVEncC (libplacebo, RPU de la source) : HDR10 sans
# compression des hautes lumières (``src_max``/``dst_max`` à 10000, sinon
# écrêtage à 1000 nits). NVEncC 9.36 sort alors en plage pleine dès que le RPU
# est P5, quelle que soit la plage déclarée : ``--vpp-tweak`` (exécuté après
# libplacebo) ramène exactement en plage limitée (Y 876/1023, chroma 896/1023).
NVENCC_P5_TONEMAP = (
    "src_csp=dovi,dst_csp=hdr10,tonemapping_function=clip,dynamic_peak_detection=false,"
    "gamut_mapping=clip,src_max=10000,dst_max=10000"
)
NVENCC_P5_RANGE_TWEAK = "contrast=0.856305,brightness=-0.009286,saturation=0.875855"


def nvencc_p5_native_args() -> list[str]:
    """Conversion P5 → HDR10 dans NVEncC (entrée lue directement, conteneur requis)."""
    return [
        "--vpp-libplacebo-tonemapping", NVENCC_P5_TONEMAP,
        "--vpp-tweak", NVENCC_P5_RANGE_TWEAK,
        "--colorrange", "limited",
    ]


def _auto_source_hdr_args(video: VideoEncodeSettings, *, direct_input: bool) -> list[str]:
    """Signalisation couleur HDR (VUI) et recopie des métadonnées statiques source.

    Sans ``--transfer``/``--colorprim``, NVEncC émet un flux PQ non signalé,
    lu comme SDR : la VUI doit suivre dès qu'un HDR statique ou dynamique est
    conservé. Le y4m ne transporte pas la couleur : HDR10 statique = BT.2020/PQ.
    """
    # NVEncC H.264 refuse toute signalisation HDR (--master-display/--max-cll).
    # Sortie HDR (source PQ/HLG sans tone-mapping) : la VUI suit, même sans
    # métadonnées statiques (case HDR10 décochée).
    transfer = output_hdr_transfer(video)
    if not transfer:
        return []
    static = bool(getattr(video, "inject_hdr_meta", False))
    if direct_input and getattr(video, "p5_to_hdr10", False):
        # VUI d'origine IPT-PQ-c2 plage pleine : décrire l'image convertie.
        return [
            "--colormatrix", "bt2020nc",
            "--colorprim", "bt2020",
            "--transfer", "smpte2084",
            "--chromaloc", "auto",
        ]
    if not direct_input:
        args = [
            "--colormatrix", "bt2020nc",
            "--colorprim", "bt2020",
            "--transfer", "arib-std-b67" if transfer == "hlg" else "smpte2084",
        ]
        if getattr(video, "copy_dv", False):
            args.extend(["--chromaloc", "2"])
        return args
    args = [
        "--colormatrix", "auto",
        "--colorprim", "auto",
        "--transfer", "auto",
        "--chromaloc", "auto",
    ]
    # Case HDR10 statique décochée : ni valeurs ni recopie source.
    if static and not video.master_display:
        args.extend(["--master-display", "copy"])
    if static and not video.max_cll:
        args.extend(["--max-cll", "copy"])
    return args


_LIBPLACEBO_TONEMAP_FUNCTIONS = frozenset({"hable", "mobius", "reinhard", "gamma", "linear", "clip", "bt2390"})


def map_nvencc_tonemap_args(video: VideoEncodeSettings) -> list[str]:
    """Tone-mapping HDR → SDR natif par libplacebo (PQ ou HLG source).

    ``--vpp-colorspace … hdr2sdr`` exige ``libnvrtc`` (absent des installations
    Linux usuelles : échec immédiat) ; libplacebo n'en a pas besoin et propose
    toutes les fonctions de l'interface. NVEncC sans libplacebo : pipe FFmpeg
    (routage ``native_tonemap``).
    """
    if not getattr(video, "tonemap_to_sdr", False):
        return []
    algo = str(getattr(video, "tonemap_algorithm", "") or "hable").strip().lower()
    if algo not in _LIBPLACEBO_TONEMAP_FUNCTIONS:
        algo = "hable"
    source = "hlg" if transfer_kind(getattr(video, "source_color_transfer", "")) == "hlg" else "hdr10"
    return [
        "--vpp-libplacebo-tonemapping",
        f"src_csp={source},dst_csp=sdr,tonemapping_function={algo}",
    ]


def _nvencc_resize_args(
    video: VideoEncodeSettings,
    source_dimensions: tuple[int, int] | None = None,
) -> list[str]:
    resize = video.resize
    if not resize.is_active():
        return []
    mode = str(resize.mode or "preset").strip().lower()
    if mode == "percent":
        # Percent resize needs source dimensions, so keep it in FFmpeg when
        # callers require exact scaling. Direct NVEncC keeps native settings.
        return []
    # Même résultat que le filtre scale FFmpeg (ratio conservé, pas
    # d'agrandissement) : calculé sur l'image recadrée.
    src_w, src_h = source_dimensions or (0, 0)
    crop_args = _nvencc_crop_args(video)
    if crop_args and src_w > 0 and src_h > 0:
        crop = video.crop
        src_w -= max(0, int(crop.left)) + max(0, int(crop.right))
        src_h -= max(0, int(crop.top)) + max(0, int(crop.bottom))
    width, height = resolve_resize_dimensions(src_w, src_h, resize)
    if width <= 0 or height <= 0:
        # Source inconnue : dimensions demandées telles quelles.
        if mode == "size":
            width = max(2, int(resize.width or 2))
            height = max(2, int(resize.height or 2))
        else:
            presets = {
                "720p": (1280, 720),
                "1080p": (1920, 1080),
                "1440p": (2560, 1440),
                "2160p": (3840, 2160),
            }
            width, height = presets.get(str(resize.preset or "720p"), presets["720p"])
    args = ["--output-res", f"{width}x{height}"]
    algo = str(resize.algorithm or "lanczos").strip().lower()
    nvencc_algo = {
        "lanczos": "lanczos",
        "bicubic": "bicubic",
        "bilinear": "bilinear",
        "spline": "spline36",
    }.get(algo, "lanczos")
    args.extend(["--vpp-resize", f"algo={nvencc_algo}"])
    return args


def _nvencc_crop_args(video: VideoEncodeSettings) -> list[str]:
    crop = video.crop
    if not crop.is_active() or crop.auto or crop.unit == "percent":
        return []
    left = max(0, int(crop.left))
    top = max(0, int(crop.top))
    right = max(0, int(crop.right))
    bottom = max(0, int(crop.bottom))
    return ["--crop", f"{left},{top},{right},{bottom}"]


# (sigma, h) de --vpp-nlmeans au plus près de nlmeans FFmpeg (s = 1 / 2 / 3 / 5),
# patch / search = p / r FFmpeg. Banc du 2026-10-06 : 720p, bruit temporel
# (PSNR 33 dB), recherche sur grille ; sorties NVEncC et FFmpeg à ≈ 43 dB.
_NVENCC_NLMEANS: dict[str, tuple[float, float]] = {
    "ultralight": (0.0, 0.04),
    "light": (0.0, 0.08),
    "medium": (0.0, 0.12),
    "strong": (0.01, 0.18),
}


def nvencc_yadif_mode(filters: VideoFilterSettings) -> str:
    """Mode ``--vpp-yadif`` équivalent au yadif FFmpeg (mode × parité).

    Frame (``send_frame``) : ``auto`` / ``tff`` / ``bff`` ; Bob (``send_field``,
    cadence doublée) : ``bob`` / ``bob_tff`` / ``bob_bff``.
    """
    mode = str(filters.yadif_mode or "send_frame").strip().lower()
    parity = str(filters.yadif_parity or "auto").strip().lower()
    parity = parity if parity in {"tff", "bff"} else ""
    if mode.startswith("send_field") or mode == "bob":
        return f"bob_{parity}" if parity else "bob"
    return parity or "auto"


def _nvencc_filter_args(video: VideoEncodeSettings) -> list[str]:
    filters = video.filters
    args: list[str] = []
    if filters.yadif_enabled:
        args.extend(["--vpp-yadif", f"mode={nvencc_yadif_mode(filters)}"])
    if filters.nlmeans_enabled:
        preset, scale, patch, search = nlmeans_settings(filters)
        sigma, h = _NVENCC_NLMEANS[preset]
        # Profil grain / animation : même réduction que FFmpeg, même plancher (ultralight).
        h = max(_NVENCC_NLMEANS["ultralight"][1], round(h * scale, 4))
        args.extend(["--vpp-nlmeans", f"sigma={sigma},h={h},patch={patch},search={search}"])
    return args


def map_nvencc_video_transform_args(
    video: VideoEncodeSettings,
    *,
    source_dimensions: tuple[int, int] | None = None,
) -> list[str]:
    args: list[str] = []
    args.extend(_nvencc_crop_args(video))
    args.extend(_nvencc_resize_args(video, source_dimensions))
    args.extend(_nvencc_filter_args(video))
    return args


def normalize_nvencc_qp_triplet(value: str | None) -> str | None:
    """Normalise ``X`` en ``X:X:X`` pour les champs QP I:P:B."""
    text = str(value or "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d+", text):
        return f"{text}:{text}:{text}"
    return text


def normalize_nvencc_parallel(value: str | None) -> str | None:
    """Canonise l'ancien alias ``all`` vers ``auto``."""
    text = str(value or "").strip()
    if not text:
        return None
    if text.lower() == "all":
        return "auto"
    return text


def sanitize_nvencc_extra_params(extra_params: str) -> list[str]:
    """Retire les flags pilotés par le workflow et normalise les triplets QP."""
    return split_nvencc_extra_params(extra_params)[0]


def split_nvencc_extra_params(extra_params: str) -> tuple[list[str], list[str]]:
    """Comme ``sanitize_nvencc_extra_params`` ; retourne aussi les tokens retirés."""
    removed: list[str] = []
    raw = (extra_params or "").strip()
    if not raw:
        return [], removed
    try:
        tokens = shlex.split(raw)
    except ValueError:
        tokens = raw.split()

    sanitized: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if not token.startswith("--"):
            sanitized.append(token)
            i += 1
            continue

        if "=" in token:
            option, value = token.split("=", 1)
            if option in NVENCC_WORKFLOW_OWNED_FLAGS:
                removed.append(token)
                i += 1
                continue
            if option == "--parallel":
                normalized_parallel = normalize_nvencc_parallel(value)
                if normalized_parallel:
                    sanitized.extend([option, normalized_parallel])
                i += 1
                continue
            if option in NVENCC_QP_TRIPLET_FLAGS:
                normalized = normalize_nvencc_qp_triplet(value)
                if normalized:
                    sanitized.extend([option, normalized])
                i += 1
                continue
            sanitized.append(token)
            i += 1
            continue

        next_is_value = i + 1 < len(tokens) and not tokens[i + 1].startswith("--")
        if token in NVENCC_WORKFLOW_OWNED_FLAGS:
            removed.extend(tokens[i:i + 2] if next_is_value else [token])
            i += 2 if next_is_value else 1
            continue
        if token == "--parallel" and next_is_value:
            normalized_parallel = normalize_nvencc_parallel(tokens[i + 1])
            sanitized.append(token)
            if normalized_parallel:
                sanitized.append(normalized_parallel)
            i += 2
            continue
        if token in NVENCC_QP_TRIPLET_FLAGS and next_is_value:
            normalized = normalize_nvencc_qp_triplet(tokens[i + 1])
            sanitized.append(token)
            if normalized:
                sanitized.append(normalized)
            i += 2
            continue

        sanitized.append(token)
        i += 1
    return sanitized, removed


def strip_nvencc_parallel_args(args: list[str]) -> list[str]:
    """Retire ``--parallel`` de la liste d'arguments NVEncC.

    Le mode parallel encode de NVEncC segmente le flux en plusieurs workers.
    Avec la recopie native DoVi/HDR10+, ce chemin peut échouer très tôt côté
    NVENC (``nvEncLockBitstream: invalid param`` / ``PECOLLECT``). On
    neutralise donc explicitement ``--parallel`` quand le workflow demande une
    copie HDR dynamique native.
    """
    stripped: list[str] = []
    i = 0
    while i < len(args):
        token = args[i]
        if token == "--parallel":
            next_is_value = i + 1 < len(args) and not args[i + 1].startswith("--")
            i += 2 if next_is_value else 1
            continue
        if token.startswith("--parallel="):
            i += 1
            continue
        stripped.append(token)
        i += 1
    return stripped


def strip_nvencc_latency_args(args: list[str]) -> list[str]:
    """Retire les réglages NVEncC orientés faible latence.

    Le tune `lowlatency`/`ultralowlatency` et le flag `--lowlatency`
    privilégient le pipeline live/streaming. Sur le chemin de copie HDR
    dynamique natif (DoVi/HDR10+), ces modes peuvent casser très tôt côté
    encodeur. On les retire donc quand le workflow demande cette recopie
    native, tout en laissant les autres tunes (`hq`, `uhq`, `lossless`) intacts.
    """
    stripped: list[str] = []
    i = 0
    while i < len(args):
        token = args[i]
        if token == "--lowlatency":
            i += 1
            continue
        if token.startswith("--tune="):
            tune_value = token.split("=", 1)[1].strip().lower()
            if tune_value in {"lowlatency", "ultralowlatency"}:
                i += 1
                continue
            stripped.append(token)
            i += 1
            continue
        if token == "--tune":
            next_is_value = i + 1 < len(args) and not args[i + 1].startswith("--")
            if next_is_value:
                tune_value = str(args[i + 1]).strip().lower()
                if tune_value in {"lowlatency", "ultralowlatency"}:
                    i += 2
                    continue
            stripped.append(token)
            if next_is_value:
                stripped.append(args[i + 1])
                i += 2
            else:
                i += 1
            continue
        stripped.append(token)
        i += 1
    return stripped


def _compute_dovi_gop_len(input_fps: str | float | None = None) -> int:
    """Calcule la longueur maximale de GOP pour borner le cycle IDR à 2 secondes max (Dolby Vision).

    Évite la saturation du buffer matériel DPB/RPU des téléviseurs (LG OLED, Sony, etc.)
    tout en adaptant le nombre d'images à la cadence réelle :
    - 23.976 / 24 fps : 48
    - 25 fps : 50
    - 29.97 / 30 fps : 60
    - 50 fps : 100
    - 59.94 / 60 fps : 120
    Fallback par défaut (cinéma 24fps) : 48
    """
    if input_fps is not None:
        try:
            if isinstance(input_fps, str) and "/" in input_fps:
                num, den = input_fps.split("/", 1)
                fps_val = float(num) / float(den)
            else:
                fps_val = float(input_fps)
            if fps_val > 0:
                return max(24, int(round(fps_val * 2.0)))
        except (ValueError, ZeroDivisionError):
            pass
    return 48


def _hdr_dynamic_args(
    video: VideoEncodeSettings,
    *,
    hdr10plus_json: Path | str | None = None,
    dovi_rpu: Path | str | None = None,
    dovi_rpu_prm: str | None = None,
    input_fps: str | float | None = None,
) -> list[str]:
    """Flux HDR10+ et DoVi : passthrough (``copy``) ou fichiers extraits amont."""
    args: list[str] = []
    if hdr10plus_json is not None:
        args.extend(["--dhdr10-info", str(hdr10plus_json)])
    elif getattr(video, "copy_hdr10plus", False):
        args.extend(["--dhdr10-info", "copy"])
    has_dovi = False
    if dovi_rpu is not None:
        has_dovi = True
        args.extend(["--dolby-vision-rpu", str(dovi_rpu)])
        mapped_profile = _dovi_profile_for_codec(video.codec, map_nvencc_dovi_profile(video.dovi_profile))
        # Quand on injecte un RPU externe, le profil doit être explicite ou
        # absent ; `copy` n'a de sens qu'en passthrough direct depuis la source.
        if mapped_profile and mapped_profile != "copy":
            args.extend(["--dolby-vision-profile", mapped_profile])
    elif getattr(video, "copy_dv", False):
        has_dovi = True
        args.extend(["--dolby-vision-rpu", "copy"])
        mapped_profile = map_nvencc_dovi_profile(video.dovi_profile)
        if mapped_profile in {None, "copy"}:
            mapped_profile = "8.1"
        mapped_profile = _dovi_profile_for_codec(video.codec, mapped_profile)
        if mapped_profile:
            args.extend(["--dolby-vision-profile", mapped_profile])
    if dovi_rpu_prm and "--dolby-vision-rpu" in args:
        args.extend(["--dolby-vision-rpu-prm", str(dovi_rpu_prm)])

    if has_dovi and video.codec == "nvencc_hevc":
        # Conformité Dolby Vision Profile 8.1 pour lecture sur diffuseurs TV :
        # - Main10 et Tier High
        # - AUD et repeat-headers pour synchronisation continue du processeur DV
        # - gop-len borné à 2 secondes max (selon la cadence réelle) pour borner le buffer matériel DPB/RPU
        if "--profile" not in args:
            args.extend(["--profile", "main10"])
        if "--tier" not in args:
            args.extend(["--tier", "high"])
        if "--repeat-headers" not in args:
            args.append("--repeat-headers")
        if "--aud" not in args:
            args.append("--aud")
        extra_raw = str(getattr(video, "extra_params", "") or "")
        if "--gop-len" not in extra_raw and "-g " not in extra_raw and not extra_raw.endswith("-g") and "--gop-len" not in args:
            gop_len = _compute_dovi_gop_len(input_fps)
            args.extend(["--gop-len", str(gop_len)])

    return args


def build_nvencc_command(
    nvencc_bin: str,
    video: VideoEncodeSettings,
    output_path: Path | str,
    *,
    input_path: Path | str | None = None,
    stream_index: int | None = None,
    input_reader: str | None = None,
    input_fps: str | None = None,
    source_fps: str | float | None = None,
    input_avsync: str | None = None,
    hdr10plus_json: Path | str | None = None,
    dovi_rpu: Path | str | None = None,
    dovi_rpu_prm: str | None = None,
    source_dimensions: tuple[int, int] | None = None,
    on_extra_report: Callable[[ExtraParamsReport], None] | None = None,
) -> list[str]:
    """Phase 2 : commande NVEncC complète (stdin = yuv4mpegpipe phase 1).

    Args:
        nvencc_bin     : chemin vers le binaire NVEncC.
        video          : settings de la piste vidéo.
        output_path    : fichier intermédiaire (.hevc/.h264/.ivf).
        hdr10plus_json : JSON HDR10+ extrait amont (sinon ``copy_hdr10plus`` → 'copy').
        dovi_rpu       : RPU DoVi (.bin) extrait amont (sinon ``copy_dv`` → 'copy').
        source_dimensions : image source (L×H), pour un ``--output-res`` au ratio conservé.
        on_extra_report : reçoit le tri des paramètres avancés (retirés,
                          remplaçant un réglage de l'onglet Video).

    Le caller appliquera les ``extra_params`` du dialog (``shlex.split``)
    en concaténation finale.
    """
    codec_flag = NVENCC_CODEC_FLAG.get(video.codec)
    if codec_flag is None:
        raise ValueError(f"Codec NVEncC inconnu : {video.codec}")

    cmd: list[str] = [str(nvencc_bin), "-c", codec_flag]
    if input_path is None:
        if video.fel_context is not None and not video.interpolates():
            # Le NUT conserve les PTS du producteur FEL, même en cadence variable.
            cmd.extend(["--avsw", "--input-format", "nut", "--avsync", "vfr", "-i", "-"])
        elif sys.platform != "win32":
            # NVEncC 9.16/9.19 : hors Windows, la couche POSIX rigaya mappe
            # strtok_s sur strtok (non ré-entrant) dans le parseur Y4M natif,
            # alors que l'initialisation est parallèle.
            # Le lecteur libavformat évite les dimensions/fps intermittents
            # corrompus, en lisant le même en-tête Y4M (sans valeur inventée).
            cmd.extend(["--avsw", "--input-format", "yuv4mpegpipe", "-i", "-"])
        else:
            cmd.extend(["--y4m", "-i", "-"])
    else:
        if input_reader in {"avhw", "avsw"}:
            cmd.append(f"--{input_reader}")
        cmd.extend(["-i", str(input_path)])
        if input_avsync:
            cmd.extend(["--avsync", str(input_avsync)])
        if input_fps:
            # Sur certains elementary streams (ex. HEVC Annex B), le reader
            # avformat ne conserve pas toujours la cadence correcte et peut
            # retomber sur un hint implicite. On transmet donc explicitement
            # le fps source quand l'appelant en dispose.
            cmd.extend(["--fps", str(input_fps)])
        if stream_index is not None:
            # ``stream_index`` correspond à l'index ffprobe/libavformat du flux
            # source ; NVEncC attend cela via ``--video-streamid`` et non
            # ``--video-track`` (qui utilise sa propre notion de track id).
            cmd.extend(["--video-streamid", str(int(stream_index))])
    cmd.extend(_rate_control_args(video))
    cmd.extend(_output_depth_args(video))

    # Preset NVEncC : si l'utilisateur a sélectionné un preset valide pour
    # NVEncC (default/performance/quality/P1..P7), on l'applique. On ne
    # propage PAS les presets x265/NVENC ffmpeg qui ne s'appliquent pas.
    preset = (video.preset or "").strip()
    if preset and (preset.lower() in {"default", "performance", "quality"}
                   or (preset.upper() in {"P1", "P2", "P3", "P4", "P5", "P6", "P7"})):
        cmd.extend(["-u", preset])

    p5_native = input_path is not None and bool(getattr(video, "p5_to_hdr10", False))
    cmd.extend(_auto_source_hdr_args(video, direct_input=input_path is not None))
    cmd.extend(_hdr_static_args(video))
    if p5_native:
        cmd.extend(nvencc_p5_native_args())
    cmd.extend(
        _hdr_dynamic_args(
            video,
            hdr10plus_json=hdr10plus_json,
            dovi_rpu=dovi_rpu,
            dovi_rpu_prm=dovi_rpu_prm,
            input_fps=source_fps or input_fps,
        )
    )
    cmd.extend(map_nvencc_video_transform_args(video, source_dimensions=source_dimensions))
    cmd.extend(map_nvencc_tonemap_args(video))

    # extra_params experts, concaténés en fin de commande : la saisie remplace
    # les réglages de l'onglet Video ; seules les options incompatibles avec
    # le workflow sont retirées.
    extra_args, removed = split_nvencc_extra_params(video.extra_params)
    compatible: list[str] = []
    for item in option_items(extra_args):
        name = nvencc_option_name(item[0])
        value = item[1] if len(item) == 2 else item[0].split("=", 1)[1] if "=" in item[0] else ""
        if name == "output-depth" and value == "10" and not supports_output_10bit(video):
            removed.extend(item)
        else:
            compatible.extend(item)
    extra_args = compatible
    if getattr(video, "copy_dv", False) or getattr(video, "copy_hdr10plus", False):
        kept = strip_nvencc_latency_args(strip_nvencc_parallel_args(extra_args))
        removed.extend(_tokens_not_kept(extra_args, kept))
        extra_args = kept
    report = classify_user_args(
        extra_args,
        cmd,
        option_name=nvencc_option_name,
        removed_if_set=_NVENCC_PIPELINE_OPTIONS | _nvencc_locked_options(video, p5_native=p5_native),
        override_names=NVENCC_RATE_CONTROL_OPTIONS | _NVENCC_UI_OPTIONS,
        exclusive_groups=(NVENCC_RATE_CONTROL_OPTIONS,),
    )
    if on_extra_report is not None:
        on_extra_report(ExtraParamsReport(report.kept, (*removed, *report.removed), report.overriding))
    cmd.extend(report.kept)

    cmd.extend(["-o", str(output_path)])
    return cmd


def _tokens_not_kept(before: list[str], after: list[str]) -> list[str]:
    """Tokens de ``before`` absents de ``after`` (ordre conservé, ``after`` sous-suite)."""
    removed: list[str] = []
    j = 0
    for token in before:
        if j < len(after) and after[j] == token:
            j += 1
        else:
            removed.append(token)
    return removed


def _nvencc_locked_options(video: VideoEncodeSettings, *, p5_native: bool = False) -> frozenset[str]:
    """Options du workflow qu'une saisie rendrait incompatibles avec le HDR conservé."""
    locked: set[str] = set()
    if p5_native:
        # Conversion P5 : plage limitée obtenue par --vpp-tweak, signalée par --colorrange.
        locked |= {"vpp-tweak", "colorrange"}
    if getattr(video, "copy_dv", False) and video.codec == "nvencc_hevc":
        # Conformité Dolby Vision P8.1 : Main10, tier High.
        locked |= {"profile", "tier"}
    if output_hdr_transfer(video):
        locked.add("output-depth")
    return frozenset(locked)


def nvencc_extra_params_report(video: VideoEncodeSettings) -> ExtraParamsReport:
    """Tri des paramètres avancés NVEncC (retirés, remplaçant l'onglet Video)."""
    if not is_nvencc_codec(video.codec) or not (video.extra_params or "").strip():
        return ExtraParamsReport()
    reports: list[ExtraParamsReport] = []
    build_nvencc_command("nvencc", video, "out.mkv", on_extra_report=reports.append)
    return reports[0] if reports else ExtraParamsReport()


def nvencc_workflow_option_values(video: VideoEncodeSettings) -> dict[str, str]:
    """Options posées par le workflow pour ces réglages (pré-remplissage de l'éditeur)."""
    if not is_nvencc_codec(video.codec):
        return {}
    cmd = build_nvencc_command("nvencc", replace(video, extra_params=""), "out.mkv")
    return option_values(cmd[1:], nvencc_option_name)


def build_remux_cmd(
    ffmpeg_bin: str,
    encoded_video: Path | str,
    source: Path | str,
    output: Path | str,
    *,
    map_audio: bool = True,
    map_subtitles: bool = True,
    map_chapters: bool = True,
    extra_args: list[str] | None = None,
) -> list[str]:
    """Phase 3 : remux ffmpeg.

    - Input 0 : bitstream encodé (vidéo seule)
    - Input 1 : source originale (audio/subs/chapitres)

    Tous les flux sont copiés sans réencodage. Si le caller veut
    réencoder l'audio, il fournit ``extra_args`` avec les options ``-c:a …``.
    """
    cmd: list[str] = [
        str(ffmpeg_bin), "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(encoded_video),
    ]
    append_ffmpeg_input_args(cmd, source)
    cmd.extend(["-map", "0:v:0"])
    if map_audio:
        cmd.extend(["-map", "1:a?"])
    if map_subtitles:
        cmd.extend(["-map", "1:s?"])
    if map_chapters:
        cmd.extend(["-map_chapters", "1"])
    cmd.extend(["-c", "copy"])
    if extra_args:
        cmd.extend(extra_args)
    cmd.append(str(output))
    return cmd


def build_nvencc_pipeline(
    *,
    ffmpeg_bin: str,
    nvencc_bin: str,
    video: VideoEncodeSettings,
    source: Path | str,
    output: Path | str,
    intermediate: Path | str,
    stream_index: int = 0,
    hdr10plus_json: Path | str | None = None,
    dovi_rpu: Path | str | None = None,
    audio_args: list[str] | None = None,
    map_audio: bool = True,
    map_subtitles: bool = True,
    map_chapters: bool = True,
) -> list[list[str]]:
    """Agrégateur : retourne les 3 commandes du pipeline NVEncC.

    Returns:
        [decode_cmd, encode_cmd, remux_cmd]

    À l'exécution, les phases 1 et 2 doivent être lancées en parallèle
    via ``Popen`` avec ``p1.stdout = p2.stdin`` (cf. ``merge_dovi.py``).
    La phase 3 démarre une fois ``intermediate`` complet.
    """
    needs_prefilter = nvencc_requires_ffmpeg_filter_pipe(video)
    decode = build_decode_pipe_cmd(
        ffmpeg_bin,
        source,
        stream_index=stream_index,
        vf=nvencc_ffmpeg_filter_vf(video) if needs_prefilter else None,
    )
    encode = build_nvencc_command(
        nvencc_bin,
        nvencc_pipe_encode_video(video) if needs_prefilter else video,
        intermediate,
        hdr10plus_json=hdr10plus_json,
        dovi_rpu=dovi_rpu,
    )
    remux = build_remux_cmd(
        ffmpeg_bin, intermediate, source, output,
        map_audio=map_audio,
        map_subtitles=map_subtitles,
        map_chapters=map_chapters,
        extra_args=audio_args,
    )
    return [decode, encode, remux]


def nvencc_intermediate_path(work_dir: Path, codec: str, base_name: str = "nvencc") -> Path:
    """Chemin du fichier intermédiaire (bitstream brut) à partir du codec."""
    ext = NVENCC_OUTPUT_EXT.get(codec, ".bin")
    return Path(work_dir) / f"{base_name}{ext}"


def is_expected_nvencc_pipe_producer_exit(returncode: int, stderr: str) -> bool:
    """True si FFmpeg s'est arrêté normalement avec le consommateur NVEncC.

    Selon le timing, la fermeture du pipe par NVEncC est rapportée soit comme
    SIGPIPE (``-13``), soit comme un code FFmpeg générique ``1`` accompagné du
    diagnostic ``Broken pipe``. Ce dernier n'est acceptable qu'après succès du
    consommateur ; les appelants doivent donc toujours vérifier NVEncC d'abord.
    """
    if returncode in (0, -13):
        return True
    # FFmpeg 8 : AVERROR(EPIPE) -> code 224.
    return returncode in (1, 224) and "broken pipe" in stderr.casefold()


__all__ = [
    "NVENCC_VIDEO_CODECS",
    "NVENCC_DYNAMIC_HDR_CODECS",
    "NVENCC_MANUAL_STATIC_HDR_CODECS",
    "NVENCC_WORKFLOW_OWNED_FLAGS",
    "NVENCC_QP_TRIPLET_FLAGS",
    "NVENCC_CODEC_FLAG",
    "NVENCC_OUTPUT_EXT",
    "is_nvencc_codec",
    "nvencc_supports_dynamic_hdr",
    "nvencc_supports_manual_static_hdr",
    "nvencc_binary_name",
    "detect_nvencc_available",
    "build_decode_pipe_cmd",
    "nvencc_requires_ffmpeg_filter_pipe",
    "nvencc_requires_ffmpeg_prefilter",
    "nvencc_ffmpeg_filter_vf",
    "nvencc_pipe_encode_video",
    "build_nvencc_command",
    "build_remux_cmd",
    "build_nvencc_pipeline",
    "map_nvencc_dovi_profile",
    "map_nvencc_tonemap_args",
    "map_nvencc_video_transform_args",
    "normalize_nvencc_qp_triplet",
    "sanitize_nvencc_extra_params",
    "split_nvencc_extra_params",
    "nvencc_extra_params_report",
    "nvencc_workflow_option_values",
    "nvencc_option_name",
    "nvencc_intermediate_path",
    "is_expected_nvencc_pipe_producer_exit",
]
