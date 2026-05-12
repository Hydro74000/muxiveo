"""Hybrid v2 CLI support."""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

from PySide6.QtCore import QEventLoop

from core.config import AppConfig
from core.profiles.hybrid import HybridResolutionError, resolve_track_selector
from core.workflows.encode import (
    AudioTrackSettings,
    EncodeConfig,
    EncodeWorkflow,
    QualityMode,
    VideoEncodeSettings,
)
from core.workflows.encode.remux_bridge import merge_remux_into_encode_config
from core.workflows.remux_models import RemuxConfig, TrackEntry

from cli.constants import EXIT_EXISTS, EXIT_OK, EXIT_VALIDATION, EXIT_WORKFLOW
from cli.errors import CliError
from cli.json_io import json_default
from cli.logging import Logger
from cli.options import CommonOptions
from cli.remux_config import build_remux_config
from cli.runtime import workflow as remux_workflow
from cli.serializers import serialize_remux_config


def is_v2_job(job: dict[str, Any]) -> bool:
    return int(job.get("version", 1) or 1) == 2


def _selector_fallback_profile(job: dict[str, Any]) -> str:
    fallback = job.get("fallback_profile")
    if isinstance(fallback, dict):
        return str(fallback.get("name") or "").strip()
    return str(fallback or "").strip()


def _source_index_for_track(track: TrackEntry) -> int:
    file_id = str(track.file_id or "")
    if file_id.startswith("src"):
        try:
            return int(file_id[3:])
        except ValueError:
            return 0
    return 0


def _source_path_for_track(remux_config: RemuxConfig, track: TrackEntry) -> Path:
    source_index = _source_index_for_track(track)
    for source in remux_config.sources:
        if source.file_index == source_index:
            return source.path
    return remux_config.sources[0].path


def _all_tracks(remux_config: RemuxConfig) -> list[TrackEntry]:
    return [track for source in remux_config.sources for track in source.tracks]


def _ordered_enabled_tracks(remux_config: RemuxConfig) -> list[TrackEntry]:
    by_entry_id = {
        track.entry_id: track
        for source in remux_config.sources
        for track in source.tracks
    }
    by_key = {
        (source.file_index, track.mkv_tid): track
        for source in remux_config.sources
        for track in source.tracks
    }
    ordered: list[TrackEntry] = []
    for item in remux_config.track_order:
        entry_id = str(item[2]).strip() if len(item) > 2 else ""
        track = by_entry_id.get(entry_id) if entry_id else by_key.get((int(item[0]), int(item[1])))
        if track is not None and track.enabled:
            ordered.append(track)
    return ordered


def _quality_mode(value: object) -> QualityMode:
    try:
        return QualityMode(str(value or QualityMode.CRF.value))
    except ValueError:
        return QualityMode.CRF


def _int_value(value: object, default: int) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def _bitrate_from_display(display_info: str, default: int = 384) -> int:
    match = re.search(r"\b(\d+)\s*kbps\b", str(display_info or ""), flags=re.IGNORECASE)
    if not match:
        return default
    try:
        return int(match.group(1))
    except ValueError:
        return default


def _resolve_spec_track(
    spec: dict[str, Any],
    tracks: list[TrackEntry],
    *,
    context: str,
    fallback_profile: str = "",
) -> TrackEntry | None:
    selector = spec.get("selector")
    if isinstance(selector, dict):
        try:
            return resolve_track_selector(
                selector,
                tracks,
                context=context,
                strict=True,
                suggested_profile=fallback_profile,
            )
        except HybridResolutionError as exc:
            if fallback_profile:
                exc.report["suggested_profile"] = fallback_profile
            raise
    if any(key in spec for key in ("source", "source_index", "id", "mkv_tid", "stream")):
        source_index = int(spec.get("source", spec.get("source_index", 0)) or 0)
        track_id = spec.get("id", spec.get("mkv_tid", spec.get("stream")))
        if track_id is None:
            return None
        for track in tracks:
            if _source_index_for_track(track) == source_index and int(track.mkv_tid) == int(track_id):
                return track
    return None


