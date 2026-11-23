"""
core/workflows/remux_sync_rewrite.py — Réécriture de synchro du Remux, commune aux deux backends.

Une piste audio / sous-titres décalée (mode « container ») ou calibrée est
réécrite en Matroska mono-piste quand ``[sync] rewrite_enabled`` le permet.
Les backends FFmpeg et natif prennent la même décision et produisent le même
artefact ; seul le muxage final diffère.

- **Intention planifiée** (:func:`plan_sync_rewrites`, sans sonde, aperçu) :
  décalage ou calibration à appliquer, réécriture requise si une calibration
  est effective.
- **Décision définitive** (:func:`materialize_sync_rewrites`, au lancement) :
  :meth:`SyncRewriteService.maybe_materialize` sonde la piste (canaux, profil
  immersif, codec réel) ; ``None`` → décalage conteneur, sauf réécriture
  requise (erreur explicite).
- Toujours lancée sur la source d'origine et son index d'origine, avant toute
  normalisation (synchro timeline FFmpeg, canonicalisation, lecture en flux).
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from core.bluray import ffprobe_input_args
from core.runner import TaskCancelledError
from core.subprocess_utils import run_cancellable_capture, subprocess_text_kwargs
from core.workflows.common.audio_timestamps import (
    preroll_input_offset,
    probe_timestamp_payload,
    timestamp_seconds,
)

from core.workflows.common.sync_rewrite import (
    EmptySubtitleRewrite,
    SyncRewritePreparedInput,
    SyncRewriteService,
    audio_bitrate_kbps_from_display_info,
    normalized_rewrite_codec,
    sync_rewrite_forced_offset,
)
from core.workflows.remux_mapping import MappedTrack, track_sync_payload
from core.workflows.remux_models import RemuxConfig, RemuxError
from core.workflows.sync_calibration import SyncCalibration, effective_calibration, resolve_track_sync


@dataclass(frozen=True)
class SyncRewriteSettings:
    """Réglages de la réécriture, transmis explicitement (``AppConfig`` → workflow → backends, aperçu)."""

    enabled: bool = False
    advanced_audio: bool = False
    audio_bitrates: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class SyncRewriteIntent:
    """Piste à réécrire si elle est éligible après sonde (rang dans la liste des pistes mappées)."""

    index: int
    mapped_track: MappedTrack
    offset_ms: int
    calibration: SyncCalibration | None

    @property
    def required(self) -> bool:
        """Une calibration effective n'a pas d'équivalent en décalage conteneur."""
        return self.calibration is not None


@dataclass(frozen=True)
class MaterializedRewrite:
    """Piste réécrite : artefact Matroska mono-piste (``prepared``) ou sous-titre vidé (``empty``)."""

    intent: SyncRewriteIntent
    prepared: SyncRewritePreparedInput | None = None
    empty: bool = False


_CALIBRATION_NEEDS_REWRITE = (
    "Synchronisation multi-segments : la réécriture sync réelle est requise "
    "(activer sync.rewrite_enabled ou choisir sync_mode=physical)."
)


def plan_sync_rewrites(
    config: RemuxConfig,
    mapped_tracks: Sequence[MappedTrack],
    settings: SyncRewriteSettings,
) -> tuple[list[MappedTrack], list[SyncRewriteIntent]]:
    """Pistes mappées (décalage résolu depuis la calibration) et intentions de réécriture.

    Règles de l'étape historique du backend FFmpeg : décalage nul sans
    calibration → rien ; mode « offset » forcé → décalage conteneur, seulement
    sans calibration ; réécriture désactivée avec une calibration effective →
    erreur. Mode ``physical`` : la préparation physique s'en charge.
    """
    tracks = list(mapped_tracks)
    if config.sync_mode != "container":
        return tracks, []
    if not settings.enabled:
        if any(
            mapped.track.track_type in {"audio", "subtitle"}
            and effective_calibration(
                track_sync_payload(config, mapped.track, mapped.source_file_index),
                mapped.track.track_type,
            ) is not None
            for mapped in tracks
        ):
            raise RemuxError(_CALIBRATION_NEEDS_REWRITE)
        return tracks, []
    intents: list[SyncRewriteIntent] = []
    for index, mapped in enumerate(tracks):
        if mapped.track.track_type not in {"audio", "subtitle"}:
            continue
        offset_ms, calibration = resolve_track_sync(
            track_sync_payload(config, mapped.track, mapped.source_file_index),
            int(mapped.track.time_shift_ms or 0),
            mapped.track.track_type,
        )
        if offset_ms != mapped.track.time_shift_ms:
            mapped = replace(mapped, track=replace(mapped.track, time_shift_ms=offset_ms))
            tracks[index] = mapped
        if offset_ms == 0 and calibration is None:
            continue
        if calibration is None and sync_rewrite_forced_offset(mapped.track):
            continue
        intents.append(SyncRewriteIntent(index, mapped, int(offset_ms), calibration))
    return tracks, intents


def audio_rewrite_preserve_source(track) -> bool:
    """Réécriture aux paramètres de la source (codec, débit) : aucune variante demandée sur la piste."""
    source_codec = normalized_rewrite_codec(str(getattr(track, "orig_codec", "") or getattr(track, "codec", "") or ""))
    current_codec = normalized_rewrite_codec(str(getattr(track, "codec", "") or ""))
    if current_codec and source_codec and current_codec != source_codec:
        return False
    source_bitrate = audio_bitrate_kbps_from_display_info(str(getattr(track, "orig_display_info", "") or ""))
    current_bitrate = audio_bitrate_kbps_from_display_info(str(getattr(track, "display_info", "") or ""))
    if source_bitrate is not None and current_bitrate is not None and source_bitrate != current_bitrate:
        return False
    source_display = str(getattr(track, "orig_display_info", "") or "")
    current_display = str(getattr(track, "display_info", "") or "")
    if source_display != current_display and (source_bitrate is None) != (current_bitrate is None):
        return False
    return True


def rewrite_token(mapped: MappedTrack) -> str:
    """Nom stable de l'artefact : source, index, rang de sortie, type (deux copies → deux artefacts)."""
    return (
        f"remux_f{mapped.source_file_index}_s{mapped.stream_index}_"
        f"{mapped.out_type_index}_{mapped.track.track_type}"
    )


