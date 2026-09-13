"""
tests/test_remux_panel_tags.py — Synchronisation des balises globales du
panneau Conteneur quand une ligne de balises source est (dé)cochée.
"""
from __future__ import annotations

import pytest

from ui.panels.remux_panel.widgets.attachments import _AttachmentItemWidget, _AttachmentPanel


@pytest.fixture
def panel(qt_app):
    return _AttachmentPanel(config=None)


def _add_tag_item(panel: _AttachmentPanel, file_id: str, tags: dict[str, str]) -> _AttachmentItemWidget:
    item = _AttachmentItemWidget(file_id=file_id, source_color="#fff", tags=tags, is_tag=True)
    panel._add_item(item)
    return item


class TestPanelTagOverridesSync:
    def test_uncheck_source_tags_removes_them_from_edited_tags(self, panel):
        item = _add_tag_item(panel, "f1", {"ENCODED_BY": "j3rico", "IMDB": "tt1"})
        # Simule une édition (TMDB) : les overrides figent les balises fusionnées.
        panel._panel_tag_overrides = {"ENCODED_BY": "j3rico", "IMDB": "tt1", "GENRE": "Action"}

        item._cb.setChecked(False)

        assert panel.get_global_tag_overrides() == {"GENRE": "Action"}

    def test_uncheck_keeps_tags_still_provided_by_another_checked_source(self, panel):
        item1 = _add_tag_item(panel, "f1", {"IMDB": "tt1"})
        _add_tag_item(panel, "f2", {"IMDB": "tt1"})
        panel._panel_tag_overrides = {"IMDB": "tt1"}

        item1._cb.setChecked(False)

        assert panel.get_global_tag_overrides() == {"IMDB": "tt1"}

    def test_uncheck_keeps_user_modified_value(self, panel):
        item = _add_tag_item(panel, "f1", {"IMDB": "tt1"})
        panel._panel_tag_overrides = {"IMDB": "tt-corrigé"}

        item._cb.setChecked(False)

        assert panel.get_global_tag_overrides() == {"IMDB": "tt-corrigé"}

    def test_recheck_restores_source_tags(self, panel):
        item = _add_tag_item(panel, "f1", {"ENCODED_BY": "j3rico"})
        panel._panel_tag_overrides = {"ENCODED_BY": "j3rico", "GENRE": "Action"}

        item._cb.setChecked(False)
        item._cb.setChecked(True)

        assert panel.get_global_tag_overrides() == {"GENRE": "Action", "ENCODED_BY": "j3rico"}

    def test_uncheck_without_edition_requests_explicit_tag_removal(self, panel):
        """Décocher sans édition = suppression explicite ({}), pas « aucune
        décision » (None) : sinon ffmpeg recopie les balises de la source."""
        item = _add_tag_item(panel, "f1", {"ENCODED_BY": "j3rico"})

        item._cb.setChecked(False)

        assert panel.get_global_tag_overrides() == {}

    def test_no_tag_line_at_all_leaves_decision_undefined(self, panel):
        assert panel.get_global_tag_overrides() is None

    def test_partial_uncheck_keeps_checked_source_tags(self, panel):
        item1 = _add_tag_item(panel, "f1", {"ENCODED_BY": "j3rico"})
        _add_tag_item(panel, "f2", {"IMDB": "tt1"})

        item1._cb.setChecked(False)

        assert panel.get_global_tag_overrides() == {"IMDB": "tt1"}
