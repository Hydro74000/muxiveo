"""
core/json_documents.py — Lecture des documents JSON utilisateur et de leurs chemins.

Règles communes GUI/CLI :
  - UTF-8 avec ou sans BOM (fichiers écrits par des outils Windows) ;
  - constantes non finies (``NaN``, ``Infinity``) refusées : elles ne sont pas
    du JSON standard et deviendraient des timestamps ou débits invalides ;
  - chemins relatifs d'un document (sources, pièces jointes, import de
    chapitres, sortie) résolus depuis le dossier du document, pas depuis le
    répertoire courant. Les chemins fournis en ligne de commande restent
    relatifs au répertoire courant.
"""

from __future__ import annotations

import contextvars
import json
import math
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path, PureWindowsPath
from typing import Any, Literal

PathKind = Literal["absolute", "relative", "unusable"]

#: Caractères au plus produits par la résolution des chemins relatifs d'une requête de l'API contre son dossier de
#: référence (chaque chemin recopie le dossier, environ quatre octets par caractère une fois découpé) : une requête
#: de 16 Mio n'en fait jamais des centaines de Go.
MAX_RESOLVED_PATH_CHARS = 16 * 1024 * 1024


class ResolutionBudget:
    """Volume des chemins relatifs résolus contre un dossier de référence, borné par
    :data:`MAX_RESOLVED_PATH_CHARS` ; ``spend`` renvoie faux dès le dépassement, et ensuite toujours (rien n'est
    plus résolu). Un seul budget par lecture de requête (:func:`resolution_budget`), hooks des documents compris."""

    __slots__ = ("used", "exceeded")

    def __init__(self) -> None:
        self.used = 0
        self.exceeded = False

    def spend(self, base_dir: Path, value: str) -> bool:
        if not self.exceeded:
            self.used += len(str(base_dir)) + len(value) + 1
            self.exceeded = self.used > MAX_RESOLVED_PATH_CHARS
        return not self.exceeded

    MESSAGE = (f"chemins relatifs trop nombreux pour ce dossier de référence (plus de {MAX_RESOLVED_PATH_CHARS} "
               "caractères à résoudre) : indiquer des chemins absolus")


_BUDGET: contextvars.ContextVar[ResolutionBudget | None] = contextvars.ContextVar("resolution_budget", default=None)


@contextmanager
def resolution_budget() -> Iterator[ResolutionBudget]:
    """Budget partagé par tous les chemins résolus pendant la lecture d'une requête (champs et documents)."""
    budget = ResolutionBudget()
    token = _BUDGET.set(budget)
    try:
        yield budget
    finally:
        _BUDGET.reset(token)


def current_budget() -> ResolutionBudget:
    """Budget de la lecture en cours, sinon un budget propre à l'appel."""
    return _BUDGET.get() or ResolutionBudget()


def home_unknown(value: str) -> bool:
    """``~utilisateur`` dont le dossier personnel est introuvable (ou nom invalide) : chemin jamais résolu."""
    if "\0" in value:
        return False  # caractère nul : refusé à part, jamais développé
    try:
        Path(value).expanduser()
    except (RuntimeError, ValueError, OSError):
        return True
    return False


class JsonDocumentError(ValueError):
    """Document JSON illisible ou non standard."""


def _reject_constant(name: str) -> Any:
    raise JsonDocumentError(f"constante JSON non finie interdite : {name}")


def parse_json_text(text: str) -> Any:
    """JSON standard strict (sans NaN/Infinity)."""
    def finite_float(value: str) -> float:
        number = float(value)
        if not math.isfinite(number):
            raise JsonDocumentError(f"nombre JSON non fini interdit : {value}")
        return number

    try:
        return json.loads(text, parse_constant=_reject_constant, parse_float=finite_float)
    except RecursionError as exc:
        raise JsonDocumentError("document JSON trop imbriqué") from exc


def read_json_document(path: Path) -> Any:
    """Lit un document JSON utilisateur (BOM toléré, constantes non finies refusées).

    Lève ``OSError`` (lecture), ``json.JSONDecodeError`` (syntaxe) ou
    :class:`JsonDocumentError` (constante non finie).
    """
    return parse_json_text(Path(path).read_text(encoding="utf-8-sig"))


