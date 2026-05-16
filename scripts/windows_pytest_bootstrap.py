from __future__ import annotations

import argparse
import os
import sys
import threading
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


class _QEventLoop:
    def __init__(self, parent=None) -> None:
        self._parent = parent
        self._quit_event = threading.Event()

    def exec(self) -> int:
        self._quit_event.wait()
        return 0

    def quit(self) -> None:
        self._quit_event.set()


class _QLocale:
    @staticmethod
    def system():
        return _QLocale()

    def name(self) -> str:
        return os.environ.get("LANG", "en_US").split(".", 1)[0]


class _Qt:
    class ConnectionType:
        QueuedConnection = 0

    class ItemDataRole:
        UserRole = 256


class _QSettings:
    class Format:
        IniFormat = 0

    class Scope:
        UserScope = 0

    _stores: dict[tuple[str, ...], dict[str, Any]] = {}

    def __init__(self, *args, **_kwargs) -> None:
        key = tuple(str(arg) for arg in args)
        self._values = self._stores.setdefault(key, {})

    def value(self, key: str, default=None):
        return self._values.get(str(key), default)

    def setValue(self, key: str, value) -> None:
        self._values[str(key)] = value

    def sync(self) -> None:
        return None

    def clear(self) -> None:
        self._values.clear()


class _QStandardPaths:
    class StandardLocation:
        AppDataLocation = 0
        MoviesLocation = 1

    @staticmethod
    def writableLocation(location) -> str:
        if location == _QStandardPaths.StandardLocation.AppDataLocation:
            return os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        if location == _QStandardPaths.StandardLocation.MoviesLocation:
            return str(Path.home() / "Videos")
        return ""


def _install_fake_pyside6() -> None:
    if "PySide6" in sys.modules:
        return

    pkg = cast(Any, types.ModuleType("PySide6"))
    qtcore = cast(Any, types.ModuleType("PySide6.QtCore"))
    qtwidgets = cast(Any, types.ModuleType("PySide6.QtWidgets"))

    qtcore.QObject = _QObject
    qtcore.Signal = _SignalDescriptor
    qtcore.QCoreApplication = _QCoreApplication
    qtcore.QEventLoop = _QEventLoop
    qtcore.QLocale = _QLocale
    qtcore.QSettings = _QSettings
    qtcore.QStandardPaths = _QStandardPaths
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
