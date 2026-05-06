"""Headless remux CLI for Mediarecode."""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer

from core.config import AppConfig
from core.inspector import (
    AttachmentInfo,
    ChapterEntry,
    FileInfo,
    FileInspector,
    fmt_timecode_display,
)
from core.lang_tags import Rfc5646LanguageTags
from core.media_info_fetcher import (
    TmdbError,
    TmdbFetcher,
    clean_filename_for_search,
    extract_year_from_filename,
)
from core.runner import ToolNotFoundError
from core.workflows.remux import RemuxWorkflow
from core.workflows.remux_models import RemuxConfig, SourceInput, TrackEntry, tracks_from_file_info


EXIT_OK = 0
EXIT_ARGS = 2
EXIT_VALIDATION = 3
EXIT_TOOL = 4
EXIT_EXISTS = 5
EXIT_WORKFLOW = 6
EXIT_PARTIAL = 7

TRACK_TYPES = {"video", "audio", "subtitle"}
FLAG_NAMES = (
    "enabled",
    "default",
    "forced",
    "hearing_impaired",
    "visual_impaired",
    "original",
    "commentary",
)


class CliError(RuntimeError):
    def __init__(self, message: str, exit_code: int = EXIT_WORKFLOW) -> None:
        self.exit_code = exit_code
        super().__init__(message)


class Logger:
    def __init__(self, *, fmt: str = "text", stream=None) -> None:
        self.fmt = fmt
        self.stream = stream or sys.stderr

    def emit(self, level: str, message: str, **fields: Any) -> None:
        if self.fmt == "jsonl":
            payload = {"level": level.lower(), "message": message, **fields}
            print(json.dumps(payload, ensure_ascii=False, default=_json_default), file=self.stream)
            return
        print(f"[{level.upper()}] {message}", file=self.stream)

    def workflow_log(self, level: str, message: str) -> None:
        self.emit(level, message)


