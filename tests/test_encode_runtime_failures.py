"""Branches d'échec, de refus et d'annulation des runtimes d'encodage.

Injection DoVi / HDR10+ (``metadata_inject``), préparation d'une piste du pipeline multi-pistes
(``multi_video``) et encodage NVEncC direct (``nvencc_execution``) : chaque refus produit un échec
explicite (jamais une sortie), chaque annulation un état annulé, et le dossier de travail propre au
traitement est supprimé. Outils externes simulés : seules les décisions du runtime sont vérifiées.
"""

from __future__ import annotations

import dataclasses
import sys
import threading
import time
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

import pytest

from core.fel.context import FelExecution
from core.i18n import translate_text
from core.fel.engine import FelError
from core.fel.hdr10plus import FrameStatisticsError
from core.matroska.hevc.access_units import HevcStreamCancelled
from core.matroska.hevc.timing_skeleton import TimingSkeletonResult
from core.matroska.writer import MatroskaWriteCancelled
from core.runner import TaskCancelledError, TaskSignals
from core.workflows.encode import EncodeConfig, EncodeError, EncodeWorkflow, QualityMode, VideoEncodeSettings
from core.workflows.encode.dovi_policy import P5_COPY_NORMALIZE_ERROR
from core.dovi_profile_detector import DoviSubProfile
from core.workflows.encode.runtime import metadata_inject, multi_video, nvencc_execution
from core.workflows.encode.runtime.dovi_p7_router import P7RoutingDecision
from core.workflows.encode.runtime.frame_count_guard import FrameCountAuditError
from core.workflows.encode.runtime.multi_video import MultiVideoPipelineRunner, MultiVideoPipelineRunnerCallbacks
from core.workflows.encode.runtime.static_hdr_estimator import StaticHdrEstimate
from core.workflows.encode.runtime_helpers import VideoTrackPrepSpec

_TOOL_OUTPUTS = (".hevc", ".mkv", ".bin", ".json")


@pytest.fixture(autouse=True)
def _ensure_qt_app(qt_app):
    return qt_app


@pytest.fixture(autouse=True)
def _matroska_stubs(monkeypatch):
    """Squelette de timing et réécriture des payloads simulés : les entrées sont des fichiers factices."""

    def skeleton(encoded_mkv, output, **_kwargs):
        Path(output).write_bytes(b"\x00" * 64)
        return TimingSkeletonResult(output=Path(output), blocks_written=1)

    class Rewriter:
        def rewrite(self, *, output, **_kwargs):
            Path(output).write_bytes(b"\x00" * 64)
            return SimpleNamespace(frames_rewritten=1)

    for module in (metadata_inject, multi_video):
        monkeypatch.setattr(module, "write_timing_skeleton", skeleton)
        monkeypatch.setattr(module, "MatroskaHevcPayloadRewriter", Rewriter)


def _create_output(cmd: list[str]) -> None:
    """Sortie simulée d'un outil : le dernier argument portant une extension de média ou de métadonnées."""
    for arg in reversed(cmd):
        if str(arg).endswith(_TOOL_OUTPUTS):
            Path(arg).parent.mkdir(parents=True, exist_ok=True)
            Path(arg).write_bytes(b"\x00" * 4096)
            return


@dataclass
class _Outcome:
    finished: list[str] = field(default_factory=list)
    failed: list[tuple[str, object]] = field(default_factory=list)
    cancelled: int = 0
    progress: list[str] = field(default_factory=list)


def _watch(signals: TaskSignals) -> _Outcome:
    outcome = _Outcome()
    signals.finished.connect(outcome.finished.append)
    signals.failed.connect(lambda message, exc: outcome.failed.append((message, exc)))
    signals.cancelled.connect(lambda: setattr(outcome, "cancelled", outcome.cancelled + 1))
    signals.progress.connect(outcome.progress.append)
    return outcome


class _AuditRefusal:
    """Garde de comptage dont l'application refuse (métadonnées plus courtes que la vidéo)."""

    def __init__(self, **_kwargs) -> None:
        pass

    def audit(self, **_kwargs):
        return SimpleNamespace()

    def enforce(self, *_args, **_kwargs):
        raise FrameCountAuditError("RPU Dolby Vision : 3 trame(s) manquante(s)", missing={"rpu": 3})


# --- Injection DoVi / HDR10+ (metadata_inject) --------------------------------------------------


def _video(**overrides: Any) -> VideoEncodeSettings:
    values: dict[str, Any] = dict(codec="libx265", quality_mode=QualityMode.CRF, crf=18, preset="slow")
    values.update(overrides)
    return VideoEncodeSettings(**values)


def _inject_config(tmp_path: Path, **video: Any) -> EncodeConfig:
    source = tmp_path / "source.mkv"
    source.write_bytes(b"\x00" * 8192)
    return EncodeConfig(source=source, output=tmp_path / "sortie.mkv", video=_video(**video), audio_tracks=[],
                        copy_subtitles=False, duration_s=60.0, work_dir=tmp_path / "work")


