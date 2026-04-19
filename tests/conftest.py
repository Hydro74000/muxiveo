"""
tests/conftest.py — Fixtures partagées entre tous les fichiers de test.

Crée une QApplication de session (nécessaire pour les tests de widgets Qt).
QApplication étant une sous-classe de QCoreApplication, les tests qui
n'utilisaient que QCoreApplication continuent de fonctionner.
"""
from __future__ import annotations

import sys

import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication


def _ensure_qapplication() -> QApplication:
    """
    Garantit qu'une QApplication (pas juste une QCoreApplication) existe.

    Certains tests instancient une QCoreApplication à l'import (ex.
    test_runner.py). Si l'ordre de collecte place ces modules avant les
    tests de widgets (ex. test_nfo_generation.py), QWidget crashe
    faute de QGuiApplication. On force donc la création d'une
    QApplication au tout début de la session.
    """
    existing = QCoreApplication.instance()
    if isinstance(existing, QApplication):
        return existing
    if existing is not None:
        # Une QCoreApplication "nue" est déjà installée : on ne peut pas la
        # promouvoir. Dans ce cas on s'arrange pour que conftest soit importé
        # en premier (grâce à _qapp_session autouse), donc ce chemin ne doit
        # jamais être atteint en CI.
        raise RuntimeError(
            "QCoreApplication déjà créée avant conftest : widget Qt impossible."
        )
    return QApplication(sys.argv)


# autouse + scope=session : s'exécute une seule fois, avant tout autre test,
# et fournit une QApplication valide pour les widgets.
@pytest.fixture(scope="session", autouse=True)
def _qapp_session():
    app = _ensure_qapplication()
    yield app


@pytest.fixture(scope="session")
def qt_app(_qapp_session):
    """Alias explicite pour les tests qui demandent la QApplication."""
    return _qapp_session
