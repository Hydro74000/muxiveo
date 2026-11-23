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


# --- Relecture du lot 8 -------------------------------------------------------

def test_fingerprint_covers_the_lacing_mode(tmp_path: Path, monkeypatch) -> None:
    from core.matroska import copy_check

    path = _badly_interleaved(tmp_path / "a.mkv", delay_ms=0)

    def laced(mode: int):
        def blocks(_self, **_kwargs):
            yield MatroskaBlock(1, 0, 0x80 | (mode << 1), b"x", lace_count=2, lacing_mode=mode,
                                encoded_frames_payload=b"\x01\x02xx", timestamp_ns=0)
        return blocks

    monkeypatch.setattr(MatroskaReader, "blocks", laced(1))
    xiph = copy_check.block_fingerprints(path)
    monkeypatch.setattr(MatroskaReader, "blocks", laced(2))
    assert copy_check.block_fingerprints(path) != xiph


def _with_opaque(path: Path, opaque: bytes, *, delay_ms: int = 60_000) -> Path:
    tracks = (MatroskaMuxTrack(Path("a.mkv"), _track(1, 11), 1, 101),
              MatroskaMuxTrack(Path("b.mkv"), _track(2, 22), 2, 202))
    first, second = _packets(1, 0, 70_000, 1), _packets(2, 0, 70_000, 2)
    lag = delay_ms // 32
    order: list[MatroskaMuxPacket] = []
    for index, packet in enumerate(first):
        order.append(packet)
        if index >= lag:
            order.append(second[index - lag])
    order.extend(second[len(first) - lag:])
    MatroskaWriter().write(MatroskaMuxPlan(path, tracks, iter(order), duration_ms=70_000, opaque_top_level=(opaque,)))
    return path


def _tag(language: str) -> bytes:
    from core.matroska.ebml import string_element
    from core.matroska.ids import SIMPLE_TAG_ID, TAG_ID, TAG_NAME_ID, TAG_STRING_ID, TAG_TRACK_UID_ID, TAGS_ID, TARGETS_ID

    return element(TAGS_ID, element(TAG_ID, element(TARGETS_ID, uint_element(TAG_TRACK_UID_ID, 202)) + element(
        SIMPLE_TAG_ID, string_element(TAG_NAME_ID, "COMMENT") + string_element(TAG_STRING_ID, "x")
        + string_element(bytes.fromhex("447a"), language),
    )))


def test_tag_language_change_is_detected(tmp_path: Path) -> None:
    french = _with_opaque(tmp_path / "fr.mkv", _tag("fre"), delay_ms=0)
    english = _with_opaque(tmp_path / "en.mkv", _tag("eng"), delay_ms=0)
    assert any("Tags" in difference for difference in compare_matroska_copy(french, english))
    assert compare_matroska_copy(french, french) == []


def test_sub_millisecond_original_is_compared_like_the_assembly(tmp_path: Path) -> None:
    """Candidat à 10 µs (FFmpeg master) : copie quantifiée à 1 ms acceptée."""
    path = tmp_path / "fine.mkv.partial"
    tracks = (MatroskaMuxTrack(Path("a.mkv"), _track(1, 11), 1, 101),
              MatroskaMuxTrack(Path("b.mkv"), _track(2, 22), 2, 202))
    first = [MatroskaMuxPacket(1, MatroskaBlock(1, ms, 0x80, bytes([1, ms % 251]) * 50,
                                                timestamp_ns=ms * _MS + 500_000), ms) for ms in range(0, 90_000, 32)]
    second = [MatroskaMuxPacket(2, MatroskaBlock(2, ms, 0x80, bytes([2, ms % 251]) * 50,
                                                 timestamp_ns=ms * _MS + 500_000), ms) for ms in range(0, 90_000, 32)]
    lag = 60_000 // 32
    order = []
    for index, packet in enumerate(first):
        order.append(packet)
        if index >= lag:
            order.append(second[index - lag])
    order.extend(second[len(first) - lag:])
    MatroskaWriter().write(MatroskaMuxPlan(path, tracks, iter(order), timestamp_scale_ns=10_000, duration_ms=90_000))
    repaired, logs = _repair(path)
    assert repaired, logs


