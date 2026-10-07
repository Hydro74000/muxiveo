"""
core/workdir.py — Helpers pour gérer le répertoire de travail applicatif.

Propriété des dossiers :
  - racine du work_dir marquée ``.muxiveo-workdir`` seulement si Muxiveo l'a
    créée ou si c'est un chemin par défaut de l'application ;
  - dossier process toujours créé neuf (``mkdtemp``) et marqué
    ``.muxiveo-process`` avec un jeton aléatoire : seul le détenteur du jeton
    peut le supprimer récursivement ;
  - pendant le job, le processus créateur détient un verrou OS sur
    ``.muxiveo-process.lock`` : un dossier verrouillé est **actif** et n'est
    jamais proposé au nettoyage, même par une autre instance. Le système
    libère le verrou à la mort du processus (dossier alors abandonné).
Un dossier existant qui n'est pas prouvé appartenir à Muxiveo n'est jamais
vidé ni supprimé.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import stat
import sys
import tempfile
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from core.file_lock import FileLock, lock_is_free
from core.file_types import _WINDOWS_RESERVED_STEMS
from core.tls import urlopen_tls
from core.version import APP_ENV_PREFIX

WORK_DIR_MARKER = ".muxiveo-workdir"
PROCESS_DIR_MARKER = ".muxiveo-process"
PROCESS_DIR_LOCK = ".muxiveo-process.lock"
TMDB_COVERS_DIRNAME = "tmdb_covers"
_MARKER_NAMES = frozenset({WORK_DIR_MARKER, PROCESS_DIR_MARKER, PROCESS_DIR_LOCK})
# Dossier sans marqueur plus récent que ce délai : création de job en cours
# (fenêtre entre mkdtemp et l'écriture du marqueur), jamais nettoyé.
_UNMARKED_GRACE_S = 120.0
# Verrous des dossiers process créés par ce processus, par jeton : détenus
# jusqu'à la suppression du dossier par son propriétaire.
_HELD_LOCKS_GUARD = threading.Lock()
_HELD_LOCKS: dict[str, FileLock] = {}
_TMDB_INSECURE_SSL_ENV = f"{APP_ENV_PREFIX}_TMDB_INSECURE_SSL"
# Nom de dossier process tronqué : le suffixe aléatoire de mkdtemp doit tenir
# dans la limite de 255 octets par composant de chemin.
_PROCESS_NAME_MAX = 80


def _urlopen_image(req: urllib.request.Request, timeout: int = 30):
    """urlopen TLS vérifié pour les images TMDB (même opt-in explicite que l'API)."""
    return urlopen_tls(req, timeout, insecure_env=_TMDB_INSECURE_SSL_ENV)


_PROCESS_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize_process_folder_name(name: str, fallback: str = "job") -> str:
    """Normalise un nom de dossier process à partir d'un texte libre.

    Noms valides sur Windows, macOS et Linux : caractères ``[A-Za-z0-9._-]``,
    jamais de point ou d'espace final, jamais de nom de périphérique Windows
    (``CON``, ``NUL``, ``COM1``… même suivi d'une extension) — appliqué sur
    tous les OS pour que le comportement soit identique partout.
    """
    raw = (name or "").strip()
    normalized = _PROCESS_NAME_RE.sub("_", raw).strip("._-") or fallback
    if normalized.split(".", 1)[0].lower() in _WINDOWS_RESERVED_STEMS:
        normalized = f"job_{normalized}"
    return normalized


def process_folder_name_from_output(output_path: Path, fallback: str = "job") -> str:
    """
    Retourne un nom de dossier process dérivé du fichier de sortie.

    Ex: /videos/Mon.Film.mkv -> "Mon.Film"
    """
    stem = output_path.stem if output_path else ""
    return sanitize_process_folder_name(stem, fallback=fallback)


def ensure_work_dir(path: Path, *, adopt: bool = False) -> Path:
    """Crée le work_dir si besoin.

    La racine n'est marquée comme appartenant à Muxiveo que si elle vient
    d'être créée ici ou si ``adopt`` (chemin par défaut de l'application) :
    un dossier choisi par l'utilisateur n'est jamais revendiqué.
    """
    created = not path.exists()
    path.mkdir(parents=True, exist_ok=True)
    marker = path / WORK_DIR_MARKER
    if (created or adopt) and not marker.exists():
        try:
            marker.write_text(
                "Dossier de travail Muxiveo : contenu temporaire supprimable par l'application.\n",
                encoding="utf-8",
            )
        except OSError:
            pass
    return path


def is_owned_work_root(path: Path) -> bool:
    """Vrai si la racine porte le marqueur Muxiveo (fichier régulier, pas un lien)."""
    marker = path / WORK_DIR_MARKER
    try:
        return marker.is_file() and not marker.is_symlink()
    except OSError:
        return False


def _is_link_or_junction(path: Path) -> bool:
    """Lien symbolique, ou jonction NTFS (Windows ; ``Path.is_junction`` dès Python 3.12)."""
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction):
        return bool(is_junction())
    return bool(getattr(os.path, "isjunction", lambda _p: False)(path))


