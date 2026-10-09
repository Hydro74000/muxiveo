"""Dépendances Python du setup alignées sur requirements.txt (A24)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import core.python_requirements as reqs
from core.python_requirements import parse_version, read_requirements


def test_requirements_file_is_the_single_source():
    requirements = {r.distribution: r for r in read_requirements()}
    assert set(requirements) == {"PySide6", "numpy", "pymediainfo", "certifi"}
    assert requirements["PySide6"].module == "PySide6"  # casse exacte du module
    assert requirements["PySide6"].minimum == (6, 6, 0)
    assert requirements["PySide6"].excluded == ((6, 12, 0),)  # None décrémenté par les méthodes sans retour
    assert requirements["certifi"].minimum is None


def test_setup_installs_from_requirements():
    import setup

    assert setup.PYTHON_PACKAGES == [r.spec for r in read_requirements()]
    assert "PySide6>=6.6.0,!=6.12.0" in setup.PYTHON_PACKAGES and "certifi" in setup.PYTHON_PACKAGES


def test_unsupported_requirement_syntax_is_explicit(tmp_path: Path):
    path = tmp_path / "requirements.txt"
    path.write_text("PySide6<7\n", encoding="utf-8")
    with pytest.raises(ValueError):
        read_requirements(path)


@pytest.mark.parametrize(("text", "expected"), [("6.10.0", (6, 10, 0)), ("2.3.0rc1", (2, 3, 0)), ("1.24", (1, 24))])
def test_parse_version(text, expected):
    assert parse_version(text) == expected


@pytest.fixture
def setup_module(monkeypatch):
    import setup

    commands: list[list[str]] = []
    monkeypatch.setattr(setup, "run", lambda cmd, dry_run=False, **_kw: commands.append(list(cmd)))
    for name in ("title", "ok", "warn", "step"):
        monkeypatch.setattr(setup, name, lambda *_a, **_k: None)
    monkeypatch.delattr(sys, "frozen", raising=False)
    return setup, commands


def _installed(monkeypatch, versions: dict[str, str | None]):
    monkeypatch.setattr(reqs, "installed_version", lambda name: versions.get(name))
    import setup
    monkeypatch.setattr(setup, "installed_version", lambda name: versions.get(name))


def test_satisfied_requirements_run_no_pip(setup_module, monkeypatch):
    setup, commands = setup_module
    _installed(monkeypatch, {"PySide6": "6.10.1", "numpy": "2.3.0", "pymediainfo": "7.0.1", "certifi": "2025.1.1"})
    setup.install_python_packages(dry_run=False)
    setup.install_python_packages(dry_run=False)
    assert commands == []  # idempotent : aucune réinstallation


def test_only_missing_or_outdated_are_installed(setup_module, monkeypatch):
    setup, commands = setup_module
    _installed(monkeypatch, {"PySide6": "6.5.3", "numpy": "2.3.0", "pymediainfo": "7.0.1", "certifi": None})
    setup.install_python_packages(dry_run=False)
    assert commands == [[sys.executable, "-m", "pip", "install", "PySide6>=6.6.0,!=6.12.0", "certifi"]]


def test_excluded_version_is_reinstalled(setup_module, monkeypatch):
    setup, commands = setup_module
    _installed(monkeypatch, {"PySide6": "6.12.0", "numpy": "2.3.0", "pymediainfo": "7.0.1", "certifi": "2025.1.1"})
    setup.install_python_packages(dry_run=False)
    assert commands == [[sys.executable, "-m", "pip", "install", "PySide6>=6.6.0,!=6.12.0"]]


def test_exclusion_syntax(tmp_path: Path):
    path = tmp_path / "requirements.txt"
    path.write_text("Lib>=1.2, != 1.3.0 ,!=1.4\nOther,!=2\n", encoding="utf-8")
    lib, other = read_requirements(path)
    assert lib.spec == "Lib>=1.2,!=1.3.0,!=1.4" and lib.excluded == ((1, 3, 0), (1, 4))
    assert other.spec == "Other!=2" and other.minimum is None
    assert reqs.is_excluded("1.4.0", lib) and reqs.is_excluded("1.3", lib) and not reqs.is_excluded("1.3.1", lib)


def test_frozen_application_never_calls_pip(setup_module, monkeypatch):
    setup, commands = setup_module
    _installed(monkeypatch, {})
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    setup.install_python_packages(dry_run=False, force=True)
    assert commands == []


@pytest.mark.parametrize(("version", "accepted"), [
    ("6.6", True), ("6.6.0", True), ("6.6.0.0", True), ("6.6.0+mybuild", True),
    ("6.6.0rc1", False), ("6.6.0.dev1", False), ("6.7.0rc1", True),
])
def test_minimum_release_comparison(monkeypatch, version, accepted):
    monkeypatch.setattr(reqs, "installed_version", lambda _name: version)
    requirement = next(r for r in read_requirements() if r.distribution == "PySide6")
    assert bool(reqs.unsatisfied_requirements([requirement])) is not accepted
