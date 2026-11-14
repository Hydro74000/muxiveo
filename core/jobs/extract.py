"""Extraction de pistes, de couches Dolby Vision / HDR10+ et de pièces jointes (CLI, service, panneau Conteneur).

Chaque sortie est écrite dans un candidat ``.partial`` réservé à côté de la destination, puis publiée
sans écrasement imprévu (``core.output_commit``) ; un échec ou une annulation laisse la destination intacte.
Couches d'une piste vidéo HEVC : ``stream`` (la piste elle-même), ``el`` / ``bl`` (couches d'un Dolby Vision
profil 7, ``dovi_tool demux``), ``rpu`` (``dovi_tool extract-rpu``), ``hdr10plus`` (``hdr10plus_tool extract``).
"""

from __future__ import annotations

import contextlib
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from core.bluray import append_ffmpeg_input_args, ffmpeg_input_args
from core.extractor import ExtractPlan, TrackExtractor
from core.inspector import FileInfo, HDRType
from core.jobs.constants import EXIT_ARGS, EXIT_EXISTS, EXIT_TOOL, EXIT_VALIDATION, EXIT_WORKFLOW
from core.jobs.errors import JobError
from core.output_commit import (
    OutputBusyError,
    OutputChangedError,
    OutputCommitError,
    OutputReservation,
    reserve_candidate,
)
from core.pipeline_command import PipelineCommand
from core.service.context import CancelToken
from core.service.errors import ApiError, ErrorCode

LAYERS: tuple[str, ...] = ("stream", "el", "bl", "rpu", "hdr10plus")
_LAYER_SUFFIX = {"el": ".el.hevc", "bl": ".bl.hevc", "rpu": ".rpu.bin", "hdr10plus": ".hdr10plus.json"}
LAYER_LABELS = {"el": "Couche d'amélioration Dolby Vision", "bl": "Couche de base Dolby Vision",
                "rpu": "RPU Dolby Vision", "hdr10plus": "Métadonnées HDR10+"}
_KIND_LETTER = {"v": "video", "a": "audio", "s": "subtitle"}
_HDR10PLUS_TYPES = (HDRType.HDR10PLUS, HDRType.DOLBY_VISION_HDR10PLUS)


@dataclass(frozen=True)
class ExtractionPlan:
    """Élément à extraire : piste (index ffprobe) ou pièce jointe, couche, plan FFmpeg, nom proposé."""

    kind: str  # video | audio | subtitle | attachment
    stream_index: int
    layer: str = "stream"
    plan: ExtractPlan | None = None
    default_name: str = ""
    label: str = ""
    attachment: int | None = None  # rang de la pièce jointe Matroska (local_index)
    attached_pic: bool = False  # image de couverture portée par un flux (MP4)

    def describe(self) -> dict[str, Any]:
        return {"kind": self.kind, "stream_index": self.stream_index, "layer": self.layer,
                "attachment": self.attachment, "format": self.label, "name": self.default_name}


@dataclass(frozen=True)
class ExtractTools:
    ffmpeg: str
    dovi_tool: str = "dovi_tool"
    hdr10plus_tool: str = "hdr10plus_tool"


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------


def _tracks(info: FileInfo) -> list[tuple[str, Any]]:
    return [("video", t) for t in info.video_tracks] + [("audio", t) for t in info.audio_tracks] + \
        [("subtitle", t) for t in info.subtitle_tracks]


def resolve_track(info: FileInfo, selector: str | int) -> tuple[str, Any]:
    """Piste désignée par son index ffprobe (``3``) ou par nature et rang (``v:0``, ``a:1``, ``s:0``)."""
    text = str(selector).strip()
    if ":" in text:
        letter, _, rank = text.partition(":")
        kind = _KIND_LETTER.get(letter.lower())
        if kind is None or not rank.isdigit():
            raise JobError(f"Extraction : sélecteur de piste invalide {text!r} (index ou v:N, a:N, s:N).", EXIT_ARGS)
        of_kind = [track for k, track in _tracks(info) if k == kind]
        if int(rank) >= len(of_kind):
            raise JobError(f"Extraction : aucune piste {text} ({len(of_kind)} de ce type).", EXIT_ARGS)
        return kind, of_kind[int(rank)]
    if not text.isdigit():
        raise JobError(f"Extraction : sélecteur de piste invalide {text!r} (index ou v:N, a:N, s:N).", EXIT_ARGS)
    for kind, track in _tracks(info):
        if track.index == int(text):
            return kind, track
    raise JobError(f"Extraction : aucune piste d'index {text} dans {info.path.name}.", EXIT_ARGS)


