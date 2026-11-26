"""HDR10+ généré depuis le Dolby Vision : préparation (méthode, mesure, repli L1), routes d'exécution (injection,
Copy patché, multi-pistes, NVEncC), ratio de trames des métadonnées, aperçus. Outils externes simulés."""
from __future__ import annotations

import dataclasses
import json
import threading
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.fel.preparation import fallback_video
from core.i18n import translate_text
from core.hdr10plus_generation import METHOD_FEL_STATISTICS, METHOD_L1, METHOD_MEASURE
from core.runner import TaskCancelledError, TaskSignals
from core.workflows.encode import EncodeConfig, EncodeError, EncodeWorkflow, VideoEncodeSettings
from core.workflows.encode.models import FrameInterpolationSettings, VideoFilterSettings
from core.workflows.encode.planning.track_settings import is_pure_copy
from core.workflows.encode.runtime import metadata_inject, multi_video, nvencc_execution
from core.workflows.encode.runtime.nvencc import build_nvencc_command
from tests.test_encode_runtime_failures import (  # noqa: F401 — fixtures autouse importées
    _create_output,
    _ensure_qt_app,
    _Inject,
    _inject_config,
    _matroska_stubs,
    _video,
)
from tests.test_hdr10plus_generation import _Engine
from tests.test_hdr_edit import write_statistics
from tests.test_hdr_inspection import _rpu, _write_export


class _PermissiveGuard:
    """Audit des trames accepté (comptes cohérents) ; mémorise les métadonnées contrôlées."""

    seen: list[dict] = []

    def __init__(self, **_kwargs) -> None:
        pass

    def audit(self, **kwargs):
        _PermissiveGuard.seen.append(kwargs)
        return SimpleNamespace()

    def enforce(self, *_args, **_kwargs) -> None:
        return None


@pytest.fixture(autouse=True)
def _guard(monkeypatch):
    _PermissiveGuard.seen = []
    monkeypatch.setattr(metadata_inject, "FrameCountGuard", _PermissiveGuard)
    monkeypatch.setattr(multi_video, "FrameCountGuard", _PermissiveGuard)


def _assert_only_final_validation_failed(outcome) -> None:
    """Sortie finale simulée (fichier factice) : seule sa validation Matroska peut échouer."""
    assert all("Segment Matroska" in message or "EBML" in message for message, _exc in outcome.failed), outcome.failed


def _generated(tmp_path: Path, frames: int = 3) -> Path:
    path = tmp_path / "prepared" / "generated_hdr10plus_1.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"JSONInfo": {"HDR10plusProfile": "A", "Version": "1.0"},
                                "SceneInfo": [{"SceneFrameIndex": i} for i in range(frames)]}), encoding="utf-8")
    return path


# ── Ratio de trames des métadonnées ──────────────────────────────────────────
def _bob(deint: str = "all") -> VideoFilterSettings:
    return VideoFilterSettings(yadif_enabled=True, yadif_mode="send_field", yadif_deint=deint)


def test_metadata_ratio_counts_bob_deinterlacing_without_interpolation():
    assert _video(filters=_bob()).metadata_frame_ratio() == 2
    assert _video(filters=_bob("interlaced")).metadata_frame_ratio() == 1
    assert _video().metadata_frame_ratio() == 1
    assert _video(codec="copy", filters=_bob()).metadata_frame_ratio() == 1


def test_metadata_ratio_with_interpolation_follows_frame_ratio():
    factor = _video(filters=_bob(), interpolation=FrameInterpolationSettings(enabled=True, factor=2))
    assert factor.metadata_frame_ratio() == 4  # facteur × images par champ
    target = _video(filters=_bob(), interpolation=FrameInterpolationSettings(enabled=True, target_fps="60000/1001"))
    assert target.metadata_frame_ratio("24000/1001") == Fraction(5, 2)  # cadence cible : sans multiplicateur


def test_restoration_check_uses_the_engine_ratio_with_bob():
    from core.fel.hdr10plus import dynamic_metadata_adequacy
    from core.workflows.encode.models import RestoreSettings

    tmp = Path(__file__).parent  # chemins jamais lus : statistiques absentes
    video = _video(filters=_bob(), restoration=RestoreSettings(enabled=True), generate_hdr10plus=True,
                   restore_statistics=tmp / "absent.jsonl", restore_source_statistics=tmp / "absent2.jsonl")
    level, message = dynamic_metadata_adequacy(video, Fraction(2))
    assert level == "WARN" and "HDR10+" in message  # contrôlé (génération comptée), lecture impossible signalée


