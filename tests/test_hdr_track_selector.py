"""
tests/test_hdr_track_selector.py — HDR Studio : choix de la piste vidéo de A et de B par un bouton à menu, affiché
seulement pour un fichier à plusieurs pistes vidéo ; première piste vidéo par défaut, comme en CLI.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import ui.panels.hdr_studio.panel as panel_module
from core.config import AppConfig
from tests.test_hdr_inspection import _info, _video
from tests.test_hdr_studio import _wait
from ui.panels.hdr_studio.panel import HdrStudio
from ui.panels.hdr_studio.track_selector import VideoTrackSelector


def test_selector_is_hidden_for_a_single_track_and_emits_on_choice(qt_app):
    from PySide6.QtWidgets import QWidget

    host = QWidget()
    selector = VideoTrackSelector(host)
    chosen: list[int] = []
    selector.currentIndexChanged.connect(chosen.append)
    selector.addItem("#0 · HEVC 3840x2160 · HDR10", 0)
    assert selector.isHidden() and selector.currentData() == 0  # une seule piste : rien d'affiché
    selector.addItem("#2 · H264 1920x1080 · SDR", 2)
    assert not selector.isHidden() and selector.text().endswith("#0") and selector.currentData() == 0
    assert chosen == [0]  # première piste par défaut
    selector.menu().actions()[1].trigger()
    assert selector.currentData() == 2 and chosen == [0, 1] and selector.text().endswith("#2")
    assert [action.isChecked() for action in selector.menu().actions()] == [False, True]
    selector.blockSignals(True)
    selector.clear()
    selector.blockSignals(False)
    assert selector.count() == 0 and selector.currentData() is None and selector.isHidden()
    host.deleteLater()


class _Inspector:
    tracks: dict[str, tuple[int, ...]] = {}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def inspect(self, path):
        info = _info(Path(path))
        side_data = info.video_tracks[0].raw["side_data_list"]
        info.video_tracks = [_video(index=index, side_data=side_data)
                             for index in self.tracks.get(Path(path).name, (0,))]
        return info


@pytest.fixture
def studio(qt_app, tmp_path, monkeypatch):
    monkeypatch.setattr(panel_module, "FileInspector", _Inspector)
    config = AppConfig()
    config.app_data_dir = tmp_path / "appdata"
    widget = HdrStudio(config)
    widget.show()
    yield widget
    widget.close()
    _wait(qt_app, lambda: getattr(widget, "_shutdown", None) is not None and widget._shutdown.done.is_set(), 2.0)


def _media(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.write_bytes(b"\x1a\x45\xdf\xa3")
    return path


def test_a_and_b_default_to_their_first_video_track_and_can_each_pick_another(qt_app, studio, tmp_path):
    _Inspector.tracks = {"a.mkv": (0, 1), "b.mkv": (3, 4), "seule.mkv": (0,)}
    studio.load_source(_media(tmp_path, "a.mkv"))
    assert _wait(qt_app, lambda: studio.report() is not None)
    assert studio.report().snapshot.stream_index == 0 and studio._track_combo.isVisible()
    studio._track_combo.menu().actions()[1].trigger()
    assert _wait(qt_app, lambda: studio.report().snapshot.stream_index == 1)

    studio.load_compare(_media(tmp_path, "b.mkv"))
    assert _wait(qt_app, lambda: studio._compare_info is not None and studio._compare_tab.track.count() == 2)
    assert studio._compare_tab.track.currentData() == 3  # première piste de B, jamais l'index de A
    seen: list[int] = []
    studio._compare_tab.track_requested.connect(seen.append)
    studio._compare_tab.track.menu().actions()[1].trigger()
    assert seen == [4]

    studio.load_source(_media(tmp_path, "seule.mkv"))
    assert _wait(qt_app, lambda: studio.report() is not None and studio.report().snapshot.path.endswith("seule.mkv"))
    assert not studio._track_combo.isVisible()  # piste unique : aucun sélecteur
