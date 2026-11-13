"""Constantes du CLI ; celles des jobs vivent dans ``core.jobs.constants``."""

from __future__ import annotations

from core.jobs.constants import *  # noqa: F401,F403

#: Formats du journal : texte, JSON Lines historique, événements versionnés de la couche de service.
LOG_FORMATS = ("text", "jsonl", "events")
