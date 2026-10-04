"""
tests/test_merge_dovi_policy.py — Politique explicite des métadonnées, espace disque
et nettoyage du workflow Merge DoVi.
"""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from core.workdir import PROCESS_DIR_MARKER, create_process_work_dir
from core.workflows.encode.runtime.frame_count_guard import MetadataAdjustment
from core.workflows.merge_dovi import (
    DoviProfile,
    FrameCountResult,
    HDRFlags,
    MergeDoviWorkflow,
    StaticHdrMetadata,
    ValidationContext,
    WorkflowError,
    WorkflowStep,
    _WorkflowPaths,
)
from core.workflows.merge_dovi_storage import (
    MergeStorageInputs,
    merge_storage_phases,
    storage_margin,
    storage_requirement,
)

EXACT = MetadataAdjustment.EXACT
TRIM = MetadataAdjustment.TRIM_TAIL
GIB = 1024 ** 3


def _paths(tmp_path: Path, film1: Path, *, owned: bool = False) -> _WorkflowPaths:
    output_dir = tmp_path / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    if owned:
        job = create_process_work_dir(tmp_path / "work", process_name="job")
        return _WorkflowPaths.from_config(job.path, output_dir, film1, "out", owned_dir=job)
    work_dir = tmp_path / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    return _WorkflowPaths.from_config(work_dir, output_dir, film1, "out")


def _completed(stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def test_hdr10_only_does_not_extract_film2(tmp_path, monkeypatch):
    film1 = tmp_path / "film1.hevc"
    film2 = tmp_path / "film2.mp4"
    paths = _paths(tmp_path, film1)
    wf = MergeDoviWorkflow()
    monkeypatch.setattr(wf, "_extract_hevc", lambda *_a: pytest.fail("extraction inutile"))
    monkeypatch.setattr(wf, "_video_stream_bytes", lambda *_a: pytest.fail("Film 2 ne doit pas être compté pour l'extraction"))
    wf._step_extract_hevc(film1, film2, paths, HDRFlags(False, False), None)
    assert not paths.film2_hevc.exists()


# ---------------------------------------------------------------------------
# Politique de comptage
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fc1,fc2,adjustment,refused", [
    (1000, 1000, EXACT, None),
    (1000, 1002, EXACT, "politique exacte"),
    (1000, 1002, TRIM, None),
    (1000, 998, TRIM, "aucune trame de métadonnées n'est fabriquée"),
    (1000, 1010, TRIM, "Écart de 10 frames"),
    (None, 1000, TRIM, "illisible"),
])
def test_metadata_verdict(fc1, fc2, adjustment, refused) -> None:
    diff = abs(fc2 - fc1) if fc1 is not None and fc2 is not None else None
    verdict = FrameCountResult(fc1, fc2, diff).metadata_verdict(adjustment)
    if refused is None:
        assert verdict is None
    else:
        assert verdict is not None and refused in verdict


def _framecount_workflow(counts: dict[Path, int | None]) -> tuple[MergeDoviWorkflow, list[str]]:
    wf = MergeDoviWorkflow()
    cast(Any, wf)._get_framecount = lambda path: counts[path]
    messages: list[str] = []
    wf.step_progress.connect(lambda _step, message: messages.append(message))
    return wf, messages


def test_framecount_exact_refuses_small_surplus_with_hint(tmp_path: Path) -> None:
    film1, film2 = tmp_path / "f1.mkv", tmp_path / "f2.mkv"
    wf, _messages = _framecount_workflow({film1: 1000, film2: 1002})
    with pytest.raises(WorkflowError, match="Retirer l'excédent"):
        wf._step_framecount(film1, film2, adjustment=EXACT)


def test_framecount_trim_tail_accepts_small_surplus_and_says_so(tmp_path: Path) -> None:
    film1, film2 = tmp_path / "f1.mkv", tmp_path / "f2.mkv"
    wf, messages = _framecount_workflow({film1: 1000, film2: 1002})
    result = wf._step_framecount(film1, film2, adjustment=TRIM)
    assert result.diff == 2
    assert any("début supposé aligné" in m for m in messages)