def _ensure_qcore_app(argv: list[str] | None = None) -> QCoreApplication:
    app = QCoreApplication.instance()
    if isinstance(app, QCoreApplication):
        return app
    return QCoreApplication(argv or [sys.argv[0]])


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.name
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CliError(f"JSON introuvable : {path}", EXIT_ARGS) from exc
    except json.JSONDecodeError as exc:
        raise CliError(f"JSON invalide {path}:{exc.lineno}:{exc.colno} : {exc.msg}", EXIT_ARGS) from exc
    if not isinstance(data, dict):
        raise CliError("Le fichier JSON racine doit être un objet.", EXIT_ARGS)
    return data


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default) + "\n",
        encoding="utf-8",
    )


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if (
            key in merged
            and isinstance(merged[key], dict)
            and isinstance(value, dict)
        ):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _csv_values(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _normalize_lang(tag: str | None, title: str | None = None) -> str:
    if not tag:
        return ""
    normalized = Rfc5646LanguageTags.regionalize_track_language(str(tag), title)
    if normalized:
        return normalized
    canonical = Rfc5646LanguageTags.normalize(str(tag))
    return canonical or str(tag).strip()


def _lang_name(tag: str) -> str:
    if not tag:
        return ""
    canonical = Rfc5646LanguageTags.normalize(tag) or tag
    if canonical in Rfc5646LanguageTags.TAGS:
        return Rfc5646LanguageTags.TAGS[canonical].split(" (", 1)[0]
    base = canonical.split("-", 1)[0]
    return Rfc5646LanguageTags.TAGS.get(base, canonical).split(" (", 1)[0]


def _flag_value(track: TrackEntry, flag: str, *, original: bool = False) -> bool:
    attr = f"{'orig_' if original else ''}flag_{flag}"
    if flag == "enabled":
        attr = f"{'orig_' if original else ''}flag_enabled"
    return bool(getattr(track, attr, False))


def _channels_from_display(display_info: str) -> str:
    text = str(display_info or "")
    match = re.search(r"\b(?:mono|stereo|[1-9](?:\.[0-9])?)\b", text, flags=re.IGNORECASE)
    return match.group(0) if match else ""


def _audio_object_from_display(display_info: str) -> str:
    text = str(display_info or "").lower()
    if "atmos" in text:
        return "Atmos"
    if "dts:x" in text or "dtsx" in text:
        return "DTS:X"
    return ""


def _flag_label(track: TrackEntry, flag: str) -> str:
    labels = {
        "default": "Default",
        "forced": "Forced",
        "hearing_impaired": "Malentendant",
        "visual_impaired": "Malvoyant",
        "original": "Original",
        "commentary": "Commentaire",
        "enabled": "Enabled",
    }
    return labels[flag] if _flag_value(track, flag) else ""


def _render_pattern(pattern: str, track: TrackEntry) -> str:
    lang = track.language or ""
    values = {
        "lang": lang,
        "Lang": lang,
        "langname": _lang_name(lang),
        "LangName": _lang_name(lang),
        "codec": track.codec,
        "Codec": track.codec,
        "canaux": _channels_from_display(track.display_info),
        "channels": _channels_from_display(track.display_info),
        "atmos": _audio_object_from_display(track.display_info),
        "audio_object": _audio_object_from_display(track.display_info),
        "title": track.orig_title or track.title,
        "source_title": track.orig_title,
        "type": track.track_type,
        "tag_default": _flag_label(track, "default"),
        "tag_forced": _flag_label(track, "forced"),
        "tag_malentendant": _flag_label(track, "hearing_impaired"),
        "tag_malvoyant": _flag_label(track, "visual_impaired"),
        "tag_original": _flag_label(track, "original"),
        "tag_commentary": _flag_label(track, "commentary"),
        "flags": track.flags_label,
    }
    rendered = pattern
    for key, value in values.items():
        rendered = rendered.replace("{" + key + "}", str(value or ""))
        rendered = rendered.replace("<" + key + ">", str(value or ""))
    return " ".join(rendered.split()).strip()


def _track_rule(rules: dict[str, Any], track_type: str) -> dict[str, Any]:
    tracks = rules.get("tracks", {})
    if not isinstance(tracks, dict):
        return {}
    specific = tracks.get(track_type, {})
    return specific if isinstance(specific, dict) else {}


def _matches_flag_filters(track: TrackEntry, rule: dict[str, Any]) -> bool:
    filters = rule.get("flags")
    if not isinstance(filters, dict):
        return True
    for name, expected in filters.items():
        if name not in FLAG_NAMES:
            continue
        if _flag_value(track, name, original=True) != bool(expected):
            return False
    return True


def apply_track_rules(tracks: list[TrackEntry], rules: dict[str, Any]) -> list[TrackEntry]:
    if not isinstance(rules, dict):
        rules = {}
    rename_patterns = rules.get("rename_patterns", {})
    if not isinstance(rename_patterns, dict):
        rename_patterns = {}
    normalize_languages = bool(rules.get("normalize_languages", True))

    out: list[TrackEntry] = []
    for track in tracks:
        rule = _track_rule(rules, track.track_type)
        include = bool(rule.get("include", True))
        allowed_languages = [
            _normalize_lang(lang)
            for lang in rule.get("languages", [])
            if str(lang).strip()
        ]
        if allowed_languages:
            track_lang = _normalize_lang(track.language, track.title)
            include = include and track_lang in allowed_languages
        include = include and _matches_flag_filters(track, rule)

        track.enabled = include
        if normalize_languages:
            track.language = _normalize_lang(track.language, track.title)

        pattern = str(rule.get("rename_pattern") or rename_patterns.get(track.track_type) or "").strip()
        if pattern:
            track.title = _render_pattern(pattern, track)
        out.append(track)
    return out


def _source_path_items(job: dict[str, Any], cli_inputs: list[str] | None = None) -> list[dict[str, Any]]:
    if cli_inputs:
        return [{"path": value} for value in cli_inputs]
    raw_sources = job.get("sources")
    if raw_sources is None and job.get("input"):
        raw_sources = [job["input"]]
    if isinstance(raw_sources, (str, Path)):
        raw_sources = [raw_sources]
    if not isinstance(raw_sources, list) or not raw_sources:
        raise CliError("Au moins une source est requise.", EXIT_ARGS)
    items: list[dict[str, Any]] = []
    for item in raw_sources:
        if isinstance(item, dict):
            if not item.get("path"):
                raise CliError("Chaque source JSON doit contenir `path`.", EXIT_ARGS)
            items.append(item)
        else:
            items.append({"path": item})
    return items


def _inspect_sources(
    job: dict[str, Any],
    config: AppConfig,
    args: argparse.Namespace,
    logger: Logger,
    *,
    cli_inputs: list[str] | None = None,
) -> tuple[list[SourceInput], list[FileInfo], list[TrackEntry]]:
    ffprobe = args.ffprobe or job.get("ffprobe") or config.tool_ffprobe
    mediainfo = args.mediainfo or job.get("mediainfo") or config.tool_mediainfo
    inspector = FileInspector(
        ffprobe_bin=str(ffprobe),
        mediainfo_bin=str(mediainfo),
        verbose_output=(lambda line: logger.emit("debug", line)) if getattr(args, "verbose", False) else None,
    )
    source_items = _source_path_items(job, cli_inputs)
    sources: list[SourceInput] = []
    infos: list[FileInfo] = []
    all_tracks: list[TrackEntry] = []

    for source_index, item in enumerate(source_items):
        path = Path(str(item["path"])).expanduser()
        if not path.exists():
            raise CliError(f"Source introuvable : {path}", EXIT_VALIDATION)
        info = inspector.inspect(path)
        file_id = f"src{source_index}"
        tracks = tracks_from_file_info(info, file_id=file_id)
        infos.append(info)
        attachment_selection = _selected_attachments(info, item)
        source = SourceInput(
            path=path,
            file_index=source_index,
            tracks=tracks,
            selected_attachments=attachment_selection,
            attachment_count=len(info.attachments),
            copy_tags=bool(item.get("copy_tags", False)),
            has_chapters=bool(info.chapters and info.chapters.entries),
        )
        sources.append(source)
        all_tracks.extend(tracks)
    return sources, infos, all_tracks


def _selected_attachments(info: FileInfo, item: dict[str, Any]) -> list[AttachmentInfo]:
    selection = item.get("attachments", "none")
    if selection is True or selection == "all":
        return list(info.attachments)
    if selection in (False, None, "none"):
        return []
    names: set[str] = set()
    indices: set[int] = set()
    if isinstance(selection, list):
        for entry in selection:
            if isinstance(entry, int):
                indices.add(entry)
            else:
                names.add(str(entry))
    return [
        att
        for att in info.attachments
        if att.local_index in indices or att.index in indices or att.filename in names
    ]


def _apply_explicit_track_edits(job: dict[str, Any], tracks: list[TrackEntry]) -> None:
    specs = job.get("tracks", [])
    if not isinstance(specs, list):
        return
    lookup = {(t.file_id, t.mkv_tid): t for t in tracks}
    for spec in specs:
        if not isinstance(spec, dict):
            continue
        source_index = int(spec.get("source", spec.get("source_index", 0)))
        file_id = f"src{source_index}"
        tid = spec.get("id", spec.get("mkv_tid", spec.get("stream")))
        if tid is None:
            continue
        track = lookup.get((file_id, int(tid)))
        if track is None:
            continue
        if "enabled" in spec:
            track.enabled = bool(spec["enabled"])
        if "language" in spec:
            track.language = _normalize_lang(str(spec["language"]), track.title)
        if "title" in spec:
            track.title = str(spec["title"])
        flags = spec.get("flags")
        if isinstance(flags, dict):
            for name, value in flags.items():
                if name in FLAG_NAMES:
                    setattr(track, f"flag_{name}", bool(value))
        if "time_shift_ms" in spec:
            track.time_shift_ms = int(spec["time_shift_ms"])


def _track_order(job: dict[str, Any], tracks: list[TrackEntry]) -> list[tuple[int, int, str]]:
    explicit = job.get("track_order")
    if isinstance(explicit, list):
        order: list[tuple[int, int, str]] = []
        by_key = {(t.file_id, t.mkv_tid): t for t in tracks}
        for item in explicit:
            if isinstance(item, dict):
                source_index = int(item.get("source", item.get("source_index", 0)))
                tid = int(item.get("id", item.get("mkv_tid", item.get("stream"))))
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


def _parse_timecode(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", ".")
    if not text:
        raise ValueError("timecode vide")
    parts = text.split(":")
    if len(parts) == 1:
        return float(parts[0])
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    raise ValueError(f"timecode invalide: {value}")


def _parse_chapters_json(path: Path) -> list[ChapterEntry]:
    data = _load_json(path)
    raw_entries = data.get("chapters", data.get("entries", []))
    if not isinstance(raw_entries, list):
        raise CliError(f"Chapitres JSON invalides : {path}", EXIT_ARGS)
    return [_chapter_from_mapping(item) for item in raw_entries if isinstance(item, dict)]


def _parse_chapters_ffmetadata(path: Path) -> list[ChapterEntry]:
    entries: list[ChapterEntry] = []
    current: dict[str, str] = {}
    in_chapter = False
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped == "[CHAPTER]":
            if current:
                entries.append(_chapter_from_ffmetadata(current))
            current = {}
            in_chapter = True
            continue
        if not in_chapter or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        current[key.strip().upper()] = value.strip()
    if current:
        entries.append(_chapter_from_ffmetadata(current))
    return entries


def _chapter_from_ffmetadata(data: dict[str, str]) -> ChapterEntry:
    timebase = data.get("TIMEBASE", "1/1000")
    start = float(data.get("START", "0"))
    if timebase == "1/1000":
        seconds = start / 1000
    else:
        try:
            num, den = timebase.split("/", 1)
            seconds = start * float(num) / float(den)
        except Exception:
            seconds = start / 1000
    return ChapterEntry(timecode_s=seconds, name=data.get("TITLE", ""))


def _parse_chapters_ogm(path: Path) -> list[ChapterEntry]:
    times: dict[int, str] = {}
    names: dict[int, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        match = re.fullmatch(r"CHAPTER(\d{2,})", key.strip(), flags=re.IGNORECASE)
        if match:
            times[int(match.group(1))] = value.strip()
            continue
        match = re.fullmatch(r"CHAPTER(\d{2,})NAME", key.strip(), flags=re.IGNORECASE)
        if match:
            names[int(match.group(1))] = value.strip()
    return [
        ChapterEntry(timecode_s=_parse_timecode(times[index]), name=names.get(index, ""))
        for index in sorted(times)
    ]


def _chapter_from_mapping(item: dict[str, Any]) -> ChapterEntry:
    raw_time = item.get("timestamp", item.get("timecode", item.get("time", item.get("timecode_s"))))
    if raw_time is None:
        raise CliError("Chapitre sans timestamp/timecode.", EXIT_ARGS)
    name = item.get("chaptername", item.get("name", item.get("title", "")))
    return ChapterEntry(timecode_s=_parse_timecode(raw_time), name=str(name or ""))


def _chapter_entries(job: dict[str, Any], infos: list[FileInfo]) -> tuple[bool, list[ChapterEntry] | None, int | None]:
    chapters = job.get("chapters", {})
    if chapters is False:
        return False, None, None
    if not isinstance(chapters, dict):
        return True, None, None

    source_index = chapters.get("source_index")
    selected_source = int(source_index) if source_index is not None else None
    entries: list[ChapterEntry] = []
    if bool(chapters.get("include_source", False)):
        idx = selected_source if selected_source is not None else 0
        if 0 <= idx < len(infos) and infos[idx].chapters is not None:
            entries.extend(infos[idx].chapters.entries)

    import_path = chapters.get("import")
    if import_path:
        path = Path(str(import_path)).expanduser()
        suffix = path.suffix.lower()
        if suffix == ".json":
            entries.extend(_parse_chapters_json(path))
        elif suffix in {".txt", ".ogm"}:
            entries.extend(_parse_chapters_ogm(path))
        else:
            entries.extend(_parse_chapters_ffmetadata(path))

    additions = chapters.get("add", [])
    if isinstance(additions, list):
        entries.extend(_chapter_from_mapping(item) for item in additions if isinstance(item, dict))

    entries = sorted(entries, key=lambda entry: entry.timecode_s)
    return True, entries if entries else None, selected_source


def _resolve_tmdb(job: dict[str, Any], config: AppConfig, first_source: Path, logger: Logger) -> tuple[str, dict[str, str] | None, tuple[str, str] | None]:
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
        result = fetcher.search(title, kind=kind, year=str(tmdb.get("year") or ""))[0] if False else None
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
    args: argparse.Namespace,
    logger: Logger,
    *,
    cli_inputs: list[str] | None = None,
    cli_output: str | None = None,
) -> RemuxConfig:
    sources, infos, tracks = _inspect_sources(job, config, args, logger, cli_inputs=cli_inputs)
    tracks = apply_track_rules(tracks, job.get("rules", {}))
    _apply_explicit_track_edits(job, tracks)

    output_raw = cli_output or getattr(args, "output", None) or job.get("output")
    if not output_raw:
        raise CliError("Une sortie est requise (`--output` ou `output` JSON).", EXIT_ARGS)
    output = Path(str(output_raw)).expanduser()

    keep_chapters, chapter_overrides, chapter_source_index = _chapter_entries(job, infos)
    tmdb_title = ""
    tmdb_tags = None
    tmdb_cover = None
    try:
        tmdb_title, tmdb_tags, tmdb_cover = _resolve_tmdb(job, config, sources[0].path, logger)
    except TmdbError as exc:
        raise CliError(str(exc), EXIT_VALIDATION) from exc

    tag_overrides = job.get("tag_overrides", None)
    if tmdb_tags:
        tag_overrides = _deep_merge(tmdb_tags, tag_overrides if isinstance(tag_overrides, dict) else {})

    work_dir = Path(str(getattr(args, "work_dir", None) or job.get("work_dir") or config.work_dir)).expanduser()
    return RemuxConfig(
        sources=sources,
        output=output,
        track_order=_track_order(job, tracks),
        keep_chapters=keep_chapters,
        chapter_overrides=chapter_overrides,
        chapter_source_index=chapter_source_index,
        extra_attachments=[Path(str(p)).expanduser() for p in job.get("extra_attachments", [])],
        work_dir=work_dir,
        file_title=str(job.get("file_title") or tmdb_title or ""),
        tag_overrides=tag_overrides if isinstance(tag_overrides, dict) else None,
        tmdb_cover=tmdb_cover,
    )


def _workflow(config: AppConfig, args: argparse.Namespace, logger: Logger) -> RemuxWorkflow:
    return RemuxWorkflow(
        ffmpeg_bin=str(args.ffmpeg or config.tool_ffmpeg),
        ffprobe_bin=str(args.ffprobe or config.tool_ffprobe),
        ffmpeg_threads=args.threads if args.threads is not None else config.ffmpeg_threads,
        writing_application=str(getattr(args, "writing_application", "") or ""),
        generate_nfo=config.generate_nfo if getattr(args, "nfo", None) is None else bool(args.nfo),
        mediainfo_bin=str(args.mediainfo or config.tool_mediainfo),
    )


def _config_to_template(job: dict[str, Any], *, include_output: bool = False) -> dict[str, Any]:
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


def _template_from_info(info: FileInfo, *, output: str = "") -> dict[str, Any]:
    return {
        "version": 1,
        "sources": [{"path": str(info.path), "attachments": "none", "copy_tags": False}],
        "output": output or str(info.path.with_suffix(".remux.mkv")),
        "rules": {
            "normalize_languages": True,
            "tracks": {
                "video": {"include": True},
                "audio": {"include": True},
                "subtitle": {"include": True},
            },
            "rename_patterns": {},
        },
        "chapters": {"source_index": 0},
    }


def serialize_file_info(info: FileInfo) -> dict[str, Any]:
    def track_payload(track: Any, track_type: str) -> dict[str, Any]:
        payload = asdict(track)
        payload.pop("raw", None)
        payload["type"] = track_type
        if hasattr(track, "hdr_type"):
            payload["hdr_type"] = track.hdr_type.label()
        return payload

    return {
        "path": str(info.path),
        "format": info.format,
        "duration_s": info.duration_s,
        "duration": info.duration_human,
        "size_bytes": info.size_bytes,
        "size": info.size_human,
        "bit_rate": info.bit_rate,
        "title": info.title,
        "hdr_type": info.hdr_type.label(),
        "frame_count": info.frame_count,
        "tag_count": info.tag_count,
        "global_tags": info.global_tags,
        "tracks": [
            *(track_payload(track, "video") for track in info.video_tracks),
            *(track_payload(track, "audio") for track in info.audio_tracks),
            *(track_payload(track, "subtitle") for track in info.subtitle_tracks),
        ],
        "attachments": [asdict(att) for att in info.attachments],
        "chapters": [
            {"timecode_s": ch.timecode_s, "timecode": fmt_timecode_display(ch.timecode_s), "name": ch.name}
            for ch in (info.chapters.entries if info.chapters else [])
        ],
    }


def _load_job(args: argparse.Namespace) -> dict[str, Any]:
    job: dict[str, Any] = {}
    if getattr(args, "config", None):
        job = _deep_merge(job, _load_json(Path(args.config).expanduser()))
    if getattr(args, "template", None):
        job = _deep_merge(_load_json(Path(args.template).expanduser()), job)
    if getattr(args, "input", None):
        job["sources"] = [{"path": item} for item in args.input]
    if getattr(args, "output", None):
        job["output"] = args.output
    if getattr(args, "languages", None):
        langs = _csv_values(args.languages)
        job.setdefault("rules", {}).setdefault("tracks", {}).setdefault("audio", {})["languages"] = langs
        job.setdefault("rules", {}).setdefault("tracks", {}).setdefault("subtitle", {})["languages"] = langs
    if getattr(args, "tmdb", False):
        tmdb = job.setdefault("tmdb", {})
        if not isinstance(tmdb, dict):
            tmdb = {"enabled": True}
            job["tmdb"] = tmdb
        tmdb["enabled"] = True
    if getattr(args, "tmdb_id", None):
        tmdb = job.setdefault("tmdb", {})
        if not isinstance(tmdb, dict):
            tmdb = {"enabled": True}
            job["tmdb"] = tmdb
        tmdb["enabled"] = True
        tmdb["id"] = args.tmdb_id
    return job


def cmd_inspect(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    ffprobe = args.ffprobe or config.tool_ffprobe
    mediainfo = args.mediainfo or config.tool_mediainfo
    inspector = FileInspector(ffprobe_bin=str(ffprobe), mediainfo_bin=str(mediainfo))
    infos = [inspector.inspect(Path(value).expanduser()) for value in args.input]
    if args.config_template:
        payload = _template_from_info(infos[0], output=args.output or "")
    else:
        payload = {"files": [serialize_file_info(info) for info in infos]}
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))
    return EXIT_OK


def cmd_validate(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    job = _load_job(args)
    remux_config = build_remux_config(job, config, args, logger)
    errors = _workflow(config, args, logger).validate(remux_config)
    if errors:
        for error in errors:
            logger.emit("error", error)
        return EXIT_VALIDATION
    logger.emit("info", "Configuration valide.")
    return EXIT_OK


def cmd_preview(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    job = _load_job(args)
    remux_config = build_remux_config(job, config, args, logger)
    errors = _workflow(config, args, logger).validate(remux_config)
    if errors:
        for error in errors:
            logger.emit("error", error)
        return EXIT_VALIDATION
    print(_workflow(config, args, logger).preview_command(remux_config))
    return EXIT_OK


def _run_remux_config(args: argparse.Namespace, config: AppConfig, logger: Logger, remux_config: RemuxConfig) -> int:
    if remux_config.output.exists() and not args.force:
        raise CliError(f"Sortie déjà existante : {remux_config.output} (utiliser --force)", EXIT_EXISTS)
    wf = _workflow(config, args, logger)
    wf.log_message.connect(logger.workflow_log)
    signals = wf.run(remux_config)
    loop = QEventLoop()
    state: dict[str, Any] = {"exit": EXIT_OK, "error": ""}

    def done(message: str = "") -> None:
        if message:
            logger.emit("info", message)
        state["exit"] = EXIT_OK
        loop.quit()

    def failed(message: str, exc: object) -> None:
        logger.emit("error", message, exception=repr(exc))
        state["exit"] = EXIT_WORKFLOW
        state["error"] = message
        loop.quit()

    def cancelled() -> None:
        logger.emit("error", "Opération annulée.")
        state["exit"] = EXIT_WORKFLOW
        loop.quit()

    signals.progress.connect(lambda line: logger.emit("info", line))
    signals.finished.connect(done)
    signals.failed.connect(failed)
    signals.cancelled.connect(cancelled)

    stop = threading.Event()

    def _watch_stdin() -> None:
        # Réservé pour annulation future; garde le CLI non interactif.
        stop.wait()

    watcher = threading.Thread(target=_watch_stdin, daemon=True)
    watcher.start()
    try:
        loop.exec()
    finally:
        stop.set()
    return int(state["exit"])


def cmd_remux(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    job = _load_job(args)
    if args.save:
        _write_json(Path(args.save).expanduser(), _config_to_template(job))
        logger.emit("info", f"Template sauvegardé : {args.save}")
    remux_config = build_remux_config(job, config, args, logger)
    return _run_remux_config(args, config, logger, remux_config)


def _batch_jobs(batch: dict[str, Any]) -> list[dict[str, Any]]:
    raw_jobs = batch.get("jobs", batch.get("inputs", []))
    if not isinstance(raw_jobs, list) or not raw_jobs:
        raise CliError("Le batch doit contenir `jobs` ou `inputs`.", EXIT_ARGS)
    jobs: list[dict[str, Any]] = []
    for item in raw_jobs:
        if isinstance(item, dict):
            jobs.append(item)
        else:
            jobs.append({"sources": [str(item)]})
    return jobs


def cmd_batch(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    template = _load_json(Path(args.template).expanduser())
    batch = _load_json(Path(args.batch).expanduser()) if args.batch else {"inputs": args.input or []}
    failures = 0
    total = 0
    for item in _batch_jobs(batch):
        total += 1
        job = _deep_merge(template, item)
        if "output" not in job and args.output_dir:
            first = _source_path_items(job)[0]["path"]
            job["output"] = str(Path(args.output_dir).expanduser() / (Path(str(first)).stem + ".mkv"))
        try:
            remux_config = build_remux_config(job, config, args, logger)
            rc = _run_remux_config(args, config, logger, remux_config)
            if rc != EXIT_OK:
                failures += 1
        except Exception as exc:
            failures += 1
            logger.emit("error", f"Job batch échoué : {exc}")
            if not args.continue_on_error:
                break
    logger.emit("info", f"Batch terminé : {total - failures}/{total} succès.")
    return EXIT_OK if failures == 0 else EXIT_PARTIAL


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", help="Fichier JSON job/template.")
    parser.add_argument("--ffmpeg", help="Chemin ffmpeg override.")
    parser.add_argument("--ffprobe", help="Chemin ffprobe override.")
    parser.add_argument("--mediainfo", help="Chemin mediainfo override.")
    parser.add_argument("--work-dir", help="Répertoire de travail override.")
    parser.add_argument("--threads", type=int, help="Nombre de threads ffmpeg.")
    parser.add_argument("--log-format", choices=("text", "jsonl"), default="text")
    parser.add_argument("--verbose", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mediarecode-cli", description="Mediarecode headless CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    inspect = sub.add_parser("inspect", help="Inspecter une ou plusieurs sources.")
    _add_common_options(inspect)
    inspect.add_argument("input", nargs="+")
    inspect.add_argument("--config-template", action="store_true")
    inspect.add_argument("--output")
    inspect.set_defaults(func=cmd_inspect)

    for name, help_text, func in (
        ("validate", "Valider une config remux.", cmd_validate),
        ("preview", "Afficher la commande ffmpeg prévue.", cmd_preview),
        ("remux", "Exécuter un remux.", cmd_remux),
    ):
        p = sub.add_parser(name, help=help_text)
        _add_common_options(p)
        p.add_argument("-i", "--input", action="append")
        p.add_argument("-o", "--output")
        p.add_argument("--languages", help="Langues audio/sous-titres autorisées, séparées par virgules.")
        p.add_argument("--tmdb", action="store_true", help="Activer TMDB; premier résultat si pas d'ID.")
        p.add_argument("--tmdb-id", type=int, help="ID TMDB explicite.")
        p.add_argument("--force", action="store_true", help="Autoriser l'écrasement de la sortie.")
        p.add_argument("--nfo", dest="nfo", action="store_true", default=None)
        p.add_argument("--no-nfo", dest="nfo", action="store_false")
        p.add_argument("--writing-application", default="")
        if name == "remux":
            p.add_argument("--save", help="Sauvegarder les options/règles en template réutilisable.")
        p.set_defaults(func=func)

    batch = sub.add_parser("batch", help="Appliquer un template à plusieurs jobs.")
    _add_common_options(batch)
    batch.add_argument("--template", required=True)
    batch.add_argument("--batch", help="JSON contenant `jobs` ou `inputs`.")
    batch.add_argument("-i", "--input", action="append", help="Entrée batch simple; répétable.")
    batch.add_argument("--output-dir")
    batch.add_argument("--force", action="store_true")
    batch.add_argument("--continue-on-error", action="store_true")
    batch.add_argument("--nfo", dest="nfo", action="store_true", default=None)
    batch.add_argument("--no-nfo", dest="nfo", action="store_false")
    batch.add_argument("--writing-application", default="")
    batch.set_defaults(func=cmd_batch)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    _ensure_qcore_app([sys.argv[0], *argv])
    parser = build_parser()
    args = parser.parse_args(argv)
    logger = Logger(fmt=args.log_format)
    try:
        config = AppConfig()
        return int(args.func(args, config, logger))
    except ToolNotFoundError as exc:
        logger.emit("error", str(exc))
        return EXIT_TOOL
    except CliError as exc:
        logger.emit("error", str(exc))
        return exc.exit_code
    except KeyboardInterrupt:
        logger.emit("error", "Interrompu.")
        return EXIT_WORKFLOW
    except Exception as exc:
        logger.emit("error", str(exc), exception=repr(exc))
        return EXIT_WORKFLOW


if __name__ == "__main__":
    raise SystemExit(main())
