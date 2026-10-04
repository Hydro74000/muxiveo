"""Sélection du moteur, compatibilité des profils et pipelines communs."""
from __future__ import annotations

import json
import hashlib
import zipfile
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import pytest

from core.pipeline_command import command_stages
from core.workflows.encode import EncodeConfig, EncodeError, EncodePreset, EncodeWorkflow, FrameInterpolationSettings, VideoEncodeSettings
from core.workflows.encode.interpolation import InterpolationSource, build_interpolation_stage, mvtools_thread_count, parse_interpolation_progress, frame_repeats
from tests.test_encode_interpolation import _builder
from core.workflows.encode.runtime_helpers import VideoPreparationResourcePolicy
from core.native_mvtools import bundle_errors, extract_bundle
from core.version import MUXIVEO_MVTOOLS_VERSION


def test_old_profile_and_independent_parameters():
    old = FrameInterpolationSettings.from_value({"enabled": True, "quality": "light", "tta": 4, "gpu": 1})
    assert old.backend == "rife" and old.mvtools_mode == "standard"
    selected = replace(old, backend="mvtools", mvtools_mode="uhd")
    restored = EncodePreset(**json.loads(json.dumps(EncodePreset(name="x", interpolation=selected).to_json_dict())))
    assert restored.interpolation == selected
    assert replace(restored.interpolation, backend="rife") == replace(old, mvtools_mode="uhd")


@pytest.mark.parametrize("mode,budget,expected", [("standard", 1, 1), ("standard", 6, 3), ("standard", 64, 4), ("uhd", 6, 6), ("uhd", 1024, 256)])
def test_thread_budget(mode, budget, expected):
    assert mvtools_thread_count(mode, budget) == expected


def test_stage_and_progress():
    settings = FrameInterpolationSettings(enabled=True, backend="mvtools", mvtools_mode="uhd", target_fps="60000/1001", tta=8)
    args = build_interpolation_stage(settings, InterpolationSource("bt2020nc", "limited", "topleft"),
                                     mvtools_bin="/opt/muxiveo-mvtools", thread_budget=5)
    assert args[0] == "/opt/muxiveo-mvtools" and args[args.index("--fps") + 1] == "60000/1001"
    assert args[args.index("--threads") + 1] == "5" and "--tta" not in args and "--factor" not in args
    result = parse_interpolation_progress("[muxiveo-mvtools] progress in=8 out=21 scenes=0 static=0 fps=4.2")
    assert result is not None and result.frames_in == 8 and result.frames_out == 21
    with pytest.raises(EncodeError, match="introuvable"):
        build_interpolation_stage(settings, InterpolationSource(), rife_bin="available-rife")


def test_decimal_target_rate_is_sent_as_exact_fraction():
    settings = FrameInterpolationSettings(enabled=True, backend="mvtools", target_fps="59.94")
    args = build_interpolation_stage(settings, InterpolationSource(), mvtools_bin="/opt/muxiveo-mvtools")
    assert args[args.index("--fps") + 1] == "2997/50"


def test_ffmpeg_filters_and_per_job_budget(tmp_path):
    video = VideoEncodeSettings(codec="libx265", interpolation=FrameInterpolationSettings(enabled=True, backend="mvtools", mvtools_mode="uhd"))
    config = EncodeConfig(source=tmp_path / "s.mkv", output=tmp_path / "o.mkv", video=video)
    builder = _builder()
    builder._cb = replace(builder._cb, mvtools_bin="/opt/muxiveo-mvtools")
    command = builder.build_video_only_mkv_commands(config, video, config.source, tmp_path / "v.mkv", thread_count=3)[0]
    decode, interpolate, encode = command_stages(command)
    assert interpolate[0] == "/opt/muxiveo-mvtools" and interpolate[interpolate.index("--threads") + 1] == "3"
    assert decode[-1] == "-" and encode[encode.index("-i") + 1] == "pipe:0"


def test_nvencc_stage_keeps_color_and_offset(tmp_path, monkeypatch):
    workflow = EncodeWorkflow(ffmpeg_bin="ffmpeg", mvtools_bin="/opt/muxiveo-mvtools", ffmpeg_threads=8)
    monkeypatch.setattr(workflow, "_interpolation_source", lambda *_: InterpolationSource("bt2020nc", "limited", "topleft", 0.04))
    video = VideoEncodeSettings(codec="nvencc_hevc", interpolation=FrameInterpolationSettings(enabled=True, backend="mvtools"))
    stages = command_stages(workflow._wrap_decode_with_interpolation(["ffmpeg", "-i", "s.mkv", "-f", "yuv4mpegpipe", "-"], video, tmp_path / "s.mkv"))
    assert stages[-1][0] == "/opt/muxiveo-mvtools" and "--matrix" in stages[-1]
    assert stages[-1][stages[-1].index("--threads") + 1] == "4"


