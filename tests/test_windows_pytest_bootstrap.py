"""Le bootstrap Windows doit rester compatible avec les fixtures Qt communes."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def test_bootstrap_runs_shared_conftest_and_isolates_settings(tmp_path):
    root = Path(__file__).resolve().parents[1]
    shutil.copy2(root / "tests" / "conftest.py", tmp_path / "conftest.py")
    smoke = tmp_path / "test_bootstrap_smoke.py"
    smoke.write_text(
        """from PySide6.QtCore import QCoreApplication, QSettings


def test_shared_fixtures_and_settings(qt_app, tmp_path):
    assert QCoreApplication.instance() is qt_app
    format = QSettings.Format.IniFormat
    scope = QSettings.Scope.UserScope
    first = QSettings(format, scope, "Muxiveo", "Bootstrap smoke")
    first.setValue("smoke/value", "saved")
    assert QSettings(format, scope, "Muxiveo", "Bootstrap smoke").value("smoke/value") == "saved"
    QSettings.setPath(format, scope, str(tmp_path / "other-settings"))
    assert QSettings(format, scope, "Muxiveo", "Bootstrap smoke").value("smoke/value") is None
    assert first.value("smoke/value") == "saved"
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(root / "scripts" / "windows_pytest_bootstrap.py"), str(smoke)],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