def test_long_candidate_name_uses_a_bounded_temporary(tmp_path: Path) -> None:
    candidate = _badly_interleaved(tmp_path / ("n" * 230 + ".mkv.partial"))
    repaired, logs = _repair(candidate)
    assert repaired, logs
    assert not list(tmp_path.glob(".muxiveo-repair-*"))


def test_cleanup_failure_never_masks_the_cancellation(tmp_path: Path, monkeypatch) -> None:
    candidate = _badly_interleaved(tmp_path / "out.mkv.partial")
    real_unlink = Path.unlink

    def failing_unlink(self, *args, **kwargs):
        if self.name.startswith(".muxiveo-repair-") and not self.name.endswith(".partial"):
            raise PermissionError(13, "verrouillé")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failing_unlink)
    with pytest.raises(TaskCancelledError):
        _repair(candidate, cancelled=lambda: True)


@pytest.mark.parametrize("chapters", ["ordered", "segment_uid"])
def test_non_copyable_chapters_are_refused(tmp_path: Path, chapters: str) -> None:
    from core.matroska.ids import CHAPTER_ATOM_ID, CHAPTER_TIME_START_ID, CHAPTER_UID_ID, CHAPTERS_ID, EDITION_ENTRY_ID
    from core.matroska.ebml import binary_element

    atom = uint_element(CHAPTER_UID_ID, 7) + uint_element(CHAPTER_TIME_START_ID, 0)
    if chapters == "segment_uid":
        atom += binary_element(bytes.fromhex("6e67"), b"\x02" * 16)
    edition = element(CHAPTER_ATOM_ID, atom)
    if chapters == "ordered":
        edition = uint_element(bytes.fromhex("45dd"), 1) + edition
    candidate = _with_opaque(tmp_path / "out.mkv.partial", element(CHAPTERS_ID, element(EDITION_ENTRY_ID, edition)))
    before = candidate.read_bytes()
    repaired, logs = _repair(candidate)
    assert repaired is False and any("chapitres" in message for _level, message in logs)
    assert candidate.read_bytes() == before


def test_missing_measurement_of_the_reassembly_refuses_the_substitution(tmp_path: Path, monkeypatch) -> None:
    candidate = _badly_interleaved(tmp_path / "out.mkv.partial")
    before = candidate.read_bytes()
    report = scan_interleave(candidate)
    monkeypatch.setattr(finalize, "scan_interleave", lambda *_a, **_k: None)
    logs: list[tuple[str, str]] = []
    assert repair_ffmpeg_interleave(candidate, None, report, log=lambda *item: logs.append(item)) is False
    assert any("mesure" in message for _level, message in logs)
    assert candidate.read_bytes() == before


def test_enospc_after_the_partial_exists_keeps_the_candidate(tmp_path: Path, monkeypatch) -> None:
    import core.matroska.writer as writer_module

    candidate = _badly_interleaved(tmp_path / "out.mkv.partial")
    before = candidate.read_bytes()
    real_write = writer_module._BackgroundWriter.write
    calls = {"count": 0}

    def full_disk(self, data, size=None):
        calls["count"] += 1
        if calls["count"] > 5:
            raise OSError(28, "No space left on device")
        return real_write(self, data, size)

    monkeypatch.setattr(writer_module._BackgroundWriter, "write", full_disk)
    repaired, logs = _repair(candidate)
    assert repaired is False and any("non corrigé" in message for _level, message in logs)
    assert candidate.read_bytes() == before
    assert sorted(path.name for path in tmp_path.iterdir()) == ["out.mkv.partial"]


def test_cancellation_after_the_partial_exists_cleans_everything(tmp_path: Path) -> None:
    candidate = _badly_interleaved(tmp_path / "out.mkv.partial")
    calls = {"count": 0}

    def later() -> bool:
        calls["count"] += 1
        return calls["count"] > 2

    with pytest.raises(TaskCancelledError):
        _repair(candidate, cancelled=later)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["out.mkv.partial"]


