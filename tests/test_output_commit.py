"""
tests/test_output_commit.py — Candidats réservés, verrou de destination et publication (A01).
"""

from __future__ import annotations

import errno
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from core import output_commit
from core.output_commit import (
    OutputBusyError,
    OutputChangedError,
    OutputReservation,
    candidate_pattern,
    publish_candidate,
    reserve_candidate,
)
from core.runner import TaskCancelledError, TaskSignals

REPO_ROOT = Path(__file__).resolve().parents[1]


def _lock_dir(tmp_path: Path) -> Path:
    return tmp_path / "locks"


# ---------------------------------------------------------------------------
# Candidats
# ---------------------------------------------------------------------------

def test_reserved_candidates_are_unique_and_keep_mkv_partial_suffix(tmp_path: Path) -> None:
    output = tmp_path / "film.mkv"
    first = reserve_candidate(output)
    second = reserve_candidate(output)
    assert first != second
    for candidate in (first, second):
        assert candidate.parent == tmp_path
        assert candidate.name.startswith("film.") and candidate.name.endswith(".mkv.partial")
        assert candidate.read_bytes() == b""
    assert not output.exists()
    assert candidate_pattern(output).name == "film.<unique>.mkv.partial"


@pytest.mark.skipif(sys.platform == "win32", reason="droits POSIX")
def test_reserved_candidate_uses_default_permissions(tmp_path: Path) -> None:
    previous = os.umask(0o022)
    try:
        candidate = reserve_candidate(tmp_path / "film.mkv")
    finally:
        os.umask(previous)
    # Comme un fichier créé par FFmpeg (pas le 0600 de mkstemp) : lisible par
    # un serveur média tournant sous un autre compte.
    assert candidate.stat().st_mode & 0o777 == 0o644


def test_reserved_candidate_name_fits_filesystem_limit(tmp_path: Path) -> None:
    output = tmp_path / ("é" * 120 + ".mkv")  # 244 octets UTF-8
    candidate = reserve_candidate(output)
    assert len(candidate.name.encode("utf-8")) <= 255
    assert candidate.name.endswith(".mkv.partial")


# ---------------------------------------------------------------------------
# Réservation de destination
# ---------------------------------------------------------------------------

def test_same_destination_is_refused_until_release(tmp_path: Path) -> None:
    output = tmp_path / "out" / "film.mkv"
    first = OutputReservation.acquire(output, lock_dir=_lock_dir(tmp_path))
    with pytest.raises(OutputBusyError):
        OutputReservation.acquire(output, lock_dir=_lock_dir(tmp_path))
    first.release()
    OutputReservation.acquire(output, lock_dir=_lock_dir(tmp_path)).release()
    first.release()  # idempotent


def test_distinct_destinations_are_independent(tmp_path: Path) -> None:
    with OutputReservation.acquire(tmp_path / "a.mkv", lock_dir=_lock_dir(tmp_path)):
        OutputReservation.acquire(tmp_path / "b.mkv", lock_dir=_lock_dir(tmp_path)).release()


