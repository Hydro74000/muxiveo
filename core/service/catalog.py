"""Catalogue des opérations publiées (liste explicite, sans enregistrement par import)."""

from __future__ import annotations

from core.service.operations import Catalog
from core.service.ops import batch, media, profile, system

CATALOG = Catalog((
    system.system_version,
    media.media_inspect,
    media.media_validate,
    media.media_preview,
    media.media_run,
    batch.media_batch,
    profile.profile_validate,
    profile.profile_preview,
    profile.profile_apply,
    batch.profile_batch,
))