class _Inject:
    """Injection exécutée dans le fil du test (signaux de préparation), outils simulés."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.workflow = EncodeWorkflow(ffmpeg_bin="ffmpeg", dovi_tool_bin="dovi_tool", hdr10plus_bin="hdr10plus_tool")
        self.signals = TaskSignals()
        self.outcome = _watch(self.signals)
        self.commands: list[list[str]] = []
        self.logs: list[tuple[str, str]] = []
        self.workflow.log_message.connect(lambda level, message: self.logs.append((level, message)))
        self.estimate_failures: list[tuple[str, str]] = []
        self.workflow.static_hdr_estimate_failed.connect(
            lambda track, message: self.estimate_failures.append((track, message)))

    def tool(self, cmd, signals=None, cwd=None, progress_cb=None, **_kwargs) -> str:
        self.commands.append([str(arg) for arg in cmd])
        _create_output(cmd)
        return ""

    def run(self, config: EncodeConfig, **kwargs: Any) -> _Outcome:
        with patch.object(self.workflow._runner, "_run_cmd", side_effect=self.tool):
            self.workflow._run_with_metadata_inject(config, prep_signals=self.signals, **kwargs)
        return self.outcome

    def assert_work_dir_removed(self) -> None:
        work = self.tmp_path / "work"
        assert not work.exists() or not any(work.iterdir()), list(work.rglob("*"))


def _decision(sub_profile: DoviSubProfile) -> P7RoutingDecision:
    return P7RoutingDecision(conversion_needed=True, sub_profile=sub_profile, convert_mode="2",
                             reason="conversion requise")


def test_friendly_conversion_error_keeps_the_tool_message_or_names_the_free_space(tmp_path):
    assert metadata_inject._friendly_dovi_conversion_error(RuntimeError("RPU invalide"), tmp_path) == (
        "Conversion Dolby Vision P5→P8.1 impossible : RPU invalide")
    with patch.object(metadata_inject.shutil, "disk_usage", side_effect=OSError("indisponible")):
        message = metadata_inject._friendly_dovi_conversion_error(
            RuntimeError("Disk quota exceeded (os error 122)"), tmp_path)
    assert "quota ou espace temporaire insuffisant" in message and "libre : inconnu" in message


@pytest.mark.parametrize("result", [
    OSError("mediainfo absent"),
    subprocess.CompletedProcess([], 1, stdout="", stderr="erreur"),
    subprocess.CompletedProcess([], 0, stdout="{pas du json", stderr=""),
    subprocess.CompletedProcess([], 0, stdout='{"media": {"track": [{"@type": "Audio"}]}}', stderr=""),
])
def test_unreadable_mediainfo_gives_no_video_track(tmp_path, result):
    side_effect = result if isinstance(result, Exception) else None
    with patch.object(metadata_inject.subprocess, "run", side_effect=side_effect, return_value=result):
        assert metadata_inject._load_mediainfo_video("mediainfo", tmp_path / "f.mkv") is None


def test_p5_copy_is_refused_before_any_tool(tmp_path):
    run = _Inject(tmp_path)
    with patch.object(metadata_inject, "_load_mediainfo_video", return_value=None), \
            patch.object(metadata_inject.DoviP7Router, "analyze", return_value=_decision(DoviSubProfile.P5)):
        outcome = run.run(_inject_config(tmp_path, codec="copy", copy_dv=True, dovi_profile="2"))
    assert [message for message, _ in outcome.failed] == [P5_COPY_NORMALIZE_ERROR]
    assert run.commands == [] and not outcome.finished
    run.assert_work_dir_removed()


def test_p7_conversion_failure_names_the_quota_and_reports_the_pending_hdr10_analysis(tmp_path):
    run = _Inject(tmp_path)
    config = _inject_config(tmp_path, copy_dv=True, track_entry_id="v1", static_hdr_metadata_analysis_request="fast")
    with patch.object(metadata_inject, "_load_mediainfo_video", return_value=None), \
            patch.object(metadata_inject.DoviP7Router, "analyze", return_value=_decision(DoviSubProfile.P7_FEL)), \
            patch.object(metadata_inject.DoviP7Router, "execute_conversion",
                         side_effect=RuntimeError("No space left on device (os error 28)")):
        outcome = run.run(config)
    (message, error), = outcome.failed
    assert isinstance(error, EncodeError) and "quota ou espace temporaire insuffisant" in message
    assert run.estimate_failures == [("v1", message)]
    assert not any("inject-rpu" in cmd for cmd in run.commands)
    run.assert_work_dir_removed()


def test_timing_skeleton_cancellation_ends_cancelled(tmp_path):
    run = _Inject(tmp_path)
    with patch.object(metadata_inject, "write_timing_skeleton", side_effect=MatroskaWriteCancelled()):
        outcome = run.run(_inject_config(tmp_path, copy_hdr10plus=True))
    assert outcome.cancelled == 1 and not outcome.failed and not outcome.finished
    run.assert_work_dir_removed()


def test_payload_rewrite_cancellation_ends_cancelled(tmp_path):
    run = _Inject(tmp_path)
    with patch.object(metadata_inject, "FrameCountGuard", Mock()), \
            patch.object(metadata_inject.MatroskaHevcPayloadRewriter, "rewrite", side_effect=HevcStreamCancelled()):
        outcome = run.run(_inject_config(tmp_path, copy_hdr10plus=True))
    assert outcome.cancelled == 1 and not outcome.failed and not outcome.finished
    run.assert_work_dir_removed()


def test_cancellation_during_a_tool_ends_cancelled_without_running_the_next_one(tmp_path):
    run = _Inject(tmp_path)

    def tool(cmd, *args, **kwargs):
        run.tool(cmd, *args, **kwargs)
        run.signals.cancel()  # annulation reçue pendant le premier outil
        return ""

    with patch.object(run.workflow._runner, "_run_cmd", side_effect=tool):
        run.workflow._run_with_metadata_inject(_inject_config(tmp_path, copy_hdr10plus=True), prep_signals=run.signals)
    assert run.outcome.cancelled == 1 and not run.outcome.failed and not run.outcome.finished
    assert len(run.commands) == 1
    run.assert_work_dir_removed()


@pytest.mark.parametrize("estimate, expected", [
    (RuntimeError("lecture impossible"), "Analyse HDR10 P5→P8.1 impossible : lecture impossible"),
    (object(), "Analyse HDR10 P5→P8.1 impossible : résultat interne invalide."),
])
def test_static_hdr_estimate_failure_is_reported_and_fails(tmp_path, estimate, expected):
    run = _Inject(tmp_path)
    estimator = Mock(side_effect=estimate) if isinstance(estimate, Exception) else Mock(return_value=estimate)
    with patch.object(run.workflow._static_hdr_estimator, "estimate_converted_static_hdr", estimator):
        outcome = run.run(_inject_config(tmp_path, track_entry_id="v1", static_hdr_metadata_analysis_request="fast"))
    assert [message for message, _ in outcome.failed] == [expected]
    assert run.estimate_failures == [("v1", expected)]
    run.assert_work_dir_removed()


def test_static_hdr_estimate_interrupted_by_cancellation_ends_cancelled(tmp_path):
    run = _Inject(tmp_path)

    def estimate(*_args, **_kwargs):
        run.signals.cancel()
        raise RuntimeError("lecteur interrompu")

    with patch.object(run.workflow._static_hdr_estimator, "estimate_converted_static_hdr", side_effect=estimate):
        outcome = run.run(_inject_config(tmp_path, static_hdr_metadata_analysis_request="fast"))
    assert outcome.cancelled == 1 and not outcome.failed and run.estimate_failures == []


def test_static_hdr_estimate_success_is_injected(tmp_path):
    run = _Inject(tmp_path)
    estimate = StaticHdrEstimate(master_display="G(1,1)B(1,1)R(1,1)WP(1,1)L(10000000,50)", max_cll="1000,400",
                                 confidence="high", sample_count=12, coverage=0.9)
    ready: list[tuple[str, object]] = []
    run.workflow.static_hdr_estimate_ready.connect(lambda track, value: ready.append((track, value)))
    injected: list[dict] = []

    def inject(source, output, **kwargs):
        injected.append(kwargs)
        Path(output).write_bytes(b"\x00" * 64)
        return SimpleNamespace(applied=True, injected_access_units=1)

    with patch.object(run.workflow._static_hdr_estimator, "estimate_converted_static_hdr", return_value=estimate), \
            patch.object(metadata_inject, "inject_static_hdr_sei_file", side_effect=inject), \
            patch.object(run.workflow, "_finalize_ffmpeg_output", return_value="sortie.mkv"):
        outcome = run.run(_inject_config(tmp_path, track_entry_id="v1", static_hdr_metadata_analysis_request="fast"))
    assert outcome.finished == ["sortie.mkv"] and not outcome.failed
    assert ready == [("v1", estimate)]
    assert injected == [{"master_display": estimate.master_display, "max_cll": "1000,400"}]
    assert any(level == "WARN" and "estimé" in message for level, message in run.logs)


def test_unreadable_light_level_statistics_fail_the_encode(tmp_path):
    run = _Inject(tmp_path)
    with patch.object(metadata_inject, "light_level_statistics_path", return_value=tmp_path / "stats.bin"), \
            patch.object(metadata_inject, "deferred_light_levels", return_value=True), \
            patch.object(metadata_inject, "read_frame_statistics",
                         side_effect=FrameStatisticsError("statistiques tronquées")):
        outcome = run.run(_inject_config(tmp_path))
    assert [message for message, _ in outcome.failed] == ["statistiques tronquées"]
    run.assert_work_dir_removed()


def test_frame_count_refusal_fails_without_an_explicit_decision(tmp_path):
    run = _Inject(tmp_path)
    with patch.object(metadata_inject, "FrameCountGuard", _AuditRefusal):
        outcome = run.run(_inject_config(tmp_path, copy_hdr10plus=True))
    (message, error), = outcome.failed
    assert message == "Audit frame count : RPU Dolby Vision : 3 trame(s) manquante(s)"
    assert isinstance(error, RuntimeError)
    assert not any(cmd[:2] == ["hdr10plus_tool", "inject"] for cmd in run.commands)
    run.assert_work_dir_removed()


def test_frame_count_refusal_overridden_by_the_user_continues_with_a_warning(tmp_path):
    run = _Inject(tmp_path)
    decisions: list[str] = []
    run.workflow.set_validation_override(lambda path, message, cancelled: decisions.append(message) or True)
    with patch.object(metadata_inject, "FrameCountGuard", _AuditRefusal), \
            patch.object(run.workflow, "_finalize_ffmpeg_output", return_value="sortie.mkv"):
        outcome = run.run(_inject_config(tmp_path, copy_hdr10plus=True))
    assert outcome.finished == ["sortie.mkv"] and not outcome.failed
    assert decisions == ["Audit frame count : RPU Dolby Vision : 3 trame(s) manquante(s)"]
    assert any(cmd[:2] == ["hdr10plus_tool", "inject"] for cmd in run.commands)
    assert any(level == "WARN" and "décision explicite" in message for level, message in run.logs)


def test_native_assembly_refuses_a_missing_dolby_vision_record(tmp_path):
    run = _Inject(tmp_path)
    with patch.object(metadata_inject, "_load_mediainfo_video", return_value=None), \
            patch.object(metadata_inject.DoviP7Router, "analyze",
                         return_value=P7RoutingDecision(False, DoviSubProfile.P8_1, None, "P8.1")), \
            patch.object(metadata_inject, "convert_p7_rpu_to_p81", return_value=False), \
            patch.object(metadata_inject, "_build_dovi_record_from_rpu", return_value=None):
        outcome = run.run(_inject_config(tmp_path, copy_dv=True), mux_decision=SimpleNamespace(selected="native"))
    assert [message for message, _ in outcome.failed] == [
        "Dolby Vision natif requis mais le DolbyVisionConfigRecord n'a pas pu être construit."]
    assert not any("inject-rpu" in cmd for cmd in run.commands)
    run.assert_work_dir_removed()


def _dovi_post_action(tmp_path: Path) -> tuple[_Inject, Any]:
    """Action de signalisation DoVi passée à la finalisation FFmpeg (fichier candidat)."""
    run = _Inject(tmp_path)
    finalize = Mock(return_value="sortie.mkv")
    with patch.object(metadata_inject, "_load_mediainfo_video", return_value=None), \
            patch.object(metadata_inject.DoviP7Router, "analyze",
                         return_value=P7RoutingDecision(False, DoviSubProfile.P8_1, None, "P8.1")), \
            patch.object(metadata_inject, "convert_p7_rpu_to_p81", return_value=False), \
            patch.object(metadata_inject, "_build_dovi_record_from_rpu", return_value=None), \
            patch.object(run.workflow, "_finalize_ffmpeg_output", finalize):
        outcome = run.run(_inject_config(tmp_path, copy_dv=True))
    assert outcome.finished == ["sortie.mkv"], outcome.failed
    (action,) = finalize.call_args.kwargs["extra_post_actions"]
    return run, action


def test_dolby_vision_signalling_without_record_warns_and_keeps_the_file(tmp_path):
    run, action = _dovi_post_action(tmp_path)
    with patch.object(metadata_inject, "_build_dovi_record_from_rpu", return_value=None):
        assert action(tmp_path / "candidat.mkv") is None
    assert any("Impossible d'extraire le profil DOVI" in line for line in run.outcome.progress)


def test_dolby_vision_signalling_failure_warns_and_keeps_the_file(tmp_path):
    run, action = _dovi_post_action(tmp_path)
    with patch.object(metadata_inject, "_build_dovi_record_from_rpu", return_value=object()), \
            patch.object(metadata_inject.MatroskaDoviBlockAdditionEditor, "patch",
                         side_effect=OSError("lecture seule")):
        assert action(tmp_path / "candidat.mkv") is None
    assert any("Patch BlockAdditionMapping DOVI échoué : lecture seule" in line for line in run.outcome.progress)


@pytest.mark.parametrize("result, expected", [
    (SimpleNamespace(applied=True, skipped=False, patched_track_number=1, bytes_delta=42),
     "BlockAdditionMapping DOVI ajouté (track #1, Δ +42 octets)."),
    (SimpleNamespace(applied=False, skipped=True, reason="déjà présent"),
     "Signal DOVI Matroska non modifié : déjà présent"),
])
def test_dolby_vision_signalling_reports_its_result(tmp_path, result, expected):
    run, action = _dovi_post_action(tmp_path)
    with patch.object(metadata_inject, "_build_dovi_record_from_rpu", return_value=object()), \
            patch.object(metadata_inject.MatroskaDoviBlockAdditionEditor, "patch", return_value=result):
        assert action(tmp_path / "candidat.mkv") is result
    assert expected in run.outcome.progress


# --- Préparation d'une piste du pipeline multi-pistes (multi_video) -----------------------------


def _check_cancelled(signals: TaskSignals | None) -> None:
    if signals is not None and signals._cancel_event.is_set():
        raise TaskCancelledError()


class _Multi:
    """Préparation d'une piste vidéo, callbacks minimaux, outils simulés."""

    def __init__(self, tmp_path: Path, **callbacks: Any) -> None:
        self.tmp_path = tmp_path
        self.work = tmp_path / "work"
        self.work.mkdir()
        self.source = tmp_path / "source.hevc"
        self.source.write_bytes(b"\x00" * 8192)
        self.signals = TaskSignals()
        self.outcome = _watch(self.signals)
        self.commands: list[tuple[str, list[str]]] = []
        self.logs: list[str] = []
        unused = {
            item.name: (lambda *args, **kwargs: None)
            for item in dataclasses.fields(MultiVideoPipelineRunnerCallbacks)
            if item.default is dataclasses.MISSING and item.default_factory is dataclasses.MISSING
        }
        values: dict[str, Any] = {
            **unused,
            "ffmpeg_bin": "ffmpeg",
            "bins": {"dovi_tool": "dovi_tool", "hdr10plus_tool": "hdr10plus_tool", "ffprobe": "ffprobe"},
            "max_parallel_video_encodes": 1,
            "check_cancelled": _check_cancelled,
            "log_info": self.logs.append,
            "build_video_only_mkv_commands": lambda config, video, source, output, **kwargs: [
                ["ffmpeg", "-i", str(source), str(output)]],
            "build_dovi_record_from_rpu": lambda **kwargs: object(),
        }
        values.update(callbacks)
        self.runner = MultiVideoPipelineRunner(MultiVideoPipelineRunnerCallbacks(**values))

    def tool(self, cmd: list[str], label: str) -> str:
        self.commands.append((label, [str(arg) for arg in cmd]))
        _create_output(cmd)
        return ""

    def spec(self, **video: Any) -> VideoTrackPrepSpec:
        return VideoTrackPrepSpec(order=0, video=_video(**video), source=self.source, stream_index=0, offset_ms=0)

    def prepare(self, spec: VideoTrackPrepSpec, *, public: bool = False):
        config = EncodeConfig(source=self.source, output=self.tmp_path / "sortie.mkv", video=spec.video,
                              audio_tracks=[], copy_subtitles=False, duration_s=60.0, work_dir=self.work)
        method = self.runner.prepare_multi_video_track if public else self.runner._prepare_multi_video_track
        with patch.object(multi_video, "stream_start_offset", return_value=0.0), \
                patch.object(multi_video, "convert_p7_rpu_to_p81", return_value=False):
            return method(config=config, spec=spec, work_dir=self.work, total_tracks=2, thread_count=None,
                          signals=self.signals, run_cmd=self.tool)