def path_kind(value: str) -> PathKind:
    """Nature d'un chemin pour l'OS qui s'exécute.

    ``absolute`` : absolu ici (``~`` développé) ; ``relative`` : à résoudre depuis un dossier de référence ;
    ``unusable`` : absolu pour l'autre OS (``C:/film.mkv`` ou UNC sous POSIX) ou ambigu sous Windows
    (``C:film.mkv`` relatif au lecteur, ``\\film.mkv`` enraciné sans lecteur) — jamais rattaché à un dossier.
    """
    # Valeur classée telle qu'elle sera utilisée (aucun blanc retiré : « /m » précédé d'un espace est relatif).
    if "\0" in value or home_unknown(value):  # caractère nul, « ~inconnu/a » : jamais rattaché à un dossier
        return "unusable"
    if Path(value).expanduser().is_absolute():
        return "absolute"
    windows = PureWindowsPath(value)
    if os.name == "nt":
        return "unusable" if windows.drive or windows.root else "relative"
    return "unusable" if windows.drive or value.startswith("\\") else "relative"


def _resolve(value: Any, base_dir: Path) -> Any:
    if not isinstance(value, str) or not value or path_kind(value) != "relative":
        return value
    resolved = str(base_dir / value)
    # « dossier/ » : le séparateur final désigne un dossier de sortie (gabarit rendu dedans), il est conservé.
    return resolved + os.sep if value.endswith(("/", "\\")) else resolved


#: Emplacement d'un chemin dans un document de job (chemin pointé, valeur).
PathSlot = tuple[str, str]


def _map_document_paths(job: dict[str, Any], visit: Callable[[str, str], str], *,
                        preset_base: Path | None) -> dict[str, Any]:
    """Copie de ``job`` où chaque chemin (texte) passe par ``visit(emplacement, valeur)``.

    Inventaire unique des chemins d'un document (résolution et contrôle d'admission) : ``sources`` (chaînes ou
    objets ``path``, ``sync.calibration``), ``input``, ``extra_attachments``, ``chapters.import``, ``output``,
    ``calibration``, ``work_dir``, ``tracks[].sync.calibration`` et ``encode.preset`` quand il désigne un fichier
    (``preset_base`` : dossier où le chercher). ``output_template`` n'est pas un chemin.
    """
    def one(slot: str, value: Any) -> Any:
        return visit(slot, value) if isinstance(value, str) else value

    def sync_of(item: dict[str, Any], slot: str) -> dict[str, Any]:
        sync = item.get("sync")
        if isinstance(sync, dict) and isinstance(sync.get("calibration"), str):
            return dict(item, sync=dict(sync, calibration=one(f"{slot}.sync.calibration", sync["calibration"])))
        return item

    resolved = dict(job)
    sources = resolved.get("sources")
    if isinstance(sources, str):
        resolved["sources"] = one("sources", sources)
    elif isinstance(sources, list):
        resolved["sources"] = [
            sync_of(dict(item, path=one(f"sources[{i}].path", item.get("path"))), f"sources[{i}]")
            if isinstance(item, dict) and "path" in item else one(f"sources[{i}]", item)
            for i, item in enumerate(sources)
        ]
    raw_input = resolved.get("input")
    if isinstance(raw_input, list):
        resolved["input"] = [one(f"input[{i}]", item) for i, item in enumerate(raw_input)]
    elif raw_input is not None:
        resolved["input"] = one("input", raw_input)
    attachments = resolved.get("extra_attachments")
    if isinstance(attachments, list):
        resolved["extra_attachments"] = [one(f"extra_attachments[{i}]", item) for i, item in enumerate(attachments)]
    chapters = resolved.get("chapters")
    if isinstance(chapters, dict) and "import" in chapters:
        resolved["chapters"] = dict(chapters, **{"import": one("chapters.import", chapters["import"])})
    for key in ("output", "calibration", "work_dir"):
        if key in resolved:
            resolved[key] = one(key, resolved[key])
    tracks = resolved.get("tracks")
    if isinstance(tracks, list):
        resolved["tracks"] = [sync_of(item, f"tracks[{i}]") if isinstance(item, dict) else item
                              for i, item in enumerate(tracks)]
    encode = resolved.get("encode")
    if isinstance(encode, dict):
        resolved["encode"] = _map_encode_paths(encode, visit, preset_base=preset_base, prefix="encode.")
    return resolved