def plan_track(info: FileInfo, selector: str | int, *, layer: str = "stream",
               container: bool = False) -> ExtractionPlan:
    """Plan d'extraction d'une piste ou d'une de ses couches HDR."""
    if layer not in LAYERS:
        raise JobError(f"Extraction : couche inconnue {layer!r} ({', '.join(LAYERS)}).", EXIT_ARGS)
    kind, track = resolve_track(info, selector)
    index = int(track.index)
    codec = str(getattr(track, "codec", "") or "")
    if layer != "stream":
        if kind != "video" or codec.lower() != "hevc":
            raise JobError("Extraction : couches Dolby Vision / HDR10+ réservées aux pistes vidéo HEVC.",
                           EXIT_VALIDATION)
        profile = getattr(track, "dovi_profile", None)
        if layer in ("el", "bl") and profile != 7:
            raise JobError("Extraction : couches BL / EL réservées au Dolby Vision profil 7 (double couche).",
                           EXIT_VALIDATION)
        if layer == "rpu" and profile is None:
            raise JobError("Extraction : aucune métadonnée Dolby Vision (RPU) dans cette piste.", EXIT_VALIDATION)
        if layer == "hdr10plus" and getattr(track, "hdr_type", None) not in _HDR10PLUS_TYPES:
            raise JobError("Extraction : aucune métadonnée HDR10+ dans cette piste.", EXIT_VALIDATION)
        label = LAYER_LABELS[layer]
        if layer == "el" and getattr(track, "dovi_el_type", None):
            label = f"{label} ({track.dovi_el_type})"
        return ExtractionPlan(kind, index, layer, None, f"{info.path.stem}.t{index}{_LAYER_SUFFIX[layer]}", label)
    if kind == "subtitle":
        try:
            plan = TrackExtractor.plan_subtitle(codec)
        except ValueError as exc:
            raise JobError(str(exc), EXIT_VALIDATION) from exc
        if container and plan.extension != ".mks":
            plan = ExtractPlan("copy", ".mks", f"{codec} (Matroska)", "Matroska (*.mks)")
    elif kind == "audio":
        plan = TrackExtractor.plan_audio(codec, container=container)
    else:
        plan = TrackExtractor.plan_video(codec, container=container)
    language = str(getattr(track, "language", "") or "und")
    name = TrackExtractor.default_output_name(info.path.stem, language, index, plan.extension)
    return ExtractionPlan(kind, index, layer, plan, name, plan.format_label)


def plan_attachment(info: FileInfo, selector: str | int) -> ExtractionPlan:
    """Pièce jointe désignée par son rang (``local_index`` affiché par ``inspect``) ou son nom de fichier."""
    text = str(selector).strip()
    attachments = list(info.attachments)
    found = next((att for att in attachments
                  if (text.isdigit() and att.local_index == int(text)) or att.filename == text), None)
    if found is None:
        raise JobError(f"Extraction : pièce jointe {text!r} introuvable ({len(attachments)} dans le fichier).",
                       EXIT_ARGS)
    name = Path(str(found.filename or "")).name or f"{info.path.stem}.attachment{found.local_index}"
    return ExtractionPlan("attachment", int(found.index), "stream", None, name, str(found.mimetype or ""),
                          attachment=int(found.local_index), attached_pic=bool(found.is_attached_pic))


def plan_all(info: FileInfo, *, container: bool = False, attachments: bool = True) -> list[ExtractionPlan]:
    """Toutes les pistes extractibles (sous-titres à OCR exclus) puis les pièces jointes, noms distincts."""
    plans: list[ExtractionPlan] = []
    for _kind, track in _tracks(info):
        with contextlib.suppress(JobError):
            plans.append(plan_track(info, track.index, container=container))
    if attachments:
        taken = {plan.default_name for plan in plans}
        for att in info.attachments:
            plan = plan_attachment(info, att.local_index)
            if plan.default_name in taken:  # deux pièces jointes du même nom : rang ajouté
                name = Path(plan.default_name)
                plan = replace(plan, default_name=f"{name.stem}.{att.local_index}{name.suffix}")
            taken.add(plan.default_name)
            plans.append(plan)
    return plans


def check_outputs(outputs: Sequence[Path], *, source: Path, force: bool) -> None:
    """Refuse toute collision avant la première extraction : doublons, source, fichiers existants sans ``force``."""
    seen: set[Path] = set()
    for output in outputs:
        resolved = Path(output).expanduser().resolve()
        if resolved == Path(source).resolve():
            raise JobError(f"Extraction : la sortie ne peut pas être la source ({output}).", EXIT_ARGS)
        if resolved in seen:
            raise JobError(f"Extraction : deux éléments écriraient {output}.", EXIT_EXISTS)
        seen.add(resolved)
    existing = [str(output) for output in outputs if Path(output).expanduser().exists()]
    if existing and not force:
        raise JobError("Sortie(s) déjà existante(s) (utiliser --force) : " + ", ".join(existing), EXIT_EXISTS)


