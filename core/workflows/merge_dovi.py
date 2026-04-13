"""
core/workflows/merge_dovi.py — Workflow d'injection DoVi RPU + HDR10+.

Extrait la logique métier de merge_dovi_hdr10plus.py et l'adapte pour
être piloté depuis l'interface Qt via ToolRunner.

Classes publiques :
    FrameCountResult   — résultat de la comparaison des frame counts
    DoviProfile        — profil Dolby Vision cible (8.0 / 8.1)
    WorkflowStep       — énumération des étapes du workflow
    StepResult         — résultat d'une étape
    MergeDoviWorkflow  — orchestrateur du workflow

Signaux :
    MergeDoviWorkflow.step_started(step: WorkflowStep)
    MergeDoviWorkflow.step_progress(step: WorkflowStep, message: str)
    MergeDoviWorkflow.step_finished(step: WorkflowStep, result: StepResult)
    MergeDoviWorkflow.workflow_finished(output_path: str)
    MergeDoviWorkflow.workflow_failed(step: WorkflowStep, error: str)

Conventions :
    - Jamais shell=True
    - pathlib.Path pour tous les chemins
    - ThreadPoolExecutor pour les extractions parallèles
    - Signaux Qt thread-safe (QueuedConnection depuis les workers)
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, Signal
from core.subprocess_utils import subprocess_text_kwargs
from core.workdir import prepare_process_work_dir

_FALLBACK_HEVC_FRAME_RATE = "24000/1001"


# =============================================================================
# Types de données
# =============================================================================

class DoviProfile(Enum):
    """
    Profil Dolby Vision cible pour l'injection RPU.

    La valeur de l'enum est le flag `-m` passé en argument GLOBAL à dovi_tool
    (avant la sous-commande inject-rpu).

    Modes :
        P8_1 = "2"  → normalise le RPU en Profile 8.1, supprime le mapping FEL.
                       Standard pour les remux UHD Blu-ray. Recommandé.
        P8_0 = "0"  → copie le RPU sans modification (rewrite untouched).
                       Préserve le profil source tel quel.
    """
    P8_1 = "2"   # -m 2 : conversion Profile 8.1 (standard remux UHD)
    P8_0 = "0"   # -m 0 : copie brute sans conversion


class WorkflowStep(Enum):
    """Étapes du workflow dans l'ordre d'exécution."""
    VALIDATION        = auto()   # Vérifications préliminaires
    FRAME_COUNT       = auto()   # Comparaison des frame counts
    EXTRACT_PARALLEL  = auto()   # Extractions parallèles (HEVC + RPU + HDR10+)
    INJECT_DOVI       = auto()   # Injection RPU DoVi
    INJECT_HDR10PLUS  = auto()   # Injection HDR10+
    VERIFY            = auto()   # Vérification intégrité RPU frames
    REMUX             = auto()   # Remuxage final MKV
    CLEANUP           = auto()   # Nettoyage des fichiers intermédiaires


@dataclass
class FrameCountResult:
    """Résultat de la comparaison des frame counts des deux fichiers."""
    fc1: int | None
    fc2: int | None
    diff: int | None

    @property
    def compatible(self) -> bool:
        if self.diff is None:
            return False
        return self.diff <= 4

    @property
    def warning(self) -> bool:
        """True si l'écart est tolérable mais non nul."""
        return self.diff is not None and 0 < self.diff <= 4

    @property
    def status_text(self) -> str:
        if self.fc1 is None or self.fc2 is None:
            return "Frame count illisible"
        if self.diff == 0:
            return f"{self.fc1} frames — identiques ✓"
        if self.warning:
            return f"{self.fc1} / {self.fc2} frames — écart {self.diff} (tolérable)"
        return f"{self.fc1} / {self.fc2} frames — écart {self.diff} (incompatible)"


@dataclass
class HDRFlags:
    """Résultat de la détection des formats HDR dans Film 2."""
    has_dovi: bool = False
    has_hdr10plus: bool = False

    @property
    def label(self) -> str:
        parts = []
        if self.has_dovi:
            parts.append("Dolby Vision")
        if self.has_hdr10plus:
            parts.append("HDR10+")
        return " + ".join(parts) if parts else "Aucun HDR avancé détecté"


