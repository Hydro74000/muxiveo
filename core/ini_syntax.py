"""
core/ini_syntax.py — Lecture ligne à ligne de config.ini selon les règles du chargeur.

Le chargeur est ``configparser`` (``inline_comment_prefixes=("#",)``, délimiteurs « = » et « : »,
lignes indentées = continuation de la valeur précédente). Toute réécriture textuelle de config.ini
(application, CLI, setup) passe par ces règles : une clé ou une section reconnue ici est exactement
celle que le chargeur lit. Module sans Qt (utilisé par setup.py avant l'installation des dépendances).
"""

from __future__ import annotations

import configparser
import re

_INLINE_COMMENT_RE = re.compile(r"\s#")
# Identique à configparser.RawConfigParser.SECTCRE / OPTCRE (délimiteurs par défaut).
_SECTION_RE = re.compile(r"\[(?P<header>.+)\]")
_OPTION_RE = re.compile(r"(?P<option>.*?)\s*[=:]")


def line_content(line: str) -> str:
    """Ligne sans commentaire, comme le lit le chargeur (commentaire en ligne « # » après un blanc)."""
    stripped = line.strip()
    if not stripped or stripped.startswith(("#", ";")):
        return ""
    match = _INLINE_COMMENT_RE.search(stripped)
    return (stripped[:match.start()] if match else stripped).strip()


def is_indented(line: str) -> bool:
    """Ligne indentée non vide et hors commentaire : continuation de la valeur précédente."""
    return bool(line[:1].isspace() and line_content(line))


def section_name(line: str) -> str | None:
    """Nom exact de la section ouverte par ``line`` (casse et espaces conservés, comme le chargeur)."""
    if is_indented(line):
        return None
    match = _SECTION_RE.match(line_content(line))
    return match.group("header") if match else None


def option_name(line: str) -> str | None:
    """Clé (minuscules, comme le chargeur) définie par ``line``, sinon None."""
    if is_indented(line):
        return None
    content = line_content(line)
    if not content or _SECTION_RE.match(content):
        return None
    match = _OPTION_RE.match(content)
    option = match.group("option").strip().lower() if match else ""
    return option or None


def option_value(line: str) -> str:
    """Valeur (première ligne) d'une ligne d'option."""
    return _OPTION_RE.sub("", line_content(line), count=1).strip()


def section_bounds(lines: list[str], section: str) -> tuple[int, int]:
    """Index de l'en-tête de ``section`` (-1 si absente) et fin de la section (en-tête suivant)."""
    start = next((index for index, line in enumerate(lines) if section_name(line) == section), -1)
    if start == -1:
        return -1, len(lines)
    end = next((index for index in range(start + 1, len(lines)) if section_name(lines[index]) is not None),
               len(lines))
    return start, end


def option_end(lines: list[str], index: int, end: int) -> int:
    """Fin (exclue) de l'option commencée à ``index`` : ses lignes de continuation comprises,
    lignes vides intercalées comprises seulement si une continuation les suit."""
    last = index
    for cursor in range(index + 1, end):
        line = lines[cursor]
        if is_indented(line):
            last = cursor
        elif line_content(line):
            break           # lignes vides et commentaires n'interrompent pas la valeur pour le chargeur
    return last + 1


def remove_options(lines: list[str], section: str, keys: set[str]) -> list[str]:
    """Retire les options ``keys`` de ``section``, continuations comprises."""
    start, end = section_bounds(lines, section)
    if start == -1:
        return lines
    wanted = {key.lower() for key in keys}
    kept = lines[:start + 1]
    index = start + 1
    while index < end:
        if option_name(lines[index]) in wanted:
            index = option_end(lines, index, end)
            continue
        kept.append(lines[index])
        index += 1
    return kept + lines[end:]


def is_loadable(text: str) -> bool:
    """Vrai si le chargeur lit ``text`` (même configuration, lecture stricte)."""
    parser = configparser.ConfigParser(inline_comment_prefixes=("#",), default_section="DEFAULT")
    try:
        parser.read_string(text)
    except configparser.Error:
        return False
    return True
