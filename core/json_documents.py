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

import json
from pathlib import Path, PureWindowsPath
from typing import Any


class JsonDocumentError(ValueError):
    """Document JSON illisible ou non standard."""


def _reject_constant(name: str) -> Any:
    raise JsonDocumentError(f"constante JSON non finie interdite : {name}")


def parse_json_text(text: str) -> Any:
    """JSON standard strict (sans NaN/Infinity)."""
    return json.loads(text, parse_constant=_reject_constant)


def read_json_document(path: Path) -> Any:
    """Lit un document JSON utilisateur (BOM toléré, constantes non finies refusées).

    Lève ``OSError`` (lecture), ``json.JSONDecodeError`` (syntaxe) ou
    :class:`JsonDocumentError` (constante non finie).
    """
    return parse_json_text(Path(path).read_text(encoding="utf-8-sig"))


def _is_absolute_anywhere(value: str) -> bool:
    """Chemin absolu ou enraciné (POSIX, ``~``, lecteur ou UNC Windows), quel que soit l'OS."""
    if not value:
        return True
    if Path(value).expanduser().is_absolute():
        return True
    # « /x » ou « \\x » : enraciné (racine du lecteur sous Windows), jamais
    # relatif au dossier du document.
    return bool(PureWindowsPath(value).drive) or value.startswith(("/", "\\"))


def _resolve(value: Any, base_dir: Path) -> Any:
    if not isinstance(value, str) or _is_absolute_anywhere(value):
        return value
    return str(base_dir / value)


def resolve_document_paths(job: dict[str, Any], base_dir: Path) -> dict[str, Any]:
    """Copie de ``job`` dont les chemins relatifs sont résolus depuis ``base_dir``.

    Champs concernés : ``sources`` (chaînes ou objets ``path``), ``input``,
    ``extra_attachments``, ``chapters.import`` et ``output``.
    ``output_template`` n'est pas un chemin et reste inchangé.
    """
    resolved = dict(job)
    base_dir = Path(base_dir)
    sources = resolved.get("sources")
    if isinstance(sources, str):
        resolved["sources"] = _resolve(sources, base_dir)
    elif isinstance(sources, list):
        resolved["sources"] = [
            dict(item, path=_resolve(item.get("path"), base_dir)) if isinstance(item, dict) and "path" in item
            else _resolve(item, base_dir)
            for item in sources
        ]
    raw_input = resolved.get("input")
    if isinstance(raw_input, list):
        resolved["input"] = [_resolve(item, base_dir) for item in raw_input]
    elif raw_input is not None:
        resolved["input"] = _resolve(raw_input, base_dir)
    attachments = resolved.get("extra_attachments")
    if isinstance(attachments, list):
        resolved["extra_attachments"] = [_resolve(item, base_dir) for item in attachments]
    chapters = resolved.get("chapters")
    if isinstance(chapters, dict) and "import" in chapters:
        resolved["chapters"] = dict(chapters, **{"import": _resolve(chapters["import"], base_dir)})
    if "output" in resolved:
        resolved["output"] = _resolve(resolved["output"], base_dir)
    return resolved


__all__ = ["JsonDocumentError", "parse_json_text", "read_json_document", "resolve_document_paths"]
