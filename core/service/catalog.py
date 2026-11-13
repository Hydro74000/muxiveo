"""Catalogue des opérations publiées (liste explicite, sans enregistrement par import)."""

from __future__ import annotations

from core.service.operations import Catalog
from core.service.ops import media, system

CATALOG = Catalog((
    system.system_version,
    media.media_inspect,
))
