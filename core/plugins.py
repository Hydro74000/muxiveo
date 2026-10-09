"""
core/plugins.py — Extensions facultatives téléchargées à la demande (dépôt muxiveo-plugins).

Registre (``EXTENSIONS``) :

- ``mvo-rife`` : interpolation d'images (moteur muxiveo-rife, modèles, poids du sélecteur, préréglages) ;
  Linux et Windows x86-64, macOS arm64 ;
- ``mvo-rife-trt`` : accélération NVIDIA (TensorRT for RTX) de l'inférence RIFE de muxiveo-rife, proposée
  uniquement sur les GPU NVIDIA compatibles (Linux et Windows x86-64).

Version installée : la plus récente compatible annoncée par le flux de l'extension (release
``extensions-feed``, ``<extension>.json`` : contrat de mvo-rife, interface de mvo-rife-trt), jamais en dessous
de la version épinglée par Muxiveo (``MVO_*_VERSION``), qui sert aussi de repli quand le flux est injoignable.

Emplacements, jamais dans le paquet de l'application (AppImage en lecture seule, MSIX, Program Files) :

- Linux : ``$XDG_DATA_HOME/muxiveo/plugins/<extension>/<version>/`` ;
  moteurs TensorRT : ``$XDG_CACHE_HOME/muxiveo/trt-engines/`` ;
- Windows : ``%LOCALAPPDATA%\\Muxiveo\\plugins\\…`` et ``%LOCALAPPDATA%\\Muxiveo\\cache\\trt-engines\\`` ;
- macOS : ``~/Library/Application Support/Muxiveo/plugins/…``.

Installation : archive vérifiée (SHA-256 publié par GitHub), extraction dans un dossier temporaire, contrôle du
manifeste (extension, contrat ou interface, plate-forme, SHA-256 de chaque fichier), déplacement, puis bascule
atomique du pointeur ``current.json`` ; l'ancienne version est supprimée ensuite (ou au prochain nettoyage si
elle est encore utilisée).
"""

from __future__ import annotations

import json
import os
import platform as _platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
import uuid
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from core.atomic_io import atomic_write_text
from core.github_release import ReleaseAsset, fetch_release_by_repo, file_sha256, select_asset, verify_download
from core.subprocess_utils import subprocess_text_kwargs
from core.version import (
    APP_USER_AGENT,
    MUXIVEO_PLUGINS_REPOSITORY,
    MVO_RIFE_CONTRACT,
    MVO_RIFE_TRT_VERSION,
    MVO_RIFE_VERSION,
)

TRT_PLUGIN_ID = "mvo-rife-trt"
# Interface C attendue par muxiveo-rife (mvo-rife/src/trt_plugin_abi.h du dépôt muxiveo-plugins).
TRT_PLUGIN_ABI = 1
TRT_LIBRARIES = {"linux-x86_64": "libmvo_rife_trt.so", "windows-x86_64": "mvo_rife_trt.dll"}
TRT_LICENSE_URL = "https://docs.nvidia.com/deeplearning/tensorrt-rtx/latest/reference/sla.html"
RIFE_PLUGIN_ID = "mvo-rife"
RIFE_EXECUTABLES = {
    "linux-x86_64": "muxiveo-rife",
    "windows-x86_64": "muxiveo-rife.exe",
    "macos-arm64": "muxiveo-rife",
}
# Release fixe du flux des extensions (un ``<extension>.json`` par extension, écrit par sa CI).
FEED_TAG = "extensions-feed"
FEED_SCHEMA = 1
_FEED_MAX_BYTES = 1 << 20
_VERSION_RE = re.compile(r"^\d+(\.\d+){1,3}$")

_POINTER = "current.json"
_DOWNLOAD_CHUNK = 1 << 20

ProgressFn = Callable[[int, int], None]


class PluginError(RuntimeError):
    """Installation, mise à jour ou suppression impossible (message destiné à l'utilisateur)."""


