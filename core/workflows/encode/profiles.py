"""
core/workflows/encode/profiles.py — JSON persistence for EncodePreset profiles.

Public:
    ProfileManager
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from core.profile_store import ProfileLoadError, ProfileStore
from core.workflows.encode.models import EncodePreset


def _safe_stem(name: str) -> str:
    """Nom de fichier historique : caractères hors ``[\\w-]`` remplacés par ``_``."""
    return re.sub(r"[^\w\-]", "_", name) or "profil"


class ProfileManager:
    """
    Sauvegarde et charge les profils EncodePreset en JSON.

    Dossier : <app_data_dir>/encode_profiles/
    Identité = nom exact du profil (champ ``name``) ; écriture atomique.
    Les profils illisibles restent sur disque et sont listés dans
    :attr:`load_errors` après chaque lecture.
    """

    _FIELDS = EncodePreset.__dataclass_fields__

    def __init__(self, profiles_dir: Path) -> None:
        self._dir = profiles_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._store = ProfileStore(self._dir, _safe_stem, self._name_of)

    @classmethod
    def _preset(cls, raw: dict[str, Any]) -> EncodePreset:
        return EncodePreset(**{k: v for k, v in raw.items() if k in cls._FIELDS})

    @classmethod
    def _name_of(cls, raw: dict[str, Any]) -> str | None:
        try:
            return cls._preset(raw).name
        except Exception:
            return None

    @property
    def load_errors(self) -> list[ProfileLoadError]:
        return list(self._store.load_errors)

    def save(self, preset: EncodePreset) -> None:
        data = preset.to_json_dict()
        self._store.write(preset.name, json.dumps(data, indent=2, ensure_ascii=False))

    def load_all(self) -> list[EncodePreset]:
        presets: list[EncodePreset] = []
        errors: list[ProfileLoadError] = []
        for path, raw in self._store.scan():
            try:
                presets.append(self._preset(raw))
            except Exception as exc:
                errors.append(ProfileLoadError(path, str(exc)))
        self._store.load_errors.extend(errors)
        return presets

    def delete(self, name: str) -> None:
        self._store.delete(name)

    def names(self) -> list[str]:
        return [p.name for p in self.load_all()]
