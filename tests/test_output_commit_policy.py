"""Politique d'existence des sorties (refus, état confirmé) et publication de documents sous réservation."""

from __future__ import annotations

import multiprocessing
import os
import threading
from pathlib import Path

import pytest

from core import output_commit
from core.output_commit import (
    ExistingPolicy,
    OutputBusyError,
    OutputChangedError,
    OutputExistsError,
    OutputReservation,
    OutputStateChangedError,
    capture_state,
    publish_document,
    publish_unique,
    reserve_candidate,
)


def _partials(directory: Path) -> list[Path]:
    return sorted(directory.glob("*.partial"))


def test_refuse_existing_destination_without_candidate(tmp_path: Path) -> None:
    target = tmp_path / "out.mkv"
    target.write_bytes(b"premier")
    with pytest.raises(OutputExistsError):
        OutputReservation.acquire(target, existing=ExistingPolicy.refuse())
    assert target.read_bytes() == b"premier"
    assert _partials(tmp_path) == []
    # Verrous libérés : une nouvelle réservation autorisée réussit.
    OutputReservation.acquire(target, existing=ExistingPolicy.allow()).release()


def test_expect_absent_refuses_file_created_after_confirmation(tmp_path: Path) -> None:
    target = tmp_path / "out.m4a"
    policy = ExistingPolicy.expect(capture_state(target))
    target.write_bytes(b"tiers")
    with pytest.raises(OutputStateChangedError):
        OutputReservation.acquire(target, existing=policy)
    assert target.read_bytes() == b"tiers"


def test_expect_existing_refuses_modified_file(tmp_path: Path) -> None:
    target = tmp_path / "out.m4a"
    target.write_bytes(b"confirme")
    policy = ExistingPolicy.expect(capture_state(target))
    target.write_bytes(b"remplace pendant la question")
    with pytest.raises(OutputStateChangedError):
        OutputReservation.acquire(target, existing=policy)
    assert target.read_bytes() == b"remplace pendant la question"


def test_expect_unchanged_file_is_replaced(tmp_path: Path) -> None:
    target = tmp_path / "out.m4a"
    target.write_bytes(b"ancien")
    policy = ExistingPolicy.expect(capture_state(target))
    with OutputReservation.acquire(target, existing=policy) as reservation:
        candidate = reserve_candidate(target)
        candidate.write_bytes(b"nouveau")
        reservation.publish(candidate)
    assert target.read_bytes() == b"nouveau"


def test_expect_symlink_unchanged_then_retargeted(tmp_path: Path) -> None:
    first, second = tmp_path / "a.mkv", tmp_path / "b.mkv"
    first.write_bytes(b"A")
    second.write_bytes(b"B")
    link = tmp_path / "sortie.mkv"
    link.symlink_to(first)
    policy = ExistingPolicy.expect(capture_state(link))
    OutputReservation.acquire(first, existing=policy).release()  # sortie résolue par le workflow : acceptée
    link.unlink()
    link.symlink_to(second)
    with pytest.raises(OutputStateChangedError):
        OutputReservation.acquire(second, existing=policy)
    assert first.read_bytes() == b"A" and second.read_bytes() == b"B"


def test_expect_symlink_retargeted_during_processing_keeps_candidate(tmp_path: Path) -> None:
    first, second = tmp_path / "a.mkv", tmp_path / "b.mkv"
    first.write_bytes(b"A")
    second.write_bytes(b"B")
    link = tmp_path / "sortie.mkv"
    link.symlink_to(first)
    policy = ExistingPolicy.expect(capture_state(link))
    with OutputReservation.acquire(first, existing=policy) as reservation:
        candidate = reserve_candidate(first)
        candidate.write_bytes(b"resultat")
        link.unlink()
        link.symlink_to(second)
        with pytest.raises(OutputChangedError):
            reservation.publish(candidate)
    assert candidate.read_bytes() == b"resultat"
    assert first.read_bytes() == b"A" and second.read_bytes() == b"B"


def test_expect_link_round_trip_resolved_elsewhere_is_refused(tmp_path: Path) -> None:
    first, second = tmp_path / "a.mkv", tmp_path / "b.mkv"
    first.write_bytes(b"A")
    second.write_bytes(b"B")
    link = tmp_path / "sortie.mkv"
    link.symlink_to(first)
    policy = ExistingPolicy.expect(capture_state(link))
    # Le workflow a résolu la sortie pendant que le lien visait B ; le lien est revenu sur A depuis.
    with pytest.raises(OutputStateChangedError):
        OutputReservation.acquire(second, existing=policy)
    assert second.read_bytes() == b"B"