def test_multi_unexpected_fel_rpu_on_a_non_first_stream_is_refused(tmp_path):
    run = _Multi(tmp_path)
    original = tmp_path / "original.rpu.bin"
    original.write_bytes(b"\x00" * 64)
    fel = FelExecution(source=Mock(), original_rpu=original, original_max_cll="1000,400", original_light_source="")
    spec = dataclasses.replace(run.spec(copy_dv=True, fel_context=fel), stream_index=1)
    with pytest.raises(EncodeError, match="RPU FEL original inattendu"):
        run.prepare(spec)
    assert not any("dovi_tool" in cmd for _, cmd in run.commands)


def test_multi_native_assembly_refuses_a_missing_dolby_vision_record(tmp_path):
    run = _Multi(tmp_path, native_assemble=lambda *args, **kwargs: None,
                 build_dovi_record_from_rpu=lambda **kwargs: None)
    with pytest.raises(EncodeError, match="DolbyVisionConfigRecord n'a pas pu être construit"):
        run.prepare(run.spec(copy_dv=True))
    assert not any(label.startswith("ffmpeg-video") for label, _ in run.commands)


def test_multi_timing_skeleton_cancellation_is_a_cancellation(tmp_path):
    run = _Multi(tmp_path)
    with patch.object(multi_video, "write_timing_skeleton", side_effect=MatroskaWriteCancelled()), \
            pytest.raises(TaskCancelledError):
        run.prepare(run.spec(copy_hdr10plus=True))