# ── Préparation ──────────────────────────────────────────────────────────────
def _workflow(tmp_path: Path, cache: bool = False) -> tuple[EncodeWorkflow, list[tuple[str, str]]]:
    workflow = EncodeWorkflow(ffmpeg_bin="ffmpeg", dovi_tool_bin="dovi_tool", hdr10plus_bin="hdr10plus_tool",
                              measurement_cache_dir=tmp_path / "cache" if cache else None)
    logs: list[tuple[str, str]] = []
    workflow.log_message.connect(lambda level, message: logs.append((level, message)))
    return workflow, logs


class _MeasureEngine(_Engine):
    capabilities = {"measure": ["base", "reconstructed"]}


def _prepare(workflow: EncodeWorkflow, tmp_path: Path, video: VideoEncodeSettings, *, engine=None,
             signals: TaskSignals | None = None, run=None):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"\x00" * 64)
    config = EncodeConfig(source=source, output=tmp_path / "out.mkv", video=video, audio_tracks=[],
                          copy_subtitles=False)
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)

    def loader():
        if engine is None:
            raise RuntimeError("extension absente")
        return engine

    with patch.object(workflow, "_frame_count_guard", return_value=SimpleNamespace(source_frame_count=lambda *a, **_k: 4)):
        return workflow.prepare_generated_hdr10plus(config, video, index=1, work_dir=work,
                                                    run=run or (lambda cmd: ""), signals=signals,
                                                    engine_loader=loader)


def test_preparation_measures_the_source_and_writes_the_json(tmp_path):
    workflow, logs = _workflow(tmp_path, cache=True)
    engine = _MeasureEngine(write_statistics(tmp_path / "s.jsonl", [100.0, 800.0, 1500.0, 200.0], scenes=[0, 2]))
    video = _video(generate_hdr10plus=True, dovi_source_profile="p8_1")
    prepared = _prepare(workflow, tmp_path, video, engine=engine)
    assert prepared.hdr10plus_generation_method == METHOD_MEASURE and engine.calls == [("base", "all")]
    document = json.loads(prepared.generated_hdr10plus_json.read_text(encoding="utf-8"))
    assert len(document["SceneInfo"]) == 4 and document["SceneInfoSummary"]["SceneFirstFrameIndex"] == [0, 2]
    assert any(translate_text("mesure de chaque image de la source") in message for _level, message in logs)
    assert list((tmp_path / "cache").glob("*.base.npz"))  # mesure gardée pour le HDR Studio


def test_preview_segment_measure_is_not_cached(tmp_path):
    workflow, _ = _workflow(tmp_path, cache=True)
    engine = _MeasureEngine(write_statistics(tmp_path / "s.jsonl", [100.0] * 4))
    video = _video(generate_hdr10plus=True, dovi_source_profile="p8_1", hdr10plus_generation_cache_write=False)
    _prepare(workflow, tmp_path, video, engine=engine)
    assert not list((tmp_path / "cache").glob("*.npz"))


def _export_run(tmp_path: Path, frames: int = 4):
    rpus = [_rpu(i, scene=i in (0, 2)) for i in range(frames)]
    calls: list[list[str]] = []

    def run(cmd):
        calls.append([str(arg) for arg in cmd])
        if "export" in cmd:
            _write_export(Path(str(cmd[-1]).split("=", 1)[1]), rpus)
        return ""
    return run, calls


def test_preparation_falls_back_to_l1_without_extension(tmp_path):
    workflow, logs = _workflow(tmp_path)
    rpu = tmp_path / "source.rpu"
    rpu.write_bytes(b"rpu")
    run, calls = _export_run(tmp_path)
    video = _video(generate_hdr10plus=True, dovi_source_profile="p8_1", source_rpu_path=rpu)
    prepared = _prepare(workflow, tmp_path, video, run=run)
    assert prepared.hdr10plus_generation_method == METHOD_L1
    assert calls[0][:4] == ["dovi_tool", "export", "-i", str(rpu)]  # RPU préparé réutilisé, jamais réextrait
    assert any(level == "WARN" and "L1" in message for level, message in logs)  # « L1 » : toutes langues
    assert json.loads(prepared.generated_hdr10plus_json.read_text(encoding="utf-8"))["ToolInfo"]["Tool"]