@dataclass
class StepResult:
    """Résultat d'une étape du workflow."""
    step:     WorkflowStep
    success:  bool
    message:  str
    duration: float = 0.0
    detail:   str   = ""


# =============================================================================
# Configuration interne du workflow
# =============================================================================

@dataclass
class _WorkflowPaths:
    """Chemins intermédiaires utilisés pendant le workflow."""
    work_dir:         Path
    film1:            Path   # Chemin original Film 1 (MKV ou HEVC brut)
    film1_hevc:       Path   # HEVC extrait de Film 1 (uniquement si Film 1 est MKV)
    film2_hevc:       Path   # HEVC temporaire Film 2 (cas 2 : double extraction)
    film2_rpu:        Path   # RPU DoVi extrait de Film 2
    film2_hdr10plus:  Path   # Métadonnées HDR10+ extraites de Film 2
    film1_with_dovi:  Path   # Film 1 + RPU DoVi injecté
    film1_final:      Path   # Film 1 + RPU DoVi + HDR10+ (résultat final HEVC)
    film1_wrapped_video: Path  # Encapsulation MKV de la vidéo injectée (PTS reconstruit)
    output_mkv:       Path   # Fichier de sortie final

    @classmethod
    def from_config(
        cls,
        work_dir: Path,
        output_dir: Path,
        film1: Path,
        basename: str,
    ) -> "_WorkflowPaths":
        return cls(
            work_dir        = work_dir,
            film1           = film1,
            film1_hevc      = work_dir / "film1.hevc",
            film2_hevc      = work_dir / "film2.hevc",
            film2_rpu       = work_dir / "film2_rpu.bin",
            film2_hdr10plus = work_dir / "film2_hdr10plus.json",
            film1_with_dovi = work_dir / "film1_with_dovi.hevc",
            film1_final     = work_dir / "film1_final.hevc",
            film1_wrapped_video = work_dir / "film1_wrapped_video.mkv",
            output_mkv      = output_dir / f"{basename}.mkv",
        )

    @property
    def film1_hevc_input(self) -> Path:
        """
        Chemin HEVC en entrée des outils d'injection.
        Si Film 1 est MKV  → film1_hevc (extrait durant l'étape EXTRACT_PARALLEL).
        Si Film 1 est HEVC → film1 directement (pas d'extraction nécessaire).
        """
        return self.film1_hevc if self.film1.suffix.lower() == ".mkv" else self.film1

    def injection_chain_final(self, flags: HDRFlags) -> Path:
        """Fichier HEVC final à muxer selon les opérations effectuées."""
        if flags.has_dovi and flags.has_hdr10plus:
            return self.film1_final
        if flags.has_dovi:
            return self.film1_with_dovi
        return self.film1_final  # HDR10+ seul


# =============================================================================
# Exceptions
# =============================================================================

class WorkflowError(RuntimeError):
    """Erreur bloquante pendant le workflow."""
    def __init__(self, step: WorkflowStep, message: str) -> None:
        self.step    = step
        self.message = message
        super().__init__(f"[{step.name}] {message}")


# =============================================================================
# MergeDoviWorkflow
# =============================================================================

