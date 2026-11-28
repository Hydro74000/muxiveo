"""Opérations privilégiées (refusées sans autorisation explicite) et files d'ordonnancement déclarées."""

from __future__ import annotations

import io
import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, cast

import pytest

from core.service import ApiError, ErrorCode, EventType, ExecutionContext, ListSink, Model, call, operation
from core.service.catalog import CATALOG
from core.service.operations import Catalog

_J, _X = "job", "exclusive"
#: Classe de chaque opération du catalogue : (file, privilégiée). Une opération ajoutée sans classe échoue ici.
EXPECTED: dict[str, tuple[str, bool]] = {
    **{name: ("analysis", False) for name in (
        "calibration.compose", "calibration.edit", "calibration.invert", "calibration.show", "encode_preset.delete",
        "encode_preset.export", "encode_preset.import", "encode_preset.list", "encode_preset.save",
        "encode_preset.show", "hdr.inspect", "media.bluray_titles", "media.inspect", "media.preview",
        "media.validate", "nvencc.status", "plugins.list", "profile.capture", "profile.delete", "profile.list",
        "profile.preview", "profile.save", "profile.show", "profile.validate", "schema.get", "sync.scan",
        "sync.shift_subtitles", "sync.waveform", "system.capabilities", "system.tools", "system.version",
        "tmdb.details", "tmdb.search", "workdir.list")},
    **{name: (_J, False) for name in (
        "media.run", "media.extract", "media.batch", "profile.apply", "profile.batch", "hybrid.plan", "hybrid.run",
        "hdr.patch", "encode.preview_capture")},
    **{name: (_X, False) for name in ("config.path", "config.show", "config.get")},
    **{name: (_X, True) for name in (
        "config.set", "config.unset", "config.reset", "workdir.clean", "nvencc.install", "nvencc.remove",
        "plugins.install", "plugins.update", "plugins.remove", "app.update_install")},
}


def test_every_operation_declares_its_queue_and_privilege():
    assert {spec.name: (spec.queue, spec.privileged) for spec in CATALOG} == EXPECTED
    described = CATALOG.describe()["operations"]
    for name, (queue, privileged) in EXPECTED.items():
        assert (described[name]["queue"], described[name]["privileged"]) == (queue, privileged), name
        assert ("forbidden" in described[name]["errors"]) is privileged, name


@dataclass(frozen=True)
class _Empty(Model):
    pass


def _catalog(ran: list[str]) -> Catalog:
    @operation("test.secret", request=_Empty, result=_Empty, summary="Privilégiée.", privileged=True,
               queue="exclusive")
    def secret(_request: _Empty, _ctx: ExecutionContext) -> _Empty:
        ran.append("secret")
        return _Empty()

    return Catalog([secret])


def test_privileged_operation_needs_an_explicit_permission():
    ran: list[str] = []
    sink = ListSink()
    with pytest.raises(ApiError) as caught:
        call(_catalog(ran), "test.secret", {}, ExecutionContext.create(SimpleNamespace(), sink))
    assert caught.value.code is ErrorCode.FORBIDDEN and ran == []
    assert [(e.type, e.data.get("state")) for e in sink.events] == [(EventType.ERROR, None),
                                                                    (EventType.STATE, "failed")]
    call(_catalog(ran), "test.secret", {}, ExecutionContext.create(SimpleNamespace(), ListSink(),
                                                                   allow_privileged=True))
    assert ran == ["secret"]


def test_call_command_refuses_without_allow_privileged(monkeypatch, capsys):
    from cli.logging import Logger
    from cli.parser import build_parser

    ran: list[str] = []
    monkeypatch.setattr("cli.service_adapter.CATALOG", _catalog(ran))

    def run(*argv: str) -> int:
        args = build_parser().parse_args(["call", "test.secret", *argv])
        return int(args.func(args, cast(Any, SimpleNamespace()), Logger(fmt="events", stream=io.StringIO())))

    assert run() == 3
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "forbidden" and ran == []
    # Refus avant toute lecture : ni fichier absent signalé, ni stdin attendu.
    assert run("--request", "/fichier/absent.json") == 3
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "forbidden"
    monkeypatch.setattr("sys.stdin", SimpleNamespace(read=lambda: pytest.fail("stdin lu avant le refus")))
    assert run("--request", "-") == 3
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "forbidden"
    assert run("--allow-privileged") == 0 and ran == ["secret"]


def test_refusal_precedes_the_reading_of_the_api_request(monkeypatch):
    """Requête jamais parcourue pour une opération refusée ; refus d'imbrication et de nombre hors plage."""
    monkeypatch.setattr("core.service.operations.api_payload", lambda _payload: pytest.fail("requête parcourue"))
    deep: dict[str, Any] = {}
    for _ in range(5000):
        deep = {"x": deep}
    with pytest.raises(ApiError) as caught:
        call(_catalog([]), "test.secret", deep, ExecutionContext.create(SimpleNamespace(), ListSink()))
    assert caught.value.code is ErrorCode.FORBIDDEN
    monkeypatch.undo()
    for payload in (deep, {"x": 10 ** 400}, {"x": float("inf")}):
        sink = ListSink()
        with pytest.raises(ApiError) as caught:
            call(_catalog([]), "test.secret", payload,
                 ExecutionContext.create(SimpleNamespace(), sink, allow_privileged=True))
        assert caught.value.code is ErrorCode.INVALID_REQUEST
        assert [e.type for e in sink.events] == [EventType.ERROR, EventType.STATE]


def test_api_payload_walk_stays_linear_with_long_keys():
    """Chemin de diagnostic construit seulement pour un refus : une très longue clé ne se recopie pas par élément."""
    import time

    from core.service.models import api_payload

    payload: dict[str, Any] = {"k" * 256: [0] * 1_000_000 + [1.0]}
    for _ in range(60):  # 60 niveaux de clés de 256 caractères : chemin de 15 000 caractères jamais recopié
        payload = {"k" * 256: payload}
    started = time.monotonic()
    leaf = api_payload(payload)
    for _ in range(61):
        leaf = leaf["k" * 256]
    assert leaf[-1] == 1 and time.monotonic() - started < 10
    with pytest.raises(ApiError, match="clé de plus de 256 caractères"):
        api_payload({"k" * 257: 0})
    with pytest.raises(ApiError) as caught:
        api_payload({"a": [{"b": 10 ** 400}]})
    assert caught.value.message.startswith("a[0].b : nombre hors")
