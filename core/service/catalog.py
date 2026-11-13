"""Catalogue des opérations publiées (liste explicite, sans enregistrement par import)."""

from __future__ import annotations

from core.service.operations import Catalog
from core.service.ops import batch, encode, hdr, hybrid, media, plugins, profile, sync, system

CATALOG = Catalog((
    system.system_version,
    system.system_tools,
    system.system_capabilities,
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
    encode.encode_preview_capture,
    encode.preset_list,
    encode.preset_show,
    encode.preset_save,
    encode.preset_import,
    encode.preset_export,
    encode.preset_delete,
))
