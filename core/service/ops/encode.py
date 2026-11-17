"""Opérations d'encodage : aperçu image / vidéo d'un job encodé, presets d'encodage enregistrés."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from core.jobs.constants import EXIT_ARGS, EXIT_EXISTS, EXIT_VALIDATION, EXIT_WORKFLOW
from core.jobs.encode_runtime import encode_workflow, prepare_vulkan, probe_cancel_scope
from core.jobs.errors import JobError
from core.json_documents import read_json_document
from core.service.context import ExecutionContext
from core.service.errors import ApiError, ErrorCode
from core.service.jobs import await_task, raise_for_outcome
from core.service.models import MAX_API_DIAGNOSTICS, Model, dataclass_schema, field, list_of
from core.service.operations import operation
from core.service.ops.media import MediaJobRequest, _build_cached, resolve_job_encode
from core.workflows.encode.models import EncodePreset, EncodePreviewRequest
from core.workflows.encode.profiles import ProfileManager

# ---------------------------------------------------------------------------
# Aperçu
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PreviewCaptureRequest(MediaJobRequest):
    """Aperçu réel d'un job encodé (comme les boutons Aperçu du panneau Encodage)."""

    output_dir: Path | None = field(default=None,
                                    description="Dossier où copier les captures ; défaut : dossier de la requête.")
    mode: Literal["image", "video"] = "image"
    timecode_s: float = field(default=0.0, minimum=0, description="Instant de l'aperçu (secondes).")
    duration_s: float | None = field(default=None, minimum=0.5, description="Durée (vidéo : 5–30 s).")
    random_scene: bool = field(default=False, description="Instant tiré au hasard.")


@dataclass(frozen=True)
class PreviewImage(Model):
    """Image d'aperçu livrée."""

    path: str
    scene_time_s: float
    label: str


@dataclass(frozen=True)
class PreviewCaptureResult(Model):
    mode: str
    captures: tuple[dict[str, Any], ...] = field(default=(), description="Images : chemin, instant, libellé.",
                                                 schema=list_of(PreviewImage))
    video: str | None = None
    warning: str = ""


def _unique(target_dir: Path, name: str) -> Path:
    candidate = target_dir / name
    stem, suffix = candidate.stem, candidate.suffix
    index = 1
    while candidate.exists():
        candidate = target_dir / f"{stem}-{index}{suffix}"
        index += 1
    return candidate


def _deliver(source: Path, target_dir: Path, name: str) -> Path:
    """Copie la capture dans ``target_dir`` puis supprime l'original (jamais le dossier d'aperçus entier)."""
    target = _unique(target_dir, name)
    shutil.copy2(source, target)
    source.unlink(missing_ok=True)
    return target


