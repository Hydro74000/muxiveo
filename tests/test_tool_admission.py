"""Admission des modifications d'extensions face aux usages des outils (audit B01)."""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication

from ui.tool_admission import ToolAdmission


@pytest.fixture
def usage(qt_app):
    return {"busy": False}


@pytest.fixture
def admission(qt_app, usage):
    return ToolAdmission(lambda: usage["busy"])


def test_mutation_refused_while_tools_in_use(admission, usage):
    usage["busy"] = True
    assert not admission.acquire_mutation("mvo-rife")
    usage["busy"] = False
    assert admission.acquire_mutation("mvo-rife")
    assert admission.mutation_active()


def test_single_mutation_at_a_time(admission):
    assert admission.acquire_mutation("mvo-rife-trt")  # installation + préchauffage
    assert not admission.acquire_mutation("mvo-rife")  # suppression pendant le préchauffage : refusée
    admission.release_mutation("mvo-rife-trt")
    assert admission.acquire_mutation("mvo-rife")


def test_start_not_started_releases_token(admission):
    assert not admission.start_mutation("nvencc", lambda: False)  # contrôleur déjà occupé
    assert not admission.mutation_active()
    assert admission.start_mutation("nvencc", lambda: True)
    assert admission.mutation_active()


def test_deferred_updates_run_one_after_another(admission, usage):
    started: list[str] = []

    def start(name: str):
        return lambda: (started.append(name), True)[1]

    usage["busy"] = True
    admission.defer("mvo-rife", start("mvo-rife"))
    admission.defer("mvo-fel", start("mvo-fel"))
    assert started == []  # traitement en cours
    usage["busy"] = False
    admission.usage_changed()
    assert started == ["mvo-rife"]  # une seule modification à la fois
    admission.release_mutation("mvo-rife")  # fin (succès ou échec) : la suivante part sans usage intermédiaire
    assert started == ["mvo-rife", "mvo-fel"]
    admission.release_mutation("mvo-fel")
    assert admission.pending() == ()


def test_deferred_update_not_started_lets_next_run(admission):
    started: list[str] = []
    admission.defer("mvo-rife", lambda: False)  # contrôleur occupé : rien n'a démarré
    admission.defer("mvo-fel", lambda: (started.append("mvo-fel"), True)[1])
    assert started == ["mvo-fel"]


def test_deferred_update_waits_for_modal_dialog(admission, monkeypatch):
    started: list[str] = []
    monkeypatch.setattr(QApplication, "activeModalWidget", staticmethod(lambda: object()))
    admission.defer("mvo-rife", lambda: (started.append("mvo-rife"), True)[1])
    assert started == []  # un lancement se décide peut-être dans la boîte de dialogue
    monkeypatch.setattr(QApplication, "activeModalWidget", staticmethod(lambda: None))
    admission.drain()
    assert started == ["mvo-rife"]
