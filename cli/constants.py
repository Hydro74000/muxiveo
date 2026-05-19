"""Constants shared by the headless CLI."""

from __future__ import annotations


EXIT_OK = 0
EXIT_ARGS = 2
EXIT_VALIDATION = 3
EXIT_TOOL = 4
EXIT_EXISTS = 5
EXIT_WORKFLOW = 6
EXIT_PARTIAL = 7

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
