"""Résolution du bloc ``encode`` d'un job en configuration d'encodage (règles du panneau Encodage).

Couches, de la plus basse à la plus haute : défauts de la source (HDR compris) → preset (champs
propres à la source exclus) → ``video.default`` → réglages de la piste. L'état obtenu est
normalisé par ``core.workflows.encode.planning.track_settings`` (même politique HDR que la GUI),
puis converti en :class:`EncodeConfig`. Tout en copie sans transformation : aucun encodage, le
job reste un remux.
"""

from __future__ import annotations

import threading

import dataclasses
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.inspector import AudioTrack, FileInfo
from core.jobs.constants import EXIT_VALIDATION
from core.jobs.errors import JobError
from core.jobs.logging import JobLogger
from core.service.schemas.encode import (
    AudioTrackSpec,
    EncodeBlock,
    TrackSelector,
    VideoEncodeSpec,
)
from core.workflows.encode.catalog import (
    AUDIO_CODECS,
    HARDWARE_VIDEO_CODECS,
    NVENCC_VIDEO_CODECS,
    SOFTWARE_VIDEO_CODECS,
    presets_for_codec,
    rate_control_spec,
    rate_controls_for_codec,
)
from core.workflows.encode.domain.codecs import resolve_output_bit_depth
from core.workflows.encode.models import (
    AudioTrackSettings,
    EncodeConfig,
    EncodePreset,
    FrameInterpolationSettings,
    QualityMode,
    RestoreSettings,
    VideoCropSettings,
    VideoEncodeSettings,
    VideoFilterSettings,
    VideoResizeSettings,
    default_audio_bitrate_kbps,
)
from core.workflows.encode.planning.track_settings import (
    EncoderEnvironment,
    SourceVideoContext,
    apply_preset_to_state,
    default_video_state,
    extract_hdr_meta_fields,
    is_pure_copy,
    normalize_video_state,
    preset_problem,
    restore_flags,
    source_has_dv,
    source_has_hdr10plus,
    source_has_static_hdr10,
    video_settings_from_state,
    video_track_for,
)
from core.workflows.encode.remux_bridge import merge_remux_into_encode_config
from core.workflows.remux_models import RemuxConfig, SourceInput, TrackEntry

VIDEO_CODEC_IDS = frozenset({"copy", *(c for c, _ in SOFTWARE_VIDEO_CODECS), *(c for c, _ in HARDWARE_VIDEO_CODECS),
                             *NVENCC_VIDEO_CODECS})
AUDIO_CODEC_IDS = frozenset(c for c, _ in AUDIO_CODECS)

#: Réglages publiés pour chaque piste vidéo (plan résolu de ``validate`` / ``preview``).
_PLAN_FIELDS = (
    "codec", "rate_control", "quality_mode", "crf", "cq", "bitrate_kbps", "target_size_mb", "preset", "extra_params",
    "bit_depth", "inject_hdr_meta", "master_display", "max_cll", "static_hdr_metadata_analysis_request", "copy_dv",
    "dovi_profile", "copy_hdr10plus", "bake_dovi_fel", "fel_precision", "fel_device", "fel_hdr10plus",
    "tonemap_to_sdr", "tonemap_algorithm",
)

CropDetector = Callable[..., tuple[int, int, int, int]]


