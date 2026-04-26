from __future__ import annotations

import subprocess
from unittest.mock import patch

from core.subprocess_utils import (
    decode_subprocess_output,
    subprocess_text_kwargs,
    subprocess_windows_no_window_kwargs,
)


def test_subprocess_text_kwargs_forces_utf8_on_windows():
    with patch("core.subprocess_utils.sys.platform", "win32"):
        kwargs = subprocess_text_kwargs()

    assert kwargs["text"] is True
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["errors"] == "replace"
    if hasattr(subprocess, "CREATE_NO_WINDOW") or hasattr(subprocess, "STARTUPINFO"):
        assert "creationflags" in kwargs or "startupinfo" in kwargs


def test_subprocess_text_kwargs_off_windows_includes_devnull_stdin():
    # stdin=DEVNULL est appliqué partout : empêche les outils externes
    # d'altérer le tty parent (terminal sans echo après fermeture).
    with patch("core.subprocess_utils.sys.platform", "linux"):
        kwargs = subprocess_text_kwargs()

    assert kwargs == {"text": True, "stdin": subprocess.DEVNULL}


def test_subprocess_windows_no_window_kwargs_off_windows_only_devnull():
    with patch("core.subprocess_utils.sys.platform", "linux"):
        kwargs = subprocess_windows_no_window_kwargs()

    assert kwargs == {"stdin": subprocess.DEVNULL}


def test_decode_subprocess_output_reads_utf8_text():
    assert decode_subprocess_output("Français".encode("utf-8")) == "Français"
