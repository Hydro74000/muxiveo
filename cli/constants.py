"""Constants shared by the headless CLI."""

from __future__ import annotations


EXIT_OK = 0
EXIT_ARGS = 2
EXIT_VALIDATION = 3
EXIT_TOOL = 4
EXIT_EXISTS = 5
EXIT_WORKFLOW = 6
EXIT_PARTIAL = 7
EXIT_UPDATE_AVAILABLE = 8

TRACK_TYPES = {"video", "audio", "subtitle"}
FLAG_NAMES = (
    "enabled",
    "default",
    "forced",
    "hearing_impaired",
    "visual_impaired",
    "original",
    "commentary",
)

# Énumérations du contrat de job : source unique du schéma public (schema.py)
# et du validateur structurel (contract.py).
SYNC_MODES = ("physical", "container")
SYNC_SUBTITLE_MODES = ("mirror", "none")
SYNC_REWRITE_MODES = ("", "offset")
MUX_BACKEND_CHOICES = ("auto", "native", "ffmpeg")
TMDB_KINDS = ("all", "movie", "tv")