def _video_settings_from_spec(
    spec: dict[str, Any],
    track: TrackEntry,
    remux_config: RemuxConfig,
) -> VideoEncodeSettings:
    codec = str(spec.get("codec") or "copy")
    return VideoEncodeSettings(
        stream_index=int(track.mkv_tid),
        source_path=_source_path_for_track(remux_config, track),
        track_entry_id=track.entry_id,
        codec=codec,
        quality_mode=_quality_mode(spec.get("quality_mode")),
        crf=_int_value(spec.get("crf"), 18),
        cq=_int_value(spec.get("cq"), 26),
        bitrate_kbps=_int_value(spec.get("bitrate_kbps"), 5000),
        target_size_mb=_int_value(spec.get("target_size_mb"), 4000),
        preset=str(spec.get("preset") or "slow"),
        extra_params=str(spec.get("extra_params") or ""),
        force_8bit=bool(spec.get("force_8bit", False)),
        force_10bit=bool(spec.get("force_10bit", False)),
        inject_hdr_meta=bool(spec.get("inject_hdr_meta", False)),
        master_display=str(spec.get("master_display") or ""),
        max_cll=str(spec.get("max_cll") or ""),
        copy_dv=bool(spec.get("copy_dv", False)),
        copy_hdr10plus=bool(spec.get("copy_hdr10plus", False)),
        dovi_profile=str(spec.get("dovi_profile") or "0"),
        tonemap_to_sdr=bool(spec.get("tonemap_to_sdr", False)),
        tonemap_algorithm=str(spec.get("tonemap_algorithm") or "hable"),
    )


def _audio_settings_from_spec(
    spec: dict[str, Any],
    track: TrackEntry,
    remux_config: RemuxConfig,
) -> AudioTrackSettings:
    codec = str(spec.get("codec") or spec.get("target_codec") or "copy").strip().lower() or "copy"
    bitrate = _int_value(spec.get("bitrate_kbps"), _bitrate_from_display(track.display_info))
    return AudioTrackSettings(
        stream_index=int(track.mkv_tid),
        codec=codec,
        bitrate_kbps=bitrate,
        extract_truehd_core=bool(spec.get("extract_truehd_core", False)),
        source_path=_source_path_for_track(remux_config, track),
        track_entry_id=track.entry_id,
    )


def _variant_spec_for_track(job: dict[str, Any], track: TrackEntry, tracks: list[TrackEntry]) -> dict[str, Any] | None:
    specs = job.get("audio_variants", job.get("derived_audio_tracks", []))
    if not isinstance(specs, list):
        return None
    source_entry_id = track.source_entry_id or ""
    for spec in specs:
        if not isinstance(spec, dict):
            continue
        if spec.get("entry_id") and str(spec.get("entry_id")) == track.entry_id:
            return spec
        selector = spec.get("source_selector", spec.get("selector"))
        if not isinstance(selector, dict):
            continue
        try:
            source_track = resolve_track_selector(selector, tracks, strict=False)
        except HybridResolutionError:
            source_track = None
        if source_track is not None and source_track.entry_id == source_entry_id:
            return spec
    return None


def _encode_block(job: dict[str, Any]) -> dict[str, Any]:
    block = job.get("encode", job.get("encoding", {}))
    return block if isinstance(block, dict) else {}


def build_hybrid_remux_config(
    job: dict[str, Any],
    config: AppConfig,
    options: CommonOptions,
    logger: Logger,
    *,
    cli_inputs: list[str] | None = None,
    cli_output: str | None = None,
) -> RemuxConfig:
    return build_remux_config(
        job,
        config,
        options,
        logger,
        cli_inputs=cli_inputs,
        cli_output=cli_output,
    )


