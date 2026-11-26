"""
tests/test_sidecar_output.py — Sorties annexes protégées (N1, plan de remédiation 5.2.0, lot 1).

Une annexe (rapport, calibration, template, résumé, exact-job, recette, NFO, PNG) n'est jamais écrite sur un fichier
que la requête lit ou produit ; tout est contrôlé avant la première écriture et le premier job.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import cli.batch as batch_mod
import cli.hdr as cli_hdr
from cli.constants import EXIT_ARGS, EXIT_OK
from cli.logging import Logger
from cli.parser import build_parser
from core.config import AppConfig
from core.jobs.errors import JobError
from core.jobs.json_io import write_json
from core.service import ExecutionContext, ListSink, call
from core.service.catalog import CATALOG
from core.service.errors import ApiError, ErrorCode
from core.service.ops import batch as batch_ops
from core.service.ops import hybrid as hybrid_ops
from core.sidecar_output import (
    SidecarConflictError,
    SidecarGuard,
    nfo_input_conflict,
    publish_pending,
    stage_bytes,
)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _media(path: Path) -> Path:
    path.write_bytes(b"\x1a\x45\xdf\xa3" + b"\0" * 64)
    return path


# ── Garde ─────────────────────────────────────────────────────────────────────


def test_claim_refuses_protected_file_and_aliases(tmp_path):
    source = _media(tmp_path / "film.mkv")
    guard = SidecarGuard([(source, "source")])
    with pytest.raises(SidecarConflictError) as caught:
        guard.claim(source, "rapport")
    assert caught.value.exit_code == EXIT_ARGS and "source" in str(caught.value)
    link = tmp_path / "lien.mkv"
    link.symlink_to(source)
    with pytest.raises(SidecarConflictError):
        guard.claim(link, "rapport")  # lien symbolique
    hard = tmp_path / "ailleurs" / "physique.mkv"
    hard.parent.mkdir()
    os.link(source, hard)
    with pytest.raises(SidecarConflictError):
        guard.claim(hard, "rapport")  # lien physique, autre dossier
    assert _digest(source) == hashlib.sha256(b"\x1a\x45\xdf\xa3" + b"\0" * 64).hexdigest()


def test_two_sidecars_on_one_path_are_refused(tmp_path):
    guard = SidecarGuard()
    guard.claim(tmp_path / "a.json", "rapport")
    with pytest.raises(SidecarConflictError):
        guard.claim(tmp_path / "a.json", "résumé")


def test_unrelated_existing_sidecar_is_replaced_decision_da(tmp_path):
    """Non-régression : une annexe existante sans lien avec la requête est remplacée (décision DA)."""
    existing = tmp_path / "rapport.json"
    existing.write_text("ancien", encoding="utf-8")
    guard = SidecarGuard([(_media(tmp_path / "film.mkv"), "source")])
    guard.claim(existing, "rapport")
    guard.write_text(existing, "nouveau")
    assert existing.read_text(encoding="utf-8") == "nouveau"


def test_verify_sees_a_link_retargeted_after_the_claim(tmp_path):
    source = _media(tmp_path / "film.mkv")
    guard = SidecarGuard([(source, "source")])
    link = tmp_path / "rapport.json"
    guard.claim(link, "rapport")
    link.symlink_to(source)  # reciblé entre la réservation et l'écriture
    with pytest.raises(SidecarConflictError):
        guard.write_text(link, "{}")
    assert source.read_bytes().startswith(b"\x1a\x45\xdf\xa3")


@pytest.mark.skipif(sys.platform == "win32", reason="droits POSIX")
def test_write_json_keeps_replaced_mode_creates_parents_and_uses_umask(tmp_path):
    """Non-régression : ``write_json`` devenu atomique garde les droits et les dossiers créés de l'écriture directe."""
    private = tmp_path / "prive.json"
    private.write_text("{}", encoding="utf-8")
    private.chmod(0o600)
    write_json(private, {"a": 1})
    assert stat.S_IMODE(private.stat().st_mode) == 0o600 and json.loads(private.read_text()) == {"a": 1}
    previous = os.umask(0o022)
    try:
        fresh = tmp_path / "nouveau" / "dossier" / "resume.json"
        write_json(fresh, {"b": 2})
    finally:
        os.umask(previous)
    assert stat.S_IMODE(fresh.stat().st_mode) == 0o644  # création ordinaire, jamais le 0600 d'un temporaire
    assert not [p for p in fresh.parent.iterdir() if p.name.startswith(".")]


