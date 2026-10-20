"""Styles Qt : une variable de thème n'est valable que dans une f-string."""
from __future__ import annotations

import ast
import re
from pathlib import Path

_THEME_VAR = re.compile(r"\{_?C\.[A-Z_]+\}")


def test_theme_variables_are_never_left_in_plain_strings():
    offenders = []
    for path in sorted((Path(__file__).resolve().parents[1] / "ui").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        in_fstring = {id(part) for node in ast.walk(tree) if isinstance(node, ast.JoinedStr) for part in node.values}
        offenders += [
            f"{path.name}:{node.lineno}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and id(node) not in in_fstring and _THEME_VAR.search(node.value)
        ]
    assert offenders == []


def test_merge_result_and_error_sections_use_theme_colors(qt_app):
    from ui.design_system import colors as C
    from ui.panels import merge_dovi_panel as mod

    result, error = mod._ResultSection(), mod._ErrorSection()
    styles = " ".join(widget.styleSheet() for section in (result, error) for widget in section.findChildren(mod.QWidget))
    assert "{_C." not in styles
    assert C.BADGE_OK_BG in styles and C.BADGE_ERROR_BG in styles and C.ERROR in error._msg_lbl.styleSheet()
