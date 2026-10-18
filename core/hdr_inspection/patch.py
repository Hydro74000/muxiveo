"""Correction des métadonnées HDR d'un fichier sans réencodage (HDR Studio).

HDR10 statique (MaxCLL/MaxFALL : SEI du flux et élément Colour du conteneur), RPU Dolby Vision
(``dovi_tool editor`` : L6, zone active L5, L11, écran de mastering du RPU, retrait CM v4.0) et
HDR10+ (retrait, écran ciblé, profil A, plafond). Flux vidéo extrait en Annex B, corrigé par les
outils, puis replacé bloc pour bloc dans une copie du MKV (autres pistes, chapitres, pièces
jointes et tags conservés) ; publication protégée de la sortie (candidat ``.partial``).
"""
from __future__ import annotations

import json
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from core.bluray import append_ffmpeg_input_args
from core.hdr_inspection.json_stream import iter_json_array, read_json_head
from core.hdr_inspection.pq import nits_to_pq
from core.i18n import translate_text
from core.output_commit import OutputReservation, reserve_candidate
from core.runner import TaskCancelledError

#: Lance une commande (liste), lève en cas d'échec ou d'annulation.
RunCommand = Callable[[list[str]], object]
StepCallback = Callable[[str, float, float], None]
_VERSION_RE = re.compile(r'"Version"\s*:\s*"([^"]*)"')
#: Valeurs L11 (Dolby Vision) : type de contenu.
L11_CONTENT_TYPES = (0, 1, 2, 3, 4)


class PatchError(RuntimeError):
    """Correction impossible (entrée, place, outil)."""


class PatchCancelled(Exception):
    """Correction annulée par l'utilisateur."""


@dataclass(frozen=True)
class StaticEdit:
    max_cll: int
    max_fall: int

    def __post_init__(self) -> None:
        if not (0 <= self.max_cll <= 65535 and 0 <= self.max_fall <= 65535) or self.max_fall > max(self.max_cll, 0) > 0:
            raise ValueError(translate_text("MaxCLL/MaxFALL invalides : MaxFALL doit rester sous MaxCLL."))


@dataclass(frozen=True)
class RpuEdit:
    """Modifications du RPU (``None`` : conservé)."""

    #: L6 : luminance max. du mastering (nits), min. (nits), MaxCLL, MaxFALL.
    l6: tuple[int, float, int, int] | None = None
    #: Zone active L5 de toutes les images : gauche, droite, haut, bas (pixels).
    active_area: tuple[int, int, int, int] | None = None
    #: L11 : type de contenu, point blanc, mode de référence.
    l11: tuple[int, int, bool] | None = None
    #: Écran de mastering du RPU (source_min_pq / source_max_pq), en nits.
    source_display: tuple[float, float] | None = None
    remove_cmv4: bool = False

    def is_empty(self) -> bool:
        return not (self.l6 or self.active_area or self.l11 or self.source_display or self.remove_cmv4)

    def editor_config(self) -> dict:
        """Configuration JSON de ``dovi_tool editor``."""
        config: dict = {}
        if self.l6 is not None:
            max_mastering, min_mastering, max_cll, max_fall = self.l6
            config["level6"] = {
                "max_display_mastering_luminance": int(max_mastering),
                "min_display_mastering_luminance": int(round(min_mastering * 10000)),
                "max_content_light_level": int(max_cll),
                "max_frame_average_light_level": int(max_fall),
            }
        if self.active_area is not None:
            left, right, top, bottom = (int(v) for v in self.active_area)
            config["active_area"] = {"crop": False, "edits": {"all": 0},
                                     "presets": [{"id": 0, "left": left, "right": right, "top": top, "bottom": bottom}]}
        if self.l11 is not None:
            content, whitepoint, reference = self.l11
            config["level11"] = {"content_type": int(content), "whitepoint": int(whitepoint),
                                 "reference_mode_flag": bool(reference)}
        if self.source_display is not None:
            low, high = self.source_display
            config["min_pq"] = int(round(float(nits_to_pq(low)) * 4095))
            config["max_pq"] = int(round(float(nits_to_pq(high)) * 4095))
        if self.remove_cmv4:
            config["remove_cmv4"] = True
        return config


