"""Opérations système : version, outils résolus, capacités de la machine, schémas publiés."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from core.service.context import ExecutionContext
from core.service.models import Model, field
from core.service.operations import operation
from core.update_check import UpdateCheckError, normalize_update_channel, query_latest_release
from core.version import APP_BUILD_VERSION, APP_NAME


@dataclass(frozen=True)
class VersionRequest(Model):
    """Version de Muxiveo, avec comparaison facultative à la dernière release."""

    check: bool = field(default=False, description="Comparer avec la dernière release du canal (accès réseau).")
    channel: Literal["stable", "unstable"] | None = field(
        default=None, description="Canal de mise à jour ; défaut : réglage [ui] update_channel.")


@dataclass(frozen=True)
class VersionResult(Model):
    """Version installée et, si demandé, état de la mise à jour."""

    name: str
    version: str
    channel: str | None = field(default=None, description="Canal comparé (seulement avec check).")
    latest: str | None = field(default=None, description="Dernière version publiée sur le canal.")
    update_available: bool | None = None
    url: str | None = field(default=None, description="Page de la release.")
    check_failed: bool = field(default=False, description="Vérification demandée mais impossible.")
    check_error: str | None = None


@operation(
    "system.version",
    request=VersionRequest,
    result=VersionResult,
    summary="Version de Muxiveo ; avec check, comparaison à la dernière release du canal.",
)
def system_version(request: VersionRequest, ctx: ExecutionContext) -> VersionResult:
    if not request.check:
        return VersionResult(name=APP_NAME, version=APP_BUILD_VERSION)
    channel = normalize_update_channel(request.channel or getattr(ctx.config, "update_channel", None))
    query = ctx.port("query_latest_release", query_latest_release)
    try:
        info = query(channel)
    except UpdateCheckError as exc:
        ctx.events.log("error", f"Impossible de joindre GitHub pour vérifier les mises à jour : {exc}")
        return VersionResult(name=APP_NAME, version=APP_BUILD_VERSION, channel=channel, check_failed=True,
                             check_error=str(exc))
    if info is None:
        return VersionResult(name=APP_NAME, version=APP_BUILD_VERSION, channel=channel, check_failed=True)
    return VersionResult(name=APP_NAME, version=APP_BUILD_VERSION, channel=channel, latest=info.version,
                         update_available=info.is_newer, url=info.url)


@dataclass(frozen=True)
class ToolsRequest(Model):
    """Commandes d'outils résolues par Muxiveo (configuration, extensions, PATH)."""


@dataclass(frozen=True)
class ToolsResult(Model):
    version: int = 1
    tools: dict[str, str] = field(default_factory=dict, description="Outil → commande exécutée.")


@operation("system.tools", request=ToolsRequest, result=ToolsResult,
           summary="Commandes d'outils externes résolues par Muxiveo.")
def system_tools(_request: ToolsRequest, ctx: ExecutionContext) -> ToolsResult:
    return ToolsResult(tools=dict(ctx.config.tool_commands()))


SchemaVersion = Literal["1", "exact-job", "decision-profile", "all", "api"]


@dataclass(frozen=True)
class SchemaRequest(Model):
    """Schéma JSON publié."""

    version: SchemaVersion = field(
        default="1", description="« 1 » : job ; exact-job ; decision-profile ; all ; api : catalogue des opérations.")


@dataclass(frozen=True)
class SchemaResult(Model):
    schema: dict[str, Any]


@operation("schema.get", request=SchemaRequest, result=SchemaResult,
           summary="Schémas JSON publiés : contrat des jobs, profils, catalogue de l'API.")
def schema_get(request: SchemaRequest, _ctx: ExecutionContext) -> SchemaResult:
    from core.jobs import schema

    if request.version == "api":
        from core.service.catalog import CATALOG

        return SchemaResult(schema=CATALOG.describe())
    builders = {"1": schema.build_cli_json_schema, "exact-job": schema.build_exact_job_schema_v1,
                "decision-profile": schema.build_decision_profile_schema_v1,
                "all": schema.build_cli_json_schema_bundle}
    return SchemaResult(schema=builders[request.version]())


