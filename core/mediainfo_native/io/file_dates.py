"""Shared helpers for filesystem date formatting."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from os import stat_result


def epoch_ms_from_stat_mtime(stat: stat_result) -> int:
    if hasattr(stat, "st_mtime_ns"):
        return int(stat.st_mtime_ns / 1_000_000)
    return int(stat.st_mtime * 1000.0)


def format_file_dates_from_ms(epoch_ms: int) -> tuple[str, str]:
    dt_utc = datetime.fromtimestamp(epoch_ms / 1000.0, tz=timezone.utc)
    dt_local = dt_utc.astimezone()
    if os.name == "nt":
        ms_suffix = f".{epoch_ms % 1000:03d}"
        return (
            dt_utc.strftime("%Y-%m-%d %H:%M:%S") + ms_suffix + " UTC",
            dt_local.strftime("%Y-%m-%d %H:%M:%S") + ms_suffix,
        )
    return (
        dt_utc.strftime("%Y-%m-%d %H:%M:%S UTC"),
        dt_local.strftime("%Y-%m-%d %H:%M:%S"),
    )