def test_nfo_conflict_with_an_input():
    output = Path("/films/x.mkv")
    assert nfo_input_conflict(output, [Path("/films/x.nfo")]) is not None
    assert nfo_input_conflict(output, [Path("/films/y.nfo"), None]) is None


# ── Publication en une passe (moments FEL) ──────────────────────────────────


def test_publish_refuses_collision_and_removes_candidates(tmp_path):
    source = _media(tmp_path / "film.mkv")
    guard = SidecarGuard([(source, "source")])
    out = tmp_path / "moments"
    pending = [stage_bytes(out / "a.png", b"A", role="image"), stage_bytes(source, b"B", role="image")]
    assert all(item.candidate.parent == item.final.parent for item in pending)  # même dossier, même volume
    with pytest.raises(SidecarConflictError):
        publish_pending(guard, pending)
    assert not (out / "a.png").exists() and _digest(source) == _digest(_media(tmp_path / "ref.mkv"))
    assert not [p for p in out.iterdir() if p.name.endswith(".partial")]
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".partial")]


def test_publish_failure_midway_keeps_first_and_reports(tmp_path):
    guard = SidecarGuard()
    pending = [stage_bytes(tmp_path / f"{name}.png", name.encode(), role="image") for name in ("a", "b", "c")]
    calls: list[Path] = []

    def rename(src: Path, dst: Path) -> None:
        calls.append(dst)
        if len(calls) == 2:
            raise OSError(28, "No space left on device")
        os.replace(src, dst)

    with pytest.raises(JobError) as caught:
        publish_pending(guard, pending, lock_dir=tmp_path / "locks", rename=rename)
    message = str(caught.value)
    assert (tmp_path / "a.png").read_bytes() == b"a" and not (tmp_path / "b.png").exists()
    assert "a.png" in message and "b.png" in message and "c.png" in message
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".partial")]


def test_publish_busy_name_publishes_nothing_and_releases_reservations(tmp_path):
    from core.output_commit import OutputReservation

    locks = tmp_path / "locks"
    held = OutputReservation.acquire(tmp_path / "b.png", lock_dir=locks)
    try:
        pending = [stage_bytes(tmp_path / f"{name}.png", b"x", role="image") for name in ("a", "b")]
        with pytest.raises(ApiError) as caught:
            publish_pending(SidecarGuard(), pending, lock_dir=locks)
        assert caught.value.code is ErrorCode.BUSY
        assert not (tmp_path / "a.png").exists() and not (tmp_path / "b.png").exists()
    finally:
        held.release()
    # Réservation de « a.png » libérée : une nouvelle publication passe.
    published = publish_pending(SidecarGuard(), [stage_bytes(tmp_path / "a.png", b"y", role="image")],
                                lock_dir=locks)
    assert published == [tmp_path / "a.png"]


def test_fel_moments_of_same_named_sources_get_side_suffixes(tmp_path, monkeypatch):
    import numpy as np

    from core.fel import engine as fel_engine
    from core.hdr_inspection import fel_analyzer
    from core.hdr_inspection.fel_analyzer import FelReport, MomentImages
    from core.service.ops.hdr import stage_fel_moments
    from tests.test_cli_hdr_patch import _report

    monkeypatch.setattr(fel_engine.FelEngine, "installed", classmethod(lambda cls: object()))
    monkeypatch.setattr(fel_analyzer, "plugin_capture", lambda *a, **k: lambda layer, t, n: [object()])
    monkeypatch.setattr(fel_analyzer, "active_rows", lambda frame, area=None: slice(None))
    codes = np.full((2, 3, 3), 30000, dtype=np.uint16)
    monkeypatch.setattr(fel_analyzer, "moment_images", lambda r, m, rows, t: MomentImages(
        t, codes, codes, np.zeros((2, 3, 3), dtype=np.uint8)))
    report = _report(tmp_path)
    fel = FelReport(None, (), (), (SimpleNamespace(time_s=5.0),))  # type: ignore[arg-type]
    logger = SimpleNamespace(emit=lambda *a, **k: None)
    pending = []
    for side in "AB":
        pending += stage_fel_moments(report, fel, tmp_path / "m", exposure=0.0, ffmpeg="ffmpeg", threads=2,
                                     logger=logger, side=side)  # type: ignore[arg-type]
    published = publish_pending(SidecarGuard(), pending, lock_dir=tmp_path / "locks")
    names = sorted(path.name for path in published)
    assert len(names) == 6 and len(set(names)) == 6
    assert names[0].startswith("film.A.fel-") and names[-1].startswith("film.B.fel-")