def materialize_sync_rewrites(
    config: RemuxConfig,
    intents: Sequence[SyncRewriteIntent],
    *,
    service: SyncRewriteService,
    tmp_dir: Path,
    first_input_idx: int,
    cancel_cb: Callable[[], bool] | None = None,
    allow_empty_subtitles: bool = False,
) -> list[MaterializedRewrite]:
    """Décision définitive (sonde) et artefacts, sur les références d'origine des pistes.

    ``None`` du service → décalage conteneur conservé, sauf réécriture requise
    (``RemuxError``). ``allow_empty_subtitles`` : sous-titre dont tous les
    événements disparaissent → piste vide (le backend garde son TrackEntry).
    """
    results: list[MaterializedRewrite] = []
    input_idx = int(first_input_idx)
    for intent in intents:
        mapped = intent.mapped_track
        track = mapped.track
        try:
            prepared = service.maybe_materialize(
                source_path=mapped.source_path,
                stream_index=int(mapped.stream_index),
                track_type=track.track_type,
                codec=track.orig_codec or track.codec,
                title=track.title,
                display_info=track.orig_display_info or track.display_info,
                offset_ms=intent.offset_ms,
                tmp_dir=tmp_dir,
                input_idx=input_idx,
                token=rewrite_token(mapped),
                preserve_source_audio_params=audio_rewrite_preserve_source(track),
                audio_target_codec=track.codec,
                audio_target_bitrate_kbps=audio_bitrate_kbps_from_display_info(track.display_info),
                cancel_cb=cancel_cb,
                calibration=intent.calibration,
                crossfade_ms=config.crossfade_ms,
            )
        except EmptySubtitleRewrite:
            if not allow_empty_subtitles or track.track_type != "subtitle":
                raise
            results.append(MaterializedRewrite(intent, empty=True))
            continue
        if prepared is None:
            if intent.required:
                raise RemuxError(
                    "Calibration multi-segments non applicable à cette piste "
                    f"(#{mapped.stream_index}) : synchronisation interrompue.",
                )
            continue
        results.append(MaterializedRewrite(intent, prepared))
        input_idx += 1
    return results


