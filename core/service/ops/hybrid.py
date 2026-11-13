"""Opération ``hybrid.run`` : hybridation par paires (référence + donneur), un job enfant par épisode.

Moteur par paires de la commande ``hybrid`` (``core.jobs.hybrid_pairs``) ; le moteur du Hybrid Studio
(rôles, plusieurs donneurs, recette) le remplacera sans changer la forme de la requête.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from core.jobs.hybrid_pairs import HybridOptions, HybridOutcome, HybridSteps, pairs_for, run_hybrid
from core.jobs.logging import EmitterLogger
from core.jobs.profile import build_profile_remux_config, load_decision_profile
from core.jobs.remux_config import build_remux_config
from core.jobs.runtime import remux_workflow
from core.service.context import ExecutionContext
from core.service.errors import ErrorCode
from core.service.models import Model, field
from core.service.operations import operation
from core.service.ops.batch import _Children
from core.service.ops.media import JobOptions
from core.workflows.audio_sync_scan import AudioSyncScanner, coherent_sync_accepted, quick_check_enabled

#: Réglages par défaut d'une hybridation (réécriture physique, sous-titres en miroir, fondu 80 ms).
_HYBRID_DEFAULTS = {"sync_mode": "physical", "sync_subtitles": "mirror", "clean_nfo": True, "crossfade_ms": 80}


@dataclass(frozen=True)
class HybridRequest(Model):
    """Paire (ref + donor) ou saison (ref_dir + donor_dir) à hybrider."""

    output_dir: Path = field(description="Dossier des sorties.")
    ref: Path | None = field(default=None, description="Fichier de référence (vidéo, chapitres).")
    donor: Path | None = field(default=None, description="Fichier donneur (audio, sous-titres).")
    ref_dir: Path | None = field(default=None, description="Dossier de référence (épisodes).")
    donor_dir: Path | None = field(default=None, description="Dossier donneur (épisodes).")
    profile: str = field(default="", description="Profil décisionnel (chemin ou nom).")
    auto_tmdb: int | None = field(default=None, description="TMDB : absent = sans ; 0 = recherche ; ID sinon.")
    tmdb_apikey: str = ""
    no_cover: bool = False
    tag: str = field(default="MVO", description="Groupe de release ({group} du gabarit).")
    output_template: str = ""
    output_all: bool = False
    options: JobOptions = field(default_factory=JobOptions,
                                description="Synchro et pistes ; défauts hybrides : physical, mirror, fondu 80 ms.")
    export_workflow: Path | None = field(default=None, description="Enregistrer les jobs exacts au lieu d'exécuter.")
    dry_run: bool = field(default=False, description="Plan d'exécution de chaque épisode, sans écriture.")
    force: bool = False
    continue_on_error: bool = False
    report_json: Path | None = field(default=None, description="Rapport JSON des épisodes.")


@dataclass(frozen=True)
class HybridResult(Model):
    """Bilan : un élément par épisode (référence, donneur, sortie, calibration, état)."""

    status: Literal["succeeded", "partial", "failed", "cancelled"]
    episodes: tuple[dict[str, Any], ...] = ()


def _status(outcome: HybridOutcome) -> Literal["succeeded", "partial", "failed", "cancelled"]:
    states = [entry.get("status") for entry in outcome.episodes]
    succeeded = sum(1 for state in states if state in ("ok", "planned", "exported"))
    if not any(state in ("failed", "cancelled") for state in states) and not outcome.cancelled:
        return "succeeded"
    if succeeded:
        return "partial"
    return "cancelled" if outcome.cancelled else "failed"


@operation(
    "hybrid.run",
    request=HybridRequest,
    result=HybridResult,
    summary="Hybrider une paire ou une saison : vidéo de la référence, audio / sous-titres du donneur calés.",
    long_running=True,
    mutating=True,
    errors=(ErrorCode.INVALID_REQUEST, ErrorCode.VALIDATION, ErrorCode.OUTPUT_EXISTS, ErrorCode.TOOL_MISSING,
            ErrorCode.WORKFLOW_FAILED, ErrorCode.CANCELLED),
)
def hybrid_run(request: HybridRequest, ctx: ExecutionContext) -> HybridResult:
    payload = request.options.to_dict()
    for key, value in _HYBRID_DEFAULTS.items():
        if payload.get(key) is None:
            payload[key] = value
    job_options = JobOptions.from_dict(payload, allow_relative=True)
    options = HybridOptions(
        output_dir=request.output_dir, common=job_options.to_common(ctx.overrides), profile=request.profile,
        auto_tmdb=request.auto_tmdb, tmdb_apikey=request.tmdb_apikey, no_cover=request.no_cover, tag=request.tag,
        output_template=request.output_template, output_all=request.output_all,
        export_workflow=str(request.export_workflow) if request.export_workflow is not None else "",
        dry_run=request.dry_run, force=request.force, continue_on_error=request.continue_on_error,
        report_json=str(request.report_json) if request.report_json is not None else "",
    )
    children = _Children(ctx)
    steps = HybridSteps(
        build=build_remux_config,
        load_profile=load_decision_profile,
        build_profile=build_profile_remux_config,
        audio_scanner=lambda: AudioSyncScanner(ctx.tool("ffmpeg"), ctx.tool("ffprobe"), cancel_event=ctx.cancel.event,
                                               quick_check=quick_check_enabled(job_options, ctx.config),
                                               accept_coherent=coherent_sync_accepted(job_options)),
        run=lambda config, common, logger, remux_config, *, force: children.run(
            config, common, children.logger(0), remux_config, force=force),
        preview=lambda config, common, remux_config: dict(remux_workflow(config, common).execution_preview(
            remux_config)),
    )
    pairs = pairs_for(str(request.ref) if request.ref else None, str(request.donor) if request.donor else None,
                      str(request.ref_dir) if request.ref_dir else None,
                      str(request.donor_dir) if request.donor_dir else None)
    outcome = run_hybrid(pairs, options, ctx.config, EmitterLogger(ctx.events), steps, cancel=ctx.cancel)
    return HybridResult(status=_status(outcome), episodes=tuple(outcome.episodes))
