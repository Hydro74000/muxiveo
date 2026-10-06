"""
core/command_preview.py — Commandes copiables dans un terminal (aperçus, journaux).

Rendu POSIX (``shlex.quote``, une option par ligne, continuations ``\\``) ou
cmd.exe (protection des opérateurs, ``%`` et ``!``, une ligne par commande,
commentaires ``REM``). Consomme des argv structurés : jamais une chaîne de
journal ré-analysée. Partagé par l'encodage et le remux.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys


# cmd.exe : shell cible sous Windows (PowerShell 5.1 altère les pipes binaires
# ffmpeg | muxiveo-rife | encodeur). Caractères interprétés hors guillemets.
_CMD_SPECIAL = frozenset(' \t"&|<>^(),;=%!')


def _quote_cmd(argument: str) -> str:
    """Argument pour une invite cmd.exe, lu ensuite par l'analyse argv de CreateProcess (MSVCRT).

    Les fragments contenant des opérateurs sont entre guillemets. ``%`` et
    ``!`` sont protégés hors guillemets, même avec l'expansion différée ; les
    guillemets littéraux sont échappés pour cmd puis pour MSVCRT. Les antislashs
    précédant une fermeture de guillemets sont doublés.
    """
    if argument and not any(char in _CMD_SPECIAL for char in argument):
        return argument
    out: list[str] = []
    for match in re.finditer(r'[^"%!]+|["%!]', argument):
        part = match.group()
        if part == '"':
            out.append('\\^"')
        elif part in {"%", "!"}:
            out.append("^" + part)
        elif any(char in _CMD_SPECIAL for char in part) or part.endswith("\\"):
            quoted = subprocess.list2cmdline([part])
            if not quoted.startswith('"'):
                trailing = len(quoted) - len(quoted.rstrip("\\"))
                quoted = '"' + quoted + "\\" * trailing + '"'
            out.append(quoted)
        else:
            out.append(part)
    if not out:
        return '""'
    return "".join(out)


def preview_comment(text: str, *, platform: str | None = None) -> str:
    """Ligne de commentaire du shell cible (``REM`` sous cmd.exe, ``#`` en POSIX)."""
    return ("REM " if (sys.platform if platform is None else platform) == "win32" else "# ") + text


def quote_preview_argument(argument: str, *, platform: str | None = None) -> str:
    """Argument protégé pour un collage dans un terminal (POSIX : shlex ; Windows : cmd.exe)."""
    if (sys.platform if platform is None else platform) == "win32":
        return _quote_cmd(str(argument))
    return shlex.quote(str(argument))


def format_preview_command(cmd: list[str], *, prefix: str = "", platform: str | None = None) -> str:
    """Commande lisible et collable telle quelle.

    POSIX : une option par ligne, lignes continuées par ``\\``. Windows : syntaxe
    cmd.exe, une seule ligne par commande. Le ``|`` d'un pipeline n'est jamais
    protégé.
    """
    if not cmd:
        return ""
    windows = (sys.platform if platform is None else platform) == "win32"

    def q(argument: str) -> str:
        return quote_preview_argument(argument, platform=platform)

    lines = [q(cmd[0])]
    index = 1
    while index < len(cmd):
        argument = cmd[index]
        if argument == "|" and index + 1 < len(cmd):
            # étage suivant d'un pipeline (ex. décodage | muxiveo-rife | encodeur)
            lines.append(f"| {q(cmd[index + 1])}")
            index += 2
        elif argument.startswith("-") and index + 1 < len(cmd) and not cmd[index + 1].startswith("-") and cmd[index + 1] != "|":
            lines.append(f"    {q(argument)} {q(cmd[index + 1])}")
            index += 2
        else:
            lines.append(f"    {q(argument)}")
            index += 1
    if windows:
        return prefix + " ".join(line.strip() for line in lines)
    return prefix + " \\\n".join(lines)


def format_preview_commands(commands: list[list[str]]) -> str:
    blocks: list[str] = []
    for index, cmd in enumerate(commands, start=1):
        if not cmd:
            continue
        blocks.append(preview_comment(f"Commande {index}") + "\n" + format_preview_command(cmd))
    return "\n\n".join(blocks)


__all__ = [
    "format_preview_command",
    "format_preview_commands",
    "preview_comment",
    "quote_preview_argument",
]