# =============================================================================
# Placement temporel : celui du muxage final FFmpeg, mesuré
# =============================================================================

#: Démultiplexeurs FFmpeg à discontinuités (``AVFMT_TS_DISCONT``) : l'origine
#: retirée par ``ffmpeg`` est recalculée sur les flux retenus
#: (``correct_input_start_times``), pas celle du format.
TS_DISCONT_FORMATS = frozenset({"mpegts", "mpegtsraw", "mpeg", "hls", "flv", "live_flv", "rtsp", "sdp", "rtp"})


def probe_origin_payload(
    source: Path, ffprobe: str, *, cancel_cb: Callable[[], bool] | None = None,
) -> dict[str, object]:
    """``start_time`` et nom du format, ``start_time`` de chaque flux (sonde annulable, au lancement)."""
    def check_cancelled() -> None:
        if cancel_cb is not None and cancel_cb():
            raise TaskCancelledError()

    command = [
        ffprobe, "-v", "error", "-show_entries",
        "format=start_time,format_name:stream=index,start_time", "-of", "json",
        *ffprobe_input_args(source),
    ]
    try:
        result = run_cancellable_capture(
            command, cancel_cb=cancel_cb, check_cancelled=check_cancelled,
            check=True, timeout=20, **subprocess_text_kwargs(),
        )
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict):
            raise ValueError("réponse ffprobe non objet")
        return payload
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise RemuxError(f"Origine temporelle illisible : {Path(source).name}") from exc


def ffmpeg_input_origin(payload: Mapping[str, object], retained_streams: Sequence[int] = ()) -> float:
    """Origine (s) que ``ffmpeg`` retire d'une entrée lue sans ``-copyts``.

    ``start_time`` du format ; format à discontinuités : minimum des
    ``start_time`` des flux retenus (sans flux retenu lisible : celle du format).
    """
    fmt = payload.get("format")
    fmt = fmt if isinstance(fmt, dict) else {}
    start = timestamp_seconds(fmt.get("start_time"))
    names = {name.strip() for name in str(fmt.get("format_name") or "").split(",")}
    if not names & TS_DISCONT_FORMATS:
        return start
    retained = {int(index) for index in retained_streams}
    streams = payload.get("streams")
    starts = [
        timestamp_seconds(stream.get("start_time"))
        for stream in (streams if isinstance(streams, list) else [])
        if isinstance(stream, dict) and str(stream.get("index", "")).isdigit()
        and int(stream["index"]) in retained and stream.get("start_time") not in (None, "", "N/A")
    ]
    return min(starts) if starts else start


def rewrite_input_offsets(
    base: Mapping[Path, float],
    materialized: Sequence[MaterializedRewrite],
    sources: Sequence[Path],
    probe_preroll: Callable[[Path], float],
) -> dict[Path, float]:
    """``-itsoffset`` du muxage final FFmpeg : artefacts réécrits et sources (pré-roll).

    Règle historique du backend FFmpeg, partagée avec le natif qui en déduit le
    placement : artefact → ``ffmpeg_timestamp_offset_s`` ; source d'un audio
    réécrit → ``source_timestamp_offset_s`` (premier venu) ; dès qu'un audio est
    réécrit, toute autre source reçoit son pré-roll (CodecDelay de ses pistes
    audio, même non retenues).
    """
    offsets = {Path(path): float(value) for path, value in base.items()}
    any_audio = False
    for item in materialized:
        prepared = item.prepared
        if prepared is None:
            continue
        offsets[Path(prepared.path)] = prepared.ffmpeg_timestamp_offset_s
        if item.intent.mapped_track.track.track_type == "audio":
            any_audio = True
            offsets.setdefault(Path(item.intent.mapped_track.source_path), prepared.source_timestamp_offset_s)
    if any_audio:
        for source in sources:
            if Path(source) not in offsets:
                offsets[Path(source)] = probe_preroll(Path(source))
    return offsets


