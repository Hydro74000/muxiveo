"""Run the release identity/embedding scripts with hostile version strings."""

import ast
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest


_WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def _scripts(filename):
    """Read literal run blocks without adding a YAML dependency to the suite."""
    text = (_WORKFLOWS / filename).read_text(encoding="utf-8")
    blocks = re.split(r"^      - name: ", text, flags=re.MULTILINE)[1:]
    for block in blocks:
        name = block.splitlines()[0]
        match = re.search(r"^        run: \|\n((?:          .*\n|\n)+)", block, re.MULTILINE)
        if match:
            yield name, "\n".join(line[10:] for line in match.group(1).splitlines())


@pytest.mark.parametrize("filename", ["release.yml", "build-windows-store-upload.yml"])
def test_release_scripts_do_not_interpolate_actions_expressions(filename):
    scripts = list(_scripts(filename))
    assert scripts
    for name, script in scripts:
        assert "${{" not in script, name


@pytest.mark.skipif(not shutil.which("bash"), reason="Bash is required to run release scripts")
@pytest.mark.parametrize("version,valid", [
    ("v4.0.0", True),
    ("4.0.0-unstable.20260924.42.abcdef0", True),
    ('4.0.0$(touch injected)', False),
    ('4.0.0`touch injected`', False),
    ('4.0.0"; touch injected; #', False),
    ("4.0.0\nrelease_tag=evil", False),
    ("../../outside", False),
])
@pytest.mark.parametrize("source", ["input", "tag"])
def test_release_identity_validates_input_and_tags(tmp_path, version, valid, source):
    script = dict(_scripts("release.yml"))["Compute release identity"]
    output = tmp_path / "outputs"
    env = {
        **os.environ,
        "PYTHONPATH": str(_WORKFLOWS.parents[1]),
        "RELEASE_INPUT": version if source == "input" else "",
        "GITHUB_REF_TYPE": "tag" if source == "tag" else "branch",
        "GITHUB_REF_NAME": version if source == "tag" else "main",
        "GITHUB_EVENT_NAME": "workflow_dispatch" if source == "input" else "push",
        "GITHUB_OUTPUT": str(output),
    }
    result = subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert (result.returncode == 0) is valid, result.stderr
    assert not (tmp_path / "injected").exists()
    if valid:
        assert f"package_version={version.lstrip('v')}\n" in output.read_text()
    else:
        assert not output.exists()


@pytest.mark.skipif(not shutil.which("bash"), reason="Bash is required to run release scripts")
def test_embedded_version_is_serialized_as_data(tmp_path):
    version = '4.0.0"; __import__("os").system("touch injected") #\n$(touch injected)'
    (tmp_path / "core").mkdir()
    scripts = [script for name, script in _scripts("release.yml") if name == "Embed build version"]
    assert len(scripts) == 3
    for script in scripts:
        result = subprocess.run(
            ["bash", "-c", script], cwd=tmp_path,
            env={**os.environ, "PACKAGE_VERSION": version}, capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        module = ast.parse((tmp_path / "core" / "_build_version.py").read_text())
        assert len(module.body) == 1
        assert ast.literal_eval(module.body[0].value) == version
        assert not (tmp_path / "injected").exists()
