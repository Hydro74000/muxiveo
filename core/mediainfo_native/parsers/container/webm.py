"""Native WebM parser specialization."""

from __future__ import annotations

from dataclasses import dataclass

from .matroska import MatroskaParseResult, parse_matroska


@dataclass(slots=True)
class WebmParseResult:
    container: str
    matroska: MatroskaParseResult

    def to_metadata(self) -> dict[str, str]:
        meta = self.matroska.to_metadata()
        meta["container"] = "webm"
        return meta


def parse_webm(source: str) -> WebmParseResult:
    parsed = parse_matroska(source)
    parsed.container = "webm"
    return WebmParseResult(container="webm", matroska=parsed)