def _map_encode_paths(encode: dict[str, Any], visit: Callable[[str, str], str], *, preset_base: Path | None,
                      prefix: str = "") -> dict[str, Any]:
    preset = encode.get("preset")
    if isinstance(preset, str) and preset and preset_is_file(preset, preset_base):
        return dict(encode, preset=visit(f"{prefix}preset", preset))
    return encode


def resolve_document_paths(job: dict[str, Any], base_dir: Path) -> dict[str, Any]:
    """Copie de ``job`` dont les chemins relatifs sont résolus depuis ``base_dir`` (voir ``_map_document_paths``)."""
    base_dir = Path(base_dir)
    return _map_document_paths(job, lambda _slot, value: _resolve(value, base_dir), preset_base=base_dir)


def document_path_errors(job: Any, base_dir: Path | None, *, prefix: str = "job",
                         budget: ResolutionBudget | None = None) -> list[tuple[str, str]]:
    """Chemins d'un document refusés pour une requête de l'API : inutilisables sur cet OS, relatifs sans
    dossier de référence (jamais résolus contre le dossier courant du processus), ou trop nombreux à résoudre."""
    errors: list[tuple[str, str]] = []
    budget = budget or current_budget()

    def check(slot: str, value: str) -> str:
        message = _path_problem(value, base_dir, budget)
        if message:
            errors.append((f"{prefix}.{slot}" if prefix else slot, message))
        return value

    if isinstance(job, dict):
        _map_document_paths(job, check, preset_base=base_dir)
    return errors


def encode_path_errors(encode: Any, base_dir: Path | None, *, prefix: str = "encode") -> list[tuple[str, str]]:
    """Comme :func:`document_path_errors` pour un bloc ``encode`` seul (preset désigné par un fichier)."""
    errors: list[tuple[str, str]] = []
    budget = current_budget()

    def check(slot: str, value: str) -> str:
        message = _path_problem(value, base_dir, budget)
        if message:
            errors.append((f"{prefix}.{slot}", message))
        return value

    if isinstance(encode, dict):
        _map_encode_paths(encode, check, preset_base=base_dir)
    return errors


def reference_path_errors(slot: str, value: Any, base_dir: Path | None, *,
                          name_allowed: bool) -> list[tuple[str, str]]:
    """Profil ou sortie désigné par un texte d'une requête de l'API : chemin inutilisable, ou relatif sans dossier
    de référence. ``name_allowed`` : un texte relatif sans suffixe ``.json`` est le nom d'un élément enregistré."""
    if not isinstance(value, str) or not value:
        return []
    if not value.strip():
        return [(slot, "chemin attendu")]  # blancs seuls : jamais un nom ni un chemin
    # Classé tel qu'il sera utilisé : profil sans ses blancs (comme son chargeur), sortie telle quelle.
    text = value.strip() if name_allowed else value
    if name_allowed and path_kind(text) == "relative" and not text.lower().endswith(".json"):
        return []
    message = _path_problem(text, base_dir, current_budget())  # résolu ensuite : compté dans le budget
    return [(slot, message)] if message else []


def without_internal_keys(document: Any) -> Any:
    """Document sans ses clés internes ``_…`` de premier niveau (ignorées d'une requête, jamais honorées)."""
    if not isinstance(document, dict):
        return document
    return {key: value for key, value in document.items() if not str(key).startswith("_")}