class PluginCancelled(PluginError):
    """Opération annulée par l'utilisateur."""


@dataclass(frozen=True)
class InstalledPlugin:
    """Extension installée et vérifiée (structure, interface, plate-forme)."""

    version: str
    path: Path
    manifest: dict

    @property
    def size_bytes(self) -> int:
        return sum(p.stat().st_size for p in self.path.rglob("*") if p.is_file())


def platform_tag(sys_platform: str | None = None, machine: str | None = None) -> str | None:
    """Plate-forme des extensions (``linux-x86_64``, ``windows-x86_64``, ``macos-arm64``), ``None`` sinon."""
    sys_platform = sys.platform if sys_platform is None else sys_platform
    machine = (_platform.machine() if machine is None else machine).lower()
    if sys_platform == "darwin":
        return "macos-arm64" if machine in {"arm64", "aarch64"} else None
    if machine not in {"x86_64", "amd64"}:
        return None
    if sys_platform.startswith("linux"):
        return "linux-x86_64"
    if sys_platform == "win32":
        return "windows-x86_64"
    return None


def _local_base(env: Mapping[str, str], sys_platform: str, cache: bool) -> Path:
    if sys_platform == "win32":
        local = env.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        base = Path(local) / "Muxiveo"
        return base / "cache" if cache else base
    if sys_platform == "darwin":
        return Path.home() / "Library" / ("Caches" if cache else "Application Support") / "Muxiveo"
    xdg = env.get("XDG_CACHE_HOME" if cache else "XDG_DATA_HOME")
    default = Path.home() / (".cache" if cache else ".local/share")
    return (Path(xdg) if xdg else default) / "muxiveo"


def plugins_root(env: Mapping[str, str] | None = None, sys_platform: str | None = None) -> Path:
    """Dossier des extensions (dossier utilisateur, hors du paquet de l'application)."""
    return _local_base(os.environ if env is None else env, sys.platform if sys_platform is None else sys_platform, False) / "plugins"


def trt_engine_cache_dir(env: Mapping[str, str] | None = None, sys_platform: str | None = None) -> Path:
    """Cache des moteurs TensorRT (reconstructibles), passé à muxiveo-rife par ``--trt-cache``."""
    return _local_base(os.environ if env is None else env, sys.platform if sys_platform is None else sys_platform, True) / "trt-engines"


def version_key(version: str) -> tuple[int, ...]:
    """Clé de comparaison d'une version ``X.Y.Z`` (composantes manquantes à zéro)."""
    parts = tuple(int(p) for p in str(version).split(".") if p.isdigit())
    return parts + (0,) * (4 - len(parts))


def _check_rife(manifest: Mapping) -> str | None:
    contract = manifest.get("contract")
    if contract != MVO_RIFE_CONTRACT:
        return f"contrat {contract} non pris en charge (version {MVO_RIFE_CONTRACT} attendue)"
    return None


def _check_trt(manifest: Mapping) -> str | None:
    abi = manifest.get("abi")
    if abi != TRT_PLUGIN_ABI:
        return f"interface {abi} incompatible (version {TRT_PLUGIN_ABI} attendue)"
    return None


@dataclass(frozen=True)
class PluginSpec:
    """Extension connue de Muxiveo : version épinglée, fichier principal par plate-forme, compatibilité."""

    id: str
    min_version: str                                # plancher et repli (flux injoignable)
    files: Mapping[str, str]                        # plate-forme → exécutable ou bibliothèque principale
    check: Callable[[Mapping], str | None]          # raison d'incompatibilité d'un manifeste (ou d'une entrée du flux)
    cache_dir: Callable[[], Path] | None = None     # cache reconstructible vidé au changement de version

    def tag(self, version: str) -> str:
        return f"{self.id}-v{version}"

    def supports(self, platform: str | None) -> bool:
        return platform is not None and platform in self.files


