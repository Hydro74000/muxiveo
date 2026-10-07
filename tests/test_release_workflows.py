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


@pytest.fixture
def release_checkout(tmp_path):
    """Checkout minimal et API simulée : aucune donnée ni authentification du runner."""
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "__init__.py").touch()
    version_file = tmp_path / "core" / "version.py"
    version_file.write_text('APP_VERSION = "4.2.2"\n', encoding="utf-8")
    (tmp_path / "scripts").mkdir()
    shutil.copyfile(_WORKFLOWS.parents[1] / "scripts" / "check_release_tag.sh",
                    tmp_path / "scripts" / "check_release_tag.sh")
    for args in (["init"], ["config", "user.name", "Test"],
                 ["config", "user.email", "test@example.com"], ["add", "."],
                 ["commit", "-m", "release fixture"]):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True,
                       env={**os.environ, "GIT_AUTHOR_DATE": "2026-10-06T12:00:00Z",
                            "GIT_COMMITTER_DATE": "2026-10-06T12:00:00Z"})
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env python\n"
        "import os,sys\n"
        "endpoint = next(arg for arg in sys.argv if arg.startswith('repos/'))\n"
        "if endpoint.endswith('/tags/v'):\n"
        "    print(os.environ.get('TEST_STABLE_REFS', 'refs/tags/v4.2.1'))\n"
        "elif '/git/tags/' in endpoint:\n"
        "    print('commit ' + os.environ['GITHUB_SHA'])\n"
        "else:\n"
        "    print(os.environ.get('TEST_TAG_OBJECT', ''))\n",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('GITHUB_', 'GH_', 'TEST_TAG_', 'TEST_STABLE_'))}
    env.update({
        "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
        "PYTHONPATH": str(tmp_path),
        "GITHUB_REF_TYPE": "branch", "GITHUB_REF_NAME": "main",
        "GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_NAME": "push",
        "GITHUB_REPOSITORY": "example/muxiveo", "GITHUB_SHA": sha,
        "GITHUB_RUN_NUMBER": "42", "GITHUB_OUTPUT": str(tmp_path / "outputs"),
    })
    return tmp_path, env


def _release_identity(checkout, **overrides):
    root, env = checkout
    result = subprocess.run(
        ["bash", "-c", dict(_scripts("release.yml"))["Compute release identity"]],
        cwd=root, env={**env, **overrides}, capture_output=True, text=True,
    )
    output = root / "outputs"
    values = dict(line.split("=", 1) for line in output.read_text().splitlines()) if output.exists() else {}
    return result, values


@pytest.mark.skipif(not shutil.which("bash"), reason="Bash is required to run release scripts")
@pytest.mark.parametrize("branch,channel,publish", [
    ("main", "stable", "true"), ("devel-cli", "unstable", "true"),
    ("feature/test", "build", "false"),
])
def test_release_identity_follows_branch(release_checkout, branch, channel, publish):
    result, values = _release_identity(release_checkout, GITHUB_REF_NAME=branch)
    assert result.returncode == 0, result.stderr + result.stdout
    assert values["channel"] == channel
    assert values["publish"] == publish
    expected = "4.2.2" if channel == "stable" else (
        "4.2.2-unstable.20261006.42." + release_checkout[1]["GITHUB_SHA"][:7])
    assert values["package_version"] == expected
    assert values["release_tag"] == "v" + expected


@pytest.mark.skipif(not shutil.which("bash"), reason="Bash is required to run release scripts")
@pytest.mark.parametrize("version", [
    '4.0.0$(touch injected)', '4.0.0`touch injected`',
    '4.0.0"; touch injected; #', "4.0.0\nrelease_tag=evil", "../../outside",
])
def test_release_identity_rejects_invalid_app_version(release_checkout, version):
    root, _env = release_checkout
    (root / "core" / "version.py").write_text(f"APP_VERSION = {version!r}\n", encoding="utf-8")
    result, values = _release_identity(release_checkout)
    assert result.returncode != 0
    assert values == {}
    assert not (root / "injected").exists()


@pytest.mark.skipif(not shutil.which("bash"), reason="Bash is required to run release scripts")
def test_release_identity_ignores_removed_freeform_input(release_checkout):
    result, values = _release_identity(release_checkout, RELEASE_INPUT='9.0.0$(touch injected)')
    assert result.returncode == 0, result.stderr
    assert values["package_version"] == "4.2.2"
    assert not (release_checkout[0] / "injected").exists()


@pytest.mark.skipif(not shutil.which("bash"), reason="Bash is required to run release scripts")
def test_release_identity_rejects_tag_trigger(release_checkout):
    result, values = _release_identity(release_checkout, GITHUB_REF_TYPE="tag",
                                      GITHUB_REF_NAME="v4.2.2", GITHUB_REF="refs/tags/v4.2.2")
    assert result.returncode != 0
    assert values == {}


@pytest.mark.skipif(not shutil.which("bash"), reason="Bash is required to run release scripts")
@pytest.mark.parametrize("branch", ["main", "devel-cli"])
@pytest.mark.parametrize("last", ["4.2.2", "4.3.0"])
def test_release_identity_requires_version_above_stable(release_checkout, branch, last):
    result, values = _release_identity(release_checkout, GITHUB_REF_NAME=branch,
                                      TEST_STABLE_REFS="refs/tags/v" + last)
    assert result.returncode != 0
    assert values == {}