def test_framecount_identical_counts_do_not_claim_alignment(tmp_path: Path) -> None:
    film1, film2 = tmp_path / "f1.mkv", tmp_path / "f2.mkv"
    wf, messages = _framecount_workflow({film1: 1000, film2: 1000})
    wf._step_framecount(film1, film2)
    assert any("n'est pas vérifié" in m for m in messages)


def test_framecount_unreadable_counts_refuse_metadata_injection(tmp_path: Path) -> None:
    film1, film2 = tmp_path / "f1.mkv", tmp_path / "f2.mkv"
    wf, _messages = _framecount_workflow({film1: None, film2: 1000})
    with pytest.raises(WorkflowError, match="illisible"):
        wf._step_framecount(film1, film2, adjustment=TRIM)
    # Sans métadonnées dynamiques à injecter, le comptage n'est pas bloquant.
    wf._step_framecount(film1, film2, metadata_requested=False)


def test_framecount_raw_film2_is_not_read_and_defers_to_metadata(tmp_path: Path) -> None:
    """Film 2 HEVC brut : aucun comptage vidéo (lecture complète évitée), contrôle à CHECK_METADATA."""
    film1, film2 = tmp_path / "f1.mkv", tmp_path / "f2.hevc"
    wf = MergeDoviWorkflow()
    counted: list[Path] = []
    def count_frame(path: Path) -> int:
        counted.append(path)
        return 1000

    cast(Any, wf)._get_framecount = count_frame
    result = wf._step_framecount(film1, film2, adjustment=EXACT)
    assert counted == [film1]
    assert result.fc2_deferred and result.metadata_verdict(EXACT) is None
    assert "métadonnées" in result.status_text


def test_verify_reuses_film1_count_without_rereading_film1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    film1 = tmp_path / "film1.hevc"
    film1.write_bytes(b"film1")
    paths = _paths(tmp_path, film1)
    paths.film1_with_dovi.write_bytes(b"hevc")
    read: list[str] = []

    def _fake_run(cmd, **_kwargs):
        read.append(cmd[-1])
        if Path(cmd[0]).name == "mediainfo" or "-count_packets" in cmd:
            return _completed("1000")
        if "info" in cmd:
            return _completed("Frames: 1000\n")
        return _completed()

    monkeypatch.setattr(MergeDoviWorkflow, "_run_probe", lambda self, cmd, **kw: _fake_run(cmd, **kw))
    wf = MergeDoviWorkflow()
    cast(Any, wf)._run_raw = lambda cmd, step=None: Path(cmd[cmd.index("-o") + 1]).write_bytes(b"rpu") or ""
    wf._step_verify(film1, paths, HDRFlags(has_dovi=True), film1_frames=1000)
    assert str(film1) not in read


# ---------------------------------------------------------------------------
# Contrôle des métadonnées extraites (avant injection)
# ---------------------------------------------------------------------------

def _metadata_workflow(monkeypatch: pytest.MonkeyPatch, rpu_counts: list[int]) -> tuple[MergeDoviWorkflow, list[list[str]]]:
    calls: list[list[str]] = []
    counts = iter(rpu_counts)

    def _fake_run(cmd, **_kwargs):
        calls.append(list(cmd))
        if "info" in cmd:
            return _completed(f"Frames: {next(counts)}\n")
        if "editor" in cmd:
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"trimmed")
        return _completed()

    monkeypatch.setattr(MergeDoviWorkflow, "_run_probe", lambda self, cmd, **kw: _fake_run(cmd, **kw))
    return MergeDoviWorkflow(), calls


def test_check_metadata_exact_refuses_rpu_surplus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = _paths(tmp_path, tmp_path / "f1.mkv")
    paths.film2_rpu.write_bytes(b"rpu")
    wf, calls = _metadata_workflow(monkeypatch, [1002])
    with pytest.raises(WorkflowError, match="politique exacte"):
        wf._step_check_metadata(FrameCountResult(1000, 1002, 2), paths, HDRFlags(has_dovi=True), EXACT)
    assert not any("editor" in c for c in calls)
    assert paths.film2_rpu.read_bytes() == b"rpu"