# ── Commandes ─────────────────────────────────────────────────────────────────


def _hdr_cli(tmp_path: Path, monkeypatch, *argv: str) -> int:
    from tests.test_hdr_studio import _FakeInspector

    monkeypatch.setattr(cli_hdr, "FileInspector", _FakeInspector)
    config = AppConfig()
    config.app_data_dir = tmp_path / "appdata"
    args = build_parser().parse_args(["inspect", *argv, "--work-dir", str(tmp_path / "work")])
    return args.func(args, config, Logger())


def test_hdr_report_on_the_source_is_refused(tmp_path, monkeypatch):
    """Reproduction du contre-audit : `inspect F.mkv --hdr static --hdr-report F.mkv` remplaçait le MKV."""
    source = _media(tmp_path / "film.mkv")
    before = _digest(source)
    with pytest.raises(JobError) as caught:
        _hdr_cli(tmp_path, monkeypatch, str(source), "--hdr", "static", "--hdr-report", str(source))
    assert caught.value.exit_code == EXIT_ARGS and _digest(source) == before
    with pytest.raises(JobError):  # B d'une comparaison aussi
        _hdr_cli(tmp_path, monkeypatch, str(source), str(_media(tmp_path / "b.mkv")), "--hdr", "static",
                 "--hdr-compare", "--hdr-report", str(tmp_path / "b.mkv"))


def test_call_report_on_its_own_request_document_is_refused(tmp_path, monkeypatch, capsys):
    from tests.test_hdr_studio import _FakeInspector

    monkeypatch.setattr(cli_hdr, "FileInspector", _FakeInspector)
    source = _media(tmp_path / "film.mkv")
    request = tmp_path / "requete.json"
    request.write_text(json.dumps({"inputs": [str(source)], "level": "static", "report": str(request)}),
                       encoding="utf-8")
    before = _digest(request)
    config = AppConfig()
    config.app_data_dir = tmp_path / "appdata"
    args = build_parser().parse_args(["call", "hdr.inspect", "--request", str(request)])
    code = args.func(args, config, Logger())
    response = json.loads(capsys.readouterr().out)
    assert code == EXIT_ARGS and response["error"]["code"] == "invalid_request"
    assert _digest(request) == before


def test_sync_scan_output_on_the_reference_is_refused_before_analysis(tmp_path):
    reference = _media(tmp_path / "ref.mkv")
    target = _media(tmp_path / "cible.mkv")
    before = _digest(reference)
    analysed: list[Any] = []
    ctx = ExecutionContext.create(AppConfig(), ListSink(), ports={
        "audio_scanner": lambda: analysed.append(1), "detect_stream_kind": lambda *a, **k: "audio"})
    with pytest.raises(ApiError) as caught:
        call(CATALOG, "sync.scan", {"ref": str(reference), "target": str(target), "output_json": str(reference)},
             ctx, base_dir=tmp_path)
    assert caught.value.code is ErrorCode.INVALID_REQUEST and analysed == [] and _digest(reference) == before


