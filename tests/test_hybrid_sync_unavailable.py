"""
tests/test_hybrid_sync_unavailable.py — Hybrid : synchro impossible = épisode en échec, la saison continue (N9,
décision DB ; plan de remédiation 5.2.0, lot 9).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cli.logging import Logger
from cli.options import CommonOptions
from core.service import ExecutionContext, ListSink
from core.service.ops import batch as batch_ops
from core.service.ops import hybrid as hybrid_ops
from core.workflows.hybrid_matrix import (
    HybridRecipe,
    MatrixEpisode,
    MatrixSource,
    SourceRole,
    SyncUnavailableError,
    prepare_matrix_episode,
)
from core.workflows.remux_models import TrackEntry
from tests.test_hybrid_matrix import _selected_plan


def _prepare(tmp_path: Path, monkeypatch, master_tracks: list[TrackEntry], donors: list[list[TrackEntry]],
             *, calibrate: bool = True) -> Any:
    from core.workflows.audio_sync_scan import AudioSyncScanner
    from core.workflows.subtitle_sync_scan import SubtitleSyncScanner
    from core.workflows.sync_calibration import SyncCalibration

    master = tmp_path / "master.mkv"
    sources = [(MatrixSource(f"d{i}", tmp_path / f"donor{i}.mkv", SourceRole.DONOR), tmp_path / f"donor{i}.mkv")
               for i in range(len(donors))]
    episode = MatrixEpisode(master_file=master, donor_files=sources)

    def build(job, config, options, logger, *, select_tracks, **_kwargs):
        return _selected_plan(job, select_tracks, (master, master_tracks),
                              *((path, tracks) for (_s, path), tracks in zip(sources, donors)))

    monkeypatch.setattr("core.jobs.remux_config.build_remux_config", build)
    monkeypatch.setattr(AudioSyncScanner, "scan", lambda self, *a, **k: SyncCalibration.linear(40))
    monkeypatch.setattr(SubtitleSyncScanner, "scan", lambda self, *a, **k: SyncCalibration.linear(40))
    return prepare_matrix_episode(episode, HybridRecipe(sync_strategy="auto"), tmp_path / "out",
                                  SimpleNamespace(tool_ffmpeg="ffmpeg", tool_ffprobe="ffprobe"),
                                  CommonOptions(sync_mode="physical"), Logger(), calibrate=calibrate)


_VIDEO = TrackEntry(0, "video", "HEVC", "", "", "", file_id="src0")
_AUDIO_FR = TrackEntry(0, "audio", "AC3", "", "fra", "", file_id="src1")


def test_master_without_audio_and_audio_donor_is_refused(tmp_path, monkeypatch):
    """Reproduction du contre-audit : aucun décalage nul en silence."""
    with pytest.raises(SyncUnavailableError) as caught:
        _prepare(tmp_path, monkeypatch, [_VIDEO], [[_AUDIO_FR]])
    assert caught.value.donors and caught.value.donors[0][0] == 1


def test_master_with_subtitles_only_and_audio_only_donor_is_refused(tmp_path, monkeypatch):
    master = [_VIDEO, TrackEntry(1, "subtitle", "SUBRIP", "", "eng", "", file_id="src0")]
    with pytest.raises(SyncUnavailableError):
        _prepare(tmp_path, monkeypatch, master, [[_AUDIO_FR]])


def test_one_of_two_donors_unsyncable_names_that_donor(tmp_path, monkeypatch):
    master = [_VIDEO, TrackEntry(1, "subtitle", "SUBRIP", "", "eng", "", file_id="src0")]
    subtitle_donor = [TrackEntry(0, "subtitle", "SUBRIP", "", "fra", "", file_id="src1")]
    audio_donor = [TrackEntry(0, "audio", "AC3", "", "spa", "", file_id="src2")]
    with pytest.raises(SyncUnavailableError) as caught:
        _prepare(tmp_path, monkeypatch, master, [subtitle_donor, audio_donor])
    assert [index for index, _reason in caught.value.donors] == [2]


def test_deferred_preparation_never_reports_unavailable(tmp_path, monkeypatch):
    """Non-régression : préparation sans calibration (Hybrid Studio), vérification différée."""
    result, _calibration = _prepare(tmp_path, monkeypatch, [_VIDEO], [[_AUDIO_FR]], calibrate=False)
    assert result is not None


def test_normal_sync_is_unchanged(tmp_path, monkeypatch):
    """Non-régression : master avec audio, donneur audio → calé."""
    master = [_VIDEO, TrackEntry(1, "audio", "AAC", "", "eng", "", file_id="src0")]
    result, calibration = _prepare(tmp_path, monkeypatch, master, [[_AUDIO_FR]])
    assert calibration is not None and calibration.segments[0].shift_ms == 40


# ── Service : la saison continue ─────────────────────────────────────────────


def _items(tmp_path: Path, refused: tuple[int, ...]) -> list[Any]:
    return [SimpleNamespace(error=SyncUnavailableError(f"E{i}", [(1, "aucune piste audio du master")])
                            if i in refused else None, calibration=None,
                            remux_config=None if i in refused else SimpleNamespace(output=tmp_path / f"E{i}.mkv"),
                            episode=SimpleNamespace(display_name=f"E{i}", master_file=tmp_path / f"m{i}.mkv",
                                                    donor_files=[]))
            for i in range(3)]


def test_season_continues_past_a_refused_episode_without_continue_on_error(tmp_path, monkeypatch):
    spec = SimpleNamespace(common=None, output_template=None)
    items = _items(tmp_path, refused=(1,))
    monkeypatch.setattr(hybrid_ops, "_prepare", lambda request, ctx, cache=None: (spec, items))
    monkeypatch.setattr(hybrid_ops, "check_outputs", lambda *a, **k: None)
    ran: list[Any] = []
    monkeypatch.setattr(batch_ops._Children, "run", lambda self, *a, **k: ran.append(a[-1].output) or 0)
    sink = ListSink()
    ctx = ExecutionContext.create(SimpleNamespace(), sink, base_dir=tmp_path)
    result = hybrid_ops.hybrid_run.handler(hybrid_ops.HybridRequest.from_dict({"output_dir": str(tmp_path)}), ctx)
    assert ran == [tmp_path / "E0.mkv", tmp_path / "E2.mkv"]  # épisodes 1 et 3 produits
    assert [entry["status"] for entry in result.episodes] == ["ok", "failed", "ok"]
    assert result.status == "partial" and result.sync_unavailable[0]["episode"] == "E1"
    recaps = [event for event in sink.events
              if event.type.value == "warning" and event.data.get("code") == "sync_unavailable"
              and "episodes" in event.data]
    assert len(recaps) == 1 and recaps[0].data["episodes"] == ["E1"]  # récapitulatif émis une fois


def test_plan_reports_the_same_refusals(tmp_path, monkeypatch):
    spec = SimpleNamespace(common=None, output_template=None, recipe=HybridRecipe())
    items = _items(tmp_path, refused=(1,))
    for item in items:
        item.describe = lambda item=item: {"episode": item.episode.display_name}
    monkeypatch.setattr(hybrid_ops, "_prepare", lambda request, ctx, cache=None: (spec, items))
    ctx = ExecutionContext.create(SimpleNamespace(), ListSink(), base_dir=tmp_path)
    result = hybrid_ops.hybrid_plan.handler(hybrid_ops.HybridPlanRequest.from_dict({"output_dir": str(tmp_path)}),
                                            ctx)
    assert result.sync_unavailable[0]["episode"] == "E1"


# ── Hybrid Studio ───────────────────────────────────────────────────────────


@pytest.fixture
def studio(qt_app, monkeypatch):
    from core.config import AppConfig
    from tests.test_hybrid_sync_ui import _wait
    from ui.panels.hybrid_studio import HybridStudio
    from ui.panels.remux_panel.widgets.sync_studio_dialog import SyncStudioDialog

    monkeypatch.setattr(SyncStudioDialog, "_load_segment_audio_async", lambda *_a: None)
    panel = HybridStudio(AppConfig())
    yield panel
    panel.close()
    _wait(qt_app, lambda: panel._shutdown.done.is_set())
    qt_app.processEvents()


def test_studio_refused_row_never_blocks_nor_is_reprepared(studio, tmp_path, monkeypatch):
    import copy

    from core.workflows.hybrid_matrix import MatrixEpisode
    from tests.test_hybrid_sync_ui import _job

    studio.table.setRowCount(3)
    studio.matrix_episodes = [MatrixEpisode(season=1, episode=i + 1, master_file=tmp_path / f"m{i}.mkv",
                                            donor_files=[(None, tmp_path / f"d{i}.mkv")]) for i in range(3)]
    studio.jobs = {0: _job(tmp_path), 2: copy.deepcopy(_job(tmp_path))}
    generation = studio._prepare_generation
    monkeypatch.setattr(studio, "_row_returned", lambda generation_, row: True)
    error = SyncUnavailableError("S01E02", [(1, "aucune piste audio du master")])
    studio._on_sync_refused(generation, 1, error)
    studio._on_episode_failed(generation, 1, str(error))
    assert 1 not in studio.jobs and 1 in studio._sync_refused_rows
    assert studio._complete_missing_plans() is False  # ligne refusée jamais reprise
    launched: list[bool] = []
    monkeypatch.setattr(studio, "run", lambda: launched.append(True))
    studio._run_after_prepare = True
    studio.set_busy(True)
    studio._finish_preparation()
    assert launched == [True]  # épisodes 1 et 3 lancés, jamais « préparation impossible »


def test_studio_recap_emitted_once_after_the_operation(studio):
    recaps: list[str] = []
    studio.sync_refusals_ready.connect(recaps.append)
    studio._sync_refused_rows = {1: SyncUnavailableError("S01E02", [(1, "aucune piste audio du master")])}
    studio._operation_active = True
    studio._sync_recap_shown = False
    studio._finish_operation("finished", "fin")
    studio._operation_active = True
    studio._finish_operation("finished", "fin")
    assert len(recaps) == 1 and "S01E02" in recaps[0]


def test_main_window_defers_the_recap_while_a_panel_works(monkeypatch):
    from unittest.mock import Mock

    from ui import main_window as main_window_module

    shown: list[str] = []
    deferred = Mock()
    monkeypatch.setattr(main_window_module.QTimer, "singleShot", deferred)
    monkeypatch.setattr(main_window_module.QMessageBox, "warning", lambda _p, _t, text: shown.append(text))
    monkeypatch.setattr(main_window_module.QApplication, "activeModalWidget", staticmethod(lambda: None))
    busy = SimpleNamespace(_running=True)
    main_window_module.MainWindow._show_sync_refusals(busy, "récap")  # type: ignore[arg-type]
    assert shown == [] and deferred.called  # différée tant qu'un panneau travaille
    idle = SimpleNamespace(_running=False)
    main_window_module.MainWindow._show_sync_refusals(idle, "récap")  # type: ignore[arg-type]
    assert shown == ["récap"]
