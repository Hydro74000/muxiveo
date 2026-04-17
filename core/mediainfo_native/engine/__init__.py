"""
Public facade for the MediaInfo engine.

This module intentionally re-exports the API layer so existing imports
(`core.mediainfo_native.engine`) keep working while internals are being
modularized.
"""

from __future__ import annotations

from ..api.engine import (
    CLI_VERSION_TEXT,
    VERSION_TEXT,
    MediaInfoEngine,
    MediaInfoNativeError,
    analyze,
)

__all__ = [
    "CLI_VERSION_TEXT",
    "VERSION_TEXT",
    "MediaInfoEngine",
    "MediaInfoNativeError",
    "analyze",
]
