"""Le bootstrap Windows doit rester compatible avec les fixtures Qt communes."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def test_bootstrap_runs_shared_conftest_and_isolates_settings(tmp_path):
    root = Path(__file__).resolve().parents[1]
    shutil.copy2(root / "tests" / "conftest.py", tmp_path / "conftest.py")
    smoke = tmp_path / "test_bootstrap_smoke.py"
    smoke.write_text(
        """from PySide6.QtCore import QCoreApplication, QSettings, QTimer, Qt
from PySide6.QtGui import QColor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QPushButton


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


def test_real_widgets_and_queued_signals(qt_app, qtbot):
    assert QCoreApplication.instance() is qt_app
    assert QColor("red").isValid()
    button = QPushButton("Bootstrap smoke")
    qtbot.addWidget(button)
    with qtbot.waitSignal(button.clicked, timeout=1000):
        QTimer.singleShot(0, lambda: QTest.mouseClick(button, Qt.MouseButton.LeftButton))
""",
        encoding="utf-8",
    )
    # Reproduit le chargement automatique de pytest-qt dans le gate Linux,
    # même si l'environnement qui exécute ce test désactive les plugins.
    env = os.environ.copy()
    env.pop("PYTEST_DISABLE_PLUGIN_AUTOLOAD", None)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTEST_QT_API"] = "pyside6"
    result = subprocess.run(
        [sys.executable, str(root / "scripts" / "windows_pytest_bootstrap.py"), str(smoke)],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