def build_hybrid_encode_config(
    job: dict[str, Any],
    remux_config: RemuxConfig,
) -> EncodeConfig:
    block = _encode_block(job)
    tracks = _all_tracks(remux_config)
    ordered_tracks = _ordered_enabled_tracks(remux_config)
    fallback_profile = _selector_fallback_profile(job)

    video_specs: list[dict[str, Any]] = []
    if isinstance(block.get("video_tracks"), list):
        video_specs = [item for item in block["video_tracks"] if isinstance(item, dict)]
    elif isinstance(block.get("video"), dict):
        video_specs = [block["video"]]

    video_tracks: list[VideoEncodeSettings] = []
    for index, spec in enumerate(video_specs):
        track = _resolve_spec_track(
            spec,
            tracks,
            context=f"encode.video_tracks[{index}]",
            fallback_profile=fallback_profile,
        )
        if track is None:
            continue
        video_tracks.append(_video_settings_from_spec(spec, track, remux_config))

    if not video_tracks:
        first_video = next((track for track in ordered_tracks if track.track_type == "video"), None)
        if first_video is None:
            raise CliError("Aucune piste vidéo active pour l'encodage hybride.", EXIT_VALIDATION)
        video_tracks.append(_video_settings_from_spec({"codec": "copy"}, first_video, remux_config))

    audio_specs = block.get("audio_tracks", [])
    audio_tracks: list[AudioTrackSettings] = []
    if isinstance(audio_specs, list) and audio_specs:
        for index, spec in enumerate(audio_specs):
            if not isinstance(spec, dict):
                continue
            track = _resolve_spec_track(
                spec,
                tracks,
                context=f"encode.audio_tracks[{index}]",
                fallback_profile=fallback_profile,
            )
            if track is None:
                continue
            audio_tracks.append(_audio_settings_from_spec(spec, track, remux_config))
    else:
        for track in ordered_tracks:
            if track.track_type != "audio":
                continue
            spec = _variant_spec_for_track(job, track, tracks) or {}
            if track.is_new and not spec:
                spec = {"codec": track.codec.lower(), "bitrate_kbps": _bitrate_from_display(track.display_info)}
            audio_tracks.append(_audio_settings_from_spec(spec, track, remux_config))

    primary_source = Path(video_tracks[0].source_path or remux_config.sources[0].path)
    encode_config = EncodeConfig(
        source=primary_source,
        output=remux_config.output,
        video=video_tracks[0],
        video_tracks=video_tracks,
        audio_tracks=audio_tracks,
        copy_subtitles=True,
        keep_chapters=remux_config.keep_chapters,
        chapter_overrides=remux_config.chapter_overrides,
        extra_attachments=list(remux_config.extra_attachments),
        tag_overrides=remux_config.tag_overrides,
        file_title=remux_config.file_title,
        work_dir=remux_config.work_dir,
        tmdb_cover=remux_config.tmdb_cover,
        allow_missing_output_dir=bool(getattr(remux_config, "allow_missing_output_dir", False)),
    )
    return merge_remux_into_encode_config(encode_config, remux_config)


def hybrid_uses_encode(job: dict[str, Any], encode_config: EncodeConfig | None = None) -> bool:
    block = _encode_block(job)
    if job.get("audio_variants") or job.get("derived_audio_tracks"):
        return True
    if encode_config is None:
        return bool(block)
    return any(
        str(video.codec or "copy").strip().lower() != "copy"
        or bool(video.inject_hdr_meta)
        or bool(video.tonemap_to_sdr)
        or bool(video.copy_dv)
        or bool(video.copy_hdr10plus)
        or bool(video.extra_params)
        for video in encode_config.video_tracks
    ) or any(str(audio.codec or "copy").strip().lower() != "copy" for audio in encode_config.audio_tracks)


def encode_workflow(config: AppConfig, options: CommonOptions, logger: Logger) -> EncodeWorkflow:
    return EncodeWorkflow(
        ffmpeg_bin=str(options.ffmpeg or config.tool_ffmpeg),
        dovi_tool_bin=str(config.tool_dovi_tool),
        hdr10plus_bin=str(config.tool_hdr10plus),
        mediainfo_bin=str(options.mediainfo or config.tool_mediainfo),
        ffmpeg_threads=options.threads if options.threads is not None else config.ffmpeg_threads,
        writing_application=str(options.writing_application or ""),
        generate_nfo=config.generate_nfo if options.nfo is None else bool(options.nfo),
        nvencc_bin=getattr(config, "tool_nvencc", None) or None,
    )


