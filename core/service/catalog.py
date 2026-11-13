"""Catalogue des opérations publiées (liste explicite, sans enregistrement par import)."""

from __future__ import annotations

from core.service.operations import Catalog
from core.service.ops import batch, hdr, hybrid, media, plugins, profile, sync, system

CATALOG = Catalog((
    system.system_version,
    system.system_tools,
    system.schema_get,
    media.media_inspect,
    media.media_validate,
    media.media_preview,
    media.media_run,
    batch.media_batch,
    profile.profile_validate,
    profile.profile_preview,
    profile.profile_apply,
    batch.profile_batch,
    plugins.plugins_list,
    plugins.plugins_install,
    plugins.plugins_update,
    plugins.plugins_remove,
    sync.sync_scan,
    sync.sync_shift_subtitles,
    hdr.hdr_inspect,
    hybrid.hybrid_run,
))
