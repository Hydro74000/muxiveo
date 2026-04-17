"""Native ac3_eac3 parser (scaffold)."""

from __future__ import annotations


def parse_ac3_eac3(_data: bytes) -> dict[str, str]:
    return {"codec": "ac3_eac3"}