def skipped_tracks(info: FileInfo, plans: Sequence[ExtractionPlan]) -> list[str]:
    """Pistes que ``plan_all`` a écartées (sous-titres à OCR), pour le journal."""
    kept = {plan.stream_index for plan in plans if plan.kind != "attachment"}
    return [f"#{track.index} ({track.codec})" for _kind, track in _tracks(info) if track.index not in kept]


# ---------------------------------------------------------------------------
# Commandes
# ---------------------------------------------------------------------------


def _annexb(ffmpeg: str, path: Path, stream_index: int) -> list[str]:
    decode = [ffmpeg, "-nostdin", "-loglevel", "error"]
    append_ffmpeg_input_args(decode, path)
    return decode + ["-map", f"0:{stream_index}", "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", "-"]


def layer_commands(layer: str, tools: ExtractTools, path: Path, stream_index: int, output: Path,
                   scratch: Path) -> list[Any]:
    """Commandes d'une couche HDR à essayer dans l'ordre : lecture directe par l'outil, puis flux Annex B.

    La décision de lecture directe est celle de l'inspection HDR (``direct_tool_input``).
    """
    from core.hdr_inspection.inspector import direct_tool_input

    if layer in ("el", "bl"):
        args = ["--el-only", "-e", str(output)] if layer == "el" else ["-b", str(output), "-e", str(scratch)]
        return [PipelineCommand([tools.dovi_tool, "demux", "-", *args], [_annexb(tools.ffmpeg, path, stream_index)])]
    if layer == "rpu":
        direct = [tools.dovi_tool, "extract-rpu", "-i", str(path), "-o", str(output)]
        piped: list[str] = [tools.dovi_tool, "extract-rpu", "-", "-o", str(output)]
    else:
        direct = [tools.hdr10plus_tool, "extract", str(path), "-o", str(output)]
        piped = [tools.hdr10plus_tool, "extract", "-o", str(output), "-"]
    pipe = PipelineCommand(piped, [_annexb(tools.ffmpeg, path, stream_index)])
    if not direct_tool_input(path, stream_index):
        return [pipe]
    # MKV : le lecteur Matroska des outils peut encore refuser le fichier → repli en pipe.
    return [direct, pipe] if path.suffix.lower() == ".mkv" else [direct]


def stream_command(plan: ExtractionPlan, tools: ExtractTools, path: Path, output: Path,
                   progress_args: Sequence[str] = ()) -> list[str]:
    """Commande FFmpeg d'une piste (ou d'une couverture portée par un flux)."""
    if plan.kind == "attachment":
        return [tools.ffmpeg, "-hide_banner", "-y", *ffmpeg_input_args(path), "-map", f"0:{plan.stream_index}",
                "-c", "copy", "-frames:v", "1", "-f", "image2", str(output)]
    assert plan.plan is not None
    return TrackExtractor.build_command(tools.ffmpeg, path, plan.stream_index, plan.kind, plan.plan, output,
                                        progress_args=progress_args, input_args=ffmpeg_input_args(path))


def required_tools(plans: Sequence[ExtractionPlan], tools: ExtractTools) -> None:
    """Refus avant toute extraction si un outil nécessaire est introuvable."""
    needed = {tools.ffmpeg}
    for plan in plans:
        if plan.layer in ("el", "bl", "rpu"):
            needed.add(tools.dovi_tool)
        elif plan.layer == "hdr10plus":
            needed.add(tools.hdr10plus_tool)
    missing = sorted(tool for tool in needed if not (shutil.which(tool) or Path(tool).is_file()))
    if missing:
        raise JobError("Outil(s) introuvable(s) : " + ", ".join(missing), EXIT_TOOL)


# ---------------------------------------------------------------------------
# Exécution
# ---------------------------------------------------------------------------


RunCommand = Callable[[Any, str], None]


def tool_runner(cancel: CancelToken, on_progress_pct: Callable[[int], None] | None = None) -> RunCommand:
    """Exécute une commande par ``ToolRunner`` et attend sa fin (annulation par ``cancel``)."""
    from core.runner import ToolRunner
    from core.service.jobs import await_task

    runner = ToolRunner(max_workers=1)

    def run(cmd: Any, label: str) -> None:
        outcome = await_task(runner.run(cmd, label=label), cancel, on_progress_pct=on_progress_pct)
        if outcome.status == "cancelled":
            raise ApiError(ErrorCode.CANCELLED, "Extraction annulée.")
        if outcome.status == "failed":
            raise JobError(f"Extraction échouée : {outcome.message}", EXIT_WORKFLOW)

    return run


