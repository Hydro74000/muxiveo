"""
tests/test_remux_panel_tags.py — Synchronisation des balises globales du
panneau Conteneur quand une ligne de balises source est (dé)cochée.
"""
from __future__ import annotations

import pytest
from PySide6.QtWidgets import QDialog

from core.media_info_fetcher import MediaDetails
from ui.panels.remux_panel.widgets import attachments
from ui.panels.remux_panel.widgets.attachments import _AttachmentItemWidget, _AttachmentPanel


@pytest.fixture
def panel(qt_app):
    widget = _AttachmentPanel(config=None)
    yield widget
    widget.close()
    widget.deleteLater()


@pytest.fixture
def edit_tags(panel, monkeypatch):
    def edit(tags, *, accepted=True):
        class Dialog:
            def __init__(self, current, parent):
                pass

            def exec(self):
                return QDialog.DialogCode.Accepted if accepted else QDialog.DialogCode.Rejected

            def result_tags(self):
                return dict(tags)

        monkeypatch.setattr(attachments, "_TagEditDialog", Dialog)
        panel._open_global_tag_dialog()

    return edit


def _add_tag_item(panel: _AttachmentPanel, file_id: str, tags: dict[str, str]) -> _AttachmentItemWidget:
    item = _AttachmentItemWidget(file_id=file_id, source_color="#fff", tags=tags, is_tag=True)
    panel._add_item(item)
    return item