@dataclass(frozen=True)
class Hdr10PlusEdit:
    remove: bool = False
    #: TargetedSystemDisplayMaximumLuminance (nits) de toutes les images.
    targeted_display: int | None = None
    #: Courbe de Bézier retirée : profil A.
    drop_bezier: bool = False
    #: Plafond (nits) des MaxScl, AverageRGB et percentiles (valeurs saturées, pics aberrants).
    cap_nits: float | None = None

    def is_empty(self) -> bool:
        return not (self.remove or self.targeted_display is not None or self.drop_bezier or self.cap_nits)


@dataclass(frozen=True)
class PatchPlan:
    source: Path
    stream_index: int
    output: Path
    static: StaticEdit | None = None
    rpu: RpuEdit | None = None
    hdr10plus: Hdr10PlusEdit | None = None
    notes: tuple[str, ...] = field(default=())

    def is_empty(self) -> bool:
        return (self.static is None and (self.rpu is None or self.rpu.is_empty())
                and (self.hdr10plus is None or self.hdr10plus.is_empty()))


def transform_hdr10plus(source: Path, target: Path, edit: Hdr10PlusEdit) -> int:
    """Réécrit le JSON HDR10+ (lecture en flux) ; renvoie le nombre d'images."""
    head = read_json_head(source)
    profile_match = re.search(r'"HDR10plusProfile"\s*:\s*"([^"]*)"', head)
    version = _VERSION_RE.search(head)
    profile = "A" if edit.drop_bezier else (profile_match.group(1) if profile_match else "A")
    cap = int(round(edit.cap_nits * 10)) if edit.cap_nits else None
    firsts: list[int] = []
    lengths: list[int] = []
    frame = 0
    with Path(target).open("w", encoding="utf-8") as out:
        out.write(json.dumps({"HDR10plusProfile": profile, "Version": version.group(1) if version else "1.0"},
                             separators=(",", ":")).join(('{"JSONInfo":', ',"SceneInfo":[')))
        for frame, info in enumerate(iter_json_array(source, key="SceneInfo")):
            if not isinstance(info, dict):
                raise PatchError(translate_text("JSON HDR10+ invalide (image {frame}).", frame=frame))
            if edit.drop_bezier:
                info.pop("BezierCurveData", None)
                info["TargetedSystemDisplayMaximumLuminance"] = 0
            elif edit.targeted_display is not None:
                info["TargetedSystemDisplayMaximumLuminance"] = int(edit.targeted_display)
            luminance = info.get("LuminanceParameters")
            if cap is not None and isinstance(luminance, dict):
                luminance["MaxScl"] = [min(int(v), cap) for v in luminance.get("MaxScl") or ()]
                if isinstance(luminance.get("AverageRGB"), int):
                    luminance["AverageRGB"] = min(luminance["AverageRGB"], cap)
                distribution = luminance.get("LuminanceDistributions") or {}
                indices = distribution.get("DistributionIndex") or []
                values = distribution.get("DistributionValues") or []
                # Position 10 : pourcentage de pixels ≤ 100 nits, jamais plafonné.
                distribution["DistributionValues"] = [v if i == 10 else min(int(v), cap) for i, v in zip(indices, values)]
            if info.get("SceneFrameIndex") == 0 or not firsts:
                firsts.append(frame)
                lengths.append(0)
            lengths[-1] += 1
            out.write(("," if frame else "") + json.dumps(info, separators=(",", ":")))
        out.write('],"SceneInfoSummary":' + json.dumps(
            {"SceneFirstFrameIndex": firsts, "SceneFrameNumbers": lengths}, separators=(",", ":"))
                  + ',"ToolInfo":{"Tool":"Muxiveo HDR Studio","Version":"1.0"}}')
    return frame + 1 if firsts else 0