def is_owned_process_dir(path: Path, *, root: Path, token: str | None = None) -> bool:
    """Vrai si ``path`` est un dossier process Muxiveo, enfant direct de ``root``.

    Avec ``token``, exige en plus que le marqueur contienne ce jeton : preuve
    que le dossier est celui créé par l'exécution détentrice du jeton.
    Les chemins sont comparés résolus : racine derrière un lien (``/home`` →
    ``/var/home`` des Linux immuables, ``/var`` → ``/private/var`` sous macOS,
    jonction ou lecteur réseau sous Windows) reconnue, dossier redirigé ailleurs refusé.
    """
    try:
        if _is_link_or_junction(path) or not path.is_dir():
            return False
        if path.resolve().parent != root.resolve():
            return False
        marker = path / PROCESS_DIR_MARKER
        if marker.is_symlink() or not marker.is_file():
            return False
        return token is None or marker.read_text(encoding="ascii").strip() == token
    except (OSError, UnicodeDecodeError):
        return False


@dataclass(frozen=True)
class ProcessWorkDir:
    """Dossier process créé par une exécution ; ``token`` prouve la propriété."""

    path: Path
    root: Path
    token: str

    def is_owned(self) -> bool:
        return is_owned_process_dir(self.path, root=self.root, token=self.token)

    def remove(self) -> bool:
        """Supprime récursivement le dossier s'il est toujours celui de cette exécution.

        Retourne False (sans rien supprimer) si la propriété n'est plus prouvée
        (marqueur absent ou modifié, lien symbolique, dossier déplacé) ou si
        le dossier est actif (verrou détenu par un autre processus).
        Le verrou est pris pendant la suppression : un job ne peut pas devenir
        actif entre la vérification et l'effacement.
        Le marqueur est supprimé en dernier : après un échec partiel (fichier
        verrouillé sous Windows, antivirus), le dossier reste identifiable et
        le nettoyage de démarrage peut le reprendre. Lève OSError dans ce cas.
        """
        if not self.is_owned():
            return False
        lock = _take_held_lock(self.token)
        lock_path = self.path / PROCESS_DIR_LOCK
        if lock is None and lock_path.exists():
            probe = FileLock(lock_path)
            if not probe.try_acquire():
                return False
            lock = probe
        try:
            for entry in list(self.path.iterdir()):
                if entry.name not in (PROCESS_DIR_MARKER, PROCESS_DIR_LOCK):
                    _remove_entry(entry)
        finally:
            if lock is not None:
                lock.release(unlink=True)
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass
        marker = self.path / PROCESS_DIR_MARKER
        marker.unlink()
        try:
            self.path.rmdir()
        except OSError:
            # Un verrou sur le dossier ou un fichier apparu entre-temps peut
            # empêcher rmdir : restaurer le jeton pour le prochain nettoyage.
            try:
                with marker.open("x", encoding="ascii") as fh:
                    fh.write(f"{self.token}\n")
            except OSError:
                pass
            raise
        return True


def _register_held_lock(token: str, lock: FileLock) -> None:
    with _HELD_LOCKS_GUARD:
        _HELD_LOCKS[token] = lock


