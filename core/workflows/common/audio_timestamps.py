"""Origine commune du média et conservation du pré-roll des artefacts audio."""
from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path
from typing import Callable, Mapping

from core.bluray import ffprobe_input_args
from core.runner import TaskCancelledError
from core.subprocess_utils import run_cancellable_capture, subprocess_text_kwargs
from core.workflows.remux_models import RemuxError


def timestamp_seconds(value: object) -> float:
    """Valeur temporelle finie ; les formats sans origine explicite commencent à zéro."""
    try:
        result = float(str(value))
    except (ValueError, TypeError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def common_timestamp_origin(payload: Mapping[str, object]) -> float:
    """Origine du conteneur, sans confondre son pré-roll négatif avec un décalage global.

    FFprobe 6 soustrait CodecDelay au start_time Matroska, FFprobe 8 expose
    l'origine présentée. Un pré-roll borné par initial_padding reste négatif ;
    une origine absolue (TS, M2TS, Blu-ray) est retirée avant toute coupe PCM.
    """
    fmt = payload.get("format")
    start = timestamp_seconds(fmt.get("start_time")) if isinstance(fmt, dict) else 0.0
    padding = 0.0
    streams = payload.get("streams")
    for stream in streams if isinstance(streams, list) else []:
        if not isinstance(stream, dict):
            continue
        rate = timestamp_seconds(stream.get("sample_rate"))
        if rate > 0:
            padding = max(padding, timestamp_seconds(stream.get("initial_padding")) / rate)
    if start < 0 and padding > 0 and start >= -padding - 0.001:
        return 0.0
    return start


def preroll_input_offset(payload: Mapping[str, object]) -> float:
    """Annule le rebasage dû au seul pré-roll, sans conserver une origine TS absolue."""
    fmt = payload.get("format")
    start = timestamp_seconds(fmt.get("start_time")) if isinstance(fmt, dict) else 0.0
    return start - common_timestamp_origin(payload)


def adjust_input_timestamp(cmd: list[str], input_index: int, offset_s: float) -> None:
    """Compose la correction avec l'offset éventuel de cette entrée, dans un argv existant."""
    if not offset_s:
        return
    positions = [index for index, arg in enumerate(cmd) if arg == "-i"]
    position = positions[input_index]
    start = positions[input_index - 1] + 2 if input_index else 1
    options = cmd[start:position]
    if "-itsoffset" in options:
        value_index = start + options.index("-itsoffset") + 1
        cmd[value_index] = f"{float(cmd[value_index]) + offset_s:.9f}"
    else:
        cmd[position:position] = input_timestamp_offset_args(offset_s)


def probe_timestamp_payload(
    source: Path, ffprobe: str, *, cancel_cb: Callable[[], bool] | None = None,
) -> dict[str, object]:
    """Sonde courte annulable, réservée à l'exécution ; jamais appelée par les previews."""
    def check_cancelled() -> None:
        if cancel_cb is not None and cancel_cb():
            raise TaskCancelledError()

    command = [
        ffprobe, "-v", "error", "-show_entries",
        "format=start_time:stream=initial_padding,sample_rate", "-of", "json",
        *ffprobe_input_args(source),
    ]
    try:
        result = run_cancellable_capture(
            command, cancel_cb=cancel_cb, check_cancelled=check_cancelled,
            check=True, timeout=20, **subprocess_text_kwargs(),
        )
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict):
            raise ValueError("réponse ffprobe non objet")
        return payload
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise RemuxError(f"Origine temporelle illisible : {source.name}") from exc


def input_timestamp_offset_args(offset_s: float) -> list[str]:
    """Compense le rebasage FFmpeg d'une seule entrée, sans modifier les autres sources."""
    return ["-itsoffset", f"{offset_s:.9f}"] if offset_s else []
