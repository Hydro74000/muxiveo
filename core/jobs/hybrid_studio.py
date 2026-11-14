"""Hybridation headless sur le moteur du Hybrid Studio : rôles des sources, plusieurs donneurs, appariement
(épisodes, similarité, ordre ou paires manuelles), recette, préparations en parallèle, cadences tranchées.

Mêmes étapes que le Studio : ``HybridMatrix.scan`` → ``prepare_matrix_episode`` (cache de session partagé)
→ ``resolve_plan_cadence_methods`` → export des exact-jobs, aperçu ou exécution (par la couche de service).
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from core.jobs.batch import PlannedBatchJob, assert_unique_batch_outputs
from core.jobs.constants import EXIT_ARGS, EXIT_VALIDATION
from core.jobs.errors import JobError
from core.jobs.options import CommonOptions
from core.service.context import CancelToken
from core.workflows.hybrid_matrix import (
    HybridMatrix,
    HybridRecipe,
    MatchingMode,
    MatrixEpisode,
    MatrixSource,
    SourceRole,
    parse_episode_key,
    plan_needs_cadence_method,
    prepare_matrix_episode,
    resolve_plan_cadence_methods,
    store_plan_calibration,
)
from core.workflows.preparation_cache import PreparationCache
from core.workflows.remux_models import RemuxConfig
from core.workflows.sync_calibration import SyncCalibration

#: Rôles publics (``--source RÔLE=CHEMIN``).
ROLES: dict[str, SourceRole] = {
    "master": SourceRole.MASTER, "donor": SourceRole.DONOR, "donor-audio": SourceRole.DONOR_AUDIO,
    "donor-sub": SourceRole.DONOR_SUBTITLE,
}
MATCHING: dict[str, MatchingMode] = {"episode": MatchingMode.EPISODE, "fuzzy": MatchingMode.FUZZY,
                                     "order": MatchingMode.ORDER}
#: Préparations simultanées (comme le Studio) ; avec analyse audio, deux seulement.
PREPARE_WORKERS = 4
CALIBRATE_WORKERS = 2


@dataclass(frozen=True)
class StudioSpec:
    """Hybridation à préparer : sources et rôles, appariement, recette, nommage, options de job."""

    sources: tuple[tuple[SourceRole, Path], ...]
    output_dir: Path
    matching: MatchingMode = MatchingMode.EPISODE
    pairs: tuple[tuple[Path, tuple[Path, ...]], ...] = ()
    recipe: HybridRecipe = field(default_factory=HybridRecipe)
    common: CommonOptions = field(default_factory=CommonOptions)
    profile: str = ""
    auto_tmdb: int | None = None
    tmdb_apikey: str = ""
    no_cover: bool = False
    tag: str = "MVO"
    output_template: str = ""
    output_all: bool = False


@dataclass
class PreparedEpisode:
    episode: MatrixEpisode
    remux_config: RemuxConfig | None = None
    calibration: SyncCalibration | None = None
    error: Exception | None = None

    def describe(self) -> dict[str, Any]:
        episode = self.episode
        entry: dict[str, Any] = {
            "episode": episode.display_name,
            "master": str(episode.master_file) if episode.master_file else None,
            "donors": [str(path) for _source, path in episode.donor_files],
        }
        if self.remux_config is not None:
            entry["output"] = str(self.remux_config.output)
            entry["sync_mode"] = self.remux_config.sync_mode
            entry["tracks"] = [
                {"source": index, "mkv_tid": track.mkv_tid, "type": track.track_type, "language": track.language,
                 "title": track.title, "time_shift_ms": track.time_shift_ms}
                for index, source in enumerate(self.remux_config.sources) for track in source.tracks if track.enabled
            ]
        if self.calibration is not None:
            entry["calibration"] = self.calibration.to_dict()
        if self.error is not None:
            entry["error"] = str(self.error)
        return entry


def parse_sources(values: Sequence[str]) -> tuple[tuple[SourceRole, Path], ...]:
    """``RÔLE=CHEMIN`` (master, donor, donor-audio, donor-sub) ; fichiers ou dossiers."""
    sources = []
    for value in values:
        role, sep, path = str(value).partition("=")
        if not sep or role.strip().lower() not in ROLES or not path.strip():
            raise JobError(f"--source : RÔLE=CHEMIN attendu, rôle parmi {', '.join(ROLES)} (reçu {value!r}).",
                           EXIT_ARGS)
        sources.append((ROLES[role.strip().lower()], Path(path.strip()).expanduser()))
    return tuple(sources)


def parse_pairs(values: Sequence[str]) -> tuple[tuple[Path, tuple[Path, ...]], ...]:
    """``MASTER::DONNEUR[::DONNEUR…]`` : paires manuelles."""
    pairs = []
    for value in values:
        parts = [part.strip() for part in str(value).split("::")]
        if len(parts) < 2 or not all(parts):
            raise JobError(f"--pair : MASTER::DONNEUR attendu (reçu {value!r}).", EXIT_ARGS)
        pairs.append((Path(parts[0]).expanduser(), tuple(Path(part).expanduser() for part in parts[1:])))
    return tuple(pairs)


def load_recipe(path: Path) -> HybridRecipe:
    from core.json_documents import read_json_document

    try:
        return HybridRecipe.from_dict(read_json_document(path))
    except (OSError, ValueError) as exc:
        raise JobError(f"Recette illisible ({path}) : {exc}", EXIT_ARGS) from exc


def _episodes_from_pairs(pairs: Sequence[tuple[Path, tuple[Path, ...]]]) -> list[MatrixEpisode]:
    episodes = []
    for master, donors in pairs:
        master = master.resolve()
        if not master.is_file() or not all(donor.is_file() for donor in donors):
            raise JobError(f"--pair : fichier introuvable ({master} :: {', '.join(map(str, donors))}).", EXIT_ARGS)
        key = parse_episode_key(master)
        episode = MatrixEpisode(season=key[0] if key else 0, episode=key[1] if key else 0,
                                item_key="" if key else master.stem, master_file=master)
        # Identifiants des donneurs comme dans le Studio (src_2, src_3…) : recettes interchangeables.
        episode.donor_files = [(MatrixSource(f"src_{index}", donor, SourceRole.DONOR), donor.resolve())
                               for index, donor in enumerate(donors, start=2)]
        episodes.append(episode)
    return episodes


def plan_episodes(spec: StudioSpec, logger: Any) -> list[MatrixEpisode]:
    """Épisodes appariés : paires manuelles, sinon balayage des sources selon le mode d'appariement."""
    if spec.pairs:
        if spec.sources:
            raise JobError("--pair exclut --source / --ref / --donor.", EXIT_ARGS)
        return _episodes_from_pairs(spec.pairs)
    masters = [path for role, path in spec.sources if role is SourceRole.MASTER]
    if len(masters) != 1 or len(spec.sources) < 2:
        raise JobError("Hybridation : une source master et au moins un donneur requis.", EXIT_ARGS)
    matrix = HybridMatrix(matching_mode=spec.matching)
    for priority, (role, path) in enumerate(sorted(spec.sources, key=lambda item: item[0] is not SourceRole.MASTER)):
        if not path.exists():
            raise JobError(f"Source introuvable : {path}", EXIT_ARGS)
        matrix.add_source(path, role=role, priority=10 if priority == 1 else 5)
    episodes = matrix.scan()
    complete = [episode for episode in episodes if episode.is_complete]
    for episode in episodes:
        if not episode.is_complete:
            logger.emit("warning", f"{episode.display_name} : incomplet (master ou donneur absent), ignoré.")
    if not complete:
        raise JobError("Hybridation : aucun épisode apparié (vérifier les sources et --matching).", EXIT_VALIDATION)
    return complete