def test_measure_failure_falls_back_to_l1_but_cancellation_does_not(tmp_path):
    workflow, logs = _workflow(tmp_path)
    rpu = tmp_path / "source.rpu"
    rpu.write_bytes(b"rpu")
    run, _ = _export_run(tmp_path)
    broken = _MeasureEngine(tmp_path / "absent.jsonl")  # mesure illisible
    video = _video(generate_hdr10plus=True, dovi_source_profile="p8_1", source_rpu_path=rpu)
    assert _prepare(workflow, tmp_path, video, engine=broken, run=run).hdr10plus_generation_method == METHOD_L1
    assert any("Mesure de l'image impossible" in message for _level, message in logs)
    signals = TaskSignals()
    signals._cancel_event.set()
    engine = _MeasureEngine(write_statistics(tmp_path / "s.jsonl", [100.0] * 4))
    with pytest.raises(TaskCancelledError):
        _prepare(workflow, tmp_path, video, engine=engine, signals=signals, run=run)


def test_p5_and_fel_choose_their_method(tmp_path):
    workflow, _ = _workflow(tmp_path)
    run, _ = _export_run(tmp_path)
    rpu = tmp_path / "p5.rpu"
    rpu.write_bytes(b"rpu")
    engine = _MeasureEngine(write_statistics(tmp_path / "s.jsonl", [100.0] * 4))
    p5 = _video(generate_hdr10plus=True, dovi_source_profile="p5", p5_rpu_path=rpu, p5_to_hdr10=True)
    assert _prepare(workflow, tmp_path, p5, engine=engine, run=run).hdr10plus_generation_method == METHOD_L1
    fel = _video(generate_hdr10plus=True, dovi_source_profile="p7_fel",
                 fel_context=SimpleNamespace(statistics=tmp_path / "fel.jsonl"))
    prepared = _prepare(workflow, tmp_path, fel, engine=engine)
    assert prepared.hdr10plus_generation_method == METHOD_FEL_STATISTICS and prepared.generated_hdr10plus_json is None


def test_rpu_without_l1_is_refused(tmp_path):
    workflow, _ = _workflow(tmp_path)
    rpu = tmp_path / "source.rpu"
    rpu.write_bytes(b"rpu")
    rpus = [_rpu(i, l1=False) for i in range(3)]

    def run(cmd):
        if "export" in cmd:
            _write_export(Path(str(cmd[-1]).split("=", 1)[1]), rpus)
        return ""
    video = _video(generate_hdr10plus=True, dovi_source_profile="p8_1", source_rpu_path=rpu)
    with pytest.raises(EncodeError) as caught:
        _prepare(workflow, tmp_path, video, run=run)
    assert translate_text("RPU Dolby Vision sans L1 : HDR10+ impossible à générer.") in str(caught.value)


def test_fel_fallback_invalidates_a_reconstructed_generation():
    context = SimpleNamespace(original_max_cll="1000,400", original_light_source="source", original_master_display="")
    video = _video(generate_hdr10plus=True, fel_context=context, hdr10plus_generation_method="measure_reconstructed",
                   generated_hdr10plus_json=Path("/tmp/x.json"))
    fallback = fallback_video(video)
    assert fallback.generated_hdr10plus_json is None and fallback.hdr10plus_generation_method == ""
    kept = fallback_video(dataclasses.replace(video, hdr10plus_generation_method=METHOD_MEASURE))
    assert kept.generated_hdr10plus_json == Path("/tmp/x.json")


# ── Validation et routage ────────────────────────────────────────────────────
def test_copy_with_generation_is_patched_not_remuxed(tmp_path):
    copy = _video(codec="copy", copy_dv=True, generate_hdr10plus=True)
    config = EncodeConfig(source=tmp_path / "s.mkv", output=tmp_path / "o.mkv", video=copy, audio_tracks=[])
    assert EncodeWorkflow._needs_metadata_inject(config)
    assert not is_pure_copy(SimpleNamespace(video_tracks=[copy], audio_tracks=[]))
    encode = _video(generate_hdr10plus=True)  # réencodage sans copie DV / HDR10+
    assert EncodeWorkflow._needs_metadata_inject(dataclasses.replace(config, video=encode, video_tracks=[encode]))


