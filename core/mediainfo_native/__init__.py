"""
Public API for native MediaInfo shim.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .cli import CliResult, run_cli
from .compat import MediaInfo, MediaInfoList
from .engine import (
    CLI_VERSION_TEXT,
    VERSION_TEXT,
    MediaInfoEngine,
    MediaInfoNativeError,
)

__all__ = [
    "CLI_VERSION_TEXT",
    "VERSION_TEXT",
    "MediaInfoEngine",
    "MediaInfoNativeError",
    "MediaInfo",
    "MediaInfoList",
    "CompatCompletedProcess",
    "query_inform",
    "render_text_report",
    "render_output",
    "run_cli",
    "run_mediainfo_subprocess_compat",
]

_DEFAULT_ENGINE = MediaInfoEngine()


@dataclass(slots=True)
class CompatCompletedProcess:
    args: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""


def query_inform(source: str | Path, inform_expr: str) -> str:
    return _DEFAULT_ENGINE.query_inform(str(source), inform_expr)


def render_text_report(source: str | Path) -> str:
    return _DEFAULT_ENGINE.render(str(source), output_mode="Text")


def render_output(source: str | Path, output_mode: str) -> str:
    return _DEFAULT_ENGINE.render(str(source), output_mode=output_mode)


def run_mediainfo_subprocess_compat(args: Sequence[str]) -> CompatCompletedProcess:
    """
    Subprocess-like helper for callers that still build a mediainfo CLI command.
    """
    argv = list(args)
    if not argv:
        result = CliResult(returncode=1, stderr="Empty mediainfo command\n")
        return CompatCompletedProcess(argv, result.returncode, result.stdout, result.stderr)

    # Drop binary token if present.
    if not argv[0].startswith("--") and argv[0] not in {"-h", "-f"} and not Path(argv[0]).exists():
        argv = argv[1:]
    elif Path(argv[0]).name.lower().startswith("mediainfo"):
        argv = argv[1:]

    result = run_cli(argv)
    return CompatCompletedProcess(list(args), result.returncode, result.stdout, result.stderr)