def _existing(path: Path) -> Path:
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return probe


def _free_bytes(path: Path) -> int:
    return shutil.disk_usage(_existing(path)).free


def _same_volume(first: Path, second: Path) -> bool:
    try:
        return _existing(first).stat().st_dev == _existing(second).stat().st_dev
    except OSError:
        return False


class MetadataPatcher:
    """Exécute un :class:`PatchPlan` dans ``work_dir`` (dossier process jetable)."""

    def __init__(self, *, ffmpeg_bin: str, dovi_tool_bin: str, hdr10plus_bin: str, run_cmd: RunCommand,
                 work_dir: Path, on_step: StepCallback | None = None,
                 cancelled: Callable[[], bool] | None = None,
                 progress: Callable[[float], None] | None = None) -> None:
        self._ffmpeg, self._dovi, self._hdr10plus = ffmpeg_bin, dovi_tool_bin, hdr10plus_bin
        self._run = run_cmd
        self._work = Path(work_dir)
        self._on_step = on_step
        self._cancelled = cancelled or (lambda: False)
        self._progress = progress

    def _step(self, label: str, start: float, span: float) -> None:
        if self._cancelled():
            raise PatchCancelled
        if self._on_step is not None:
            self._on_step(label, start, span)

    def _check_space(self, plan: PatchPlan) -> None:
        size = plan.source.stat().st_size
        # Flux Annex B courant + suivant dans le dossier de travail ; copie complète à côté de la sortie
        # (les deux sur le même volume : somme des besoins).
        work_need = 2.1 * size + (1.05 * size if _same_volume(self._work, plan.output.parent) else 0)
        if _free_bytes(self._work) < work_need:
            raise PatchError(translate_text(
                "Espace insuffisant dans le dossier de travail : {needed} Go nécessaires.",
                needed=round(work_need / 1e9, 1)))
        if _free_bytes(plan.output.parent) < 1.05 * size:
            raise PatchError(translate_text(
                "Espace insuffisant à côté de la sortie : {needed} Go nécessaires.", needed=round(1.05 * size / 1e9, 1)))

    def run(self, plan: PatchPlan) -> Path:
        """Corrige ``plan.source`` vers ``plan.output`` ; renvoie la sortie publiée."""
        source, output = Path(plan.source), Path(plan.output)
        if plan.is_empty():
            raise PatchError(translate_text("Aucune modification demandée."))
        if output.resolve() == source.resolve():
            raise PatchError(translate_text("La sortie doit être un autre fichier que la source."))
        suffix = source.suffix.lower()
        if suffix not in {".mkv", ".hevc", ".h265", ".265"}:
            raise PatchError(translate_text("Correction réservée aux fichiers MKV et HEVC bruts."))
        self._check_space(plan)
        steps = ["extract"] if suffix == ".mkv" else []
        if plan.rpu is not None and not plan.rpu.is_empty():
            steps.append("rpu")
        if plan.hdr10plus is not None and not plan.hdr10plus.is_empty():
            steps.append("hdr10plus")
        if plan.static is not None:
            steps.append("static")
        steps.append("write")
        span = 1.0 / len(steps)
        position = 0.0
        current = source
        temporaries: list[Path] = []

        def advance(produced: Path) -> Path:
            nonlocal current
            if current != source:
                current.unlink(missing_ok=True)
            current = produced
            temporaries.append(produced)
            return produced

        try:
            with OutputReservation.acquire(output) as reservation:
                if "extract" in steps:
                    self._step(translate_text("Extraction du flux vidéo…"), position, span)
                    target = self._work / "video.hevc"
                    command = [self._ffmpeg, "-nostdin", "-y", "-loglevel", "error"]
                    append_ffmpeg_input_args(command, source)
                    command += ["-map", f"0:{plan.stream_index}", "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb",
                                "-f", "hevc", str(target)]
                    self._run(command)
                    advance(target)
                    position += span
                if "rpu" in steps:
                    assert plan.rpu is not None
                    self._step(translate_text("Édition du RPU Dolby Vision…"), position, span)
                    rpu, edited, config = self._work / "rpu.bin", self._work / "rpu_edit.bin", self._work / "rpu_edit.json"
                    temporaries += [rpu, edited, config]
                    config.write_text(json.dumps(plan.rpu.editor_config()), encoding="utf-8")
                    self._run([self._dovi, "extract-rpu", "-i", str(current), "-o", str(rpu)])
                    self._run([self._dovi, "editor", "-i", str(rpu), "-j", str(config), "-o", str(edited)])
                    target = self._work / "video_rpu.hevc"
                    self._run([self._dovi, "inject-rpu", "-i", str(current), "-r", str(edited), "-o", str(target)])
                    advance(target)
                    position += span
                if "hdr10plus" in steps:
                    assert plan.hdr10plus is not None
                    self._step(translate_text("Édition du HDR10+…"), position, span)
                    stripped = self._work / "video_sans_hdr10plus.hevc"
                    extracted, edited_json = self._work / "hdr10plus.json", self._work / "hdr10plus_edit.json"
                    temporaries += [extracted, edited_json]
                    if not plan.hdr10plus.remove:
                        self._run([self._hdr10plus, "extract", str(current), "-o", str(extracted)])
                        transform_hdr10plus(extracted, edited_json, plan.hdr10plus)
                        extracted.unlink(missing_ok=True)
                    self._run([self._hdr10plus, "remove", "-i", str(current), "-o", str(stripped)])
                    advance(stripped)
                    if not plan.hdr10plus.remove:
                        target = self._work / "video_hdr10plus.hevc"
                        self._run([self._hdr10plus, "inject", "-i", str(current), "-j", str(edited_json),
                                   "-o", str(target)])
                        advance(target)
                    position += span
                if "static" in steps:
                    assert plan.static is not None
                    self._step(translate_text("MaxCLL/MaxFALL du flux…"), position, span)
                    from core.workflows.hevc_static_hdr_metadata import replace_content_light_level_sei_file

                    target = self._work / "video_cll.hevc"
                    result = replace_content_light_level_sei_file(
                        current, target, max_cll=f"{plan.static.max_cll},{plan.static.max_fall}")
                    if not result.applied:
                        raise PatchError(translate_text("SEI MaxCLL/MaxFALL non écrit dans le flux."))
                    advance(target)
                    position += span
                if self._cancelled():
                    raise PatchCancelled
                self._step(translate_text("Écriture du fichier corrigé…"), position, span)
                candidate = reserve_candidate(output)
                total = source.stat().st_size
                report = self._progress
                start = position
                try:
                    if suffix == ".mkv":
                        from core.matroska.hevc.payload_rewriter import rewrite_file_video_payloads

                        rewrite_file_video_payloads(
                            source=source, track_index=plan.stream_index, injected_hevc=current, output=candidate,
                            light_levels=(plan.static.max_cll, plan.static.max_fall) if plan.static else None,
                            cancel_cb=self._cancelled,
                            progress_cb=(lambda p: report(start + span * _write_fraction(p, total)))
                            if report is not None else None)
                    else:
                        shutil.copyfile(current, candidate)
                    if self._cancelled():
                        raise PatchCancelled
                    reservation.publish(candidate)
                except BaseException:
                    candidate.unlink(missing_ok=True)
                    raise
            return output
        except TaskCancelledError as exc:
            raise PatchCancelled from exc
        finally:
            for item in temporaries:
                if item != source:
                    item.unlink(missing_ok=True)


def _write_fraction(progress: object, total_bytes: int) -> float:
    """Avancement de l'écriture Matroska (0–1) : pourcentage du writer, sinon octets écrits / source."""
    percent = getattr(progress, "percent", None)
    if isinstance(percent, (int, float)):
        return max(0.0, min(1.0, percent / 100))
    written = getattr(progress, "bytes_written", 0)
    return max(0.0, min(1.0, written / total_bytes)) if total_bytes > 0 and isinstance(written, int) else 0.0