def test_check_metadata_trim_tail_removes_surplus_before_injection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path, tmp_path / "f1.mkv")
    paths.film2_rpu.write_bytes(b"rpu")
    wf, calls = _metadata_workflow(monkeypatch, [1002, 1000])
    flags = wf._step_check_metadata(FrameCountResult(1000, 1002, 2), paths, HDRFlags(has_dovi=True), TRIM)
    assert flags.has_dovi
    assert any("editor" in c for c in calls)
    assert paths.film2_rpu.read_bytes() == b"trimmed"


def test_check_metadata_short_rpu_falls_back_to_hdr10_for_sdr_film1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path, tmp_path / "f1.mkv")
    paths.film2_rpu.write_bytes(b"rpu")
    wf, _calls = _metadata_workflow(monkeypatch, [998])
    flags = wf._step_check_metadata(
        FrameCountResult(1000, 1000, 0), paths, HDRFlags(has_dovi=True), TRIM, allow_hdr10_fallback=True,
    )
    assert not flags.has_dovi and not flags.has_hdr10plus


# ---------------------------------------------------------------------------
# VERIFY : relecture du flux final, sans ajustement
# ---------------------------------------------------------------------------

def test_verify_rereads_injected_rpu_and_refuses_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    film1 = tmp_path / "film1.mkv"
    film1.write_bytes(b"film1")
    paths = _paths(tmp_path, film1)
    paths.film1_with_dovi.write_bytes(b"hevc")

    def _fake_run(cmd, **_kwargs):
        if Path(cmd[0]).name == "mediainfo" or "-count_packets" in cmd:
            return _completed("1000")
        if "info" in cmd:
            return _completed("Frames: 999\n")
        return _completed()

    monkeypatch.setattr(MergeDoviWorkflow, "_run_probe", lambda self, cmd, **kw: _fake_run(cmd, **kw))
    wf = MergeDoviWorkflow()

    def _fake_run_raw(cmd, step=None):
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"rpu")
        return ""

    cast(Any, wf)._run_raw = _fake_run_raw
    with pytest.raises(WorkflowError, match="RPU Dolby Vision du flux final : 999 trames contre 1000"):
        wf._step_verify(film1, paths, HDRFlags(has_dovi=True))
    assert not paths.verify_rpu.exists()


def test_verify_does_not_accept_plausible_estimate_for_final_video(tmp_path, monkeypatch):
    film1 = tmp_path / "film1.mkv"
    paths = _paths(tmp_path, film1)
    paths.film1_hevc.write_bytes(b"candidate")

    def probe(_self, cmd, **_kwargs):
        return _completed("47" if "-count_packets" in cmd else "48")

    monkeypatch.setattr(MergeDoviWorkflow, "_run_probe", probe)
    with pytest.raises(WorkflowError, match="47 trames contre 48"):
        MergeDoviWorkflow()._step_verify(film1, paths, HDRFlags(False, False), film1_frames=48)


# ---------------------------------------------------------------------------
# Espace disque
# ---------------------------------------------------------------------------

def _inputs(**overrides) -> MergeStorageInputs:
    base = MergeStorageInputs(
        film1_bytes=60 * GIB, film1_video_bytes=55 * GIB, film1_raw=False, film1_matroska=True,
        film2_video_bytes=50 * GIB, film2_extracted=False, film2_converted=False,
        sdr_to_hdr10=False, inject_dovi=True, inject_hdr10plus=True, static_hdr_copy=False,
    )
    return replace(base, **overrides)


def test_storage_phases_follow_simultaneous_files() -> None:
    phases = {p.label: p for p in merge_storage_phases(_inputs())}
    assert phases["Extraction HEVC"].work_bytes == 55 * GIB
    # Entrée + sortie de chaque injection, le maillon précédent étant ensuite supprimé.
    assert phases["Injection RPU Dolby Vision"].work_bytes == 110 * GIB
    assert phases["Injection HDR10+"].work_bytes == 110 * GIB
    assert phases["Assemblage final"].work_bytes == 55 * GIB
    assert phases["Assemblage final"].output_bytes == 60 * GIB