def test_multi_payload_rewrite_cancellation_is_a_cancellation(tmp_path):
    run = _Multi(tmp_path)
    with patch.object(multi_video, "FrameCountGuard", Mock()), \
            patch.object(multi_video.MatroskaHevcPayloadRewriter, "rewrite", side_effect=HevcStreamCancelled()), \
            pytest.raises(TaskCancelledError):
        run.prepare(run.spec(copy_hdr10plus=True))


def test_multi_unreadable_light_level_statistics_fail(tmp_path):
    run = _Multi(tmp_path)
    with patch.object(multi_video, "light_level_statistics_path", return_value=tmp_path / "stats.bin"), \
            patch.object(multi_video, "deferred_light_levels", return_value=True), \
            patch.object(multi_video, "read_frame_statistics",
                         side_effect=FrameStatisticsError("statistiques tronquées")), \
            pytest.raises(EncodeError, match="statistiques tronquées"):
        run.prepare(run.spec())


def test_multi_frame_count_refusal_fails_without_an_explicit_decision(tmp_path):
    run = _Multi(tmp_path)
    with patch.object(multi_video, "FrameCountGuard", _AuditRefusal), \
            pytest.raises(RuntimeError, match="^Audit frame count : RPU Dolby Vision : 3 trame"):
        run.prepare(run.spec(copy_hdr10plus=True))
    assert not any(label.startswith("hdr10plus-inject") for label, _ in run.commands)


