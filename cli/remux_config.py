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
from core.workflows.remux_models import RemuxConfig, TrackEntry

from cli.chapters import chapter_entries
from cli.constants import EXIT_ARGS, EXIT_VALIDATION, FLAG_NAMES
from cli.contract import validate_job_contract
from cli.errors import CliError
from cli.inspection import inspect_sources
from cli.json_io import deep_merge
from cli.logging import Logger
from cli.options import CommonOptions
from cli.rules import apply_track_rules, normalize_lang


def apply_explicit_track_edits(job: dict[str, Any], tracks: list[TrackEntry]) -> None:
    specs = job.get("tracks", [])
    if not isinstance(specs, list):
        return
    lookup = {(t.file_id, t.mkv_tid): t for t in tracks}
    for spec in specs:
        if not isinstance(spec, dict):
            continue
        raw_source = spec.get("source", spec.get("source_index", 0))
        source_index = int(0 if raw_source is None else raw_source)
        file_id = f"src{source_index}"
        tid = spec.get("id", spec.get("mkv_tid", spec.get("stream")))
        if tid is None:
            continue
        track_id = int(tid)
        track = lookup.get((file_id, track_id))
        if track is None:
            continue
        if "enabled" in spec:
            track.enabled = bool(spec["enabled"])
        if "language" in spec:
            track.language = normalize_lang(str(spec["language"]), track.title)
        if "title" in spec:
            track.title = str(spec["title"])
        flags = spec.get("flags")
        if isinstance(flags, dict):
            for name, value in flags.items():
                if name in FLAG_NAMES:
                    setattr(track, f"flag_{name}", bool(value))
        if "time_shift_ms" in spec:
            track.time_shift_ms = int(spec["time_shift_ms"])


def track_order(job: dict[str, Any], tracks: list[TrackEntry]) -> list[tuple[int, int, str]]:
    explicit = job.get("track_order")
    if isinstance(explicit, list):
        order: list[tuple[int, int, str]] = []
        by_key = {(t.file_id, t.mkv_tid): t for t in tracks}
        for item in explicit:
            if isinstance(item, dict):
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
    apply_explicit_track_edits(job, tracks)

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
        track_order=track_order(job, tracks),
        keep_chapters=keep_chapters,
        chapter_overrides=chapter_overrides,
        chapter_source_index=chapter_source_index,
        extra_attachments=[Path(str(p)).expanduser() for p in job.get("extra_attachments", [])],
        work_dir=work_dir,
        file_title=str(job.get("file_title") or tmdb_title or ""),
        tag_overrides=tag_overrides if isinstance(tag_overrides, dict) else None,
        tmdb_cover=tmdb_cover,
    )


def config_to_template(job: dict[str, Any], *, include_output: bool = False) -> dict[str, Any]:
    template = {
        "version": 1,
        "rules": job.get("rules", {}),
        "chapters": job.get("chapters", {}),
        "tmdb": job.get("tmdb", False),
        "extra_attachments": job.get("extra_attachments", []),
        "tag_overrides": job.get("tag_overrides", None),
    }
    if include_output and job.get("output"):
        template["output"] = job["output"]
    return {k: v for k, v in template.items() if v not in ({}, None, False, [])}
