"""Build RemuxConfig objects from CLI JSON jobs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.config import AppConfig
from core.media_info_fetcher import (
    TmdbError,
    TmdbFetcher,
    clean_filename_for_search,
    extract_year_from_filename,
)
from core.workflows.remux_models import RemuxConfig, TrackEntry, clone_track_entry
from core.profiles.hybrid import (
    HybridResolutionError,
    apply_track_spec,
    resolve_track_selector,
)

from cli.chapters import chapter_entries
from cli.constants import EXIT_ARGS, EXIT_VALIDATION
from cli.contract import validate_job_contract
from cli.errors import CliError
from cli.inspection import inspect_sources
from cli.json_io import deep_merge
from cli.logging import Logger
from cli.options import CommonOptions
from cli.rules import apply_track_rules


def _fallback_profile_name(job: dict[str, Any]) -> str:
    fallback = job.get("fallback_profile")
    if isinstance(fallback, dict):
        return str(fallback.get("name") or "").strip()
    return str(fallback or "").strip()


def _track_from_spec(
    spec: dict[str, Any],
    tracks: list[TrackEntry],
    *,
    strict_selectors: bool = False,
    context: str = "tracks",
) -> TrackEntry | None:
    selector = spec.get("selector")
    if isinstance(selector, dict):
        return resolve_track_selector(
            selector,
            tracks,
            context=context,
            strict=strict_selectors,
        )
    raw_source = spec.get("source", spec.get("source_index", 0))
    source_index = int(0 if raw_source is None else raw_source)
    file_id = f"src{source_index}"
    tid = spec.get("id", spec.get("mkv_tid", spec.get("stream")))
    if tid is None:
        return None
    track_id = int(tid)
    lookup = {(t.file_id, t.mkv_tid): t for t in tracks}
    return lookup.get((file_id, track_id))


def apply_explicit_track_edits(
    job: dict[str, Any],
    tracks: list[TrackEntry],
    *,
    strict_selectors: bool = False,
) -> None:
    specs = job.get("tracks", [])
    if not isinstance(specs, list):
        return
    for index, spec in enumerate(specs):
        if not isinstance(spec, dict):
            continue
        try:
            track = _track_from_spec(
                spec,
                tracks,
                strict_selectors=strict_selectors,
                context=f"tracks[{index}]",
            )
        except HybridResolutionError as exc:
            if _fallback_profile_name(job):
                exc.report["suggested_profile"] = _fallback_profile_name(job)
            raise
        if track is None:
            continue
        apply_track_spec(track, spec)


def apply_audio_variants(
    job: dict[str, Any],
    sources,
    tracks: list[TrackEntry],
    *,
    strict_selectors: bool = False,
) -> None:
    specs = job.get("audio_variants", job.get("derived_audio_tracks", []))
    if not isinstance(specs, list):
        return
    source_by_file_id = {
        track.file_id: source
        for source in sources
        for track in source.tracks
    }
    for index, spec in enumerate(specs):
        if not isinstance(spec, dict):
            continue
        selector = spec.get("source_selector", spec.get("selector"))
        if not isinstance(selector, dict):
            continue
        try:
            source_track = resolve_track_selector(
                selector,
                tracks,
                context=f"audio_variants[{index}]",
                strict=strict_selectors,
            )
        except HybridResolutionError as exc:
            if _fallback_profile_name(job):
                exc.report["suggested_profile"] = _fallback_profile_name(job)
            raise
        if source_track is None or source_track.track_type != "audio":
            continue
        new_entry = clone_track_entry(source_track, entry_id=str(spec.get("entry_id") or "") or None)
        codec = str(spec.get("codec") or spec.get("target_codec") or "").strip()
        bitrate = int(spec.get("bitrate_kbps") or 0)
        if codec:
            new_entry.codec = codec.upper() if codec.lower() != "copy" else source_track.codec
        if codec and codec.lower() != "copy":
            parts = [
                part.strip()
                for part in str(source_track.display_info or "").replace("·", "  ").split("  ")
                if part.strip() and "kbps" not in part.lower()
            ]
            if bitrate > 0:
                parts.append(f"{bitrate} kbps")
            new_entry.display_info = "  ".join(parts)
        apply_track_spec(new_entry, {**spec, "enabled": spec.get("enabled", True)})
        source = source_by_file_id.get(source_track.file_id)
        if source is not None:
            source.tracks.append(new_entry)
        tracks.append(new_entry)


def track_order(
    job: dict[str, Any],
    tracks: list[TrackEntry],
    *,
    strict_selectors: bool = False,
) -> list[tuple[int, int, str]]:
    explicit = job.get("track_order")
    if isinstance(explicit, list):
        order: list[tuple[int, int, str]] = []
        by_key = {(t.file_id, t.mkv_tid): t for t in tracks}
        for index, item in enumerate(explicit):
            if isinstance(item, dict):
                selector = item.get("selector")
                if isinstance(selector, dict):
                    try:
                        track = resolve_track_selector(
                            selector,
                            tracks,
                            context=f"track_order[{index}]",
                            strict=strict_selectors,
                        )
                    except HybridResolutionError as exc:
                        if _fallback_profile_name(job):
                            exc.report["suggested_profile"] = _fallback_profile_name(job)
                        raise
                    if track is not None and track.enabled:
                        source_index = int(track.file_id.removeprefix("src")) if track.file_id.startswith("src") else 0
                        order.append((source_index, track.mkv_tid, track.entry_id))
                    continue
                raw_source = item.get("source", item.get("source_index", 0))
                source_index = int(0 if raw_source is None else raw_source)
                raw_tid = item.get("id", item.get("mkv_tid", item.get("stream")))
                if raw_tid is None:
                    continue
                tid = int(raw_tid)
            else:
                source_index = int(item[0])
                tid = int(item[1])
            track = by_key.get((f"src{source_index}", tid))
            if track is not None and track.enabled:
                order.append((source_index, tid, track.entry_id))
        return order
    return [
        (int(track.file_id.removeprefix("src")), track.mkv_tid, track.entry_id)
        for track in tracks
        if track.enabled
    ]


def _resolve_tmdb(
    job: dict[str, Any],
    config: AppConfig,
    first_source: Path,
    logger: Logger,
) -> tuple[str, dict[str, str] | None, tuple[str, str] | None]:
    tmdb = job.get("tmdb")
    if not tmdb:
        return "", None, None
    if tmdb is True:
        tmdb = {"enabled": True}
    if not isinstance(tmdb, dict) or not tmdb.get("enabled", True):
        return "", None, None

    fetcher = TmdbFetcher(
        api_key=str(tmdb.get("api_key") or config.tmdb_api_key or ""),
        bearer_token=str(tmdb.get("bearer_token") or config.tmdb_bearer_token or ""),
        language=str(tmdb.get("language") or "fr-FR"),
    )
    kind = str(tmdb.get("kind") or "all")
    season = str(tmdb.get("season") or "")
    episode = str(tmdb.get("episode") or "")
    tmdb_id = tmdb.get("id", tmdb.get("tmdb_id"))
    if tmdb_id:
        title = str(tmdb.get("title") or clean_filename_for_search(first_source) or first_source.stem)
        from core.media_info_fetcher import MediaSearchResult

        result = MediaSearchResult(
            tmdb_id=int(tmdb_id),
            title=title,
            year=str(tmdb.get("year") or ""),
            kind="movie" if kind not in {"movie", "tv"} else kind,
        )
    else:
        query = str(tmdb.get("query") or clean_filename_for_search(first_source) or first_source.stem)
        year = str(tmdb.get("year") or extract_year_from_filename(first_source) or "")
        results = fetcher.search(query, kind=kind, year=year)
        if not results:
            raise CliError(f"Aucun résultat TMDB pour : {query}", EXIT_VALIDATION)
        result = results[0]
        logger.emit("info", f"TMDB premier résultat retenu : {result.title} ({result.year}) #{result.tmdb_id}")

    details = fetcher.get_details(result, season=season, episode=episode)
    title = details.formatted_container_title()
    cover = (details.cover_url, details.cover_filename) if details.cover_url and details.cover_filename else None
    return title, details.to_mkv_tags(), cover


def build_remux_config(
    job: dict[str, Any],
    config: AppConfig,
    options: CommonOptions,
    logger: Logger,
    *,
    cli_inputs: list[str] | None = None,
    cli_output: str | None = None,
) -> RemuxConfig:
    validate_job_contract(job, require_version=False)
    sources, infos, tracks = inspect_sources(job, config, options, logger, cli_inputs=cli_inputs)
    tracks = apply_track_rules(tracks, job.get("rules", {}))
    strict_selectors = int(job.get("version", 1) or 1) == 2 or str(job.get("kind") or "") == "exact-job"
    apply_audio_variants(job, sources, tracks, strict_selectors=strict_selectors)
    apply_explicit_track_edits(job, tracks, strict_selectors=strict_selectors)

    output_raw = cli_output or job.get("output")
    if not output_raw:
        raise CliError("Une sortie est requise (`--output` ou `output` JSON).", EXIT_ARGS)
    output = Path(str(output_raw)).expanduser()

    keep_chapters, chapter_overrides, chapter_source_index = chapter_entries(job, infos)
    tmdb_title = ""
    tmdb_tags = None
    tmdb_cover = None
    try:
        tmdb_title, tmdb_tags, tmdb_cover = _resolve_tmdb(job, config, sources[0].path, logger)
    except TmdbError as exc:
        raise CliError(str(exc), EXIT_VALIDATION) from exc

    tag_overrides = job.get("tag_overrides", None)
    if tmdb_tags:
        tag_overrides = deep_merge(tmdb_tags, tag_overrides if isinstance(tag_overrides, dict) else {})

    work_dir = Path(str(options.work_dir or job.get("work_dir") or config.work_dir)).expanduser()
    return RemuxConfig(
        sources=sources,
        output=output,
        track_order=track_order(job, tracks, strict_selectors=strict_selectors),
        keep_chapters=keep_chapters,
        chapter_overrides=chapter_overrides,
        chapter_source_index=chapter_source_index,
        extra_attachments=[Path(str(p)).expanduser() for p in job.get("extra_attachments", [])],
        work_dir=work_dir,
        file_title=str(job.get("file_title") or tmdb_title or ""),
        tag_overrides=tag_overrides if isinstance(tag_overrides, dict) else None,
        tmdb_cover=tmdb_cover,
        allow_missing_output_dir=bool(job.get("_allow_missing_output_dir", False)),
    )


def config_to_template(job: dict[str, Any], *, include_output: bool = False) -> dict[str, Any]:
    template = {
        "version": 1,
        "kind": "exact-job",
        "chapters": job.get("chapters", {}),
        "tmdb": job.get("tmdb", False),
        "extra_attachments": job.get("extra_attachments", []),
        "tag_overrides": job.get("tag_overrides", None),
    }
    if include_output and job.get("output"):
        template["output"] = job["output"]
    return {k: v for k, v in template.items() if v not in ({}, None, False, [])}
