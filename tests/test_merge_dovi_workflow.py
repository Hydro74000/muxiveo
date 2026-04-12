from __future__ import annotations

from pathlib import Path

import pytest

from core.workflows.merge_dovi import HDRFlags, MergeDoviWorkflow, WorkflowError, WorkflowStep, _WorkflowPaths


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


def test_verify_raises_when_injected_framecount_is_outside_tolerance(tmp_path: Path) -> None:
    film1 = tmp_path / "film1.mkv"
    film1.write_bytes(b"film1")
    paths = _paths(tmp_path, film1)
    flags = HDRFlags(has_dovi=False, has_hdr10plus=True)

    wf = MergeDoviWorkflow()

    def _fake_get_framecount(path: Path) -> int | None:
        if path == film1:
            return 1000
        return 1005

    wf._get_framecount = _fake_get_framecount  # type: ignore[method-assign]

    with pytest.raises(WorkflowError, match="flux injecté"):
        wf._step_verify(film1, paths, flags)


def test_verify_keeps_rpu_check_for_dovi(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    film1 = tmp_path / "film1.mkv"
    film1.write_bytes(b"film1")
    paths = _paths(tmp_path, film1)
    flags = HDRFlags(has_dovi=True, has_hdr10plus=False)

    wf = MergeDoviWorkflow(dovi_tool_bin="dovi_tool")

    def _fake_get_framecount(path: Path) -> int | None:
        if path == film1:
            return 1000
        return 1000

    wf._get_framecount = _fake_get_framecount  # type: ignore[method-assign]

    class _Result:
        returncode = 0
        stdout = "rpu frames: 1010\n"
        stderr = ""

    monkeypatch.setattr("core.workflows.merge_dovi.subprocess.run", lambda *a, **k: _Result())

    with pytest.raises(WorkflowError, match="RPU frames"):
        wf._step_verify(film1, paths, flags)


def test_cleanup_removes_wrapped_video(tmp_path: Path) -> None:
    film1 = tmp_path / "film1.mkv"
    film1.write_bytes(b"film1")
    paths = _paths(tmp_path, film1)

    for p in [
        paths.film1_hevc,
        paths.film2_hevc,
        paths.film2_rpu,
        paths.film2_hdr10plus,
        paths.film1_with_dovi,
        paths.film1_final,
        paths.film1_wrapped_video,
    ]:
        p.write_bytes(b"x")

    wf = MergeDoviWorkflow()
    wf._step_cleanup(paths)

    for p in [
        paths.film1_hevc,
        paths.film2_hevc,
        paths.film2_rpu,
        paths.film2_hdr10plus,
        paths.film1_with_dovi,
        paths.film1_final,
        paths.film1_wrapped_video,
    ]:
        assert not p.exists()
