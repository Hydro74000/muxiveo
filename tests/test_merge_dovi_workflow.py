from __future__ import annotations

from pathlib import Path

import pytest

from core.workflows.merge_dovi import (
    DoviProfile,
    HDRFlags,
    MergeDoviWorkflow,
    StaticHdrMetadata,
    WorkflowError,
    WorkflowStep,
    _WorkflowPaths,
    _format_master_display_from_mediainfo,
    _format_max_cll_from_mediainfo,
)
from core.dovi_profile_detector import DoviSubProfile


def _paths(tmp_path: Path, film1: Path, basename: str = "out") -> _WorkflowPaths:
    work_dir = tmp_path / "work"
    output_dir = tmp_path / "output"
    work_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    return _WorkflowPaths.from_config(work_dir, output_dir, film1, basename)


def test_required_tools_uses_ffmpeg_stack() -> None:
    assert MergeDoviWorkflow.required_tools() == [
        "mediainfo",
        "ffmpeg",
        "ffprobe",
        "dovi_tool",
        "hdr10plus_tool",
    ]


def test_extract_hevc_uses_ffmpeg_command(tmp_path: Path) -> None:
    wf = MergeDoviWorkflow(ffmpeg_bin="ffmpeg")
    source = tmp_path / "src.mkv"
    dest = tmp_path / "video.hevc"
    source.write_bytes(b"x")

    calls: list[list[str]] = []

    def _fake_run_raw(cmd: list[str]) -> str:
        calls.append(cmd)
        return ""

    wf._run_raw = _fake_run_raw  # type: ignore[method-assign]

    msg = wf._extract_hevc(source, dest, lambda _: None)

    assert msg == "HEVC extrait → video.hevc"
    assert calls == [[
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-c:v",
        "copy",
        "-bsf:v",
        "hevc_mp4toannexb",
        "-an",
        "-sn",
        "-dn",
        "-f",
        "hevc",
        str(dest),
    ]]