def probe_preroll_offset(ffprobe: str, cancel_cb: Callable[[], bool] | None = None) -> Callable[[Path], float]:
    """Pré-roll d'entrée d'une source (sonde courte), pour :func:`rewrite_input_offsets`."""
    return lambda source: preroll_input_offset(probe_timestamp_payload(source, ffprobe, cancel_cb=cancel_cb))


@dataclass(frozen=True)
class NativeRewrite:
    """Piste réécrite substituée dans le plan natif ; décalage final connu une fois ``R(S)`` établi.

    ``relative_s`` = placement relatif à la source dans le muxage final FFmpeg :
    ``itsoffset(A) − Δ(A) − (itsoffset(S) − Δ(S))``.
    """

    source_index: int           # nouvelle source (artefact mono-piste)
    origin_source_index: int    # source d'origine
    origin_path: Path
    relative_s: float
    #: Origine à appliquer si la source d'origine n'a plus aucune route native : Δ(S) − itsoffset(S).
    ffmpeg_origin_s: float


def _with_resolved_offsets(config: RemuxConfig, resolved: Sequence[MappedTrack]) -> RemuxConfig:
    """Décalages résolus depuis les calibrations reportés dans les pistes de la configuration."""
    offsets = {
        (mapped.source_file_index, mapped.track.entry_id): int(mapped.track.time_shift_ms or 0)
        for mapped in resolved
    }
    sources = []
    for source in config.sources:
        tracks = [
            replace(track, time_shift_ms=offsets[(source.file_index, track.entry_id)])
            if offsets.get((source.file_index, track.entry_id), track.time_shift_ms) != track.time_shift_ms
            else track
            for track in source.tracks
        ]
        sources.append(replace(source, tracks=tracks))
    return replace(config, sources=sources)


