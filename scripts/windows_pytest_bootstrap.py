from __future__ import annotations

import argparse
import os
import sys
import types
from pathlib import Path
from typing import Any, cast


class _BoundSignal:
    def __init__(self) -> None:
        self._slots: list = []

    def connect(self, slot, _connection_type=None) -> None:
        self._slots.append(slot)

    def emit(self, *args, **kwargs) -> None:
        for slot in list(self._slots):
            slot(*args, **kwargs)


class _SignalDescriptor:
    def __init__(self, *args, **kwargs) -> None:
        self._name: str | None = None

    def __set_name__(self, owner, name: str) -> None:
        self._name = name

    def __get__(self, instance, owner):
        if instance is None:
            return self
        assert self._name is not None
        bound = instance.__dict__.get(self._name)
        if bound is None:
            bound = _BoundSignal()
            instance.__dict__[self._name] = bound
        return bound


class _QObject:
    def __init__(self, parent=None) -> None:
        self._parent = parent


class _QCoreApplication:
    _instance = None

    def __init__(self, argv=None) -> None:
        _QCoreApplication._instance = self
        type(self)._instance = self
        self.argv = list(argv or [])

    @classmethod
    def instance(cls):
        return cls._instance

    def processEvents(self) -> None:
        return None


class _QApplication(_QCoreApplication):
    pass


class _Qt:
    class ConnectionType:
        QueuedConnection = 0


def _install_fake_pyside6() -> None:
    if "PySide6" in sys.modules:
        return

    pkg = cast(Any, types.ModuleType("PySide6"))
    qtcore = cast(Any, types.ModuleType("PySide6.QtCore"))
    qtwidgets = cast(Any, types.ModuleType("PySide6.QtWidgets"))

    qtcore.QObject = _QObject
    qtcore.Signal = _SignalDescriptor
    qtcore.QCoreApplication = _QCoreApplication
    qtcore.Qt = _Qt
    qtwidgets.QApplication = _QApplication

    pkg.QtCore = qtcore
    pkg.QtWidgets = qtwidgets

    sys.modules["PySide6"] = pkg
    sys.modules["PySide6.QtCore"] = qtcore
    sys.modules["PySide6.QtWidgets"] = qtwidgets


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run selected pytest suites under Windows/Wine with a minimal PySide6 stub."
    )
    parser.add_argument("tests", nargs="+", help="Test paths/patterns to run")
    parser.add_argument("--repo-root", default=None, help="Repository root to prepend to sys.path")
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve() if args.repo_root else Path(__file__).resolve().parents[1]
    os.chdir(repo_root)
    sys.path.insert(0, str(repo_root))

    _install_fake_pyside6()

    import pytest

    return pytest.main(args.tests)


if __name__ == "__main__":
    raise SystemExit(main())