def test_remux_save_on_its_config_or_source_is_refused(tmp_path, monkeypatch):
    from cli.commands import cmd_remux

    source = _media(tmp_path / "film.mkv")
    job = tmp_path / "job.json"
    job.write_text(json.dumps({"version": 1, "sources": [str(source)], "output": str(tmp_path / "out.mkv")}),
                   encoding="utf-8")
    ran: list[Any] = []
    monkeypatch.setattr("cli.commands._run_job", lambda *a, **k: ran.append(1) or EXIT_OK)
    for target in (job, source):
        before = _digest(target)
        args = build_parser().parse_args(["remux", "--config", str(job), "--save", str(target)])
        with pytest.raises(JobError) as caught:
            cmd_remux(args, AppConfig(), Logger())
        assert caught.value.exit_code == EXIT_ARGS and _digest(target) == before
    assert ran == []


def test_calibration_edit_force_on_itself_is_accepted_but_probed_media_is_protected(tmp_path):
    calibration = tmp_path / "calage.json"
    calibration.write_text(json.dumps({"segments": [{"start_ms": 0, "shift_ms": 0}]}), encoding="utf-8")
    ctx = ExecutionContext.create(AppConfig(), ListSink())
    call(CATALOG, "calibration.edit", {"calibration": {"file": str(calibration)}, "edits": [{"shift_ms": 40}],
                                       "output": str(calibration), "force": True}, ctx, base_dir=tmp_path)
    assert json.loads(calibration.read_text(encoding="utf-8"))["segments"][0]["shift_ms"] == 40
    media = _media(tmp_path / "ref.mkv")
    before = _digest(media)
    with pytest.raises(ApiError) as caught:
        call(CATALOG, "calibration.edit", {
            "calibration": {"file": str(calibration)}, "edits": [{"shift_ms": 10}],
            "resolve_cadence": [{"path": str(media), "stream": 1}, {"path": str(media), "stream": 2}],
            "output": str(media), "force": True}, ctx, base_dir=tmp_path)
    assert caught.value.code is ErrorCode.INVALID_REQUEST and _digest(media) == before


# ── Lots ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def batch_doubles(monkeypatch):
    ran: list[Path] = []

    def build(job, _config, _options, _logger):
        output = Path(str(job["output"])).expanduser().absolute()
        sources = [SimpleNamespace(path=Path(str(item)).absolute()) for item in job.get("sources", [])]
        return SimpleNamespace(output=output, sources=sources, extra_attachments=[],
                               allow_missing_output_dir=False)

    def run(_config, _options, _logger, remux_config, *, force=False):
        ran.append(remux_config.output)
        return EXIT_OK

    monkeypatch.setattr(batch_mod, "build_remux_config", build)
    monkeypatch.setattr(batch_mod, "run_remux_config", run)
    monkeypatch.setattr(batch_mod, "preview_remux_config", lambda *_a, **_k: EXIT_OK)
    return ran


def _batch(tmp_path: Path, items: list, *, summary: Path | None, config: Any = None, dry_run: bool = False) -> int:
    template = tmp_path / "template.json"
    template.write_text(json.dumps({"version": 1}), encoding="utf-8")
    batch = tmp_path / "batch.json"
    batch.write_text(json.dumps({"jobs": items}), encoding="utf-8")
    return batch_mod.run_batch(
        template_path=str(template), batch_path=str(batch), cli_inputs=None, input_dirs=None, recursive=False,
        include_patterns=None, exclude_patterns=None, output_dir=None, dry_run=dry_run, force=False,
        continue_on_error=False, summary_path=str(summary) if summary else None, config=config, options=None,
        logger=SimpleNamespace(emit=lambda *a, **k: None))  # type: ignore[arg-type]


def test_batch_summary_on_its_template_is_refused_before_the_first_job(tmp_path, batch_doubles):
    items = [{"sources": [str(_media(tmp_path / "a.mkv"))], "output": str(tmp_path / "a.out.mkv")}]
    with pytest.raises(JobError):
        _batch(tmp_path, items, summary=tmp_path / "template.json")
    assert batch_doubles == [] and json.loads((tmp_path / "template.json").read_text()) == {"version": 1}


