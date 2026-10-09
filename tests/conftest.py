"""
tests/conftest.py — Fixtures partagées entre tous les fichiers de test.

Crée une QApplication de session (nécessaire pour les tests de widgets Qt).
QApplication étant une sous-classe de QCoreApplication, les tests qui
n'utilisaient que QCoreApplication continuent de fonctionner.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QSettings
from PySide6.QtWidgets import QApplication

_SETTINGS_DIR = ""
_SETTINGS_ENV: dict[str, str | None] = {}


def pytest_configure(config):
    """Réglages QSettings (IniFormat, portée utilisateur) dans un dossier jetable : un test qui appelle
    ``AppConfig.save()`` ou ferme la fenêtre principale ne touche jamais les réglages réels."""
    global _SETTINGS_DIR
    _SETTINGS_DIR = tempfile.mkdtemp(prefix="muxiveo-tests-settings-")
    # setPath ne concerne que ce processus. config.ini et les QSettings des
    # sous-processus CLI doivent aussi rester dans le dossier de test.
    for name in ("XDG_CONFIG_HOME", "APPDATA", "MUXIVEO_CONFIG_HOME"):
        _SETTINGS_ENV[name] = os.environ.get(name)
        os.environ[name] = _SETTINGS_DIR
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, _SETTINGS_DIR)


def pytest_unconfigure(config):
    for name, value in _SETTINGS_ENV.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    _SETTINGS_ENV.clear()
    if _SETTINGS_DIR:
        shutil.rmtree(_SETTINGS_DIR, ignore_errors=True)


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
        app = existing
    elif existing is not None:
        raise RuntimeError(
            "QCoreApplication déjà créée sans être une QApplication : "
            "impossible d'instancier des widgets Qt."
        )
    else:
        # Sans affichage, QApplication avorte (qFatal) au lieu de lever une exception.
        if (
            sys.platform.startswith("linux")
            and not os.environ.get("DISPLAY")
            and not os.environ.get("WAYLAND_DISPLAY")
        ):
            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app = QApplication(sys.argv)
    yield app
    # Détruire Qt tant que les classes et callbacks Python sont encore vivants,
    # plutôt que laisser Shiboken parcourir les widgets pendant Py_Finalize.
    app.closeAllWindows()
    windows = app.topLevelWidgets()
    for widget in windows:
        widget.close()
    deadline = time.monotonic() + 10
    while any(
        getattr(widget, "_shutdown", None) is not None and not widget._shutdown.done.is_set()
        for widget in windows
    ):
        if time.monotonic() >= deadline:
            pytest.fail("Des travaux Qt restent actifs après la fermeture des fenêtres.")
        app.processEvents()
        time.sleep(0.01)
    for widget in app.topLevelWidgets():
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.shutdown()


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
    monkeypatch.setattr(
        interpolation, "detect_gpu_acceleration",
        lambda _rife_bin, **_kw: interpolation.GpuAcceleration(
            interpolation.NvofCapability(reason="tests"), interpolation.TrtCapability(reason="tests"),
        ),
    )


@pytest.fixture(autouse=True)
def _isolated_plugins(monkeypatch, tmp_path_factory):
    """Extensions et moteurs TensorRT dans un dossier de test, jamais ceux de l'utilisateur ; flux hors ligne."""
    from core import plugins

    base = tmp_path_factory.mktemp("plugins")
    monkeypatch.setattr(plugins, "plugins_root", lambda *_a, **_k: base / "plugins")
    monkeypatch.setattr(plugins, "trt_engine_cache_dir", lambda *_a, **_k: base / "trt-engines")
    # flux des versions publiées : jamais de réseau en test (repli sur la version épinglée)
    monkeypatch.setattr(plugins, "fetch_feed", lambda *_a, **_k: None)


@pytest.fixture(autouse=True)
def _isolated_output_locks(monkeypatch, tmp_path_factory):
    """Verrous de destination des workflows dans un dossier de test, jamais dans le cache utilisateur."""
    from core import output_commit

    lock_dir = tmp_path_factory.mktemp("output-locks")
    monkeypatch.setattr(output_commit, "default_lock_dir", lambda: lock_dir)