def test_storage_requirement_depends_on_volumes() -> None:
    phases = merge_storage_phases(_inputs())
    same = storage_requirement(phases, same_volume=True)
    split = storage_requirement(phases, same_volume=False)
    assert same.work_bytes == 115 * GIB + storage_margin(115 * GIB)   # encapsulée + sortie
    assert same.output_bytes == 0
    assert split.work_bytes == 110 * GIB + storage_margin(110 * GIB)
    assert split.output_bytes == 60 * GIB + storage_margin(60 * GIB)


def test_storage_raw_film1_and_p7_conversion() -> None:
    phases = {p.label: p for p in merge_storage_phases(_inputs(
        film1_raw=True, film1_matroska=False, film1_bytes=55 * GIB,
        film2_extracted=True, film2_converted=True,
    ))}
    assert phases["Extraction HEVC"].work_bytes == 50 * GIB             # Film 2 seul
    assert phases["Conversion Dolby Vision → P8.1"].work_bytes == 100 * GIB
    assert phases["Injection RPU Dolby Vision"].work_bytes == 55 * GIB  # Film 1 brut lu en place
    assert phases["Assemblage final"].work_bytes == 110 * GIB           # encapsulée + canonical


def test_check_storage_refuses_when_free_space_is_short(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    film1 = tmp_path / "f1.mkv"
    film2 = tmp_path / "f2.mkv"
    film1.write_bytes(b"x" * 4096)
    film2.write_bytes(b"x" * 4096)
    paths = _paths(tmp_path, film1)
    validation = ValidationContext(
        flags=HDRFlags(has_dovi=True), static_film1=StaticHdrMetadata(), static_film2=StaticHdrMetadata(),
    )
    class _Usage:
        free = 1024

    monkeypatch.setattr("core.workflows.merge_dovi.shutil.disk_usage", lambda _path: _Usage())
    wf = MergeDoviWorkflow()
    with pytest.raises(WorkflowError, match="Espace insuffisant"):
        wf._step_check_storage(film1, film2, paths, HDRFlags(has_dovi=True), None, validation)


# ---------------------------------------------------------------------------
# Suppression progressive et nettoyage
# ---------------------------------------------------------------------------

def test_discard_never_touches_user_files(tmp_path: Path) -> None:
    film1 = tmp_path / "film1.hevc"
    film1.write_bytes(b"user")
    paths = _paths(tmp_path, film1)
    paths.film1_with_dovi.write_bytes(b"x")
    removed = paths.discard(film1, paths.film1_with_dovi, tmp_path / "autre.mkv")
    assert removed == [paths.film1_with_dovi]
    assert film1.read_bytes() == b"user"


def _run_with_failing_step(wf: MergeDoviWorkflow, paths: _WorkflowPaths, tmp_path: Path, step_fn) -> dict:
    seen: dict[str, Any] = {"failed": [], "cancelled": 0}
    wf.workflow_failed.connect(lambda step, message: seen["failed"].append(message))
    wf.workflow_cancelled.connect(lambda: seen.__setitem__("cancelled", seen["cancelled"] + 1))
    cast(Any, wf)._step_validate = step_fn
    wf._run(tmp_path / "f1.mkv", tmp_path / "f2.mkv", paths, DoviProfile.P8_1)
    return seen


def test_failed_run_removes_owned_process_dir(tmp_path: Path) -> None:
    paths = _paths(tmp_path, tmp_path / "f1.mkv", owned=True)
    (paths.work_dir / "film1.hevc").write_bytes(b"x")

    def _boom(*_args):
        raise WorkflowError(WorkflowStep.VALIDATION, "panne")

    seen = _run_with_failing_step(MergeDoviWorkflow(), paths, tmp_path, _boom)
    assert seen["failed"] == ["panne"]
    assert not paths.work_dir.exists()


def test_failed_run_keeps_dir_without_ownership_proof(tmp_path: Path) -> None:
    paths = _paths(tmp_path, tmp_path / "f1.mkv", owned=True)
    (paths.work_dir / PROCESS_DIR_MARKER).write_text("autre\n", encoding="ascii")

    def _boom(*_args):
        raise WorkflowError(WorkflowStep.VALIDATION, "panne")

    _run_with_failing_step(MergeDoviWorkflow(), paths, tmp_path, _boom)
    assert paths.work_dir.exists()


def test_failed_run_keeps_dir_while_a_process_is_alive(tmp_path: Path) -> None:
    paths = _paths(tmp_path, tmp_path / "f1.mkv", owned=True)
    wf = MergeDoviWorkflow()
    messages: list[str] = []
    wf.step_progress.connect(lambda _step, message: messages.append(message))

    class _StuckProc:
        def poll(self):
            return None

        def kill(self):
            pass

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("tool", timeout or 0.0)

    def _boom(*_args):
        wf._run_procs.append(cast(Any, _StuckProc()))
        raise WorkflowError(WorkflowStep.VALIDATION, "panne")

    _run_with_failing_step(wf, paths, tmp_path, _boom)
    assert paths.work_dir.exists()
    assert any("ne s'est pas arrêté" in m for m in messages)


def test_cancelled_run_emits_cancelled_signal_and_cleans(tmp_path: Path) -> None:
    paths = _paths(tmp_path, tmp_path / "f1.mkv", owned=True)
    wf = MergeDoviWorkflow()

    def _killed(*_args):
        wf._cancelled = True
        raise WorkflowError(WorkflowStep.VALIDATION, "Commande échouée (code -9)")

    seen = _run_with_failing_step(wf, paths, tmp_path, _killed)
    assert seen["cancelled"] == 1 and seen["failed"] == []
    assert not paths.work_dir.exists()


def test_successful_cleanup_removes_owned_dir_with_marker(tmp_path: Path) -> None:
    paths = _paths(tmp_path, tmp_path / "f1.mkv", owned=True)
    paths.film1_wrapped_video.write_bytes(b"x")
    MergeDoviWorkflow()._step_cleanup(paths)
    assert not paths.work_dir.exists()


def test_run_frees_consumed_intermediates_progressively(tmp_path: Path) -> None:
    film1 = tmp_path / "film1.mkv"
    film1.write_bytes(b"film1")
    paths = _paths(tmp_path, film1, owned=True)
    wf = MergeDoviWorkflow()
    present: dict[str, set[str]] = {}

    def _snapshot(label: str) -> None:
        present[label] = {p.name for p in paths.work_dir.iterdir()}

    cast(Any, wf)._step_validate = lambda *_a: ValidationContext(
        flags=HDRFlags(has_dovi=True, has_hdr10plus=True),
        static_film1=StaticHdrMetadata(), static_film2=StaticHdrMetadata(),
    )
    cast(Any, wf)._step_detect_dovi = lambda *_a: None
    cast(Any, wf)._step_framecount = lambda *_a, **_k: FrameCountResult(10, 10, 0)
    cast(Any, wf)._step_check_storage = lambda *_a, **_k: None
    cast(Any, wf)._step_extract_hevc = lambda _f1, _f2, p, *_a: p.film1_hevc.write_bytes(b"hevc")

    def _extract_metadata(*_a):
        paths.film2_rpu.write_bytes(b"rpu")
        paths.film2_hdr10plus.write_text("{}", encoding="utf-8")

    cast(Any, wf)._step_extract_metadata = _extract_metadata
    cast(Any, wf)._step_check_metadata = lambda _c, _p, flags, *_a, **_k: flags

    def _inject_dovi(p, *_a):
        _snapshot("before_dovi")
        p.film1_with_dovi.write_bytes(b"dv")

    def _inject_hdr10plus(p, *_a):
        _snapshot("before_hdr10plus")
        p.film1_final.write_bytes(b"final")

    cast(Any, wf)._step_inject_dovi = _inject_dovi
    cast(Any, wf)._step_inject_hdr10plus = _inject_hdr10plus
    cast(Any, wf)._step_inject_static_hdr = lambda *_a: False
    cast(Any, wf)._step_verify = lambda *_a, **_k: _snapshot("verify")
    cast(Any, wf)._step_remux = lambda *_a, **_k: None
    finished: list[str] = []
    wf.workflow_finished.connect(finished.append)

    wf._run(film1, tmp_path / "film2.mkv", paths, DoviProfile.P8_1)

    assert finished
    assert "film1.hevc" in present["before_dovi"]
    assert "film1.hevc" not in present["before_hdr10plus"]
    assert "film1_with_dovi.hevc" not in present["verify"]
    assert "film1_final.hevc" in present["verify"]
    assert not paths.work_dir.exists()
