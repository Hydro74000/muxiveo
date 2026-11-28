"""Architecture : le cœur (core/) ne dépend jamais de l'adaptateur CLI (cli/), imports différés compris."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _cli_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and (node.module == "cli" or node.module.startswith("cli.")):
            found.append(f"{path.relative_to(ROOT)}:{node.lineno} from {node.module}")
        elif isinstance(node, ast.Import):
            found.extend(f"{path.relative_to(ROOT)}:{node.lineno} import {alias.name}" for alias in node.names
                         if alias.name == "cli" or alias.name.startswith("cli."))
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "import_module"
              and node.args and isinstance(node.args[0], ast.Constant) and str(node.args[0].value).startswith("cli")):
            found.append(f"{path.relative_to(ROOT)}:{node.lineno} import_module({node.args[0].value})")
    return found


def test_core_never_imports_cli():
    offenders = [hit for path in sorted((ROOT / "core").rglob("*.py")) for hit in _cli_imports(path)]
    assert offenders == []
