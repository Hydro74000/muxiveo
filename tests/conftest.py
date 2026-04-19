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


@pytest.fixture(scope="session")
def qt_app():
    """
    QApplication partagée (widgets). Créée uniquement à la demande : les
    workflows CI sans deps Qt complètes (ex. encode-integration) peuvent
    exécuter leurs tests sans jamais instancier QApplication.
    """
    existing = QCoreApplication.instance()
    if isinstance(existing, QApplication):
        return existing
    if existing is not None:
        raise RuntimeError(
            "QCoreApplication déjà créée sans être une QApplication : "
            "impossible d'instancier des widgets Qt."
        )
    return QApplication(sys.argv)