def test_multi_frame_count_refusal_overridden_by_the_user_continues(tmp_path):
    decisions: list[str] = []
    run = _Multi(tmp_path, validation_override=lambda path, message, cancelled: decisions.append(message) or True)
    with patch.object(multi_video, "FrameCountGuard", _AuditRefusal):
        prepared, cleanup = run.prepare(run.spec(copy_hdr10plus=True))
    assert decisions == ["Audit frame count : RPU Dolby Vision : 3 trame(s) manquante(s)"]
    assert Path(prepared.path).name == "video_1.wrapped.mkv" and Path(prepared.path) in cleanup
    assert any(label == "hdr10plus-inject-1" for label, _ in run.commands)
    assert any("décision explicite" in line for line in run.outcome.progress)


def test_multi_fel_failure_retries_the_track_on_the_base_layer(tmp_path):
    run = _Multi(tmp_path)
    fel = FelExecution(source=Mock(), original_rpu=tmp_path / "absent.bin", original_max_cll="1000,400",
                       original_light_source="source")
    spec = run.spec(fel_context=fel, bake_dovi_fel=True, max_cll="1200,300")
    attempts: list[VideoEncodeSettings] = []
    attempt_dirs: list[Path] = []

    def prepare(**kwargs):
        attempts.append(kwargs["spec"].video)
        attempt_dirs.append(kwargs["work_dir"])
        if len(attempts) == 1:
            (kwargs["work_dir"] / "fel.partiel.mkv").write_bytes(b"\x00")
            raise FelError("couche d'amélioration illisible")
        output = kwargs["work_dir"] / "video_1.mkv"
        output.write_bytes(b"\x00")
        return multi_video.PreparedVideoInput(input_args=[], path=output, map_arg="0:v:0"), [output]

    with patch.object(run.runner, "_prepare_multi_video_track", side_effect=prepare):
        prepared, cleanup = run.prepare(spec, public=True)
    assert [video.fel_context is not None for video in attempts] == [True, False]
    assert attempts[1].max_cll == "1000,400" and attempts[1].bake_dovi_fel is False
    assert attempt_dirs[0] != run.work and not attempt_dirs[0].exists()  # tentative FEL : dossier propre supprimé
    assert attempt_dirs[1] == run.work and cleanup == [run.work / "video_1.mkv"]
    assert any("reprise complète sur le BL" in line for line in run.outcome.progress)


