"""Run application-wide style checks in a fresh Qt process.

The shared QApplication retains widgets from earlier UI tests. Reapplying its
stylesheet for every theme/scale combination restyles all those widget trees
and can exhaust the CI job's 40-minute timeout.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


def test_spinbox_geometry_in_fresh_application(tmp_path):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable, "-m", "pytest", "-q", "-rs",
            "tests/qt_cases/spinbox_geometry.py",
            "-W", "error::pytest.PytestUnhandledThreadExceptionWarning",
            "-o", "faulthandler_timeout=30",
            "-o", f"cache_dir={tmp_path / 'pytest-cache'}",
        ],
        cwd=root,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
