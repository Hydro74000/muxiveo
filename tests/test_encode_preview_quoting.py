"""Arguments collables dans le shell de l'aperçu, avec caractères spéciaux."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from core import command_preview
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
    # Formateur partagé (core.command_preview) : plateforme simulée à sa source.
    monkeypatch.setattr(command_preview, "sys", SimpleNamespace(platform="win32"))
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
@pytest.mark.parametrize("delayed", ["on", "off"])
def test_windows_preview_preserves_actual_arguments_with_delayed_expansion(tmp_path, piped, delayed):
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
        f'cmd.exe /d /v:{delayed} /s /c "{rendered}"',
        env={**os.environ, "MRE_QUOTE_VALUE": "NE_PAS_DEVELOPPER"},
        capture_output=True, check=True, timeout=15,
    )
    assert json.loads(result.stdout) == arguments


# ---------------------------------------------------------------------------
# A13 — aperçu remux : même formateur que l'encodage
# ---------------------------------------------------------------------------

def _remux_config(tmp_path, *, chapters: bool = False):
    from core.inspector import ChapterEntry
    from core.workflows.remux_models import RemuxConfig

    return RemuxConfig(
        sources=[], output=tmp_path / "out.mkv", track_order=[],
        chapter_overrides=[ChapterEntry(timecode_s=0.0, name="x")] if chapters else None,
    )


def test_remux_preview_quotes_paths_with_spaces(tmp_path, monkeypatch):
    """Reproduction de l'audit : `-i /tmp/A B.mkv` ressortait sans protection."""
    from core.workflows.remux_command import preview_remux_command

    monkeypatch.setattr(command_preview, "sys", SimpleNamespace(platform="linux"))
    text = preview_remux_command(
        _remux_config(tmp_path),
        build_command=lambda *_a, **_k: ["ffmpeg", "-i", "/tmp/A B.mkv", "-c", "copy", "/tmp/sortie d'été.mkv"],
    )
    assert "-i '/tmp/A B.mkv'" in text
    assert "'/tmp/sortie d'\"'\"'été.mkv'" in text


@pytest.mark.skipif(sys.platform == "win32", reason="Shell POSIX requis")
def test_remux_preview_round_trips_through_posix_shell(tmp_path, monkeypatch):
    from core.workflows.remux_command import preview_remux_command

    monkeypatch.setattr(command_preview, "sys", SimpleNamespace(platform="linux"))
    arguments = ["Film & images.mkv", "100%PATH%!", "L'été", "$HOME", "-metadata:s:t:0", "filename=a b.jpg"]
    text = preview_remux_command(
        _remux_config(tmp_path),
        build_command=lambda *_a, **_k: [
            sys.executable, "-c", "import json,sys;print(json.dumps(sys.argv[1:]))", *arguments,
        ],
    )
    result = subprocess.run(["/bin/sh", "-c", text], capture_output=True, text=True, check=True, timeout=15)
    assert json.loads(result.stdout) == arguments


@pytest.mark.parametrize(("platform", "marker"), [("linux", "# "), ("win32", "REM ")])
def test_remux_preview_flags_chapter_placeholder(tmp_path, monkeypatch, platform, marker):
    from core.workflows.remux_command import preview_remux_command

    monkeypatch.setattr(command_preview, "sys", SimpleNamespace(platform=platform))
    text = preview_remux_command(
        _remux_config(tmp_path, chapters=True),
        build_command=lambda *_a, **_k: ["ffmpeg", "-i", "<chapitres.ffmetadata>", "out.mkv"],
    )
    first, _, rest = text.partition("\n")
    assert first.startswith(marker + "<chapitres.ffmetadata>")
    assert "\\\n" not in rest if platform == "win32" else "\\\n" in rest


def test_native_remux_preview_uses_platform_comments_and_quoting(tmp_path, monkeypatch):
    from core.workflows.remux import RemuxWorkflow

    monkeypatch.setattr(command_preview, "sys", SimpleNamespace(platform="win32"))
    wf = RemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
    monkeypatch.setattr(wf, "backend_report", lambda _cfg: {
        "selected_backend": "native", "plan_version": 1,
        "preparation_commands": [["ffmpeg", "-i", r"D:\A&B.mkv", r"D:\work dir\a.h265"]],
    })
    monkeypatch.setattr(wf, "build_command", lambda *_a, **_k: ["ffmpeg", "-i", r"D:\A&B.mkv", "out.mkv"])
    text = wf.preview_command(_remux_config(tmp_path))
    lines = text.splitlines()
    assert lines[0].startswith("REM Backend: native Matroska")
    assert r'ffmpeg -i "D:\A&B.mkv" "D:\work dir\a.h265"' in text
    assert not any(line.startswith("#") for line in lines)
