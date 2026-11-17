"""Opérations d'administration : configuration (``config.*``, même écriture que le panneau Paramètres) et
dossier de travail (``workdir.*``, même nettoyage que « Nettoyer » au démarrage)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from core.jobs.constants import EXIT_ARGS, EXIT_VALIDATION
from core.jobs.errors import JobError
from core.service.context import ExecutionContext
from core.service.models import Model, field, list_of
from core.service.operations import operation

#: Réglages des extensions, hors des groupes du panneau Paramètres (page Extensions).
_EXTRA_FIELDS = (
    {"section": "plugins", "key": "tensorrt_enabled", "attr": "trt_enabled", "kind": "bool",
     "label": "Accélération TensorRT (extension mvo-rife-trt)"},
    {"section": "plugins", "key": "auto_update", "attr": "plugins_auto_update", "kind": "bool",
     "label": "Mise à jour automatique des extensions"},
)


def config_fields() -> dict[str, dict[str, Any]]:
    """Catalogue des réglages publics : ``section.clé`` → spécification (groupes du panneau Paramètres + extensions)."""
    from core.config import INI_FIELD_GROUPS

    fields: dict[str, dict[str, Any]] = {}
    for group in INI_FIELD_GROUPS:
        for spec in group["fields"]:
            fields[f"{group['section']}.{spec['key']}"] = {**spec, "section": group["section"]}
    for spec in _EXTRA_FIELDS:
        fields[f"{spec['section']}.{spec['key']}"] = dict(spec)
    return fields


def _spec(name: str) -> dict[str, Any]:
    fields = config_fields()
    if name not in fields:
        close = sorted(key for key in fields if key.split(".")[-1] == name.split(".")[-1])
        hint = f" (voulez-vous dire {', '.join(close)} ?)" if close else ""
        raise JobError(f"Réglage inconnu : {name}{hint} — liste : config show", EXIT_ARGS)
    return fields[name]


def _display(value: Any) -> Any:
    return str(value) if isinstance(value, Path) else value


#: Réglages secrets (identifiants de services) : masqués dans une requête de l'API sans autorisation privilégiée.
SECRET_SETTINGS = frozenset({"metadata.tmdb_api_key", "metadata.tmdb_bearer_token"})
_REDACTED = "********"


def _reveal(ctx: ExecutionContext) -> bool:
    """Secrets lisibles : commande historique, ou requête de l'API autorisée (``--allow-privileged``)."""
    return not ctx.api or ctx.allow_privileged


def _app_config(ctx: ExecutionContext) -> Any:
    """Configuration relue ; requête de l'API sans autorisation : une erreur de syntaxe de config.ini est rendue
    sans les lignes fautives (qui peuvent contenir un secret coupé), avec leurs seuls numéros."""
    import configparser

    from core.config import AppConfig, config_syntax_message

    try:
        return ctx.port("app_config", AppConfig)()
    except configparser.Error as exc:
        if _reveal(ctx):
            raise
        raise JobError(config_syntax_message(exc), EXIT_VALIDATION) from None


def describe(config: Any, name: str, spec: dict[str, Any], *, reveal: bool = True) -> dict[str, Any]:
    source, ini_value, settings_value = config.value_source(spec["section"], spec["key"])
    entry: dict[str, Any] = {"key": name, "label": spec.get("label", ""), "kind": spec["kind"],
                             "value": _display(getattr(config, spec["attr"], None)), "source": source}
    if ini_value is not None:
        entry["ini_value"] = ini_value
    if settings_value is not None:
        entry["settings_value"] = settings_value
    if spec.get("options"):
        entry["choices"] = [value for value, _label in spec["options"]]
    if spec.get("steps"):
        entry["choices"] = list(spec["steps"])
    if source == "ini" and settings_value not in (None, "") and settings_value != ini_value:
        entry["masked"] = "config.ini masque le réglage de l'interface"
    if not reveal and name in SECRET_SETTINGS:  # renseigné ou non reste visible, jamais la valeur
        for field_name in ("value", "ini_value", "settings_value"):
            if entry.get(field_name) not in (None, ""):
                entry[field_name] = _REDACTED
        entry["redacted"] = True
    return entry


