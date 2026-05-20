"""
tests/test_ui_scale_widgets.py — Smoke tests ciblés pour le scaling UI.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from core.config import AppConfig
from core.workflows.remux_models import TrackEntry
from ui.design_system import DesignSystem


def _mock_qsettings():
    inst = MagicMock()
    inst.value.side_effect = lambda key, default=None: default
    return inst


def _build_config(tmp_path):
    import core.config as cfg_mod

    ini_path = tmp_path / "config.ini"
    ini_path.write_text("[ui]\nui_scale_percent = 100\n", encoding="utf-8")

    with patch("core.config.QSettings") as mock_qs, \
         patch("core.config._app_data_dir", return_value=tmp_path), \
         patch.object(cfg_mod, "_INI_PATH", ini_path):
        mock_qs.return_value = _mock_qsettings()
        return AppConfig()


@pytest.fixture(autouse=True)
def _reset_ui_scale():
    previous = DesignSystem.current_ui_scale()
    DesignSystem.set_ui_scale(100)
    yield
    DesignSystem.set_ui_scale(previous)


@pytest.mark.parametrize("percent", [50, 100, 150, 200])
def test_sidebar_scales_width_with_ui_scale(qt_app, percent):
    from ui.main_window import _Sidebar

    DesignSystem.set_ui_scale(percent)
    sidebar = _Sidebar(compact=False)

    assert sidebar.width() == DesignSystem.scale(sidebar._FULL_WIDTH)
    assert sidebar._toggle_btn.width() > 0


@pytest.mark.parametrize("percent", [50, 100, 150, 200])
def test_log_panel_builds_at_multiple_scales(qt_app, percent):
    from ui.main_window import LogPanel

    DesignSystem.set_ui_scale(percent)
    panel = LogPanel()

    assert panel._collapse_btn.width() > 0
    assert panel._text.font().pointSize() > 0


@pytest.mark.parametrize("percent", [50, 100, 150, 200])
def test_dashboard_page_builds_at_multiple_scales(tmp_path, qt_app, percent):
    from core.logging import LogLevel
    from ui.main_window import DashboardPage

    DesignSystem.set_ui_scale(percent)
    cfg = _build_config(tmp_path)
    page = DashboardPage(cfg, lambda _msg, _level=LogLevel.INFO: None)

    assert page.layout() is not None
    assert page.minimumWidth() >= 0


@pytest.mark.parametrize("percent", [50, 100, 150, 200])
def test_file_inspector_widget_builds_at_multiple_scales(tmp_path, qt_app, percent):
    from ui.file_inspector_widget import FileInspectorWidget

    DesignSystem.set_ui_scale(percent)
    cfg = _build_config(tmp_path)
    widget = FileInspectorWidget(cfg)

    assert widget._drop_zone.height() == DesignSystem.scale(96)
    assert widget._summary.height() == DesignSystem.scale(44)
    assert widget._tabs.count() == 4


@pytest.mark.parametrize("percent", [50, 100, 150, 200])
def test_remux_panel_builds_at_multiple_scales(tmp_path, qt_app, percent):
    from ui.panels.remux_panel import RemuxPanel

    DesignSystem.set_ui_scale(percent)
    cfg = _build_config(tmp_path)
    panel = RemuxPanel(cfg)

    assert panel._file_list.height() > 0
    assert panel._cmd_preview.height() == DesignSystem.scale(120)
    panel.close()


@pytest.mark.parametrize("percent", [50, 100, 150, 200])
def test_merge_dovi_panel_builds_at_multiple_scales(tmp_path, qt_app, percent):
    from ui.panels.merge_dovi_panel import MergeDoviPanel

    DesignSystem.set_ui_scale(percent)
    cfg = _build_config(tmp_path)
    panel = MergeDoviPanel(cfg)

    assert panel._run_btn.height() == DesignSystem.scale(36)
    assert panel._cancel_btn.height() == DesignSystem.scale(30)
    panel.close()


@pytest.mark.parametrize("percent", [50, 100, 150, 200])
def test_encode_file_zone_builds_at_multiple_scales(qt_app, percent):
    from ui.panels.encode_panel.widgets import _FileZone

    DesignSystem.set_ui_scale(percent)
    widget = _FileZone()

    assert widget.minimumHeight() == DesignSystem.scale(72)
    assert widget._icon is not None


@pytest.mark.parametrize("percent", [50, 100, 150, 200])
def test_chapter_panel_builds_at_multiple_scales(qt_app, percent):
    from ui.panels.remux_panel.widgets.chapters import _ChapterPanel

    DesignSystem.set_ui_scale(percent)
    panel = _ChapterPanel()

    assert panel._add_btn.height() == DesignSystem.scale(22)
    assert panel._keep_cb is not None


@pytest.mark.parametrize("percent", [50, 100, 150, 200])
def test_attachment_panel_builds_at_multiple_scales(tmp_path, qt_app, percent):
    from ui.panels.remux_panel.widgets.attachments import _AttachmentPanel

    DesignSystem.set_ui_scale(percent)
    cfg = _build_config(tmp_path)
    panel = _AttachmentPanel(cfg)

    assert panel._placeholder is not None
    assert panel._imdb_btn.height() == DesignSystem.scale(22)


@pytest.mark.parametrize("percent", [50, 100, 150, 200])
def test_tmdb_modal_builds_at_multiple_scales(tmp_path, qt_app, percent):
    from ui.panels.tmdb_search_modal import TmdbSearchModal

    DesignSystem.set_ui_scale(percent)
    cfg = _build_config(tmp_path)
    modal = TmdbSearchModal(cfg, suggested_title="Blade Runner")

    assert modal.minimumWidth() == DesignSystem.scale(560)
    assert modal._search_btn.height() == DesignSystem.scale(28)
    modal.close()


def test_tmdb_modal_series_fields_stay_available_without_results(tmp_path, qt_app):
    from ui.panels.tmdb_search_modal import TmdbSearchModal

    cfg = _build_config(tmp_path)
    modal = TmdbSearchModal(cfg, suggested_season=1, suggested_episode=2)

    assert not modal._series_row.isHidden()

    modal._kind_combo.setCurrentIndex(1)
    assert modal._series_row.isHidden()

    modal._kind_combo.setCurrentIndex(2)
    assert not modal._series_row.isHidden()

    modal._on_results([])
    assert not modal._series_row.isHidden()
    modal.close()


@pytest.mark.parametrize("percent", [50, 100, 150, 200])
def test_track_edit_dialog_builds_at_multiple_scales(qt_app, percent):
    from ui.panels.track_edit_dialog import TrackEditDialog

    DesignSystem.set_ui_scale(percent)
    entry = TrackEntry(
        mkv_tid=1,
        track_type="audio",
        codec="AAC",
        display_info="2.0  192 kb/s",
        language="eng",
        title="Main",
    )
    dlg = TrackEditDialog(entry)

    assert dlg.minimumWidth() == DesignSystem.scale(440)
    assert dlg._lang_edit.maximumWidth() == DesignSystem.scale(120)
    dlg.close()
