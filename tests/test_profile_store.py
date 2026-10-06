"""
tests/test_profile_store.py — Identité et persistance des profils (A06, A16).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.profiles.decision import DecisionProfileManager
from core.workflows.encode.models import EncodePreset
from core.workflows.encode.profiles import ProfileManager


def _decision(name: str, description: str = "") -> dict:
    return {"version": 1, "kind": "decision-profile", "name": name, "description": description, "rules": []}


# ---------------------------------------------------------------------------
# Profils d'encodage
# ---------------------------------------------------------------------------

def test_encode_profiles_with_same_normalized_name_coexist(tmp_path: Path) -> None:
    """Reproduction de l'audit : `Film/4K` puis `Film:4K`."""
    manager = ProfileManager(tmp_path)
    manager.save(EncodePreset(name="Film/4K", crf=18))
    manager.save(EncodePreset(name="Film:4K", crf=22))
    presets = {p.name: p for p in manager.load_all()}
    assert set(presets) == {"Film/4K", "Film:4K"}
    assert presets["Film/4K"].crf == 18 and presets["Film:4K"].crf == 22

    manager.delete("Film:4K")
    assert manager.names() == ["Film/4K"]
    assert {p.name for p in manager.load_all()} == {"Film/4K"}


def test_encode_profile_names_differing_by_case_coexist(tmp_path: Path) -> None:
    manager = ProfileManager(tmp_path)
    manager.save(EncodePreset(name="Film", crf=18))
    manager.save(EncodePreset(name="film", crf=20))
    assert sorted(manager.names()) == ["Film", "film"]
    # Noms de fichiers distincts même sur un système insensible à la casse.
    stems = [p.stem.casefold() for p in tmp_path.glob("*.json")]
    assert len(stems) == len(set(stems)) == 2


def test_encode_profile_resave_rewrites_same_file(tmp_path: Path) -> None:
    manager = ProfileManager(tmp_path)
    manager.save(EncodePreset(name="HQ", crf=18))
    manager.save(EncodePreset(name="HQ", crf=16))
    files = list(tmp_path.glob("*.json"))
    assert [f.name for f in files] == ["HQ.json"]
    assert manager.load_all()[0].crf == 16


def test_legacy_profile_file_is_reused_without_rename(tmp_path: Path) -> None:
    legacy = tmp_path / "Film_4K.json"
    legacy.write_text(json.dumps({"name": "Film/4K", "crf": 19}), encoding="utf-8")
    manager = ProfileManager(tmp_path)
    manager.save(EncodePreset(name="Film/4K", crf=17))
    manager.save(EncodePreset(name="Film:4K", crf=23))
    assert json.loads(legacy.read_text(encoding="utf-8"))["crf"] == 17
    assert len(list(tmp_path.glob("*.json"))) == 2


def test_interrupted_save_keeps_previous_profile(tmp_path: Path, monkeypatch) -> None:
    manager = ProfileManager(tmp_path)
    manager.save(EncodePreset(name="HQ", crf=18))
    before = (tmp_path / "HQ.json").read_text(encoding="utf-8")

    def broken_replace(*_args, **_kwargs):
        raise OSError("disque plein")

    monkeypatch.setattr(os, "replace", broken_replace)
    with pytest.raises(OSError):
        manager.save(EncodePreset(name="HQ", crf=30))
    monkeypatch.undo()
    assert (tmp_path / "HQ.json").read_text(encoding="utf-8") == before
    assert [p.name for p in tmp_path.iterdir()] == ["HQ.json"]


def test_unreadable_profile_is_reported_and_kept(tmp_path: Path) -> None:
    (tmp_path / "broken.json").write_text("{ pas du json", encoding="utf-8")
    manager = ProfileManager(tmp_path)
    manager.save(EncodePreset(name="OK"))
    assert manager.names() == ["OK"]
    assert [e.path.name for e in manager.load_errors] == ["broken.json"]
    assert (tmp_path / "broken.json").exists()


# ---------------------------------------------------------------------------
# Profils décisionnels
# ---------------------------------------------------------------------------

def test_decision_profiles_with_same_normalized_name_coexist(tmp_path: Path) -> None:
    manager = DecisionProfileManager(tmp_path)
    first = manager.save(_decision("Film/4K", "un"))
    second = manager.save(_decision("Film:4K", "deux"))
    assert first != second
    assert (manager.load("Film/4K") or {}).get("description") == "un"
    assert (manager.load("Film:4K") or {}).get("description") == "deux"
    manager.delete("Film/4K")
    assert manager.names() == ["Film:4K"]
    assert manager.path_for_name("Film:4K") == second


def test_decision_profile_cli_lookup_by_exact_name(tmp_path: Path) -> None:
    from cli.profile import resolve_decision_profile_path

    manager = DecisionProfileManager(tmp_path / "decision")
    manager.save(_decision("Film/4K", "un"))
    second = manager.save(_decision("Film:4K", "deux"))
    config = SimpleNamespace(profiles_dir=tmp_path)
    assert resolve_decision_profile_path("Film:4K", config) == second  # type: ignore[arg-type]


def test_unreadable_decision_profile_is_reported(tmp_path: Path) -> None:
    (tmp_path / "broken.json").write_text("[1, 2", encoding="utf-8")
    manager = DecisionProfileManager(tmp_path)
    manager.save(_decision("OK"))
    assert manager.names() == ["OK"]
    assert [e.path.name for e in manager.load_errors] == ["broken.json"]
