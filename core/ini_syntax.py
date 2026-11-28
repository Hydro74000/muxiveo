"""
core/ini_syntax.py — Lecture ligne à ligne de config.ini selon les règles du chargeur.

Le chargeur est ``configparser`` (``inline_comment_prefixes=("#",)``, délimiteurs « = » et « : »,
interpolation « % »). Une ligne plus indentée que l'option qui la précède en continue la valeur ;
lignes vides et commentaires ne l'interrompent pas. Toute réécriture textuelle de config.ini
(application, CLI, setup) passe par ce module : une clé ou une section repérée ici est exactement
celle que le chargeur lit. Module sans Qt (utilisé par setup.py avant l'installation des dépendances).
"""

from __future__ import annotations

import configparser
import re
from dataclasses import dataclass

_INLINE_COMMENT_RE = re.compile(r"\s#")
# Fins de ligne du chargeur (lecture en texte : \r\n et \r ramenés à \n).
_LOADER_LINE_BREAK_RE = re.compile(r"\r\n|\r|\n")
#: Caractères que ``str.splitlines()`` prend pour des fins de ligne : refusés dans une valeur (une réécriture
#: découpée autrement que par le chargeur en ferait de nouvelles lignes, donc de nouvelles options).
INI_LINE_BREAKS = frozenset("\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029")
# Identiques à configparser.RawConfigParser.SECTCRE / OPTCRE (délimiteurs par défaut).
_SECTION_RE = re.compile(r"\[(?P<header>.+)\]")
_OPTION_RE = re.compile(r"(?P<option>.*?)\s*[=:]\s*(?P<value>.*)$")


@dataclass(frozen=True)
class IniOption:
    """Option lue par le chargeur : lignes ``start`` à ``end`` (exclue), continuations comprises."""

    section: str
    key: str            # minuscules, comme le chargeur
    start: int
    end: int
    value: str          # première ligne de la valeur, telle qu'écrite (« %% » compris)

    @property
    def multiline(self) -> bool:
        return self.end > self.start + 1


def ini_lines(text: str) -> list[str]:
    """Lignes de ``text`` découpées comme le chargeur (``\n``, ``\r\n``, ``\r`` seulement), jamais sur les
    autres séparateurs Unicode que reconnaît ``str.splitlines()`` (U+2028, U+0085, \v…)."""
    lines = _LOADER_LINE_BREAK_RE.split(text)
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def line_content(line: str) -> str:
    """Ligne sans commentaire, comme le lit le chargeur (commentaire en ligne « # » après un blanc)."""
    stripped = line.strip()
    if not stripped or stripped.startswith(("#", ";")):
        return ""
    match = _INLINE_COMMENT_RE.search(stripped)
    return (stripped[:match.start()] if match else stripped).strip()


def scan(lines: list[str]) -> tuple[list[tuple[str, int]], list[IniOption]]:
    """En-têtes de section (nom exact, ligne) et options, dans l'ordre du fichier."""
    sections: list[tuple[str, int]] = []
    options: list[IniOption] = []
    section: str | None = None
    current: list = []                  # [section, clé, début, dernière ligne, valeur]
    indent_level = 0

    def close() -> None:
        if current:
            options.append(IniOption(current[0], current[1], current[2], current[3] + 1, current[4]))
            current.clear()

    for index, line in enumerate(lines):
        content = line_content(line)
        if not content:
            continue                    # vide ou commentaire : la valeur en cours continue
        indent = len(line) - len(line.lstrip())
        if section is not None and current and indent > indent_level:
            current[3] = index          # continuation de la valeur
            continue
        indent_level = indent
        close()
        header = _SECTION_RE.match(content)
        if header:
            section = header.group("header")
            sections.append((section, index))
            continue
        match = _OPTION_RE.match(content)
        if section is not None and match and match.group("option").strip():
            current[:] = [section, match.group("option").strip().lower(), index, index,
                          match.group("value").strip()]
    close()
    return sections, options


