"""Binding Qt sain : les méthodes sans valeur de retour ne décrémentent pas None.

PySide6 6.12.0 rend None sans référence (Python < 3.12, où None n'est pas immortel) : chaque appel
décrémente son compteur jusqu'à « deallocating None » et l'abandon du processus (application et
tests). Version exclue sur ces Python dans requirements.txt ; ce test signale une version défectueuse installée.
"""

from __future__ import annotations

import sys

import pytest


@pytest.mark.skipif(sys.version_info >= (3, 12), reason="None immortel à partir de Python 3.12")
def test_void_methods_keep_none_refcount(qt_app):
    from PySide6 import __version__ as pyside_version
    from PySide6.QtWidgets import QWidget

    widget = QWidget()
    widget.setToolTip("x")
    before = sys.getrefcount(None)
    for _ in range(100):
        widget.setToolTip("x")
    lost = before - sys.getrefcount(None)
    widget.deleteLater()
    assert lost < 50, f"PySide6 {pyside_version} décrémente None à chaque appel ({lost} / 100) : version à exclure"
