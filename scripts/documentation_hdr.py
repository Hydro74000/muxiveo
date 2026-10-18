"""Demonstration fixtures for native HDR/encoding documentation screenshots.

These are constructed data, not measurements of a movie. No analysis, plugin
installation, encoder, metadata correction or network call is launched here.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import numpy as np

from core.hdr_inspection import InspectionReport, audit, build_snapshot
from core.hdr_inspection.compare import Side
from core.hdr_inspection.dovi import ActiveArea, DoviAnalysis, L6Set
from core.hdr_inspection.fel_analyzer import (
    FelReport, FelSample, Frame, Moment, Survival, difference_map, summarise, thumbnail,
)
from core.hdr_inspection.fel_layer import LayerFacts
from core.hdr_inspection.hdr10p import Hdr10PlusAnalysis
from core.hdr_inspection.measure import ImageMeasurement
from core.hdr_inspection.pq import nits_to_pq
from core.inspector import HDRType


def demonstration(reference_info):
    """Ten minutes of deterministic sample curves and consistent header data."""
    frames, fps = 14400, 24.0
    time = np.arange(frames) / fps
    scenes = np.arange(0, frames, 480, dtype=np.int32)
    peaks = 180 + 160 * (1 + np.sin(time / 21)) + 210 * np.sin(time / 4.5) ** 6
    for centre, height in ((82, 420), (185, 680), (342, 920), (472, 560)):
        peaks += height * np.exp(-((time - centre) / 7) ** 2)
    peaks = peaks.astype(np.float32)
    average = (15 + 30 * (1 + np.sin(time / 27)) + peaks / 35).astype(np.float32)
    raw = {"side_data_list": [
        {"side_data_type": "DOVI configuration record", "dv_profile": 7, "dv_level": 6,
         "rpu_present_flag": 1, "el_present_flag": 1, "bl_present_flag": 1,
         "dv_bl_signal_compatibility_id": 6},
        {"side_data_type": "Mastering display metadata", "red_x": "34000/50000", "red_y": "16000/50000",
         "green_x": "13250/50000", "green_y": "34500/50000", "blue_x": "7500/50000", "blue_y": "3000/50000",
         "white_point_x": "15635/50000", "white_point_y": "16450/50000",
         "min_luminance": "50/10000", "max_luminance": "40000000/10000"},
        {"side_data_type": "Content light level metadata", "max_content": 4000, "max_average": 400},
        {"side_data_type": "HDR Dynamic Metadata SMPTE2094-40 (HDR10+)"},
    ]}
    track = replace(reference_info.video_tracks[0], raw=raw, frame_rate="24/1", duration_s=600,
                    hdr_type=HDRType.DOLBY_VISION_HDR10PLUS, dovi_profile=7, dovi_compat_id=6)
    info = replace(reference_info, path=Path("/media/Demo/Voyage.P7.FEL.HDR10plus.mkv"),
                   duration_s=600, frame_count=frames, video_tracks=[track], hdr_type=track.hdr_type)
    def pq(values):
        return np.rint(nits_to_pq(values) * 4095).astype(np.uint16)

    dovi = DoviAnalysis(
        frames, 7, "FEL", frames, "4.0", scenes,
        pq(np.full(frames, .005)), pq(peaks * .9), pq(average), 0,
        (ActiveArea(0, frames, 0, 0, 280, 280),),
        (L6Set(4000, .005, 4000, 400, frames),), (100, 600, 1000), (1, 6), (1000,), (0,), (1,),
        ((.005, 4000),), 0,
    )
    hdr10plus = Hdr10PlusAnalysis(frames, "B", scenes, peaks, average, peaks * .88, (1,), (1000,), frames)
    histogram = np.zeros(1024, dtype=np.uint64)
    histogram[400:700] = np.linspace(100000, 100, 300).astype(np.uint64)
    measurement = ImageMeasurement(
        "base", np.stack([peaks, peaks * .85, peaks * .7], axis=1), average, peaks * .93,
        np.full(frames, .94, np.float32),
        np.stack([peaks * ratio for ratio in (.001, .01, .03, .06, .12, .3, .91)], axis=1),
        scenes, histogram, (0, 280, 3840, 1600),
    )
    snapshot = build_snapshot(info, 0)
    report = InspectionReport(snapshot, dovi, hdr10plus, audit(snapshot, dovi, hdr10plus, measurement), dynamic=True)
    return info, report, measurement


def fel_demonstration():
    """Illustrate category and image widgets using a geometric calibration pattern."""
    samples = tuple(FelSample(float(i * 30), {
        "tone": 9 + np.sin(i) * 2, "detail": 2.3 + .4 * np.sin(i), "highlights": 18 + i % 8,
        "grain": 4.2, "colour": .7, "shadows": 1.3, "temporal": .6,
    }) for i in range(20))
    y, x = np.mgrid[:270, :480]
    image = 100 + x * 1.05 + y * .35 + 100 * np.sin(x / 17) * np.sin(y / 25)
    image += 90 * (((x - 330) ** 2 + (y - 130) ** 2) < 65 ** 2)
    mapped = Frame(np.clip(image, 64, 940).astype(np.uint16), np.full((135, 240), 495, np.uint16),
                   np.full((135, 240), 530, np.uint16))
    rebuilt = replace(mapped, y=np.clip(image + 12 + 5 * np.sin(x / 3), 64, 940).astype(np.uint16))
    rows = slice(0, 270)
    moment = Moment(342, 9.3, thumbnail(mapped, rows), thumbnail(rebuilt, rows),
                    difference_map(rebuilt, mapped, rows))
    facts = LayerFacts(14400, 600, 2_250_000_000, 375_000_000, 3_000_000, (1920, 1080))
    return FelReport(facts, samples, summarise(samples), (moment,),
                     Survival("libx265", 35000, .94, .72, .7, 2.3))


def capture_detail(scene, win, reference_info, page, settle, save, basename, text):
    """Render existing widgets only; all values come from the demo fixtures above."""
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QPushButton, QScrollArea

    # These two tabs contain complete comparison/editing forms below the fold.
    win.resize(1600, 1840 if scene == "hdr-edit" else 1510 if scene == "hdr-compare" else 1200)
    if scene in ("hdr-fel-viewer", "hdr-fel-difference"):
        from core.hdr_inspection.fel_analyzer import moment_images
        from ui.panels.hdr_studio.before_after import MomentCompareDialog

        # Full-resolution calibration pattern, independent of any movie or plugin.
        y, x = np.mgrid[:720, :1280]
        pattern = 180 + x * .32 + y * .15 + 75 * np.sin(x / 35) * np.sin(y / 45)
        pattern += 100 * (((x - 870) ** 2 + (y - 330) ** 2) < 180 ** 2)
        mapped = Frame(np.clip(pattern, 64, 940).astype(np.uint16),
                       np.full((360, 640), 495, np.uint16), np.full((360, 640), 530, np.uint16))
        rebuilt = replace(mapped, y=np.clip(pattern + 7 + 5 * np.sin(x / 4), 64, 940).astype(np.uint16))
        dialog = MomentCompareDialog(moment_images(rebuilt, mapped, slice(0, 720), 342), win)
        dialog.resize(1600, 950)
        dialog.show()
        if scene == "hdr-fel-difference":
            dialog.right.setCurrentIndex(dialog.right.findData("difference"))
        settle(.2)
        dialog.view.fit()
        save(scene, basename, dialog)
        dialog.close()
        settle(.1)
        return

    if scene in ("closed-captions", "workflow-menu"):
        page(3)
        win._log_panel.set_collapsed(True)
        win._on_log_collapsed(True)
        panel = win._remux_panel
        if scene == "closed-captions":
            assert any(track.embedded_caption_parent_id for track in
                       panel._track_table.current_tracks(include_embedded=True)), "Caption demo row missing"
            save(scene, basename)
        else:
            button = next(b for b in panel.findChildren(QPushButton) if b.text() == "Workflow")
            menu = button.menu()
            menu.popup(button.mapToGlobal(QPoint(0, button.height())))
            settle(.15)
            save(scene, basename, overlay=menu)
            menu.close()
        return

    info, report, measurement = demonstration(reference_info)
    if scene.startswith("hdr-"):
        page(7)
        win._log_panel.set_collapsed(True)
        win._on_log_collapsed(True)
        hdr = win._hdr_panel
        hdr._info = info
        hdr._path_edit.setText(str(info.path))
        hdr._track_combo.blockSignals(True)
        hdr._track_combo.clear()
        hdr._track_combo.addItem("#0 · HEVC 3840×2160 · Dolby Vision P7 FEL + HDR10+", 0)
        hdr._track_combo.blockSignals(False)
        hdr._measurement = measurement
        hdr._render(report)
        hdr._update_actions()
        index = {"hdr-overview": 0, "hdr-luminance": 1, "hdr-dovi": 2, "hdr-fel": 4,
                 "hdr-fel-detail": 4, "hdr-compare": 5, "hdr-edit": 6}[scene]
        hdr._tabs.setCurrentIndex(index)
        if scene == "hdr-luminance":
            hdr._luminance.display_peak.setValue(700)
            hdr._luminance.set_values(measurement.max_cll(), measurement.max_fall())
        elif scene in ("hdr-fel", "hdr-fel-detail"):
            hdr._fel_tab.set_fel_report(fel_demonstration())
        elif scene == "hdr-compare":
            other = replace(report, snapshot=replace(report.snapshot, path="/media/Demo/Voyage.HDR10.mkv",
                                                     dovi=None, max_cll=1500, max_fall=100), dovi=None)
            other_measure = replace(measurement, max_rgb=measurement.max_rgb * .85, average=measurement.average * .95)
            hdr._compare_tab.set_tracks(Path(other.snapshot.path), [(0, "#0 · HEVC 3840×2160 · HDR10+")], 0)
            hdr._compare_tab.set_side_b(Side(other, other_measure))
        elif scene == "hdr-edit":
            editor = hdr._editor
            editor.set_static_values(measurement.max_cll(), measurement.max_fall())
            for box in (editor.static_box, editor.mdcv_box, editor.l6_box, editor.source_box):
                box.setChecked(True)
            editor.mdcv_max.setValue(1500)
        settle(.3)
        if scene == "hdr-fel-detail":
            scroll = hdr._fel_scroll
            scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
        elif scene in ("hdr-overview", "hdr-dovi", "hdr-edit"):
            scroll = hdr._tabs.currentWidget()
            if isinstance(scroll, QScrollArea):
                scroll.verticalScrollBar().setValue(0)
        hdr._set_status(text("Données de démonstration · aucune analyse média exécutée",
                             "Demonstration data · no media analysis executed"))
        settle(.15)
        save(scene, basename)
        return

    page(2)
    win._log_panel.set_collapsed(True)
    win._on_log_collapsed(True)
    encode = win._encode_panel
    encode._queue_track_hdr_metadata = lambda: None
    encode._maybe_offer_static_hdr_estimate_for_current = lambda: None
    reference_info.video_tracks[0] = info.video_tracks[0]
    reference_info.hdr_type = info.hdr_type
    encode._codec_combo.setCurrentIndex(encode._codec_combo.findData("libx265"))
    encode._bake_fel_choice = scene == "fel-encoding"
    encode._sync_hdr_controls_for_codec()
    encode._tabs.setCurrentIndex(1)
    if scene == "fel-encoding":
        encode._bake_fel_choice = True
        encode._copy_dv_cb.setChecked(True)
        encode._copy_hdr10plus_cb.setChecked(True)
        # Illustrate an installed extension; do not install or load a library.
        with patch("core.plugins.installed_plugin", return_value=object()):
            encode._sync_dovi_profile_options()
        target = encode._bake_fel_cb
    elif scene == "interpolation":
        encode._tabs.setCurrentIndex(2)
        encode._interpolation_tool_available = lambda: True
        encode._sync_interpolation_availability()
        encode._interp_cb.setChecked(True)
        encode._set_combo_data(encode._interp_factor_combo, "2.5")
        encode._set_combo_data(encode._interp_quality_combo, "quality")
        target = encode._interp_cb
    else:
        encode._set_combo_data(encode._mode_combo, "size")
        encode._size_edit.setText("4000")
        target = encode._mode_combo
    settle(.3)
    for scroll in encode._tabs.currentWidget().findChildren(QScrollArea):
        scroll.ensureWidgetVisible(target, 0, 180)
    current = encode._tabs.currentWidget()
    if isinstance(current, QScrollArea):
        current.ensureWidgetVisible(target, 0, 180)
    win.log_requested.emit("INFO", text("Données de démonstration · aucun encodage exécuté.",
                                        "Demonstration data · no encoding executed."))
    settle(.15)
    save(scene, basename)