@pytest.mark.parametrize("backend,expected", [("auto", True), ("ffmpeg", False)])
def test_encode_final_mux_repairs_only_an_auto_fallback(tmp_path: Path, monkeypatch, backend, expected) -> None:
    """Muxage final FFmpeg de l'Encode (chemin commun : direct, NVEncC, multi-vidéo, injection)."""
    from core.workflows.common.matroska_finalize import MatroskaOutputTransaction
    from core.workflows.encode.models import EncodeConfig, VideoEncodeSettings

    seen: list[tuple[bool, object]] = []
    monkeypatch.setattr(MatroskaOutputTransaction, "execute",
                        lambda self, *_a, **_k: seen.append((self.repair_interleave, self.contract)) or "")
    from core.workflows.encode.workflow import EncodeWorkflow

    source = tmp_path / "src.mkv"
    source.write_bytes(b"")
    config = EncodeConfig(source=source, output=tmp_path / "out.mkv", video=VideoEncodeSettings(codec="copy"),
                          audio_tracks=[], copy_subtitles=False, mux_backend=backend)
    workflow = EncodeWorkflow(ffmpeg_bin="ffmpeg")
    monkeypatch.setattr(workflow, "_attachment_stream_expectations", lambda _config: ())
    monkeypatch.setattr(workflow, "_source_has_chapters_hint", lambda _config: False)
    monkeypatch.setattr("core.workflows.encode.workflow.build_encode_output_contract",
                        lambda *_a, **_k: "contrat réel")
    workflow._finalize_ffmpeg_output(config, ["ffmpeg", str(config.output)], None, "final", None,  # type: ignore[arg-type]
                                     plan=workflow._build_encode_plan(config))
    assert seen == [(expected, "contrat réel")]


def test_destination_changed_during_the_repair_is_never_overwritten(tmp_path: Path, monkeypatch) -> None:
    from core.output_commit import OutputCommitError, OutputReservation
    from core.runner import TaskSignals
    from core.workflows.common.matroska_finalize import MatroskaOutputTransaction

    output = tmp_path / "out.mkv"
    reservation = OutputReservation.acquire(output)
    real_repair = finalize.repair_ffmpeg_interleave

    def repair_then_intruder(*args, **kwargs):
        result = real_repair(*args, **kwargs)
        output.write_bytes(b"fichier apparu pendant le job")
        return result

    monkeypatch.setattr(finalize, "repair_ffmpeg_interleave", repair_then_intruder)

    def run_command(command, _cwd, _label, _progress, _signals):
        if command[0] == "ffmpeg":
            _badly_interleaved(Path(command[-1]))
        return ""

    transaction = MatroskaOutputTransaction(output=output, contract=None, ffprobe_bin="ffprobe",  # type: ignore[arg-type]
                                            run_command=run_command, repair_interleave=True)
    try:
        with pytest.raises(OutputCommitError):
            transaction.execute(["ffmpeg", str(output)], cwd=None, label="final", signals=TaskSignals())
    finally:
        reservation.release()
    assert output.read_bytes() == b"fichier apparu pendant le job"


# --- Seconde relecture du lot 8 ----------------------------------------------

def test_raw_uid_zero_is_not_confused_with_rank_zero(tmp_path: Path) -> None:
    from core.matroska.ebml import string_element
    from core.matroska.ids import SIMPLE_TAG_ID, TAG_ID, TAG_NAME_ID, TAG_STRING_ID, TAG_TRACK_UID_ID, TAGS_ID, TARGETS_ID

    def tag(uid: int) -> bytes:
        return element(TAGS_ID, element(TAG_ID, element(TARGETS_ID, uint_element(TAG_TRACK_UID_ID, uid)) + element(
            SIMPLE_TAG_ID, string_element(TAG_NAME_ID, "COMMENT") + string_element(TAG_STRING_ID, "x"),
        )))

    ranked = _with_opaque(tmp_path / "ranked.mkv", tag(101), delay_ms=0)  # piste de rang 0
    raw_zero = _with_opaque(tmp_path / "raw.mkv", tag(0), delay_ms=0)     # UID brut 0
    assert any("Tags" in difference for difference in compare_matroska_copy(ranked, raw_zero))


def test_preparation_error_releases_the_reserved_temporary(tmp_path: Path, monkeypatch) -> None:
    import core.matroska.mux_plan as mux_plan

    candidate = _badly_interleaved(tmp_path / "out.mkv.partial")

    def unreadable(_path):
        raise OSError(5, "Erreur d'entrée/sortie")

    monkeypatch.setattr(mux_plan, "deterministic_source_identity", unreadable)
    repaired, logs = _repair(candidate)
    assert repaired is False and any("non corrigé" in message for _level, message in logs)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["out.mkv.partial"]


