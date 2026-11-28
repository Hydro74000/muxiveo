"""Barre « Fichier de sortie » (états du champ) et résumé de l'opération Remux & Encode."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from PySide6.QtWidgets import QComboBox, QLineEdit, QPushButton, QScrollArea, QWidget

from core.workflows.encode.models import AudioTrackSettings, EncodeConfig, QualityMode, VideoEncodeSettings
from core.workflows.remux_models import RemuxConfig
from ui.operation_summary import media_operation_summary
from ui.panels.remux_panel.widgets.output_bar import OutputBar, ScrollFade


def _bar() -> tuple[OutputBar, QLineEdit]:
    edit = QLineEdit()
    return OutputBar(edit, QPushButton("Choisir…"), QComboBox()), edit


def test_state_follows_destination(qt_app, tmp_path, french_messages):
    bar, edit = _bar()
    bar.refresh_state()
    assert bar.state == "normal"            # aucune source : champ vide sans alerte
    bar.set_required(True)
    assert bar.state == "empty"
    edit.setText(str(tmp_path / "sortie.mkv"))
    bar.refresh_state()
    assert bar.state == "normal"
    (tmp_path / "sortie.mkv").write_bytes(b"")
    bar.refresh_state()
    assert bar.state == "exists"
    assert "remplacement" in edit.toolTip()
    edit.setText(str(tmp_path / "absent" / "sortie.mkv"))
    bar.refresh_state()
    assert bar.state == "missing_dir"
    assert str(tmp_path / "absent") in edit.toolTip()


def test_typing_refresh_is_deferred(qt_app, tmp_path):
    bar, edit = _bar()
    (tmp_path / "sortie.mkv").write_bytes(b"")
    edit.setText(str(tmp_path / "sortie.mkv"))
    assert bar.state == "normal"            # aucune sonde par caractère
    assert bar._state_timer.isActive()
    bar._state_timer.timeout.emit()
    assert bar.state == "exists"


def test_fade_hidden_without_content_below(qt_app):
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    content = QWidget()
    content.setMinimumHeight(2000)
    scroll.setWidget(content)
    scroll.resize(400, 300)
    fade = ScrollFade(scroll)
    scroll.show()
    qt_app.processEvents()
    assert fade.isVisible()
    bar = scroll.verticalScrollBar()
    bar.setValue(bar.maximum())
    assert not fade.isVisible()
    scroll.close()


def test_summary_remux_only(french_messages):
    remux = RemuxConfig(sources=[SimpleNamespace(), SimpleNamespace()], output=Path("/o.mkv"),
                        track_order=[(0, 0), (0, 1), (1, 1)])
    assert media_operation_summary(remux, None) == "2 sources · 3 pistes · sans réencodage"


def test_summary_encode(french_messages):
    remux = RemuxConfig(sources=[SimpleNamespace()], output=Path("/o.mkv"), track_order=[(0, 0), (0, 1)])
    x265 = VideoEncodeSettings(codec="libx265", crf=18)
    encode = EncodeConfig(source=Path("/s.mkv"), output=Path("/o.mkv"), video_tracks=[x265, x265],
                          audio_tracks=[AudioTrackSettings(1, codec="aac"), AudioTrackSettings(2)])
    assert media_operation_summary(remux, encode) == (
        "1 source · 2 pistes · vidéo x265 — HEVC CRF 18 · 1 piste audio réencodée")
    nvenc = VideoEncodeSettings(codec="hevc_nvenc", quality_mode=QualityMode.SIZE, rate_control="size")
    encode = EncodeConfig(source=Path("/s.mkv"), output=Path("/o.mkv"), video_tracks=[nvenc], target_size_mb=8000)
    assert media_operation_summary(None, encode) == "vidéo NVENC — HEVC 8000 Mo · audio copié"
    copied = VideoEncodeSettings(codec="copy", inject_hdr_meta=True)      # copie avec HDR10 traité
    encode = EncodeConfig(source=Path("/s.mkv"), output=Path("/o.mkv"), video_tracks=[copied],
                          audio_tracks=[AudioTrackSettings(1)])
    assert media_operation_summary(remux, encode) == "1 source · 2 pistes · sans réencodage"