@operation(
    "encode.preview_capture",
    queue="job", request=PreviewCaptureRequest,
    result=PreviewCaptureResult,
    summary="Aperçu réel d'un job encodé : images ou extrait vidéo encodés avec les réglages du job.",
    long_running=True,
    mutating=True,
    uses_gpu=True,
    errors=(ErrorCode.VALIDATION, ErrorCode.TOOL_MISSING, ErrorCode.WORKFLOW_FAILED, ErrorCode.CANCELLED),
)
def encode_preview_capture(request: PreviewCaptureRequest, ctx: ExecutionContext) -> PreviewCaptureResult:
    output_dir = request.output_dir if request.output_dir is not None else ctx.base_dir
    if output_dir is None:  # refus avant tout travail
        raise ApiError(ErrorCode.VALIDATION, "Dossier des captures requis (`output_dir`) : aucun dossier de référence.")
    target_dir = Path(output_dir)
    remux_config, options, cache = _build_cached(request, ctx)
    resolved = resolve_job_encode(request, remux_config, options, cache, ctx)
    if resolved is None or resolved.config is None:
        raise JobError("Aperçu : le job n'encode rien (bloc `encode` absent ou copie pure).", EXIT_VALIDATION)
    wf = encode_workflow(ctx.config, options)
    preview = EncodePreviewRequest(mode=request.mode, timecode_s=request.timecode_s,
                                   duration_s=request.duration_s or (5.0 if request.mode == "video" else 2.0),
                                   random_scene=request.random_scene)
    with probe_cancel_scope(wf, ctx.cancel.event):  # sondes Vulkan et internes annulables
        prepare_vulkan(wf, resolved.config, str(options.ffmpeg or ctx.config.tool_ffmpeg), ctx.cancel.event)
        outcome = await_task(wf.run_preview(resolved.config, preview), ctx.cancel)
    raise_for_outcome(outcome, ctx.cancel, cancelled="Aperçu annulé.",
                      failed=lambda message: JobError(f"Aperçu impossible : {message}", EXIT_WORKFLOW))
    payload = json.loads(outcome.result or "{}")
    target_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(resolved.config.output).stem
    captures: list[dict[str, Any]] = []
    for capture in payload.get("captures", []):
        source = Path(capture["image_path"])
        delivered = _deliver(source, target_dir, f"{stem}.apercu-{capture['scene_time_s']:.1f}s{source.suffix}")
        captures.append({"path": str(delivered), "scene_time_s": capture["scene_time_s"], "label": capture["label"]})
        ctx.events.artifact("preview-image", str(delivered))
    video = None
    if payload.get("video_path"):
        source = Path(payload["video_path"])
        delivered = _deliver(source, target_dir, f"{stem}.apercu-{request.timecode_s:.1f}s{source.suffix}")
        video = str(delivered)
        ctx.events.artifact("preview-video", video)
    return PreviewCaptureResult(mode=str(payload.get("mode") or request.mode), captures=tuple(captures),
                                video=video, warning=str(payload.get("warning") or ""))


# ---------------------------------------------------------------------------
# Presets d'encodage (mêmes fichiers que l'interface)
# ---------------------------------------------------------------------------


def _manager(ctx: ExecutionContext) -> ProfileManager:
    return ProfileManager(Path(ctx.config.app_data_dir) / "encode_profiles")


#: Preset tel que rendu (``show``) : tous ses champs, typés.
_PRESET_SCHEMA = dataclass_schema(EncodePreset, exclude=("force_8bit", "force_10bit"))


def _input_schema(node: Any, *, root: bool = True) -> Any:
    """Preset à enregistrer : seul ``name`` (non vide) requis, champs absents complétés par leurs défauts ;
    anciens champs ``force_8bit`` / ``force_10bit`` admis."""
    if isinstance(node, list):
        return [_input_schema(item, root=False) for item in node]
    if not isinstance(node, dict):
        return node
    schema = {key: _input_schema(value, root=False) for key, value in node.items() if key != "required"}
    if root:
        schema["required"] = ["name"]
        schema["properties"] = {**schema["properties"], "name": {"type": "string", "pattern": "\\S"},
                                "force_8bit": {"type": "boolean"}, "force_10bit": {"type": "boolean"}}
    return schema


_PRESET_INPUT_SCHEMA = _input_schema(_PRESET_SCHEMA)


def _type_errors(value: Any, schema: dict[str, Any], prefix: str) -> list[tuple[str, str]]:
    from core.service.schema_check import schema_errors

    return [(f"{prefix}.{path}" if path else prefix, message)
            for path, message in schema_errors(value, schema, {"$defs": {}}, limit=MAX_API_DIAGNOSTICS - 1)]


def _conforming(manager: ProfileManager, ctx: ExecutionContext) -> tuple[list[EncodePreset], list[dict[str, str]]]:
    """Presets enregistrés et fichiers illisibles ; en requête de l'API, un preset aux champs non conformes au
    schéma publié (fichier écrit à la main) est signalé comme fichier illisible plutôt que publié (commandes
    historiques : inchangé)."""
    loaded = manager.load_all()
    unreadable = [{"path": str(error.path), "error": str(error.reason)} for error in manager.load_errors]
    presets: list[EncodePreset] = []
    refused: list[dict[str, str]] = []
    for preset in loaded:
        errors = _type_errors(preset.to_json_dict(), _PRESET_SCHEMA, "preset") if ctx.api else []
        if errors:  # chemin relu après la capture des erreurs de chargement (une relecture les remet à zéro)
            refused.append({"path": str(manager.path_for_name(preset.name)),
                            "error": "; ".join(f"{path} : {message}" for path, message in errors)})
        else:
            presets.append(preset)
    return presets, unreadable + refused