def episode_options(spec: StudioSpec, config: Any, cancel: CancelToken) -> SimpleNamespace:
    """Options lues par ``prepare_matrix_episode`` (mêmes noms que celles du Studio)."""
    values = dataclasses.asdict(spec.common)
    values.update(profile=spec.profile or None, auto_tmdb=spec.auto_tmdb, tmdb_apikey=spec.tmdb_apikey,
                  no_cover=spec.no_cover, tag=spec.tag, output_template=spec.output_template,
                  output_all=spec.output_all, cancel_event=cancel.event, profile_version=(), profile_snapshot=None)
    if spec.profile:
        from core.jobs.profile import snapshot_decision_profile

        values["profile_version"], values["profile_snapshot"] = snapshot_decision_profile(spec.profile, config)
    return SimpleNamespace(**values)


def prepare_episodes(spec: StudioSpec, episodes: Sequence[MatrixEpisode], config: Any,
                     logger_for: Callable[[MatrixEpisode], Any], cancel: CancelToken) -> list[PreparedEpisode]:
    """Plans de tous les épisodes, préparés en parallèle (cache de session partagé), puis méthodes de cadence
    « auto » tranchées par l'analyse de hauteur. Erreurs rattachées à leur épisode."""
    options = episode_options(spec, config, cancel)
    cache = PreparationCache()
    recipe = spec.recipe
    if getattr(spec.common, "crossfade_ms", None) is not None:
        recipe = dataclasses.replace(recipe, crossfade_ms=int(spec.common.crossfade_ms or 0))
    workers = CALIBRATE_WORKERS if recipe.sync_strategy == "auto" else PREPARE_WORKERS

    def prepare(episode: MatrixEpisode) -> PreparedEpisode:
        if cancel.cancelled:
            return PreparedEpisode(episode, error=JobError("Préparation annulée.", EXIT_VALIDATION))
        try:
            remux_config, calibration = prepare_matrix_episode(
                episode, recipe, spec.output_dir, config, options, logger_for(episode),
                detect_cuts=spec.common.detect_cuts, drift_threshold_ms=spec.common.drift_threshold_ms or 25,
                cache=cache)
            return PreparedEpisode(episode, remux_config, calibration)
        except Exception as exc:
            return PreparedEpisode(episode, error=exc)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        prepared = list(executor.map(prepare, episodes))
    ffmpeg = spec.common.ffmpeg or config.tool_ffmpeg
    ffprobe = spec.common.ffprobe or config.tool_ffprobe
    for item in prepared:
        if item.remux_config is None or cancel.cancelled or not plan_needs_cadence_method(item.remux_config):
            continue
        name = item.episode.display_name
        try:
            resolved = resolve_plan_cadence_methods(item.remux_config, ffmpeg, ffprobe, cancel.event,
                                                    logger_for(item.episode), name)
            for key, calibration in resolved.items():
                store_plan_calibration(item.remux_config, int(key), calibration)
                if key == "1":
                    item.calibration = calibration
            if plan_needs_cadence_method(item.remux_config):
                raise JobError(f"{name} : méthode de conversion de cadence non déterminée.", EXIT_VALIDATION)
        except Exception as exc:
            item.error = exc
    return prepared