def prepare_native_sync_rewrites(
    config: RemuxConfig,
    settings: SyncRewriteSettings,
    *,
    service: SyncRewriteService,
    ffprobe_bin: str,
    tmp_dir: Path,
    empty_subtitle_artifact: Callable[[MappedTrack], Path],
    cancel_cb: Callable[[], bool] | None = None,
) -> tuple[RemuxConfig, list[NativeRewrite]]:
    """Pistes réécrites substituées par des sources mono-piste, pour l'assemblage natif.

    Même décision et mêmes artefacts que le backend FFmpeg ; chaque piste
    réécrite garde sa provenance (``rewrite_origin``). Le décalage final de sa
    route dépend de l'origine native de la source (``R(S)``), connue après la
    canonicalisation : voir :func:`finalize_native_rewrite_shifts`.
    """
    from core.workflows.remux_mapping import resolve_mapped_tracks
    from core.matroska.mux_plan import deterministic_source_identity
    from core.workflows.remux_models import SourceInput

    mapped_tracks = resolve_mapped_tracks(config)
    resolved, intents = plan_sync_rewrites(config, mapped_tracks, settings)
    config = _with_resolved_offsets(config, resolved)
    if not intents:
        return config, []
    materialized = materialize_sync_rewrites(
        config, intents, service=service, tmp_dir=tmp_dir, first_input_idx=0,
        cancel_cb=cancel_cb, allow_empty_subtitles=True,
    )
    if not materialized:
        return config, []
    offsets = rewrite_input_offsets(
        config.input_timestamp_offsets, materialized, [source.path for source in config.sources],
        probe_preroll_offset(ffprobe_bin, cancel_cb),
    )
    rewritten = {(item.intent.mapped_track.source_file_index, item.intent.mapped_track.track.entry_id)
                 for item in materialized}
    # Flux que le muxage final FFmpeg retiendrait de chaque source (origine d'un format à discontinuités).
    retained: dict[int, list[int]] = {}
    for mapped in resolved:
        if (mapped.source_file_index, mapped.track.entry_id) not in rewritten:
            retained.setdefault(mapped.source_file_index, []).append(int(mapped.stream_index))
    payloads: dict[Path, dict[str, object]] = {}

    def origin(path: Path, streams: Sequence[int] = ()) -> float:
        if path not in payloads:
            payloads[path] = probe_origin_payload(path, ffprobe_bin, cancel_cb=cancel_cb)
        return ffmpeg_input_origin(payloads[path], streams)

    by_index = {source.file_index: source for source in config.sources}
    sources = list(config.sources)
    order = list(config.track_order)
    next_index = max(by_index, default=-1) + 1
    rewrites: list[NativeRewrite] = []
    for item in materialized:
        mapped = item.intent.mapped_track
        track = mapped.track
        source_path = Path(mapped.source_path)
        if item.empty or item.prepared is None:
            artifact = empty_subtitle_artifact(mapped)
            codec = track.orig_codec or track.codec
            relative, ffmpeg_origin = 0.0, 0.0  # piste sans paquet : placement sans objet
        else:
            artifact = Path(item.prepared.path)
            codec = item.prepared.codec or track.orig_codec or track.codec
            delta_s = origin(source_path, retained.get(mapped.source_file_index, ()))
            its_s = offsets.get(source_path, 0.0)
            relative = offsets.get(artifact, 0.0) - origin(artifact) - (its_s - delta_s)
            ffmpeg_origin = delta_s - its_s
        honors_contract = normalized_rewrite_codec(codec) == normalized_rewrite_codec(track.codec)
        new_track = replace(
            track,
            mkv_tid=0,
            time_shift_ms=0,
            sync_rewrite_label="",
            sync_rewrite_mode="",
            sync_calibration=None,
            is_new=False,
            # Variante déjà produite par la réécriture : aucune seconde transformation.
            orig_codec=codec,
            orig_display_info=track.display_info if honors_contract else track.orig_display_info,
            rewrite_origin=(source_path, int(mapped.stream_index)),
        )
        origin_source = by_index[mapped.source_file_index]
        sources.append(SourceInput(
            artifact, next_index, [new_track],
            origin_identity=origin_source.origin_identity or deterministic_source_identity(origin_source.path),
        ))
        for position, entry in enumerate(order):
            if (
                int(entry[0]) == mapped.source_file_index and int(entry[1]) == int(mapped.stream_index)
                and (len(entry) < 3 or entry[2] == track.entry_id)
            ):
                order[position] = (next_index, 0, new_track.entry_id)
        rewrites.append(NativeRewrite(next_index, mapped.source_file_index, source_path, relative, ffmpeg_origin))
        next_index += 1
    # Mode container conservé, calibrations consommées par la réécriture.
    return replace(config, sources=sources, track_order=order), rewrites


def finalize_native_rewrite_shifts(
    config: RemuxConfig,
    rewrites: Sequence[NativeRewrite],
    native_origin: Callable[[NativeRewrite], float | None],
) -> RemuxConfig:
    """Décalage de route de chaque piste réécrite : ``relative − R(S)``.

    ``native_origin`` renvoie ``R(S)`` (MKV lu tel quel : 0 ; flux : ``start_time`` ;
    canonicalisé : mesuré), ou ``None`` si la source d'origine n'a plus aucune
    route native : la piste est alors placée comme FFmpeg place sa source.
    """
    shifts: dict[int, int] = {}
    for rewrite in rewrites:
        origin_s = native_origin(rewrite)
        if origin_s is None:
            origin_s = rewrite.ffmpeg_origin_s
        shifts[rewrite.source_index] = round((rewrite.relative_s - origin_s) * 1000)
    if not shifts:
        return config
    return replace(config, sources=[
        replace(source, tracks=[replace(track, time_shift_ms=shifts[source.file_index]) for track in source.tracks])
        if source.file_index in shifts else source
        for source in config.sources
    ])


__all__ = [
    "MaterializedRewrite",
    "NativeRewrite",
    "TS_DISCONT_FORMATS",
    "ffmpeg_input_origin",
    "probe_origin_payload",
    "probe_preroll_offset",
    "rewrite_input_offsets",
    "SyncRewriteIntent",
    "SyncRewriteSettings",
    "audio_rewrite_preserve_source",
    "finalize_native_rewrite_shifts",
    "prepare_native_sync_rewrites",
    "materialize_sync_rewrites",
    "plan_sync_rewrites",
    "rewrite_token",
]