@pytest.mark.parametrize("codec,profile,message", [
    ("copy", "p5", "P5"), ("libx265", "p8_4", "non PQ"), ("copy", "p8_2", "non PQ"),
])
def test_generation_refused_on_a_non_pq_or_copied_p5_base(tmp_path, codec, profile, message):
    workflow, _ = _workflow(tmp_path)
    video = _video(codec=codec, copy_dv=True, generate_hdr10plus=True, dovi_source_profile=profile)
    config = EncodeConfig(source=tmp_path / "s.mkv", output=tmp_path / "o.mkv", video=video, audio_tracks=[])
    errors = workflow._hdr10plus_generation_errors(config)
    assert len(errors) == 1 and message in errors[0]
    ok = dataclasses.replace(video, codec="libx265", dovi_source_profile="p5")
    assert not workflow._hdr10plus_generation_errors(dataclasses.replace(config, video=ok, video_tracks=[ok]))


# ── Route d'injection ────────────────────────────────────────────────────────
def test_copy_generation_injects_only_hdr10plus_and_keeps_dolby_vision(tmp_path):
    run = _Inject(tmp_path)
    json_path = _generated(tmp_path)
    config = _inject_config(tmp_path, codec="copy", copy_dv=True, generate_hdr10plus=True,
                            generated_hdr10plus_json=json_path)
    with patch.object(metadata_inject.DoviP7Router, "analyze") as analyze, \
            patch.object(metadata_inject, "has_hdr10plus_sei", return_value=True):
        outcome = run.run(config)
    _assert_only_final_validation_failed(outcome)
    analyze.assert_not_called()  # RPU, EL et record de la piste copiée intacts
    tools = [cmd[:2] for cmd in run.commands if cmd[0] != "ffmpeg"]
    assert ["hdr10plus_tool", "remove"] in tools and ["hdr10plus_tool", "inject"] in tools
    assert not any(cmd[0] == "dovi_tool" for cmd in run.commands)
    assert not any(cmd[:2] == ["hdr10plus_tool", "extract"] for cmd in run.commands)
    inject = next(cmd for cmd in run.commands if cmd[:2] == ["hdr10plus_tool", "inject"])
    assert inject[inject.index("-j") + 1] != str(json_path)  # copie de travail, JSON préparé intact
    assert json_path.is_file()


def test_reencode_generation_only_injects_without_source_hdr10plus(tmp_path):
    run = _Inject(tmp_path)
    config = _inject_config(tmp_path, generate_hdr10plus=True, generated_hdr10plus_json=_generated(tmp_path))
    with patch.object(metadata_inject, "has_hdr10plus_sei", return_value=False):
        outcome = run.run(config)
    _assert_only_final_validation_failed(outcome)
    assert any(cmd[:2] == ["hdr10plus_tool", "inject"] for cmd in run.commands)
    assert not any(cmd[:2] in (["hdr10plus_tool", "remove"], ["hdr10plus_tool", "extract"]) for cmd in run.commands)
    assert _PermissiveGuard.seen and _PermissiveGuard.seen[0]["hdr10p_json"] is not None
    # HDR10+ généré : compte source exact (paquets), jamais le raccourci des métadonnées (lot 4, route FFmpeg).
    assert _PermissiveGuard.seen[0]["exact_source"] is True


def test_missing_prepared_json_fails(tmp_path):
    run = _Inject(tmp_path)
    outcome = run.run(_inject_config(tmp_path, generate_hdr10plus=True))
    assert outcome.failed and "préparation incomplète" in outcome.failed[0][0]


def test_raw_hevc_source_is_never_deleted_in_copy(tmp_path):
    run = _Inject(tmp_path)
    source = tmp_path / "film.hevc"
    source.write_bytes(b"\x00\x00\x00\x01source")
    config = EncodeConfig(source=source, output=tmp_path / "sortie.mkv",
                          video=_video(codec="copy", copy_dv=True, generate_hdr10plus=True,
                                       generated_hdr10plus_json=_generated(tmp_path)),
                          audio_tracks=[], copy_subtitles=False, duration_s=60.0, work_dir=tmp_path / "work")
    with patch.object(metadata_inject, "has_hdr10plus_sei", return_value=True):
        outcome = run.run(config)
    _assert_only_final_validation_failed(outcome)
    assert source.is_file()  # jamais supprimée (avant : flux courant puis _free après injection)
    for cmd in run.commands:
        if cmd[0] in ("hdr10plus_tool", "dovi_tool"):
            assert str(source) not in cmd, cmd  # outils sur le flux extrait de la copie, jamais sur la source