CapabilitySection = Literal["encoders", "presets", "vulkan", "gpu", "rife"]


@dataclass(frozen=True)
class CapabilitiesRequest(Model):
    """Capacités de la machine (équivalent du tableau de bord)."""

    sections: tuple[CapabilitySection, ...] = field(
        default=(), description="encoders, presets, vulkan, gpu (NVOF, TensorRT), rife ; vide : toutes.")


@dataclass(frozen=True)
class CapabilitiesResult(Model):
    """Sections demandées ; ``None`` : section non sondée."""

    encoders: dict[str, Any] | None = field(default=None, description="Encodeurs logiciels et matériels utilisables.")
    presets: dict[str, Any] | None = field(default=None, description="Préréglages acceptés par codec.")
    vulkan: dict[str, Any] | None = field(default=None, description="Filtres GPU Vulkan (NLMeans 10 bits).")
    gpu: dict[str, Any] | None = field(default=None, description="Flux optique NVIDIA (NVOF), TensorRT.")
    rife: dict[str, Any] | None = field(default=None, description="Moteur vidéo MVO (muxiveo-rife) : version, options.")


@operation("system.capabilities", request=CapabilitiesRequest, result=CapabilitiesResult,
           summary="Capacités de la machine : encodeurs, préréglages, Vulkan, NVOF / TensorRT, moteur vidéo MVO.",
           uses_gpu=True)
def system_capabilities(request: CapabilitiesRequest, ctx: ExecutionContext) -> CapabilitiesResult:
    import dataclasses

    from core import plugins
    from core.workflows.encode.hardware import HardwareEncoderDetector
    from core.workflows.encode.interpolation import detect_gpu_acceleration, rife_capabilities
    from core.workflows.encode.vulkan import detect_vulkan

    wanted = set(request.sections) or {"encoders", "presets", "vulkan", "gpu", "rife"}
    ffmpeg = ctx.tool("ffmpeg")
    rife = str(getattr(ctx.config, "tool_muxiveo_rife", "") or "")
    values: dict[str, Any] = {}
    detector = HardwareEncoderDetector()
    software: set[str] = set()
    hardware: set[str] = set()
    if wanted & {"encoders", "presets"}:
        software = set(detector.detect_software(ffmpeg))
        detected = detector.detect(ffmpeg, nvencc_bin=getattr(ctx.config, "tool_nvencc", None) or None)
        hardware = set(detected[0] if isinstance(detected, tuple) else detected)
    if "encoders" in wanted:
        values["encoders"] = {"software": sorted(software), "hardware": sorted(hardware)}
    if "presets" in wanted:
        presets = detector.detect_presets(ffmpeg, software | hardware)
        values["presets"] = {codec: sorted(items) for codec, items in sorted(presets.items())}
    if "vulkan" in wanted:
        values["vulkan"] = dataclasses.asdict(detect_vulkan(ffmpeg))
    if "gpu" in wanted:
        installed = plugins.installed_plugin(plugins.TRT)
        acceleration = detect_gpu_acceleration(rife, trt_plugin=str(installed.path) if installed else "")
        values["gpu"] = {"nvof": dataclasses.asdict(acceleration.nvof),
                         "tensorrt": dataclasses.asdict(acceleration.trt)}
    if "rife" in wanted:
        caps = rife_capabilities(rife) if rife else None
        values["rife"] = None if caps is None else {
            "executable": rife, "version": caps.label, "contract": caps.contract, "options": sorted(caps.options),
            "models": sorted(caps.models) if caps.models is not None else None,
            "restore_models": sorted(caps.restore_models), "selector_status": caps.selector_status,
        }
    return CapabilitiesResult(**values)