def _take_held_lock(token: str) -> FileLock | None:
    with _HELD_LOCKS_GUARD:
        return _HELD_LOCKS.pop(token, None)


def is_active_process_dir(path: Path) -> bool:
    """Vrai si un processus vivant (celui-ci compris) détient le verrou du dossier."""
    lock_path = path / PROCESS_DIR_LOCK
    try:
        if lock_path.is_symlink() or not lock_path.is_file():
            return False
    except OSError:
        return False
    return not lock_is_free(lock_path)


# Suffixe aléatoire de ``tempfile.mkdtemp`` (8 caractères) : forme des dossiers process.
_MKDTEMP_NAME_RE = re.compile(r".+\.[a-z0-9_]{8}")


def _is_recent_unmarked_dir(path: Path) -> bool:
    """Dossier process sans marqueur créé récemment : job en cours de création."""
    try:
        if (
            not _MKDTEMP_NAME_RE.fullmatch(path.name)
            or _is_link_or_junction(path)
            or not path.is_dir()
            or (path / PROCESS_DIR_MARKER).exists()
        ):
            return False
        return time.time() - path.stat().st_mtime < _UNMARKED_GRACE_S
    except OSError:
        return False


def active_process_dirs(path: Path) -> list[Path]:
    """Dossiers de jobs en cours dans le work_dir (jamais nettoyés)."""
    if not path.is_dir():
        return []
    return [entry for entry in work_dir_entries(path) if entry.is_dir() and is_active_process_dir(entry)]


def capture_process_work_dir(path: Path, *, root: Path) -> ProcessWorkDir | None:
    """Capture le jeton d'un dossier process pour vérifier son identité au nettoyage."""
    if not is_owned_process_dir(path, root=root):
        return None
    try:
        token = (path / PROCESS_DIR_MARKER).read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return ProcessWorkDir(path, root, token) if token else None


def _clear_readonly_and_retry(func, path, _exc) -> None:
    """Fichier en lecture seule (attribut Windows) : retire l'attribut puis réessaie."""
    exc = _exc[1] if isinstance(_exc, tuple) else _exc
    # rmtree peut rappeler le handler pour scandir/open après l'échec d'une
    # première tentative. Ces opérations n'ont pas la signature unlink(path)
    # et leur erreur ne doit pas être masquée par un simple changement de mode.
    if not isinstance(exc, PermissionError) or func not in (os.unlink, os.remove, os.rmdir):
        raise exc
    os.chmod(path, stat.S_IMODE(os.stat(path).st_mode) | stat.S_IWRITE | stat.S_IREAD)
    func(path)


def _remove_entry(entry: Path) -> None:
    """Supprime un fichier, un lien (sans le suivre) ou un dossier ; lève OSError en cas d'échec."""
    if _is_link_or_junction(entry):
        try:
            entry.unlink()
        except OSError:
            os.rmdir(entry)  # lien de répertoire / jonction Windows : rmdir retire le lien seul
        return
    if entry.is_dir():
        if sys.version_info >= (3, 12):
            shutil.rmtree(entry, onexc=_clear_readonly_and_retry)
        else:
            shutil.rmtree(entry, onerror=_clear_readonly_and_retry)
        return
    try:
        entry.unlink()
    except PermissionError:
        os.chmod(entry, stat.S_IWRITE | stat.S_IREAD)
        entry.unlink()


