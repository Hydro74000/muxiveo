"""Container detection and parser routing without external tools."""

from __future__ import annotations

from pathlib import Path

from .container.matroska import parse_matroska
from .container.mp4 import parse_mp4
from .container.text import parse_text
from .container.webm import parse_webm


def _read_head(path: Path, size: int = 64) -> bytes:
    try:
        with path.open("rb") as fh:
            return fh.read(size)
    except OSError:
        return b""


def _looks_like_text(head: bytes) -> bool:
    if not head:
        return False
    try:
        decoded = head.decode("utf-8", errors="replace")
    except Exception:
        return False
    return "-->" in decoded or decoded.lstrip().startswith(("1\n", "1\r\n"))


def detect_container(source: str) -> str:
    path = Path(source)
    ext = path.suffix.lower()
    head = _read_head(path)

    if len(head) >= 8 and head[4:8] == b"ftyp":
        return "mp4"
    if head.startswith(b"\x1A\x45\xDF\xA3"):
        return "webm" if ext == ".webm" else "matroska"
    if head.startswith(b"RIFF") and len(head) >= 12:
        form = head[8:12]
        if form in {b"WAVE", b"AVI "}:
            return "riff"
    if head.startswith(b"OggS"):
        return "ogg"
    if head.startswith((b"\x89PNG\r\n\x1a\n", b"\xFF\xD8\xFF", b"GIF87a", b"GIF89a", b"BM")):
        return "image"
    if ext in {".ts", ".m2ts", ".mts"}:
        return "tsps"
    if _looks_like_text(head):
        return "text"

    if ext in {".mp4", ".m4v", ".mov", ".m4a"}:
        return "mp4"
    if ext in {".mkv"}:
        return "matroska"
    if ext in {".webm"}:
        return "webm"
    if ext in {".wav", ".avi"}:
        return "riff"
    if ext in {".ogg", ".opus"}:
        return "ogg"
    if ext in {".srt", ".ass", ".ssa"}:
        return "text"
    if ext in {".ts", ".m2ts", ".mts"}:
        return "tsps"
    if ext in {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff"}:
        return "image"
    return "unknown"


def dispatch_parser(source: str) -> str:
    return detect_container(source)


def parse_container(source: str) -> dict[str, object]:
    container = detect_container(source)
    if container == "mp4":
        parsed = parse_mp4(source)
        return {"container": "mp4", "metadata": parsed.to_metadata(), "parsed": parsed}
    if container == "matroska":
        parsed = parse_matroska(source)
        return {"container": "matroska", "metadata": parsed.to_metadata(), "parsed": parsed}
    if container == "webm":
        parsed = parse_webm(source)
        return {"container": "webm", "metadata": parsed.to_metadata(), "parsed": parsed}
    if container == "text":
        parsed = parse_text(source)
        data = dict(parsed)
        data.setdefault("container", "text")
        return data
    return {"container": container, "metadata": {"container": container}}
