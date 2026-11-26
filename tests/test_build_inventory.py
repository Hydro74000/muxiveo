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


def _check_module():
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "scripts" / "check_allinc_rife.py"
    spec = importlib.util.spec_from_file_location("check_allinc_rife_inventory", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_artifact_check_requires_a_complete_versioned_inventory(tmp_path):
    check = _check_module()
    complete = [{"name": name, "version": "1.0"} for name in ("PySide6", "numpy", "pymediainfo", "certifi")]
    assert check.inventory_errors(complete) == []
    assert check.inventory_errors([{"name": "PySide6"}])  # sans version, dépendances manquantes
    assert check.inventory_errors(None)
    manifest = tmp_path / "tools-manifest.json"
    manifest.write_text(json.dumps({"version": 1, "tools": [], "python": complete}), encoding="utf-8")
    assert check.manifest_inventory_errors(manifest) == []


def test_standard_installer_and_dmg_are_checked_in_the_release_workflow():
    text = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "--inventory-only" in text and "--manifest" in text and "hdiutil attach" in text


def test_exclusion_without_minimum_is_parsed(tmp_path):
    path = tmp_path / "req.txt"
    path.write_text("# != 6.0 : régression connue\npyinstaller!=6.0\n", encoding="utf-8")
    (requirement,) = read_requirements(path)
    assert requirement.spec == "pyinstaller!=6.0" and undeclared_constraints(requirement) == []


def test_skip_wine_never_inventories_the_linux_interpreter(tmp_path, monkeypatch):
    import package as package_mod

    bundle = tmp_path / "Muxiveo"
    (bundle / "tools").mkdir(parents=True)
    (bundle / "tools" / "tools-manifest.json").write_text(
        json.dumps({"python": [{"name": "PySide6", "version": "6.10.2"}]}), encoding="utf-8")
    assert package_mod._windows_bundle_inventory(bundle) == [{"name": "PySide6", "version": "6.10.2"}]
    monkeypatch.setattr(package_mod, "_WIN_PY_EXE", tmp_path / "absent" / "python.exe")
    import pytest

    with pytest.raises(SystemExit):
        package_mod._windows_bundle_inventory(tmp_path / "vide")
