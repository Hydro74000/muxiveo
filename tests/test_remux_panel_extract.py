"""Extraction depuis le panneau Conteneur : remplacement lié à l'état confirmé de la destination (audit C02)."""

from __future__ import annotations

from pathlib import Path
from types import MethodType, SimpleNamespace
from typing import Any

import pytest
from PySide6.QtWidgets import QFileDialog, QMessageBox

from core.jobs import extract as extract_job
from core.jobs.errors import JobError
from core.jobs.extract import ExtractionPlan
from ui.panels.remux_panel import panel as panel_module
from ui.panels.remux_panel.panel import RemuxPanel

FONT = b"police extraite"


def _panel(tmp_path: Path, monkeypatch, outcome: dict[str, Any], before_extract=None) -> SimpleNamespace:
    info = SimpleNamespace(path=tmp_path / "source.mkv", duration_s=1.0)
    info.path.write_bytes(b"mkv")
    source = SimpleNamespace(path=info.path, info=info)
    plan = ExtractionPlan("attachment", -1, default_name="police.ttf", label="Police", attachment=0)
    monkeypatch.setattr(panel_module, "plan_attachment", lambda _info, _index: plan)

    def start_extraction(info_, targets, *, tools, existing=None, force=False, progress_args=()):
        (plan_, output), = targets
        if before_extract is not None:
            before_extract(output)
        try:
            outcome["path"] = extract_job.extract(info_, plan_, output, tools=tools, run=lambda *_a: None,
                                                  existing=existing, force=force,
                                                  attachment_reader=lambda _p, _i: FONT)
        except JobError as exc:
            outcome["error"] = exc
        return SimpleNamespace(connect_terminal=lambda **_k: None)

    monkeypatch.setattr(panel_module, "start_extraction", start_extraction)
    fake = SimpleNamespace(
        _config=SimpleNamespace(tool_ffmpeg="ffmpeg", tool_dovi_tool="dovi_tool", tool_hdr10plus="hdr10plus_tool"),
        _find_source=lambda _id: source,
        _can_start_auxiliary_operation=lambda: True,
        extract_started=SimpleNamespace(emit=lambda *_a: None),
        log_message=SimpleNamespace(emit=lambda *_a: None),
    )
    fake._start_extraction = MethodType(RemuxPanel._start_extraction, fake)
    fake.extract = MethodType(RemuxPanel._on_extract_attachment, fake)
    return fake


def _choose(monkeypatch, output: Path) -> list[Any]:
    seen: list[Any] = []

    def dialog(*args: Any, **kwargs: Any):
        seen.append(kwargs.get("options"))
        return str(output), ""

    monkeypatch.setattr(QFileDialog, "getSaveFileName", dialog)
    return seen


def test_file_created_after_save_dialog_is_preserved(qt_app, tmp_path, monkeypatch):
    output = tmp_path / "police.ttf"
    outcome: dict[str, Any] = {}
    seen = _choose(monkeypatch, output)
    panel = _panel(tmp_path, monkeypatch, outcome,
                   before_extract=lambda path: path.write_bytes(b"fichier tiers jamais confirme"))
    panel.extract("src", SimpleNamespace(local_index=0))
    assert seen == [QFileDialog.Option.DontConfirmOverwrite]  # pas de confirmation native, la nôtre fait foi
    assert isinstance(outcome.get("error"), JobError) and outcome["error"].exit_code == 5
    assert output.read_bytes() == b"fichier tiers jamais confirme"


def test_file_replaced_during_confirmation_is_preserved(qt_app, tmp_path, monkeypatch):
    output = tmp_path / "police.ttf"
    output.write_bytes(b"version confirmee")
    outcome: dict[str, Any] = {}
    _choose(monkeypatch, output)

    def question(*_args: Any, **_kwargs: Any):
        output.write_bytes(b"remplace pendant la question, plus long")  # état relevé avant la question
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", question)
    _panel(tmp_path, monkeypatch, outcome).extract("src", SimpleNamespace(local_index=0))
    assert isinstance(outcome.get("error"), JobError)
    assert output.read_bytes() == b"remplace pendant la question, plus long"


@pytest.mark.parametrize("answer", [QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.No])
def test_confirmed_replacement_of_unchanged_file(qt_app, tmp_path, monkeypatch, answer):
    output = tmp_path / "police.ttf"
    output.write_bytes(b"ancienne")
    outcome: dict[str, Any] = {}
    _choose(monkeypatch, output)
    monkeypatch.setattr(QMessageBox, "question", lambda *_a, **_k: answer)
    _panel(tmp_path, monkeypatch, outcome).extract("src", SimpleNamespace(local_index=0))
    if answer == QMessageBox.StandardButton.Yes:
        assert outcome.get("path") == output and output.read_bytes() == FONT
    else:
        assert outcome == {} and output.read_bytes() == b"ancienne"