def _find(manager: ProfileManager, name: str, ctx: ExecutionContext | None = None) -> EncodePreset:
    for preset in manager.load_all():
        if preset.name == name:
            errors = _type_errors(preset.to_json_dict(), _PRESET_SCHEMA, "preset") if ctx and ctx.api else []
            if errors:
                raise JobError("Preset enregistré non conforme : "
                               + "; ".join(f"{path} : {message}" for path, message in errors), EXIT_VALIDATION)
            return preset
    raise JobError(f"Preset d'encodage introuvable : {name}", EXIT_ARGS)


@dataclass(frozen=True)
class PresetListRequest(Model):
    """Presets d'encodage enregistrés."""


@dataclass(frozen=True)
class PresetEntry(Model):
    """Preset d'encodage enregistré (résumé)."""

    name: str
    description: str
    codec: str
    preset: str = field(description="Préréglage de l'encodeur.")


@dataclass(frozen=True)
class LoadError(Model):
    """Fichier illisible, laissé en place."""

    path: str
    error: str


@dataclass(frozen=True)
class PresetListResult(Model):
    presets: tuple[dict[str, Any], ...] = field(default=(), schema=list_of(PresetEntry))
    load_errors: tuple[dict[str, str], ...] = field(default=(), description="Fichiers illisibles (laissés en place).",
                                                    schema=list_of(LoadError))


@dataclass(frozen=True)
class PresetNameRequest(Model):
    name: str


@dataclass(frozen=True)
class PresetResult(Model):
    preset: dict[str, Any] = field(schema=_PRESET_SCHEMA)


@dataclass(frozen=True)
class PresetImportRequest(Model):
    """Preset importé d'un fichier JSON (format des presets de l'interface)."""

    file: Path
    name: str | None = field(default=None, description="Nom enregistré (défaut : celui du fichier).")
    force: bool = field(default=False, description="Remplacer un preset du même nom.")


@dataclass(frozen=True)
class PresetSaveRequest(Model):
    """Preset enregistré d'après ses champs (format des presets de l'interface)."""

    preset: dict[str, Any] = field(description="Champs du preset (`name` requis ; absents : valeurs par défaut).",
                                   schema=_PRESET_INPUT_SCHEMA)
    force: bool = False

    @classmethod
    def document_errors(cls, values: dict[str, Any]) -> list[tuple[str, str]]:
        """Requête de l'API : champs du preset vérifiés contre le schéma publié, puis par sa construction."""
        errors = _type_errors(values.get("preset"), _PRESET_INPUT_SCHEMA, "preset")
        if not errors:
            try:
                _preset_from(values.get("preset"))
            except JobError as exc:
                errors = [("preset", str(exc))]
        return errors


@dataclass(frozen=True)
class PresetExportRequest(Model):
    name: str
    file: Path
    force: bool = False


@dataclass(frozen=True)
class PresetFileResult(Model):
    name: str
    file: str | None = None


def _preset_from(raw: Any, name: str | None = None) -> EncodePreset:
    if not isinstance(raw, dict):
        raise JobError("Preset : objet JSON attendu.", EXIT_ARGS)
    fields = EncodePreset.__dataclass_fields__
    unknown = sorted(key for key in raw if key not in fields)
    if unknown:
        raise JobError("Preset : champ(s) inconnu(s) : " + ", ".join(unknown), EXIT_ARGS)
    values = {**raw, **({"name": name} if name else {})}
    if not str(values.get("name") or "").strip():
        raise JobError("Preset : `name` requis.", EXIT_ARGS)
    try:
        return EncodePreset(**values)
    except (TypeError, ValueError) as exc:
        raise JobError(f"Preset invalide : {exc}", EXIT_ARGS) from exc


