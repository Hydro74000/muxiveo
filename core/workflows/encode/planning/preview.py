from __future__ import annotations

from .plan_models import EncodeCommandSelection


def format_preview_command(cmd: list[str], *, prefix: str = "") -> str:
    if not cmd:
        return ""
    lines = [cmd[0]]
    index = 1
    while index < len(cmd):
        token = cmd[index]
        if token.startswith("-") and index + 1 < len(cmd) and not cmd[index + 1].startswith("-"):
            lines.append(f"    {token} {cmd[index + 1]}")
            index += 2
        else:
            lines.append(f"    {token}")
            index += 1
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
