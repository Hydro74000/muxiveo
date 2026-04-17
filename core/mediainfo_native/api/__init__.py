"""Internal API surface for the native MediaInfo module."""

from .engine import CLI_VERSION_TEXT, VERSION_TEXT, MediaInfoEngine, MediaInfoNativeError, analyze
from .model import FieldValue, MediaDocument, Track, from_legacy_report, from_report

__all__ = [
    "CLI_VERSION_TEXT",
    "VERSION_TEXT",
    "MediaInfoEngine",
    "MediaInfoNativeError",
    "analyze",
    "FieldValue",
    "Track",
    "MediaDocument",
    "from_report",
    "from_legacy_report",
]