def test_batch_nfo_of_the_last_job_on_an_input_is_refused_before_any_job(tmp_path, batch_doubles):
    nfo_source = _media(tmp_path / "b.out.nfo")  # source du job 1 = NFO que le job 2 écrirait
    items = [{"sources": [str(nfo_source)], "output": str(tmp_path / "a.out.mkv")},
             {"sources": [str(_media(tmp_path / "b.mkv"))], "output": str(tmp_path / "b.out.mkv")}]
    config = SimpleNamespace(generate_nfo=True)
    with pytest.raises(JobError):
        _batch(tmp_path, items, summary=None, config=config)
    assert batch_doubles == []
    # Aperçu : aucun NFO écrit, aucune collision fictive.
    assert _batch(tmp_path, items, summary=None, config=config, dry_run=True) == EXIT_OK


# ── Hybrid ────────────────────────────────────────────────────────────────────


def _items(tmp_path: Path, count: int) -> list[Any]:
    return [SimpleNamespace(error=None, calibration=None,
                            remux_config=SimpleNamespace(output=tmp_path / f"E{i}.mkv", sources=[],
                                                         extra_attachments=[]),
                            episode=SimpleNamespace(display_name=f"E{i}", master_file=_media(tmp_path / f"m{i}.mkv"),
                                                    donor_files=[]))
            for i in range(count)]


def _hybrid(monkeypatch, tmp_path: Path, items: list[Any], config: Any, **request: Any):
    spec = SimpleNamespace(common=None, output_template=None)
    monkeypatch.setattr(hybrid_ops, "_prepare", lambda request, ctx, cache=None: (spec, items))
    monkeypatch.setattr(hybrid_ops, "check_outputs", lambda *a, **k: None)
    ran: list[Any] = []
    monkeypatch.setattr(batch_ops._Children, "run", lambda self, *a, **k: ran.append(a[-1].output) or 0)
    ctx = ExecutionContext.create(config, ListSink(), base_dir=tmp_path)
    payload = {"output_dir": str(tmp_path), **request}
    return hybrid_ops.hybrid_run.handler(hybrid_ops.HybridRequest.from_dict(payload), ctx), ran


def test_hybrid_report_on_a_master_is_refused_before_the_first_episode(monkeypatch, tmp_path):
    items = _items(tmp_path, 2)
    master = items[1].episode.master_file
    before = _digest(master)
    with pytest.raises(JobError):
        _hybrid(monkeypatch, tmp_path, items, SimpleNamespace(), report_json=str(master))
    assert _digest(master) == before


def test_hybrid_nfo_of_the_last_episode_on_an_input_is_refused(monkeypatch, tmp_path):
    items = _items(tmp_path, 2)
    items[0].episode.master_file = _media(tmp_path / "E1.nfo")  # NFO du dernier épisode = master du premier
    ran: list[Any] = []
    with pytest.raises(JobError):
        _, ran = _hybrid(monkeypatch, tmp_path, items, SimpleNamespace(generate_nfo=True))
    assert ran == []
    monkeypatch.setattr(batch_ops._Children, "preview", lambda self, *a, **k: 0)
    result, ran = _hybrid(monkeypatch, tmp_path, items, SimpleNamespace(generate_nfo=True), dry_run=True)
    assert result.status == "succeeded" and ran == []  # aperçu : NFO non réservé, aucune collision fictive
    assert [entry["status"] for entry in result.episodes] == ["planned", "planned"]


def test_hybrid_export_job_on_a_master_is_refused(monkeypatch, tmp_path):
    from core.profiles import selectors

    monkeypatch.setattr(selectors, "remux_config_to_exact_job", lambda config: {"version": 1,
                                                                                "output": str(config.output)})
    items = _items(tmp_path, 1)
    master = items[0].episode.master_file
    before = _digest(master)
    with pytest.raises(JobError):
        _hybrid(monkeypatch, tmp_path, items, SimpleNamespace(), export_workflow=str(master))
    assert _digest(master) == before


# ── NFO d'un job unique ───────────────────────────────────────────────────────


