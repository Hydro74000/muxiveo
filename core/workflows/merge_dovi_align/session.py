"""Étape ALIGN de Merge DoVi, sans Qt : passe de Film 1, audits de Film 2, alignement, L5/L6, éditions.

Le workflow fournit les fichiers, les outils, les crochets d'annulation et de suivi des processus, et un
exécuteur de commandes ; ce module rend les métadonnées éditées (ou l'identité vérifiée) et un compte rendu.
"""
from __future__ import annotations

import os
import shutil
import sys
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass, field
from enum import Enum
from fractions import Fraction
from pathlib import Path

import numpy as np

from core.hdr_inspection.dovi import parse_dovi_export
from core.i18n import translate_text

from .active_area import ActiveAreaPlan, plan_active_area
from .align import AlignmentError, AlignmentMap, Film2Frames, Film2UndecodableError, align, detect_cuts
from .edits import EditPlan, apply_hdr10plus, apply_rpu, build_edit_plan
from .features import active_band
from .frames import FrameTable, read_frame_table
from .presence import NalPredicate, PresenceGaps, has_hdr10plus, has_rpu, scan_annexb_presences, scan_matroska_presences
from .probe import DecoderPreference, ProbeError, Signatures, decode_all, decode_window
from .process import ProcessHooks
from .signals import Film2Metadata, combine, hdr10plus_track, rpu_render_digests, rpu_track
from .static_hdr import ResolvedStatic, level6_plan, resolve_static_hdr

_RAW_HEVC = frozenset({".hevc", ".h265", ".265", ".x265"})


class AlignmentMode(Enum):
    """Politique de Merge DoVi face à deux films dont les images ne correspondent pas une à une."""

    AUTO = "auto"      # analyse de l'image, éditions vérifiées
    STRICT = "strict"  # comptages identiques exigés, aucune analyse


@dataclass(frozen=True)
class AlignmentOptions:
    mode: AlignmentMode = AlignmentMode.AUTO
    #: D6 : vérifier Film 2 image par image (passe complète de Film 2). Les images de Film 1 sans correspondance
    #: sont toujours comblées par les métadonnées voisines (aucun trou), avec avertissement.
    verify_film2: bool = False
    #: Décision DC : alignement forcé sur les seules coupes des métadonnées (Film 2 indécodable), jamais lu ;
    #: demandé par la fenêtre du panneau après un refus, jamais mémorisé ni par défaut.
    metadata_only: bool = False

    @property
    def auto(self) -> bool:
        return self.mode is AlignmentMode.AUTO


@dataclass(frozen=True)
class AlignmentTools:
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    dovi_tool: str = "dovi_tool"
    hdr10plus_tool: str = "hdr10plus_tool"


@dataclass
class AlignmentResult:
    """Issue de l'étape : carte, plan d'édition, métadonnées à injecter (éditées ou d'origine)."""

    map: AlignmentMap
    plan: EditPlan | None
    rpu: Path | None
    hdr10plus: Path | None
    resolved_static: ResolvedStatic
    active_area: ActiveAreaPlan | None
    film1_table: FrameTable
    film2_table: FrameTable | None
    notes: list[str] = field(default_factory=list)

    @property
    def edited(self) -> bool:
        return self.plan is not None


def cuda_decode_available(ffmpeg: str) -> bool:
    """Décodage NVDEC plausible : FFmpeg compilé avec CUDA et pilote NVIDIA présent (sinon pas de tentative)."""
    if sys.platform == "darwin":
        return False
    if sys.platform.startswith("linux") and not Path("/dev/nvidiactl").exists():
        return False
    if sys.platform == "win32" and not Path(os.environ.get("SystemRoot", r"C:\Windows"), "System32",
                                             "nvcuda.dll").exists():
        return False
    return shutil.which(ffmpeg) is not None or Path(ffmpeg).exists()


def frame_rate(table: FrameTable) -> float:
    """Cadence nominale (images par seconde) : écart minimal des PTS d'une entrée indexable, sinon cadence
    annoncée par le conteneur, sinon 24."""
    if len(table.pts) >= 2:
        seconds = Fraction(table.frame_interval) * table.props.time_base
        if seconds > 0:
            return float(1 / seconds)
    if table.props.frame_rate > 0:
        return float(table.props.frame_rate)
    return 24.0


