"""
tests/test_build_inventory.py — Inventaire Python des builds dans tools-manifest.json, sans verrouillage
(décision DE ; plan de remédiation 5.2.0, lot 13).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import core.tool_manifest as tool_manifest
from core.python_requirements import BUILD_REQUIREMENTS_FILE, read_requirements, undeclared_constraints
from core.tool_manifest import ToolManifest, python_inventory


def test_manifest_lists_the_python_distributions_of_the_build(tmp_path):
    path = ToolManifest().write(tmp_path)
    document = json.loads(path.read_text(encoding="utf-8"))
    names = {item["name"].lower() for item in document["python"]}
    assert {"pyside6", "numpy"} <= names and all(item["version"] for item in document["python"])


def test_inventory_of_another_interpreter_uses_pip_list(monkeypatch):
    seen: list[list[str]] = []

    def run(command, **kwargs):
        seen.append(command)
        assert kwargs["env"] == {"WINEPREFIX": "/p"}
        return SimpleNamespace(stdout=json.dumps([{"name": "PySide6", "version": "6.10.2"}]))

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(tool_manifest.subprocess, "run", run)
    assert python_inventory(["wine", "python.exe"], env={"WINEPREFIX": "/p"}) == [
        {"name": "PySide6", "version": "6.10.2"}]
    assert seen[0][:4] == ["wine", "python.exe", "-m", "pip"]


def test_build_requirements_follow_the_same_rules():
    requirements = read_requirements(BUILD_REQUIREMENTS_FILE)
    assert {r.distribution.lower() for r in requirements} == {"pyinstaller", "pillow"}
    assert all(undeclared_constraints(r) == [] for r in requirements)
    assert all("==" not in r.spec for r in requirements)  # aucune version figée


def test_ci_packaging_jobs_install_the_build_requirements():
    root = Path(__file__).resolve().parents[1] / ".github" / "workflows"
    for name in ("release.yml", "ci-update-process.yml", "tmdb-smoke-packaged.yml", "build-windows-store-upload.yml"):
        text = (root / name).read_text(encoding="utf-8")
        assert "pip install -r requirements.txt pyinstaller" not in text, name
        assert "-r requirements-build.txt" in text, name
