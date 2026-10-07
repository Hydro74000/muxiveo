"""
Tests for Dolby Vision geometry alignment and Auto-crop button in EncodePanel.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtWidgets import QMessageBox

from core.config import AppConfig
from core.inspector import FileInfo, HDRType, VideoTrack
from core.workflows.remux_models import TrackEntry
from ui.panels.encode_panel.panel import EncodePanel


_PATH_A = Path("/tmp/hobbit_test.mkv")
_COLOR = "#4f6ef7"


def _video_track(width: int = 3840, height: int = 2160) -> VideoTrack:
    return VideoTrack(
        index=0,
        codec="hevc",
        codec_long="hevc",
        width=width,
        height=height,
        frame_rate="23.976",
        bit_depth=10,
        color_space=None,
        color_primaries=None,
        color_transfer=None,
        color_matrix=None,
        hdr_type=HDRType.DOLBY_VISION,
        raw={},
    )


def _file_info(path: Path, width: int = 3840, height: int = 2160) -> FileInfo:
    return FileInfo(
        path=path,
        format="matroska",
        duration_s=7200.0,
        size_bytes=20_000_000_000,
        bit_rate=22_000_000,
        video_tracks=[_video_track(width, height)],
        hdr_type=HDRType.DOLBY_VISION,
    )


def _video_entry(tid: int = 0) -> TrackEntry:
    return TrackEntry(
        mkv_tid=tid,
        track_type="video",
        codec="HEVC",
        display_info="3840x2160",
        language="und",
        title="",
        entry_id="video-0",
    )


@pytest.fixture
def encode_panel(qt_app):
    panel = EncodePanel(AppConfig())
    panel._hw_encoders = {"nvencc_hevc"}
    panel._populate_codec_combo()
    yield panel
    panel.close()


def test_auto_crop_button_click_applies_settings(encode_panel, monkeypatch):
    entry = _video_entry(0)
    fi = _file_info(_PATH_A, 3840, 2160)
    encode_panel.set_video_tracks([(fi, entry, _COLOR)])

    # Sélectionner nvencc_hevc et copy_dv
    encode_panel._codec_combo.setCurrentIndex(
        next(i for i in range(encode_panel._codec_combo.count()) if encode_panel._codec_combo.itemData(i) == "nvencc_hevc")
    )
    encode_panel._copy_dv_cb.setChecked(True)

    with patch("core.workflows.encode.runtime.crop_detector.detect_video_crop", return_value=(280, 280, 0, 0)):
        encode_panel._auto_crop_btn.click()

    assert encode_panel._crop_enabled_cb.isChecked() is True
    assert encode_panel._crop_top_spin.value() == 280
    assert encode_panel._crop_bottom_spin.value() == 280
    assert encode_panel._crop_left_spin.value() == 0
    assert encode_panel._crop_right_spin.value() == 0
    assert encode_panel._crop_auto_cb.isChecked() is False


def test_confirm_dovi_geometry_alignment_not_needed_for_other_codecs(encode_panel):
    entry = _video_entry(0)
    fi = _file_info(_PATH_A, 3840, 2160)
    encode_panel.set_video_tracks([(fi, entry, _COLOR)])

    encode_panel._codec_combo.setCurrentIndex(
        next(i for i in range(encode_panel._codec_combo.count()) if encode_panel._codec_combo.itemData(i) == "libx265")
    )
    encode_panel._copy_dv_cb.setChecked(False)

    assert encode_panel.confirm_dovi_geometry_alignment_if_needed() is True


def test_confirm_dovi_geometry_alignment_not_needed_when_already_divisible_by_32(encode_panel):
    entry = _video_entry(0)
    fi = _file_info(_PATH_A, 3840, 1920)  # 1920 % 32 == 0
    encode_panel.set_video_tracks([(fi, entry, _COLOR)])

    encode_panel._codec_combo.setCurrentIndex(
        next(i for i in range(encode_panel._codec_combo.count()) if encode_panel._codec_combo.itemData(i) == "nvencc_hevc")
    )
    encode_panel._copy_dv_cb.setChecked(True)

    with patch("core.dovi_profile_detector.DoviProfileDetector.probe_l5_offsets", return_value=None):
        assert encode_panel.confirm_dovi_geometry_alignment_if_needed() is True


def test_confirm_dovi_geometry_alignment_prompt_accept(encode_panel):
    entry = _video_entry(0)
    fi = _file_info(_PATH_A, 3840, 2160)
    encode_panel.set_video_tracks([(fi, entry, _COLOR)])

    encode_panel._codec_combo.setCurrentIndex(
        next(i for i in range(encode_panel._codec_combo.count()) if encode_panel._codec_combo.itemData(i) == "nvencc_hevc")
    )
    encode_panel._copy_dv_cb.setChecked(True)

    # L5 offset simulant 275/275 -> alignement à 280/280
    with patch("core.dovi_profile_detector.DoviProfileDetector.probe_l5_offsets", return_value=(275, 275, 0, 0)):
        with patch.object(QMessageBox, "exec", return_value=None), \
             patch.object(QMessageBox, "clickedButton") as mock_clicked:
            # Simuler le clic sur le premier bouton (Appliquer et continuer)
            mock_clicked.side_effect = lambda: encode_panel.findChildren(QMessageBox)[-1].buttons()[0] if encode_panel.findChildren(QMessageBox) else None

            # On simule le QMessageBox en interceptant dlg.exec et dlg.clickedButton
            with patch("PySide6.QtWidgets.QMessageBox.exec"):
                with patch("PySide6.QtWidgets.QMessageBox.clickedButton") as m_btn:
                    # Renvoyer le bouton apply
                    def fake_clicked(dlg_self):
                        return dlg_self.buttons()[0]
                    m_btn.side_effect = lambda: fake_clicked

                    # Appel direct
                    with patch("ui.panels.encode_panel.panel.QMessageBox") as MockMsgBox:
                        mock_dlg = MagicMock()
                        btn_apply = MagicMock()
                        btn_cancel = MagicMock()
                        mock_dlg.addButton.side_effect = [btn_apply, btn_cancel]
                        mock_dlg.clickedButton.return_value = btn_apply
                        MockMsgBox.return_value = mock_dlg

                        confirmed = encode_panel.confirm_dovi_geometry_alignment_if_needed()

                        assert confirmed is True
                        assert encode_panel._crop_enabled_cb.isChecked() is True
                        assert encode_panel._crop_top_spin.value() == 280
                        assert encode_panel._crop_bottom_spin.value() == 280
                        assert encode_panel._tabs.currentIndex() == 2


def test_confirm_dovi_geometry_alignment_prompt_cancel(encode_panel):
    entry = _video_entry(0)
    fi = _file_info(_PATH_A, 3840, 2160)
    encode_panel.set_video_tracks([(fi, entry, _COLOR)])

    encode_panel._codec_combo.setCurrentIndex(
        next(i for i in range(encode_panel._codec_combo.count()) if encode_panel._codec_combo.itemData(i) == "nvencc_hevc")
    )
    encode_panel._copy_dv_cb.setChecked(True)

    with patch("core.dovi_profile_detector.DoviProfileDetector.probe_l5_offsets", return_value=(275, 275, 0, 0)):
        with patch("ui.panels.encode_panel.panel.QMessageBox") as MockMsgBox:
            mock_dlg = MagicMock()
            btn_apply = MagicMock()
            btn_cancel = MagicMock()
            mock_dlg.addButton.side_effect = [btn_apply, btn_cancel]
            mock_dlg.clickedButton.return_value = btn_cancel
            MockMsgBox.return_value = mock_dlg

            confirmed = encode_panel.confirm_dovi_geometry_alignment_if_needed()

            assert confirmed is False
            assert encode_panel._tabs.currentIndex() == 2


def test_resize_disables_dv_in_ui_and_leaves_crop_optional(encode_panel):
    encode_panel.set_video_tracks([(_file_info(_PATH_A), _video_entry(), _COLOR)])
    encode_panel._set_combo_data(encode_panel._codec_combo, "nvencc_hevc")
    encode_panel._copy_dv_cb.setChecked(True)
    messages = []
    encode_panel.log_message.connect(lambda level, text: messages.append(text))
    encode_panel._set_combo_data(encode_panel._resize_preset_combo, "1080p")
    encode_panel._resize_enabled_cb.setChecked(True)
    assert not encode_panel._copy_dv_cb.isChecked()
    assert not encode_panel._crop_enabled_cb.isChecked()
    assert encode_panel._inject_hdr_cb.isChecked()
    assert any("Dolby Vision désactivée" in message for message in messages)
    assert encode_panel.confirm_dovi_geometry_alignment_if_needed()


def _select_nvenc_dv(panel, codec: str = "nvencc_hevc", height: int = 2160) -> None:
    panel.set_video_tracks([(_file_info(_PATH_A, 3840, height), _video_entry(0), _COLOR)])
    panel._set_combo_data(panel._codec_combo, codec)
    panel._copy_dv_cb.setChecked(True)


class _FakeDialog:
    """QMessageBox simulé : boutons enregistrés avec leur rôle, clic choisi par le test."""

    def __init__(self, choose):
        self.buttons: list[tuple[MagicMock, object]] = []
        self.text = ""
        self._choose = choose

    def __call__(self, *_args):
        return self

    def addButton(self, _label, role):
        button = MagicMock()
        self.buttons.append((button, role))
        return button

    def setText(self, text):
        self.text = text

    def clickedButton(self):
        return self._choose(self.buttons)

    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


def _role_buttons(buttons, role):
    return [button for button, button_role in buttons if button_role == role]


@pytest.mark.parametrize("codec", ["nvencc_hevc", "hevc_nvenc"])
def test_fullframe_prompt_crops_to_2144_and_continues(encode_panel, codec):
    encode_panel._hw_encoders = {"nvencc_hevc", "hevc_nvenc"}
    encode_panel._populate_codec_combo()
    _select_nvenc_dv(encode_panel, codec)
    dialog = _FakeDialog(lambda buttons: _role_buttons(buttons, QMessageBox.ButtonRole.AcceptRole)[0])
    with patch("core.dovi_profile_detector.DoviProfileDetector.probe_l5_offsets", return_value=(0, 0, 0, 0)), \
         patch("ui.panels.encode_panel.panel.QMessageBox", side_effect=dialog) as box:
        box.ButtonRole = QMessageBox.ButtonRole
        assert encode_panel.confirm_dovi_geometry_alignment_if_needed() is True

    assert "3840" in dialog.text and "2176" in dialog.text and "2144" in dialog.text
    assert encode_panel._crop_enabled_cb.isChecked()
    assert (encode_panel._crop_top_spin.value(), encode_panel._crop_bottom_spin.value()) == (8, 8)
    assert encode_panel._tabs.currentIndex() == 2
    # Recadrage appliqué : plus de question au lancement suivant.
    with patch("core.dovi_profile_detector.DoviProfileDetector.probe_l5_offsets", return_value=(0, 0, 0, 0)), \
         patch("ui.panels.encode_panel.panel.QMessageBox") as box:
        assert encode_panel.confirm_dovi_geometry_alignment_if_needed() is True
        box.assert_not_called()


def test_fullframe_prompt_recommends_detected_encoders_and_cancels(encode_panel):
    encode_panel._sw_encoders = {"libx265"}
    encode_panel._hw_encoders = {"nvencc_hevc", "hevc_qsv", "hevc_vaapi"}
    encode_panel._populate_codec_combo()
    _select_nvenc_dv(encode_panel)
    dialog = _FakeDialog(lambda buttons: _role_buttons(buttons, QMessageBox.ButtonRole.RejectRole)[0])
    with patch("core.dovi_profile_detector.DoviProfileDetector.probe_l5_offsets", return_value=(0, 0, 0, 0)), \
         patch("ui.panels.encode_panel.panel.QMessageBox", side_effect=dialog) as box:
        box.ButtonRole = QMessageBox.ButtonRole
        assert encode_panel.confirm_dovi_geometry_alignment_if_needed() is False

    # x265, QSV, VAAPI recommandés dans le texte ; deux boutons seulement, rien n'est modifié.
    assert all(name in dialog.text for name in ("x265", "QSV", "VAAPI")) and "AMF —" not in dialog.text
    assert len(dialog.buttons) == 2
    assert encode_panel._codec_combo.currentData() == "nvencc_hevc"
    assert not encode_panel._crop_enabled_cb.isChecked()
    assert encode_panel._tabs.currentIndex() == 1


def test_fullframe_prompt_without_alternative_keeps_crop_path(encode_panel):
    encode_panel._sw_encoders = set()
    encode_panel._hw_encoders = {"nvencc_hevc"}
    encode_panel._populate_codec_combo()
    _select_nvenc_dv(encode_panel)
    dialog = _FakeDialog(lambda buttons: _role_buttons(buttons, QMessageBox.ButtonRole.RejectRole)[0])
    with patch("core.dovi_profile_detector.DoviProfileDetector.probe_l5_offsets", return_value=(0, 0, 0, 0)), \
         patch("ui.panels.encode_panel.panel.QMessageBox", side_effect=dialog) as box:
        box.ButtonRole = QMessageBox.ButtonRole
        assert encode_panel.confirm_dovi_geometry_alignment_if_needed() is False

    assert "Aucun autre encodeur" in dialog.text or "No other" in dialog.text
    assert len(_role_buttons(dialog.buttons, QMessageBox.ButtonRole.AcceptRole)) == 1
    assert not encode_panel._crop_enabled_cb.isChecked()


def test_fullframe_1080p_needs_no_prompt(encode_panel):
    _select_nvenc_dv(encode_panel, height=1080)
    with patch("core.dovi_profile_detector.DoviProfileDetector.probe_l5_offsets", return_value=(0, 0, 0, 0)), \
         patch("ui.panels.encode_panel.panel.QMessageBox") as box:
        assert encode_panel.confirm_dovi_geometry_alignment_if_needed() is True
        box.assert_not_called()