def test_hdr_counts_share_exact_cadence():
    video = VideoEncodeSettings(codec="libx265", interpolation=FrameInterpolationSettings(enabled=True, backend="mvtools", target_fps="60"))
    ratio = video.frame_ratio("24000/1001")
    assert ratio == Fraction(1001, 400)
    assert sum(frame_repeats(i, ratio) for i in range(19)) == 48


def test_parallel_scheduler_reserves_measured_cpu_memory():
    policy = VideoPreparationResourcePolicy(ffmpeg_threads=4)
    plain = VideoEncodeSettings(codec="libx265")
    standard = replace(plain, interpolation=FrameInterpolationSettings(enabled=True, backend="mvtools"))
    uhd = replace(standard, interpolation=replace(standard.interpolation, mvtools_mode="uhd"))
    base = policy.estimated_ram_bytes(plain, source_size=1)
    assert policy.estimated_ram_bytes(standard, source_size=1) >= base + 2 * 2**30
    assert policy.estimated_ram_bytes(uhd, source_size=1) >= base + 6 * 2**30


def test_archive_installation_rejects_missing_and_changed_libraries(tmp_path):
    files = {"muxiveo-mvtools": b"native", **{f"mvtools-runtime/{name}.so": name.encode() for name in
                                             ("libvapoursynth", "libvapoursynthfilters", "mvtools")}}
    manifest = {"version": MUXIVEO_MVTOOLS_VERSION, "files": {name: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                                               for name, data in files.items()}}
    archive = tmp_path / "bundle.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for name, data in files.items():
            zf.writestr("package/" + name, data)
        zf.writestr("package/mvtools-runtime/manifest.json", json.dumps(manifest))
    binary = extract_bundle(archive, tmp_path / "installed")
    assert bundle_errors(binary, render=False) == []
    library = binary.parent / "mvtools-runtime/mvtools.so"
    library.write_bytes(b"changed")
    assert any("empreinte" in e for e in bundle_errors(binary, render=False))
    library.unlink()
    assert any("absent" in e for e in bundle_errors(binary, render=False))


def test_archive_cannot_write_outside_installation(tmp_path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("package/../../outside", b"bad")
    with pytest.raises(ValueError, match="chemin"):
        extract_bundle(archive, tmp_path / "installed")
    assert not (tmp_path / "outside").exists()


@pytest.mark.parametrize("payload", [[], None, {"version": MUXIVEO_MVTOOLS_VERSION, "files": []},
                                     {"version": MUXIVEO_MVTOOLS_VERSION, "files": {"muxiveo-mvtools": None}}])
def test_malformed_manifest_reports_error(tmp_path, payload):
    runtime = tmp_path / "mvtools-runtime"
    runtime.mkdir()
    (runtime / "manifest.json").write_text(json.dumps(payload))
    assert bundle_errors(tmp_path / "muxiveo-mvtools", render=False)


def test_extraction_refuses_existing_symlink_destination(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    target = tmp_path / "destination"
    try:
        target.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("création du lien indisponible")
    with pytest.raises(ValueError, match="destination"):
        extract_bundle(tmp_path / "unused.zip", target)
    assert not list(outside.iterdir())


def test_manifest_can_be_regenerated(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("mvtools_build", Path(__file__).parents[1] / "native/muxiveo-mvtools/scripts/build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for filename in ("muxiveo-mvtools", "mvtools-runtime/libvapoursynth.so", "mvtools-runtime/libvapoursynthfilters.so", "mvtools-runtime/mvtools.so"):
        path = tmp_path / filename
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(filename.encode())
    for _ in range(2):
        module.write_manifest(tmp_path, {}, {}, MUXIVEO_MVTOOLS_VERSION)
        assert not bundle_errors(tmp_path / "muxiveo-mvtools", render=False)
    manifest = json.loads((tmp_path / "mvtools-runtime/manifest.json").read_text())
    assert "mvtools-runtime/manifest.json" not in manifest["files"]


def test_runtime_cache_detects_changed_library(tmp_path, monkeypatch):
    import subprocess
    from core.workflows.encode.interpolation import mvtools_runtime_info
    files = {"muxiveo-mvtools": b"native", **{f"mvtools-runtime/{name}.so": name.encode() for name in
                                             ("libvapoursynth", "libvapoursynthfilters", "mvtools")}}
    for name, data in files.items():
        path = tmp_path / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(data)
    manifest = {"version": MUXIVEO_MVTOOLS_VERSION, "files": {name: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                                               for name, data in files.items()}}
    (tmp_path / "mvtools-runtime/manifest.json").write_text(json.dumps(manifest))
    calls = []

    def selftest(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, json.dumps({"ok": True, "version": MUXIVEO_MVTOOLS_VERSION}), "")

    monkeypatch.setattr(subprocess, "run", selftest)
    binary = str(tmp_path / "muxiveo-mvtools")
    assert mvtools_runtime_info(binary)["ok"] is True
    assert mvtools_runtime_info(binary)["ok"] is True
    assert len(calls) == 1
    library = tmp_path / "mvtools-runtime/mvtools.so"
    library.write_bytes(b"damaged")
    assert mvtools_runtime_info(binary) is None
    assert len(calls) == 1