class Film1Pass:
    """Passe complète de Film 1 lancée en arrière-plan, en parallèle de l'extraction (une seule fois)."""

    def __init__(self, film1: Path, tools: AlignmentTools, hooks: ProcessHooks, decoder: DecoderPreference) -> None:
        self._stop = threading.Event()
        self.decoded = 0
        cancelled = hooks.cancelled
        self.hooks = ProcessHooks(cancelled=lambda: cancelled() or self._stop.is_set(), on_start=hooks.on_start,
                                  on_end=hooks.on_end)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="merge-dovi-film1")
        self._future: Future = self._executor.submit(self._run, film1, tools, decoder)
        self._executor.shutdown(wait=False)

    def _count(self, frames: int) -> None:
        self.decoded = frames

    def _run(self, film1: Path, tools: AlignmentTools,
             decoder: DecoderPreference) -> tuple[FrameTable, Signatures]:
        table = read_frame_table(film1, ffprobe=tools.ffprobe, hooks=self.hooks)
        return table, decode_all(table, ffmpeg=tools.ffmpeg, hooks=self.hooks, hwaccel=decoder, progress=self._count)

    def wait(self, total: int, report: Callable[[int], None] | None, hooks: ProcessHooks) -> tuple[FrameTable, Signatures]:
        """Résultat de la passe ; pendant l'attente, avancement en % (``report``) et annulation surveillée."""
        while True:
            hooks.check()
            try:
                return self._future.result(timeout=0.5)
            except TimeoutError:
                if report is not None and total > 0:
                    report(min(99, int(100 * self.decoded / total)))

    def result(self) -> tuple[FrameTable, Signatures]:
        return self._future.result()

    def stop(self, timeout: float = 30.0) -> bool:
        """Arrête la passe (son processus est tué par la surveillance d'annulation) et attend la fin du fil ;
        vrai si le fil est terminé."""
        self._stop.set()
        try:
            self._future.result(timeout=timeout)
        except TimeoutError:
            return False
        except BaseException:  # noqa: BLE001 — issue sans intérêt une fois la passe abandonnée
            pass
        return self._future.done()


def _first_video_track(path: Path) -> int:
    from core.matroska.reader import MatroskaReader

    tracks = MatroskaReader(path).tracks()
    return next(index for index, track in enumerate(tracks) if track.track_type == 1)


def audit_film2(film2_scan: Path, *, rpu: bool, hdr10plus: bool, hooks: ProcessHooks) -> dict[str, PresenceGaps]:
    """Présence du RPU / du HDR10+ image par image dans le flux vidéo de Film 2 (une lecture, sans décodage).

    À faire **avant** toute conversion du flux (un Film 2 hors MKV n'est lisible que par son extrait Annex B,
    supprimé par la conversion P7/P5)."""
    wanted: dict[str, NalPredicate] = {}
    if rpu:
        wanted["rpu"] = has_rpu
    if hdr10plus:
        wanted["hdr10plus"] = has_hdr10plus
    if not wanted:
        return {}
    if film2_scan.suffix.lower() in {".mkv", ".mk3d"}:
        return scan_matroska_presences(film2_scan, _first_video_track(film2_scan), wanted, cancelled=hooks.cancelled)
    return scan_annexb_presences(film2_scan, wanted, cancelled=hooks.cancelled)


