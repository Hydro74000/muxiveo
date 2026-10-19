"""HDR Studio, lot 4 : comparaison de deux pistes, rapport exporté (JSON / HTML), CLI ``inspect --hdr``."""
from __future__ import annotations

import base64
import json
import sys
import time
from pathlib import Path

import numpy as np
import pytest

import cli.hdr as cli_hdr
import ui.panels.hdr_studio.panel as panel_module
from cli.constants import EXIT_OK, EXIT_VALIDATION
from cli.logging import Logger
from cli.parser import build_parser
from core.config import AppConfig
from core.hdr_inspection import InspectionReport, audit, build_snapshot
from core.hdr_inspection.compare import Side, compare, curve_offset, scene_offset
from core.hdr_inspection.measure import ImageMeasurement
from core.hdr_inspection.report import png_data_uri, render_html, report_data, write_report
from core.i18n import current_language, set_current_language
from tests.test_hdr_inspection import _analyses, _info
from tests.test_hdr_studio import _FakeInspector, _media, _wait
from ui.panels.hdr_studio import HdrStudio
from ui.panels.hdr_studio.compare_tab import CompareTab, shifted


@pytest.fixture(autouse=True)
def _french():
    previous = current_language()
    set_current_language("fra")
    yield
    set_current_language(previous)


def _measurement(peaks: np.ndarray, starts: np.ndarray, gain: float = 1.0) -> ImageMeasurement:
    frames = peaks.size
    max_rgb = np.stack([peaks * gain] * 3, axis=1).astype(np.float32)
    return ImageMeasurement("base", max_rgb, (peaks * gain / 5).astype(np.float32), np.zeros(frames, np.float32),
                            np.zeros(frames, np.float32), np.zeros((frames, 7), np.float32), starts.astype(np.int64),
                            np.zeros(1024, np.int64), (0, 0, 3840, 2160))


def _film(seed: int = 4, frames: int = 6000) -> tuple[np.ndarray, np.ndarray]:
    """Courbe de pics par plans (niveaux tirés au hasard) et débuts de plans."""
    rng = np.random.default_rng(seed)
    starts = np.unique(np.concatenate([[0], rng.integers(1, frames, 70)]))
    levels = rng.uniform(50, 1500, starts.size)
    peaks = levels[np.searchsorted(starts, np.arange(frames), side="right") - 1]
    return peaks.astype(np.float64), starts


def _side(tmp_path: Path, name: str, measurement: ImageMeasurement | None) -> Side:
    snapshot = build_snapshot(_info(tmp_path / name, frames=measurement.frames if measurement else 10))
    return Side(InspectionReport(snapshot, findings=audit(snapshot, measurement=measurement)), measurement)


# ── Alignement ──────────────────────────────────────────────────────────────
def test_scene_offset_with_jitter_and_extra_cuts():
    _peaks, starts = _film()
    rng = np.random.default_rng(1)
    shifted_starts = starts[starts >= 120] - 120
    shifted_starts = shifted_starts + rng.integers(-1, 2, shifted_starts.size)  # détecteur différent : ±1 image
    extra = rng.integers(1, 5000, 6)
    offset, share = scene_offset(starts, np.sort(np.concatenate([[0], shifted_starts, extra])))
    assert offset == 120 and share > 0.85
    assert scene_offset(starts[:1], starts) == (0, 0.0)


def test_curve_offset_finds_the_lag():
    peaks, _starts = _film(frames=5000)
    offset, correlation = curve_offset(peaks, peaks[300:])
    assert offset == 300 and correlation > 0.99
    offset, _ = curve_offset(peaks[250:], peaks)
    assert offset == -250


def test_compare_measured_sides(tmp_path):
    peaks, starts = _film()
    a = _side(tmp_path, "a.mkv", _measurement(peaks, starts))
    b_starts = np.concatenate([[0], starts[starts > 120] - 120])
    b = _side(tmp_path, "b.mkv", _measurement(peaks[120:], b_starts, gain=1.1))
    comparison = compare(a, b)
    assert comparison.offset == 120 and comparison.alignment == "measure" and comparison.alignment_score > 0.95
    peak = next(c for c in comparison.curves if c.kind == "peak")
    assert peak.source_a == peak.source_b == "measure" and peak.median_ratio == pytest.approx(1.1, abs=0.01)
    assert peak.correlation > 0.99 and peak.frames == peaks.size - 120 and peak.worst
    fields = {f.label: f for f in comparison.fields}
    assert not fields["MaxCLL / MaxFALL mesurés"].same and fields["Formats"].same
    manual = compare(a, b, 0)
    assert manual.alignment == "manual" and manual.offset == 0
    assert next(c for c in manual.curves if c.kind == "peak").correlation < peak.correlation
    out = shifted(np.arange(5, dtype=np.float32), 2, 6)
    assert np.isnan(out[:2]).all() and out[2:].tolist() == [0, 1, 2, 3]


