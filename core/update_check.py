"""
core/update_check.py — Vérification passive d'une nouvelle version publiée sur GitHub.

Canaux :
- « stable »   : dernière release stable (`releases/latest`, publiée depuis `main`) ;
- « unstable » : release la plus récente, pré-versions `-unstable.*` de `devel-cli` comprises.

Aucune dépendance externe (urllib uniquement). Toute erreur est silencieuse :
la vérification ne doit jamais gêner l'utilisateur.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
from dataclasses import dataclass

from core.version import APP_BUILD_VERSION, APP_IS_UNSTABLE_BUILD, APP_REPOSITORY, APP_REPOSITORY_URL, APP_USER_AGENT

RELEASES_API_URL = f"https://api.github.com/repos/{APP_REPOSITORY}/releases"
LATEST_RELEASE_API_URL = f"{RELEASES_API_URL}/latest"
RELEASE_DOWNLOAD_URL_PREFIX = f"{APP_REPOSITORY_URL}releases/download/"
# Délai total (connexion + lecture) d'une vérification : au-delà, abandon silencieux.
UPDATE_CHECK_TIMEOUT_S = 5.0
# Pré-versions récentes examinées en canal unstable (requête légère).
UNSTABLE_RELEASES_PAGE_SIZE = 10
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_READ_CHUNK_BYTES = 16 * 1024

UPDATE_CHANNEL_STABLE = "stable"
UPDATE_CHANNEL_UNSTABLE = "unstable"
UPDATE_CHANNELS = (UPDATE_CHANNEL_STABLE, UPDATE_CHANNEL_UNSTABLE)
DEFAULT_UPDATE_CHANNEL = UPDATE_CHANNEL_UNSTABLE if APP_IS_UNSTABLE_BUILD else UPDATE_CHANNEL_STABLE

_VERSION_RE = re.compile(r"^[vV]?(\d+)(?:\.(\d+))?(?:\.(\d+))?(.*)$")
_UNSTABLE_RE = re.compile(r"^-unstable\.(\d+)\.(\d+)")


@dataclass(frozen=True)
class ReleaseAsset:
    """Fichier attaché à une release GitHub."""

    name: str
    url: str
    size: int = 0


@dataclass(frozen=True)
class UpdateInfo:
    """Release publiée."""

    version: str
    url: str
    assets: tuple[ReleaseAsset, ...] = ()
    prerelease: bool = False

    @property
    def is_newer(self) -> bool:
        return is_newer(self.version)

    @property
    def display_version(self) -> str:
        return display_version(self.version)

    def asset(self, name: str) -> ReleaseAsset | None:
        return next((a for a in self.assets if a.name == name), None)


def normalize_update_channel(value: str | None) -> str:
    """Canal valide, ou le canal par défaut du build courant."""
    channel = str(value or "").strip().lower()
    return channel if channel in UPDATE_CHANNELS else DEFAULT_UPDATE_CHANNEL


def version_key(tag: str) -> tuple[int, ...]:
    """
    Clé de tri d'une version (vide si non reconnue).

    `X.Y.Z-unstable.<date>.<run>.<sha>` précède `X.Y.Z` (convention semver) et les
    pré-versions d'une même base sont ordonnées par date puis numéro de run CI.
    """
    match = _VERSION_RE.match((tag or "").strip())
    if not match:
        return ()
    base = tuple(int(g or 0) for g in match.group(1, 2, 3))
    suffix = match.group(4)
    if not suffix.startswith("-"):
        return (*base, 1, 0, 0)
    unstable = _UNSTABLE_RE.match(suffix)
    if unstable:
        return (*base, 0, int(unstable.group(1)), int(unstable.group(2)))
    return (*base, 0, 0, 0)


def is_newer(remote: str, local: str = APP_BUILD_VERSION) -> bool:
    """True si `remote` est strictement plus récente que `local`."""
    remote_key = version_key(remote)
    return bool(remote_key) and remote_key > version_key(local)


def display_version(version: str) -> str:
    """Version courte pour l'UI : « 4.0.0-unstable.20260924.123.abc » → « 4.0.0-unstable.123 »."""
    clean = (version or "").strip().lstrip("vV")
    match = re.match(r"^(.*?)-unstable\.\d+\.(\d+)", clean)
    return f"{match.group(1)}-unstable.{match.group(2)}" if match else clean


def is_repository_url(url: str, prefix: str = APP_REPOSITORY_URL) -> bool:
    """True si `url` pointe dans le dépôt Muxiveo (owner/repo insensibles à la casse sur GitHub)."""
    return (url or "").lower().startswith(prefix.lower())


def release_page_url(version: str) -> str:
    """Page GitHub de la release `version`."""
    return f"{APP_REPOSITORY_URL}releases/tag/v{(version or '').strip().lstrip('vV')}"