def test_expect_directory_link_round_trip_with_absent_file(tmp_path: Path) -> None:
    dir_a, dir_b = tmp_path / "A", tmp_path / "B"
    dir_a.mkdir()
    dir_b.mkdir()
    (dir_b / "sortie.mkv").write_bytes(b"B")
    link = tmp_path / "dossier"
    link.symlink_to(dir_a)
    policy = ExistingPolicy.expect(capture_state(link / "sortie.mkv"))  # absent dans A
    with pytest.raises(OutputStateChangedError):
        OutputReservation.acquire(dir_b / "sortie.mkv", existing=policy)
    assert (dir_b / "sortie.mkv").read_bytes() == b"B"


def test_publish_document_refuses_existing_without_force(tmp_path: Path) -> None:
    target = tmp_path / "doc.json"
    target.write_text("premier")
    with pytest.raises(OutputExistsError):
        publish_document(target, b"second", force=False)
    assert target.read_text() == "premier"
    publish_document(target, b"second", force=True)
    assert target.read_bytes() == b"second"
    assert _partials(tmp_path) == []


def test_publish_document_concurrent_threads_keep_first(tmp_path: Path) -> None:
    target = tmp_path / "doc.json"
    barrier = threading.Barrier(2, timeout=10)  # les deux écrivains ont passé leurs pré-contrôles
    results: list[object] = []

    def write(content: bytes) -> None:
        barrier.wait()
        try:
            results.append(publish_document(target, content, force=False))
        except (OutputBusyError, OutputExistsError) as exc:
            results.append(exc)

    threads = [threading.Thread(target=write, args=(content,)) for content in (b"un", b"deux")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(15)
    assert len(results) == 2
    assert len([item for item in results if isinstance(item, Path)]) == 1
    assert target.read_bytes() in (b"un", b"deux")
    assert _partials(tmp_path) == []


def _child_write(target: str, lock_dir: str, content: bytes, start: object, done: object) -> None:
    output_commit.default_lock_dir = lambda: Path(lock_dir)  # type: ignore[assignment]
    start.wait(10)  # type: ignore[attr-defined]
    try:
        publish_document(Path(target), content, force=False)
        done.put("ok")  # type: ignore[attr-defined]
    except (OutputBusyError, OutputExistsError):
        done.put("refused")  # type: ignore[attr-defined]


def test_publish_document_concurrent_processes_keep_first(tmp_path: Path) -> None:
    target = tmp_path / "doc.json"
    context = multiprocessing.get_context("spawn")
    start, done = context.Event(), context.Queue()
    lock_dir = output_commit.default_lock_dir()
    processes = [context.Process(target=_child_write, args=(str(target), str(lock_dir), content, start, done))
                 for content in (b"un", b"deux")]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(60)
    outcomes = sorted(done.get(timeout=5) for _ in processes)
    assert outcomes == ["ok", "refused"]
    assert target.read_bytes() in (b"un", b"deux")


def test_publish_unique_concurrent_deliveries_get_distinct_names(tmp_path: Path, monkeypatch) -> None:
    sources = []
    for index in range(2):
        source = tmp_path / f"capture{index}.png"
        source.write_bytes(f"image {index}".encode())
        sources.append(source)
    out = tmp_path / "livraison"
    out.mkdir()
    barrier = threading.Barrier(2, timeout=10)
    original = output_commit.OutputReservation.acquire.__func__  # type: ignore[attr-defined]
    calls = {"n": 0}
    lock = threading.Lock()

    def synchronised(cls, output, **kwargs):  # les deux livraisons visent le même premier nom
        with lock:
            calls["n"] += 1
            first = calls["n"] <= 2
        if first:
            barrier.wait()
        return original(cls, output, **kwargs)

    monkeypatch.setattr(output_commit.OutputReservation, "acquire", classmethod(synchronised))
    results: list[Path] = []
    threads = [threading.Thread(target=lambda s=s: results.append(publish_unique(s, out, "film.apercu.png")))
               for s in sources]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(15)
    assert len(results) == 2 and len(set(results)) == 2
    assert sorted(path.read_bytes() for path in results) == [b"image 0", b"image 1"]
    assert all(source.exists() for source in sources)  # la source reste à l'appelant


def test_publish_document_default_mode_is_private(tmp_path: Path) -> None:
    target = tmp_path / "prive.json"
    publish_document(target, b"{}", force=False)
    assert (os.stat(target).st_mode & 0o777) == 0o600
    shared = tmp_path / "partage.json"
    publish_document(shared, b"{}", force=False, mode=0o644)
    assert (os.stat(shared).st_mode & 0o777) == 0o644