RIFE = PluginSpec(RIFE_PLUGIN_ID, MVO_RIFE_VERSION, RIFE_EXECUTABLES, _check_rife)
# Cache lu au moment de l'appel (tests : emplacement isolé par tests/conftest.py).
TRT = PluginSpec(TRT_PLUGIN_ID, MVO_RIFE_TRT_VERSION, TRT_LIBRARIES, _check_trt, lambda: trt_engine_cache_dir())
EXTENSIONS: dict[str, PluginSpec] = {spec.id: spec for spec in (RIFE, TRT)}


def _plugin_dir(spec: PluginSpec, root: Path | None) -> Path:
    return (root or plugins_root()) / spec.id


def _validate(spec: PluginSpec, path: Path, platform: str) -> dict:
    """Manifeste d'une extension extraite ; lève PluginError si elle est incomplète ou incompatible."""
    try:
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PluginError(f"manifeste illisible ({exc})") from exc
    if not isinstance(manifest, dict) or manifest.get("name") != spec.id:
        raise PluginError("archive d'une autre extension")
    reason = spec.check(manifest)
    if reason:
        raise PluginError(reason)
    if manifest.get("platform") != platform:
        raise PluginError(f"plate-forme {manifest.get('platform')} au lieu de {platform}")
    if not spec.supports(platform) or not (path / spec.files[platform]).is_file():
        raise PluginError("fichier principal de l'extension absent")
    return manifest


