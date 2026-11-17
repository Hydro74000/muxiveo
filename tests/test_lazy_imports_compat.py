"""Python 3.15 (PEP 810) + PySide6 : exécuteur de concurrent.futures utilisable depuis un fil secondaire."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_thread_pool_executor_is_usable_from_a_worker_thread_after_pyside6():
    """Import dans un fil secondaire après PySide6 (synchro audio du panneau Conteneur) : la classe, jamais l'objet
    paresseux non résolu."""
    code = (
        "import threading\n"
        "from PySide6 import QtWidgets\n"
        "import core\n"
        "out = []\n"
        "def work():\n"
        "    from concurrent.futures import ThreadPoolExecutor\n"
        "    ThreadPoolExecutor(max_workers=1).shutdown()\n"
        "    out.append('ok')\n"
        "thread = threading.Thread(target=work)\n"
        "thread.start(); thread.join()\n"
        "print(out)\n"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120,
                            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
    assert result.stdout.strip() == "['ok']", result.stderr
