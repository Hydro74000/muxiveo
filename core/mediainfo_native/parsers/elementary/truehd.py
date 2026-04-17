"""Native truehd parser (scaffold)."""

from __future__ import annotations


def parse_truehd(_data: bytes) -> dict[str, str]:
    return {"codec": "truehd"}