def task_runner(signals: Any) -> RunCommand:
    """Exécution synchrone dans le fil d'une tâche ``TaskSignals`` (annulation : ``signals.cancel()``)."""
    from core.runner import CommandError, ToolRunner

    runner = ToolRunner(max_workers=1)

    def run(cmd: Any, label: str) -> None:
        try:
            runner._run_cmd(cmd, label=label, progress_cb=signals.progress.emit,
                            progress_pct_cb=signals.progress_pct.emit, signals=signals)
        except CommandError as exc:
            raise JobError(f"Extraction échouée : {exc}", EXIT_WORKFLOW) from exc

    return run


def start_extraction(info: FileInfo, targets: Sequence[tuple[ExtractionPlan, Path]], *, tools: ExtractTools,
                     force: bool = False, progress_args: Sequence[str] = ()) -> Any:
    """Extractions en tâche de fond (panneau Conteneur) : ``TaskSignals`` finished (chemins) / failed / cancelled."""
    from concurrent.futures import ThreadPoolExecutor

    from core.runner import TaskCancelledError, TaskSignals

    signals = TaskSignals()
    executor = ThreadPoolExecutor(max_workers=1)

    def task() -> None:
        try:
            run = task_runner(signals)
            done = [str(extract(info, plan, output, tools=tools, run=run, force=force, progress_args=progress_args))
                    for plan, output in targets]
            signals.finished.emit("\n".join(done))
        except TaskCancelledError:
            signals.cancelled.emit()
        except ApiError as exc:  # JobError comprise
            signals.failed.emit(exc.message, exc)
        except Exception as exc:
            signals.failed.emit(str(exc), exc)

    signals.watch_future(executor.submit(task))
    executor.shutdown(wait=False)
    return signals


def read_attachment(path: Path, local_index: int) -> bytes:
    """Contenu d'une pièce jointe Matroska (rang ``local_index``), sans lire les autres."""
    from core.matroska.reader import MatroskaReader

    return MatroskaReader(path).attachment_data(local_index)


def extract(
    info: FileInfo,
    plan: ExtractionPlan,
    output: Path,
    *,
    tools: ExtractTools,
    run: RunCommand,
    force: bool = False,
    progress_args: Sequence[str] = (),
    attachment_reader: Callable[[Path, int], bytes] = read_attachment,
) -> Path:
    """Extrait ``plan`` vers ``output`` (candidat ``.partial`` publié à la fin)."""
    output = Path(output).expanduser()
    if output.exists() and not force:
        raise JobError(f"Sortie déjà existante : {output} (utiliser --force)", EXIT_EXISTS)
    if output.resolve() == Path(info.path).resolve():
        raise JobError("Extraction : la sortie ne peut pas être la source.", EXIT_ARGS)
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        reservation = OutputReservation.acquire(output)
    except OutputBusyError as exc:
        raise ApiError(ErrorCode.BUSY, str(exc)) from exc
    candidate: Path | None = None
    scratch: Path | None = None
    try:
        candidate = reserve_candidate(output)
        # BL : dovi_tool demux écrit aussi l'EL (``-e`` obligatoire), dans un candidat jeté à la fin.
        scratch = reserve_candidate(output.with_name(output.name + ".el")) if plan.layer == "bl" else None
        if plan.kind == "attachment" and not plan.attached_pic:
            assert plan.attachment is not None
            candidate.write_bytes(attachment_reader(Path(info.path), plan.attachment))
        elif plan.layer != "stream":
            commands = layer_commands(plan.layer, tools, Path(info.path), plan.stream_index, candidate,
                                      scratch or candidate)
            for position, cmd in enumerate(commands):
                try:
                    run(cmd, f"extract-{plan.layer}")
                    break
                except JobError:
                    if position == len(commands) - 1:
                        raise
                    candidate.unlink(missing_ok=True)
        else:
            run(stream_command(plan, tools, Path(info.path), candidate, progress_args),
                f"extract-{plan.kind}-{plan.stream_index}")
        if not candidate.exists() or candidate.stat().st_size == 0:
            raise JobError("Extraction : sortie vide (piste sans données ou couche absente).", EXIT_VALIDATION)
        reservation.publish(candidate)
    except OutputChangedError as exc:  # destination modifiée pendant l'extraction : les deux sont conservés
        raise JobError(str(exc), EXIT_EXISTS) from exc
    except OutputCommitError:  # publication refusée : le candidat est conservé, comme l'annonce l'erreur
        raise
    except BaseException:
        if candidate is not None:
            candidate.unlink(missing_ok=True)
        raise
    finally:
        if scratch is not None:
            scratch.unlink(missing_ok=True)
        reservation.release()
    return output
