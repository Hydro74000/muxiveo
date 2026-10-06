from __future__ import annotations

import shlex
import subprocess
import sys

from .plan_models import EncodeCommandSelection


def quote_preview_argument(argument: str, *, platform: str | None = None) -> str:
    """Argument protégé pour un collage dans un terminal (POSIX : shlex ; Windows : CreateProcess / cmd)."""
    if (sys.platform if platform is None else platform) == "win32":
        return subprocess.list2cmdline([str(argument)])
    return shlex.quote(str(argument))


def format_preview_command(cmd: list[str], *, prefix: str = "", platform: str | None = None) -> str:
    """Commande lisible et collable telle quelle.

    POSIX : une option par ligne, lignes continuées par ``\\``. Windows : une
    seule ligne (aucune continuation commune à cmd et PowerShell). Le ``|``
    d'un pipeline n'est jamais protégé.
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
        blocks.append(f"# Commande {index}\n" + format_preview_command(cmd))
    return "\n\n".join(blocks)


def format_preview_selection(selection: EncodeCommandSelection) -> str:
    if selection.is_multi_video:
        return format_preview_commands([list(cmd) for cmd in selection.commands])

    cmd = list(selection.preview_command)
    if not cmd:
        return ""
    prefix = (
        "# Mode taille cible : passe 1 omise de cet aperçu\n"
        if selection.is_two_pass
        else ""
    )
    return format_preview_command(cmd, prefix=prefix)