def create_process_work_dir(
    work_root: Path,
    *,
    output_path: Path | None = None,
    process_name: str | None = None,
    fallback_name: str = "job",
) -> ProcessWorkDir:
    """Crée un dossier process neuf, propriété exclusive de l'exécution courante.

    Création atomique (``mkdtemp`` : échoue plutôt que de réutiliser un dossier
    existant, droits 0700) ; jamais de vidage d'un dossier préexistant.
    """
    root = ensure_work_dir(work_root)
    if process_name is not None:
        folder_name = sanitize_process_folder_name(process_name, fallback=fallback_name)
    elif output_path is not None:
        folder_name = process_folder_name_from_output(output_path, fallback=fallback_name)
    else:
        folder_name = fallback_name
    folder_name = folder_name[:_PROCESS_NAME_MAX].rstrip("._-") or fallback_name
    path = Path(tempfile.mkdtemp(prefix=f"{folder_name}.", dir=root))
    token = secrets.token_hex(16)
    # Verrou pris avant le marqueur : un dossier marqué de cette version est
    # toujours verrouillé tant que son job vit.
    lock = FileLock(path / PROCESS_DIR_LOCK)
    try:
        if not lock.try_acquire():
            raise OSError(f"Verrou du dossier process indisponible : {path}")
        (path / PROCESS_DIR_MARKER).write_text(f"{token}\n", encoding="ascii")
    except BaseException:
        lock.release(unlink=True)
        shutil.rmtree(path, ignore_errors=True)
        raise
    _register_held_lock(token, lock)
    return ProcessWorkDir(path=path, root=root, token=token)


def prepare_process_work_dir(
    work_root: Path,
    *,
    output_path: Path | None = None,
    process_name: str | None = None,
    fallback_name: str = "job",
) -> Path:
    """Compat : chemin d'un dossier process neuf (voir :func:`create_process_work_dir`)."""
    return create_process_work_dir(
        work_root,
        output_path=output_path,
        process_name=process_name,
        fallback_name=fallback_name,
    ).path


def work_dir_entries(path: Path) -> list[Path]:
    """Retourne les entrées de premier niveau dans le work_dir (marqueurs exclus)."""
    if not path.exists() or not path.is_dir():
        return []
    return sorted(
        [p for p in path.iterdir() if not _is_ignorable_work_dir_entry(p)],
        key=lambda p: p.name.lower(),
    )


def work_dir_has_entries(path: Path) -> bool:
    if not path.exists() or not path.is_dir():
        return False
    return any(not _is_ignorable_work_dir_entry(p) for p in path.iterdir())


def cleanable_work_dir_entries(path: Path) -> list[Path]:
    """Entrées que le nettoyage peut supprimer : exactement celles montrées à l'utilisateur.

    Jamais un job actif (verrou détenu par un processus vivant) ni un dossier
    en cours de création. Racine marquée Muxiveo : le reste de son contenu ;
    sinon seulement les dossiers process marqués et abandonnés (un
    ``tmdb_covers`` hors racine marquée n'est pas adopté sur son seul nom).
    Les dossiers marqués par une version antérieure, sans verrou, sont
    considérés abandonnés.
    """
    if not path.is_dir():
        return []
    owned_root = is_owned_work_root(path)
    entries: list[Path] = []
    for entry in work_dir_entries(path):
        if is_active_process_dir(entry):
            continue
        if is_owned_process_dir(entry, root=path):
            entries.append(entry)
        elif owned_root and not _is_recent_unmarked_dir(entry):
            entries.append(entry)
    return entries


def clear_work_dir(path: Path) -> list[Path]:
    """Supprime les entrées nettoyables du work_dir (jamais la racine) ; retourne la liste traitée."""
    entries = cleanable_work_dir_entries(path)
    for entry in entries:
        remove_path(entry)
    return entries


def remove_path(path: Path) -> None:
    """Supprime un fichier ou dossier, en tolérant les cas limites de FS."""
    if is_owned_process_dir(path, root=path.parent):
        owned = capture_process_work_dir(path, root=path.parent)
        if owned is not None:
            try:
                owned.remove()
            except OSError:
                pass
        # Aucun repli rmtree : il effacerait le marqueur malgré un fichier
        # verrouillé, rendant les restes invisibles au nettoyage de démarrage.
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        # Peut être un lien cassé/objet non standard : fallback rmtree.
        shutil.rmtree(path, ignore_errors=True)


def _is_ignorable_work_dir_entry(entry: Path) -> bool:
    """
    Retourne True pour une entrée à ignorer dans le check de démarrage.

    Règle métier :
      - marqueurs de propriété Muxiveo ;
      - `tmdb_covers` est ignoré s'il ne contient aucun fichier utile.
    """
    if entry.name in _MARKER_NAMES:
        return True
    if entry.name != TMDB_COVERS_DIRNAME or not entry.is_dir():
        return False
    return not _dir_has_payload_files(entry)


