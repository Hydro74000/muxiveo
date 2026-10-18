"""HDR Studio, lot 3 : FEL Analyzer (couches, mesures, verdict, survie, onglet FEL)."""
from __future__ import annotations

import dataclasses
import shutil
import subprocess
import threading
import time
from pathlib import Path

import numpy as np
import pytest

import core.hdr_inspection.fel_layer as fel_layer
from core.hdr_inspection.fel_analyzer import (
    LEVELS,
    FelReport,
    FelSample,
    Frame,
    active_rows,
    analyse,
    classify,
    difference_map,
    encode_round_trip,
    frame_metrics,
    pooled_jnd,
    recommendation,
    sample_times,
    summarise,
    survival,
    survival_indices,
    thumbnail,
)
from core.hdr_inspection.fel_layer import LayerFacts, scan_layers, sps_size
from core.i18n import current_language, set_current_language


@pytest.fixture(autouse=True)
def _french():
    previous = current_language()
    set_current_language("fra")
    yield
    set_current_language(previous)


# ── SPS de l'EL ──────────────────────────────────────────────────────────────
class _Writer:
    def __init__(self) -> None:
        self.bits: list[int] = []

    def put(self, value: int, count: int) -> None:
        self.bits += [(value >> (count - 1 - i)) & 1 for i in range(count)]

    def ue(self, value: int) -> None:
        code = value + 1
        length = code.bit_length()
        self.put(0, length - 1)
        self.put(code, length)

    def payload(self) -> bytes:
        bits = self.bits + [1] + [0] * ((8 - (len(self.bits) + 1) % 8) % 8)
        return bytes(int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8))


def _sps(width: int, height: int, window: tuple[int, int, int, int] | None = None, sub_layers: int = 0) -> bytes:
    writer = _Writer()
    writer.put(0, 4)
    writer.put(sub_layers, 3)
    writer.put(1, 1)
    writer.put(0, 96)
    for _ in range(sub_layers):
        writer.put(0, 2)
    if sub_layers:
        writer.put(0, 2 * (8 - sub_layers))
    writer.ue(0)
    writer.ue(1)  # 4:2:0
    writer.ue(width)
    writer.ue(height)
    writer.put(1 if window else 0, 1)
    for value in window or ():
        writer.ue(value)
    return bytes([33 << 1, 1]) + writer.payload()


def test_sps_size_with_conformance_window():
    assert sps_size(_sps(1920, 1080)) == (1920, 1080)
    assert sps_size(_sps(1920, 1088, window=(0, 0, 0, 4))) == (1920, 1080)
    assert sps_size(_sps(3840, 2160, sub_layers=1)) == (3840, 2160)
    assert sps_size(b"\x42\x01") is None


def test_scan_layers_counts_nal_kinds(monkeypatch, tmp_path):
    def nal(kind: int, size: int, body: bytes = b"") -> bytes:
        payload = bytes([kind << 1, 1]) + body + b"\x00" * max(0, size - 2 - len(body))
        return len(payload).to_bytes(4, "big") + payload

    sps = _sps(1920, 1080)
    blocks = [nal(1, 100) + nal(63, 0, sps) + nal(62, 20), nal(1, 80) + nal(63, 30) + nal(62, 20)]

    class Track:
        number, track_type = 1, 1
        codec_private = bytes([1] + [0] * 20 + [3]) + b"\x00"

    class Block:
        def __init__(self, payload: bytes) -> None:
            self.payload = payload

    class Reader:
        def __init__(self, _path) -> None:
            pass

        def segment(self) -> None:
            return None

        def tracks(self):
            return [Track()]

        def segment_duration_ns(self) -> int:
            return 2_000_000_000

        def blocks(self, track_numbers=None):
            return iter(Block(b) for b in blocks)

    import core.matroska.reader as reader_module
    monkeypatch.setattr(reader_module, "MatroskaReader", Reader)
    media = tmp_path / "x.mkv"
    media.write_bytes(b"\x00" * 10)
    facts = scan_layers(media, 0)
    assert facts.frames == 2 and facts.el_size == (1920, 1080)
    assert facts.bl_bytes == (104 + 84) and facts.rpu_bytes == 48
    assert facts.el_bytes == (4 + 2 + len(sps)) + 34 and 0 < facts.el_share < 1
    assert facts.bitrate(1000) == pytest.approx(4000)
    with pytest.raises(ValueError):
        scan_layers(media, 3)