def check_outputs(prepared: Sequence[PreparedEpisode], template: str = "") -> None:
    """Collisions de sorties refusées avant le premier traitement (règle des lots)."""
    assert_unique_batch_outputs([
        PlannedBatchJob(index, str(item.episode.master_file), str(item.remux_config.output),
                        remux_config=item.remux_config, template=template)
        for index, item in enumerate(prepared) if item.remux_config is not None
    ])


def export_jobs(prepared: Sequence[PreparedEpisode], directory: Path) -> list[Path]:
    """Exact-jobs finalisés des épisodes préparés (« Sauvegarder les workflows » du Studio)."""
    from core.profiles.selectors import remux_config_to_exact_job
    from core.workflows.workflow_store import save_workflow

    ready = [item.remux_config for item in prepared if item.remux_config is not None]
    payloads = [(directory / (config.output.stem + ".exact-job.json"), remux_config_to_exact_job(config))
                for config in ready]
    if len({path for path, _payload in payloads}) != len(payloads):
        raise JobError("Plusieurs workflows portent le même nom.", EXIT_VALIDATION)
    directory.mkdir(parents=True, exist_ok=True)
    for path, payload in payloads:
        save_workflow(path, payload)
    return [path for path, _payload in payloads]


def recipe_with_overrides(recipe: HybridRecipe, **overrides: Any) -> HybridRecipe:
    """Recette (fichier ou défaut) complétée des options de la commande (valeurs ``None`` ignorées)."""
    values = {key: value for key, value in overrides.items() if value is not None}
    unknown = set(values) - {f.name for f in dataclasses.fields(HybridRecipe)}
    if unknown:
        raise JobError("Recette : option(s) inconnue(s) : " + ", ".join(sorted(unknown)), EXIT_ARGS)
    try:
        return HybridRecipe.from_dict({**recipe.to_dict(), **{key: list(value) if isinstance(value, tuple) else value
                                                              for key, value in values.items()}})
    except ValueError as exc:
        raise JobError(str(exc), EXIT_ARGS) from exc


def recipe_json(recipe: HybridRecipe) -> str:
    return json.dumps(recipe.to_dict(), ensure_ascii=False, indent=2) + "\n"
