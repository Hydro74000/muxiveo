"""Arguments collables dans le shell de l'aperçu, avec caractères spéciaux."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from core.workflows.encode.planning import preview


@pytest.mark.parametrize(
    ("argument", "expected"),
    [
        ("simple.mkv", "simple.mkv"),
        ("", '""'),
        (r"D:\A&B.mkv", r'"D:\A&B.mkv"'),
        ("D:\\A&B\\", '"D:\\A&B\\\\"'),
        ("x\\%PATH%", '"x\\\\"^%PATH^%'),
        ("!VALUE!", "^!VALUE^!"),
        ("title=100%PATH%!", '"title=100"^%PATH^%^!'),
        ('a""b', r'a\^"\^"b'),
    ],
)
def test_windows_quoting_protects_shell_operators_and_trailing_backslashes(argument, expected):
    assert preview.quote_preview_argument(argument, platform="win32") == expected


def test_all_windows_preview_comments_use_cmd_syntax(monkeypatch):
    monkeypatch.setattr(preview, "sys", SimpleNamespace(platform="win32"))
    commands = [["ffmpeg", "-i", r"D:\A&B.mkv"], ["ffmpeg", "-i", "second.mkv"]]
    text = preview.format_preview_commands(commands)
    assert text.startswith("REM Commande 1\n")
    assert "\nREM Commande 2\n" in text
    assert "\n#" not in text
    selection = SimpleNamespace(
        is_multi_video=False, preview_command=commands[0], is_two_pass=True,
    )
    assert preview.format_preview_selection(selection).startswith("REM Mode taille cible :")


@pytest.mark.skipif(sys.platform == "win32", reason="Shell POSIX requis")
def test_posix_preview_preserves_actual_arguments():
    arguments = ["", "Film & images.mkv", "100%PATH%!", 'title="A&B"', "L'été", "$HOME", "fin\\"]
    command = [sys.executable, "-c", "import json,sys;print(json.dumps(sys.argv[1:]))", *arguments]
    rendered = preview.format_preview_command(command, platform="linux")
    result = subprocess.run(
        ["/bin/sh", "-c", rendered], capture_output=True, text=True, check=True, timeout=15,
    )
    assert json.loads(result.stdout) == arguments


@pytest.mark.skipif(sys.platform != "win32", reason="Invite cmd.exe réelle requise")
@pytest.mark.parametrize("piped", [False, True])
def test_windows_preview_preserves_actual_arguments_with_delayed_expansion(tmp_path, piped):
    """Contrôle cmd.exe/MSVCRT, y compris quand cmd relance les étages d'un pipe."""
    script = tmp_path / "arguments & unicode.py"
    script.write_text(
        "import json,sys\nsys.stdout.buffer.write(json.dumps(sys.argv[1:]).encode('utf-8'))\n",
        encoding="utf-8",
    )
    arguments = [
        "", r"D:\A&B.mkv", "D:\\A&B\\", "%MRE_QUOTE_VALUE%", "!MRE_QUOTE_VALUE!",
        "x\\%MRE_QUOTE_VALUE%", 'title="A&B"', 'a""b', "L'été", "日本語.mkv",
    ]
    command = [sys.executable, str(script), *arguments]
    if piped:
        command += [
            "|", sys.executable, "-c", "import sys;sys.stdout.buffer.write(sys.stdin.buffer.read())",
        ]
    rendered = preview.format_preview_command(command, platform="win32")
    result = subprocess.run(
        f'cmd.exe /d /v:on /s /c "{rendered}"',
        env={**os.environ, "MRE_QUOTE_VALUE": "NE_PAS_DEVELOPPER"},
        capture_output=True, check=True, timeout=15,
    )
    assert json.loads(result.stdout) == arguments
