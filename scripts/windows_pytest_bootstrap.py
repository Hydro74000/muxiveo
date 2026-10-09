from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run selected pytest suites under Windows/Wine with the installed Qt runtime."
    )
    parser.add_argument("tests", nargs="+", help="Test paths/patterns to run")
    parser.add_argument("--repo-root", default=None, help="Repository root to prepend to sys.path")
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve() if args.repo_root else Path(__file__).resolve().parents[1]
    os.chdir(repo_root)
    sys.path.insert(0, str(repo_root))

    # Les tests et pytest-qt utilisent le même PySide6 que l'application.
    # Une dépendance absente ou un Qt défectueux doit faire échouer les tests.
    import pytest

    return pytest.main(args.tests)


if __name__ == "__main__":
    raise SystemExit(main())