class TestPanelTagOverridesSync:
    def test_uncheck_source_tags_removes_them_from_edited_tags(self, panel, edit_tags):
        item = _add_tag_item(panel, "f1", {"ENCODED_BY": "j3rico", "IMDB": "tt1"})
        edit_tags({"ENCODED_BY": "j3rico", "IMDB": "tt1", "GENRE": "Action"})

        item._cb.setChecked(False)

        assert panel.get_global_tag_overrides() == {"GENRE": "Action"}

    def test_uncheck_keeps_tags_still_provided_by_another_checked_source(self, panel, edit_tags):
        item1 = _add_tag_item(panel, "f1", {"IMDB": "tt1"})
        _add_tag_item(panel, "f2", {"IMDB": "tt1"})
        edit_tags({"IMDB": "tt1"})

        item1._cb.setChecked(False)

        assert panel.get_global_tag_overrides() == {"IMDB": "tt1"}

    def test_uncheck_keeps_user_modified_value(self, panel, edit_tags):
        item = _add_tag_item(panel, "f1", {"IMDB": "tt1"})
        edit_tags({"IMDB": "tt-corrigé"})

        item._cb.setChecked(False)

        assert panel.get_global_tag_overrides() == {"IMDB": "tt-corrigé"}

    def test_recheck_restores_source_tags(self, panel, edit_tags):
        item = _add_tag_item(panel, "f1", {"ENCODED_BY": "j3rico"})
        edit_tags({"ENCODED_BY": "j3rico", "GENRE": "Action"})

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

    def test_conflicting_sources_follow_import_priority_after_toggles(self, panel, edit_tags):
        first = _add_tag_item(panel, "f1", {"IMDB": "tt1"})
        second = _add_tag_item(panel, "f2", {"IMDB": "tt2"})
        edit_tags({"IMDB": "tt1", "GENRE": "Action"})
        observed = []
        panel.changed.connect(lambda: observed.append(dict(panel.get_global_tag_overrides())))

        first._cb.setChecked(False)
        second._cb.setChecked(False)
        second._cb.setChecked(True)
        first._cb.setChecked(True)

        assert observed == [
            {"IMDB": "tt2", "GENRE": "Action"},
            {"GENRE": "Action"},
            {"IMDB": "tt2", "GENRE": "Action"},
            {"IMDB": "tt1", "GENRE": "Action"},
        ]

    def test_manual_value_matching_other_source_remains_explicit(self, panel, edit_tags):
        first = _add_tag_item(panel, "f1", {"IMDB": "tt1"})
        second = _add_tag_item(panel, "f2", {"IMDB": "tt2"})
        edit_tags({"IMDB": "tt2"})

        first._cb.setChecked(False)
        # Une seconde validation sans changement ne doit pas perdre l'édition.
        edit_tags({"IMDB": "tt2"})
        second._cb.setChecked(False)
        first._cb.setChecked(True)

        assert panel.get_global_tag_overrides() == {"IMDB": "tt2"}

    @pytest.mark.parametrize("edited", [{"IMDB": "tt1"}, {}])
    def test_recheck_preserves_manual_deletions(self, panel, edit_tags, edited):
        item = _add_tag_item(panel, "f1", {"IMDB": "tt1", "ENCODED_BY": "source"})
        edit_tags(edited)

        item._cb.setChecked(False)
        edit_tags(panel.get_global_tag_overrides())
        item._cb.setChecked(True)

        assert panel.get_global_tag_overrides() == edited

    def test_deleted_key_stays_deleted_when_another_source_is_checked(self, panel, edit_tags):
        _add_tag_item(panel, "f1", {"IMDB": "tt1"})
        second = _add_tag_item(panel, "f2", {"IMDB": "tt2"})
        second._cb.setChecked(False)
        edit_tags({})

        second._cb.setChecked(True)

        assert panel.get_global_tag_overrides() == {}

    def test_manually_readding_original_tag_restores_inheritance(self, panel, edit_tags):
        item = _add_tag_item(panel, "f1", {"IMDB": "tt1"})
        edit_tags({})
        edit_tags({"IMDB": "tt1"})

        item._cb.setChecked(False)

        assert panel.get_global_tag_overrides() == {}
        item._cb.setChecked(True)
        assert panel.get_global_tag_overrides() == {"IMDB": "tt1"}

    def test_restoring_source_value_discards_manual_override(self, panel, edit_tags):
        first = _add_tag_item(panel, "f1", {"IMDB": "tt1"})
        second = _add_tag_item(panel, "f2", {"IMDB": "tt2"})
        edit_tags({"IMDB": "tt-corrected", "GENRE": "Action"})
        edit_tags({"IMDB": "tt1", "GENRE": "Action"})

        first._cb.setChecked(False)
        assert panel.get_global_tag_overrides() == {"IMDB": "tt2", "GENRE": "Action"}
        second._cb.setChecked(False)
        assert panel.get_global_tag_overrides() == {"GENRE": "Action"}

    def test_unchanged_tmdb_values_remain_explicit_after_editor_acceptance(self, panel, edit_tags):
        item = _add_tag_item(panel, "f1", {"GENRE": "Action"})
        panel._apply_tmdb_details(MediaDetails(genre="Action"), open_editor=False)
        edited = dict(panel.get_global_tag_overrides())
        edit_tags(edited)

        item._cb.setChecked(False)

        assert panel.get_global_tag_overrides() == edited

    def test_cancelled_edit_does_not_suppress_source_tags(self, panel, edit_tags):
        item = _add_tag_item(panel, "f1", {"IMDB": "tt1"})
        edit_tags({}, accepted=False)
        item._cb.setChecked(False)
        item._cb.setChecked(True)

        assert panel.get_global_tag_overrides() == {"IMDB": "tt1"}

    def test_tmdb_values_matching_source_survive_uncheck(self, panel):
        item = _add_tag_item(panel, "f1", {"GENRE": "Action", "ENCODED_BY": "source"})
        panel._apply_tmdb_details(MediaDetails(genre="Action"), open_editor=False)

        item._cb.setChecked(False)

        assert panel.get_global_tag_overrides() == {
            "GENRE": "Action", "COMMENTS": panel._tmdb_comment_value(),
        }

    def test_tmdb_editor_deletions_survive_recheck(self, panel, edit_tags):
        item = _add_tag_item(panel, "f1", {"GENRE": "Action"})
        edit_tags({})  # Installe aussi le faux dialogue pour l'édition TMDB.
        panel._apply_tmdb_details(MediaDetails(genre="Drama"))

        item._cb.setChecked(False)
        item._cb.setChecked(True)

        assert panel.get_global_tag_overrides() == {}

    def test_new_source_merges_without_restoring_deleted_tags(self, panel, edit_tags):
        _add_tag_item(panel, "f1", {"IMDB": "tt1", "ENCODED_BY": "source"})
        edit_tags({"IMDB": "tt1", "GENRE": "Action"})

        panel.add_source_tags("f2", "#fff", {"ENCODED_BY": "other", "DIRECTOR": "Name"})

        assert panel.get_global_tag_overrides() == {
            "IMDB": "tt1", "GENRE": "Action", "DIRECTOR": "Name",
        }

    def test_remove_source_preserves_edits_and_remerges_inherited_tags(self, panel, edit_tags):
        _add_tag_item(panel, "f1", {"IMDB": "tt1", "ENCODED_BY": "source"})
        second = _add_tag_item(panel, "f2", {"IMDB": "tt2", "ENCODED_BY": "other"})
        edit_tags({"IMDB": "tt1", "GENRE": "Action"})
        observed = []
        panel.changed.connect(lambda: observed.append(dict(panel.get_global_tag_overrides())))

        panel.remove_by_file_id("f1")

        assert observed == [{"IMDB": "tt2", "GENRE": "Action"}]
        second._cb.setChecked(False)
        second._cb.setChecked(True)
        assert panel.get_global_tag_overrides() == {"IMDB": "tt2", "GENRE": "Action"}

    def test_remove_last_source_preserves_tmdb_and_manual_values(self, panel, edit_tags):
        _add_tag_item(panel, "f1", {"IMDB": "tt1", "GENRE": "Action", "ENCODED_BY": "source"})
        panel._apply_tmdb_details(MediaDetails(genre="Action"), open_editor=False)
        edit_tags({**panel.get_global_tag_overrides(), "IMDB": "tt-corrected"})

        panel.remove_by_file_id("f1")

        expected = {"IMDB": "tt-corrected", "GENRE": "Action", "COMMENTS": panel._tmdb_comment_value()}
        assert panel.get_global_tag_overrides() == expected
        panel.add_source_tags("f2", "#fff", {"IMDB": "tt2", "GENRE": "Drama"})
        assert panel.get_global_tag_overrides() == expected

    def test_remove_last_source_preserves_manual_deletions(self, panel, edit_tags):
        _add_tag_item(panel, "f1", {"IMDB": "tt1"})
        edit_tags({})

        panel.remove_by_file_id("f1")
        panel.add_source_tags("f2", "#fff", {"IMDB": "tt2"})

        assert panel.get_global_tag_overrides() == {}

    def test_remove_attachment_only_source_preserves_tags(self, panel, edit_tags):
        _add_tag_item(panel, "f1", {"IMDB": "tt1"})
        panel._add_item(_AttachmentItemWidget(file_id="f2", source_color="#fff"))
        edit_tags({"IMDB": "tt-corrected"})

        panel.remove_by_file_id("f2")

        assert panel.get_global_tag_overrides() == {"IMDB": "tt-corrected"}

    def test_clear_all_discards_previous_edits(self, panel, edit_tags):
        _add_tag_item(panel, "f1", {"IMDB": "tt1"})
        edit_tags({"GENRE": "Action"})
        panel.clear_all()
        item = _add_tag_item(panel, "f2", {"IMDB": "tt2"})
        edit_tags({"IMDB": "tt2"})
        item._cb.setChecked(False)
        item._cb.setChecked(True)

        assert panel.get_global_tag_overrides() == {"IMDB": "tt2"}
