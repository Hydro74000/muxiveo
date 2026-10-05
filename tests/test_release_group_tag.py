"""Tests du tag de Release Group ajouté aux noms de fichier de sortie proposés."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _qt_app(qt_app):
    return qt_app


def _make_config(tmp_path: Path, ini_content: str = ""):
    import core.config as cfg_mod
    from core.config import AppConfig

    ini_path = tmp_path / "config.ini"
    ini_path.write_text(ini_content, encoding="utf-8")
    settings = MagicMock()
    settings.value.side_effect = lambda key, default=None: default
    with patch("core.config.QSettings", return_value=settings), \
         patch("core.config._app_data_dir", return_value=tmp_path), \
         patch.object(cfg_mod, "_INI_PATH", ini_path):
        return AppConfig()


class TestReleaseGroupTagConfig:

    def test_default_adds_nothing(self, tmp_path):
        """Par défaut : désactivé, tag vide, aucun suffixe."""
        cfg = _make_config(tmp_path, f"[paths]\noutput_dir = {tmp_path}\n")
        assert cfg.release_group_tag_enabled is False
        assert cfg.release_group_tag == ""
        assert cfg.default_output_path(Path("/src/Film.2024.mkv")) == tmp_path / "Film.2024.mkv"

    def test_enabled_tag_is_appended(self, tmp_path):
        """Activé + « ReleaseGROUP » → « -ReleaseGROUP » ajouté au nom proposé."""
        cfg = _make_config(
            tmp_path,
            f"[paths]\noutput_dir = {tmp_path}\nrelease_group_tag_enabled = true\nrelease_group_tag = ReleaseGROUP\n",
        )
        assert cfg.default_output_path(Path("/src/Film.mp4")) == tmp_path / "Film-ReleaseGROUP.mkv"

    def test_tag_ignored_when_disabled(self, tmp_path):
        """Un tag saisi mais désactivé n'est pas ajouté."""
        cfg = _make_config(tmp_path, f"[paths]\noutput_dir = {tmp_path}\nrelease_group_tag = ReleaseGROUP\n")
        assert cfg.default_output_path(Path("/src/Film.mkv")).name == "Film.mkv"

    def test_enabled_without_tag_uses_nogroup(self, tmp_path):
        """Activé sans tag (ou tag réduit à rien après nettoyage) → « -NoGroup »."""
        cfg = _make_config(tmp_path, f"[paths]\noutput_dir = {tmp_path}\nrelease_group_tag_enabled = true\nrelease_group_tag = - \n")
        assert cfg.default_output_path(Path("/src/Film.mkv")).name == "Film-NoGroup.mkv"

    def test_tag_is_normalized(self, tmp_path):
        """Tiret de tête, espaces et caractères interdits retirés ; pas de doublon si déjà présent."""
        cfg = _make_config(
            tmp_path,
            f"[paths]\noutput_dir = {tmp_path}\nrelease_group_tag_enabled = true\nrelease_group_tag =  -Re/lease:GROUP \n",
        )
        assert cfg.release_group_tag == "ReleaseGROUP"
        assert cfg.default_output_path(Path("/src/Film-releasegroup.mkv")).name == "Film-releasegroup.mkv"

    def test_ini_sections_round_trip(self, tmp_path):
        cfg = _make_config(tmp_path, "[paths]\nrelease_group_tag_enabled = true\nrelease_group_tag = GRP\n")
        paths = cfg.to_ini_sections()["paths"]
        assert paths["release_group_tag_enabled"] == "true"
        assert paths["release_group_tag"] == "GRP"


class TestReleaseGroupTagSettingsPanel:

    def test_tag_field_follows_checkbox(self, tmp_path):
        """Le champ de saisie n'est actif que si la case est cochée."""
        from ui.panels.settings_panel import SettingsPanel

        panel = SettingsPanel(_make_config(tmp_path))
        checkbox = cast(Any, panel.widget_for("paths", "release_group_tag_enabled"))
        edit = cast(Any, panel.widget_for("paths", "release_group_tag"))
        assert checkbox.isChecked() is False
        assert edit.isEnabled() is False
        checkbox.setChecked(True)
        assert edit.isEnabled() is True
