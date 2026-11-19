"""Séparateurs de ligne Unicode dans config.ini (audit A05) : refusés à l'écriture, jamais découpés en nouvelles
options lors d'une réécriture ultérieure."""

from __future__ import annotations

import configparser
from unittest.mock import patch

import pytest

from core import config as cfg_mod
from core import ini_syntax

SEPARATORS = ["\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", " ", " "]


@pytest.mark.parametrize("separator", ["\n", "\r", *SEPARATORS], ids=lambda s: f"U+{ord(s):04X}")
def test_every_line_separator_is_refused(separator):
    reason = ini_syntax.unwritable_reason(f"pre{separator}post")
    assert reason is not None
    assert "pre" not in reason and "post" not in reason  # message sans la valeur


def test_null_character_is_not_a_line_separator():
    assert "\x00" not in ini_syntax.INI_LINE_BREAKS


def test_ini_lines_splits_like_the_loader():
    text = "[tools]\r\nnvencc = pre post\rffmpeg = a\x85b\n"
    assert ini_syntax.ini_lines(text) == ["[tools]", "nvencc = pre post", "ffmpeg = a\x85b"]
    assert ini_syntax.ini_lines("") == []
    assert ini_syntax.ini_lines("a\n\n") == ["a", ""]


@pytest.mark.parametrize("separator", SEPARATORS, ids=lambda s: f"U+{ord(s):04X}")
def test_write_refuses_separator_values(tmp_path, separator):
    ini_path = tmp_path / "config.ini"
    ini_path.write_text("[ui]\ntheme = dark\n", encoding="utf-8")
    with patch.object(cfg_mod, "_INI_PATH", ini_path), pytest.raises(cfg_mod.ConfigIniWriteRefused):
        cfg_mod.write_ini_settings({"tools": {"nvencc": f"pre{separator}ffmpeg = /tmp/other-ffmpeg"}})
    assert ini_path.read_text(encoding="utf-8") == "[ui]\ntheme = dark\n"


@pytest.mark.parametrize("separator", SEPARATORS, ids=lambda s: f"U+{ord(s):04X}")
def test_existing_value_survives_unrelated_rewrite(tmp_path, separator):
    """Valeur déjà présente (écrite avant le correctif) : une modification d'une autre clé la conserve telle quelle."""
    value = f"pre{separator}ffmpeg = /tmp/other-ffmpeg"
    ini_path = tmp_path / "config.ini"
    ini_path.write_text(f"[tools]\nnvencc = {value}\n\n[ui]\ntheme = dark\n", encoding="utf-8")
    with patch.object(cfg_mod, "_INI_PATH", ini_path):
        cfg_mod.write_ini_settings({"ui": {"theme": "light"}})
        cfg_mod.remove_ini_settings({"ui": ["log_max_lines"]})
    parser = configparser.ConfigParser(inline_comment_prefixes=("#",))
    parser.read_string(ini_path.read_text(encoding="utf-8"))
    assert parser.get("tools", "nvencc") == value.strip()
    assert not parser.has_option("tools", "ffmpeg")
    assert parser.get("ui", "theme") == "light"
