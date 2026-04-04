from __future__ import annotations

from unittest.mock import patch

from core.subprocess_utils import decode_subprocess_output, subprocess_text_kwargs


def test_subprocess_text_kwargs_forces_utf8_on_windows():
    with patch("core.subprocess_utils.sys.platform", "win32"):
        kwargs = subprocess_text_kwargs()

    assert kwargs == {"text": True, "encoding": "utf-8", "errors": "replace"}


def test_subprocess_text_kwargs_keeps_default_behavior_off_windows():
    with patch("core.subprocess_utils.sys.platform", "linux"):
        kwargs = subprocess_text_kwargs()

    assert kwargs == {"text": True}


def test_decode_subprocess_output_reads_utf8_text():
    assert decode_subprocess_output("Français".encode("utf-8")) == "Français"
