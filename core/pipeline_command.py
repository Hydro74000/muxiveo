"""
core/pipeline_command.py — Commande composée de processus reliés par des pipes.

``PipelineCommand`` permet de faire transiter un pipeline ``a | b | c`` par les
API qui manipulent une commande ``list[str]`` (constructeurs, logs, runner).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path
from core.fel.engine import FelSource


class PipelineCommand(list[str]):
    """Commande finale précédée d'étages amont reliés par des pipes (stdout → stdin).

    La liste elle-même est le dernier étage : les constructeurs de commandes
    existants continuent de la compléter avec ``extend``/``append``.
    ``ToolRunner._run_cmd`` exécute l'ensemble des étages.
    """

    def __init__(self, final: Iterable[str], upstream: Sequence[Sequence[str]], *, producer: FelSource | None = None) -> None:
        super().__init__(final)
        self.upstream: list[list[str]] = [list(stage) for stage in upstream]
        self.producer = producer

    def stages(self) -> list[list[str]]:
        return [*self.upstream, list(self)]

    def display(self) -> str:
        prefix = f"[{self.producer.describe()}] → " if self.producer else ""
        return prefix + " | ".join(" ".join(stage) for stage in self.stages())


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


def is_broken_pipe_exit(stage: Sequence[str], returncode: int, stderr: str = "") -> bool:
    """Vrai si un étage s'est arrêté parce que l'aval a fermé le pipe (échec secondaire).

    SIGPIPE (``-13``), code ``EXIT_IO`` de muxiveo-rife (écriture interrompue)
    ou diagnostic « Broken pipe » (FFmpeg : ``1`` ou ``224`` = AVERROR(EPIPE)).
    """
    if returncode == -13:
        return True
    if returncode == 4 and stage and Path(stage[0]).stem == "muxiveo-rife":
        return True
    return returncode != 0 and "broken pipe" in stderr.casefold()


def pipeline_root_failure(results: Sequence[tuple[Sequence[str], int, str]]) -> int | None:
    """Index de l'étage à l'origine de l'échec d'un pipeline (None si tout a réussi).

    ``results`` : ``(étage, code, stderr)`` de l'amont vers l'aval. Un étage
    tué par un pipe fermé est une conséquence de l'aval ; parmi les échecs
    restants, le plus en amont est la cause (l'aval reçoit un flux tronqué).
    """
    failed = [index for index, result in enumerate(results) if result[1] != 0]
    for index in failed:
        stage, rc, err = results[index]
        if index == len(results) - 1 or not is_broken_pipe_exit(stage, rc, err):
            return index
    return failed[0] if failed else None


def command_preview_tokens(cmd: Sequence[str]) -> list[str]:
    """Jetons d'aperçu : étages séparés par ``|`` (copiable dans un shell)."""
    tokens: list[str] = []
    for index, stage in enumerate(command_stages(cmd)):
        if index:
            tokens.append("|")
        tokens.extend(stage)
    return tokens


__all__ = [
    "PipelineCommand",
    "command_display",
    "command_preview_tokens",
    "command_stages",
    "is_broken_pipe_exit",
    "pipeline_root_failure",
]