def test_multi_fel_success_moves_the_track_out_of_its_attempt_folder(tmp_path):
    run = _Multi(tmp_path)
    fel = FelExecution(source=Mock(), original_rpu=tmp_path / "absent.bin", original_max_cll="",
                       original_light_source="")

    def prepare(**kwargs):
        output = kwargs["work_dir"] / "video_1.wrapped.mkv"
        output.write_bytes(b"\x00")
        return multi_video.PreparedVideoInput(input_args=[], path=output, map_arg="0:v:0"), [output]

    with patch.object(run.runner, "_prepare_multi_video_track", side_effect=prepare):
        prepared, cleanup = run.prepare(run.spec(fel_context=fel), public=True)
    assert Path(prepared.path) == run.work / "video_1.wrapped.mkv" and Path(prepared.path).is_file()
    assert cleanup == [run.work / "video_1.wrapped.mkv"]
    assert sorted(item.name for item in run.work.iterdir()) == ["video_1.wrapped.mkv"]
    assert "Piste vidéo 1 : reconstruction FEL réussie." in run.logs


def test_multi_fel_failure_after_cancellation_is_a_cancellation(tmp_path):
    run = _Multi(tmp_path)
    fel = FelExecution(source=Mock(), original_rpu=tmp_path / "absent.bin", original_max_cll="",
                       original_light_source="")

    def prepare(**kwargs):
        run.signals.cancel()
        raise FelError("plugin arrêté")

    with patch.object(run.runner, "_prepare_multi_video_track", side_effect=prepare) as attempts, \
            pytest.raises(TaskCancelledError):
        run.prepare(run.spec(fel_context=fel), public=True)
    assert attempts.call_count == 1


# --- Encodage NVEncC direct (nvencc_execution) --------------------------------------------------


class _Nvencc:
    """Encodage NVEncC direct exécuté dans le fil du test, outils simulés, remux final simulé."""

    def __init__(self, tmp_path: Path, **video: Any) -> None:
        source = tmp_path / "source.mkv"
        source.write_bytes(b"\x00" * 1000)
        self.config = EncodeConfig(source=source, output=tmp_path / "sortie.mkv",
                                   video=_video(codec="nvencc_hevc", **video), audio_tracks=[],
                                   copy_subtitles=False, duration_s=60.0)
        self.workflow = EncodeWorkflow(ffmpeg_bin="ffmpeg", dovi_tool_bin="dovi_tool",
                                       hdr10plus_bin="hdr10plus_tool", mediainfo_bin="mediainfo",
                                       nvencc_bin="/usr/bin/NVEncC")
        self.signals = TaskSignals()
        self.outcome = _watch(self.signals)
        self.logs: list[tuple[str, str]] = []
        self.workflow.log_message.connect(lambda level, message: self.logs.append((level, message)))
        self.labels: list[str] = []
        self.nvencc_output: list[str] = []
        self.cleanup_paths: list[Path] = []

    def tool(self, cmd, *, cwd=None, label=None, progress_cb=None, progress_pct_cb=None, signals=None) -> str:
        self.labels.append(str(label))
        if label == "nvencc" and progress_cb is not None:
            for line in self.nvencc_output:
                progress_cb(line)
        return ""

    def run(self, *patches: Any) -> _Outcome:
        with patch.object(self.workflow._runner, "_run_cmd", side_effect=self.tool), \
                patch.object(self.workflow, "_finalize_ffmpeg_output", return_value=str(self.config.output)), \
                patch.object(nvencc_execution, "_stream_start_offset", return_value=0.0), \
                patch.object(nvencc_execution, "_probe_interpolation_source", return_value=None):
            for item in patches:
                item.start()
            try:
                self.workflow._run_nvencc_direct_output(self.config, self.cleanup_paths, prep_signals=self.signals)
            finally:
                for item in reversed(patches):
                    item.stop()
        return self.outcome