def _path_problem(value: str, base_dir: Path | None, budget: ResolutionBudget | None = None) -> str:
    if not value.strip():
        return "chemin attendu"
    if "\0" in value:
        return "caractère nul interdit dans un chemin"
    kind = path_kind(value)
    if home_unknown(value):
        return "dossier personnel inconnu (~utilisateur)"
    if kind == "unusable":
        return "chemin inutilisable sur ce système (absolu pour un autre OS ou ambigu)"
    if kind == "relative" and base_dir is None:
        return "chemin absolu attendu (aucun dossier de référence)"
    if kind == "relative" and base_dir is not None and budget is not None and not budget.spend(base_dir, value):
        return ResolutionBudget.MESSAGE
    return ""


def resolve_encode_paths(encode: dict[str, Any], base_dir: Path) -> dict[str, Any]:
    """Bloc ``encode`` dont le preset désigné par un fichier relatif est résolu depuis ``base_dir``."""
    base_dir = Path(base_dir)
    return _map_encode_paths(encode, lambda _slot, value: _resolve(value, base_dir), preset_base=base_dir)


def portable_encode_block(encode: Any, base_dir: Path | None) -> Any:
    """Bloc ``encode`` à enregistrer dans un job exporté : preset fichier en chemin absolu.

    Relu depuis le dossier de l'export, un chemin relatif (au dossier de référence de la commande)
    désignerait un autre fichier. Sans dossier de référence, seul un nom enregistré reste relatif.
    """
    if not isinstance(encode, dict):
        return encode
    resolved = resolve_encode_paths(encode, base_dir) if base_dir is not None else encode
    preset = resolved.get("preset")
    if isinstance(preset, str) and preset != encode.get("preset"):
        return dict(resolved, preset=str(Path(preset).resolve()))
    if isinstance(preset, str) and preset.startswith("~"):
        return dict(resolved, preset=str(Path(preset).expanduser()))
    return resolved


def resolve_profile_reference(value: str, base_dir: Path) -> str:
    """Profil décisionnel d'une requête : fichier relatif (suffixe ``.json``, fichier présent, ou présent avec
    ``.json``) lu depuis ``base_dir`` (dossier de la requête) ; sinon nom d'un profil enregistré, inchangé."""
    text = str(value or "").strip()
    path = Path(text).expanduser()
    if not text or path_kind(text) != "relative":  # absolu, ~, C:\…, \…, UNC : inchangés
        return value
    candidate = Path(base_dir) / path
    if (text.lower().endswith(".json") or candidate.is_file()
            or (not path.suffix and candidate.with_suffix(".json").is_file())):
        return str(candidate)
    return value


def resolve_output_reference(value: str | None, base_dir: Path) -> str | None:
    """Sortie d'une requête (fichier, ou dossier avec ``/`` final) : relative → depuis ``base_dir``, ``/`` gardé."""
    # Classée et résolue telle qu'elle sera utilisée (aucun blanc retiré).
    if not value or not value.strip() or path_kind(value) != "relative":  # absolu, autre OS, vide : inchangé
        return value
    resolved = str(Path(base_dir) / Path(value).expanduser())
    return resolved + "/" if value.endswith(("/", "\\")) else resolved


def preset_is_file(value: str, base_dir: Path | None = None) -> bool:
    """Preset désigné par un fichier (suffixe ``.json`` ou fichier présent) plutôt que par son nom.

    Règle unique du chargeur (``core.jobs.encode.preset_loader``) et des documents : un nom
    enregistré peut contenir ``/`` (``Film/4K``) sans devenir un chemin.
    """
    if value.lower().endswith(".json") or path_kind(value) == "unusable":
        return True  # chemin (inutilisable ici : refusé tel quel, jamais sondé relativement à un lecteur)
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.is_file()
    # Sans dossier de référence, un nom relatif reste un nom (jamais cherché dans le dossier courant).
    return base_dir is not None and (Path(base_dir) / path).is_file()


__all__ = ["JsonDocumentError", "PathKind", "document_path_errors", "encode_path_errors", "parse_json_text",
           "path_kind", "portable_encode_block", "preset_is_file", "read_json_document", "reference_path_errors",
           "resolve_document_paths", "resolve_encode_paths", "without_internal_keys"]