def test_compare_mixed_sources_and_no_curves(tmp_path):
    dovi, hdr10plus = _analyses(tmp_path)
    snapshot = build_snapshot(_info(tmp_path / "a.mkv"))
    a = Side(InspectionReport(snapshot, dovi, None, audit(snapshot, dovi), dynamic=True))
    b = Side(InspectionReport(snapshot, None, hdr10plus, audit(snapshot, None, hdr10plus), dynamic=True))
    comparison = compare(a, b)
    peak = next(c for c in comparison.curves if c.kind == "peak")
    assert (peak.source_a, peak.source_b) == ("dovi", "hdr10plus")
    bare = Side(InspectionReport(snapshot))
    assert compare(bare, bare).curves == () and compare(bare, bare).alignment == "none"


# ── Rapport ─────────────────────────────────────────────────────────────────
def test_report_json_and_html(tmp_path):
    peaks, starts = _film()
    a = _side(tmp_path, "a <film>.mkv", _measurement(peaks, starts))
    b = _side(tmp_path, "b.mkv", _measurement(peaks[60:], np.concatenate([[0], starts[starts > 60] - 60])))
    comparison = compare(a, b)
    data = report_data(a.report, measurement=a.measurement, comparison=comparison, other=b.report, columns=300)
    text = json.dumps(data, allow_nan=False)
    assert data["format"] == "muxiveo-hdr-report" and data["measurement"]["max_cll"] == round(peaks.max())
    assert data["curves"]["columns"] == 300 and len(data["curves"]["series"][0]["values"]) == 300
    assert data["comparison"]["offset_frames"] == 60 and data["comparison"]["other"]["name"] == "b.mkv"
    assert "NaN" not in text
    page = render_html(a.report, measurement=a.measurement, comparison=comparison, other=b.report)
    assert page.startswith("<!doctype html>") and page.count("<svg") == 2 and "Comparaison" in page
    assert "a &lt;film&gt;.mkv" in page and "<film>" not in page
    html_path = write_report(tmp_path / "rapport.html", a.report, measurement=a.measurement)
    json_path = write_report(tmp_path / "rapport.json", a.report)
    assert html_path.read_text(encoding="utf-8").startswith("<!doctype html>")
    if sys.platform != "win32":
        assert html_path.stat().st_mode & 0o777 == 0o644
    assert json.loads(json_path.read_text(encoding="utf-8"))["source"]["name"] == "a <film>.mkv"


def test_png_data_uri_is_a_valid_png(qt_app):
    from PySide6.QtGui import QImage

    image = np.zeros((8, 12, 3), dtype=np.uint8)
    image[:, :, 0] = 255
    uri = png_data_uri(image)
    decoded = QImage.fromData(base64.b64decode(uri.split(",", 1)[1]), "PNG")
    assert (decoded.width(), decoded.height()) == (12, 8) and decoded.pixelColor(3, 3).red() == 255


# ── CLI ─────────────────────────────────────────────────────────────────────
def _cli(tmp_path, monkeypatch, capsys, *argv: str) -> tuple[int, dict]:
    monkeypatch.setattr(cli_hdr, "FileInspector", _FakeInspector)
    config = AppConfig()
    config.app_data_dir = tmp_path / "appdata"
    args = build_parser().parse_args(["inspect", *argv, "--work-dir", str(tmp_path / "work")])
    code = args.func(args, config, Logger())
    return code, json.loads(capsys.readouterr().out)