# ── Mesures ──────────────────────────────────────────────────────────────────
def _frame(y: np.ndarray, cb: float = 512.0, cr: float = 512.0) -> Frame:
    h, w = y.shape
    return Frame(y.astype(np.uint16), np.full((h // 2, w // 2), cb, np.uint16), np.full((h // 2, w // 2), cr, np.uint16))


def _base(h: int = 64, w: int = 96, value: float = 400.0) -> np.ndarray:
    rng = np.random.default_rng(1)
    return np.clip(value + rng.normal(0, 2, (h, w)), 64, 940)


def test_frame_metrics_categories():
    m = _base()
    rows = slice(0, 64)
    tone = frame_metrics(_frame(m + 10), _frame(m), rows)
    assert tone["tone"] == pytest.approx(10, abs=0.2) and tone["detail"] < 0.2 and tone["colour"] == 0
    rng = np.random.default_rng(2)
    detail = frame_metrics(_frame(m + rng.normal(0, 3, m.shape)), _frame(m), rows)
    assert detail["detail"] > 2 and detail["tone"] < 1 and detail["grain"] > 10
    bright = m.copy()
    bright[:4, :8] = 900
    high = frame_metrics(_frame(bright + np.where(bright > 800, 30, 0)), _frame(bright), rows)
    assert high["highlights"] > 5
    colour = frame_metrics(_frame(m, cb=520), _frame(m), rows)
    assert colour["colour"] == pytest.approx(4, abs=0.01)
    dark = _base(value=150)
    assert frame_metrics(_frame(dark + 3), _frame(dark), rows)["shadows"] == pytest.approx(3, abs=0.2)


def test_active_rows_from_area_and_detection():
    y = np.full((40, 10), 64.0)
    y[10:30] = 400
    frame = _frame(y)
    assert active_rows(frame, (0, 0, 5, 5)) == slice(5, 35)
    assert active_rows(frame) == slice(10, 30)


def test_classify_summary_and_recommendation():
    assert classify(1, (2, 8)) == LEVELS[0] and classify(-5, (2, 8)) == LEVELS[1] and classify(9, (2, 8)) == LEVELS[2]
    weak = [FelSample(t, {"tone": 0.5, "detail": 0.2, "highlights": 1, "grain": 1, "colour": 0.1, "shadows": 0.2,
                          "temporal": 0.1}) for t in range(10)]
    report = FelReport(None, tuple(weak), summarise(weak), ())
    assert recommendation(report)[0].endswith("inutile")
    strong_tone = [dataclasses.replace(s, metrics={**s.metrics, "tone": 12.0}) for s in weak]
    assert recommendation(FelReport(None, tuple(strong_tone), summarise(strong_tone), ()))[0].endswith("utile")
    assert summarise(weak)[0].jnd == pytest.approx(0.5 / 1.2)
    # Détail de 3 codes RMS ≈ 1,2 JND vu à une hauteur d'image : faible.
    assert summarise([dataclasses.replace(s, metrics={**s.metrics, "detail": 3.0}) for s in weak])[2].level == LEVELS[0]
    detail_only = [dataclasses.replace(s, metrics={**s.metrics, "detail": 12.0}) for s in weak]
    detail_report = FelReport(None, tuple(detail_only), summarise(detail_only), ())
    assert "si le débit le permet" in recommendation(detail_report)[0]
    from core.hdr_inspection.fel_analyzer import Survival
    lost = dataclasses.replace(detail_report, survival=Survival("libx265", 20000, 0.9, 0.2, 5.0, 3.0))
    assert "haut débit" in recommendation(lost)[0] and "20000" in recommendation(lost)[1]
    assert summarise([])[0].level == LEVELS[0]


def test_overall_contribution_pools_weak_categories():
    # Quatre catégories faibles (≈ 1,3 JND chacune) au même instant : cumul ≈ 2,1 JND, apport moyen.
    base = {"tone": 1.6, "detail": 3.3, "highlights": 0, "grain": 0, "colour": 0.6, "shadows": 1.6, "temporal": 0.9}
    weak = [FelSample(t, dict(base)) for t in range(10)]
    report = FelReport(None, tuple(weak), summarise(weak), ())
    assert all(s.level == LEVELS[0] for s in report.summary)
    assert report.overall.p90 == pytest.approx(1.6 / 1.2 * 4 ** (1 / 3), rel=0.02)
    assert report.overall.level == LEVELS[1]
    verdict, detail = recommendation(report)
    assert verdict.endswith("apport modéré") and "cumul" in detail
    # Ombres comptées avec le ton (mêmes pixels) : pas de double compte.
    assert pooled_jnd({"tone": 2.4}) == pytest.approx(pooled_jnd({"tone": 2.4, "shadows": 2.4}))
    medium = [FelSample(t, {**base, "tone": 4.5, "shadows": 4.5, "colour": 1.6, "detail": 9.0, "temporal": 2.5})
              for t in range(10)]
    strong = FelReport(None, tuple(medium), summarise(medium), ())
    assert LEVELS[2] not in [s.level for s in strong.summary] and strong.overall.level == LEVELS[2]
    assert recommendation(strong)[0].endswith("utile") and "cumul" in recommendation(strong)[1]


def test_thumbnails_and_analyse_with_fake_capture():
    m = _base(h=128, w=192)

    def capture(layer: str, time_s: float, count: int) -> list[Frame]:
        offset = time_s if layer == "reconstructed" else 0.0  # apport proportionnel au temps
        return [_frame(m + offset + index * 0.5 * (layer == "reconstructed")) for index in range(count)]

    progress: list[float] = []
    report = analyse(capture, [1.0, 2.0, 8.0, 4.0], progress=progress.append, moments=2)
    assert len(report.samples) == 4 and progress[-1] == 1.0
    assert [m.time_s for m in report.moments] == [4.0, 8.0]
    assert report.samples[2].metrics["tone"] == pytest.approx(8, abs=0.3)
    assert report.samples[0].metrics["temporal"] == pytest.approx(0.5, abs=0.05)
    moment = report.moments[0]
    assert moment.mapped.dtype == np.uint8 and moment.mapped.shape[2] == 3
    assert moment.difference.shape[:2] == moment.mapped.shape[:2]
    assert thumbnail(_frame(m), slice(0, 128), width=48).shape == (64, 96, 3) or thumbnail(_frame(m), slice(0, 128), width=48).ndim == 3
    assert difference_map(_frame(m + 16), _frame(m), slice(0, 128)).max() > 200
    with pytest.raises(InterruptedError):
        analyse(capture, [1.0], cancelled=lambda: True)


def test_sample_times_and_survival():
    times = sample_times(3600, 60)
    assert len(times) == 59 and 5 < times[0] < 70 and times[-1] < 3595
    assert sample_times(4, 60) == [2.0]
    m = _base()
    rng = np.random.default_rng(3)
    r = [_frame(m + 6 + rng.normal(0, 2, m.shape)) for _ in range(3)]
    mapped = [_frame(m) for _ in range(3)]
    perfect = survival(r, mapped, r, encoder="x", bitrate_kbps=1000, rows=slice(0, 64))
    assert perfect.tone_kept == pytest.approx(1) and perfect.detail_kept == pytest.approx(1)
    assert perfect.encode_error == pytest.approx(0)
    lost = survival(r, mapped, mapped, encoder="x", bitrate_kbps=1000, rows=slice(0, 64))
    assert lost.tone_kept == pytest.approx(0) and lost.detail_kept == pytest.approx(0) and lost.detail > 1
    with pytest.raises(ValueError):
        survival(r, mapped, [], encoder="x", bitrate_kbps=1000, rows=slice(0, 64))
    assert survival_indices(24) == [0, 3, 7, 10, 13, 16, 20, 23] and survival_indices(5) == [0, 1, 2, 3, 4]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg requis")
def test_encode_round_trip_keeps_requested_frames():
    rng = np.random.default_rng(5)
    base = _base()
    frames = [_frame(base + rng.normal(0, 3, base.shape)) for _ in range(6)]
    encoders = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    if "libx265" not in encoders:
        pytest.skip("libx265 requis")
    kept = encode_round_trip(frames, ffmpeg_bin="ffmpeg", encoder="libx265", bitrate_kbps=20000,
                             frame_rate="24/1", keep=[0, 5])
    assert len(kept) == 2 and kept[0].y.shape == base.shape and kept[0].cb.shape == (32, 48)
    assert float(np.abs(kept[1].y.astype(float) - frames[5].y.astype(float)).mean()) < 4
    with pytest.raises(subprocess.CalledProcessError):
        encode_round_trip(frames, ffmpeg_bin="ffmpeg", encoder="encodeur_absent", bitrate_kbps=1000,
                          frame_rate="24/1")


def test_encoder_args_per_family(monkeypatch):
    import core.workflows.encode.hw_devices as hw_devices
    from core.hdr_inspection import fel_analyzer

    monkeypatch.setattr(fel_analyzer.sys, "platform", "linux")
    monkeypatch.setattr(hw_devices, "select_linux_hwaccel_device", lambda codec: "/dev/dri/renderD129")
    assert fel_analyzer._encoder_args("hevc_vaapi") == (["-vaapi_device", "/dev/dri/renderD129"],
                                                        ["-vf", "format=p010le,hwupload"])
    assert fel_analyzer._encoder_args("hevc_qsv")[0] == ["-qsv_device", "/dev/dri/renderD129"]
    assert "p010le" in fel_analyzer._encoder_args("hevc_nvenc")[1]
    assert "yuv420p10le" in fel_analyzer._encoder_args("libx265")[1]


def test_producer_refuses_layer_without_capability():
    from core.fel.engine import FelError, FelSource, layers_supported

    class Lib:
        def mvo_fel_create(self, *args):
            return 5

        def mvo_fel_destroy(self, handle):
            self.destroyed = True

        def mvo_fel_set_precision(self, *args):
            return 0

    class Engine:
        capabilities: dict = {"precision": ["exact", "standard", "fast"]}
        lib = Lib()

    assert not layers_supported(Engine(), "mapped")
    source = FelSource(engine=Engine(), path=Path("/x.mkv"), stream=0, layer="mapped")  # type: ignore[arg-type]
    with pytest.raises(FelError, match="0.4.0"):
        source.start(open(__file__, "rb"), threading.Event())  # noqa: SIM115
    assert Engine.lib.destroyed


# ── Interface ────────────────────────────────────────────────────────────────
def _fel_report() -> FelReport:
    samples = [FelSample(float(t), {"tone": 9.0, "detail": 1.0, "highlights": 2.0, "grain": 1.0, "colour": 0.2,
                                    "shadows": 0.5, "temporal": 0.1}) for t in range(5)]
    image = np.zeros((20, 40, 3), np.uint8)
    from core.hdr_inspection.fel_analyzer import Moment
    moments = (Moment(12.0, 1.5, image, image, image),)
    facts = LayerFacts(100, 4.0, 4_000_000, 1_000_000, 10_000, (1920, 1080))
    return FelReport(facts, tuple(samples), summarise(samples), moments)


def test_fel_tab_renders_report(qt_app, tmp_path):
    from core.hdr_inspection import InspectionReport, build_snapshot
    from tests.test_hdr_inspection import _info
    from ui.panels.hdr_studio.fel_tab import FelTab

    tab = FelTab()
    try:
        assert not tab.analyse_btn.isEnabled()
        snapshot = build_snapshot(_info(tmp_path / "a.mkv"))
        tab.set_report(InspectionReport(snapshot))
        assert tab.available() and tab.analyse_btn.isEnabled() and not tab.survival_btn.isEnabled()
        requested: list[int] = []
        tab.analysis_requested.connect(requested.append)
        tab.analyse_btn.click()
        assert requested == [30]
        report = _fel_report()
        tab.set_fel_report(report)
        tab.set_fel_report(report)  # second rendu : contrôles de survie conservés
        assert tab.survival_btn.isEnabled()
        survival_requests: list[tuple] = []
        tab.survival_requested.connect(lambda *args: survival_requests.append(args))
        tab.survival_btn.click()
        assert survival_requests == [("libx265", 35000)]
        texts = [w.text() for w in tab.findChildren(type(tab.analyse_btn).__mro__[0]) if hasattr(w, "text")]
        assert texts is not None
        from PySide6.QtWidgets import QLabel
        labels = [label.text() for label in tab.findChildren(QLabel)]
        assert any(text.startswith("Intégrer le FEL") for text in labels)
        assert any("1920×1080" in text for text in labels) and any("0:12" in text for text in labels)
        moments: list[float] = []
        tab.moment_requested.connect(moments.append)
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        from ui.panels.hdr_studio.fel_tab import _ClickableLabel
        QTest.mouseClick(tab.findChildren(_ClickableLabel)[1], Qt.MouseButton.LeftButton)
        assert moments == [12.0]
    finally:
        tab.close()


def test_panel_fel_analysis_flow(qt_app, tmp_path, monkeypatch):
    import ui.panels.hdr_studio.panel as panel_module
    import core.hdr_inspection.fel_analyzer as analyzer_module
    from core.config import AppConfig
    from tests.test_hdr_inspection import _info
    from ui.panels.hdr_studio import HdrStudio

    class Inspector:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def inspect(self, path):
            return _info(Path(path))

    monkeypatch.setattr(panel_module, "FileInspector", Inspector)
    monkeypatch.setattr(analyzer_module, "plugin_capture", lambda *a, **k: (lambda layer, t, n: []))
    report = _fel_report()
    monkeypatch.setattr(analyzer_module, "analyse", lambda *a, **k: report)
    monkeypatch.setattr(fel_layer, "scan_layers", lambda *a, **k: report.facts)
    config = AppConfig()
    config.app_data_dir = tmp_path / "appdata"
    studio = HdrStudio(config)
    try:
        media = tmp_path / "film.mkv"
        media.write_bytes(b"\x1a\x45\xdf\xa3")
        studio.load_source(media, 0)
        end = time.monotonic() + 5
        while studio._info is None and time.monotonic() < end:
            qt_app.processEvents()
        from core.hdr_inspection import InspectionReport, audit, build_snapshot
        from tests.test_hdr_inspection import _analyses
        # Sans métadonnées dynamiques : type de couche inconnu, lecture demandée d'abord.
        studio.analyse_fel(30)
        assert "métadonnées dynamiques" in studio._status.text() and not studio.is_busy()
        snapshot = build_snapshot(_info(media))
        mel, _h = _analyses(tmp_path, el_type="MEL", residual_off=False)
        studio._render(InspectionReport(snapshot, mel, None, audit(snapshot, mel), dynamic=True))
        studio.analyse_fel(30)
        assert studio._status.text().startswith("MEL") and not studio.is_busy()
        assert not studio._fel_tab.analyse_btn.isEnabled()
        fel, _h = _analyses(tmp_path, el_type="FEL", residual_off=False)
        studio._render(InspectionReport(snapshot, fel, None, audit(snapshot, fel), dynamic=True))
        assert studio._fel_tab.analyse_btn.isEnabled()
        studio._engine_factory = lambda: type("E", (), {"capabilities": {"layers": ["base", "mapped", "reconstructed"]}})()
        studio.analyse_fel(30)
        end = time.monotonic() + 5
        while (studio.is_busy() or studio._fel_tab.fel_report() is None) and time.monotonic() < end:
            qt_app.processEvents()
        assert studio._fel_tab.fel_report() is report
        assert studio._tabs.currentWidget() is studio._fel_scroll
        m = _base(h=64, w=96)
        monkeypatch.setattr(analyzer_module, "plugin_capture", lambda *a, **k: (
            lambda layer, t, n: [_frame(m + (8 if layer == "reconstructed" else 0))] * n))
        studio.open_fel_moment(12.0)
        end = time.monotonic() + 5
        while (studio.is_busy() or studio._moment_dialog is None) and time.monotonic() < end:
            qt_app.processEvents()
        from ui.panels.hdr_studio.before_after import MomentCompareDialog
        dialog = studio._moment_dialog
        assert isinstance(dialog, MomentCompareDialog) and dialog.isVisible()
        assert "0:12" in dialog.windowTitle()
        dialog.close()
        qt_app.processEvents()
        studio._engine_factory = lambda: type("E", (), {"capabilities": {}})()
        studio.analyse_fel(30)
        assert "0.4.0" in studio._status.text()
    finally:
        studio.close()


# ── Comparateur pleine définition ────────────────────────────────────────────
def test_pq_codes_moment_images_and_display():
    from core.hdr_inspection.fel_analyzer import _rgb_pq, luminance_nits, moment_images, pq_codes, render_sdr

    rng = np.random.default_rng(3)
    frame = Frame(rng.integers(64, 940, (40, 64), dtype=np.uint16), rng.integers(64, 960, (20, 32), dtype=np.uint16),
                  rng.integers(64, 960, (20, 32), dtype=np.uint16))

    def up(c):
        return np.repeat(np.repeat(c.astype(np.float64), 2, 0), 2, 1)

    reference = np.round(_rgb_pq(frame.y.astype(np.float64), up(frame.cb), up(frame.cr)) * 65535)
    for rows in (slice(0, 40), slice(3, 37), slice(1, 40)):
        codes = pq_codes(frame, rows)
        assert codes.dtype == np.uint16 and codes.shape == (rows.stop - rows.start, 64, 3)
        assert np.abs(codes.astype(np.int64) - reference[rows]).max() <= 1
    images = moment_images(frame, frame, slice(3, 37), 5.0)
    assert images.mapped.shape == (34, 64, 3) and not images.difference.any()
    shifted = moment_images(Frame(frame.y + 16, frame.cb, frame.cr), frame, slice(0, 40), 5.0)
    assert shifted.difference.max() > 200
    dark, bright = render_sdr(images.mapped, 0.0), render_sdr(images.mapped, 2.0)
    assert dark.dtype == np.uint8 and dark.shape == images.mapped.shape and (bright >= dark).all()
    assert luminance_nits(np.array([65535, 65535, 65535])) == pytest.approx(10000, rel=1e-3)


def test_before_after_view_zoom_pan_split_and_dialog(qt_app):
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtTest import QTest

    from core.hdr_inspection.fel_analyzer import moment_images
    from ui.panels.hdr_studio.before_after import BeforeAfterView, MomentCompareDialog, to_qimage

    view = BeforeAfterView()
    try:
        left = to_qimage(np.zeros((100, 200, 3), np.uint8))
        right = to_qimage(np.full((100, 200, 3), 255, np.uint8))
        view.resize(400, 200)
        view.set_images(left, right, ("Avant", "Après"))
        assert view.zoom() == pytest.approx(2.0)
        assert view.image_point(QPointF(200, 100)) == QPointF(100, 50)
        view.zoom_to(8.0, QPointF(300, 50))  # le point visé reste sous le curseur
        assert view.image_point(QPointF(300, 50)) == QPointF(150, 25) and view.zoom() == 8.0
        view.zoom_to(100.0)
        assert view.zoom() == 16.0
        view.fit()
        assert view.zoom() == pytest.approx(2.0)
        # Trait déplacé à la souris, déplacement de l'image ailleurs.
        QTest.mousePress(view, Qt.MouseButton.LeftButton, pos=QPoint(200, 100))
        QTest.mouseMove(view, QPoint(100, 100))
        QTest.mouseRelease(view, Qt.MouseButton.LeftButton, pos=QPoint(100, 100))
        assert view.split() == pytest.approx(0.25, abs=0.01)
        view.zoom_to(4.0)
        before = view.image_point(QPointF(300, 100))
        QTest.mousePress(view, Qt.MouseButton.LeftButton, pos=QPoint(300, 100))
        QTest.mouseMove(view, QPoint(340, 100))
        QTest.mouseRelease(view, Qt.MouseButton.LeftButton, pos=QPoint(340, 100))
        assert view.image_point(QPointF(340, 100)).x() == pytest.approx(before.x())
        assert not view.grab().isNull()
    finally:
        view.close()
    m = _base(h=64, w=96)
    dialog = MomentCompareDialog(moment_images(_frame(m + 8), _frame(m), slice(0, 64), 12.0))
    try:
        dialog.resize(800, 500)
        dialog.show()
        qt_app.processEvents()
        dialog.right.setCurrentIndex(1)
        dialog.exposure.setValue(1.5)
        dialog._render()
        dialog._show_pixel(10, 10)
        assert "nits" in dialog.readout.text() and "%" in dialog.readout.text()
        dialog._show_pixel(-1, -1)
        assert dialog.readout.text() == ""
    finally:
        dialog.close()
