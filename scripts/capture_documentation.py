#!/usr/bin/env python3
"""Render the real Qt widgets with isolated demonstration data for the docs.

No media workflow, network request, user setting or installed plugin is changed.
The child process owns a temporary configuration and a fresh QApplication per locale.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

# Existing public image URLs keep their meaning and numbering.
SCENES = {
    "fr": {
        "01": "dashboard", "02": "container-empty", "03": "tracks",
        "04": "search", "05": "tags", "06": "chapters", "07": "audio",
        "08": "encoding", "09": "merge", "10": "running",
        "11": "hybrid", "12": "waveform", "13": "cuts", "14": "profile",
    },
    "en": {
        "01": "container-light", "02": "track-edit", "03": "tags",
        "04": "chapters", "05": "audio", "06": "encoding", "07": "merge",
        "08": "dashboard", "09": "hybrid", "10": "waveform",
        "11": "cuts", "12": "profile",
    },
}
EXTRA_SCENES = ("settings", "plugins", "operations")


def render(locale: str, destination: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="muxiveo-documentation-") as temp:
        scratch = Path(temp)
        for variable in ("MUXIVEO_CONFIG_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
                         "XDG_CACHE_HOME", "APPDATA"):
            os.environ[variable] = str(scratch / variable)
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        sys.path.insert(0, str(ROOT))
        _render_widgets(locale, destination, scratch)


def _render_widgets(locale: str, destination: Path, scratch: Path) -> None:
    import numpy as np
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QFont, QPainter
    from PySide6.QtWidgets import (
        QApplication, QLineEdit, QPlainTextEdit, QScrollArea, QVBoxLayout, QWidget,
    )
    from core.config import AppConfig
    from core.i18n import apply_translations, set_current_language
    from core.inspector import (
        AudioTrack, ChapterEntry, ChapterInfo, FileInfo, HDRType, SubtitleTrack, VideoTrack,
    )
    from core.media_info_fetcher import MediaSearchResult
    from core.profiles.decision import DecisionProfileManager
    from core.workflows.hybrid_matrix import MatrixEpisode
    from core.workflows.merge_dovi import HdrState
    from core.workflows.sync_calibration import SyncCalibration, SyncSegment
    from ui.design_system import DesignSystem
    from ui.main_window import DashboardPage, MainWindow
    from ui.panels.encode_panel.widgets import _AudioSourceDialog
    from ui.panels.remux_panel.models import SourceFile, _pick_file_color
    from ui.panels.remux_panel.profile_editor import DecisionProfileEditorDialog, blank_decision_profile
    from ui.panels.remux_panel.widgets.attachments import _TagEditDialog
    from ui.panels.remux_panel.widgets.chapters import _AddChapterDialog
    from ui.panels.remux_panel.widgets.sync_studio_dialog import SyncStudioDialog
    from ui.panels.tmdb_search_modal import TmdbSearchModal
    from ui.panels.track_edit_dialog import TrackEditDialog

    app = QApplication([])
    app.setFont(QFont("Noto Sans", 10))
    language = "fra" if locale == "fr" else "eng"
    set_current_language(language)
    DesignSystem.set_ui_scale(100)
    destination.mkdir(parents=True, exist_ok=True)
    is_fr = locale == "fr"

    def text(fr: str, en: str) -> str:
        return fr if is_fr else en

    def settle(seconds: float = 0.12) -> None:
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            app.processEvents()
            time.sleep(0.01)

    reference = Path("/media/Sources/Voyage.S01E01.2160p.HDR10.mkv")
    donor = Path("/media/Sources/Voyage.S01E01.MULTi.1080p.mkv")
    tags = {
        "TITLE": text("Voyage — Le départ", "Voyage — Departure"),
        "DATE_RELEASED": "2026", "GENRE": text("Aventure", "Adventure"),
        "SYNOPSIS": text("Une équipe part explorer de nouveaux horizons.",
                         "A team sets out to explore new horizons."),
        "SEASON": "1", "EPISODE": "1", "LANGUAGE": "fra / eng",
    }
    video = VideoTrack(0, "hevc", "H.265 / HEVC", 3840, 2160, "24000/1001", 10,
                       "yuv420p10le", "bt2020", "smpte2084", "bt2020nc", HDRType.HDR10,
                       duration_s=2700)
    reference_info = FileInfo(
        reference, "matroska,webm", 2700, 8_100_000_000, 24_000_000,
        video_tracks=[video],
        audio_tracks=[AudioTrack(1, "eac3", "Dolby Digital Plus", 6, "5.1", 48000,
                                 640000, "eng", "Original", duration_s=2700)],
        subtitle_tracks=[SubtitleTrack(2, "subrip", "eng", "English SDH")],
        chapters=ChapterInfo([ChapterEntry(i * 300, f"{text('Chapitre', 'Chapter')} {i+1}")
                              for i in range(9)]),
        hdr_type=HDRType.HDR10, title=tags["TITLE"], global_tags=tags, tag_count=len(tags),
    )
    donor_info = FileInfo(
        donor, "matroska,webm", 2700, 2_700_000_000, 8_000_000,
        audio_tracks=[AudioTrack(1, "eac3", "Dolby Digital Plus", 6, "5.1", 48000,
                                 640000, "fra", "Français 5.1", duration_s=2700),
                      AudioTrack(2, "aac", "AAC LC", 2, "stereo", 48000,
                                 192000, "fra", "Français stéréo", duration_s=2700)],
        subtitle_tracks=[SubtitleTrack(3, "subrip", "fra", "Français forcés", forced=True),
                         SubtitleTrack(4, "subrip", "fra", "Français complets")],
    )

    def new_window(theme: str = "dark"):
        DesignSystem.set_theme(theme)
        DesignSystem.apply_to_application(app)
        cfg = AppConfig()
        cfg.check_updates = False
        cfg.language = language
        cfg.theme = theme
        cfg.startup_menu_compact = False
        # All controller probes are disabled for reproducible offline rendering.
        with patch.object(MainWindow, "_wire_extensions", lambda self: None), \
             patch.object(DashboardPage, "_start_hw_detection", lambda self: None), \
             patch.object(DashboardPage, "start_accel_detection", lambda self: None):
            win = MainWindow(cfg)
        win.resize(1600, 1080)
        win._remux_panel._preview_timer.timeout.disconnect()
        win._encode_panel._command_timer.timeout.disconnect()
        win._dashboard._run_sw_detection()
        win._dashboard._on_hw_detected(set())
        win._dovi_panel._refresh_framecounts = lambda: None
        win.show()
        settle()
        return win

    win = new_window()
    panel = win._remux_panel
    h = win._hybrid_panel
    h.reference.setText("/media/Sources/Voyage.S01.2160p.HDR10")
    h.donor.setText("/media/Sources/Voyage.S01.MULTi.1080p")
    h.output.setText("/media/Sortie/Voyage.S01" if is_fr else "/media/Output/Voyage.S01")
    h.matrix = h._build_matrix()
    for number in range(1, 9):
        episode = MatrixEpisode(
            season=1, episode=number,
            master_file=reference.with_name(f"Voyage.S01E{number:02}.2160p.mkv"),
            donor_files=[(h.matrix.sources[1], donor.with_name(f"Voyage.S01E{number:02}.1080p.mkv"))],
        )
        h.matrix_episodes.append(episode)
        h._insert_table_row(number-1, episode)
        h.table.item(number-1, 3).setText(text("Prêt · +125 ms", "Ready · +125 ms"))
    h._update_run_button()
    dovi = win._dovi_panel
    dovi._file_section._film1, dovi._file_section._film2 = reference, donor
    for edit, path in zip(dovi._file_section.findChildren(QLineEdit), (reference, donor)):
        edit.setText(str(path))
    dovi._config_section.set_hdr_states(HdrState(transfer="pq"), HdrState(has_dovi=True, has_hdr10plus=True))

    def save(scene: str, basename: str, widget=None, overlay=None) -> None:
        win._operation_refresh_timer.stop()
        win._refresh_operation_bar()
        if widget is None:
            widget = win
        if widget is not win:
            apply_translations(widget)
        settle()
        win._refresh_operation_bar()
        pixmap = widget.grab()
        if overlay is not None:
            painter = QPainter(pixmap)
            position = overlay.mapToGlobal(QPoint()) - widget.mapToGlobal(QPoint())
            painter.drawPixmap(position, overlay.grab())
            painter.end()
        image = pixmap.toImage()
        image.setText("Description", "Muxiveo native Qt interface; demonstration data; " + scene)
        image.setText("Language", locale)
        target = destination / basename
        if not image.save(str(target)):
            raise RuntimeError(f"Cannot save {target}")
        thumbs = destination / "thumbs"
        thumbs.mkdir(exist_ok=True)
        image.scaled(640, 432, Qt.AspectRatioMode.KeepAspectRatio,
                     Qt.TransformationMode.SmoothTransformation).save(str(thumbs / basename))
        print(f"{locale}: {basename} — {scene} ({image.width()}×{image.height()})", flush=True)

    populated = False

    def populate():
        nonlocal populated
        if populated:
            return
        for i, info in enumerate((reference_info, donor_info)):
            identity, color = f"documentation-source-{i}", _pick_file_color(i)
            source = SourceFile(identity, info.path, color)
            panel._source_files.append(source)
            panel._source_names[identity] = info.path.name
            panel._source_colors[identity] = color
            panel._file_list.add_file(source)
            panel._apply_inspection(identity, info)
        panel._output_edit.setText("/media/Sortie/Voyage.S01E01.mkv" if is_fr else "/media/Output/Voyage.S01E01.mkv")
        panel._workflow_options["sync_mode"] = "physical"
        panel._rebuild_preview()
        encode = win._encode_panel
        encode._codec_combo.setCurrentIndex(encode._codec_combo.findData("copy"))
        populated = True
        settle()

    def page(index: int):
        win._stack.setCurrentIndex(index)
        win._refresh_operation_bar()
        for scroll in win._stack.currentWidget().findChildren(QScrollArea):
            scroll.verticalScrollBar().setValue(0)
        win._log_panel.clear()
        win._log_panel.set_collapsed(False)
        win._on_log_collapsed(False)
        win.log_requested.emit("INFO", text("Données de démonstration · documentation de l’interface.",
                                            "Demonstration data · interface documentation."))
        win._log_panel._flush_pending()
        settle()

    def dialog_capture(dialog, scene, basename, size=None):
        if size:
            dialog.resize(*size)
        apply_translations(dialog)
        dialog.show()
        settle()
        save(scene, basename, dialog)
        dialog.close()
        settle()

    scenes = {**SCENES[locale], **{name: name for name in EXTRA_SCENES}}
    for key, scene in scenes.items():
        basename = f"Muxiveo-{locale}-{key}.png"
        if scene == "container-empty":
            page(3)
            save(scene, basename)
            continue
        if scene == "dashboard":
            page(0)
            save(scene, basename)
            continue
        populate()
        if scene in ("tracks", "track-edit"):
            page(3)
            if scene == "track-edit":
                dialog_capture(TrackEditDialog(panel._source_files[1].tracks[0], win), scene, basename)
            else:
                save(scene, basename)
        elif scene == "search":
            page(3)
            with patch.object(TmdbSearchModal, "_on_search", lambda self: None):
                dialog = TmdbSearchModal(win._config, suggested_title="Voyage", parent=win)
            dialog._on_results([MediaSearchResult(0, "Voyage", "2026", "tv",
                                                 text("Exemple de résultat de recherche.", "Example search result."))])
            dialog_capture(dialog, scene, basename, (780, 650))
        elif scene == "tags":
            page(3)
            dialog_capture(_TagEditDialog(tags, win), scene, basename, (900, 600))
        elif scene == "chapters":
            page(3)
            dialog = _AddChapterDialog(win)
            dialog._tc_edit.setText("00:35:00.000")
            dialog._name_edit.setText(text("Un nouvel horizon", "A new horizon"))
            dialog_capture(dialog, scene, basename, (600, 260))
        elif scene in ("audio", "encoding"):
            page(2)
            encode = win._encode_panel
            encode._codec_combo.setCurrentIndex(encode._codec_combo.findData("libx265"))
            encode._tabs.setCurrentIndex(0 if scene == "audio" else 1)
            if scene == "audio":
                dialog = _AudioSourceDialog([(track, "#8ebcf1", info.path)
                                            for info in (reference_info, donor_info) for track in info.audio_tracks],
                                           win._config, win)
                dialog._codec_combo.setCurrentIndex(dialog._codec_combo.findData("ac3"))
                dialog_capture(dialog, scene, basename, (820, 550))
            else:
                save(scene, basename)
            encode._codec_combo.setCurrentIndex(encode._codec_combo.findData("copy"))
            encode._tabs.setCurrentIndex(0)
        elif scene == "merge":
            page(1)
            save(scene, basename)
        elif scene == "running":
            page(3)
            win._op_mode = "remux"
            win._running = True
            win._prog_widget.show()
            win._prog_bar.setRange(0, 100)
            win._prog_bar.setValue(62)
            win._prog_lbl.setText(text("Assemblage Matroska · 62 % · données de démonstration",
                                       "Matroska assembly · 62% · demonstration data"))
            win.log_requested.emit("INFO", "[MEDIA] " + text("Synchronisation physique de l’audio français : +125 ms.",
                                                            "Physical synchronization of French audio: +125 ms."))
            win.log_requested.emit("OK", "[MEDIA] " + text("Préparation des pistes terminée.", "Track preparation complete."))
            win._log_panel._flush_pending()
            save(scene, basename)
            win._running = False
            win._op_mode = ""
        elif scene in ("hybrid", "operations"):
            page(4)
            win.log_requested.emit("OK", "[HYBRID] " + text("8 épisodes appariés · référence HDR10 et donneurs MULTi.",
                                                            "8 paired episodes · HDR10 reference and MULTi donors."))
            win._log_panel._flush_pending()
            if scene == "operations":
                win._refresh_operation_bar()
                win._action_bar.open_menu()
                menu = win._action_bar.menu
                menu.move(win._run_btn.mapToGlobal(QPoint(win._run_btn.width()-menu.sizeHint().width(),
                                                         -menu.sizeHint().height()-10)))
                settle()
                save(scene, basename, overlay=menu)
                menu.close()
            else:
                save(scene, basename)
        elif scene in ("waveform", "cuts"):
            page(4)
            segments = ((SyncSegment(0, -125), SyncSegment(900000, -625), SyncSegment(1800000, -375))
                        if scene == "cuts" else (SyncSegment(0, -125),))
            with patch.object(SyncStudioDialog, "_load_segment_audio_async", lambda *args: None):
                dialog = SyncStudioDialog(panel._source_files[1].tracks[0], donor, 1,
                                          panel._source_files[0].tracks[1], reference, 1,
                                          calibration=SyncCalibration(segments, confidence=0.98), parent=win)
            rng = np.random.default_rng(7)
            samples = np.arange(8 * 16000) / 16000
            envelope = sum(np.exp(-((samples-position)/0.11)**2) for position in (0.5, 1.2, 2.1, 3.7, 4.2, 5.6, 6.5, 7.2))
            audio = (envelope * (0.45*np.sin(2*np.pi*180*samples) + rng.normal(0, 0.12, len(samples)))).astype(np.float32)
            dialog._apply_audio_to_waveform(0, audio, np.roll(audio, 2000), 0, None)
            dialog._on_duration_ready(2400)
            dialog_capture(dialog, scene, basename, (1100, 790))
        elif scene == "profile":
            page(3)
            profile = blank_decision_profile()
            profile.update(name="BestOfAll", description=text("Vidéo de référence · VF et VO · sous-titres français",
                                                              "Reference video · French and original audio · French subtitles"))
            dialog = DecisionProfileEditorDialog(manager=DecisionProfileManager(scratch / "profiles"),
                                                  profile=profile, current_config=panel.collect_config(),
                                                  current_tracks=[track for source in panel._source_files for track in source.tracks],
                                                  parent=win)
            for kind in ("video", "language", "no_commentary", "variant", "order"):
                dialog._add_template_rule(kind)
            dialog._rule_list.setCurrentRow(1)
            dialog_capture(dialog, scene, basename, (1400, 920))
        elif scene in ("settings", "plugins"):
            page(5 if scene == "settings" else 6)
            save(scene, basename)
        elif scene == "container-light":
            # Build fresh widgets so every local stylesheet uses the light palette.
            old_win = win
            win = new_window("light")
            panel = win._remux_panel
            populated = False
            populate()
            page(3)
            save(scene, basename)
            win.close()
            settle()
            win = old_win
            panel = win._remux_panel
            populated = True
            DesignSystem.set_theme("dark")
            DesignSystem.apply_to_application(app)

    # Real command output, rather than a fabricated successful processing log.
    terminal = QWidget()
    terminal.setWindowTitle("Muxiveo CLI")
    terminal.resize(1280, 900)
    terminal.setStyleSheet("background:#0d0f14;color:#e8ecf4;")
    layout = QVBoxLayout(terminal)
    output = QPlainTextEdit()
    output.setReadOnly(True)
    output.setFont(QFont("DejaVu Sans Mono", 11))
    output.setStyleSheet("border:none;padding:18px;")
    result = subprocess.run([sys.executable, str(ROOT / "main.py"), "--cli", "hybrid", "--help"],
                            cwd=ROOT, capture_output=True, text=True, check=True, timeout=30)
    output.setPlainText("$ muxiveo --cli hybrid --help\n\n" + result.stdout)
    layout.addWidget(output)
    terminal.show()
    save("cli-help", "Muxiveo-cli-terminal.png", terminal)
    terminal.close()
    win._running = False
    win.close()
    settle(2)
    app.shutdown()


def publish(site: Path, wiki: Path) -> None:
    records = []
    for locale in SCENES:
        destination = site / "assets/img/screenshots" / locale
        subprocess.run([sys.executable, __file__, "--render-locale", locale,
                        "--destination", str(destination)], check=True, cwd=ROOT)
        for path in sorted(destination.glob("*.png")):
            records.append({"locale": locale, "file": path.name,
                            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
            if locale == "fr":
                for target_dir in (wiki / "assets/img", ROOT / "docs/assets/img"):
                    target_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(path, target_dir / path.name)
    (ROOT / "docs/screenshots.json").write_text(json.dumps({
        "source": "Native PySide6 widgets; isolated demonstration data; no media workflow executed",
        "scenes": SCENES, "extra_scenes": EXTRA_SCENES, "images": records,
    }, ensure_ascii=False, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site-root", type=Path, default=ROOT.parent / "muxiveo_web")
    parser.add_argument("--wiki-root", type=Path, default=ROOT.parent / "mediarecode.wiki")
    parser.add_argument("--render-locale", choices=SCENES)
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    if args.render_locale:
        if args.destination is None:
            parser.error("--destination is required with --render-locale")
        render(args.render_locale, args.destination)
    else:
        for directory in (args.site_root, args.wiki_root):
            if not directory.is_dir():
                parser.error(f"Missing repository: {directory}")
        publish(args.site_root, args.wiki_root)


if __name__ == "__main__":
    main()