def coerce(spec: dict[str, Any], raw: str) -> str:
    """Texte écrit dans config.ini, validé selon la nature du réglage (refus explicite sinon)."""
    kind, value = spec["kind"], str(raw).strip()
    if kind == "bool":
        lowered = value.lower()
        if lowered in ("1", "true", "yes", "oui", "on"):
            return "true"
        if lowered in ("0", "false", "no", "non", "off"):
            return "false"
        raise JobError(f"{spec['key']} : booléen attendu (true/false).", EXIT_VALIDATION)
    if kind in ("int", "stepped_slider"):
        try:
            number = int(value)
        except ValueError as exc:
            raise JobError(f"{spec['key']} : entier attendu.", EXIT_VALIDATION) from exc
        low, high = spec.get("min"), spec.get("max")
        if (low is not None and number < low) or (high is not None and number > high):
            raise JobError(f"{spec['key']} : valeur entre {low} et {high} attendue.", EXIT_VALIDATION)
        if spec.get("steps") and number not in spec["steps"]:
            raise JobError(f"{spec['key']} : valeur parmi {list(spec['steps'])} attendue.", EXIT_VALIDATION)
        return str(number)
    if kind == "choice":
        choices = [choice for choice, _label in spec.get("options", ())]
        if value not in choices:
            raise JobError(f"{spec['key']} : valeur parmi {', '.join(map(str, choices))} attendue.", EXIT_VALIDATION)
        return value
    if kind == "language":
        from core.lang_tags import Rfc5646LanguageTags
        from core.ui_language import supported_ui_languages

        supported = sorted(supported_ui_languages())
        # Même conversion que la configuration, sans repli sur la langue du système.
        if len(value) == 3:
            ietf = Rfc5646LanguageTags.from_iso639_2(value)
            code = (Rfc5646LanguageTags.to_iso639_2(ietf) or "").lower() if ietf else ""
        else:
            code = Rfc5646LanguageTags.from_locale_name(value) or ""
        if code not in supported:
            raise JobError(f"{spec['key']} : langue de l'interface parmi {', '.join(supported)}.", EXIT_VALIDATION)
        return code
    if kind in ("directory", "tool"):
        return str(Path(value).expanduser()) if value else ""
    return value


@dataclass(frozen=True)
class ConfigPathRequest(Model):
    """Emplacements de la configuration."""


@dataclass(frozen=True)
class ConfigPathResult(Model):
    config_ini: str
    settings: str | None = None
    config_dir: str | None = None


@operation("config.path", queue="exclusive", request=ConfigPathRequest, result=ConfigPathResult,
           summary="Emplacements de config.ini (surcharges) et du fichier de réglages de l'interface.")
def config_path(_request: ConfigPathRequest, ctx: ExecutionContext) -> ConfigPathResult:
    from core.config import config_ini_path

    config = _app_config(ctx)
    settings = config.settings_file()
    return ConfigPathResult(config_ini=str(config_ini_path()), settings=str(settings) if settings else None,
                            config_dir=str(getattr(config, "config_dir", "") or "") or None)


@dataclass(frozen=True)
class ConfigShowRequest(Model):
    section: str | None = field(default=None, description="Section seule (paths, tools, ui…).")


@dataclass(frozen=True)
class ConfigEntry(Model):
    """Réglage : valeur effective et provenance (clés facultatives absentes quand elles ne s'appliquent pas)."""

    key: str = field(description="Nom `section.clé`.")
    label: str = field()
    kind: str = field(description="Nature : bool, int, text, choice, directory, tool, language, stepped_slider…")
    value: Any = field(description="Valeur effective.")
    source: Literal["ini", "settings", "default"] = field()
    ini_value: str = field(default="", description="Valeur de config.ini (absente sans surcharge).")
    settings_value: str = field(default="", description="Valeur du réglage de l'interface (absente sans réglage).")
    choices: tuple[Any, ...] = field(default=(), description="Valeurs admises (réglages à choix ou à paliers).")
    masked: str = field(default="", description="config.ini masque un réglage différent de l'interface.")
    redacted: bool = field(default=False, description="Secret masqué (requête de l'API sans `--allow-privileged`) : "
                                                      "valeurs renseignées remplacées par `********`.")