@dataclass(frozen=True)
class ResolvedEncode:
    """Configuration d'encodage (``None`` : copie pure, le job reste un remux) et plan effectif."""

    config: EncodeConfig | None
    video: tuple[dict[str, Any], ...] = ()
    audio: tuple[dict[str, Any], ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def pure_copy(self) -> bool:
        return self.config is None


def _fail(message: str) -> JobError:
    return JobError(f"encode : {message}", EXIT_VALIDATION)


# ---------------------------------------------------------------------------
# Sélection des pistes
# ---------------------------------------------------------------------------


def _selected(remux_config: RemuxConfig, track_type: str) -> list[tuple[SourceInput, TrackEntry]]:
    """Pistes retenues d'un type, dans l'ordre de sortie."""
    sources = {source.file_index: source for source in remux_config.sources}
    selected: list[tuple[SourceInput, TrackEntry]] = []
    for item in remux_config.track_order:
        source = sources.get(int(item[0]))
        if source is None:
            continue
        entry_id = str(item[2]) if len(item) > 2 else ""
        track = next((t for t in source.tracks if (entry_id and t.entry_id == entry_id)
                      or (not entry_id and t.mkv_tid == int(item[1]))), None)
        if track is not None and track.enabled and track.track_type == track_type:
            selected.append((source, track))
    return selected


def _matches(selector: TrackSelector, source: SourceInput, track: TrackEntry) -> bool:
    if selector.entry_id is not None:
        return track.entry_id == selector.entry_id
    if source.file_index != selector.source:
        return False
    if selector.stream is not None:
        return track.mkv_tid == selector.stream and not track.is_new
    same_type = [t for t in source.tracks if t.track_type == track.track_type and not t.is_new]
    ordinal = selector.index if selector.index is not None else 0
    return not track.is_new and ordinal < len(same_type) and same_type[ordinal].entry_id == track.entry_id


def _target(selector: TrackSelector, selected: list[tuple[SourceInput, TrackEntry]], label: str
            ) -> tuple[SourceInput, TrackEntry]:
    found = [item for item in selected if _matches(selector, *item)]
    if not found:
        raise _fail(f"piste {label} introuvable ou non retenue ({selector.to_dict(omit_none=True)})")
    return found[0]


# ---------------------------------------------------------------------------
# Couches de réglages vidéo
# ---------------------------------------------------------------------------


def _merge(current: object, spec: Any, cls: Any, *, enable_on_presence: bool) -> Any:
    values = dataclasses.asdict(cls.from_value(current))
    updates = {key: value for key, value in spec.to_dict().items() if value is not None}
    if enable_on_presence and "enabled" in values and "enabled" not in updates:
        updates["enabled"] = True
    values.update(updates)
    return cls.from_value(values)


def apply_video_spec(state: dict[str, object], spec: VideoEncodeSpec,
                     env: EncoderEnvironment) -> dict[str, str | None]:
    """Applique une couche publique à un état ; renvoie ses intentions à vérifier après normalisation, par famille
    (``static``, ``dv``, ``hdr10plus``, ``tonemap`` → intention ou ``None``) : une couche suivante qui touche la même
    famille la remplace (``resolve_encode``)."""
    intents: dict[str, str | None] = {}
    if spec.codec is not None:
        codec = spec.codec.strip().lower()
        if codec not in VIDEO_CODEC_IDS:
            raise _fail(f"codec vidéo inconnu : {spec.codec}")
        previous = str(state.get("codec") or "copy")
        state["codec"] = codec
        if codec != previous and spec.encoder_preset is None and codec != "copy":
            available = presets_for_codec(codec)
            if available and str(state.get("preset") or "") not in available:
                state["preset"] = env.default_preset(codec)
        if codec != previous and spec.extra_params is None:
            by_codec = state.get("extra_params_by_codec")
            state["extra_params"] = by_codec.get(codec, "") if isinstance(by_codec, dict) else ""
    codec = str(state.get("codec") or "copy")
    if spec.rate_control is not None:
        rc = rate_control_spec(codec, spec.rate_control)
        if rc is None:
            known = ", ".join(item.rc_id for item in rate_controls_for_codec(codec)) or "aucun"
            raise _fail(f"mode de débit {spec.rate_control!r} inconnu pour {codec} (modes : {known})")
        state["rate_control"] = rc.rc_id
        state["quality_mode"] = QualityMode(rc.family)
    for key in ("crf", "cq", "bitrate_kbps", "extra_params", "bit_depth"):
        value = getattr(spec, key)
        if value is not None:
            state[key] = value
    if spec.encoder_preset is not None:
        state["preset"] = spec.encoder_preset
    if spec.crop is not None:
        crop = _merge(state.get("crop"), spec.crop, VideoCropSettings, enable_on_presence=True)
        if not crop.auto and not any((crop.top, crop.bottom, crop.left, crop.right)):
            crop = dataclasses.replace(crop, enabled=False)
        state["crop"] = crop
    if spec.resize is not None:
        state["resize"] = _merge(state.get("resize"), spec.resize, VideoResizeSettings, enable_on_presence=True)
    if spec.filters is not None:
        state["filters"] = _merge(state.get("filters"), spec.filters, VideoFilterSettings, enable_on_presence=False)
    if spec.interpolation is not None:
        state["interpolation"] = _merge(state.get("interpolation"), spec.interpolation, FrameInterpolationSettings,
                                        enable_on_presence=True)
    if spec.restoration is not None:
        state["restoration"] = _merge(state.get("restoration"), spec.restoration, RestoreSettings,
                                      enable_on_presence=True)
    if spec.static_hdr is not None and spec.static_hdr.estimate is not None:
        # Analyse HDR10 P5→P8.1 (règle du panneau : sur champs vides) : valeurs de la source, aucune saisie, HDR10
        # injecté. Une couche la demande ou la retire en entier (les fusions de documents l'isolent, ``json_io``).
        hdr = spec.static_hdr
        typed = [name for name in ("master_display", "max_cll") if str(getattr(hdr, name) or "").strip()]
        if hdr.mode == "off" or typed:
            raise _fail("static_hdr : estimate (analyse sur champs vides) incompatible avec mode off ou des valeurs "
                        f"saisies ({', '.join(typed) or 'mode off'})")
        state.update(inject_hdr_meta=True, hdr_disabled_by_codec=False, static_hdr_manual=False,
                     master_display=state.get("default_master_display") or "",
                     max_cll=state.get("default_max_cll") or "", static_hdr_master_display_manual=False,
                     static_hdr_max_cll_manual=False, static_hdr_metadata_analysis_request=hdr.estimate,
                     static_hdr_metadata_analysis_mode="", static_hdr_metadata_source="",
                     static_hdr_metadata_confidence="", static_hdr_analysis_disabled_by_codec="")
        intents["static"] = None
    elif spec.static_hdr is not None:
        hdr = spec.static_hdr
        mode = hdr.mode or ("manual" if hdr.master_display is not None or hdr.max_cll is not None else None)
        if mode is not None:
            state["hdr_disabled_by_codec"] = False  # choix explicite : rien à rétablir au changement de codec
            state["static_hdr_manual"] = mode == "manual"
            intents["static"] = "static_off" if mode == "off" else None
            # Mode explicite : une analyse demandée par une couche précédente est retirée (jamais à la place de ce
            # choix au lancement).
            state.update(static_hdr_metadata_analysis_request="", static_hdr_metadata_analysis_mode="",
                         static_hdr_metadata_source="", static_hdr_metadata_confidence="",
                         static_hdr_analysis_disabled_by_codec="")
        if mode in ("off", "source"):
            # Valeurs de la source et provenance remises ensemble (une couche suivante repart de la source).
            state.update(master_display=state.get("default_master_display") or "",
                         max_cll=state.get("default_max_cll") or "", static_hdr_master_display_manual=False,
                         static_hdr_max_cll_manual=False)
        if mode == "off":
            state["inject_hdr_meta"] = False
        elif mode == "source":
            state["inject_hdr_meta"] = True
        elif mode == "manual":
            if hdr.master_display is None and hdr.max_cll is None:
                raise _fail("static_hdr manual : master_display ou max_cll attendu")
            state["inject_hdr_meta"] = True
            # Seuls les champs fournis par cette couche : saisie explicite, même égale à la source ; les autres
            # gardent leur valeur et leur provenance (couche précédente).
            if hdr.master_display is not None:
                state.update(master_display=hdr.master_display, static_hdr_master_display_manual=True)
            if hdr.max_cll is not None:
                state.update(max_cll=hdr.max_cll, static_hdr_max_cll_manual=True)
    if spec.dolby_vision is not None:
        if spec.dolby_vision.mode is not None:
            state["copy_dv"] = spec.dolby_vision.mode == "copy"
            state["dynamic_hdr_disabled_by_codec"] = (False, restore_flags(state)[1])
            intents["dv"] = f"dv_{spec.dolby_vision.mode}"
        if spec.dolby_vision.profile is not None:
            state["dovi_profile"] = spec.dolby_vision.profile
    if spec.hdr10plus is not None and spec.hdr10plus.mode is not None:
        state["copy_hdr10plus"] = spec.hdr10plus.mode == "copy"
        state["dynamic_hdr_disabled_by_codec"] = (restore_flags(state)[0], False)
        intents["hdr10plus"] = f"hdr10plus_{spec.hdr10plus.mode}"
    if spec.fel is not None:
        for key, target in (("bake", "bake_dovi_fel"), ("precision", "fel_precision"), ("device", "fel_device"),
                            ("hdr10plus", "fel_hdr10plus")):
            value = getattr(spec.fel, key)
            if value is not None:
                state[target] = value
    if spec.tonemap is not None:
        if spec.tonemap.enabled is not None:  # choix explicite, gardé aux changements de codec
            state.update(tonemap_to_sdr=spec.tonemap.enabled, tonemap_choice=spec.tonemap.enabled)
            intents["tonemap"] = "tonemap" if spec.tonemap.enabled else None
        if spec.tonemap.algorithm is not None:
            state["tonemap_algorithm"] = spec.tonemap.algorithm
    return intents


def _check_intents(state: Mapping[str, object], ctx: SourceVideoContext, intents: set[str], label: str,
                   warnings: list[str]) -> None:
    """Politique HDR en copie = GUI : la source est recopiée telle quelle ; un retrait exige un réencodage."""
    copy = str(state.get("codec") or "copy") == "copy"
    if copy and "dv_off" in intents and source_has_dv(ctx.hdr_type):
        raise _fail(f"{label} : en copie, le Dolby Vision de la source est conservé ; réencoder pour le retirer")
    if copy and "hdr10plus_off" in intents and source_has_hdr10plus(ctx.hdr_type):
        raise _fail(f"{label} : en copie, le HDR10+ de la source est conservé ; réencoder pour le retirer")
    if copy and "static_off" in intents and source_has_static_hdr10(ctx.hdr_type, ctx.transfer):
        raise _fail(f"{label} : en copie, le HDR10 statique de la source est conservé ; réencoder pour le retirer")
    if copy and "tonemap" in intents:
        raise _fail(f"{label} : le tone mapping exige un réencodage")
    if "dv_copy" in intents and not state.get("copy_dv"):
        warnings.append(f"{label} : Dolby Vision non copié (source sans Dolby Vision ou codec incompatible)")
    if "hdr10plus_copy" in intents and not state.get("copy_hdr10plus"):
        warnings.append(f"{label} : HDR10+ non copié (source sans HDR10+ ou codec incompatible)")


def _plan_entry(source: SourceInput, track: TrackEntry, settings: VideoEncodeSettings) -> dict[str, Any]:
    entry: dict[str, Any] = {"source": source.file_index, "stream": track.mkv_tid, "entry_id": track.entry_id}
    for key in _PLAN_FIELDS:
        value = getattr(settings, key)
        entry[key] = value.value if isinstance(value, QualityMode) else value
    # Seuls les réglages du mode de débit actif sont publiés (les autres sont des valeurs dormantes).
    active = {QualityMode.CRF: "crf", QualityMode.CQ: "cq", QualityMode.BITRATE: "bitrate_kbps",
              QualityMode.SIZE: "target_size_mb"}
    for mode, key in active.items():
        if settings.codec == "copy" or settings.quality_mode != mode:
            entry.pop(key, None)
    if settings.codec != "copy":
        entry["output_bit_depth"] = resolve_output_bit_depth(settings)
    for key in ("crop", "resize", "filters", "interpolation", "restoration"):
        entry[key] = dataclasses.asdict(getattr(settings, key))
    return entry


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------

_KBPS = re.compile(r"(\d+)\s*kbps", re.IGNORECASE)


def _audio_info(info: FileInfo | None, track: TrackEntry) -> AudioTrack | None:
    if info is None:
        return None
    return next((audio for audio in info.audio_tracks if audio.index == track.mkv_tid), None)


def _audio_bitrate(codec: str, audio: AudioTrack | None, config: Any) -> int:
    per_channel = {"aac": getattr(config, "aac_bitrate_per_channel_kbps", None),
                   "eac3": getattr(config, "eac3_bitrate_per_channel_kbps", None)}.get(codec)
    return default_audio_bitrate_kbps(codec, getattr(audio, "channels", None), getattr(audio, "sample_rate", None),
                                      getattr(audio, "channel_layout", None), kbps_per_channel=per_channel)


def _audio_settings(remux_config: RemuxConfig, infos: Mapping[str, FileInfo], specs: tuple[AudioTrackSpec, ...],
                    config: Any) -> tuple[list[AudioTrackSettings], list[dict[str, Any]]]:
    selected = _selected(remux_config, "audio")
    by_entry: dict[str, AudioTrackSpec] = {}
    for spec in specs:
        if spec.codec.strip().lower() not in AUDIO_CODEC_IDS:
            raise _fail(f"codec audio inconnu : {spec.codec}")
        _source, track = _target(spec.select, selected, "audio")
        by_entry[track.entry_id] = spec
    settings: list[AudioTrackSettings] = []
    plan: list[dict[str, Any]] = []
    for source, track in selected:
        audio = _audio_info(infos.get(str(source.path)), track)
        audio_spec = by_entry.get(track.entry_id)
        codec, bitrate, core = "copy", 0, False
        if track.is_new:  # variante audio du job (audio_variants)
            variant = str(track.codec or "").strip().lower().replace("-", "")
            codec = variant if variant in AUDIO_CODEC_IDS else "copy"
            match = _KBPS.search(str(track.display_info or ""))
            bitrate = int(match.group(1)) if match else 0
        if audio_spec is not None:
            codec, core = audio_spec.codec.strip().lower(), audio_spec.truehd_core
            bitrate = audio_spec.bitrate_kbps or 0
        if codec != "copy" and not bitrate:
            bitrate = _audio_bitrate(codec, audio, config)
        settings.append(AudioTrackSettings(
            stream_index=track.mkv_tid, codec=codec, bitrate_kbps=bitrate or 384, extract_truehd_core=core,
            input_channels=getattr(audio, "channels", None), input_channel_layout=getattr(audio, "channel_layout", None),
            source_path=source.path, track_entry_id=track.entry_id,
        ))
        plan.append({"source": source.file_index, "stream": track.mkv_tid, "entry_id": track.entry_id,
                     "codec": codec, "bitrate_kbps": bitrate if codec != "copy" else None, "truehd_core": core})
    return settings, plan


# ---------------------------------------------------------------------------
# Résolution
# ---------------------------------------------------------------------------


def resolve_encode(
    block: EncodeBlock,
    remux_config: RemuxConfig,
    *,
    infos: Mapping[str, FileInfo],
    config: Any,
    env: EncoderEnvironment | None = None,
    load_preset: Callable[[str], EncodePreset] | None = None,
    detect_crop: CropDetector | None = None,
    logger: JobLogger | None = None,
) -> ResolvedEncode:
    """Configuration d'encodage du job ; ``None`` si tout reste en copie (le job reste un remux)."""
    env = env or EncoderEnvironment()
    preset = None
    if block.preset:
        if load_preset is None:
            raise _fail("aucun chargeur de preset d'encodage")
        preset = load_preset(block.preset)
    selected = _selected(remux_config, "video")
    if not selected:
        raise _fail("aucune piste vidéo retenue dans le job")
    video_block = block.video
    per_track: dict[str, list[Any]] = {}
    for spec in (video_block.tracks if video_block is not None else ()):
        _source, track = _target(spec.select, selected, "vidéo")
        per_track.setdefault(track.entry_id, []).append(spec)
    warnings: list[str] = []
    video_tracks: list[VideoEncodeSettings] = []
    video_plan: list[dict[str, Any]] = []
    for source, track in selected:
        info = infos.get(str(source.path))
        if info is None:
            raise _fail(f"source non inspectée : {source.path}")
        source_video = video_track_for(info, track.mkv_tid)
        raw = (source_video.raw or {}) if source_video is not None else {}
        master_display, max_cll = extract_hdr_meta_fields(raw, mediainfo_json=info.mediainfo_json or {})
        ctx = SourceVideoContext.from_file_info(info, track.mkv_tid, master_display=master_display, max_cll=max_cll)
        state = default_video_state(ctx, env)
        layers: list[VideoEncodeSpec] = [video_block.default] if video_block is not None and video_block.default else []
        layers += per_track.get(track.entry_id, [])
        if preset is not None:
            # Mêmes refus que « Charger » du panneau Encodage, jamais de normalisation silencieuse.
            tonemap = bool(state.get("tonemap_to_sdr")) or any(
                layer.tonemap is not None and bool(layer.tonemap.enabled) for layer in layers)
            problem = preset_problem(preset.to_video_settings(), ctx, env, tonemap=tonemap)
            if problem:
                raise _fail(f"preset « {block.preset} » non applicable à la vidéo {source.file_index}:"
                            f"{track.mkv_tid} : {problem}")
            state = apply_preset_to_state(state, preset, ctx, env)
        families: dict[str, str | None] = {}
        for layer in layers:
            families.update(apply_video_spec(state, layer, env))  # dernière couche de chaque famille
        intents = {intent for intent in families.values() if intent}
        # Tone mapping : choix explicite des couches, sinon défaut du codec final (règle du panneau).
        state = normalize_video_state(state, ctx, env)
        label = f"vidéo {source.file_index}:{track.mkv_tid}"
        _check_intents(state, ctx, intents, label, warnings)
        crop = VideoCropSettings.from_value(state.get("crop"))
        if crop.enabled and crop.auto:
            state["crop"] = _detect_crop(crop, info, track, state, detect_crop, logger, label)
        settings = video_settings_from_state(state, ctx, stream_index=track.mkv_tid, source_path=source.path,
                                             track_entry_id=track.entry_id, env=env)
        if block.target_size_mb is not None and settings.quality_mode == QualityMode.SIZE:
            settings = dataclasses.replace(settings, target_size_mb=block.target_size_mb)
        video_tracks.append(settings)
        video_plan.append(_plan_entry(source, track, settings))
    audio_tracks, audio_plan = _audio_settings(remux_config, infos, block.audio, config)
    first_info = infos.get(str(selected[0][0].path))
    duration = _job_duration(first_info, selected[0][1])
    encode_config = EncodeConfig(
        target_size_mb=block.target_size_mb if any(v.quality_mode == QualityMode.SIZE for v in video_tracks) else None,
        source=video_tracks[0].source_path or selected[0][0].path,
        output=remux_config.output,
        video=video_tracks[0],
        video_tracks=video_tracks,
        audio_tracks=audio_tracks,
        copy_subtitles=True,
        duration_s=duration,
        copy_dv=video_tracks[0].copy_dv,
        copy_hdr10plus=video_tracks[0].copy_hdr10plus,
        dovi_profile=video_tracks[0].dovi_profile,
        work_dir=remux_config.work_dir or getattr(config, "work_dir", None),
        file_title=remux_config.file_title,
        extra_attachments=list(remux_config.extra_attachments),
        tmdb_cover=remux_config.tmdb_cover,
        tag_overrides=remux_config.tag_overrides,
        chapter_overrides=remux_config.chapter_overrides,
        mux_backend=remux_config.mux_backend,
    )
    if logger is not None:  # copie pure comprise : le job reste un remux, l'avertissement est publié
        for warning in warnings:
            logger.emit("warning", warning)
    if is_pure_copy(encode_config):
        return ResolvedEncode(None, tuple(video_plan), tuple(audio_plan), tuple(warnings))
    merged = merge_remux_into_encode_config(encode_config, remux_config)
    return ResolvedEncode(merged, tuple(video_plan), tuple(audio_plan), tuple(warnings))


def _job_duration(info: FileInfo | None, track: TrackEntry) -> float | None:
    """Durée de référence : piste vidéo principale (comme le panneau), sinon le conteneur."""
    if info is None:
        return None
    video = video_track_for(info, track.mkv_tid)
    duration = getattr(video, "duration_s", None) if video is not None else None
    if isinstance(duration, (int, float)) and duration > 0:
        return float(duration)
    return info.duration_s


def _detect_crop(crop: VideoCropSettings, info: FileInfo, track: TrackEntry, state: Mapping[str, object],
                 detect: CropDetector | None, logger: JobLogger | None, label: str) -> VideoCropSettings:
    """Recadrage automatique mesuré (bouton Auto-crop du panneau) : valeurs en pixels dans le plan."""
    if detect is None:
        raise _fail(f"{label} : recadrage automatique indisponible (aucun détecteur)")
    video = video_track_for(info, track.mkv_tid)
    dimensions = (int(getattr(video, "width", 0) or 0), int(getattr(video, "height", 0) or 0))
    if dimensions[0] <= 0 or dimensions[1] <= 0:
        raise _fail(f"{label} : recadrage automatique impossible (dimensions inconnues)")
    top, bottom, left, right = detect(
        info.path, dimensions=dimensions, codec=str(state.get("codec") or "libx265"),
        copy_dv=bool(state.get("copy_dv")), duration_s=_job_duration(info, track), stream_index=track.mkv_tid,
    )
    if logger is not None:
        logger.emit("info", f"{label} : recadrage mesuré haut={top} bas={bottom} gauche={left} droite={right}")
    if not any((top, bottom, left, right)):
        return VideoCropSettings(enabled=False, unit="px")
    return VideoCropSettings(enabled=True, unit="px", top=top, bottom=bottom, left=left, right=right, auto=False)


def preset_loader(profiles_dir: Path, base_dir: Path | None = Path()) -> Callable[[str], EncodePreset]:
    """Preset par nom (presets enregistrés de la GUI) ou par chemin JSON (relatif : sous ``base_dir``, dossier de
    référence ; ``None`` : un nom relatif n'est jamais cherché comme fichier)."""
    from core.json_documents import preset_is_file, read_json_document
    from core.workflows.encode.profiles import ProfileManager

    def load(name_or_path: str) -> EncodePreset:
        path = Path(name_or_path).expanduser()
        if not path.is_absolute() and base_dir is not None:
            path = Path(base_dir) / path
        if preset_is_file(name_or_path, base_dir):
            try:
                return ProfileManager._preset(read_json_document(path))
            except (OSError, ValueError, TypeError) as exc:
                raise _fail(f"preset illisible ({path}) : {exc}") from exc
        for preset in ProfileManager(profiles_dir).load_all():
            if preset.name == name_or_path:
                return preset
        raise _fail(f"preset d'encodage introuvable : {name_or_path}")

    return load


def resolve_from_job(
    raw_block: Any,
    remux_config: RemuxConfig,
    *,
    config: Any,
    options: Any,
    logger: JobLogger | None = None,
    cache: Any = None,
    report_unknown: bool = True,
    cancel_event: threading.Event | None = None,
) -> ResolvedEncode | None:
    """Bloc ``encode`` brut d'un job résolu ; ``None`` sans bloc. Sources inspectées (cache réutilisé si fourni).

    Document tolérant : clé inconnue ignorée, signalée au journal sauf ``report_unknown=False`` (déjà
    contrôlée à la lecture de la requête : refusée par l'API, signalée par le CLI).
    ``cancel_event`` : inspections et recadrage automatique annulables.
    """
    if raw_block is None:
        return None
    import functools

    from core.inspector import FileInspector
    from core.workflows.encode.runtime.crop_detector import detect_video_crop

    block, unknown = EncodeBlock.parse(raw_block, strict=False)
    if report_unknown and logger is not None:
        for detail in unknown:
            logger.emit("warning", f"encode : clé inconnue ignorée : {detail.path}")
    ffprobe = str(getattr(options, "ffprobe", None) or config.tool_ffprobe)
    inspector = FileInspector(ffprobe_bin=ffprobe, mediainfo_bin=str(getattr(options, "mediainfo", None)
                                                                      or config.tool_mediainfo),
                              cancel_event=cancel_event)
    infos = {str(source.path): (cache.inspect(inspector, Path(source.path)) if cache is not None
                                else inspector.inspect(Path(source.path)))
             for source in remux_config.sources}
    ffmpeg = str(getattr(options, "ffmpeg", None) or config.tool_ffmpeg)
    resolved = resolve_encode(
        block, remux_config, infos=infos, config=config,
        load_preset=preset_loader(Path(config.app_data_dir) / "encode_profiles",
                                  getattr(options, "base_dir", Path())),
        detect_crop=functools.partial(detect_video_crop, ffmpeg_bin=ffmpeg,
                                      dovi_tool_bin=str(getattr(config, "tool_dovi_tool", "dovi_tool")),
                                      ffprobe_bin=ffprobe, cancel_event=cancel_event),
        logger=logger,
    )
    if getattr(options, "api", False) and resolved.config is not None:
        _refuse_foreign_extra_params(resolved.config, ffmpeg=ffmpeg,
                                     nvencc=str(getattr(config, "tool_nvencc", None) or "nvencc"))
    return resolved


def _refuse_foreign_extra_params(encode_config: Any, *, ffmpeg: str, nvencc: str) -> None:
    """Requête de l'API : paramètres libres effectifs (presets compris) limités aux options de l'encodeur."""
    from core.service.errors import ApiError, ErrorCode, ErrorDetail
    from core.workflows.encode.domain.api_extra_params import api_extra_params_errors
    from core.workflows.encode.planning.track_settings import routing_video_tracks

    errors = [error for video in routing_video_tracks(encode_config)
              for error in api_extra_params_errors(getattr(video, "codec", ""), getattr(video, "extra_params", ""),
                                                   ffmpeg=ffmpeg, nvencc=nvencc)]
    if errors:
        raise ApiError(ErrorCode.VALIDATION, "Paramètres libres refusés :\n" + "\n".join(f"- {e}" for e in errors),
                       details=[ErrorDetail("", error) for error in errors])
