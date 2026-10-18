"""HDR Studio : rendu, chargement asynchrone, analyse, menus « Inspecter le HDR… », navigation."""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from PySide6.QtWidgets import QCheckBox

import ui.panels.hdr_studio.panel as panel_module
from core.config import AppConfig
from core.hdr_inspection import InspectionCancelled, InspectionReport, audit, build_snapshot
from core.i18n import current_language, set_current_language
from core.workflows.remux_models import TrackEntry
from tests.test_hdr_inspection import _analyses, _info
from ui.panels.hdr_studio import HdrStudio
from ui.panels.hdr_studio.chart import ChartMarker, ChartSeries, LuminanceChart, bucket, format_time


@pytest.fixture(autouse=True)
def _french():
    previous = current_language()
    set_current_language("fra")
    yield
    set_current_language(previous)


def _wait(qt_app, predicate, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        qt_app.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return False


@pytest.fixture
def studio(qt_app, tmp_path):
    config = AppConfig()
    config.app_data_dir = tmp_path / "appdata"
    widget = HdrStudio(config)
    yield widget
    widget.close()
    _wait(qt_app, lambda: not widget.isVisible() and getattr(widget, "_shutdown", None) is not None
          and widget._shutdown.done.is_set(), 2.0)


def _media(tmp_path) -> Path:
    path = tmp_path / "film.mkv"
    path.write_bytes(b"\x1a\x45\xdf\xa3")
    return path


class _FakeInspector:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def inspect(self, path):
        return _info(Path(path))


def test_bucket_and_time_format():
    import numpy as np
    values = np.array([1, 5, 2, 8, 3, 3], dtype=np.float32)
    assert bucket(values, 0, 6, 3, "max").tolist() == [5, 8, 3]
    assert bucket(values, 0, 6, 3, "mean").tolist() == [3, 5, 3]
    assert bucket(values, 2, 4, 10, "max").tolist() == [2, 8]
    assert format_time(59) == "0:59" and format_time(3725) == "1:02:05"


def test_chart_draws_series_and_zooms(qt_app):
    import numpy as np
    chart = LuminanceChart()
    chart.resize(600, 300)
    values = np.linspace(1, 1500, 5000).astype(np.float32)
    chart.set_data(frames=5000, fps=24.0, series=[ChartSeries("a", "A", "#ff0000", values, fill=True)],
                   markers=[ChartMarker("MaxCLL", 1000, "#00ff00")])
    chart.grab()  # dessin complet sans erreur
    assert chart.view() == (0, 5000)
    assert "image 2500" in chart.hover_text(2500)
    chart.set_series_visible("a", False)
    assert not chart.series()[0].visible
    chart.clear()
    chart.grab()


def test_render_report_tabs(studio, tmp_path):
    dovi, hdr10plus = _analyses(tmp_path, el_type="FEL", residual_off=False)
    snapshot = build_snapshot(_info(tmp_path / "a.mkv"))
    report = InspectionReport(snapshot, dovi, hdr10plus, audit(snapshot, dovi, hdr10plus), dynamic=True)
    studio._render(report)
    assert studio.report() is report
    assert [s.key for s in studio._chart.series()] == ["l1_max", "l1_avg", "maxscl", "avgrgb"]
    # Avec Dolby Vision, les courbes HDR10+ sont masquées par défaut.
    assert [s.visible for s in studio._chart.series()] == [True, True, False, False]
    assert len(studio._luminance_tab.findChildren(QCheckBox)) == 4
    assert studio._overview.count() > 3 and studio._dovi_tab.count() > 2 and studio._hdr10plus_tab.count() > 1
    studio._render(None)
    assert studio._chart.series() == []


def test_load_source_then_analyze(studio, qt_app, tmp_path, monkeypatch):
    monkeypatch.setattr(panel_module, "FileInspector", _FakeInspector)
    dovi, hdr10plus = _analyses(tmp_path)
    calls: list[tuple[int | None, bool]] = []

    class FakeDynamic:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        def analyze(self, info, stream, *, refresh=False):
            calls.append((stream, refresh))
            self.kwargs["on_step"]("Extraction", 0.0, 0.5)
            snapshot = build_snapshot(info, stream)
            return InspectionReport(snapshot, dovi, hdr10plus, audit(snapshot, dovi, hdr10plus), dynamic=True)

    monkeypatch.setattr(panel_module, "DynamicInspector", FakeDynamic)
    media = _media(tmp_path)
    studio.load_source(media, 0)
    assert _wait(qt_app, lambda: studio._info is not None)
    assert studio._track_combo.count() == 1 and studio.report() is not None and not studio.report().dynamic
    assert studio._analyze_btn.isEnabled()
    studio.analyze()
    assert _wait(qt_app, lambda: studio.report() is not None and studio.report().dynamic)
    assert calls == [(0, False)] and not studio.is_busy()
    assert not studio._analyze_btn.isVisibleTo(studio)


def test_load_source_with_auto_analyze_and_cache(studio, qt_app, tmp_path, monkeypatch):
    monkeypatch.setattr(panel_module, "FileInspector", _FakeInspector)
    dovi, hdr10plus = _analyses(tmp_path)
    media = _media(tmp_path)
    studio._cache.store(media, 0, dovi, hdr10plus)

    class Unused:
        def __init__(self, **kwargs) -> None:
            raise AssertionError("analyse relancée malgré le cache")

    monkeypatch.setattr(panel_module, "DynamicInspector", Unused)
    studio.load_source(media, 0, analyze=True)
    assert _wait(qt_app, lambda: studio.report() is not None and studio.report().from_cache)
    assert studio._refresh_btn.isVisibleTo(studio)


def test_analyze_cancel_and_error(studio, qt_app, tmp_path, monkeypatch):
    monkeypatch.setattr(panel_module, "FileInspector", _FakeInspector)
    outcome = {"raise": InspectionCancelled()}

    class FakeDynamic:
        def __init__(self, **kwargs) -> None:
            pass

        def analyze(self, info, stream, *, refresh=False):
            raise outcome["raise"]

    monkeypatch.setattr(panel_module, "DynamicInspector", FakeDynamic)
    studio.load_source(_media(tmp_path), 0)
    assert _wait(qt_app, lambda: studio._info is not None)
    messages: list[tuple[str, str]] = []
    studio.log_message.connect(lambda level, text: messages.append((level, text)))
    studio.analyze()
    assert _wait(qt_app, lambda: not studio.is_busy())
    assert studio._status.text() == "Analyse annulée."
    outcome["raise"] = RuntimeError("dovi_tool absent")
    studio.analyze()
    assert _wait(qt_app, lambda: not studio.is_busy() and "dovi_tool absent" in studio._status.text())
    assert any(level == "ERROR" for level, _ in messages)
    assert studio.report() is not None and not studio.report().dynamic


def test_load_source_rejects_missing_file(studio, tmp_path):
    studio.load_source(tmp_path / "absent.mkv")
    assert "introuvable" in studio._status.text() and studio._info is None


def test_track_table_video_menu_emits(qt_app, monkeypatch):
    from ui.panels.remux_panel.widgets.track_table import _TrackTable

    table = _TrackTable()
    entry = TrackEntry(mkv_tid=0, track_type="video", codec="HEVC", display_info="", language="und", title="")
    table.append_tracks("#5588ff", [entry])
    emitted: list[TrackEntry] = []
    table.hdr_inspect_requested.connect(emitted.append)
    import ui.panels.remux_panel.widgets.track_table as track_table_module

    class FakeMenu:
        def __init__(self, *_args) -> None:
            self.labels: list[str] = []

        def addAction(self, label):  # noqa: N802 (API Qt)
            self.labels.append(label)
            return label

        def exec(self, *_args):
            return self.labels[0]

    monkeypatch.setattr(track_table_module, "QMenu", FakeMenu)
    table._on_context_menu(table.visualRect(table.model().index(0, 0)).center())
    assert emitted == [entry]
    table.close()


def test_main_window_opens_hdr_studio(qt_app, tmp_path, monkeypatch):
    from ui.main_window import MainWindow, DashboardPage
    from ui.panels.encode_panel.panel import EncodePanel

    monkeypatch.setattr(DashboardPage, "_start_hw_detection", lambda self: None)
    monkeypatch.setattr(DashboardPage, "start_accel_detection", lambda self: None)
    monkeypatch.setattr(MainWindow, "_wire_extensions", lambda self: None)
    monkeypatch.setattr(EncodePanel, "_detect_hw_encoders", lambda self: None)
    monkeypatch.setattr(panel_module, "FileInspector", _FakeInspector)
    config = AppConfig()
    config.check_updates = False
    config.app_data_dir = tmp_path / "appdata"
    win = MainWindow(config)
    try:
        assert any(b.page_index == 7 for b in win._sidebar._buttons)
        media = _media(tmp_path)
        win._remux_panel.hdr_studio_requested.emit(media, 0)
        assert win._stack.currentIndex() == 7 and win._stack.currentWidget() is win._hdr_panel
        assert _wait(qt_app, lambda: win._hdr_panel._info is not None)
        assert win._hdr_panel._path_edit.text() == str(media)
        assert MainWindow.startup_page_index("hdr") == 7
    finally:
        win._running = False
        win.close()
        _wait(qt_app, lambda: not win.isVisible(), 3.0)


def test_chart_drag_pans_zoomed_view(qt_app):
    import numpy as np
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtCore import QEvent, Qt

    chart = LuminanceChart()
    chart.resize(600, 300)
    chart.set_data(frames=10000, fps=24.0, series=[ChartSeries("a", "A", "#ff0000", np.ones(10000, np.float32))],
                   scenes=np.arange(0, 10000, 500, dtype=np.int32))
    chart._view = (2000, 3000)
    clicked: list[int] = []
    chart.frame_clicked.connect(clicked.append)

    def mouse(kind, x, buttons):
        return QMouseEvent(kind, QPointF(x, 100), QPointF(x, 100), Qt.MouseButton.LeftButton, buttons,
                           Qt.KeyboardModifier.NoModifier)

    chart.mousePressEvent(mouse(QEvent.Type.MouseButtonPress, 300, Qt.MouseButton.LeftButton))
    chart.mouseMoveEvent(mouse(QEvent.Type.MouseMove, 200, Qt.MouseButton.LeftButton))
    chart.mouseReleaseEvent(mouse(QEvent.Type.MouseButtonRelease, 200, Qt.MouseButton.NoButton))
    start, end = chart.view()
    assert end - start == 1000 and start > 2000 and not clicked
    chart.grab()
    chart.mousePressEvent(mouse(QEvent.Type.MouseButtonPress, 300, Qt.MouseButton.LeftButton))
    chart.mouseReleaseEvent(mouse(QEvent.Type.MouseButtonRelease, 300, Qt.MouseButton.NoButton))
    assert len(clicked) == 1
