"""
core/workflows/encode/profiles.py — JSON persistence for EncodePreset profiles.

Public:
    ProfileManager
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from core.workflows.encode.models import EncodePreset


class ProfileManager:
    """
    Sauvegarde et charge les profils EncodePreset en JSON.

    Dossier : <app_data_dir>/encode_profiles/
    """

    _FIELDS = EncodePreset.__dataclass_fields__

    def __init__(self, profiles_dir: Path) -> None:
        self._dir = profiles_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    def save(self, preset: EncodePreset) -> None:
        safe = re.sub(r"[^\w\-]", "_", preset.name)
        path = self._dir / f"{safe}.json"
        data = preset.to_json_dict()
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def load_all(self) -> list[EncodePreset]:
        presets: list[EncodePreset] = []
        for f in sorted(self._dir.glob("*.json")):
            try:
                raw = json.loads(f.read_text(encoding="utf-8"))
                presets.append(EncodePreset(**{k: v for k, v in raw.items() if k in self._FIELDS}))
            except Exception:
                pass
        return presets

    def delete(self, name: str) -> None:
        safe = re.sub(r"[^\w\-]", "_", name)
        (self._dir / f"{safe}.json").unlink(missing_ok=True)

    def names(self) -> list[str]:
        return [p.name for p in self.load_all()]