# ── Multi-pistes ─────────────────────────────────────────────────────────────
def test_copied_track_input_args_compensate_the_artifact_start():
    callbacks = SimpleNamespace(offset_input_args=lambda ms: ["-itsoffset", f"{ms / 1000:.3f}"] if ms > 0 else [],
                                stream_start_time=lambda path, stream: 0.0 if "wrapped" not in str(path) else 0.083)
    runner = multi_video.MultiVideoPipelineRunner(callbacks)  # type: ignore[arg-type]
    assert runner._copied_input_args(Path("s.mkv"), 0, Path("wrapped.mkv"), 0) == ["-itsoffset", "-0.083000"]
    assert runner._copied_input_args(Path("s.mkv"), 0, Path("wrapped.mkv"), 500) == ["-itsoffset", "0.417000"]
    aligned = SimpleNamespace(offset_input_args=lambda ms: [], stream_start_time=lambda path, stream: 0.0)
    assert multi_video.MultiVideoPipelineRunner(aligned)._copied_input_args(  # type: ignore[arg-type]
        Path("s.mkv"), 0, Path("wrapped.mkv"), 0) == []


# ── NVEncC ───────────────────────────────────────────────────────────────────
def test_nvencc_receives_the_generated_json_and_drops_parallel_options(tmp_path):
    video = _video(codec="nvencc_hevc", generate_hdr10plus=True, extra_params="--parallel 2 --tune lowlatency")
    cmd = build_nvencc_command("NVEncC64", video, tmp_path / "o.mkv", input_path=tmp_path / "s.mkv",
                               hdr10plus_json=tmp_path / "generated_hdr10plus.json")
    assert cmd[cmd.index("--dhdr10-info") + 1] == str(tmp_path / "generated_hdr10plus.json")
    assert "--parallel" not in cmd and "lowlatency" not in cmd


# ── Aperçus ──────────────────────────────────────────────────────────────────
def test_preview_segment_resets_generation(tmp_path):
    workflow, _ = _workflow(tmp_path)
    video = _video(generate_hdr10plus=True, generated_hdr10plus_json=tmp_path / "x.json",
                   hdr10plus_generation_method=METHOD_MEASURE)
    config = EncodeConfig(source=tmp_path / "s.mkv", output=tmp_path / "o.mkv", video=video, audio_tracks=[])
    preview = workflow._preview_encode_config(config, source_segment=tmp_path / "seg.mkv", output=tmp_path / "p.mkv",
                                              clip_duration_s=5.0)
    assert preview.video.generate_hdr10plus and preview.video.generated_hdr10plus_json is None
    assert preview.video.hdr10plus_generation_cache_write is False


def test_command_preview_announces_the_generation(tmp_path):
    workflow, _ = _workflow(tmp_path)
    source = tmp_path / "s.mkv"
    source.write_bytes(b"\x00" * 64)
    video = _video(generate_hdr10plus=True)
    config = EncodeConfig(source=source, output=tmp_path / "o.mkv", video=video, audio_tracks=[],
                          allow_missing_output_dir=True)
    with patch.object(workflow, "_detect_source_dynamic_hdr_presence", return_value=(True, False)):
        text = workflow.preview_command(config)
    assert "HDR10+ généré au lancement depuis le Dolby Vision" in text
    assert threading.active_count() >= 1