def test_relative_and_symlinked_aliases_share_the_reservation(tmp_path: Path, monkeypatch) -> None:
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    monkeypatch.chdir(tmp_path)
    with OutputReservation.acquire(Path("real/film.mkv"), lock_dir=_lock_dir(tmp_path)):
        with pytest.raises(OutputBusyError):
            OutputReservation.acquire(real_dir / "film.mkv", lock_dir=_lock_dir(tmp_path))
        link = tmp_path / "link"
        try:
            link.symlink_to(real_dir, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("liens symboliques indisponibles")
        with pytest.raises(OutputBusyError):
            OutputReservation.acquire(link / "film.mkv", lock_dir=_lock_dir(tmp_path))


def test_hardlink_alias_of_existing_destination_is_busy(tmp_path: Path) -> None:
    output = tmp_path / "film.mkv"
    output.write_bytes(b"OLD")
    alias = tmp_path / "alias.mkv"
    try:
        os.link(output, alias)
    except OSError:
        pytest.skip("liens physiques indisponibles")
    with OutputReservation.acquire(output, lock_dir=_lock_dir(tmp_path)):
        with pytest.raises(OutputBusyError):
            OutputReservation.acquire(alias, lock_dir=_lock_dir(tmp_path))


def test_reservation_held_by_another_process_until_it_dies(tmp_path: Path) -> None:
    output = tmp_path / "film.mkv"
    script = textwrap.dedent(f"""
        import sys, time
        from pathlib import Path
        from core.output_commit import OutputReservation
        OutputReservation.acquire(Path({str(output)!r}), lock_dir=Path({str(_lock_dir(tmp_path))!r}))
        print("ready", flush=True)
        sys.stdin.readline()
    """)
    child = subprocess.Popen(
        [sys.executable, "-c", script], cwd=REPO_ROOT,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "ready"
        with pytest.raises(OutputBusyError):
            OutputReservation.acquire(output, lock_dir=_lock_dir(tmp_path))
    finally:
        child.kill()
        child.wait(timeout=10)
    # Processus mort : le système a libéré le verrou.
    OutputReservation.acquire(output, lock_dir=_lock_dir(tmp_path)).release()


# ---------------------------------------------------------------------------
# Publication
# ---------------------------------------------------------------------------

def test_publish_new_destination(tmp_path: Path) -> None:
    output = tmp_path / "film.mkv"
    with OutputReservation.acquire(output, lock_dir=_lock_dir(tmp_path)):
        candidate = reserve_candidate(output)
        candidate.write_bytes(b"NEW")
        publish_candidate(candidate, output)
    assert output.read_bytes() == b"NEW"
    assert not candidate.exists()


def test_destination_appearing_during_job_is_kept(tmp_path: Path) -> None:
    output = tmp_path / "film.mkv"
    with OutputReservation.acquire(output, lock_dir=_lock_dir(tmp_path)):
        candidate = reserve_candidate(output)
        candidate.write_bytes(b"NEW")
        output.write_bytes(b"FOREIGN")
        with pytest.raises(OutputChangedError) as excinfo:
            publish_candidate(candidate, output)
    assert output.read_bytes() == b"FOREIGN"
    assert candidate.read_bytes() == b"NEW"
    assert str(candidate) in str(excinfo.value)


def test_accepted_overwrite_replaces_unchanged_destination(tmp_path: Path) -> None:
    output = tmp_path / "film.mkv"
    output.write_bytes(b"OLD")
    with OutputReservation.acquire(output, lock_dir=_lock_dir(tmp_path)):
        candidate = reserve_candidate(output)
        candidate.write_bytes(b"NEW")
        publish_candidate(candidate, output)
    assert output.read_bytes() == b"NEW"


def test_destination_modified_during_job_is_kept(tmp_path: Path) -> None:
    output = tmp_path / "film.mkv"
    output.write_bytes(b"OLD")
    with OutputReservation.acquire(output, lock_dir=_lock_dir(tmp_path)):
        candidate = reserve_candidate(output)
        candidate.write_bytes(b"NEW")
        time.sleep(0.01)
        output.write_bytes(b"CHANGED BY SOMEONE")
        with pytest.raises(OutputChangedError):
            publish_candidate(candidate, output)
    assert output.read_bytes() == b"CHANGED BY SOMEONE"
    assert candidate.read_bytes() == b"NEW"


def test_publish_without_reservation_replaces(tmp_path: Path) -> None:
    """Intermédiaires d'un workspace : remplacement atomique historique."""
    output = tmp_path / "intermediate.mkv"
    output.write_bytes(b"OLD")
    candidate = reserve_candidate(output)
    candidate.write_bytes(b"NEW")
    publish_candidate(candidate, output)
    assert output.read_bytes() == b"NEW"


@pytest.mark.skipif(sys.platform == "win32", reason="publication par lien physique (POSIX)")
def test_filesystem_without_hardlinks_still_refuses_to_clobber(tmp_path: Path, monkeypatch) -> None:
    output = tmp_path / "film.mkv"

    def no_link(*_args, **_kwargs):
        raise OSError(errno.EPERM, "liens physiques non pris en charge")

    monkeypatch.setattr(output_commit.os, "link", no_link)
    with OutputReservation.acquire(output, lock_dir=_lock_dir(tmp_path)):
        candidate = reserve_candidate(output)
        candidate.write_bytes(b"NEW")
        publish_candidate(candidate, output)
    assert output.read_bytes() == b"NEW"

    other = tmp_path / "other.mkv"
    with OutputReservation.acquire(other, lock_dir=_lock_dir(tmp_path)):
        candidate = reserve_candidate(other)
        other.write_bytes(b"FOREIGN")
        with pytest.raises(OutputChangedError):
            publish_candidate(candidate, other)
    assert other.read_bytes() == b"FOREIGN"


# ---------------------------------------------------------------------------
# Producteurs : transaction FFmpeg et writer natif
# ---------------------------------------------------------------------------

def test_transaction_cancelled_before_start_touches_nothing(tmp_path: Path) -> None:
    """Reproduction de l'audit : `.partial` préexistant + annulation initiale."""
    from core.matroska.contract import MatroskaOutputContract
    from core.workflows.common.matroska_finalize import MatroskaOutputTransaction

    output = tmp_path / "out.mkv"
    foreign = tmp_path / "out.mkv.partial"
    foreign.write_bytes(b"autre job")
    calls: list[str] = []
    transaction = MatroskaOutputTransaction(
        output, MatroskaOutputContract(track_types=("video",)), "ffprobe",
        lambda *args: calls.append(args[2]) or "",
    )
    signals = TaskSignals()
    signals._cancel_event.set()
    with pytest.raises(TaskCancelledError):
        transaction.execute(["ffmpeg", "-y", str(output)], cwd=tmp_path, label="final", signals=signals)
    assert calls == []
    assert foreign.read_bytes() == b"autre job"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out.mkv.partial"]


def test_transaction_failure_keeps_foreign_partial(tmp_path: Path) -> None:
    from core.matroska.contract import MatroskaOutputContract
    from core.workflows.common.matroska_finalize import MatroskaOutputTransaction

    output = tmp_path / "out.mkv"
    foreign = tmp_path / "out.mkv.partial"
    foreign.write_bytes(b"autre job")
    written: list[Path] = []

    def run(command, _cwd, _label, _progress, _signals):
        written.append(Path(command[-1]))
        raise RuntimeError("ffmpeg KO")

    transaction = MatroskaOutputTransaction(
        output, MatroskaOutputContract(track_types=("video",)), "ffprobe", run,
    )
    with pytest.raises(RuntimeError):
        transaction.execute(["ffmpeg", "-y", str(output)], cwd=tmp_path, label="final", signals=TaskSignals())
    assert written and written[0] != foreign
    assert foreign.read_bytes() == b"autre job"
    assert not written[0].exists()


def test_native_writer_keeps_foreign_partial(tmp_path: Path) -> None:
    from core.matroska.writer import MatroskaWriter
    from tests.test_remux_hardening import _writer_plan

    foreign = tmp_path / "out.mkv.partial"
    foreign.write_bytes(b"autre job")
    plan = _writer_plan(tmp_path)
    MatroskaWriter().write(plan)
    assert (tmp_path / "out.mkv").stat().st_size > 0
    assert foreign.read_bytes() == b"autre job"
    assert sorted(p.name for p in tmp_path.glob("*.partial")) == ["out.mkv.partial"]
