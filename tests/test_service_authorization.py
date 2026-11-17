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
    assert run("--allow-privileged") == 0 and ran == ["secret"]
