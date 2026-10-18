"""HDR Studio : rendu, chargement asynchrone, analyse, menus « Inspecter le HDR… », navigation."""
from __future__ import annotations

import dataclasses
import time
from pathlib import Path

import numpy as np

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
    chart = studio._luminance.chart
    assert [s.key for s in chart.series()] == ["l1_max", "l1_avg", "maxscl", "avgrgb", "displayed"]
    # Avec Dolby Vision, les courbes HDR10+ sont masquées par défaut.
    assert [s.visible for s in chart.series()] == [True, True, False, False, True]
    assert len(studio._luminance.findChildren(QCheckBox)) == 5
    assert studio._overview.count() > 3 and studio._dovi_tab.count() > 2 and studio._hdr10plus_tab.count() > 1
    studio._render(None)
    assert chart.series() == []


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



# ── Lot 2 : aperçu, mesure, édition ──────────────────────────────────────────
def _dynamic_report(tmp_path):
    dovi, hdr10plus = _analyses(tmp_path, max_pq=3300)
    snapshot = build_snapshot(_info(tmp_path / "a.mkv"))
    return InspectionReport(snapshot, dovi, hdr10plus, audit(snapshot, dovi, hdr10plus), dynamic=True)


def test_luminance_preview_follows_values(studio, qt_app, tmp_path):
    report = _dynamic_report(tmp_path)
    studio._render(report)
    tab = studio._luminance
    assert tab.values() == (1000, 400)
    tab.display_peak.setValue(300)
    tab.set_values(200, 100)
    tab._update_preview()
    labels = [m.label for m in tab.chart._markers]
    assert "MaxCLL choisie" in labels and "MaxCLL du fichier" in labels and "Écran" in labels
    assert tab._risk_lines[0].text().startswith("Écrêtage")
    assert "Valeurs du fichier (1000/400)" in tab._original_line.text()
    displayed = next(s for s in tab.chart.series() if s.key == "displayed")
    assert np.nanmax(displayed.values) <= 300 + 1e-3
    assert tab.chart._bands is not None and tab.chart._bands.any()
    assert studio._config.display_peak_nits == 300
    tab.reset_values()
    assert tab.values() == (1000, 400)
    tab.apply_suggestion()
    assert tab.values()[0] == 400  # pic HDR10+ (hors saturation)
    emitted: list[tuple] = []
    studio.apply_to_encode_requested.connect(lambda *args: emitted.append(args))
    tab.encode_btn.click()
    assert emitted and emitted[0][1:] == (0, 400, 38)


class _FakeEngine:
    capabilities = {"measure": ["base", "reconstructed"]}

    def __init__(self, peaks):
        self.peaks = peaks

    def measure(self, path, stream, statistics, *, layer, progress=None, cancelled=None, **_kwargs):
        from tests.test_hdr_edit import write_statistics

        write_statistics(statistics, self.peaks, scenes=[0, 5])
        if progress:
            progress(len(self.peaks))
        return "fake"


def test_measure_flow_updates_report(studio, qt_app, tmp_path, monkeypatch):
    monkeypatch.setattr(panel_module, "FileInspector", _FakeInspector)
    media = _media(tmp_path)
    studio.load_source(media, 0)
    assert _wait(qt_app, lambda: studio._info is not None)
    studio._engine_factory = lambda: _FakeEngine([100.0] * 5 + [2500.0] * 5)
    studio.measure()
    assert _wait(qt_app, lambda: studio._measurement is not None and not studio.is_busy())
    assert studio._measurement.max_cll() == 2500
    assert [s.key for s in studio._luminance.chart.series()][:2] == ["peak", "average"]
    assert any("sous la valeur mesurée (2500 nits)" in f.title for f in studio.report().findings)
    assert studio._cache.load_measurement(media, 0) is not None
    # Extension trop ancienne : message, aucune tâche.
    class Old:
        capabilities: dict = {}
    studio._engine_factory = Old
    studio.measure()
    assert "0.4.0" in studio._status.text()


def test_measure_refuses_profile_5(studio, qt_app, tmp_path, monkeypatch):
    from tests.test_hdr_inspection import _DOVI_RECORD

    class P5Inspector(_FakeInspector):
        def inspect(self, path):
            return _info(Path(path), side_data=[dict(_DOVI_RECORD, dv_profile=5, dv_bl_signal_compatibility_id=0)])

    monkeypatch.setattr(panel_module, "FileInspector", P5Inspector)
    studio.load_source(_media(tmp_path), 0)
    assert _wait(qt_app, lambda: studio._info is not None)
    studio.measure()
    assert "Profil 5" in studio._status.text() and not studio.is_busy()


