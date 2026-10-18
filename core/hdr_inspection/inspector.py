"""Inspection HDR : instantané (niveau 1) puis métadonnées dynamiques complètes (niveau 2)."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from core.bluray import append_ffmpeg_input_args
from core.hdr_inspection.audit import Finding, audit
from core.hdr_inspection.cache import InspectionCache
from core.hdr_inspection.dovi import DoviAnalysis, parse_dovi_export
from core.hdr_inspection.hdr10p import Hdr10PlusAnalysis, parse_hdr10plus_json
from core.hdr_inspection.snapshot import HdrSnapshot, build_snapshot
from core.i18n import translate_text
from core.inspector import FileInfo
from core.matroska.reader import strict_demuxer_reads_tracks
from core.pipeline_command import PipelineCommand
from core.runner import TaskCancelledError

#: Lance une commande (liste ou ``PipelineCommand``), lève en cas d'échec ou d'annulation.
RunCommand = Callable[[object], object]
#: Début d'étape : libellé, début et part de la progression globale (0–1).
StepCallback = Callable[[str, float, float], None]
#: Conteneurs lus directement par dovi_tool / hdr10plus_tool (premier flux seulement).
_DIRECT_SUFFIXES = {".mkv", ".hevc", ".h265", ".265", ".x265"}


class InspectionCancelled(Exception):
    """Analyse annulée par l'utilisateur."""


@dataclass(frozen=True, eq=False)
class InspectionReport:
    snapshot: HdrSnapshot
    dovi: DoviAnalysis | None = None
    hdr10plus: Hdr10PlusAnalysis | None = None
    findings: tuple[Finding, ...] = ()
    #: Métadonnées dynamiques lues (niveau 2).
    dynamic: bool = False
    from_cache: bool = False
    errors: tuple[str, ...] = ()


def snapshot_report(info: FileInfo, stream_index: int | None = None) -> InspectionReport:
    """Niveau 1 : en-têtes seuls, instantané."""
    snapshot = build_snapshot(info, stream_index)
    return InspectionReport(snapshot, findings=audit(snapshot))


class DynamicInspector:
    """Niveau 2 : RPU et HDR10+ extraits du fichier puis analysés, sans décoder l'image."""

    def __init__(
        self,
        *,
        ffmpeg_bin: str,
        dovi_tool_bin: str,
        hdr10plus_bin: str,
        run_cmd: RunCommand,
        work_dir: Path,
        cache: InspectionCache | None = None,
        on_step: StepCallback | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> None:
        self._ffmpeg = ffmpeg_bin
        self._dovi_tool = dovi_tool_bin
        self._hdr10plus = hdr10plus_bin
        self._run = run_cmd
        self._work = Path(work_dir)
        self._cache = cache
        self._on_step = on_step
        self._cancelled = cancelled or (lambda: False)

    def _step(self, label: str, start: float, span: float) -> None:
        if self._cancelled():
            raise InspectionCancelled
        if self._on_step is not None:
            self._on_step(label, start, span)

    def analyze(self, info: FileInfo, stream_index: int | None = None, *, refresh: bool = False) -> InspectionReport:
        snapshot = build_snapshot(info, stream_index)
        path = Path(snapshot.path)
        if self._cache is not None and not refresh:
            cached = self._cache.load(path, snapshot.stream_index)
            if cached is not None:
                dovi, hdr10plus = cached
                return InspectionReport(snapshot, dovi, hdr10plus, audit(snapshot, dovi, hdr10plus),
                                        dynamic=True, from_cache=True)
        wants_dovi = snapshot.dovi is not None
        wants_hdr10plus = snapshot.hdr10plus
        span = 1.0 / max(1, int(wants_dovi) + int(wants_hdr10plus))
        errors: list[str] = []
        dovi = hdr10plus = None
        try:
            start = 0.0
            if wants_dovi:
                try:
                    dovi = self._dovi(path, snapshot.stream_index, start, span)
                except (InspectionCancelled, TaskCancelledError, InterruptedError):
                    raise
                except Exception as exc:
                    errors.append(translate_text("RPU Dolby Vision illisible : {error}", error=str(exc)[-500:]))
                start += span
            if wants_hdr10plus:
                try:
                    hdr10plus = self._hdr10p(path, snapshot.stream_index, start, span)
                except (InspectionCancelled, TaskCancelledError, InterruptedError):
                    raise
                except Exception as exc:
                    errors.append(translate_text("HDR10+ illisible : {error}", error=str(exc)[-500:]))
        except (TaskCancelledError, InterruptedError) as exc:
            raise InspectionCancelled from exc
        if self._cache is not None and not errors:
            self._cache.store(path, snapshot.stream_index, dovi, hdr10plus)
        return InspectionReport(snapshot, dovi, hdr10plus, audit(snapshot, dovi, hdr10plus),
                                dynamic=True, errors=tuple(errors))

    def _extract(self, path: Path, stream_index: int, direct: list[str], piped: list[str]) -> None:
        """Lecture directe par l'outil si possible, sinon flux Annex B de FFmpeg en pipe (aucun fichier HEVC)."""
        suffix = path.suffix.lower()
        if stream_index == 0 and suffix in _DIRECT_SUFFIXES and (suffix != ".mkv" or strict_demuxer_reads_tracks(path)):
            try:
                self._run(direct)
                return
            except Exception:
                if suffix != ".mkv":
                    raise  # autre limite du lecteur Matroska des outils : repli en pipe
        decode = [self._ffmpeg, "-nostdin", "-loglevel", "error"]
        append_ffmpeg_input_args(decode, path)
        decode += ["-map", f"0:{stream_index}", "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", "-"]
        self._run(PipelineCommand(piped, [decode]))

    def _dovi(self, path: Path, stream_index: int, start: float, span: float) -> DoviAnalysis:
        rpu = self._work / "inspection.rpu"
        export = self._work / "inspection_rpu.json"
        try:
            self._step(translate_text("Extraction du RPU Dolby Vision…"), start, span * 0.9)
            self._extract(path, stream_index,
                          [self._dovi_tool, "extract-rpu", "-i", str(path), "-o", str(rpu)],
                          [self._dovi_tool, "extract-rpu", "-", "-o", str(rpu)])
            self._step(translate_text("Analyse du RPU…"), start + span * 0.9, span * 0.1)
            self._run([self._dovi_tool, "export", "-i", str(rpu), "-d", f"all={export}"])
            rpu.unlink(missing_ok=True)
            return parse_dovi_export(export, cancelled=self._cancelled)
        finally:
            rpu.unlink(missing_ok=True)
            export.unlink(missing_ok=True)

    def _hdr10p(self, path: Path, stream_index: int, start: float, span: float) -> Hdr10PlusAnalysis:
        output = self._work / "inspection_hdr10plus.json"
        try:
            self._step(translate_text("Extraction du HDR10+…"), start, span * 0.9)
            self._extract(path, stream_index,
                          [self._hdr10plus, "extract", str(path), "-o", str(output)],
                          [self._hdr10plus, "extract", "-o", str(output), "-"])
            self._step(translate_text("Analyse du HDR10+…"), start + span * 0.9, span * 0.1)
            return parse_hdr10plus_json(output, cancelled=self._cancelled)
        finally:
            output.unlink(missing_ok=True)
