"""Export exact-job puis reconstruction (CLI, session du panneau) : état des pistes identique, drapeau Matroska
``FlagEnabled`` distinct de la sélection, originaux et variantes ; ancien export sans ``flags.enabled``."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from tests.integration._synth import make_av_container

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe requis",
)


def _app_config(tmp_path: Path):
    from core.config import AppConfig

    config = AppConfig()
    config.config_dir = tmp_path
    config.output_dir = tmp_path
    config.tool_ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    config.tool_ffprobe = shutil.which("ffprobe") or "ffprobe"
    config.tool_mediainfo = shutil.which("mediainfo") or "mediainfo"
    return config


def _build(job: dict, app_config):
    from cli.logging import Logger
    from core.jobs.options import CommonOptions
    from core.jobs.remux_config import build_remux_config

    return build_remux_config(json.loads(json.dumps(job)), app_config, CommonOptions(), Logger())


def _states(config) -> list[tuple]:
    from core.profiles.selectors import FLAG_NAMES

    return sorted(
        (track.track_type, int(track.mkv_tid), bool(track.is_new), track.codec, track.title, track.language,
         bool(track.enabled), *(bool(getattr(track, f"flag_{name}")) for name in FLAG_NAMES))
        for source in config.sources for track in source.tracks
    )


def _edited_config(tmp_path: Path, app_config, *, source_flag: bool, variant_flag: bool):
    """Source réelle ; audio : drapeau ``source_flag`` (sélection gardée), variante AC3 au drapeau ``variant_flag``."""
    from core.workflows.remux_models import clone_track_entry

    src = tmp_path / "src.mkv"
    make_av_container(src, duration=1.0)
    config = _build({"version": 1, "sources": [str(src)], "output": str(tmp_path / "out.mkv")}, app_config)
    audio = next(track for track in config.sources[0].tracks if track.track_type == "audio")
    audio.flag_enabled = source_flag
    variant = clone_track_entry(audio)
    variant.codec = "AC3"
    variant.flag_enabled = variant_flag
    variant.flag_commentary = True
    config.sources[0].tracks.append(variant)
    config.track_order = list(config.track_order) + [(0, variant.mkv_tid, variant.entry_id)]
    assert audio.enabled and variant.enabled  # sélection distincte du drapeau
    return config, src


@pytest.mark.parametrize("source_flag, variant_flag", [(False, False), (False, True), (True, False)])
def test_exact_job_round_trip_keeps_the_mkv_enabled_flag(qt_app, tmp_path, source_flag, variant_flag):
    from core.profiles.selectors import remux_config_to_exact_job

    app_config = _app_config(tmp_path)
    config, _src = _edited_config(tmp_path, app_config, source_flag=source_flag, variant_flag=variant_flag)
    for identity in (False, True):  # export exact-job, session (sélecteurs d'identité)
        job = remux_config_to_exact_job(config, identity_selectors=identity)
        variants = job["audio_variants"]
        assert [spec["flags"]["enabled"] for spec in variants] == [variant_flag]
        assert [spec["flags"]["enabled"] for spec in job["tracks"] if spec["selector"]["type"] == "audio"] == [
            source_flag]
        assert _states(_build(job, app_config)) == _states(config)


def test_legacy_export_without_mkv_enabled_flag_keeps_the_inspected_value(qt_app, tmp_path):
    from core.profiles.selectors import remux_config_to_exact_job

    app_config = _app_config(tmp_path)
    config, _src = _edited_config(tmp_path, app_config, source_flag=False, variant_flag=False)
    job = remux_config_to_exact_job(config)
    for spec in [*job["tracks"], *job["audio_variants"]]:
        spec.get("flags", {}).pop("enabled", None)  # export antérieur
    restored = _build(job, app_config)
    assert all(track.flag_enabled for source in restored.sources for track in source.tracks)  # inspection


def test_panel_session_round_trip_keeps_the_mkv_enabled_flag(qt_app, tmp_path):
    from core.inspector import FileInspector
    from ui.panels.remux_panel.functions.workflow import _session_job, restore
    from ui.panels.remux_panel.panel import RemuxPanel

    app_config = _app_config(tmp_path)
    config, src = _edited_config(tmp_path, app_config, source_flag=False, variant_flag=True)
    infos = [FileInspector(ffprobe_bin=str(app_config.tool_ffprobe),
                           mediainfo_bin=str(app_config.tool_mediainfo)).inspect(src)]
    panel = RemuxPanel(app_config)
    restore(panel, config, infos)
    before = panel.collect_config()
    assert before is not None
    job = _session_job(panel)  # charge utile de l'autosauvegarde
    session = job.get("_muxiveo_session")
    reloaded = RemuxPanel(app_config)
    restore(reloaded, _build(job, app_config), infos, session if isinstance(session, dict) else None)
    after = reloaded.collect_config()
    assert after is not None
    assert _states(after) == _states(before)
    flags = {(track.is_new, track.flag_enabled) for source in after.sources for track in source.tracks
             if track.track_type == "audio"}
    assert flags == {(False, False), (True, True)}
    panel.close()
    reloaded.close()
