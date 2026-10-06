"""Couverture CI : déclencheurs, suite générale et gate de publication (A20–A23)."""

from __future__ import annotations

import re
from pathlib import Path

_WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def _text(name: str) -> str:
    return (_WORKFLOWS / name).read_text(encoding="utf-8")


def _trigger_paths(name: str, event: str) -> list[str]:
    """Filtres `paths` d'un événement (lecture par indentation, sans dépendance YAML)."""
    lines = _text(name).splitlines()
    start = next(i for i, line in enumerate(lines) if line.rstrip() == f"  {event}:")
    paths: list[str] = []
    in_paths = False
    for line in lines[start + 1:]:
        if line and not line.startswith("    "):
            break
        if line.strip() == "paths:":
            in_paths = True
            continue
        if in_paths:
            match = re.match(r'^      - "(.+)"$', line)
            if not match:
                break
            paths.append(match.group(1))
    return paths


def _glob_regex(pattern: str) -> re.Pattern[str]:
    """Glob GitHub Actions : `**` traverse les dossiers, `*` non."""
    out = ""
    index = 0
    while index < len(pattern):
        if pattern.startswith("**", index):
            out += ".*"
            index += 2
        elif pattern[index] == "*":
            out += "[^/]*"
            index += 1
        else:
            out += re.escape(pattern[index])
            index += 1
    return re.compile(out + r"\Z")


def _triggered(name: str, event: str, changed: str) -> bool:
    return any(_glob_regex(pattern).match(changed) for pattern in _trigger_paths(name, event))


def test_matroska_reader_change_triggers_native_matrix():
    """Reproduction de l'audit : le lecteur actuel ne déclenchait pas la matrice native."""
    for event in ("pull_request", "push"):
        assert _triggered("ci-matroska-native.yml", event, "core/matroska/reader.py")
        assert _triggered("ci-matroska-native.yml", event, "core/matroska/editors/segment_info.py")
        assert _triggered("ci-matroska-native.yml", event, "core/workflows/common/matroska_finalize.py")
    assert "branches: [main, devel-cli]" in _text("ci-matroska-native.yml")


def test_matroska_filters_point_to_existing_modules():
    root = _WORKFLOWS.parents[1]
    for pattern in _trigger_paths("ci-matroska-native.yml", "pull_request"):
        if "*" in pattern:
            base = pattern.split("*", 1)[0].rstrip("/") or "."
            assert (root / base).exists() or list(root.glob(pattern)), pattern
        else:
            assert (root / pattern).exists(), pattern


def test_general_suite_collects_every_test_file():
    text = _text("ci-unit-all.yml")
    # Collecte du dossier entier : un nouveau test est exécuté sans liste manuelle.
    assert re.search(r"python -m pytest -q -rs tests --junitxml", text)
    assert "workflow_call:" in text


def test_windows_job_runs_real_cmd_and_lock_tests():
    text = _text("ci-unit-all.yml")
    windows = text[text.index("  windows:"):]
    assert "runs-on: windows-latest" in windows
    assert "windows_pytest_bootstrap" not in windows
    for test_file in ("test_encode_preview_quoting.py", "test_output_commit.py", "test_workdir_ownership.py"):
        assert f"tests/{test_file}" in windows
        assert (_WORKFLOWS.parents[1] / "tests" / test_file).is_file()


def test_lint_job_runs_ruff_and_mypy():
    text = _text("ci-unit-all.yml")
    assert "python -m ruff check" in text
    assert "python -m mypy core cli ui workers main.py launcher.py" in text


def test_release_publication_requires_unit_tests_of_same_run():
    text = _text("release.yml")
    assert re.search(r"^  unit-tests:\n(?:    .*\n)*?    uses: \./\.github/workflows/ci-unit-all\.yml$", text, re.M)
    release = text[text.index("\n  release:\n"):text.index("\n  publish-homebrew-tap:")]
    assert "needs.unit-tests.result == 'success'" in release
    assert re.search(r"needs:\n(?:      - .*\n)*      - unit-tests\n", release)