def installed_plugin(spec: PluginSpec, root: Path | None = None, platform: str | None = None) -> InstalledPlugin | None:
    """Extension active (pointeur ``current.json``), ``None`` si absente ou inutilisable."""
    platform = platform or platform_tag()
    if platform is None:
        return None
    base = _plugin_dir(spec, root)
    try:
        pointer = json.loads((base / _POINTER).read_text(encoding="utf-8"))
        version = str(pointer["version"])
        path = base / str(pointer["dir"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if path.parent != base or not path.is_dir():
        return None
    try:
        manifest = _validate(spec, path, platform)
    except PluginError:
        return None
    return InstalledPlugin(version, path, manifest)


def main_file(spec: PluginSpec, plugin: InstalledPlugin, platform: str | None = None) -> Path:
    """Exécutable ou bibliothèque principale d'une extension installée."""
    platform = platform or platform_tag() or ""
    return plugin.path / spec.files[platform]


def rife_executable(root: Path | None = None) -> Path | None:
    """muxiveo-rife de l'extension mvo-rife installée, ``None`` sans extension."""
    plugin = installed_plugin(RIFE, root)
    return main_file(RIFE, plugin) if plugin is not None else None


# ---------------------------------------------------------------------------
# Flux des versions publiées
# ---------------------------------------------------------------------------


def feed_url(spec: PluginSpec) -> str:
    return f"https://github.com/{MUXIVEO_PLUGINS_REPOSITORY}/releases/download/{FEED_TAG}/{spec.id}.json"


def parse_feed(spec: PluginSpec, data: object) -> list[dict] | None:
    """Entrées valides du flux d'une extension (version, tag, plates-formes), ``None`` si le flux est invalide."""
    if not isinstance(data, dict) or data.get("schema") != FEED_SCHEMA or data.get("name") != spec.id:
        return None
    releases = data.get("releases")
    if not isinstance(releases, list):
        return None
    entries: list[dict] = []
    for entry in releases:
        if not isinstance(entry, dict):
            continue
        version = str(entry.get("version") or "")
        platforms = entry.get("platforms")
        if (not _VERSION_RE.match(version) or entry.get("tag") != spec.tag(version)
                or not isinstance(platforms, list)):
            continue
        entries.append(entry)
    return entries


def fetch_feed(spec: PluginSpec, timeout: float = 10.0) -> list[dict] | None:
    """Versions publiées de l'extension (flux ``<extension>.json``) ; ``None`` si injoignable ou invalide."""
    req = urllib.request.Request(feed_url(spec), headers={"User-Agent": APP_USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 — URL github.com constante
            raw = resp.read(_FEED_MAX_BYTES + 1)
    except (OSError, ValueError):
        return None
    if len(raw) > _FEED_MAX_BYTES:
        return None
    try:
        return parse_feed(spec, json.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, ValueError):
        return None


def target_version(spec: PluginSpec, feed: list[dict] | None, platform: str | None = None) -> str:
    """Version à installer : la plus récente compatible du flux pour cette plate-forme, sinon la version épinglée."""
    platform = platform or platform_tag()
    best = spec.min_version
    for entry in feed or []:
        version = str(entry["version"])
        if version_key(version) <= version_key(best) or spec.check(entry):
            continue
        if any(isinstance(p, dict) and p.get("platform") == platform for p in entry["platforms"]):
            best = version
    return best


def update_available(spec: PluginSpec, target: str | None = None, root: Path | None = None) -> bool:
    """Vrai si l'extension installée est antérieure à la version cible (épinglée par défaut)."""
    plugin = installed_plugin(spec, root)
    return plugin is not None and version_key(plugin.version) < version_key(target or spec.min_version)


def release_asset(spec: PluginSpec, version: str | None = None, platform: str | None = None,
                  timeout: float = 30.0) -> ReleaseAsset:
    """Archive d'une version pour cette plate-forme (SHA-256 publié par GitHub obligatoire)."""
    platform = platform or platform_tag()
    version = version or spec.min_version
    if not spec.supports(platform):
        raise PluginError("plate-forme non prise en charge")
    tag = spec.tag(version)
    try:
        release = fetch_release_by_repo(MUXIVEO_PLUGINS_REPOSITORY, tag, user_agent=APP_USER_AGENT, timeout=timeout)
        return select_asset(release, MUXIVEO_PLUGINS_REPOSITORY, f"{spec.id}-{version}-{platform}.")
    except (OSError, ValueError, RuntimeError) as exc:
        raise PluginError(f"release {tag} introuvable ({exc})") from exc


def asset_size(asset: ReleaseAsset, timeout: float = 15.0) -> int:
    """Taille de l'archive en octets (0 si inconnue)."""
    req = urllib.request.Request(asset.url, method="HEAD", headers={"User-Agent": APP_USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 — URL github.com vérifiée
            return int(resp.headers.get("Content-Length") or 0)
    except (OSError, ValueError):
        return 0


def _download(asset: ReleaseAsset, dest: Path, progress: ProgressFn | None, cancel: threading.Event | None) -> None:
    req = urllib.request.Request(asset.url, headers={"User-Agent": APP_USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp, dest.open("wb") as out:  # nosec B310 — URL vérifiée
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            while True:
                if cancel is not None and cancel.is_set():
                    raise PluginCancelled("installation annulée")
                chunk = resp.read(_DOWNLOAD_CHUNK)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    except PluginError:
        raise
    except OSError as exc:
        raise PluginError(f"téléchargement interrompu ({exc})") from exc
    try:
        verify_download(asset, dest)
    except RuntimeError as exc:
        raise PluginError(str(exc)) from exc


def _safe_member(staging: Path, name: str) -> Path:
    target = (staging / name).resolve()
    if staging.resolve() not in target.parents and target != staging.resolve():
        raise PluginError(f"chemin d'archive refusé : {name}")
    return target


def _extract(archive: Path, staging: Path, is_zip: bool) -> None:
    """Extraction contrôlée : chemins confinés au dossier, fichiers et dossiers seulement."""
    try:
        _extract_members(archive, staging, is_zip)
    except (tarfile.TarError, zipfile.BadZipFile, OSError, EOFError) as exc:
        raise PluginError(f"archive illisible ({exc})") from exc


def _extract_members(archive: Path, staging: Path, is_zip: bool) -> None:
    if is_zip:
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                _safe_member(staging, info.filename)
            z.extractall(staging)
        return
    with tarfile.open(archive, "r:gz") as t:
        members = t.getmembers()
        for m in members:
            _safe_member(staging, m.name)
            if not (m.isfile() or m.isdir()):
                raise PluginError(f"élément d'archive refusé : {m.name}")
        # Membres déjà contrôlés ci-dessus ; filtre « data » en plus quand Python le fournit (3.11.4+).
        if hasattr(tarfile, "data_filter"):
            t.extractall(staging, members=members, filter="data")
        else:
            t.extractall(staging, members=members)  # nosec B202 — membres validés (chemins, types)


def _verify_files(path: Path, manifest: dict) -> None:
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise PluginError("manifeste sans liste de fichiers")
    for rel, digest in files.items():
        target = path / rel
        if not target.is_file() or file_sha256(target) != str(digest).lower():
            raise PluginError(f"fichier altéré ou absent : {rel}")


def _remove_tree(path: Path) -> bool:
    try:
        shutil.rmtree(path)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False  # bibliothèque encore chargée (Windows) : retirée au prochain nettoyage


def _cache_of(spec: PluginSpec, cache_dir: Path | None) -> Path | None:
    if cache_dir is not None:
        return cache_dir
    return spec.cache_dir() if spec.cache_dir is not None else None


def install(
    spec: PluginSpec,
    root: Path | None = None,
    *,
    version: str | None = None,
    progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
    asset: ReleaseAsset | None = None,
    cache_dir: Path | None = None,
) -> InstalledPlugin:
    """Télécharge, vérifie et active une version (épinglée par défaut) ; l'ancienne reste active en cas d'échec.

    Changement de version : le cache reconstructible de l'extension est vidé (TensorRT : modèles ONNX
    éventuellement différents sous le même nom ; le préchauffage le reconstruit).
    """
    platform = platform_tag()
    if not spec.supports(platform):
        raise PluginError("plate-forme non prise en charge")
    assert platform is not None
    version = version or spec.min_version
    previous = installed_plugin(spec, root, platform)
    base = _plugin_dir(spec, root)
    base.mkdir(parents=True, exist_ok=True)
    asset = asset or release_asset(spec, version, platform)
    token = uuid.uuid4().hex[:12]
    archive = base / f".download-{token}.part"
    staging = base / f".staging-{token}"
    try:
        _download(asset, archive, progress, cancel)
        staging.mkdir()
        _extract(archive, staging, asset.name.endswith(".zip"))
        entries = [p for p in staging.iterdir()]
        if len(entries) != 1 or not entries[0].is_dir():
            raise PluginError("structure d'archive inattendue")
        manifest = _validate(spec, entries[0], platform)
        if str(manifest.get("version")) != version:
            raise PluginError(f"version {manifest.get('version')} au lieu de {version}")
        _verify_files(entries[0], manifest)
        target = base / f"{version}-{token}"
        entries[0].rename(target)
        atomic_write_text(base / _POINTER, json.dumps(
            {"version": version, "dir": target.name, "installed_at": int(time.time())}, indent=2,
        ) + "\n")
    finally:
        archive.unlink(missing_ok=True)
        _remove_tree(staging)
    cache = _cache_of(spec, cache_dir)
    if (previous is None or previous.version != version) and cache is not None:
        _remove_tree(cache)
    cleanup_orphans(spec, root)
    plugin = installed_plugin(spec, root, platform)
    if plugin is None:
        raise PluginError("extension installée mais illisible")
    return plugin


def remove(spec: PluginSpec, root: Path | None = None, cache_dir: Path | None = None) -> bool:
    """Supprime l'extension (et son cache) ; faux si des fichiers encore utilisés restent."""
    base = _plugin_dir(spec, root)
    (base / _POINTER).unlink(missing_ok=True)
    complete = _remove_tree(base)
    cache = _cache_of(spec, cache_dir)
    if cache is not None:
        _remove_tree(cache)
    return complete


def _probe_y4m() -> bytes:
    """Deux images 64x64 différentes (y4m 4:2:0 8 bits) : une inférence RIFE, sans coupe ni image figée."""
    w = h = 64
    size = w * h * 3 // 2
    first = bytes(size)
    second = bytes((i * 37) % 256 for i in range(w * h)) + bytes([128]) * (size - w * h)
    return f"YUV4MPEG2 W{w} H{h} F25:1 Ip A1:1 C420jpeg\n".encode() + b"FRAME\n" + first + b"FRAME\n" + second


def warm_up(rife_bin: str, plugin: InstalledPlugin, cache_dir: Path | None = None,
            log: Callable[[str], None] | None = None, timeout: float = 300.0) -> list[str]:
    """Prépare (et met en cache) le moteur TensorRT de chaque modèle fourni ; retourne les échecs.

    Sert aussi de test de bon fonctionnement juste après l'installation. Les modèles dont muxiveo-rife n'a
    pas la version ncnn (repli Vulkan) sont ignorés.
    """
    cache_dir = cache_dir or trt_engine_cache_dir()
    models_dir = Path(shutil.which(rife_bin) or rife_bin).resolve().parent / "rife-models"
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="muxiveo-trt-") as tmp:
        source = Path(tmp) / "probe.y4m"
        source.write_bytes(_probe_y4m())
        for model in plugin.manifest.get("models") or []:
            name = str(model)
            uhd = name.endswith("-uhd")
            base = name[: -len("-uhd")] if uhd else name
            if not (models_dir / base / "flownet.param").is_file():
                continue
            cmd = [str(rife_bin), "-i", str(source), "-o", os.devnull, "-m", base, "--factor", "2",
                   "--matrix", "bt709", "--scene-threshold", "0", "--backend", "tensorrt", "--quiet",
                   "--trt-plugin", str(plugin.path), "--trt-cache", str(cache_dir)]
            if uhd:
                cmd.append("--uhd")
            if log:
                log(name)
            try:
                # Binaire muxiveo-rife configuré, arguments construits ici, sans shell.
                # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
                result = subprocess.run(  # nosec B603
                    cmd, capture_output=True, check=False, timeout=timeout, **subprocess_text_kwargs()
                )
            except (OSError, subprocess.SubprocessError) as exc:
                failures.append(f"{name} : {exc}")
                continue
            if result.returncode != 0:
                detail = next((line for line in (result.stderr or "").splitlines() if line.startswith("error:")), "")
                failures.append(f"{name} : {detail or f'code {result.returncode}'}")
    return failures


def cleanup_orphans(spec: PluginSpec, root: Path | None = None) -> None:
    """Retire les versions remplacées et les restes de téléchargements interrompus."""
    base = _plugin_dir(spec, root)
    if not base.is_dir():
        return
    current = installed_plugin(spec, root)
    for entry in base.iterdir():
        if entry.name == _POINTER or (current is not None and entry == current.path):
            continue
        if entry.is_dir():
            _remove_tree(entry)
        elif entry.name.startswith(".download-"):
            entry.unlink(missing_ok=True)


__all__ = [
    "EXTENSIONS",
    "FEED_TAG",
    "InstalledPlugin",
    "PluginCancelled",
    "PluginError",
    "PluginSpec",
    "RIFE",
    "RIFE_PLUGIN_ID",
    "TRT",
    "TRT_LICENSE_URL",
    "TRT_PLUGIN_ID",
    "asset_size",
    "cleanup_orphans",
    "feed_url",
    "fetch_feed",
    "install",
    "installed_plugin",
    "main_file",
    "parse_feed",
    "platform_tag",
    "plugins_root",
    "release_asset",
    "remove",
    "rife_executable",
    "target_version",
    "trt_engine_cache_dir",
    "update_available",
    "version_key",
    "warm_up",
]
