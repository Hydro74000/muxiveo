"""Insertion de champs valides dans le nom de sortie et partage avec les profils."""
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, QTimer, Qt
from PySide6.QtGui import QContextMenuEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from cli.output_template import build_output_context, render_output_template
from core.media_info_fetcher import MediaDetails
from core.workflows.remux_models import TrackEntry
from ui.panels.remux_panel.profile_editor import KeywordLineEdit
from ui.widgets.output_template_edit import OutputTemplateLineEdit, OutputTemplatePreview, output_template_choices


def _token_actions(menu):
    actions = {}
    for action in menu.actions():
        if action.menu():
            actions.update(_token_actions(action.menu()))
        elif isinstance(action.data(), str):
            actions[action.data()] = action
    return actions


@pytest.fixture
def field(qt_app):
    edit = OutputTemplateLineEdit(hybrid=True)
    yield edit
    edit.close()


def test_output_menu_offers_naming_tokens_and_describes_them(field):
    menu = field._context_menu()
    try:
        actions = _token_actions(menu)
        assert {
            "{title:release}", "{season_episode}", "{episode_title:release}", "{audio-multi}",
            "{audio-fr-tag}", "{audio-codec-release:best}", "{audio-channels:best}",
            "{audio-immersive}", "WEB-DL", "{hybrid_tag}", "{video-hdr:best}",
            "{video-dolby-vision}", "{video-codec:best}", "{ref_episode_title}",
            "{audio-codec-release:first}", "{audio-codec-release:all}", "MULTI", "ATMOS",
        } <= actions.keys()
        assert not {"{lang}", "{codec}", "{flag_forced}", "{none}", "AND", "NOT()"} & actions.keys()
        for token, action in actions.items():
            assert token in action.text()
            assert action.toolTip() and action.statusTip() == action.toolTip()
        assert "\t{title:release}" in actions["{title:release}"].text()
    finally:
        menu.deleteLater()


def test_right_click_insertion_preserves_selection_cursor_and_undo(field, monkeypatch):
    field.setText("Show.ancien.mkv")
    field.setSelection(5, len("ancien"))
    changes = []
    field.textChanged.connect(changes.append)

    menu = field._context_menu()
    monkeypatch.setattr(field, "_context_menu", lambda: menu)

    def choose():
        try:
            assert any(action.isEnabled() and not action.menu() for action in menu.actions())
            _token_actions(menu)["{title:release}"].trigger()
        finally:
            menu.close()

    QTimer.singleShot(0, choose)
    event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, QPoint(10, 10), QPoint(10, 10))
    QApplication.sendEvent(field, event)
    assert field.text() == "Show.{title:release}.mkv"
    assert field.cursorPosition() == len("Show.{title:release}")
    assert changes == [field.text()]
    field.undo()
    assert field.text() == "Show.ancien.mkv"


def test_profile_keeps_its_keywords_and_operands_while_output_remains_plain_text(qt_app, field):
    profile = KeywordLineEdit()
    menu = profile._context_menu()
    try:
        _token_actions(menu)["{codec_atmos}"].trigger()
        assert profile.text() == "{codec_atmos}"
        profile.setFocus()
        QTest.keyClick(profile, Qt.Key.Key_Plus, Qt.KeyboardModifier.ControlModifier)
        assert profile.text().endswith(" & ")
        QTest.keyClick(field, Qt.Key.Key_Plus, Qt.KeyboardModifier.ControlModifier)
        assert field.text() == ""
        field.setReadOnly(True)
        readonly_menu = field._context_menu()
        assert not _token_actions(readonly_menu)
        readonly_menu.deleteLater()
    finally:
        menu.deleteLater()
        profile.close()


def test_menu_builds_the_expected_release_name_with_the_real_renderer(field):
    tracks = [
        TrackEntry(0, "video", "HEVC", "3840x2160 HDR10 Dolby Vision 10-bit", "und", "", file_id="src0"),
        TrackEntry(1, "audio", "EAC3", "5.1 Atmos", "en-US", "VO", file_id="src0"),
        TrackEntry(0, "audio", "EAC3", "5.1", "fr-FR", "VF", file_id="src1"),
        TrackEntry(1, "subtitle", "SUBRIP", "", "fr-FR", "Complets", file_id="src1"),
    ]
    context = build_output_context(
        Path("Crystal.Lake.S01E02.WEB-DL-GRP.mkv"),
        MediaDetails(title="Crystal Lake", year="2026", season="1", episode="2", episode_title="Le séminaire"),
        tracks=tracks, track_order=[(0, 0), (1, 0), (0, 1), (1, 1)],
    )
    context["ref_episode_title"] = "Part.2"
    menu = field._context_menu()
    try:
        actions = _token_actions(menu)
        tokens = ["{title:release}", "{season_episode}", "{episode_title:release}", "{audio-multi}",
                  "{audio-fr-tag}", "{audio-codec-release:best}", "{audio-channels:best}",
                  "{audio-immersive}", "WEB-DL", "{hybrid_tag}", "{video-hdr:best}",
                  "{video-dolby-vision}", "{video-codec:best}"]
        for index, token in enumerate(tokens):
            if index:
                actions["."].trigger()
            actions[token].trigger()
        assert render_output_template(field.text(), context) == (
            "Crystal.Lake.S01E02.Le.seminaire.MULTi.VFF.DDP.5.1.Atmos.WEB-DL.Hybrid.HDR.DV.HEVC.mkv"
        )
        for choice in output_template_choices(hybrid=True):
            # VOSTFR reste volontairement vide avec une piste audio française.
            if choice.text.startswith("{") and choice.text != "{sub-vostfr}":
                assert render_output_template(choice.text, context) != ".mkv", choice.text
        assert "{ref_episode_title}" not in {choice.text for choice in output_template_choices()}
    finally:
        menu.deleteLater()


def test_hybrid_template_uses_the_shared_widget_and_updates_its_plan(qt_app):
    from core.config import AppConfig
    from ui.panels.hybrid_studio import HybridStudio

    panel = HybridStudio(AppConfig())
    try:
        edit = panel.controls["output-template"]
        assert isinstance(edit, OutputTemplateLineEdit)
        before = panel._settings_signature()
        menu = edit._context_menu()
        _token_actions(menu)["{title:release}"].trigger()
        assert panel._settings_signature() != before
        menu.deleteLater()
    finally:
        panel.close()


def test_live_preview_handles_incomplete_templates_and_is_selectable(field):
    preview = OutputTemplatePreview(field)
    try:
        preview.set_context({"title": "Été", "season_num": 1}, default_name="source.Hybrid.mkv", episode="S01E01")
        assert preview.name.text() == "source.Hybrid.mkv"
        assert preview.name.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse
        assert preview.name.textFormat() == Qt.TextFormat.PlainText
        field.setText("{title:release}.S{season_num:02d}")
        assert preview.name.text() == "Ete.S01.mkv"
        for invalid in ("{title", "../{title}", "/absolute/{title}"):
            field.setText(invalid)
            assert "mkv" not in preview.name.text()
            assert preview.name.text() != "Ete.S01.mkv"
        field.setText("{title:release}")
        assert preview.name.text() == "Ete.mkv"
        field.clear()
        assert preview.name.text() == "source.Hybrid.mkv"
    finally:
        preview.close()
