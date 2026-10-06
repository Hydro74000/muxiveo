from __future__ import annotations

# Formateur partagé avec le remux ; réexporté pour les imports existants.
from core.command_preview import (  # noqa: F401
    _quote_cmd,
    format_preview_command,
    format_preview_commands,
    preview_comment,
    quote_preview_argument,
)

from .plan_models import EncodeCommandSelection


def format_preview_selection(selection: EncodeCommandSelection) -> str:
    if selection.is_multi_video:
        return format_preview_commands([list(cmd) for cmd in selection.commands])

    cmd = list(selection.preview_command)
    if not cmd:
        return ""
    prefix = (
        preview_comment("Mode taille cible : passe 1 omise de cet aperçu") + "\n"
        if selection.is_two_pass
        else ""
    )
    return format_preview_command(cmd, prefix=prefix)