def test_multi_copy_track_is_patched_with_the_generated_json(tmp_path):
    from tests.test_encode_runtime_failures import _Multi

    run = _Multi(tmp_path, stream_start_time=lambda path, stream: 0.0, offset_input_args=lambda ms: [])
    spec = run.spec(codec="copy", copy_dv=True, generate_hdr10plus=True, generated_hdr10plus_json=_generated(tmp_path))
    with patch.object(multi_video, "has_hdr10plus_sei", return_value=False):
        prepared, _cleanup = run.prepare(spec)
    labels = [label for label, _ in run.commands]
    copy = next(cmd for label, cmd in run.commands if label == "ffmpeg-video-1")
    assert copy[copy.index("-c:v") + 1] == "copy"  # jamais réencodée
    assert "hdr10plus-inject-1" in labels and not any(label.startswith(("dovi-", "hdr10plus-extract"))
                                                     for label in labels)
    assert Path(prepared.path).name == "video_1.wrapped.mkv" and prepared.map_arg == "0:v:0"
    assert _PermissiveGuard.seen[0]["source_stream_index"] == 0  # source lue directement : flux sélectionné
    assert _PermissiveGuard.seen[0]["exact_source"] is True  # HDR10+ généré : compte exact (lot 4, multi-pistes)


def test_multi_fel_fallback_regenerates_the_hdr10plus(tmp_path):
    from unittest.mock import Mock

    from core.fel.context import FelExecution
    from core.fel.engine import FelError
    from tests.test_encode_runtime_failures import _Multi

    regenerated: list[VideoEncodeSettings] = []

    def regenerate(config, video, index, work_dir, signals):
        regenerated.append(video)
        return dataclasses.replace(video, generated_hdr10plus_json=_generated(tmp_path),
                                   hdr10plus_generation_method=METHOD_MEASURE)

    run = _Multi(tmp_path, regenerate_hdr10plus=regenerate)
    fel = FelExecution(source=Mock(), original_rpu=tmp_path / "absent.bin", original_max_cll="1000,400",
                       original_light_source="source")
    spec = run.spec(fel_context=fel, bake_dovi_fel=True, generate_hdr10plus=True,
                    hdr10plus_generation_method=METHOD_FEL_STATISTICS)
    attempts: list[VideoEncodeSettings] = []

    def prepare(**kwargs):
        attempts.append(kwargs["spec"].video)
        if len(attempts) == 1:
            raise FelError("couche d'amélioration illisible")
        output = kwargs["work_dir"] / "video_1.mkv"
        output.write_bytes(b"\x00")
        return multi_video.PreparedVideoInput(input_args=[], path=output, map_arg="0:v:0"), [output]

    with patch.object(run.runner, "_prepare_multi_video_track", side_effect=prepare):
        run.prepare(spec, public=True)
    assert len(regenerated) == 1 and regenerated[0].fel_context is None
    assert attempts[1].hdr10plus_generation_method == METHOD_MEASURE and attempts[1].generated_hdr10plus_json


def test_nvencc_fel_fallback_regenerates_the_hdr10plus_before_the_retry(tmp_path):
    from tests.test_encode_runtime_failures import _fel_context, _Nvencc
    from core.workflows.encode.runtime import nvencc_execution

    fel = dataclasses.replace(_fel_context(tmp_path), statistics=tmp_path / "fel_statistics.jsonl")
    run = _Nvencc(tmp_path, fel_context=fel, bake_dovi_fel=True, generate_hdr10plus=True,
                  hdr10plus_generation_method=METHOD_FEL_STATISTICS)
    regenerated: list[VideoEncodeSettings] = []

    def regenerate(config, video, *, index, work_dir, run, signals, engine_loader=None):
        regenerated.append(video)
        return dataclasses.replace(video, generated_hdr10plus_json=_generated(tmp_path),
                                   hdr10plus_generation_method=METHOD_MEASURE)

    pipe = __import__("unittest.mock", fromlist=["Mock"]).Mock(side_effect=nvencc_execution.FelError("illisible"))
    outcome = run.run(patch.object(nvencc_execution.NvenccPipeExecutor, "run", pipe),
                      patch("core.fel.pipeline.with_fel_input", side_effect=lambda cmd, video: cmd),
                      patch.object(run.workflow, "prepare_generated_hdr10plus", side_effect=regenerate),
                      patch.object(run.workflow, "_frame_count_guard",
                                   return_value=SimpleNamespace(source_frame_count=lambda path, stream=None, **_k: 3)))
    assert not outcome.failed, outcome.failed
    assert len(regenerated) == 1 and regenerated[0].fel_context is None
    assert "nvencc" in run.labels