def test_encode_nfo_on_an_input_attachment_is_refused_before_the_job(qt_app, tmp_path):
    from core.workflows.encode.models import EncodeConfig, EncodeError, VideoEncodeSettings
    from core.workflows.encode.workflow import EncodeWorkflow

    source = _media(tmp_path / "film.mkv")
    attachment = tmp_path / "sortie.nfo"
    attachment.write_text("notes de l'utilisateur", encoding="utf-8")
    config = EncodeConfig(source=source, output=tmp_path / "sortie.mkv", extra_attachments=[attachment],
                          video=VideoEncodeSettings(codec="libx264"))
    with pytest.raises(EncodeError) as caught:
        EncodeWorkflow(generate_nfo=True).run(config)
    assert "NFO" in str(caught.value) and attachment.read_text(encoding="utf-8") == "notes de l'utilisateur"


def test_remux_nfo_on_an_input_attachment_is_refused_by_the_validation(qt_app, tmp_path, monkeypatch):
    import core.workflows.remux as remux_module
    from core.workflows.remux import RemuxWorkflow
    from core.workflows.remux_models import RemuxValidationError

    attachment = tmp_path / "sortie.nfo"
    attachment.write_text("notes", encoding="utf-8")
    config = SimpleNamespace(output=tmp_path / "sortie.mkv", sources=[], extra_attachments=[attachment],
                             clean_nfo=True)
    workflow = RemuxWorkflow(generate_nfo=True)
    plan = SimpleNamespace(requested_backend="auto", selected_backend="native")
    monkeypatch.setattr(workflow, "_absolute_paths_config", lambda value: value)
    monkeypatch.setattr(workflow, "compile_plan", lambda value: plan)
    monkeypatch.setattr(workflow, "validate", lambda value, plan=None: [])
    monkeypatch.setattr(remux_module, "mux_backend_report_from_plan", lambda value: {})
    with pytest.raises(RemuxValidationError) as caught:
        workflow.run(config)  # type: ignore[arg-type]
    assert "NFO" in str(caught.value) and attachment.read_text(encoding="utf-8") == "notes"


# ── Corrections de la relecture (lot 1) ───────────────────────────────────────


def test_remux_save_never_targets_the_exact_job_or_the_nfo_of_the_same_job(tmp_path, monkeypatch):
    from cli.commands import cmd_remux

    source = _media(tmp_path / "film.mkv")
    job = tmp_path / "job.json"
    job.write_text(json.dumps({"version": 1, "sources": [str(source)], "output": str(tmp_path / "sortie.mkv")}),
                   encoding="utf-8")
    ran: list[Any] = []
    monkeypatch.setattr("cli.commands._run_job", lambda *a, **k: ran.append(1) or EXIT_OK)
    target = tmp_path / "x.json"
    args = build_parser().parse_args(["remux", "--config", str(job), "--save", str(target),
                                      "--export-workflow", str(target)])
    with pytest.raises(JobError):
        cmd_remux(args, AppConfig(), Logger())
    assert not target.exists() and ran == []
    config = AppConfig()
    config.generate_nfo = True
    args = build_parser().parse_args(["remux", "--config", str(job), "--save", str(tmp_path / "sortie.nfo")])
    with pytest.raises(JobError):
        cmd_remux(args, config, Logger())
    assert not (tmp_path / "sortie.nfo").exists() and ran == []


def test_media_run_nfo_never_replaces_the_request_document(tmp_path, monkeypatch):
    from core.service.ops import media as media_ops

    request = tmp_path / "sortie.nfo"
    request.write_text("{}", encoding="utf-8")
    before = _digest(request)
    monkeypatch.setattr(media_ops, "build_remux_config",
                        lambda *a, **k: SimpleNamespace(output=tmp_path / "sortie.mkv", sources=[],
                                                        extra_attachments=[]))
    ran: list[Any] = []
    monkeypatch.setattr(media_ops, "run_remux", lambda *a, **k: ran.append(1) or "")
    ctx = ExecutionContext.create(SimpleNamespace(generate_nfo=True), ListSink(), base_dir=tmp_path,
                                  protected_paths=(request,))
    with pytest.raises(ApiError) as caught:
        call(CATALOG, "media.run", {"job": {"version": 1, "sources": [str(_media(tmp_path / "a.mkv"))],
                                            "output": str(tmp_path / "sortie.mkv")}}, ctx)
    assert caught.value.code is ErrorCode.INVALID_REQUEST and ran == [] and _digest(request) == before
