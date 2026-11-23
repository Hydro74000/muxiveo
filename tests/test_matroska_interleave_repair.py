"""Filet de sécurité : réassemblage natif d'une sortie FFmpeg mal entrelacée (lot 8)."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from core.matroska.copy_check import compare_matroska_copy
from core.matroska.ebml import ascii_element, element, uint_element
from core.matroska.ids import CODEC_ID_ID, INFO_ID, TRACK_NUMBER_ID, TRACK_TYPE_ID, TRACK_UID_ID
from core.matroska.mux_plan import MatroskaMuxPacket, MatroskaMuxPlan, MatroskaMuxTrack
from core.matroska.reader import MatroskaBlock, MatroskaReader, MatroskaTrack
from core.matroska.validation import interleave_warnings, scan_interleave
from core.matroska.writer import MatroskaWriter
from core.runner import TaskCancelledError
from core.workflows.common import matroska_finalize as finalize
from core.workflows.common.matroska_finalize import repair_ffmpeg_interleave

_MS = 1_000_000


def _track(number: int, uid: int) -> MatroskaTrack:
    raw = b"".join((
        uint_element(TRACK_NUMBER_ID, number), uint_element(TRACK_UID_ID, uid),
        uint_element(TRACK_TYPE_ID, 2), ascii_element(CODEC_ID_ID, "A_AC3"),
    ))
    return MatroskaTrack(number, uid, 2, "A_AC3", b"", "", "und", "", raw)


def _packets(number: int, start_ms: int, end_ms: int, marker: int) -> list[MatroskaMuxPacket]:
    return [
        MatroskaMuxPacket(number, MatroskaBlock(number, ms, 0x80, bytes([marker, ms % 251]) * 200,
                                                timestamp_ns=ms * _MS), ms)
        for ms in range(start_ms, end_ms, 32)
    ]


def _badly_interleaved(path: Path, *, delay_ms: int = 60_000) -> Path:
    """Piste 2 écrite ``delay_ms`` après la piste 1 (même instant du film) : sortie type MVO."""
    tracks = (MatroskaMuxTrack(Path("a.mkv"), _track(1, 11), 1, 101),
              MatroskaMuxTrack(Path("b.mkv"), _track(2, 22), 2, 202))
    first, second = _packets(1, 0, 120_000, 1), _packets(2, 0, 120_000, 2)
    order: list[MatroskaMuxPacket] = []
    lag = delay_ms // 32
    for index, packet in enumerate(first):
        order.append(packet)
        if index >= lag:
            order.append(second[index - lag])
    order.extend(second[len(first) - lag:])
    MatroskaWriter().write(MatroskaMuxPlan(path, tracks, iter(order), duration_ms=120_000))
    return path


def _repair(candidate: Path, **kwargs) -> tuple[bool, list[tuple[str, str]]]:
    logs: list[tuple[str, str]] = []
    report = scan_interleave(candidate)
    return repair_ffmpeg_interleave(candidate, None, report, log=lambda *item: logs.append(item), **kwargs), logs


def test_badly_interleaved_candidate_is_reassembled_faithfully(tmp_path: Path) -> None:
    candidate = _badly_interleaved(tmp_path / "out.mkv.partial")
    pristine = tmp_path / "pristine.mkv"
    shutil.copyfile(candidate, pristine)
    assert interleave_warnings(scan_interleave(candidate))
    repaired, logs = _repair(candidate)
    assert repaired, logs
    assert interleave_warnings(scan_interleave(candidate)) == []
    assert compare_matroska_copy(pristine, candidate) == []
    assert any("corrigé par l'assembleur natif" in message for _level, message in logs)
    assert not list(tmp_path.glob("*.repair.mkv*"))


def test_clean_candidate_is_left_alone(tmp_path: Path) -> None:
    candidate = _badly_interleaved(tmp_path / "out.mkv.partial", delay_ms=0)
    before = candidate.read_bytes()
    assert _repair(candidate)[0] is False
    assert candidate.read_bytes() == before


def test_altered_copy_is_detected_even_with_identical_counts(tmp_path: Path) -> None:
    original = _badly_interleaved(tmp_path / "a.mkv", delay_ms=0)
    altered = tmp_path / "b.mkv"
    data = bytearray(original.read_bytes())
    position = data.rfind(bytes([2, 100]) * 3)  # un payload de la piste 2
    data[position] ^= 0xFF
    altered.write_bytes(bytes(data))
    differences = compare_matroska_copy(original, altered)
    assert differences and "piste 2" in differences[0]


def test_insufficient_space_keeps_the_candidate(tmp_path: Path, monkeypatch) -> None:
    candidate = _badly_interleaved(tmp_path / "out.mkv.partial")
    before = candidate.read_bytes()
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: shutil._ntuple_diskusage(10, 10, 0))  # type: ignore[attr-defined]
    repaired, logs = _repair(candidate)
    assert repaired is False and any("espace disque" in message for _level, message in logs)
    assert candidate.read_bytes() == before


def test_writer_failure_keeps_the_candidate_and_cleans_up(tmp_path: Path, monkeypatch) -> None:
    candidate = _badly_interleaved(tmp_path / "out.mkv.partial")
    before = candidate.read_bytes()

    def no_space(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(MatroskaWriter, "write", no_space)
    repaired, logs = _repair(candidate)
    assert repaired is False and any("écriture du réassemblage" in message for _level, message in logs)
    assert candidate.read_bytes() == before
    assert not list(tmp_path.glob("*.repair.mkv*"))


def test_refused_validation_keeps_the_candidate(tmp_path: Path, monkeypatch) -> None:
    candidate = _badly_interleaved(tmp_path / "out.mkv.partial")
    before = candidate.read_bytes()
    monkeypatch.setattr(finalize, "validate_matroska_output", lambda *_args, **_kwargs: ["piste manquante"])
    repaired, logs = _repair(candidate)
    assert repaired is False and any("contrat" in message for _level, message in logs)
    assert candidate.read_bytes() == before


def test_cancellation_is_never_absorbed(tmp_path: Path) -> None:
    candidate = _badly_interleaved(tmp_path / "out.mkv.partial")
    with pytest.raises(TaskCancelledError):
        _repair(candidate, cancelled=lambda: True)
    assert not list(tmp_path.glob("*.repair.mkv*"))


def test_internal_disorder_of_a_track_keeps_the_warning(tmp_path: Path) -> None:
    """Horodatages d'une piste à rebours dans l'ordre de décodage : rien à corriger sans le changer."""
    path = tmp_path / "out.mkv.partial"
    track = MatroskaMuxTrack(Path("a.mkv"), _track(1, 11), 1, 101)
    packets = _packets(1, 10_000, 20_000, 1) + _packets(1, 0, 10_000, 1)
    MatroskaWriter().write(MatroskaMuxPlan(path, (track,), iter(packets), duration_ms=20_000))
    if not interleave_warnings(scan_interleave(path)):
        pytest.skip("Le writer refuse déjà ce désordre")
    before = path.read_bytes()
    repaired, logs = _repair(path)
    assert repaired is False
    assert path.read_bytes() == before
    assert any("non corrigé" in message for level, message in logs if level == "WARN")


def test_segment_links_are_not_copied_in_isolation(tmp_path: Path, monkeypatch) -> None:
    candidate = _badly_interleaved(tmp_path / "out.mkv.partial")
    before = candidate.read_bytes()
    original = MatroskaReader.raw_top_level

    def with_prev_uid(self, element_id):
        raw = original(self, element_id)
        if element_id == INFO_ID and raw:
            from core.matroska.ebml import binary_element

            return (element(INFO_ID, binary_element(bytes.fromhex("3cb923"), b"\x01" * 16)),)
        return raw

    monkeypatch.setattr(MatroskaReader, "raw_top_level", with_prev_uid)
    repaired, logs = _repair(candidate)
    assert repaired is False and any("liens entre segments" in message for _level, message in logs)
    monkeypatch.undo()
    assert candidate.read_bytes() == before


@pytest.mark.usefixtures("qt_app")
def test_remux_repairs_only_an_auto_fallback(tmp_path: Path, monkeypatch) -> None:
    """Remux FFmpeg : réassemblage tenté en repli Auto, jamais pour un FFmpeg choisi."""
    import core.workflows.remux_runtime as runtime

    calls: list[Path] = []
    monkeypatch.setattr(runtime, "repair_ffmpeg_interleave", lambda candidate, *_a, **_k: calls.append(candidate) or False)
    from tests.integration._synth import make_av_container, wait_task

    from core.inspector import FileInspector
    from core.workflows.remux import RemuxWorkflow
    from core.workflows.remux_models import RemuxConfig, SourceInput, tracks_from_file_info

    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg/ffprobe requis")
    source = tmp_path / "src.mkv"
    make_av_container(source, duration=0.5)
    tracks = tracks_from_file_info(FileInspector().inspect(source), file_id="s")
    for backend, expected in (("auto", 1), ("ffmpeg", 0)):
        calls.clear()
        if backend == "auto":
            # Blocage natif injecté : le repli FFmpeg est effectif.
            monkeypatch.setattr("core.workflows.remux_plan.native_capability_reasons", lambda _c, **_k: ("blocage de test",))
        config = RemuxConfig(sources=[SourceInput(source, 0, tracks)], output=tmp_path / f"{backend}.mkv",
                             track_order=[(0, t.mkv_tid, t.entry_id) for t in tracks],
                             mux_backend=backend, work_dir=tmp_path / "work")
        state = wait_task(RemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe").run(config), timeout=60.0)
        assert state["failed"] is None, state["failed"]
        assert len(calls) == expected, backend
        monkeypatch.setattr("core.workflows.remux_plan.native_capability_reasons",
                            runtime.__dict__.get("native_capability_reasons", lambda _c, **_k: ()))


def test_encode_transaction_repairs_when_asked(tmp_path: Path) -> None:
    from core.runner import TaskSignals
    from core.workflows.common.matroska_finalize import MatroskaOutputTransaction

    output = tmp_path / "out.mkv"
    logs: list[str] = []

    def run_command(command, _cwd, _label, _progress, _signals):
        if command[0] == "ffmpeg":  # la sonde FFprobe de validation ne réécrit rien
            _badly_interleaved(Path(command[-1]))
        return ""

    transaction = MatroskaOutputTransaction(
        output=output, contract=None, ffprobe_bin="ffprobe",  # type: ignore[arg-type]
        run_command=run_command, warn=logs.append, info=logs.append, repair_interleave=True,
        validation_override=None,
    )
    transaction.execute(["ffmpeg", str(output)], cwd=None, label="final", signals=TaskSignals())
    assert output.exists()
    assert interleave_warnings(scan_interleave(output)) == [], logs
    assert any("corrigé par l'assembleur natif" in message for message in logs)
