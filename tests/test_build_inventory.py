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
    complete = [{"name": name, "version": "1.0"}
                for name in ("PySide6", "numpy", "pymediainfo", "certifi", "PyInstaller", "Pillow")]
    assert check.inventory_errors(complete) == []
    without_build_tools = [item for item in complete if item["name"] not in ("PyInstaller", "Pillow")]
    assert len(check.inventory_errors(without_build_tools)) == 2  # outils de build exigés aussi
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


def test_inventory_only_reads_the_root_manifest_of_a_standard_package(tmp_path):
    """Installeur standard : manifeste à la racine du bundle (aucun dossier ``tools``), jamais ignoré."""
    import zipfile

    check = _check_module()
    package = tmp_path / "Muxiveo-Windows-x64-5.2.0.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("Muxiveo/tools-manifest.json", json.dumps({"python": []}))
        archive.writestr("Muxiveo/tools/tools-manifest.json", json.dumps({"python": [{"name": "x"}]}))
    dest = tmp_path / "out"
    dest.mkdir()
    manifest = check._extract_manifest(package, dest)
    assert manifest == dest / "Muxiveo" / "tools-manifest.json"  # le moins profond


def test_release_windows_check_never_hides_a_failed_allinc_check():
    """pwsh : seule la dernière commande native fixe le code de l'étape ; échec AllInc vérifié avant le standard."""
    text = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    step = text.split("Verify embedded muxiveo-rife and NVEncC variant\n        shell: pwsh", 1)[1].split("- name:", 1)[0]
    assert step.index("if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }") < step.index("--inventory-only")


def test_packaging_logs_the_declared_reason_of_a_rejected_version(monkeypatch, tmp_path):
    import package as package_mod
    import core.python_requirements as reqs

    requirement = reqs.read_requirements()[0]
    assert requirement.distribution == "PySide6"
    monkeypatch.setattr(package_mod, "unsatisfied_requirements",
                        lambda requirements: [(requirement, "6.12.0")] if requirement in requirements else [])
    logged: list[str] = []
    monkeypatch.setattr(package_mod, "_info", logged.append)
    monkeypatch.setattr(package_mod, "_run", lambda *a, **k: None)
    package_mod._ensure_pyinstaller()
    assert any("PySide6 : 6.12.0 excluded" in line and "deallocating None" in line for line in logged)


def test_macos_bundle_ships_the_license_files_before_signing(tmp_path, monkeypatch):
    """Plan v14, lot 13 : LICENSE, NOTICE, SOURCES.md et LICENSES/ dans ``Contents/Resources``, copiés avant la
    signature (un ajout ensuite invaliderait le sceau)."""
    import package as package_mod

    root = tmp_path / "root"
    (root / "LICENSES").mkdir(parents=True)
    for name in ("LICENSE", "NOTICE", "SOURCES.md"):
        (root / name).write_text(name, encoding="utf-8")
    (root / "LICENSES" / "MIT.txt").write_text("MIT", encoding="utf-8")
    monkeypatch.setattr(package_mod, "ROOT", root)
    app = tmp_path / "Muxiveo.app"
    package_mod.bundle_macos_licenses(app)
    resources = app / "Contents" / "Resources"
    assert _check_module().license_errors(resources) == []
    assert not (resources / "LICENSE.txt").exists()  # doublons .txt réservés à Windows
    source = Path(package_mod.__file__).read_text(encoding="utf-8")
    body = source.split("def build_macos(", 1)[1].split("\ndef ", 1)[0]
    assert body.index("bundle_macos_licenses(app_final)") < body.index("_sign_macos_bundle(app_final)")


def test_license_check_reports_every_missing_file(tmp_path):
    check = _check_module()
    errors = check.license_errors(tmp_path)
    assert len(errors) == 4 and any("LICENSES" in error for error in errors)


def test_release_workflow_is_read_only_except_the_publishing_jobs():
    """Plan v14, lot 13 : ``contents: read`` par défaut, ``write`` sur les seuls jobs ``release`` et tap Homebrew ;
    contrôle des licences du DMG livré."""
    root = Path(__file__).resolve().parents[1] / ".github" / "workflows"
    text = (root / "release.yml").read_text(encoding="utf-8")
    assert "\npermissions:\n  contents: read\n" in text
    writers: list[str] = []
    job = None
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("  ") and not line.startswith("   ") and line.rstrip().endswith(":"):
            job = line.strip()[:-1]
        if line.strip() == "contents: write":
            assert lines[index - 1].strip() == "permissions:" and lines[index - 1].startswith("    ")
            writers.append(job or "")
    assert writers == ["release", "publish-homebrew-tap"]
    assert '--licenses "$mount/Muxiveo.app/Contents/Resources"' in text
    assert "\npermissions:\n  contents: read\n" in (root / "ci-unit-all.yml").read_text(encoding="utf-8")
