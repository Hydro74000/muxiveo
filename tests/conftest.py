"""
tests/conftest.py — Fixtures partagées entre tous les fichiers de test.

Crée une QApplication de session (nécessaire pour les tests de widgets Qt).
QApplication étant une sous-classe de QCoreApplication, les tests qui
n'utilisaient que QCoreApplication continuent de fonctionner.
"""
from __future__ import annotations

import sys

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call():
    """Traite les deleteLater() du test tant que ses fixtures (widgets parents) vivent encore.

    Sans boucle Qt, ces destructions restent en file jusqu'au premier exec()
    d'un test ultérieur, après le GC des parents : plantage (SIGBUS/SIGSEGV).
    """
    try:
        return (yield)
    finally:
        if QCoreApplication.instance() is not None:
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


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