def _parse_assets(raw: object) -> tuple[ReleaseAsset, ...]:
    """Ne garde que les assets téléchargeables depuis le dépôt Muxiveo."""
    if not isinstance(raw, list):
        return ()
    assets: list[ReleaseAsset] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        url = str(item.get("browser_download_url") or "")
        if not name or not is_repository_url(url, RELEASE_DOWNLOAD_URL_PREFIX):
            continue
        try:
            size = int(item.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        assets.append(ReleaseAsset(name=name, url=url, size=size))
    return tuple(assets)


def _parse_release(payload: object) -> UpdateInfo | None:
    if not isinstance(payload, dict) or payload.get("draft"):
        return None
    tag = str(payload.get("tag_name") or "").strip()
    if not version_key(tag):
        return None
    url = str(payload.get("html_url") or "")
    if not is_repository_url(url):
        url = release_page_url(tag)
    return UpdateInfo(
        version=tag.lstrip("vV"),
        url=url,
        assets=_parse_assets(payload.get("assets")),
        prerelease=bool(payload.get("prerelease")),
    )


class UpdateCheckError(Exception):
    """Vérification impossible (réseau, SSL, délai dépassé, réponse GitHub invalide)."""


def _get_json(url: str, timeout: float) -> object:
    """GET JSON borné : `timeout` couvre toute la requête (connexion, en-têtes et corps).

    ``urlopen`` attend les en-têtes sans échéance globale (le timeout socket
    s'applique à chaque lecture) : la requête tourne dans un thread démon et
    l'appelant n'attend jamais plus que `timeout`.
    """
    outcome: dict[str, object] = {}

    def _worker() -> None:
        try:
            outcome["value"] = _fetch_json(url, timeout)
        except BaseException as exc:  # relayée à l'appelant
            outcome["error"] = exc

    worker = threading.Thread(target=_worker, name="update-check-http", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise TimeoutError(f"délai de {timeout:g} s dépassé")
    if "error" in outcome:
        raise outcome["error"]  # type: ignore[misc]
    return outcome.get("value")


def _fetch_json(url: str, timeout: float) -> object:
    """Lecture JSON avec échéance entre deux blocs (la borne globale est dans `_get_json`)."""
    deadline = time.monotonic() + timeout
    req = urllib.request.Request(
        url,
        headers={"User-Agent": APP_USER_AGENT, "Accept": "application/vnd.github+json"},
    )
    chunks: list[bytes] = []
    size = 0
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310  # URL HTTPS constante
        read = getattr(resp, "read1", None) or resp.read
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError(f"délai de {timeout:g} s dépassé")
            chunk = read(_READ_CHUNK_BYTES)
            if not chunk:
                break
            size += len(chunk)
            if size > _MAX_RESPONSE_BYTES:
                raise UpdateCheckError("Réponse GitHub trop volumineuse.")
            chunks.append(chunk)
    return json.loads(b"".join(chunks).decode("utf-8"))


def query_latest_release(channel: str = DEFAULT_UPDATE_CHANNEL, timeout: float = UPDATE_CHECK_TIMEOUT_S) -> UpdateInfo | None:
    """
    Dernière release du canal demandé (None si aucune release exploitable).

    Lève UpdateCheckError si GitHub est injoignable : un échec ne doit pas
    être confondu avec « aucune mise à jour ».
    """
    try:
        if normalize_update_channel(channel) == UPDATE_CHANNEL_STABLE:
            return _parse_release(_get_json(LATEST_RELEASE_API_URL, timeout))
        payload = _get_json(f"{RELEASES_API_URL}?per_page={UNSTABLE_RELEASES_PAGE_SIZE}", timeout)
    except Exception as exc:
        reason = getattr(exc, "reason", None) or exc
        raise UpdateCheckError(f"{type(exc).__name__}: {reason}") from exc
    if not isinstance(payload, list):
        raise UpdateCheckError("Réponse GitHub inattendue.")
    releases = [info for info in map(_parse_release, payload) if info is not None]
    return max(releases, key=lambda info: version_key(info.version), default=None)


def fetch_latest_release(channel: str = DEFAULT_UPDATE_CHANNEL, timeout: float = UPDATE_CHECK_TIMEOUT_S) -> UpdateInfo | None:
    """Dernière release du canal demandé, ou None si GitHub est injoignable."""
    try:
        return query_latest_release(channel, timeout)
    except UpdateCheckError:
        return None


def check_for_update(channel: str = DEFAULT_UPDATE_CHANNEL, timeout: float = UPDATE_CHECK_TIMEOUT_S) -> UpdateInfo | None:
    """Renvoie la dernière release du canal si elle est plus récente que le build courant."""
    info = fetch_latest_release(channel, timeout)
    return info if info is not None and info.is_newer else None
