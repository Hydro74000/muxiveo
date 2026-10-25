"""Marqueur d'installation des archives source (scripts/write_source_marker.py)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from core import update_apply

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "write_source_marker.py"
_spec = importlib.util.spec_from_file_location("write_source_marker", _SCRIPT)
assert _spec and _spec.loader
marker_script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(marker_script)


def test_marker_lists_tracked_files_and_extras(tmp_path):
    out = tmp_path / update_apply.MARKER_NAME
    assert marker_script.main(["--version", "9.9.9", "--extra", "core/_build_version.py", "--out", str(out)]) == 0
    tmp_path.joinpath("main.py").write_text("")
    tmp_path.joinpath("core").mkdir()
    doc = update_apply.read_marker(tmp_path)                # format relu par la mise à jour intégrée
    assert doc is not None and doc["version"] == "9.9.9"
    assert {"main.py", "core/version.py", "config.ini", "core/_build_version.py"} <= set(doc["files"])
    assert doc["files"] == sorted(set(doc["files"]))
    assert json.loads(out.read_text(encoding="utf-8"))["schema"] == 1


@pytest.mark.parametrize("name", ["/etc/passwd", "../x", "a\\b"])
def test_marker_refuses_escaping_paths(name):
    with pytest.raises(ValueError):
        marker_script.build_marker("1.0.0", [name])