def _store(ctx: ExecutionContext, preset: EncodePreset, *, force: bool) -> None:
    manager = _manager(ctx)
    exists = JobError(f"Preset déjà enregistré : {preset.name} (utiliser force)", EXIT_EXISTS)
    if not force and preset.name in manager.names():
        raise exists
    try:
        manager.save(preset, replace=force)  # existence revérifiée sous le verrou d'écriture
    except FileExistsError:
        raise exists from None


@operation("encode_preset.list", request=PresetListRequest, result=PresetListResult,
           summary="Presets d'encodage enregistrés (communs avec l'interface).")
def preset_list(_request: PresetListRequest, ctx: ExecutionContext) -> PresetListResult:
    manager = _manager(ctx)
    loaded, errors = _conforming(manager, ctx)
    presets = tuple({"name": p.name, "description": p.description, "codec": p.codec, "preset": p.preset}
                    for p in sorted(loaded, key=lambda item: item.name.lower()))
    return PresetListResult(presets=presets, load_errors=tuple(errors))


@operation("encode_preset.show", request=PresetNameRequest, result=PresetResult,
           summary="Contenu d'un preset d'encodage.")
def preset_show(request: PresetNameRequest, ctx: ExecutionContext) -> PresetResult:
    return PresetResult(preset=_find(_manager(ctx), request.name, ctx).to_json_dict())


@operation("encode_preset.save", request=PresetSaveRequest, result=PresetFileResult,
           summary="Enregistrer un preset d'encodage d'après ses champs.", mutating=True)
def preset_save(request: PresetSaveRequest, ctx: ExecutionContext) -> PresetFileResult:
    preset = _preset_from(request.preset)
    _store(ctx, preset, force=request.force)
    return PresetFileResult(name=preset.name)


@operation("encode_preset.import", request=PresetImportRequest, result=PresetFileResult,
           summary="Importer un preset d'encodage depuis un fichier JSON.", mutating=True)
def preset_import(request: PresetImportRequest, ctx: ExecutionContext) -> PresetFileResult:
    try:
        raw = read_json_document(request.file)
    except (OSError, ValueError) as exc:
        raise JobError(f"Preset illisible ({request.file}) : {exc}", EXIT_ARGS) from exc
    named = {**raw, "name": request.name} if isinstance(raw, dict) and request.name else raw
    errors = _type_errors(named, _PRESET_INPUT_SCHEMA, "preset") if ctx.api else []
    if errors:  # requête de l'API : mêmes types qu'un preset enregistré par encode_preset.save
        raise JobError("Preset non conforme : " + "; ".join(f"{path} : {message}" for path, message in errors),
                       EXIT_VALIDATION)
    preset = _preset_from(raw, request.name)
    _store(ctx, preset, force=request.force)
    return PresetFileResult(name=preset.name, file=str(request.file))


@operation("encode_preset.export", request=PresetExportRequest, result=PresetFileResult,
           summary="Exporter un preset d'encodage dans un fichier JSON.", mutating=True)
def preset_export(request: PresetExportRequest, ctx: ExecutionContext) -> PresetFileResult:
    preset = _find(_manager(ctx), request.name)
    if request.file.exists() and not request.force:
        raise JobError(f"Fichier existant : {request.file} (utiliser force)", EXIT_EXISTS)
    from core.workflows.workflow_store import atomic_write_text

    atomic_write_text(Path(request.file), json.dumps(preset.to_json_dict(), ensure_ascii=False, indent=2) + "\n")
    ctx.events.artifact("preset", str(request.file))
    return PresetFileResult(name=preset.name, file=str(request.file))


@operation("encode_preset.delete", request=PresetNameRequest, result=PresetFileResult,
           summary="Supprimer un preset d'encodage enregistré.", mutating=True)
def preset_delete(request: PresetNameRequest, ctx: ExecutionContext) -> PresetFileResult:
    manager = _manager(ctx)
    _find(manager, request.name)
    manager.delete(request.name)
    return PresetFileResult(name=request.name)
