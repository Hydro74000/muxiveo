"""Refus de validation au lancement (remux, encodage) : texte historique du CLI, code public ``validation``."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cli.main import main
from core.jobs import encode_runtime, runtime
from core.service.ops import media
from core.workflows.remux_models import RemuxValidationError

_SIGNAL = SimpleNamespace(connect=lambda *_a: None)


@pytest.fixture(autouse=True)
def _shared_app(qt_app):
    """``main()`` réutilise l'application Qt de la suite (sinon il créerait une QCoreApplication)."""
    yield


def _job(tmp_path: Path, **extra: Any) -> Path:
    path = tmp_path / "job.json"
    path.write_text(json.dumps({"version": 1, "sources": ["a.mkv"], "output": "out.mkv", **extra}), encoding="utf-8")
    return path


def _run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    code = main(argv)
    return code, capsys.readouterr().err


@pytest.fixture
def remux_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(media, "build_remux_config", lambda *_a, **_k: SimpleNamespace(output=tmp_path / "out.mkv"))
    monkeypatch.setattr(media, "resolve_job_encode", lambda *_a, **_k: None)

    class _Wf:
        log_message = backend_event = _SIGNAL

        def run(self, _config):
            raise RemuxValidationError(["Piste 3 introuvable", "Sortie identique à la source"])

    monkeypatch.setattr(runtime, "remux_workflow", lambda *_a, **_k: _Wf())


def test_remux_refusal_keeps_its_text_and_is_a_validation_error(remux_refused, tmp_path, capsys):
    code, err = _run(["run", "--config", str(_job(tmp_path))], capsys)
    assert err == "[ERROR] Piste 3 introuvable\nSortie identique à la source\n"
    assert code == 3


def test_remux_refusal_details_in_call(remux_refused, tmp_path, capsys):
    request = tmp_path / "req.json"
    request.write_text(json.dumps({"job": json.loads(_job(tmp_path).read_text())}), encoding="utf-8")
    code = main(["call", "media.run", "--request", str(request)])
    error = json.loads(capsys.readouterr().out)["error"]
    assert code == 3 and error["code"] == "validation"
    assert [d["message"] for d in error["details"]] == ["Piste 3 introuvable", "Sortie identique à la source"]


@pytest.fixture
def encode_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(media, "build_remux_config", lambda *_a, **_k: SimpleNamespace(output=tmp_path / "out.mkv"))
    monkeypatch.setattr(media, "resolve_job_encode",
                        lambda *_a, **_k: SimpleNamespace(config=SimpleNamespace(output=tmp_path / "out.mkv")))
    monkeypatch.setattr(encode_runtime, "prepare_vulkan", lambda *_a, **_k: None)
    monkeypatch.setattr(encode_runtime, "align_dovi_nvenc", lambda config, *_a, **_k: config)

    class _Wf:
        log_message = failed_work_dir_retained = _SIGNAL

        def validate(self, _config):
            return ["Codec inconnu", "Débit invalide"]

    monkeypatch.setattr(encode_runtime, "encode_workflow", lambda *_a, **_k: _Wf())


def test_encode_refusal_keeps_its_text_and_is_a_validation_error(encode_refused, tmp_path, capsys):
    code, err = _run(["run", "--config", str(_job(tmp_path, encode={}))], capsys)
    assert err == "[ERROR] Encodage refusé :\n- Codec inconnu\n- Débit invalide\n"
    assert code == 3


def test_encode_refusal_details_in_call(encode_refused, tmp_path, capsys):
    request = tmp_path / "req.json"
    request.write_text(json.dumps({"job": json.loads(_job(tmp_path, encode={}).read_text())}), encoding="utf-8")
    code = main(["call", "media.run", "--request", str(request)])
    error = json.loads(capsys.readouterr().out)["error"]
    assert code == 3 and error["code"] == "validation"
    assert [d["message"] for d in error["details"]] == ["Codec inconnu", "Débit invalide"]