def test_writer_cleanup_failure_never_masks_its_cancellation(tmp_path: Path, monkeypatch) -> None:
    from core.matroska.writer import MatroskaWriteCancelled

    real_unlink = Path.unlink

    def locked_partial(self, *args, **kwargs):
        if self.name.endswith(".partial"):
            raise PermissionError(13, "verrouillé")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", locked_partial)
    tracks = (MatroskaMuxTrack(Path("a.mkv"), _track(1, 11), 1, 101),)
    with pytest.raises(MatroskaWriteCancelled):
        MatroskaWriter().write(
            MatroskaMuxPlan(tmp_path / "out.mkv", tracks, iter(_packets(1, 0, 30_000, 1))),
            cancel_cb=lambda: True,
        )


def test_every_encode_pipeline_finalizes_through_the_repairing_transaction() -> None:
    """Direct, NVEncC, multi-vidéo, injection : un seul muxage final FFmpeg (décision du filet incluse)."""
    import ast

    tree = ast.parse(Path("core/workflows/encode/workflow.py").read_text(encoding="utf-8"))
    finalizers = [
        ast.unparse(keyword.value)
        for node in ast.walk(tree) if isinstance(node, ast.Call)
        for keyword in node.keywords if keyword.arg == "finalize_ffmpeg"
    ]
    assert len(finalizers) >= 5
    assert set(finalizers) == {"self._finalize_ffmpeg_output"}


@pytest.mark.usefixtures("qt_app")
def test_encode_repairs_with_the_real_contract(tmp_path: Path, monkeypatch) -> None:
    """Encode Copy en Auto (repli) : contrat réel du job, post-actions réelles, candidat réassemblé."""
    import subprocess

    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg/ffprobe requis")
    from core.matroska.mux_plan import MatroskaMuxTrack as MuxTrack
    from core.workflows.encode.models import AudioTrackSettings, EncodeConfig, VideoEncodeSettings
    from core.workflows.encode.workflow import EncodeWorkflow

    source = tmp_path / "src.mkv"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "color=s=64x64:r=25:d=10",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=10",
                    "-c:v", "ffv1", "-c:a", "ac3", "-metadata:s:a:0", "language=fre", str(source)], check=True)
    config = EncodeConfig(source=source, output=tmp_path / "out.mkv", video=VideoEncodeSettings(codec="copy"),
                          audio_tracks=[AudioTrackSettings(stream_index=1)], copy_subtitles=False,
                          mux_backend="auto", write_nfo=False)
    workflow = EncodeWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
    logs: list[str] = []
    workflow.log_message.connect(lambda level, message: logs.append(f"{level} {message}"))

    def run_command(command, _cwd, _label, _progress, _signals):
        if Path(command[0]).name.startswith("ffprobe"):
            return subprocess.run(command, capture_output=True, text=True, check=True).stdout
        # « Muxage final FFmpeg » simulé : toute la vidéo, puis tout l'audio (10 s de retard).
        reader = MatroskaReader(source)
        tracks = reader.tracks()
        mux_tracks = tuple(
            MuxTrack(source, track, index, 1000 + index, patch_language=False, patch_name=False, patch_flags=False)
            for index, track in enumerate(tracks, 1)
        )
        packets = []
        for index, track in enumerate(tracks, 1):
            packets.extend(MatroskaMuxPacket(index, block, rank) for rank, block in enumerate(
                reader.blocks(track_numbers={track.number}, first_lace_only=True)))
        MatroskaWriter().write(MatroskaMuxPlan(Path(command[-1]), mux_tracks, iter(packets)))
        return ""

    monkeypatch.setattr(workflow, "_run_transaction_command", run_command)
    from core.runner import TaskSignals

    workflow._finalize_ffmpeg_output(config, ["ffmpeg", str(config.output)], None, "final", TaskSignals(),
                                     plan=workflow._build_encode_plan(config))
    assert interleave_warnings(scan_interleave(config.output)) == []
    assert any("corrigé par l'assembleur natif" in line for line in logs), logs
    output_tracks = MatroskaReader(config.output).tracks()
    assert [track.codec_id for track in output_tracks] == ["V_FFV1", "A_AC3"]
    assert output_tracks[1].language == "fre"
