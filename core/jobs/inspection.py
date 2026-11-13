"""Modèles de job tirés d'une inspection (``inspect --config-template``)."""

from __future__ import annotations

from typing import Any

from core.inspector import FileInfo


def config_template_from_info(info: FileInfo, *, output: str = "") -> dict[str, Any]:
    """Exact-job minimal d'une source : toutes ses pistes, chapitres de la source, sans pièce jointe."""
    return {
        "version": 1,
        "kind": "exact-job",
        "sources": [{"path": str(info.path), "attachments": "none", "copy_tags": False}],
        "output": output or str(info.path.with_suffix(".remux.mkv")),
        "chapters": {"source_index": 0},
    }
