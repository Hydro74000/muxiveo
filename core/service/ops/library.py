"""Opérations TMDB (recherche, fiche), profils décisionnels enregistrés (liste, lecture, capture, enregistrement,
suppression) et titres Blu-ray (playlists d'un BDMV avant sélection)."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from core.jobs.constants import EXIT_ARGS, EXIT_EXISTS, EXIT_TOOL, EXIT_VALIDATION
from core.jobs.errors import JobError
from core.service.context import ExecutionContext
from core.service.errors import ErrorCode
from core.service.models import Model, field
from core.service.operations import operation
from core.service.ops.media import MediaJobRequest, _build_cached

# ---------------------------------------------------------------------------
# TMDB
# ---------------------------------------------------------------------------


def _fetcher(ctx: ExecutionContext, api_key: str, language: str) -> Any:
    from core.media_info_fetcher import TmdbError, TmdbFetcher

    config = ctx.config
    try:
        return ctx.port("tmdb_fetcher", TmdbFetcher)(
            api_key=api_key or str(getattr(config, "tmdb_api_key", "") or ""),
            bearer_token=str(getattr(config, "tmdb_bearer_token", "") or ""), language=language)
    except TmdbError as exc:
        raise JobError(f"TMDB : {exc}", EXIT_TOOL) from exc


@dataclass(frozen=True)
class TmdbSearchRequest(Model):
    """Recherche TMDB (films et / ou séries)."""

    query: str = field(description="Titre recherché.")
    kind: Literal["all", "movie", "tv"] = "all"
    year: str = field(default="", description="Année (affine, repli sans année si aucun résultat).")
    language: str = field(default="fr-FR", description="Langue des titres (BCP 47).")
    api_key: str = field(default="", description="Clé API (défaut : réglage [metadata]).")


@dataclass(frozen=True)
class TmdbSearchResult(Model):
    results: tuple[dict[str, Any], ...] = ()


@operation("tmdb.search", request=TmdbSearchRequest, result=TmdbSearchResult,
           summary="Rechercher un film ou une série sur TMDB (25 résultats au plus).",
           errors=(ErrorCode.TOOL_MISSING, ErrorCode.WORKFLOW_FAILED))
def tmdb_search(request: TmdbSearchRequest, ctx: ExecutionContext) -> TmdbSearchResult:
    from core.media_info_fetcher import TmdbError

    try:
        results = _fetcher(ctx, request.api_key, request.language).search(request.query, kind=request.kind,
                                                                           year=request.year)
    except TmdbError as exc:
        raise JobError(f"TMDB : {exc}", 6) from exc
    return TmdbSearchResult(results=tuple(dataclasses.asdict(item) for item in results))


@dataclass(frozen=True)
class TmdbDetailsRequest(Model):
    """Fiche complète d'un film ou d'une série (épisode)."""

    tmdb_id: int = field(minimum=1)
    kind: Literal["movie", "tv"] = "movie"
    season: int | None = field(default=None, minimum=0)
    episode: int | None = field(default=None, minimum=0)
    language: str = "fr-FR"
    api_key: str = ""


@dataclass(frozen=True)
class TmdbDetailsResult(Model):
    details: dict[str, Any] = field(default_factory=dict, description="Fiche (titre, année, genres, résumé…).")
    container_title: str = field(default="", description="Titre du conteneur proposé (balise Title).")
    mkv_tags: dict[str, str] = field(default_factory=dict, description="Balises MKV écrites par Muxiveo.")


@operation("tmdb.details", request=TmdbDetailsRequest, result=TmdbDetailsResult,
           summary="Fiche TMDB complète (titre, année, épisode, résumé, genres, couverture) et balises MKV.",
           errors=(ErrorCode.TOOL_MISSING, ErrorCode.WORKFLOW_FAILED))
def tmdb_details(request: TmdbDetailsRequest, ctx: ExecutionContext) -> TmdbDetailsResult:
    from core.media_info_fetcher import MediaSearchResult, TmdbError

    fetcher = _fetcher(ctx, request.api_key, request.language)
    try:
        details = fetcher.get_details(MediaSearchResult(request.tmdb_id, "", "", request.kind),
                                      season=str(request.season or ""), episode=str(request.episode or ""))
    except TmdbError as exc:
        raise JobError(f"TMDB : {exc}", 6) from exc
    data = {key: value for key, value in dataclasses.asdict(details).items() if key != "cover_bytes"}
    return TmdbDetailsResult(details=data, container_title=details.formatted_container_title(),
                             mkv_tags=details.to_mkv_tags())


# ---------------------------------------------------------------------------
# Profils décisionnels enregistrés (mêmes fichiers que l'interface)
# ---------------------------------------------------------------------------


def _profiles(ctx: ExecutionContext) -> Any:
    from core.profiles.decision import DecisionProfileManager

    return DecisionProfileManager(Path(ctx.config.profiles_dir) / "decision")


@dataclass(frozen=True)
class ProfileListRequest(Model):
    """Profils décisionnels enregistrés."""


@dataclass(frozen=True)
class ProfileListResult(Model):
    profiles: tuple[dict[str, Any], ...] = ()
    load_errors: tuple[dict[str, str], ...] = ()


@operation("profile.list", request=ProfileListRequest, result=ProfileListResult,
           summary="Profils décisionnels enregistrés (communs avec l'interface).")
def profile_list(_request: ProfileListRequest, ctx: ExecutionContext) -> ProfileListResult:
    manager = _profiles(ctx)
    profiles = sorted(manager.load_all(), key=lambda item: str(item.get("name") or "").lower())
    return ProfileListResult(
        profiles=tuple({"name": str(p.get("name") or ""), "description": str(p.get("description") or ""),
                        "rules": len(p.get("rules") or []), "path": str(manager.path_for_name(str(p.get("name") or "")))}
                       for p in profiles),
        load_errors=tuple({"path": str(error.path), "error": str(error.reason)} for error in manager.load_errors))


@dataclass(frozen=True)
class ProfileNameRequest(Model):
    name: str


@dataclass(frozen=True)
class ProfileDocumentResult(Model):
    profile: dict[str, Any] = field(default_factory=dict)
    path: str | None = None


@operation("profile.show", request=ProfileNameRequest, result=ProfileDocumentResult,
           summary="Contenu d'un profil décisionnel enregistré.")
def profile_show(request: ProfileNameRequest, ctx: ExecutionContext) -> ProfileDocumentResult:
    manager = _profiles(ctx)
    profile = manager.load(request.name)
    if profile is None:
        raise JobError(f"Profil introuvable : {request.name}", EXIT_ARGS)
    return ProfileDocumentResult(profile=profile, path=str(manager.path_for_name(request.name)))


@dataclass(frozen=True)
class ProfileSaveRequest(Model):
    """Profil décisionnel enregistré d'après son document (éditeur de profils de l'interface)."""

    profile: dict[str, Any] = field(description="Document decision-profile v1 (`name` requis).")
    force: bool = field(default=False, description="Remplacer un profil du même nom.")


def _save(ctx: ExecutionContext, profile: dict[str, Any], *, force: bool) -> ProfileDocumentResult:
    from core.profiles.decision import (
        DECISION_PROFILE_KIND,
        DECISION_PROFILE_VERSION,
        DecisionProfileError,
        validate_decision_profile,
    )

    document = {"version": DECISION_PROFILE_VERSION, "kind": DECISION_PROFILE_KIND, **profile}
    try:
        validate_decision_profile(document)
    except DecisionProfileError as exc:
        raise JobError(f"Profil invalide : {exc}", EXIT_VALIDATION) from exc
    manager = _profiles(ctx)
    name = str(document.get("name") or "").strip()
    if manager.load(name) is not None and not force:
        raise JobError(f"Profil déjà enregistré : {name} (utiliser force)", EXIT_EXISTS)
    path = manager.save(document)
    ctx.events.artifact("profile", str(path))
    return ProfileDocumentResult(profile=manager.load(name) or document, path=str(path))


@operation("profile.save", request=ProfileSaveRequest, result=ProfileDocumentResult, mutating=True,
           summary="Enregistrer un profil décisionnel (validé) parmi les profils de l'interface.")
def profile_save(request: ProfileSaveRequest, ctx: ExecutionContext) -> ProfileDocumentResult:
    return _save(ctx, dict(request.profile), force=request.force)


@operation("profile.delete", request=ProfileNameRequest, result=ProfileDocumentResult, mutating=True,
           summary="Supprimer un profil décisionnel enregistré.")
def profile_delete(request: ProfileNameRequest, ctx: ExecutionContext) -> ProfileDocumentResult:
    manager = _profiles(ctx)
    profile = manager.load(request.name)
    if profile is None:
        raise JobError(f"Profil introuvable : {request.name}", EXIT_ARGS)
    path = manager.path_for_name(request.name)
    manager.delete(request.name)
    return ProfileDocumentResult(profile=profile, path=str(path))


@dataclass(frozen=True)
class ProfileCaptureRequest(MediaJobRequest):
    """Profil capturé d'après la table des pistes d'un job (« Capturer » du panneau Conteneur)."""

    name: str = field(default="", description="Nom du profil.")
    description: str = ""
    include_selection: bool = True
    include_metadata: bool = True
    include_flags: bool = True
    include_order: bool = True
    include_audio_variants: bool = True
    save: bool = field(default=False, description="Enregistrer le profil capturé.")
    force: bool = False


@operation("profile.capture", request=ProfileCaptureRequest, result=ProfileDocumentResult, long_running=True,
           mutating=True,
           summary="Capturer un profil décisionnel d'après un job (sélection, métadonnées, flags, ordre, variantes).")
def profile_capture(request: ProfileCaptureRequest, ctx: ExecutionContext) -> ProfileDocumentResult:
    from core.profiles.decision import remux_config_to_decision_profile

    if request.save and not request.name.strip():
        raise JobError("profile.capture : `name` requis pour enregistrer.", EXIT_ARGS)
    job = dict(request.job)
    if not job.get("output") and not job.get("output_template"):
        # Aucune écriture : sortie fictive, seule la table des pistes est capturée.
        job["output"] = str(Path.home() / "muxiveo-capture.mkv")
        job["_allow_missing_output_dir"] = True
    remux_config, _options, _cache = _build_cached(dataclasses.replace(request, job=job), ctx)
    profile = remux_config_to_decision_profile(
        remux_config, name=request.name, include_selection=request.include_selection,
        include_metadata=request.include_metadata, include_flags=request.include_flags,
        include_order=request.include_order, include_audio_variants=request.include_audio_variants)
    if request.description:
        profile["description"] = request.description
    if request.save:
        return _save(ctx, profile, force=request.force)
    return ProfileDocumentResult(profile=profile)


# ---------------------------------------------------------------------------
# Blu-ray
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BlurayTitlesRequest(Model):
    """Titres (playlists) d'un disque Blu-ray : dossier BDMV, racine du disque ou fichier .mpls / .m2ts."""

    path: Path
    min_duration_s: float = field(default=0.0, minimum=0, description="Durée minimale d'un titre (s).")


@dataclass(frozen=True)
class BlurayTitlesResult(Model):
    disc_root: str | None = None
    titles: tuple[dict[str, Any], ...] = ()


@operation("media.bluray_titles", request=BlurayTitlesRequest, result=BlurayTitlesResult,
           summary="Lister les titres (playlists) d'un Blu-ray avant sélection, du plus long au plus court : "
                   "durée, segments, flux, taille.")
def bluray_titles(request: BlurayTitlesRequest, _ctx: ExecutionContext) -> BlurayTitlesResult:
    from core.bluray import discover_titles, find_disc_root

    root = find_disc_root(request.path)
    if root is None:
        raise JobError(f"Aucun disque Blu-ray (BDMV) trouvé : {request.path}", EXIT_ARGS)
    titles = discover_titles(request.path, min_duration_s=request.min_duration_s)
    return BlurayTitlesResult(disc_root=str(root), titles=tuple({
        "playlist": title.playlist_id, "path": str(title.playlist_path), "duration_s": round(title.duration_s, 3),
        "size_bytes": title.size_bytes, "segments": [segment.clip_id for segment in title.segments],
        "streams": [{"kind": stream.kind, "pid": stream.pid, "language": stream.language}
                    for stream in title.streams],
    } for title in titles))