def test_plugin_cancellation_never_falls_back_to_l1(tmp_path):
    from core.fel.engine import FelCancelled

    class _Cancelled(_MeasureEngine):
        def measure(self, *args, **kwargs):
            raise FelCancelled()

    workflow, _ = _workflow(tmp_path)
    rpu = tmp_path / "source.rpu"
    rpu.write_bytes(b"rpu")
    run, calls = _export_run(tmp_path)
    video = _video(generate_hdr10plus=True, dovi_source_profile="p8_1", source_rpu_path=rpu)
    with pytest.raises(TaskCancelledError):
        _prepare(workflow, tmp_path, video, engine=_Cancelled(tmp_path / "x"), run=run)  # jeton non levé
    assert not calls  # aucun export L1


def test_each_generation_writes_in_its_own_folder(tmp_path):
    workflow, _ = _workflow(tmp_path)
    engine = _MeasureEngine(write_statistics(tmp_path / "s.jsonl", [100.0] * 4))
    video = _video(generate_hdr10plus=True, dovi_source_profile="p8_1")
    first = _prepare(workflow, tmp_path, video, engine=engine).generated_hdr10plus_json
    second = _prepare(workflow, tmp_path, video, engine=engine).generated_hdr10plus_json
    assert first.parent != second.parent and first.is_file() and second.is_file()


def test_copy_generation_of_a_raw_hevc_source_is_refused(tmp_path):
    workflow, _ = _workflow(tmp_path)
    video = _video(codec="copy", copy_dv=True, generate_hdr10plus=True, dovi_source_profile="p8_1")
    config = EncodeConfig(source=tmp_path / "film.hevc", output=tmp_path / "o.mkv", video=video, audio_tracks=[])
    errors = workflow._hdr10plus_generation_errors(config)
    assert len(errors) == 1 and "HEVC brut" in errors[0]


def test_nvencc_generated_json_shorter_than_the_source_is_refused(tmp_path):
    from tests.test_encode_runtime_failures import _Nvencc

    run = _Nvencc(tmp_path, generate_hdr10plus=True, generated_hdr10plus_json=_generated(tmp_path, frames=3))
    guard = SimpleNamespace(source_frame_count=lambda path, stream=None, **_k: 100)
    outcome = run.run(patch.object(run.workflow, "_frame_count_guard", return_value=guard))
    (message, error), = outcome.failed
    assert isinstance(error, EncodeError) and "3" in message and "100" in message
    assert "nvencc" not in run.labels  # refus avant l'encodage


def test_nvencc_stale_quick_count_is_recounted_exactly_before_refusing(tmp_path):
    """HDR10+ généré : compte exact des paquets demandé d'emblée, compte rapide (périmé) jamais lu (lot 4)."""
    from tests.test_encode_runtime_failures import _Nvencc

    run = _Nvencc(tmp_path, generate_hdr10plus=True, generated_hdr10plus_json=_generated(tmp_path, frames=3))
    calls: list[bool] = []

    def count(path, stream=None, *, exact=False):
        calls.append(exact)
        return 3 if exact else 4  # compte rapide périmé, compte exact juste

    guard = SimpleNamespace(source_frame_count=count)
    run.nvencc_output = ["encoded 3 frames"]
    outcome = run.run(patch.object(run.workflow, "_frame_count_guard", return_value=guard))
    assert calls == [True]
    assert "nvencc" in run.labels
    assert not any("3 entrées pour 4" in message for message, _ in outcome.failed)


@pytest.mark.parametrize("override", [False, True])
def test_nvencc_unreadable_source_count_is_refused_unless_overridden(tmp_path, override):
    from tests.test_encode_runtime_failures import _Nvencc

    run = _Nvencc(tmp_path, generate_hdr10plus=True, generated_hdr10plus_json=_generated(tmp_path, frames=3))
    guard = SimpleNamespace(source_frame_count=lambda path, stream=None, **_k: None)
    patches = [patch.object(run.workflow, "_frame_count_guard", return_value=guard)]
    if override:
        patches.append(patch.object(nvencc_execution, "accept_validation_override", return_value=True))
    outcome = run.run(*patches)
    if override:
        assert "nvencc" in run.labels
    else:
        (message, error), = outcome.failed
        assert isinstance(error, EncodeError) and message == translate_text(
            "Audit trames : HDR10+ généré : images de la source illisibles, alignement des trames invérifiable.")
        assert "nvencc" not in run.labels


