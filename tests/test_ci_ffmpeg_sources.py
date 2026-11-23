"""CI : FFmpeg master (action ``ffmpeg-master``) pour tout job Linux / Windows.

Les paquets embarquent la build master de FFmpeg-Builds ; la CI doit tester la même.
Exceptions nommées : job plancher 6.1 (FFmpeg de distribution, ``ci-media-regressions``)
et macOS (Homebrew, seul FFmpeg du paquet macOS).
"""
from __future__ import annotations

import re
from pathlib import Path

_WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
_ACTION = Path(__file__).resolve().parents[1] / ".github" / "actions" / "ffmpeg-master" / "action.yml"
_FFMPEG_WORD = re.compile(r"(?<![\w.-])ffmpeg(?![\w.-])", re.IGNORECASE)
#: Installations hors action autorisées : (fichier, nature) → nombre.
_ALLOWED = {("ci-media-regressions.yml", "packages"): 1}


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


_INSTALL = re.compile(r"\b(?:apt-get|apt|choco|winget|dnf|yum|pacman)\b[^\n]*\b(?:install|-S)\b")


def _joined_lines(text: str) -> list[str]:
    """Lignes avec continuations shell (``\\``) ou PowerShell (`` ` ``) rattachées à leur commande.

    Le numéro de ligne reste celui du début de la commande (lignes de suite vidées).
    """
    raw = text.splitlines()
    lines: list[str] = []
    index = 0
    while index < len(raw):
        line = raw[index]
        start = len(lines)
        lines.append(line)
        while line.rstrip().endswith(("\\", "`")) and index + 1 < len(raw):
            index += 1
            line = raw[index]
            lines[start] = lines[start].rstrip()[:-1] + " " + line.strip()
            lines.append("")
        index += 1
    return lines


def _ffmpeg_installs(text: str) -> list[tuple[int, str]]:
    """Installations de FFmpeg par gestionnaire de paquets (apt, choco, winget…), hors macOS."""
    lines = _joined_lines(text)
    found: list[tuple[int, str]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if stripped.startswith("packages:") or stripped.startswith("packages=("):
            block = [stripped.split("packages", 1)[1]]
            end = index + 1
            if stripped.startswith("packages=("):
                while end < len(lines) and ")" not in lines[end]:
                    block.append(lines[end])
                    end += 1
            else:
                while end < len(lines) and lines[end].strip() and _indent(lines[end]) > _indent(line):
                    block.append(lines[end])
                    end += 1
            if any(_FFMPEG_WORD.search(part) for part in block):
                found.append((index + 1, "packages"))
            index = end
            continue
        if stripped.startswith(("- name:", "name:", "#")) or "brew install" in stripped:
            index += 1
            continue
        if "Gyan.FFmpeg" in stripped or (_INSTALL.search(stripped) and _FFMPEG_WORD.search(stripped)):
            found.append((index + 1, "install"))
        index += 1
    return found


def test_linux_and_windows_jobs_use_ffmpeg_master_action() -> None:
    assert _ACTION.is_file()
    counts: dict[tuple[str, str], int] = {}
    unexpected: list[str] = []
    for workflow in sorted(_WORKFLOWS.glob("*.yml")):
        for line_number, kind in _ffmpeg_installs(workflow.read_text(encoding="utf-8")):
            key = (workflow.name, kind)
            counts[key] = counts.get(key, 0) + 1
            if counts[key] > _ALLOWED.get(key, 0):
                unexpected.append(f"{workflow.name}:{line_number} ({kind})")
    assert not unexpected, (
        "FFmpeg installé hors de .github/actions/ffmpeg-master (build master des paquets) : "
        + ", ".join(unexpected)
    )


def test_scanner_detects_package_manager_installs() -> None:
    sample = """
      - uses: ./.github/actions/apt-install
        with:
          packages: >-
            mediainfo
            ffmpeg libegl1
      - name: Install FFmpeg master
        uses: ./.github/actions/ffmpeg-master
      - run: choco install ffmpeg mediainfo-cli -y
      - run: winget install --id Gyan.FFmpeg -e
      - run: brew install ffmpeg
      - run: |
          packages=(
            ffmpeg
          )
      - run: sudo apt-get -y --no-install-recommends install ffmpeg
      - run: |
          sudo apt-get install -y \\
            mediainfo \\
            ffmpeg
      - run: choco install `
          ffmpeg
    """
    assert [kind for _line, kind in _ffmpeg_installs(sample)] == [
        "packages", "install", "install", "packages", "install", "install", "install",
    ]