@pytest.mark.skipif(not shutil.which("bash"), reason="Bash is required to run release scripts")
@pytest.mark.parametrize("object_type", ["commit", "tag"])
def test_release_identity_allows_same_commit_rerun(release_checkout, object_type):
    result, values = _release_identity(
        release_checkout, TEST_STABLE_REFS="refs/tags/v4.2.2",
        TEST_TAG_OBJECT=object_type + " " + release_checkout[1]["GITHUB_SHA"],
    )
    assert result.returncode == 0, result.stderr
    assert values["release_tag"] == "v4.2.2"


@pytest.mark.skipif(not shutil.which("bash"), reason="Bash is required to run release scripts")
def test_release_identity_rejects_tag_on_another_commit(release_checkout):
    result, values = _release_identity(release_checkout, TEST_TAG_OBJECT="commit " + "0" * 40)
    assert result.returncode != 0
    assert "existe déjà" in result.stderr
    assert values == {}


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


@pytest.mark.skipif(not shutil.which("bash"), reason="Bash is required to run release scripts")
def test_generate_release_notes_includes_installer_links(tmp_path):
    script = dict(_scripts("release.yml"))["Generate release notes"]
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "Muxiveo-Setup-AllInc-4.1.0.exe").touch()
    (artifacts / "Muxiveo-x86_64_allinc-4.1.0.AppImage").touch()
    (artifacts / "Muxiveo-x86_64_allinc-4.1.0.AppImage.zsync").touch()
    (artifacts / "Muxiveo-4.1.0.dmg").touch()
    (artifacts / "Muxiveo-4.1.0-source.zip").touch()

    # Mock gh CLI
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (bin_dir / "gh").chmod(0o755)

    # Initialize a git repo so git fetch and log will succeed
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-m", "initial commit"], cwd=tmp_path, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(tmp_path)], cwd=tmp_path, check=True)

    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
        "RELEASE_TAG": "v4.1.0",
        "RELEASE_NAME": "Muxiveo 4.1.0",
        "CHANNEL": "stable",
        "GITHUB_REPOSITORY": "Hydro74000/muxiveo",
        "GITHUB_SHA": "1234567890abcdef",
    }

    result = subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 0, f"Script failed: {result.stderr}"

    body_file = tmp_path / "release-body.md"
    assert body_file.is_file()
    content = body_file.read_text(encoding="utf-8")

    assert "### 📥 Téléchargements / Direct Downloads" in content
    assert "- 🪟 **Windows (Allinc setup)** : [Muxiveo-Setup-AllInc-4.1.0.exe](https://github.com/Hydro74000/muxiveo/releases/download/v4.1.0/Muxiveo-Setup-AllInc-4.1.0.exe)" in content
    assert "- 🐧 **Linux (AppImage)** : [Muxiveo-x86_64_allinc-4.1.0.AppImage](https://github.com/Hydro74000/muxiveo/releases/download/v4.1.0/Muxiveo-x86_64_allinc-4.1.0.AppImage)" in content
    assert "- 🍏 **macOS (DMG)** : [Muxiveo-4.1.0.dmg](https://github.com/Hydro74000/muxiveo/releases/download/v4.1.0/Muxiveo-4.1.0.dmg)" in content

    downloads_pos = content.index("### 📥 Téléchargements / Direct Downloads")
    changes_pos = content.index("## Changes since previous release")
    assert downloads_pos < changes_pos


def _jobs(filename):
    """Jobs du workflow : (needs, condition if) lus sans dépendance YAML."""
    text = (_WORKFLOWS / filename).read_text(encoding="utf-8")
    body = text.split("\njobs:\n", 1)[1]
    jobs = {}
    for match in re.finditer(r"^  ([A-Za-z0-9_-]+):\n((?:    .*\n|\n)*)", body, re.MULTILINE):
        block = match.group(2)
        inline = re.search(r"^    needs: \[(.*)\]", block, re.MULTILINE)
        if inline:
            needs = [n.strip() for n in inline.group(1).split(",") if n.strip()]
        else:
            listed = re.search(r"^    needs:\n((?:      - .*\n)+)", block, re.MULTILINE)
            single = re.search(r"^    needs: ([A-Za-z0-9_-]+)$", block, re.MULTILINE)
            needs = re.findall(r"- ([A-Za-z0-9_-]+)", listed.group(1)) if listed else ([single.group(1)] if single else [])
        condition = re.search(r"^    if: (.*)$", block, re.MULTILINE)
        jobs[match.group(1)] = (needs, condition.group(1) if condition else "")
    return jobs


def test_jobs_downstream_of_skippable_rife_job_are_not_skipped():
    """Le job muxiveo-rife est sauté quand sa release existe : ses dépendants doivent l'ignorer."""
    jobs = _jobs("release.yml")
    assert "muxiveo-rife" in jobs and "homebrew-formula" in jobs

    def depends_on_rife(name, seen=()):
        return any(n == "muxiveo-rife" or (n not in seen and depends_on_rife(n, (*seen, n))) for n in jobs[name][0])

    downstream = [name for name in jobs if depends_on_rife(name)]
    assert {"build-linux", "build-windows", "homebrew-formula", "release", "publish-homebrew-tap"} <= set(downstream)
    for name in downstream:
        condition = jobs[name][1]
        assert "!cancelled()" in condition or "always()" in condition, name
