"""Native pcm parser (scaffold)."""

from __future__ import annotations


def parse_pcm(_data: bytes) -> dict[str, str]:
    return {"codec": "pcm"}