@dataclass(frozen=True)
class ConfigEntriesResult(Model):
    entries: tuple[dict[str, Any], ...] = field(default=(), schema=list_of(ConfigEntry))
    warnings: tuple[str, ...] = ()


@operation("config.show", queue="exclusive", request=ConfigShowRequest, result=ConfigEntriesResult,
           summary="Réglages : valeur effective et provenance (config.ini, réglage de l'interface, défaut).")
def config_show(request: ConfigShowRequest, ctx: ExecutionContext) -> ConfigEntriesResult:
    config = _app_config(ctx)
    entries = [describe(config, name, spec, reveal=_reveal(ctx)) for name, spec in config_fields().items()
               if request.section is None or spec["section"] == request.section]
    if request.section is not None and not entries:
        raise JobError(f"Section inconnue : {request.section}", EXIT_ARGS)
    return ConfigEntriesResult(entries=tuple(entries))


@dataclass(frozen=True)
class ConfigKeyRequest(Model):
    key: str = field(description="Réglage `section.clé` (voir config show).")


@operation("config.get", queue="exclusive", request=ConfigKeyRequest, result=ConfigEntriesResult,
           summary="Un réglage : valeur effective et provenance.")
def config_get(request: ConfigKeyRequest, ctx: ExecutionContext) -> ConfigEntriesResult:
    spec = _spec(request.key)
    return ConfigEntriesResult(entries=(describe(_app_config(ctx), request.key, spec, reveal=_reveal(ctx)),))


@dataclass(frozen=True)
class ConfigSetRequest(Model):
    key: str = field(description="Réglage `section.clé`.")
    value: str = field(description="Nouvelle valeur (texte ; booléens true/false).")


@operation("config.set", privileged=True, queue="exclusive", request=ConfigSetRequest, result=ConfigEntriesResult, mutating=True,
           summary="Écrire un réglage dans config.ini (comme Enregistrer du panneau Paramètres).")
def config_set(request: ConfigSetRequest, ctx: ExecutionContext) -> ConfigEntriesResult:
    from core.config import AppConfig, write_ini_settings

    spec = _spec(request.key)
    write_ini_settings({spec["section"]: {spec["key"]: coerce(spec, request.value)}})
    config = ctx.port("app_config", AppConfig)()
    config.reload()
    config.save()
    return ConfigEntriesResult(entries=(describe(config, request.key, spec),))


@operation("config.unset", privileged=True, queue="exclusive", request=ConfigKeyRequest, result=ConfigEntriesResult, mutating=True,
           summary="Retirer la surcharge config.ini d'un réglage (le réglage de l'interface peut réapparaître).")
def config_unset(request: ConfigKeyRequest, ctx: ExecutionContext) -> ConfigEntriesResult:
    from core.config import AppConfig, remove_ini_settings

    spec = _spec(request.key)
    remove_ini_settings({spec["section"]: {spec["key"]}})
    config = ctx.port("app_config", AppConfig)()
    config.reload()
    entry = describe(config, request.key, spec)
    warnings = (("Valeur du réglage de l'interface toujours active (config reset pour revenir au défaut).",)
                if entry["source"] == "settings" else ())
    return ConfigEntriesResult(entries=(entry,), warnings=warnings)


@operation("config.reset", privileged=True, queue="exclusive", request=ConfigKeyRequest, result=ConfigEntriesResult, mutating=True,
           summary="Ramener un réglage au défaut (surcharge config.ini et réglage de l'interface retirés).")
def config_reset(request: ConfigKeyRequest, ctx: ExecutionContext) -> ConfigEntriesResult:
    from core.config import AppConfig, remove_ini_settings

    spec = _spec(request.key)
    remove_ini_settings({spec["section"]: {spec["key"]}})
    config = ctx.port("app_config", AppConfig)()
    config.forget_setting(spec["section"], spec["key"])
    config.reload()
    return ConfigEntriesResult(entries=(describe(config, request.key, spec),))


# ---------------------------------------------------------------------------
# Dossier de travail
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkdirRequest(Model):
    """Éléments du dossier de travail nettoyables (jobs actifs exclus, verrous respectés)."""

    dry_run: bool = field(default=False, description="workdir.clean : lister sans supprimer.")