def _prepared_rpu(tmp_path: Path) -> Path:
    rpu = tmp_path / "prepared.rpu.bin"
    rpu.write_bytes(b"\x00" * 64)
    return rpu


def _rpu_profile_8():
    return patch("core.workflows.encode.runtime.dovi_geometry.rpu_dovi_profile", return_value=8)


def test_nvencc_external_rpu_refusal_fails_before_encoding(tmp_path):
    run = _Nvencc(tmp_path, copy_dv=True, source_rpu_path=_prepared_rpu(tmp_path))
    outcome = run.run(_rpu_profile_8(), patch.object(
        run.workflow, "_check_external_rpu",
        side_effect=FrameCountAuditError("RPU plus court que la vidéo", missing={"rpu": 3})))
    assert [message for message, _ in outcome.failed] == [
        translate_text("Audit trames : {detail}", detail="RPU plus court que la vidéo")]
    assert "nvencc" not in run.labels and not outcome.finished


def test_nvencc_accepted_missing_rpu_tolerates_exactly_those_frames(tmp_path):
    run = _Nvencc(tmp_path, copy_dv=True, source_rpu_path=_prepared_rpu(tmp_path))
    run.workflow.set_validation_override(lambda path, message, cancelled: True)
    run.nvencc_output = ["Failed to get dovi rpu for frame 97"] * 3
    outcome = run.run(_rpu_profile_8(), patch.object(
        run.workflow, "_check_external_rpu",
        side_effect=FrameCountAuditError("RPU plus court que la vidéo", missing={"rpu": 3})))
    assert outcome.finished == [str(run.config.output)] and not outcome.failed
    assert any("3 trame(s) encodée(s) sans RPU" in message and "accepté explicitement" in message
               for _, message in run.logs)


def test_nvencc_more_missing_rpu_than_accepted_is_refused(tmp_path):
    run = _Nvencc(tmp_path, copy_dv=True, source_rpu_path=_prepared_rpu(tmp_path))
    run.workflow.set_validation_override(lambda path, message, cancelled: True)
    run.nvencc_output = ["Failed to get dovi rpu for frame 96"] * 4
    outcome = run.run(_rpu_profile_8(), patch.object(
        run.workflow, "_check_external_rpu",
        side_effect=FrameCountAuditError("RPU plus court que la vidéo", missing={"rpu": 3})))
    (message, error), = outcome.failed
    assert isinstance(error, EncodeError) and "4 trame(s) encodée(s) sans RPU" in message
    assert not outcome.finished


@pytest.mark.parametrize("counted", [None, 99])
def test_nvencc_encoded_frame_count_must_match_the_rpu(tmp_path, counted):
    # Message traduit à l'exécution : la langue courante peut changer entre la collecte et le test.
    expected = (translate_text("Audit trames : compte des trames encodées illisible, couverture RPU invérifiable.")
                if counted is None else "99 trames encodées pour 100 RPU Dolby Vision")
    run = _Nvencc(tmp_path, copy_dv=True, source_rpu_path=_prepared_rpu(tmp_path))
    guard = SimpleNamespace(source_frame_count=lambda path: counted)
    outcome = run.run(_rpu_profile_8(), patch.object(run.workflow, "_check_external_rpu", return_value=100),
                      patch.object(run.workflow, "_frame_count_guard", return_value=guard))
    (message, error), = outcome.failed
    assert isinstance(error, EncodeError) and expected in message
    assert "nvencc" in run.labels and not outcome.finished


def _fel_context(tmp_path: Path) -> FelExecution:
    return FelExecution(source=Mock(), original_rpu=tmp_path / "absent.bin", original_max_cll="1000,400",
                        original_light_source="source")


def test_nvencc_fel_failure_retries_the_whole_encode_on_the_base_layer(tmp_path):
    run = _Nvencc(tmp_path, fel_context=_fel_context(tmp_path), bake_dovi_fel=True, max_cll="1200,300")
    pipe = Mock(side_effect=FelError("couche d'amélioration illisible"))
    outcome = run.run(patch.object(nvencc_execution.NvenccPipeExecutor, "run", pipe),
                      patch("core.fel.pipeline.with_fel_input", side_effect=lambda cmd, video: cmd))
    assert outcome.finished == [str(run.config.output)] and not outcome.failed
    assert pipe.call_count == 1 and run.labels.count("nvencc") == 1  # reprise directe, sans pipe FFmpeg
    assert any("reprise complète NVEncC sur le BL" in line for line in outcome.progress)
    intermediates = [path for path in run.cleanup_paths if path.suffix == ".mkv"]
    assert len(intermediates) == 1  # chemins de la tentative FEL retirés avant la reprise