def run_alignment(*, film1_pass: Film1Pass, film1_frames: int | None, film2: Path, presence: dict[str, PresenceGaps],
                  rpu: Path | None, hdr10plus: Path | None, static1: object, static2: object, options: AlignmentOptions,
                  tools: AlignmentTools, work_dir: Path, hooks: ProcessHooks, decoder: DecoderPreference,
                  log: Callable[[str], None], run_tool: Callable[[list[str]], str],
                  progress: Callable[[int], None] | None = None) -> AlignmentResult:
    """Étape ALIGN complète ; :class:`AlignmentError` si la correspondance n'est pas
    établie ou si des métadonnées devraient être inventées sans accord."""
    hooks.check()
    work_dir.mkdir(parents=True, exist_ok=True)
    # 1. Présence des métadonnées image par image dans Film 2 (``audit_film2``, fait avant toute conversion).
    film2_frames = next((gaps.frames for gaps in presence.values()), None)
    if "hdr10plus" in presence and presence["hdr10plus"].missing:
        gaps = presence["hdr10plus"]
        raise AlignmentError(translate_text(
            "Film 2 : {count} image(s) sans HDR10+ (rangs {ranks}) — l'extracteur les comblerait en ordre de "
            "décodage : métadonnées non alignables.", count=len(gaps.missing), ranks=list(gaps.missing[:6])))
    if "rpu" in presence and presence["rpu"].missing and rpu is not None:
        gaps = presence["rpu"]
        if not gaps.trailing:
            raise AlignmentError(translate_text(
                "Film 2 : {count} image(s) sans RPU réparties dans le flux (rangs {ranks}) : métadonnées non "
                "alignables.", count=len(gaps.missing), ranks=list(gaps.missing[:6])))
        from core.workflows.encode.runtime.dovi_rpu_gaps import fill_rpu_gaps

        completed = work_dir / "align_rpu_completed.bin"
        fill_rpu_gaps(dovi_tool_bin=tools.dovi_tool, rpu_input=rpu, output_rpu=completed,
                      positions=list(gaps.missing), run_cmd=run_tool)
        log("[WARN] " + translate_text(
            "Film 2 : {count} image(s) finale(s) sans RPU complétée(s) par le RPU précédent.", count=len(gaps.missing)))
        rpu = completed

    # 2. Repères de Film 2 (métadonnées seules).
    tracks = []
    film2_l5 = None
    current_l6: list[tuple[int, int, int, int]] = []
    if rpu is not None:
        export = work_dir / "align_rpu_export.json"
        run_tool([tools.dovi_tool, "export", "-i", str(rpu), "-d", f"all={export}"])
        try:
            analysis = parse_dovi_export(export, cancelled=hooks.cancelled)
            render = rpu_render_digests(export, cancelled=hooks.cancelled)
        finally:
            export.unlink(missing_ok=True)
        tracks.append(rpu_track(rpu, analysis.scene_starts, analysis.l1_avg, analysis.l1_max, render_digests=render,
                                cancelled=hooks.cancelled))
        film2_l5 = np.zeros((analysis.frames, 4), dtype=np.int32)
        for area in analysis.active_areas:
            film2_l5[area.first_frame:area.first_frame + area.frames] = (area.left, area.right, area.top, area.bottom)
        current_l6 = [(int(item.max_mastering), int(round(item.min_mastering * 10000)), int(item.max_cll),
                       int(item.max_fall)) for item in analysis.l6]
    if hdr10plus is not None:
        tracks.append(hdr10plus_track(hdr10plus, cancelled=hooks.cancelled))
    metadata: Film2Metadata = combine(tracks)
    if film2_frames is not None and metadata.frames != film2_frames:
        raise AlignmentError(translate_text(
            "Film 2 : {frames} images de métadonnées pour {film2_frames} images vidéo.",
            frames=metadata.frames, film2_frames=film2_frames))

    # 3. Film 1 (passe complète lancée plus tôt) et accès à Film 2.
    log(translate_text("Passe complète de Film 1 : attente de la fin du décodage…"))
    film1_table, film1 = film1_pass.wait(film1_frames or 0, progress, hooks)
    log(translate_text("Film 1 analysé ({frames} images, décodage {decoder}).", frames=len(film1),
                       decoder=translate_text(film1.decoder)))
    if film1_frames is not None and len(film1) != film1_frames:
        raise AlignmentError(translate_text(
            "Film 1 : {count} images décodées pour {film1_frames} comptées.",
            count=len(film1), film1_frames=film1_frames))
    film2_table: FrameTable | None = None
    possible = bool(metadata.has_reference_cuts)
    if options.metadata_only:
        # Alignement forcé (décision DC) : Film 2 jamais lu, ni table ni image.
        if options.verify_film2:
            log("[WARN] " + translate_text(
                "Vérification de Film 2 image par image sans objet : alignement forcé sur les métadonnées."))
        source = Film2Frames(metadata.frames)
    else:
        try:
            film2_table = read_frame_table(film2, ffprobe=tools.ffprobe, hooks=hooks)
        except InterruptedError:
            raise
        except Exception as exc:  # noqa: BLE001 — refus immédiat avec la cause d'origine (décision DC)
            hooks.check()
            raise Film2UndecodableError(translate_text("Film 2 non décodable ({error}).", error=str(exc)),
                                        metadata_alignment_possible=possible) from exc
        if film2_table.indexable and film2_table.frames != metadata.frames:
            raise AlignmentError(translate_text(
                "Film 2 : {frames} images vidéo pour {frames2} images de métadonnées.",
                frames=film2_table.frames, frames2=metadata.frames))
        table = film2_table
        source = Film2Frames(
            metadata.frames,
            window=_undecodable_on_failure(
                lambda start, count: decode_window(table, start, count, ffmpeg=tools.ffmpeg, hooks=hooks,
                                                   hwaccel=decoder), hooks, possible)
            if table.indexable else None,
            full_loader=_undecodable_on_failure(
                lambda: decode_all(table, ffmpeg=tools.ffmpeg, hooks=hooks, hwaccel=decoder), hooks, possible),
        )
    alignment = align(film1, metadata, source, frame_rate=frame_rate(film1_table),
                      force_full_film2=options.verify_film2 and not options.metadata_only,
                      metadata_only=options.metadata_only, hooks=hooks, log=log)
    notes = [_describe_map(alignment)]

    # 4. Zone active, HDR10 statique unique, éditions.
    resolved = resolve_static_hdr(static1, static2)
    area_plan = None
    level6 = None
    if rpu is not None:
        film2_size = (film2_table.props.width, film2_table.props.height) if film2_table is not None else (0, 0)
        area_plan = plan_active_area(film1, detect_cuts(film1, active_band(film1)), alignment, film2_l5=film2_l5,
                                     film2_size=film2_size, film2_read=not options.metadata_only)
        level6, refusal = level6_plan(resolved, current_l6)
        if refusal:
            log("[WARN] " + translate_text(refusal))
        notes.append(area_plan.describe())
        if level6 is not None:
            notes.append(translate_text(
                "L6 aligné sur le HDR10 de la sortie ({source}).", source=resolved.describe()))
    if alignment.identity and (area_plan is None or area_plan.keep) and level6 is None:
        notes.append(translate_text("Métadonnées de Film 2 injectées telles quelles."))
        return AlignmentResult(alignment, None, rpu, hdr10plus, resolved, area_plan, film1_table, film2_table, notes)
    plan = build_edit_plan(alignment, active_area=area_plan, level6=level6)
    for warning in plan.warnings:
        log(f"[WARN] {warning}")
    edited_rpu = apply_rpu(plan, rpu, work_dir, dovi_tool=tools.dovi_tool, run=run_tool) if rpu is not None else None
    hooks.check()
    edited_hdr = (apply_hdr10plus(plan, hdr10plus, work_dir, hdr10plus_tool=tools.hdr10plus_tool, run=run_tool)
                  if hdr10plus is not None else None)
    return AlignmentResult(alignment, plan, edited_rpu, edited_hdr, resolved, area_plan, film1_table, film2_table,
                           notes)