@dataclass(frozen=True)
class WorkdirEntry(Model):
    """Élément nettoyable du dossier de travail."""

    path: str
    size_bytes: int


@dataclass(frozen=True)
class WorkdirResult(Model):
    work_dir: str
    entries: tuple[dict[str, Any], ...] = field(default=(), schema=list_of(WorkdirEntry))
    removed: tuple[str, ...] = ()
    dry_run: bool = False


def _entries(paths: list[Path]) -> tuple[dict[str, Any], ...]:
    def size(path: Path) -> int:
        try:
            if path.is_file():
                return path.stat().st_size
            return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
        except OSError:
            return 0

    return tuple({"path": str(path), "size_bytes": size(path)} for path in paths)


@operation("workdir.list", request=WorkdirRequest, result=WorkdirResult,
           summary="Éléments nettoyables du dossier de travail (dossiers abandonnés ; jobs actifs exclus).")
def workdir_list(_request: WorkdirRequest, ctx: ExecutionContext) -> WorkdirResult:
    from core.workdir import cleanable_work_dir_entries

    work_dir = Path(ctx.overrides.work_dir or ctx.config.work_dir)
    return WorkdirResult(work_dir=str(work_dir), entries=_entries(cleanable_work_dir_entries(work_dir)))


@operation("workdir.clean", privileged=True, queue="exclusive", request=WorkdirRequest, result=WorkdirResult, mutating=True,
           summary="Nettoyer le dossier de travail (comme « Nettoyer » au démarrage) ; dry_run : liste seule.")
def workdir_clean(request: WorkdirRequest, ctx: ExecutionContext) -> WorkdirResult:
    from core.workdir import cleanable_work_dir_entries, clear_work_dir

    work_dir = Path(ctx.overrides.work_dir or ctx.config.work_dir)
    entries = _entries(cleanable_work_dir_entries(work_dir))
    if request.dry_run:
        return WorkdirResult(work_dir=str(work_dir), entries=entries, dry_run=True)
    removed = clear_work_dir(work_dir)
    return WorkdirResult(work_dir=str(work_dir), entries=entries, removed=tuple(str(path) for path in removed))


# ---------------------------------------------------------------------------
# NVEncC (« extension externe » de la page Extensions)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NvenccStatusRequest(Model):
    check_latest: bool = field(default=False, description="Interroger GitHub pour la dernière version.")


@dataclass(frozen=True)
class NvenccStatusResult(Model):
    supported: bool
    nvidia: bool
    method: str = field(description="Installation prévue : apt, dnf, user (dossier utilisateur) ou windows.")
    path: str | None = None
    version: str = ""
    provenance: str = field(default="", description="bundled (paquet all-inclusive), user (Muxiveo), system.")
    user_binary: str | None = None
    latest_version: str = ""
    update_available: bool = False


def _nvencc_status(ctx: ExecutionContext, check_latest: bool) -> NvenccStatusResult:
    from core import nvencc_install as nv

    supported = nv.supported_platform()
    tool = nv.describe(str(getattr(ctx.config, "tool_nvencc", "") or "")) if supported else None
    latest = nv.latest_version() if supported and check_latest else ""
    user = nv.user_binary() if supported else None
    update = bool(tool and latest and tool.version and nv.version_key(latest) > nv.version_key(tool.version))
    return NvenccStatusResult(
        supported=supported, nvidia=nv.nvidia_present() if supported else False,
        method=nv.plan_method() if supported else "", path=str(tool.path) if tool else None,
        version=tool.version if tool else "", provenance=tool.provenance if tool else "",
        user_binary=str(user) if user else None, latest_version=latest, update_available=update)


@operation("nvencc.status", request=NvenccStatusRequest, result=NvenccStatusResult,
           summary="NVEncC : présence, version, provenance, méthode d'installation, mise à jour disponible.")
def nvencc_status(request: NvenccStatusRequest, ctx: ExecutionContext) -> NvenccStatusResult:
    return _nvencc_status(ctx, request.check_latest)