def test_edition_plan_and_write(studio, qt_app, tmp_path, monkeypatch):
    monkeypatch.setattr(panel_module, "FileInspector", _FakeInspector)
    media = _media(tmp_path)
    studio.load_source(media, 0)
    assert _wait(qt_app, lambda: studio._info is not None)
    report = _dynamic_report(tmp_path)
    studio._render(dataclasses.replace(report, snapshot=dataclasses.replace(report.snapshot, path=str(media))))
    studio._luminance.set_values(700, 200)
    studio._luminance.prepare_patch_requested.emit(700, 200)
    editor = studio._editor
    assert editor.static_box.isChecked() and editor.output.text().endswith("film.hdrfix.mkv")
    assert studio._tabs.currentIndex() == studio._tabs.count() - 1
    editor._copy_static_to_l6()
    editor.l5_box.setChecked(True)
    editor.h10_remove.setChecked(True)
    assert not editor.h10_target_box.isEnabled()
    plan = editor.plan()
    assert plan.static.max_cll == 700 and plan.rpu.l6[2:] == (700, 200) and plan.rpu.active_area == (0, 0, 280, 280)
    assert plan.hdr10plus.remove and "HDR10+ retiré" in editor._summary.text()
    written: list = []

    class FakePatcher:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def run(self, plan):
            written.append(plan)
            plan.output.write_bytes(b"ok")
            return plan.output

    monkeypatch.setattr(panel_module, "MetadataPatcher", FakePatcher)
    editor.write_btn.click()
    assert _wait(qt_app, lambda: not studio.is_busy() and studio._last_output is not None)
    assert written and studio._open_output_btn.isVisibleTo(studio)
    editor.output.setText(str(media))
    assert not editor.write_btn.isEnabled() and "autre fichier" in editor._summary.text()


def test_edition_mastering_display_sync(studio, qt_app, tmp_path, monkeypatch):
    monkeypatch.setattr(panel_module, "FileInspector", _FakeInspector)
    media = _media(tmp_path)
    studio.load_source(media, 0)
    assert _wait(qt_app, lambda: studio._info is not None)
    report = _dynamic_report(tmp_path)
    report = dataclasses.replace(report, snapshot=dataclasses.replace(report.snapshot, path=str(media)))
    studio._render(report)
    editor = studio._editor
    assert editor.mastering_sync.isChecked() and editor.mastering_sync.isVisibleTo(editor)
    assert editor._suggested is not None and editor._suggested[1] == "l6"
    assert "d'après L6 du RPU" in editor._mdcv_hint.text()
    # Mastering modifié dans le MDCV : L6 et écran du RPU suivent, cases cochées.
    editor.mdcv_box.setChecked(True)
    editor.mdcv_max.setValue(4000)
    editor.mdcv_min.setValue(0.005)
    assert editor.l6_box.isChecked() and editor.source_box.isChecked()
    assert editor.l6_max_mastering.value() == 4000 and editor.source_max.value() == 4000
    assert editor.l6_min_mastering.value() == pytest.approx(0.005)
    # Aperçu Luminance : modèle « suit le MDCV » sur la valeur choisie.
    assert studio._luminance._mdcv_choice == 4000
    assert "MDCV choisi" in [m.label for m in studio._luminance._markers()]
    plan = editor.plan()
    assert plan.static.mastering.max_nits == 4000 and plan.static.light_levels is None
    assert plan.rpu.l6[:2] == (4000, pytest.approx(0.005)) and plan.rpu.source_display == (pytest.approx(0.005), 4000.0)
    assert "MDCV" in editor._summary.text()
    # Et dans l'autre sens, depuis le L6.
    editor.l6_max_mastering.setValue(1000)
    assert editor.mdcv_max.value() == 1000 and editor.source_max.value() == 1000
    # Nouveau rendu du même fichier (mesure, cache) : valeurs saisies conservées.
    studio._render(report)
    assert editor.mdcv_max.value() == 1000 and editor.mdcv_box.isChecked()
    # Sans synchronisation : rien n'est recopié.
    editor.mastering_sync.setChecked(False)
    editor.mdcv_max.setValue(2000)
    assert editor.l6_max_mastering.value() == 1000
    editor.apply_suggested_mastering()
    assert editor.mdcv_max.value() == 1000 and editor.mdcv_min.value() == pytest.approx(0.005)
    # Écran invalide : écriture refusée avec le motif.
    editor.mdcv_min.setValue(20)
    editor.mdcv_max.setValue(10)
    assert not editor.write_btn.isEnabled() and "Écran de mastering invalide" in editor._summary.text()


def test_encode_panel_applies_light_levels(qt_app, monkeypatch):
    from tests.test_encode_panel_dovi_geometry import _COLOR, _PATH_A, _file_info, _video_entry
    from ui.panels.encode_panel.panel import EncodePanel

    monkeypatch.setattr(EncodePanel, "_detect_hw_encoders", lambda self: None)
    monkeypatch.setattr(EncodePanel, "_rebuild_preview", lambda self: None)
    monkeypatch.setattr(EncodePanel, "_schedule_static_hdr_estimate", lambda *a, **k: None)
    panel = EncodePanel(AppConfig())
    try:
        panel.set_video_tracks([(_file_info(_PATH_A), _video_entry(), _COLOR)])
        panel._set_combo_data(panel._codec_combo, "libx265")
        assert not panel.apply_static_light_levels(Path("/autre.mkv"), 0, 700, 200)
        assert panel.apply_static_light_levels(_PATH_A, 0, 700, 200)
        assert panel._inject_hdr_cb.isChecked() and panel._max_cll.text() == "700,200"
    finally:
        panel.close()