def _undecodable_on_failure(loader: Callable, hooks: ProcessHooks, possible: bool) -> Callable:
    """Chargeur de Film 2 dont l'échec de décodage (``ProbeError``) devient :class:`Film2UndecodableError` ;
    annulation jamais convertie."""
    def call(*args: int) -> Signatures:
        try:
            return loader(*args)
        except ProbeError as exc:
            hooks.check()
            raise Film2UndecodableError(translate_text("Film 2 non décodable ({error}).", error=str(exc)),
                                        metadata_alignment_possible=possible) from exc
    return call


def _describe_map(alignment: AlignmentMap) -> str:
    if alignment.metadata_only:
        return translate_text(
            "Alignement par les coupes des métadonnées, image de Film 2 non vérifiée : {segments} segment(s), "
            "{uncovered} image(s) de Film 1 sans correspondance.", segments=len(alignment.segments),
            uncovered=sum(gap.length for gap in alignment.uncovered))
    if alignment.identity:
        return translate_text("Alignement vérifié : les {frames} images correspondent une à une.",
                              frames=alignment.film1_frames)
    offsets = sorted({seg.offset for seg in alignment.segments})
    parts = [translate_text("{count} segment(s), décalage(s) {offsets}", count=len(alignment.segments),
                            offsets=", ".join(f"{d:+d}" for d in offsets))]
    removed = sum(length for _start, length in alignment.removed)
    if removed:
        parts.append(translate_text("{count} image(s) de Film 2 retirée(s) en {ranges} plage(s)", count=removed,
                                    ranges=len(alignment.removed)))
    uncovered = sum(gap.length for gap in alignment.uncovered)
    if uncovered:
        parts.append(translate_text("{count} image(s) de Film 1 sans correspondance", count=uncovered))
    return translate_text("Alignement : {details}.", details=" ; ".join(parts))