def _nvencc_fel_statistics(tmp_path, counts):
    """Encodage NVEncC FEL + HDR10+ généré (statistiques du plugin, 2 images encodées) ; source comptée par
    ``counts`` (compte rapide, compte exact). Renvoie l'exécution, les appels de comptage et l'injection."""
    from unittest.mock import Mock

    from tests.test_encode_runtime_failures import _fel_context, _Nvencc

    fel = dataclasses.replace(_fel_context(tmp_path), statistics=tmp_path / "fel_statistics.jsonl")
    run = _Nvencc(tmp_path, fel_context=fel, bake_dovi_fel=True, generate_hdr10plus=True,
                  hdr10plus_generation_method=METHOD_FEL_STATISTICS)
    calls: list[bool] = []

    def count(path, stream=None, *, exact=False):
        calls.append(exact)
        return counts[1] if exact else counts[0]

    def pipe(*, encode_cmd, monitor, **_kwargs):
        run.labels.append("nvencc-pipe")
        Path(encode_cmd[encode_cmd.index("-o") + 1]).write_bytes(b"\x00")
        monitor.feed("encoded 2 frames")

    inject = Mock()
    outcome = run.run(patch.object(nvencc_execution.NvenccPipeExecutor, "run", side_effect=pipe),
                      patch("core.fel.pipeline.with_fel_input", side_effect=lambda cmd, video: cmd),
                      patch.object(nvencc_execution, "_apply_fel_hdr10plus", inject),
                      patch.object(run.workflow, "_frame_count_guard",
                                   return_value=SimpleNamespace(source_frame_count=count)))
    return run, outcome, calls, inject


def test_nvencc_fel_statistics_generation_keeps_the_source_audit(tmp_path):
    _run, _outcome, calls, inject = _nvencc_fel_statistics(tmp_path, (100, 100))
    assert inject.call_args.kwargs["expected_frames"] == 100  # injection refusera 2 entrées face à 100 images
    assert calls == [True]  # HDR10+ généré : compte exact d'emblée (lot 4)
    assert "nvencc-pipe" in _run.labels


def test_nvencc_fel_statistics_stale_quick_count_is_recounted(tmp_path):
    _run, _outcome, calls, inject = _nvencc_fel_statistics(tmp_path, (4, 2))
    assert inject.call_args.kwargs["expected_frames"] == 2 and calls == [True]  # compte rapide jamais lu


def test_nvencc_fel_statistics_unreadable_source_count_is_refused_before_encoding(tmp_path):
    run, outcome, _calls, inject = _nvencc_fel_statistics(tmp_path, (None, None))
    (message, error), = outcome.failed
    assert isinstance(error, EncodeError) and message == translate_text(
        "Audit trames : HDR10+ généré : images de la source illisibles, alignement des trames invérifiable.")
    assert not inject.called and "nvencc-pipe" not in run.labels  # refus avant l'encodage


def test_frame_count_guard_probes_are_killed_by_cancellation(tmp_path):
    import shutil
    import time

    if shutil.which("sleep") is None:
        pytest.skip("commande sleep absente")
    workflow, _ = _workflow(tmp_path)
    signals = TaskSignals()
    guard = workflow._frame_count_guard(signals)
    threading.Timer(0.2, signals.cancel).start()
    started = time.monotonic()
    with pytest.raises(TaskCancelledError):
        guard._run(["sleep", "30"], capture_output=True, text=True)
    assert time.monotonic() - started < 10  # sonde arrêtée, pas attendue


def test_nvencc_cancellation_during_the_source_count_stops_before_encoding(tmp_path):
    from tests.test_encode_runtime_failures import _Nvencc

    run = _Nvencc(tmp_path, generate_hdr10plus=True, generated_hdr10plus_json=_generated(tmp_path, frames=3))
    seen: list[object] = []

    def count(path, stream=None, *, exact=False, **_kwargs):
        run.signals.cancel()  # « Annuler » pendant la lecture de la source
        return 3

    def guard_for(signals=None):
        seen.append(signals)
        return SimpleNamespace(source_frame_count=count)

    outcome = run.run(patch.object(run.workflow, "_frame_count_guard", side_effect=guard_for))
    assert outcome.cancelled == 1 and not outcome.failed and "nvencc" not in run.labels
    assert seen and seen[0] is not None  # sondes liées aux signaux du job
