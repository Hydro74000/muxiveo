"""Architecture guard: prevent external runtime dependencies in mediainfo_native."""

from __future__ import annotations

import ast
from pathlib import Path

BANNED_IMPORTS = {"subprocess"}
BANNED_CALL_PREFIXES = {
    "subprocess.run",
    "subprocess.Popen",
    "os.system",
}
ALLOWED_TRANSITIONAL_FILES: set[str] = set()


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        parts: list[str] = []
        cur: ast.AST | None = func
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
        return ".".join(reversed(parts))
    return ""


def scan_package(root: Path) -> list[str]:
    violations: list[str] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        rel_str = rel.as_posix()
        if rel_str in ALLOWED_TRANSITIONAL_FILES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".", 1)[0] in BANNED_IMPORTS:
                        violations.append(f"{rel}:{node.lineno} banned import '{alias.name}'")
            elif isinstance(node, ast.ImportFrom):
                mod = (node.module or "").split(".", 1)[0]
                if mod in BANNED_IMPORTS:
                    violations.append(f"{rel}:{node.lineno} banned from-import '{node.module}'")
            elif isinstance(node, ast.Call):
                cname = _call_name(node)
                if cname in BANNED_CALL_PREFIXES:
                    violations.append(f"{rel}:{node.lineno} banned call '{cname}'")
    return violations


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    issues = scan_package(root)
    for issue in issues:
        print(issue)
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
