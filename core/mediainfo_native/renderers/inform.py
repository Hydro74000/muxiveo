"""Inform/template parser and renderer."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Tuple


def parse_inform_expression(inform_expr: str) -> Tuple[str, str]:
    expr = inform_expr.strip()
    if expr.lower().startswith("file://"):
        path = Path(expr[7:])
        if path.exists():
            expr = path.read_text(encoding="utf-8", errors="ignore").strip()
    if ";" not in expr:
        return "General", expr
    selector, template = expr.split(";", 1)
    selector_clean = selector.strip() or "General"
    return selector_clean, template


def render_inform(fields: Mapping[str, str], template: str) -> str:
    out = template
    while "%" in out:
        start = out.find("%")
        end = out.find("%", start + 1)
        if start == -1 or end == -1:
            break
        key = out[start + 1 : end]
        value = fields.get(key)
        if value is None:
            value = fields.get(key.replace("_", " "), "")
        if key == "Duration" and fields.get("Format") == "SubRip":
            try:
                value = str(int(round(float(str(value)) * 1000)))
            except (TypeError, ValueError):
                value = str(value or "")
        out = out[:start] + str(value) + out[end + 1 :]
    return out