def build_hybrid_payload(
    job: dict[str, Any],
    config: AppConfig,
    options: CommonOptions,
    logger: Logger,
    *,
    include_command: bool = False,
) -> tuple[dict[str, Any], RemuxConfig, EncodeConfig | None, bool]:
    try:
        remux_config = build_hybrid_remux_config(job, config, options, logger)
        encode_config = build_hybrid_encode_config(job, remux_config)
        use_encode = hybrid_uses_encode(job, encode_config)
    except HybridResolutionError as exc:
        payload = dict(exc.report)
        payload.setdefault("valid", False)
        payload.setdefault("errors", [str(exc)])
        raise CliError(json.dumps(payload, ensure_ascii=False, default=json_default), EXIT_VALIDATION) from exc

    if use_encode:
        wf = encode_workflow(config, options, logger)
        errors = wf.validate(encode_config)
        payload = {
            "valid": not errors,
            "mode": "encode",
            "errors": errors,
            "output": str(encode_config.output),
            "sources": [str(path) for path in {Path(video.source_path or encode_config.source) for video in encode_config.video_tracks}],
            "video_tracks": [json_default(video) for video in encode_config.video_tracks],
            "audio_tracks": [json_default(audio) for audio in encode_config.audio_tracks],
        }
        if include_command and not errors:
            command = wf.build_command(encode_config)
            payload["command"] = command
            payload["command_text"] = wf.preview_command(encode_config)
        return payload, remux_config, encode_config, True

    wf = remux_workflow(config, options, logger)
    errors = wf.validate(remux_config)
    payload = {
        "valid": not errors,
        "mode": "remux",
        "errors": errors,
        **serialize_remux_config(remux_config),
    }
    if include_command and not errors:
        payload["command"] = wf.build_command(remux_config)
        payload["command_text"] = wf.preview_command(remux_config)
    return payload, remux_config, None, False


def preview_hybrid_job(config: AppConfig, options: CommonOptions, logger: Logger, job: dict[str, Any]) -> int:
    payload, _remux_config, _encode_config, _use_encode = build_hybrid_payload(
        job,
        config,
        options,
        logger,
        include_command=True,
    )
    if payload["errors"]:
        for error in payload["errors"]:
            logger.emit("error", error)
        return EXIT_VALIDATION
    print(payload["command_text"])
    return EXIT_OK


def run_hybrid_job(
    config: AppConfig,
    options: CommonOptions,
    logger: Logger,
    job: dict[str, Any],
    *,
    force: bool = False,
) -> int:
    payload, remux_config, encode_config, use_encode = build_hybrid_payload(job, config, options, logger)
    if payload["errors"]:
        for error in payload["errors"]:
            logger.emit("error", error)
        return EXIT_VALIDATION
    output = encode_config.output if use_encode and encode_config is not None else remux_config.output
    if output.exists() and not force:
        raise CliError(f"Sortie déjà existante : {output} (utiliser --force)", EXIT_EXISTS)

    if use_encode:
        assert encode_config is not None
        wf = encode_workflow(config, options, logger)
        wf.log_message.connect(logger.workflow_log)
        signals = wf.run(encode_config)
    else:
        wf = remux_workflow(config, options, logger)
        wf.log_message.connect(logger.workflow_log)
        signals = wf.run(remux_config)

    loop = QEventLoop()
    state_exit = {"value": EXIT_OK}

    def done(message: str = "") -> None:
        if message:
            logger.emit("info", message)
        state_exit["value"] = EXIT_OK
        loop.quit()

    def failed(message: str, exc: object) -> None:
        logger.emit("error", message, exception=repr(exc))
        state_exit["value"] = EXIT_WORKFLOW
        loop.quit()

    def cancelled() -> None:
        logger.emit("error", "Opération annulée.")
        state_exit["value"] = EXIT_WORKFLOW
        loop.quit()

    signals.progress.connect(lambda line: logger.emit("info", line))
    signals.finished.connect(done)
    signals.failed.connect(failed)
    signals.cancelled.connect(cancelled)

    stop = threading.Event()
    watcher = threading.Thread(target=stop.wait, daemon=True)
    watcher.start()
    try:
        loop.exec()
    finally:
        stop.set()
    return state_exit["value"]