def test_nvencc_fel_failure_after_cancellation_is_a_cancellation(tmp_path):
    run = _Nvencc(tmp_path, fel_context=_fel_context(tmp_path), bake_dovi_fel=True)

    def pipe(**_kwargs):
        run.signals.cancel()
        raise FelError("plugin arrêté")

    outcome = run.run(patch.object(nvencc_execution.NvenccPipeExecutor, "run", side_effect=pipe),
                      patch("core.fel.pipeline.with_fel_input", side_effect=lambda cmd, video: cmd))
    assert outcome.cancelled == 1 and not outcome.failed and not outcome.finished
    assert "nvencc" not in run.labels


def test_nvencc_fel_error_without_fel_reconstruction_fails(tmp_path):
    run = _Nvencc(tmp_path)

    def tool(cmd, *, label=None, **kwargs):
        run.labels.append(str(label))
        if label == "nvencc":
            raise FelError("erreur inattendue du plugin")
        return ""

    with patch.object(run.workflow._runner, "_run_cmd", side_effect=tool), \
            patch.object(nvencc_execution, "_stream_start_offset", return_value=0.0), \
            patch.object(nvencc_execution, "_probe_interpolation_source", return_value=None):
        run.workflow._run_nvencc_direct_output(run.config, run.cleanup_paths, prep_signals=run.signals)
    assert [message for message, _ in run.outcome.failed] == ["erreur inattendue du plugin"]
    assert run.labels.count("nvencc") == 1


def test_nvencc_cancellation_before_start_runs_nothing(tmp_path):
    run = _Nvencc(tmp_path)
    run.signals.cancel()
    outcome = run.run()
    assert outcome.cancelled == 1 and run.labels == [] and not outcome.failed


def test_static_hdr_estimate_cancelled_by_the_estimator_ends_cancelled(tmp_path):
    run = _Inject(tmp_path)
    with patch.object(run.workflow._static_hdr_estimator, "estimate_converted_static_hdr",
                      side_effect=TaskCancelledError()):
        outcome = run.run(_inject_config(tmp_path, static_hdr_metadata_analysis_request="fast"))
    assert outcome.cancelled == 1 and not outcome.failed and run.estimate_failures == []
    run.assert_work_dir_removed()


# --- Pipe décodage | NVEncC (processus réels, étages simulés en Python) -------------------------


def _stage(code: str) -> list[str]:
    return [sys.executable, "-c", code]


_FEED = "import sys; sys.stdout.buffer.write(b'\\0' * 65536); sys.stdout.flush()"
_DRAIN = "import sys; sys.stdin.buffer.read()"


def _pipe(tmp_path: Path, decode: list[str], encode: list[str], signals: TaskSignals | None = None) -> str:
    signals = signals or TaskSignals()
    return nvencc_execution.NvenccPipeExecutor().run(
        decode_cmd=decode, encode_cmd=encode, cwd=tmp_path, signals=signals,
        monitor=nvencc_execution.NvenccDoviRpuMonitor())


def test_pipe_encoder_failure_is_reported_with_its_output(tmp_path):
    encode = _stage(_DRAIN + "; print('CUDA_ERROR_OUT_OF_MEMORY', flush=True); sys.exit(1)")
    with pytest.raises(EncodeError, match="(?s)^NVEncC a échoué.*CUDA_ERROR_OUT_OF_MEMORY"):
        _pipe(tmp_path, _stage(_FEED), encode)


def test_pipe_decoder_failure_is_the_root_cause(tmp_path):
    decode = _stage("import sys; sys.stderr.write('Invalid data found when processing input\\n'); sys.exit(1)")
    with pytest.raises(EncodeError, match="(?s)^FFmpeg decode a échoué.*Invalid data found"):
        _pipe(tmp_path, decode, _stage(_DRAIN))


def test_pipe_closed_by_a_successful_encoder_is_a_normal_stop(tmp_path):
    decode = _stage("import sys; sys.stderr.write('av_interleaved_write_frame(): Broken pipe\\n'); sys.exit(1)")
    assert _pipe(tmp_path, decode, _stage(_DRAIN + "; print('encoded 240 frames', flush=True)")) == (
        "encoded 240 frames")


def test_pipe_encoder_launch_failure_stops_the_decoder(tmp_path):
    signals = TaskSignals()
    started: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    def popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        started.append(process)
        return process

    with patch.object(nvencc_execution.subprocess, "Popen", side_effect=popen), pytest.raises(OSError):
        _pipe(tmp_path, _stage("import time; time.sleep(30)"), [str(tmp_path / "NVEncC-absent")], signals)
    (decoder,) = started
    assert decoder.wait(timeout=5) is not None
    assert signals._active_procs == []


def test_pipe_cancellation_stops_both_stages(tmp_path):
    signals = TaskSignals()
    timer = threading.Timer(0.3, signals.cancel)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(TaskCancelledError):
            _pipe(tmp_path, _stage("import time; time.sleep(30)"), _stage(_DRAIN), signals)
    finally:
        timer.cancel()
    assert time.monotonic() - started < 10
    assert signals._active_procs == []