class MergeDoviWorkflow(QObject):
    """
    Orchestrateur du workflow d'injection DoVi RPU + HDR10+.

    Encapsule la logique de merge_dovi_hdr10plus.py en un QObject émettant
    des signaux Qt pour chaque étape. Toutes les opérations lourdes s'exécutent
    dans des threads secondaires via ThreadPoolExecutor.

    Usage :
        wf = MergeDoviWorkflow(config)
        wf.step_started.connect(on_step_started)
        wf.step_finished.connect(on_step_finished)
        wf.workflow_finished.connect(on_done)
        wf.workflow_failed.connect(on_error)
        wf.start(film1, film2, dovi_profile=DoviProfile.P8_1)

    Arrêt :
        wf.cancel()   — demande l'arrêt propre après l'étape en cours
    """

    # --- Signaux ---
    step_started    = Signal(object)          # WorkflowStep
    step_progress   = Signal(object, str)     # WorkflowStep, message
    step_finished   = Signal(object, object)  # WorkflowStep, StepResult
    workflow_finished = Signal(str)           # chemin du fichier de sortie
    workflow_failed   = Signal(object, str)   # WorkflowStep, message d'erreur

    def __init__(
        self,
        mediainfo_bin:    str = "mediainfo",
        ffmpeg_bin:       str = "ffmpeg",
        ffprobe_bin:      str = "ffprobe",
        dovi_tool_bin:    str = "dovi_tool",
        hdr10plus_bin:    str = "hdr10plus_tool",
        max_workers:      int = 4,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._bins = {
            "mediainfo":     mediainfo_bin,
            "ffmpeg":        ffmpeg_bin,
            "ffprobe":       ffprobe_bin,
            "dovi_tool":     dovi_tool_bin,
            "hdr10plus_tool": hdr10plus_bin,
        }
        self._max_workers    = max_workers
        self._cancelled      = False

    # ------------------------------------------------------------------
    # API publique
    # ------------------------------------------------------------------

    def start(
        self,
        film1:        Path,
        film2:        Path,
        work_dir:     Path,
        output_dir:   Path,
        dovi_profile: DoviProfile = DoviProfile.P8_1,
        output_basename: str | None = None,
    ) -> None:
        """Lance le workflow dans un thread secondaire."""
        self._cancelled = False
        basename = output_basename or f"{film1.stem}_DOVI_HDR10PLUS"
        output_path = output_dir / f"{basename}.mkv"
        process_work_dir = prepare_process_work_dir(
            work_dir,
            output_path=output_path,
            fallback_name="dovi_job",
        )
        paths = _WorkflowPaths.from_config(process_work_dir, output_dir, film1, basename)

        outer = ThreadPoolExecutor(max_workers=1)
        outer.submit(self._run, film1, film2, paths, dovi_profile)
        outer.shutdown(wait=False)

    def cancel(self) -> None:
        """
        Demande l'annulation propre du workflow.
        L'étape en cours se termine, puis _check_cancel() lève _CancelledError.
        Les sous-processus déjà lancés ne sont pas interrompus de force.
        """
        self._cancelled = True

    # ------------------------------------------------------------------
    # Orchestration principale (thread secondaire)
    # ------------------------------------------------------------------

    def _run(
        self,
        film1: Path,
        film2: Path,
        paths: _WorkflowPaths,
        profile: DoviProfile,
    ) -> None:
        try:
            paths.work_dir.mkdir(parents=True, exist_ok=True)
            paths.output_mkv.parent.mkdir(parents=True, exist_ok=True)

            # 1 — Validation
            flags = self._step_validate(film1, film2)
            self._check_cancel()

            # 2 — Frame count (non bloquant si écart ≤ 4)
            self._step_framecount(film1, film2)
            self._check_cancel()

            # 3 — Extractions parallèles
            self._step_extract(film1, film2, paths, flags)
            self._check_cancel()

            # 4 — Injection DoVi
            if flags.has_dovi:
                self._step_inject_dovi(paths, flags, profile)
                self._check_cancel()

            # 5 — Injection HDR10+
            if flags.has_hdr10plus:
                self._step_inject_hdr10plus(paths, flags)
                self._check_cancel()

            # 6 — Vérification (RPU si DoVi + cohérence framecount du flux final injecté)
            self._step_verify(film1, paths, flags)
            self._check_cancel()

            # 7 — Remuxage
            self._step_remux(film1, paths, flags)
            self._check_cancel()

            # 8 — Nettoyage
            self._step_cleanup(paths)

            self.workflow_finished.emit(str(paths.output_mkv))

        except WorkflowError as exc:
            self.workflow_failed.emit(exc.step, exc.message)
        except _CancelledError:
            self.workflow_failed.emit(WorkflowStep.VALIDATION, "Workflow annulé.")

    def _check_cancel(self) -> None:
        if self._cancelled:
            raise _CancelledError()

    # ------------------------------------------------------------------
    # Étape 1 — Validation préliminaire
    # ------------------------------------------------------------------

    def _step_validate(self, film1: Path, film2: Path) -> HDRFlags:
        step = WorkflowStep.VALIDATION
        t0   = time.monotonic()
        self.step_started.emit(step)

        # Fichiers présents
        for label, path in [("Film 1", film1), ("Film 2", film2)]:
            if not path.is_file():
                raise WorkflowError(step, f"{label} introuvable : {path}")
            self.step_progress.emit(step, f"{label} trouvé : {path.name}")

        # Extensions supportées
        for label, path in [("Film 1", film1), ("Film 2", film2)]:
            if path.suffix.lower() not in (".mkv", ".hevc"):
                raise WorkflowError(
                    step,
                    f"{label} : format non supporté '{path.suffix}' (accepté : .mkv, .hevc)",
                )

        # Outils disponibles
        missing = [n for n, cmd in self._bins.items() if not shutil.which(cmd)]
        if missing:
            raise WorkflowError(step, f"Outils manquants : {', '.join(missing)}")
        self.step_progress.emit(step, "Tous les outils sont disponibles.")

        # Flux HEVC présents
        for label, path in [("Film 1", film1), ("Film 2", film2)]:
            codec = self._mediainfo(path, "Video;%Format%").strip().upper()
            if codec != "HEVC":
                raise WorkflowError(step, f"{label} ne contient pas de flux HEVC ({codec})")
            self.step_progress.emit(step, f"{label} — flux HEVC confirmé.")

        # Détection HDR dans Film 2
        hdr_raw = self._mediainfo(film2, "Video;%HDR_Format%").strip()
        flags   = HDRFlags(
            has_dovi      = "dolby vision" in hdr_raw.lower(),
            has_hdr10plus = "smpte st 2094" in hdr_raw.lower(),
        )
        if not flags.has_dovi and not flags.has_hdr10plus:
            raise WorkflowError(step, "Film 2 ne contient ni Dolby Vision ni HDR10+.")
        self.step_progress.emit(step, f"HDR détecté dans Film 2 : {flags.label}")

        duration = time.monotonic() - t0
        self.step_finished.emit(step, StepResult(step, True, flags.label, duration))
        return flags

    # ------------------------------------------------------------------
    # Étape 2 — Comparaison frame counts
    # ------------------------------------------------------------------

    def _step_framecount(self, film1: Path, film2: Path) -> FrameCountResult:
        step = WorkflowStep.FRAME_COUNT
        t0   = time.monotonic()
        self.step_started.emit(step)

        fc1 = self._get_framecount(film1)
        fc2 = self._get_framecount(film2)

        diff = abs(fc2 - fc1) if fc1 is not None and fc2 is not None else None
        result = FrameCountResult(fc1, fc2, diff)

        self.step_progress.emit(step, f"Film 1 : {fc1} frames  |  Film 2 : {fc2} frames")

        if diff is not None and diff > 4:
            raise WorkflowError(
                step,
                f"Écart de {diff} frames trop important — "
                "les deux fichiers ne semblent pas être le même contenu.",
            )

        duration = time.monotonic() - t0
        self.step_finished.emit(
            step, StepResult(step, True, result.status_text, duration)
        )
        return result

    # ------------------------------------------------------------------
    # Étape 3 — Extractions parallèles
    # ------------------------------------------------------------------

    def _step_extract(
        self,
        film1: Path,
        film2: Path,
        paths: _WorkflowPaths,
        flags: HDRFlags,
    ) -> None:
        step = WorkflowStep.EXTRACT_PARALLEL
        t0   = time.monotonic()
        self.step_started.emit(step)

        film1_is_mkv = film1.suffix.lower() == ".mkv"
        film2_is_mkv = film2.suffix.lower() == ".mkv"
        both_needed  = flags.has_dovi and flags.has_hdr10plus

        errors: list[str] = []

        def _emit(msg: str) -> None:
            self.step_progress.emit(step, msg)

        if both_needed and film2_is_mkv:
            # ── Cas 2 : Phase 1 (film1.hevc + film2.hevc) puis Phase 2 (RPU + HDR10+) ──
            phase1: dict[str, Callable] = {}
            if film1_is_mkv:
                phase1["HEVC Film 1"] = lambda: self._extract_hevc(film1, paths.film1_hevc, _emit)
            phase1["HEVC Film 2"] = lambda: self._extract_hevc(film2, paths.film2_hevc, _emit)

            _emit(f"Phase 1 — extraction HEVC ({', '.join(phase1)})…")
            self._run_pool(phase1, errors)

            if errors:
                raise WorkflowError(step, "Phase 1 échouée :\n" + "\n".join(errors))

            source2 = paths.film2_hevc
            phase2: dict[str, Callable] = {}
            if flags.has_dovi:
                phase2["RPU DoVi"] = lambda: self._extract_rpu(source2, paths.film2_rpu, _emit)
            if flags.has_hdr10plus:
                phase2["HDR10+"]   = lambda: self._extract_hdr10plus(source2, paths.film2_hdr10plus, _emit)

            _emit(f"Phase 2 — extraction métadonnées ({', '.join(phase2)})…")
            self._run_pool(phase2, errors)

        else:
            # ── Cas 1 : tout en parallèle ──
            source2 = film2
            tasks: dict[str, Callable] = {}
            if film1_is_mkv:
                tasks["HEVC Film 1"] = lambda: self._extract_hevc(film1, paths.film1_hevc, _emit)
            if flags.has_dovi:
                tasks["RPU DoVi"] = lambda: self._extract_rpu(source2, paths.film2_rpu, _emit)
            if flags.has_hdr10plus:
                tasks["HDR10+"]   = lambda: self._extract_hdr10plus(source2, paths.film2_hdr10plus, _emit)

            _emit(f"Extraction parallèle ({', '.join(tasks)})…")
            self._run_pool(tasks, errors)

        if errors:
            raise WorkflowError(step, "Extraction échouée :\n" + "\n".join(errors))

        duration = time.monotonic() - t0
        self.step_finished.emit(
            step,
            StepResult(step, True, "Extractions terminées", duration),
        )

    # ------------------------------------------------------------------
    # Étape 4 — Injection DoVi RPU
    # ------------------------------------------------------------------

    def _step_inject_dovi(
        self,
        paths: _WorkflowPaths,
        flags: HDRFlags,
        profile: DoviProfile,
    ) -> None:
        step = WorkflowStep.INJECT_DOVI
        t0   = time.monotonic()
        self.step_started.emit(step)
        self.step_progress.emit(step, f"Injection RPU DoVi (dovi_tool -m {profile.value})…")

        # film1_hevc_input résout automatiquement MKV extrait vs HEVC direct
        hevc_input = paths.film1_hevc_input
        if not hevc_input.exists():
            raise WorkflowError(
                step,
                f"Fichier HEVC source introuvable : {hevc_input.name} — "
                "vérifiez que l'étape d'extraction s'est bien déroulée.",
            )

        # -m est un flag GLOBAL de dovi_tool placé avant la sous-commande.
        # La valeur de DoviProfile.value est directement le flag -m à passer.
        # P8_1 → -m 2 (normalise en Profile 8.1, supprime mapping FEL)
        # P8_0 → -m 0 (rewrite untouched, préserve le profil source)
        self._run_cmd([
            self._bins["dovi_tool"],
            "-m", profile.value,
            "inject-rpu",
            "-i", str(hevc_input),
            "-r", str(paths.film2_rpu),
            "-o", str(paths.film1_with_dovi),
        ], step)

        self.step_progress.emit(step, f"RPU injecté → {paths.film1_with_dovi.name}")
        duration = time.monotonic() - t0
        mode_label = "Profile 8.1 (standard remux)" if profile == DoviProfile.P8_1 else "mode 0 (untouched)"
        self.step_finished.emit(
            step, StepResult(step, True, f"DoVi RPU injecté — {mode_label}", duration)
        )

    # ------------------------------------------------------------------
    # Étape 5 — Injection HDR10+
    # ------------------------------------------------------------------

    def _step_inject_hdr10plus(
        self,
        paths: _WorkflowPaths,
        flags: HDRFlags,
    ) -> None:
        step = WorkflowStep.INJECT_HDR10PLUS
        t0   = time.monotonic()
        self.step_started.emit(step)
        self.step_progress.emit(step, "Injection HDR10+…")

        # Si DoVi a déjà été injecté → partir de film1_with_dovi.
        # Sinon (HDR10+ seul) → film1_hevc_input résout MKV extrait vs HEVC direct.
        hevc_input = paths.film1_with_dovi if flags.has_dovi else paths.film1_hevc_input

        self._run_cmd([
            self._bins["hdr10plus_tool"],
            "inject",
            "-i", str(hevc_input),
            "-j", str(paths.film2_hdr10plus),
            "-o", str(paths.film1_final),
        ], step)

        self.step_progress.emit(step, f"HDR10+ injecté → {paths.film1_final.name}")
        duration = time.monotonic() - t0
        self.step_finished.emit(
            step, StepResult(step, True, "HDR10+ injecté", duration)
        )

    # ------------------------------------------------------------------
    # Étape 6 — Vérification intégrité RPU
    # ------------------------------------------------------------------

    def _step_verify(
        self,
        film1: Path,
        paths: _WorkflowPaths,
        flags: HDRFlags,
    ) -> None:
        step = WorkflowStep.VERIFY
        t0   = time.monotonic()
        self.step_started.emit(step)

        final = paths.injection_chain_final(flags)
        fc1 = self._get_framecount(film1)
        fc_final = self._get_framecount(final)

        if fc1 is not None and fc_final is not None:
            diff_final = abs(fc_final - fc1)
            if diff_final > 4:
                raise WorkflowError(
                    step,
                    f"Désalignement critique du flux injecté : {fc_final} frames pour {fc1} frames vidéo.",
                )
            if diff_final > 0:
                self.step_progress.emit(
                    step,
                    f"Flux injecté vs Film 1 : écart {diff_final} frames — tolérable.",
                )

        rpu_frames: int | None = None
        if flags.has_dovi:
            self.step_progress.emit(step, f"Vérification RPU frames dans {final.name}…")
            try:
                result = subprocess.run(
                    [self._bins["dovi_tool"], "info", "-i", str(final)],
                    capture_output=True, check=False, **subprocess_text_kwargs(),
                )
                raw = result.stdout + result.stderr
            except Exception as exc:
                self.step_progress.emit(step, f"dovi_tool info indisponible : {exc}")
                raw = ""

            m = re.search(r"rpu frames[^\d]*(\d+)", raw, re.IGNORECASE)
            if m:
                rpu_frames = int(m.group(1))

            if rpu_frames is not None and fc1 is not None:
                diff = abs(rpu_frames - fc1)
                if diff > 4:
                    raise WorkflowError(
                        step,
                        f"Désalignement critique : {rpu_frames} RPU frames pour {fc1} frames vidéo.",
                    )
                if diff > 0:
                    self.step_progress.emit(step, f"RPU vs Film 1 : écart {diff} frames — tolérable.")

        detail = (
            f"Flux final : {fc_final if fc_final is not None else '?'}  |  "
            f"Film 1 : {fc1 if fc1 is not None else '?'}  |  "
            f"RPU : {rpu_frames if rpu_frames is not None else '-'}"
        )
        self.step_progress.emit(step, detail)

        duration = time.monotonic() - t0
        self.step_finished.emit(
            step, StepResult(step, True, detail, duration)
        )

    # ------------------------------------------------------------------
    # Étape 7 — Remuxage final
    # ------------------------------------------------------------------

    def _step_remux(
        self,
        film1: Path,
        paths: _WorkflowPaths,
        flags: HDRFlags,
    ) -> None:
        step = WorkflowStep.REMUX
        t0   = time.monotonic()
        self.step_started.emit(step)

        final_hevc = paths.injection_chain_final(flags)
        if not final_hevc.exists():
            raise WorkflowError(step, f"Flux injecté introuvable : {final_hevc.name}")

        fps_expr = self._source_video_fps_expr(film1)
        self.step_progress.emit(
            step,
            f"Encapsulation vidéo injectée (FPS source: {fps_expr}) → {paths.film1_wrapped_video.name}…",
        )

        self._run_cmd([
            self._bins["ffmpeg"],
            "-hide_banner",
            "-y",
            "-f", "hevc",
            "-framerate", fps_expr,
            "-i", str(final_hevc),
            "-map", "0:v:0",
            "-c:v", "copy",
            "-bsf:v", f"setts=pts=N/({fps_expr}*TB)",
            str(paths.film1_wrapped_video),
        ], step)

        self.step_progress.emit(step, f"Reconstruction conteneur final → {paths.output_mkv.name}…")
        self._run_cmd([
            self._bins["ffmpeg"],
            "-hide_banner",
            "-y",
            "-i", str(paths.film1_wrapped_video),
            "-i", str(film1),
            "-map", "0:v:0",
            "-map", "1:a?",
            "-map", "1:s?",
            "-map", "1:t?",
            "-map", "1:d?",
            "-c:v", "copy",
            "-c:a", "copy",
            "-c:s", "copy",
            "-c:t", "copy",
            "-c:d", "copy",
            "-map_metadata", "1",
            "-map_chapters", "1",
            str(paths.output_mkv),
        ], step)

        size_mb = paths.output_mkv.stat().st_size / (1024 ** 2)
        duration = time.monotonic() - t0
        self.step_finished.emit(
            step,
            StepResult(
                step, True,
                f"{paths.output_mkv.name}  ({size_mb:.0f} Mo)",
                duration,
            ),
        )

    # ------------------------------------------------------------------
    # Étape 8 — Nettoyage
    # ------------------------------------------------------------------

    def _step_cleanup(self, paths: _WorkflowPaths) -> None:
        step = WorkflowStep.CLEANUP
        t0   = time.monotonic()
        self.step_started.emit(step)

        for path in [
            paths.film1_hevc,
            paths.film2_hevc,
            paths.film2_rpu,
            paths.film2_hdr10plus,
            paths.film1_with_dovi,
            paths.film1_final,
            paths.film1_wrapped_video,
        ]:
            if path.exists():
                path.unlink()
                self.step_progress.emit(step, f"Supprimé : {path.name}")

        try:
            paths.work_dir.rmdir()
        except OSError:
            pass

        duration = time.monotonic() - t0
        self.step_finished.emit(
            step, StepResult(step, True, "Fichiers intermédiaires supprimés", duration)
        )

    # ------------------------------------------------------------------
    # Tâches d'extraction (utilisées dans le pool)
    # ------------------------------------------------------------------

    def _extract_hevc(
        self, source: Path, dest: Path, emit: Callable[[str], None]
    ) -> str:
        emit(f"Extraction HEVC : {source.name} → {dest.name}…")
        self._run_raw([
            self._bins["ffmpeg"],
            "-hide_banner",
            "-y",
            "-i", str(source),
            "-map", "0:v:0",
            "-c:v", "copy",
            "-an",
            "-sn",
            "-dn",
            "-f", "hevc",
            str(dest),
        ])
        return f"HEVC extrait → {dest.name}"

    def _extract_rpu(
        self, source: Path, dest: Path, emit: Callable[[str], None]
    ) -> str:
        emit(f"Extraction RPU DoVi : {source.name}…")
        self._run_raw([
            self._bins["dovi_tool"], "extract-rpu",
            "-i", str(source), "-o", str(dest),
        ])
        return f"RPU extrait → {dest.name}"

    def _extract_hdr10plus(
        self, source: Path, dest: Path, emit: Callable[[str], None]
    ) -> str:
        emit(f"Extraction HDR10+ : {source.name}…")
        self._run_raw([
            self._bins["hdr10plus_tool"], "extract",
            str(source), "-o", str(dest),
        ])
        return f"HDR10+ extrait → {dest.name}"

    # ------------------------------------------------------------------
    # Pool d'exécution parallèle
    # ------------------------------------------------------------------

    def _run_pool(
        self,
        tasks: dict[str, Callable],
        errors: list[str],
    ) -> None:
        """
        Exécute les callables en parallèle via ThreadPoolExecutor.
        Collecte les erreurs dans `errors`. Vérifie le flag d'annulation
        entre chaque future complétée pour permettre un arrêt propre.
        Ne fait rien si `tasks` est vide (ThreadPoolExecutor(max_workers=0)
        lèverait ValueError).
        """
        if not tasks:
            return
        with ThreadPoolExecutor(max_workers=min(len(tasks), self._max_workers)) as executor:
            futures: dict[Future, str] = {
                executor.submit(fn): label
                for label, fn in tasks.items()
            }
            for future in as_completed(futures):
                label = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    errors.append(f"[{label}] {exc}")
                # Vérifier l'annulation après chaque tâche terminée
                if self._cancelled:
                    break

    # ------------------------------------------------------------------
    # Helpers subprocess
    # ------------------------------------------------------------------

    def _run_cmd(self, cmd: list[str], step: WorkflowStep) -> str:
        """
        Lance une commande, émet des lignes de progression, lève WorkflowError
        si le code de retour est non nul.
        """
        try:
            with subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                **subprocess_text_kwargs(),
            ) as proc:
                lines: list[str] = []
                assert proc.stdout is not None
                for line in proc.stdout:
                    stripped = line.rstrip("\n")
                    if stripped:
                        lines.append(stripped)
                        self.step_progress.emit(step, stripped)
                proc.wait()
                output = "\n".join(lines)
                if proc.returncode != 0:
                    raise WorkflowError(
                        step,
                        f"Commande échouée (code {proc.returncode}) : {' '.join(cmd[:2])}\n"
                        + output[-1000:],
                    )
                return output
        except WorkflowError:
            raise
        except FileNotFoundError:
            raise WorkflowError(step, f"Outil introuvable : {cmd[0]}")

    def _run_raw(self, cmd: list[str]) -> str:
        """
        Lance une commande dans un worker (pas de signal Qt possible depuis
        un thread pool arbitraire). Lève RuntimeError si échec.
        """
        result = subprocess.run(
            cmd,
            capture_output=True,
            check=False,
            **subprocess_text_kwargs(),
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Commande échouée (code {result.returncode}) : {' '.join(cmd[:2])}\n"
                + (result.stdout + result.stderr)[-500:]
            )
        return result.stdout

    # ------------------------------------------------------------------
    # Helpers mediainfo
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_frame_rate_expr(value: object) -> str | None:
        raw = str(value or "").strip()
        if raw in {"", "0", "0/0", "N/A"}:
            return None
        if re.fullmatch(r"\d+(?:\.\d+)?", raw):
            return raw
        if re.fullmatch(r"\d+/\d+", raw):
            return raw
        return None

    def _source_video_fps_expr(self, source: Path) -> str:
        cmd = [
            self._bins["ffprobe"],
            "-v", "quiet",
            "-print_format", "json",
            "-show_streams",
            str(source),
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                check=False,
                **subprocess_text_kwargs(),
            )
        except Exception:
            return _FALLBACK_HEVC_FRAME_RATE
        if result.returncode != 0:
            return _FALLBACK_HEVC_FRAME_RATE
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return _FALLBACK_HEVC_FRAME_RATE
        streams = payload.get("streams")
        if not isinstance(streams, list):
            return _FALLBACK_HEVC_FRAME_RATE
        for stream in streams:
            if not isinstance(stream, dict):
                continue
            if stream.get("codec_type") != "video":
                continue
            for key in ("avg_frame_rate", "r_frame_rate"):
                fps_expr = self._normalize_frame_rate_expr(stream.get(key))
                if fps_expr is not None:
                    return fps_expr
            break
        return _FALLBACK_HEVC_FRAME_RATE

    def _mediainfo(self, path: Path, inform: str) -> str:
        """Lance mediainfo --Inform et retourne la sortie brute."""
        result = subprocess.run(
            [self._bins["mediainfo"], f"--Inform={inform}", str(path)],
            capture_output=True, check=False, **subprocess_text_kwargs(),
        )
        return result.stdout

    def _get_framecount(self, path: Path) -> int | None:
        """Retourne le frame count via mediainfo, ou None si illisible."""
        raw = self._mediainfo(path, "Video;%FrameCount%").strip()
        if re.fullmatch(r"\d+", raw):
            return int(raw)
        return None

    # ------------------------------------------------------------------
    # Utilitaire statique
    # ------------------------------------------------------------------

    @staticmethod
    def required_tools() -> list[str]:
        return ["mediainfo", "ffmpeg", "ffprobe", "dovi_tool", "hdr10plus_tool"]


class _CancelledError(Exception):
    """Levée en interne pour signaler une annulation."""
    pass