@dataclass(frozen=True)
class NvenccInstallRequest(Model):
    """Installation (ou mise à jour) de NVEncC, comme la carte NVEncC de la page Extensions."""

    method: str | None = field(default=None, description="apt, dnf, user ou windows (défaut : selon la machine).")
    only_if_newer: bool = field(default=False, description="Mise à jour : rien si la version installée est la dernière.")
    interactive: bool | None = field(default=None, description="Élévation sudo autorisée (défaut : terminal).")
    elevate: bool = field(default=True, description="Élévation (sudo, pkexec) autorisée ; false : dossier utilisateur "
                                                    "sans aucune élévation.")


@dataclass(frozen=True)
class NvenccInstallResult(Model):
    path: str | None = None
    version: str = ""
    installed: bool = False
    message: str = ""


@operation("nvencc.install", privileged=True, queue="exclusive", request=NvenccInstallRequest, result=NvenccInstallResult, long_running=True, progress=True,
           mutating=True, summary="Installer ou mettre à jour NVEncC (paquet apt/dnf, sinon dossier utilisateur).")
def nvencc_install(request: NvenccInstallRequest, ctx: ExecutionContext) -> NvenccInstallResult:
    from core import nvencc_install as nv
    from core.service.errors import ApiError, ErrorCode

    if request.method is not None and request.method not in (nv.METHOD_APT, nv.METHOD_DNF, nv.METHOD_USER,
                                                             nv.METHOD_WINDOWS):
        raise JobError(f"Méthode inconnue : {request.method} (apt, dnf, user, windows).", EXIT_ARGS)
    status = _nvencc_status(ctx, request.only_if_newer)
    if not status.supported:
        raise JobError("NVEncC n'existe que pour Linux et Windows x86-64.", EXIT_VALIDATION)
    if request.only_if_newer and status.path and not status.update_available:
        return NvenccInstallResult(path=status.path, version=status.version,
                                   message="NVEncC est à jour." if status.latest_version else
                                   "Dernière version inconnue (GitHub injoignable) : rien n'est fait.")

    def progress(done: int, total: int) -> None:
        if total > 0:
            ctx.events.progress(pct=round(done * 100 / total, 1), stage="download", label="Téléchargement de NVEncC")

    try:
        path = nv.install(request.method, progress=progress, cancel=ctx.cancel.event,
                          log=lambda level, message: (ctx.events.warning(message, code="nvencc_install")
                                                       if level.upper().startswith("WARN")
                                                      else ctx.events.log(level.lower(), message)),
                          interactive=request.interactive, elevate=request.elevate)
    except nv.NvenccCancelled as exc:
        raise ApiError(ErrorCode.CANCELLED, "Installation de NVEncC annulée.") from exc
    except nv.NvenccInstallError as exc:
        raise JobError(f"NVEncC : {exc}", 6) from exc
    ctx.events.artifact("nvencc", str(path))
    return NvenccInstallResult(path=str(path), version=nv.read_version(path), installed=True,
                               message=f"NVEncC installé : {path}")


@dataclass(frozen=True)
class NvenccRemoveRequest(Model):
    """Suppression de la copie de NVEncC installée par Muxiveo dans le dossier utilisateur."""


@dataclass(frozen=True)
class NvenccRemoveResult(Model):
    removed: bool
    message: str = ""


@operation("nvencc.remove", privileged=True, queue="exclusive", request=NvenccRemoveRequest, result=NvenccRemoveResult, mutating=True,
           summary="Supprimer la copie utilisateur de NVEncC (un paquet système se retire par apt / dnf).")
def nvencc_remove(_request: NvenccRemoveRequest, ctx: ExecutionContext) -> NvenccRemoveResult:
    from core import nvencc_install as nv

    if nv.user_binary() is None:
        status = _nvencc_status(ctx, False)
        hint = (" ; NVEncC du système (paquet) : le retirer par le gestionnaire de paquets"
                if status.provenance == nv.PROVENANCE_SYSTEM else "")
        return NvenccRemoveResult(removed=False, message="Aucune copie utilisateur de NVEncC" + hint + ".")
    if not nv.remove_user():
        raise JobError("NVEncC est en cours d'utilisation : fermer les encodages puis réessayer.", 6)
    return NvenccRemoveResult(removed=True, message=f"Copie utilisateur supprimée : {nv.user_root()}")
