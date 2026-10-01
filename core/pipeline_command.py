"""
core/pipeline_command.py — Commande composée de processus reliés par des pipes.

``PipelineCommand`` permet de faire transiter un pipeline ``a | b | c`` par les
API qui manipulent une commande ``list[str]`` (constructeurs, logs, runner).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence


class PipelineCommand(list[str]):
    """Commande finale précédée d'étages amont reliés par des pipes (stdout → stdin).

    La liste elle-même est le dernier étage : les constructeurs de commandes
    existants continuent de la compléter avec ``extend``/``append``.
    ``ToolRunner._run_cmd`` exécute l'ensemble des étages.
    """

    def __init__(self, final: Iterable[str], upstream: Sequence[Sequence[str]]) -> None:
        super().__init__(final)
        self.upstream: list[list[str]] = [list(stage) for stage in upstream]

    def stages(self) -> list[list[str]]:
        return [*self.upstream, list(self)]

    def display(self) -> str:
        return " | ".join(" ".join(stage) for stage in self.stages())


def command_stages(cmd: Sequence[str]) -> list[list[str]]:
    """Étages d'une commande : un seul pour une commande simple."""
    if isinstance(cmd, PipelineCommand):
        return cmd.stages()
    return [list(cmd)]


def command_display(cmd: Sequence[str]) -> str:
    """Représentation shell lisible (pipeline compris) d'une commande."""
    if isinstance(cmd, PipelineCommand):
        return cmd.display()
    return " ".join(cmd)


def command_preview_tokens(cmd: Sequence[str]) -> list[str]:
    """Jetons d'aperçu : étages séparés par ``|`` (copiable dans un shell)."""
    tokens: list[str] = []
    for index, stage in enumerate(command_stages(cmd)):
        if index:
            tokens.append("|")
        tokens.extend(stage)
    return tokens


__all__ = ["PipelineCommand", "command_display", "command_preview_tokens", "command_stages"]
