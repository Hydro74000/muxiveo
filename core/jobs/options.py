"""Options typées d'un job (CLI, service, Hybrid) ; ``from_namespace`` lit tout objet à attributs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.config import AppConfig

from core.jobs.logging import JobLogger as Logger


@dataclass(frozen=True)
class CommonOptions:
    ffmpeg: str | None = None
    ffprobe: str | None = None
    mediainfo: str | None = None
    work_dir: str | None = None
    threads: int | None = None
    log_format: str = "text"
    verbose: bool = False
    nfo: bool | None = None
    writing_application: str = ""
    export_workflow: str | None = None
    export_directory: bool = False
    auto_forced_subs: bool = False
    auto_sdh: bool = False
    remove_closed_captions: bool | None = None
    forced_threshold: int = 50
    sync_mode: str | None = None
    sync_subtitles: str | None = None
    clean_nfo: bool | None = None
    crossfade_ms: int | None = None
    auto_sync: bool = False
    #: Chaque source ≥ 1 sans ``sources[i].sync`` mesurée face à la source 0.
    auto_sync_all: bool = False
    calibration: str | None = None
    detect_cuts: bool = False
    quick_sync_check: bool | None = None
    reject_coherent_sync: bool = False
    drift_threshold_ms: int = 25
    cadence_auto: bool = True
    cadence_method: str = "auto"
    #: Encodage en échec : dossier de travail conservé (fichiers produits) au lieu d'être supprimé.
    keep_failed_work_dir: bool = False

    @classmethod
    def from_namespace(cls, args: Any) -> "CommonOptions":
        return cls(
            ffmpeg=getattr(args, "ffmpeg", None),
            ffprobe=getattr(args, "ffprobe", None),
            mediainfo=getattr(args, "mediainfo", None),
            work_dir=getattr(args, "work_dir", None),
            threads=getattr(args, "threads", None),
            log_format=getattr(args, "log_format", "text"),
            verbose=bool(getattr(args, "verbose", False)),
            nfo=getattr(args, "nfo", None),
            writing_application=str(getattr(args, "writing_application", "") or ""),
            export_workflow=getattr(args, "export_workflow", None),
            auto_forced_subs=bool(getattr(args, "auto_forced_subs", False)),
            auto_sdh=bool(getattr(args, "auto_sdh", False)),
            remove_closed_captions=getattr(args, "remove_closed_captions", None),
            forced_threshold=getattr(args, "forced_threshold", 50),
            export_directory=getattr(args, "command", "") == "batch" or getattr(args, "profile_command", "") == "batch",
            sync_mode=getattr(args, "sync_mode", None),
            sync_subtitles=getattr(args, "sync_subtitles", None),
            clean_nfo=getattr(args, "clean_nfo", None),
            crossfade_ms=getattr(args, "crossfade_ms", None),
            auto_sync=bool(getattr(args, "auto_sync", False)),
            auto_sync_all=bool(getattr(args, "auto_sync_all", False)),
            calibration=getattr(args, "calibration", None),
            detect_cuts=bool(getattr(args, "detect_cuts", False)),
            quick_sync_check=getattr(args, "quick_sync_check", None),
            reject_coherent_sync=bool(getattr(args, "reject_coherent_sync", False)),
            drift_threshold_ms=getattr(args, "drift_threshold_ms", 25),
            cadence_auto=getattr(args, "cadence_auto", True) if getattr(args, "cadence_auto", None) is not None else True,
            cadence_method=str(getattr(args, "cadence_method", "auto") or "auto"),
            keep_failed_work_dir=bool(getattr(args, "keep_failed_work_dir", False)),
        )


@dataclass(frozen=True)
class JobOverrides:
    config: str | None = None
    template: str | None = None
    input: list[str] | None = None
    output: str | None = None
    auto_tmdb: bool = False
    tmdb: bool = False
    tmdb_id: int | None = None
    tmdb_apikey: str = ""
    output_template: str = ""
    output_all: bool = False
    no_cover: bool = False
    no_attach: bool = False
    mux_backend: str | None = None
    sync_mode: str | None = None
    sync_subtitles: str | None = None
    clean_nfo: bool | None = None
    crossfade_ms: int | None = None
    auto_sync: bool = False
    calibration: str | None = None
    detect_cuts: bool = False
    drift_threshold_ms: int = 25

    @classmethod
    def from_namespace(cls, args: Any) -> "JobOverrides":
        return cls(
            config=getattr(args, "config", None),
            template=getattr(args, "template", None),
            input=getattr(args, "input", None),
            output=getattr(args, "output", None),
            auto_tmdb=bool(getattr(args, "auto_tmdb", False)),
            tmdb=bool(getattr(args, "tmdb", False)),
            tmdb_id=getattr(args, "tmdb_id", None),
            tmdb_apikey=str(getattr(args, "tmdb_apikey", "") or ""),
            output_template=str(getattr(args, "output_template", "") or ""),
            output_all=bool(getattr(args, "output_all", False)),
            no_cover=bool(getattr(args, "no_cover", False)),
            no_attach=bool(getattr(args, "no_attach", False)),
            mux_backend=getattr(args, "mux_backend", None),
            sync_mode=getattr(args, "sync_mode", None),
            sync_subtitles=getattr(args, "sync_subtitles", None),
            clean_nfo=getattr(args, "clean_nfo", None),
            crossfade_ms=getattr(args, "crossfade_ms", None),
            auto_sync=bool(getattr(args, "auto_sync", False)),
            calibration=getattr(args, "calibration", None),
            detect_cuts=bool(getattr(args, "detect_cuts", False)),
            drift_threshold_ms=getattr(args, "drift_threshold_ms", 25),
        )


@dataclass(frozen=True)
class CliContext:
    config: AppConfig
    logger: Logger
    options: CommonOptions


def common_options(args: Any) -> CommonOptions:
    return CommonOptions.from_namespace(args)


def cli_context(args: Any, config: AppConfig, logger: Logger) -> CliContext:
    return CliContext(config=config, logger=logger, options=common_options(args))


def namespace_value(args: Any, name: str, default: Any = None) -> Any:
    return getattr(args, name, default)


def call_cancellable(fn: Any, cancel: Any, *args: Any, context_param: str = "context", **kwargs: Any) -> Any:
    """Appelle ``fn`` en lui passant de quoi s'annuler s'il l'accepte.

    ``context_param`` : ``"context"`` (``PreparationContext`` annulable) ou ``"cancel_event"``. Les
    fonctions de substitution (tests) qui n'ont pas ce paramètre sont appelées telles quelles.
    """
    import inspect

    if cancel is not None:
        try:
            accepts = context_param in inspect.signature(fn).parameters
        except (TypeError, ValueError):
            accepts = False
        if accepts:
            if context_param == "context":
                from core.workflows.preparation_cache import PreparationContext

                kwargs[context_param] = PreparationContext(cancel_event=cancel.event)
            else:
                kwargs[context_param] = cancel.event
    return fn(*args, **kwargs)


def call_supported(fn: Any, *args: Any, **optional: Any) -> Any:
    """Appelle ``fn`` avec les seuls arguments nommés de ``optional`` qu'il accepte (étapes remplaçables)."""
    import inspect

    try:
        parameters = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return fn(*args)
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
        return fn(*args, **optional)
    return fn(*args, **{key: value for key, value in optional.items() if key in parameters})
