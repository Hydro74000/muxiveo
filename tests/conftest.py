"""
tests/conftest.py — Fixtures partagées entre tous les fichiers de test.

Crée une QApplication de session (nécessaire pour les tests de widgets Qt).
QApplication étant une sous-classe de QCoreApplication, les tests qui
n'utilisaient que QCoreApplication continuent de fonctionner.
"""
from __future__ import annotations

import os
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

    Seule source d'instance Qt de la suite : un module qui créerait sa propre
    QCoreApplication rendrait impossibles les tests de widgets exécutés après.
    """
    existing = QCoreApplication.instance()
    if isinstance(existing, QApplication):
        return existing
    if existing is not None:
        raise RuntimeError(
            "QCoreApplication déjà créée sans être une QApplication : "
            "impossible d'instancier des widgets Qt."
        )
    # Sans affichage, QApplication avorte (qFatal) au lieu de lever une exception.
    if (
        sys.platform.startswith("linux")
        and not os.environ.get("DISPLAY")
        and not os.environ.get("WAYLAND_DISPLAY")
    ):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication(sys.argv)


@pytest.fixture(autouse=True)
def _no_vulkan_probe_by_default(monkeypatch):
    """Commandes indépendantes de la machine : aucun GPU Vulkan sondé par le workflow.

    Les tests Vulkan injectent leur capacité (``set_vulkan_capability``) ; le test
    sur outils réels appelle ``core.workflows.encode.vulkan.detect_vulkan`` directement.
    """
    from core.workflows.encode import workflow as workflow_module
    from core.workflows.encode.vulkan import VulkanCapability

    monkeypatch.setattr(workflow_module, "_detect_vulkan", lambda _ffmpeg: VulkanCapability(reason="tests"))


@pytest.fixture(autouse=True)
def _no_nvof_probe_by_default(monkeypatch):
    """Tableau de bord : aucun ``muxiveo-rife --list-gpus`` réel (sonde CUDA) pendant les tests."""
    from core.workflows.encode import interpolation

    monkeypatch.setattr(
        interpolation, "detect_nvof", lambda _rife_bin, **_kw: interpolation.NvofCapability(reason="tests")
    )


@pytest.fixture(autouse=True)
def _isolated_output_locks(monkeypatch, tmp_path_factory):
    """Verrous de destination des workflows dans un dossier de test, jamais dans le cache utilisateur."""
    from core import output_commit

    lock_dir = tmp_path_factory.mktemp("output-locks")
    monkeypatch.setattr(output_commit, "default_lock_dir", lambda: lock_dir)