def test_step_remux_wraps_and_rebuilds_with_ffmpeg(tmp_path: Path) -> None:
    film1 = tmp_path / "film1.mkv"
    film1.write_bytes(b"film1")
    paths = _paths(tmp_path, film1)
    flags = HDRFlags(has_dovi=False, has_hdr10plus=True)
    paths.film1_final.write_bytes(b"hevc")

    wf = MergeDoviWorkflow(ffmpeg_bin="ffmpeg")
    wf._source_video_fps_expr = lambda _src: "24000/1001"  # type: ignore[method-assign]

    calls: list[list[str]] = []

    def _fake_run_cmd(cmd: list[str], _step: WorkflowStep) -> str:
        calls.append(cmd)
        out = Path(cmd[-1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"ok")
        return ""

    wf._run_cmd = _fake_run_cmd  # type: ignore[method-assign]

    wf._step_remux(film1, paths, flags)

    assert len(calls) == 2
    wrap_cmd, final_cmd = calls

    assert wrap_cmd[:4] == ["ffmpeg", "-hide_banner", "-y", "-f"]
    assert "-framerate" in wrap_cmd
    assert "-bsf:v" in wrap_cmd
    assert str(paths.film1_wrapped_video) == wrap_cmd[-1]

    assert final_cmd[:3] == ["ffmpeg", "-hide_banner", "-y"]
    assert "-map" in final_cmd
    assert "0:v:0" in final_cmd
    assert "1:a?" in final_cmd
    assert "1:s?" in final_cmd
    assert "1:t?" in final_cmd
    assert "1:d?" in final_cmd
    assert "-map_metadata" in final_cmd
    assert final_cmd[final_cmd.index("-map_metadata") + 1] == "1"
    assert "-map_chapters" in final_cmd
    assert final_cmd[final_cmd.index("-map_chapters") + 1] == "1"
    assert str(paths.output_mkv) == final_cmd[-1]


def test_verify_raises_when_injected_framecount_is_outside_tolerance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit FrameCountGuard : encoded != source → abort."""
    film1 = tmp_path / "film1.mkv"
    film1.write_bytes(b"film1")
    paths = _paths(tmp_path, film1)
    paths.film1_final.write_bytes(b"hevc")  # le step exige que le final existe
    flags = HDRFlags(has_dovi=False, has_hdr10plus=True)

    # Frame counts contrôlés via le lecteur mediainfo de FrameCountGuard.
    def _fake_run(cmd, capture_output=True, check=False, **kw):
        class _R:
            returncode = 0
            stderr = ""
            stdout = ""
        binary = Path(cmd[0]).name
        if binary == "mediainfo":
            target = Path(cmd[-1])
            _R.stdout = "1000" if target == film1 else "1005"
        return _R()

    monkeypatch.setattr("core.workflows.encode.runtime.frame_count_guard.subprocess.run", _fake_run)

    wf = MergeDoviWorkflow()
    with pytest.raises(WorkflowError, match="frame count"):
        wf._step_verify(film1, paths, flags)


def test_verify_passes_when_frame_counts_align(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit FrameCountGuard : tout aligné → pas d'exception."""
    film1 = tmp_path / "film1.mkv"
    film1.write_bytes(b"film1")
    paths = _paths(tmp_path, film1)
    paths.film1_with_dovi.write_bytes(b"hevc")
    paths.film2_rpu.write_bytes(b"rpu")
    flags = HDRFlags(has_dovi=True, has_hdr10plus=False)

    def _fake_run(cmd, capture_output=True, check=False, **kw):
        class _R:
            returncode = 0
            stderr = ""
            stdout = ""
        binary = Path(cmd[0]).name
        if binary == "mediainfo":
            _R.stdout = "1000"
        elif binary == "dovi_tool":
            _R.stdout = "Frames: 1000\n"
        return _R()

    monkeypatch.setattr("core.workflows.encode.runtime.frame_count_guard.subprocess.run", _fake_run)

    wf = MergeDoviWorkflow()
    wf._step_verify(film1, paths, flags)  # ne lève pas


def test_step_inject_dovi_uses_film2_p8_when_converted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Quand le router a converti P7→P8.1, _step_inject_dovi doit s'exécuter
    sur la chaîne d'inject normale (film2_rpu déjà extrait depuis film2_p8)."""
    film1 = tmp_path / "film1.mkv"
    film1.write_bytes(b"film1")
    paths = _paths(tmp_path, film1)
    paths.film1_hevc.write_bytes(b"hevc")  # film1 extrait
    paths.film2_rpu.write_bytes(b"rpu")
    flags = HDRFlags(has_dovi=True, has_hdr10plus=False)

    wf = MergeDoviWorkflow(dovi_tool_bin="dovi_tool")
    calls: list[list[str]] = []
    wf._run_cmd = lambda cmd, step: (calls.append(cmd), Path(cmd[cmd.index("-o") + 1]).write_bytes(b"x"), "")[2]  # type: ignore[method-assign]

    wf._step_inject_dovi(paths, flags, DoviProfile.P8_1)

    inject_cmd = calls[0]
    assert "inject-rpu" in inject_cmd
    assert inject_cmd[inject_cmd.index("-m") + 1] == "2"


def test_inject_static_hdr_skips_when_film1_complete(tmp_path: Path) -> None:
    """Si Film 1 a déjà ses SEI 137/144, l'étape ne fait rien."""
    film1 = tmp_path / "film1.mkv"
    film1.write_bytes(b"film1")
    paths = _paths(tmp_path, film1)
    flags = HDRFlags(has_dovi=False, has_hdr10plus=True)
    static1 = StaticHdrMetadata(
        master_display="G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(10000000,50)",
        max_cll="1000,400",
    )
    static2 = StaticHdrMetadata()

    wf = MergeDoviWorkflow()
    applied = wf._step_inject_static_hdr(paths, flags, static1, static2)

    assert applied is False
    assert not paths.film1_with_static_hdr.exists()


def test_inject_static_hdr_uses_film2_when_film1_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Si Film 1 n'a pas les SEI mais Film 2 oui, on injecte depuis Film 2."""
    film1 = tmp_path / "film1.mkv"
    film1.write_bytes(b"film1")
    paths = _paths(tmp_path, film1)
    paths.film1_final.write_bytes(b"hevc-final")
    flags = HDRFlags(has_dovi=True, has_hdr10plus=True)
    static1 = StaticHdrMetadata()
    static2 = StaticHdrMetadata(
        master_display="G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(10000000,50)",
        max_cll="1000,400",
    )

    captured: dict = {}

    def _fake_inject(src, dst, *, master_display, max_cll):
        captured["src"] = src
        captured["dst"] = dst
        captured["master_display"] = master_display
        captured["max_cll"] = max_cll
        from core.workflows.hevc_static_hdr_metadata import StaticHdrSeiInjectionResult
        dst.write_bytes(b"patched")
        return StaticHdrSeiInjectionResult(
            access_units=10, targeted_access_units=2,
            injected_access_units=2, preserved_access_units=0,
        )

    monkeypatch.setattr(
        "core.workflows.merge_dovi.inject_static_hdr_sei_file", _fake_inject,
    )

    wf = MergeDoviWorkflow()
    applied = wf._step_inject_static_hdr(paths, flags, static1, static2)

    assert applied is True
    assert captured["master_display"] == static2.master_display
    assert captured["max_cll"] == static2.max_cll
    assert captured["src"] == paths.film1_final
    assert captured["dst"] == paths.film1_with_static_hdr


def test_format_master_display_from_mediainfo_bt2020() -> None:
    track = {
        "MasteringDisplay_ColorPrimaries": "BT.2020",
        "MasteringDisplay_Luminance": "min: 0.0050 cd/m^2, max: 1000 cd/m^2",
    }
    expected = "G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(10000000,50)"
    assert _format_master_display_from_mediainfo(track) == expected


def test_format_master_display_from_mediainfo_returns_empty_on_unknown_primaries() -> None:
    track = {
        "MasteringDisplay_ColorPrimaries": "Unknown",
        "MasteringDisplay_Luminance": "min: 0.0050 cd/m^2, max: 1000 cd/m^2",
    }
    assert _format_master_display_from_mediainfo(track) == ""


def test_format_max_cll_from_mediainfo() -> None:
    track = {"MaxCLL": "1000 cd/m2", "MaxFALL": "400 cd/m2"}
    assert _format_max_cll_from_mediainfo(track) == "1000,400"


def test_cleanup_removes_wrapped_video(tmp_path: Path) -> None:
    film1 = tmp_path / "film1.mkv"
    film1.write_bytes(b"film1")
    paths = _paths(tmp_path, film1)

    intermediates = [
        paths.film1_hevc,
        paths.film2_hevc,
        paths.film2_hevc_p8,
        paths.film2_rpu,
        paths.film2_hdr10plus,
        paths.film1_with_dovi,
        paths.film1_final,
        paths.film1_with_static_hdr,
        paths.film1_wrapped_video,
    ]
    for p in intermediates:
        p.write_bytes(b"x")

    wf = MergeDoviWorkflow()
    wf._step_cleanup(paths)

    for p in intermediates:
        assert not p.exists()