def _dir_has_payload_files(directory: Path) -> bool:
    """True si le dossier contient au moins un fichier/symlink (récursif)."""
    try:
        for child in directory.rglob("*"):
            if child.is_file() or child.is_symlink():
                return True
    except OSError:
        # En cas de problème de lecture, on préfère considérer le dossier non vide.
        return True
    return False


_MOUNTINFO_ESCAPE_RE = re.compile(r"\\([0-7]{3})")


def filesystem_type(path: Path) -> str | None:
    """Type du système de fichiers contenant ``path`` (Linux, ``/proc/self/mountinfo``).

    None hors Linux ou si indéterminable. Sert à signaler un dossier de travail
    en RAM (``tmpfs``).
    """
    try:
        lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8", errors="replace").splitlines()
        target = path.resolve()
    except OSError:
        return None
    best: tuple[int, str | None] = (-1, None)
    for line in lines:
        left, sep, right = line.partition(" - ")
        fields = left.split()
        if not sep or len(fields) < 5 or not right.split():
            continue
        mount_point = Path(_MOUNTINFO_ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 8)), fields[4]))
        if target != mount_point and mount_point not in target.parents:
            continue
        depth = len(mount_point.parts)
        if depth >= best[0]:
            best = (depth, right.split()[0])
    return best[1]


def normalized_tmdb_cover_filename(filename: str) -> str:
    """Retourne le nom de fichier TMDB effectivement écrit sur disque."""
    raw = (filename or "").strip()
    name = Path(raw).name if raw else ""
    return name if name not in {"", ".", ".."} else "cover.jpg"


def download_tmdb_cover(url: str, filename: str, target_dir: Path) -> Path:
    """
    Télécharge une cover depuis l'URL TMDB et la place dans target_dir.

    Retourne le chemin du fichier téléchargé.
    Lève une OSError ou urllib.error.URLError en cas d'échec réseau/disque.
    """
    from core.version import APP_USER_AGENT

    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / normalized_tmdb_cover_filename(filename)
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "image/*,*/*;q=0.8",
            "User-Agent": APP_USER_AGENT,
        },
    )
    with _urlopen_image(req, timeout=30) as resp:
        dest.write_bytes(resp.read())
    return dest


def relocate_tmdb_covers_to_process_dir(
    attachments: list[Path],
    *,
    work_root: Path,
    process_dir: Path,
) -> list[Path]:
    """
    Déplace les covers TMDB (situées sous `<work_root>/tmdb_covers`) dans `process_dir`.

    Les chemins retournés sont ceux à utiliser par le workflow.
    Les fichiers déplacés sont supprimés de `tmdb_covers` (move/rename).
    """
    tmdb_root = work_root / TMDB_COVERS_DIRNAME
    destination_root = process_dir / "attachments"
    destination_root.mkdir(parents=True, exist_ok=True)

    relocated: list[Path] = []
    for path in attachments:
        src = Path(path)
        if not _is_path_under(src, tmdb_root):
            relocated.append(src)
            continue
        if not src.exists():
            relocated.append(src)
            continue
        dest = _unique_destination(destination_root, src.name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        src.replace(dest)
        _prune_empty_parents(src.parent, stop_at=tmdb_root)
        relocated.append(dest)
    return relocated


def _is_path_under(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except Exception:
        return False


def _unique_destination(directory: Path, filename: str) -> Path:
    base = directory / filename
    if not base.exists():
        return base
    stem = base.stem
    suffix = base.suffix
    idx = 1
    while True:
        candidate = directory / f"{stem}_{idx}{suffix}"
        if not candidate.exists():
            return candidate
        idx += 1


def _prune_empty_parents(start: Path, *, stop_at: Path) -> None:
    current = start
    stop = stop_at.resolve()
    while True:
        try:
            cur_resolved = current.resolve()
        except OSError:
            break
        if cur_resolved == stop:
            break
        try:
            current.rmdir()
        except OSError:
            break
        parent = current.parent
        if parent == current:
            break
        current = parent