def section_bounds(lines: list[str], section: str) -> tuple[int, int]:
    """Ligne de l'en-tête de ``section`` (nom exact ; -1 si absente) et fin de la section."""
    sections, _options = scan(lines)
    for position, (name, start) in enumerate(sections):
        if name == section:
            end = sections[position + 1][1] if position + 1 < len(sections) else len(lines)
            return start, end
    return -1, len(lines)


def options(lines: list[str], section: str | None = None) -> list[IniOption]:
    """Options du fichier, ou de la première section ``section``."""
    start, end = section_bounds(lines, section) if section is not None else (0, len(lines))
    if start == -1:
        return []
    return [option for option in scan(lines)[1] if start <= option.start < end]


def find_option(lines: list[str], section: str, key: str) -> IniOption | None:
    wanted = key.lower()
    return next((option for option in options(lines, section) if option.key == wanted), None)


def remove_options(lines: list[str], section: str, keys: set[str]) -> list[str]:
    """Retire les options ``keys`` de ``section``, continuations comprises."""
    wanted = {key.lower() for key in keys}
    removed = [option for option in options(lines, section) if option.key in wanted]
    updated = list(lines)
    for option in reversed(removed):
        del updated[option.start:option.end]
    return updated


def option_line(lines: list[str], section: str, key: str, value: str, option: IniOption | None = None) -> str:
    """Ligne ``clé = valeur`` à l'indentation de l'option remplacée, sinon de la dernière option de la
    section : les lignes qui suivent gardent leur rôle (option ou continuation) pour le chargeur."""
    reference = option or next(reversed(options(lines, section)), None)
    # Sans option : indentation de l'en-tête (une ligne moins indentée ferait de la section
    # suivante, si elle est indentée, la continuation de cette valeur).
    line = lines[reference.start] if reference is not None else lines[max(section_bounds(lines, section)[0], 0)]
    indent = line[:len(line) - len(line.lstrip())] if lines else ""
    return f"{indent}{key} = {escape_value(value)}"


def unwritable_reason(value: str) -> str | None:
    """Pourquoi ``value`` ne serait pas relue à l'identique par le chargeur (None si elle le serait) :
    la ligne réellement écrite est relue (blancs de bord exceptés, ignorés par le chargeur)."""
    if "\n" in value or "\r" in value:
        return "retour à la ligne"
    if any(char in INI_LINE_BREAKS for char in value):
        return "séparateur de ligne"
    parser = configparser.ConfigParser(inline_comment_prefixes=("#",), default_section="DEFAULT")
    try:
        parser.read_string(f"[s]\nk = {escape_value(value)}\n")
        read = parser.get("s", "k")
    except (configparser.Error, ValueError):
        return "syntaxe refusée par config.ini"
    if read == value.strip():
        return None
    if "#" in value:
        return "« # » lu comme un commentaire"
    return "valeur relue différemment"


def escape_value(value: str) -> str:
    """Valeur écrite telle que le chargeur la relira (« % » littéral doublé pour l'interpolation)."""
    return value.replace("%", "%%")


def loaded_sections(text: str) -> set[str] | None:
    """Sections lues par le chargeur dans ``text`` (même configuration, lecture stricte, valeurs
    interpolées comme au chargement), None s'il le refuse."""
    parser = configparser.ConfigParser(inline_comment_prefixes=("#",), default_section="DEFAULT")
    try:
        parser.read_string(text)
        for section in parser.sections():
            parser.items(section)       # interpolation : « % » isolé refusé comme au chargement
    except (configparser.Error, ValueError):
        return None
    return set(parser.sections())


def is_loadable(text: str) -> bool:
    return loaded_sections(text) is not None


def rewrite_refusal(original: str | None, rewritten: str) -> str | None:
    """Raison de ne pas remplacer ``original`` (None : fichier absent) par ``rewritten`` : le chargeur
    lisait l'original et refuserait le résultat, ou une de ses sections disparaîtrait. Un original
    déjà illisible n'est pas protégé (rien à préserver)."""
    before = set() if original is None else loaded_sections(original)
    if before is None:
        return None
    after = loaded_sections(rewritten)
    if after is None:
        return "le résultat serait illisible"
    missing = before - after
    return f"sections perdues : {', '.join(sorted(missing))}" if missing else None