def test_cli_inspect_hdr_static_compare_and_report(tmp_path, monkeypatch, capsys):
    first, second = tmp_path / "a.mkv", tmp_path / "b.mkv"
    for path in (first, second):
        path.write_bytes(b"\x1a\x45\xdf\xa3")
    report = tmp_path / "rapport.html"
    code, payload = _cli(tmp_path, monkeypatch, capsys, str(first), str(second), "--hdr", "static", "--hdr-compare",
                         "--hdr-report", str(report))
    assert code == EXIT_OK and [item["source"]["name"] for item in payload["hdr"]] == ["a.mkv", "b.mkv"]
    assert payload["comparison"]["alignment"] == "none" and report.read_text(encoding="utf-8").count("<svg") == 0
    warns = any(f["severity"] in ("warn", "error") for f in payload["hdr"][0]["findings"])
    code, _ = _cli(tmp_path, monkeypatch, capsys, str(first), "--hdr", "static", "--hdr-fail-on", "warn")
    assert code == (EXIT_VALIDATION if warns else EXIT_OK)
    from cli.errors import CliError
    with pytest.raises(CliError):
        _cli(tmp_path, monkeypatch, capsys, str(first), "--hdr", "static", "--hdr-compare")


# ── Interface ───────────────────────────────────────────────────────────────
def test_compare_tab_renders_and_accepts_manual_offset(qt_app, tmp_path):
    peaks, starts = _film()
    a = _side(tmp_path, "a.mkv", _measurement(peaks, starts))
    b = _side(tmp_path, "b.mkv", _measurement(peaks[120:], np.concatenate([[0], starts[starts > 120] - 120])))
    tab = CompareTab()
    tab.set_side_a(a)
    assert tab.comparison() is None and not tab.offset.isEnabled()
    tab.set_side_b(b)
    assert tab.comparison() is not None and tab.offset.value() == 120 and "plans mesurés" in tab._alignment.text()
    assert [s.key for s in tab.chart.series()] == ["a_max", "b_max", "a_mean", "b_mean"]
    tab.offset.setValue(100)
    assert tab.comparison().alignment == "manual" and "décalage saisi" in tab._alignment.text()
    tab.auto_btn.click()
    assert tab.comparison().offset == 120
    # Nouveau rendu de A (même piste) : décalage conservé.
    tab.offset.setValue(118)
    tab.set_side_a(a)
    assert tab.comparison().offset == 118
    tab.deleteLater()


def test_panel_compare_flow_and_export(qt_app, tmp_path, monkeypatch):
    monkeypatch.setattr(panel_module, "FileInspector", _FakeInspector)
    config = AppConfig()
    config.app_data_dir = tmp_path / "appdata"
    studio = HdrStudio(config)
    try:
        media = _media(tmp_path)
        studio.load_source(media, 0)
        assert _wait(qt_app, lambda: studio._info is not None)
        other = tmp_path / "autre.mkv"
        other.write_bytes(b"\x1a\x45\xdf\xa3")
        logs: list[tuple[str, str]] = []
        studio.log_message.connect(lambda *args: logs.append(args))
        studio.load_compare(tmp_path / "absent.mkv")
        assert ("ERROR", "Fichier vidéo introuvable ou non pris en charge : absent.mkv") in logs
        studio.load_compare(other)
        assert _wait(qt_app, lambda: studio._compare_tab.side_b() is not None)
        assert studio._compare_tab.comparison() is not None and "en-têtes seulement" in studio._compare_tab._status.text()
        assert studio._compare_tab.analyze_btn.isEnabled()
        written = studio.export_report(tmp_path / "rapport.json")
        data = json.loads(written.read_text(encoding="utf-8"))
        assert data["comparison"]["other"]["name"] == "autre.mkv" and ("OK", f"Rapport HDR exporté : {written}") in logs
        assert studio._export_btn.isEnabled()
    finally:
        studio.close()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not (getattr(studio, "_shutdown", None) and studio._shutdown.done.is_set()):
            qt_app.processEvents()


def test_cli_shares_the_application_data_directory_with_the_gui():
    """La CLI nomme son application comme l'interface : mêmes caches HDR Studio."""
    import subprocess
    import sys as _sys

    code = ("import sys; sys.argv=['main.py']; from cli.main import _ensure_qcore_app; "
            "app=_ensure_qcore_app(); print(app.applicationName(), app.organizationName())")
    result = subprocess.run([_sys.executable, "-c", code], capture_output=True, text=True, check=True,
                            cwd=Path(__file__).resolve().parents[1])
    assert result.stdout.split() == ["Muxiveo", "Muxiveo"]
