from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from core.workflows.merge_dovi import (
    DoviProfile,
    FrameCountResult,
    HDRFlags,
    MergeDoviWorkflow,
    StaticHdrMetadata,
    ValidationContext,
    WorkflowError,
    WorkflowStep,
    _WorkflowPaths,
    _format_master_display_from_mediainfo,
    _format_max_cll_from_mediainfo,
)


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

    wf._run_raw = _fake_run_raw  # type: ignore[method-assign, assignment]

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


def test_step_remux_executes_native_mux_and_assembly(tmp_path: Path) -> None:
    film1 = tmp_path / "film1.mkv"
    film1.write_bytes(b"film1")
    paths = _paths(tmp_path, film1)
    flags = HDRFlags(has_dovi=False, has_hdr10plus=True)
    paths.film1_final.write_bytes(b"hevc")

    wf = MergeDoviWorkflow()
    mux_calls: list[dict] = []
    assemble_calls: list[dict] = []

    def _fake_mux(final_hevc, src_film1, step_paths, video_props, dovi_rec, colour_elem):
        mux_calls.append({
            "final_hevc": final_hevc,
            "source_for_timestamps": src_film1,
            "output": step_paths.film1_wrapped_video,
            "video_props": video_props,
            "dovi_record": dovi_rec,
            "colour_element": colour_elem,
        })
        step_paths.film1_wrapped_video.write_bytes(b"wrapped")

    def _fake_assemble(plan, step_flags, dovi_rec):
        assemble_calls.append({
            "plan": plan,
            "flags": step_flags,
            "dovi_record": dovi_rec,
        })
        plan.output.parent.mkdir(parents=True, exist_ok=True)
        plan.output.write_bytes(b"final_mkv")

    wf._mux_native_video = _fake_mux  # type: ignore[method-assign]
    wf._assemble_final_mkv = _fake_assemble  # type: ignore[method-assign]
    wf._read_video_track_props = lambda _f: {  # type: ignore[method-assign]
        "pixel_width": 3840,
        "pixel_height": 2160,
        "language": "und",
        "name": "",
    }

    class _FakeTrack:
        track_type = 2
        language = "fre"
        language_bcp47 = "fr"
        name = "Audio FR"
        flag_enabled = True
        flag_default = True
        flag_forced = False
        flag_hearing_impaired = False
        flag_visual_impaired = False
        flag_original = False
        flag_commentary = False

    class _FakeReader:
        def __init__(self, _p): pass
        def tracks(self): return [_FakeTrack()]
        def raw_top_level(self, _id): return []
        def attachment_headers(self): return []
        def segment_title(self): return "My Movie"

    with patch("core.workflows.merge_dovi.MatroskaReader", _FakeReader):
        wf._step_remux(film1, paths, flags)

    assert len(mux_calls) == 1
    assert mux_calls[0]["final_hevc"] == paths.film1_final
    assert mux_calls[0]["output"] == paths.film1_wrapped_video
    assert len(assemble_calls) == 1
    plan = assemble_calls[0]["plan"]
    assert plan.output == paths.output_mkv
    assert len(plan.ordered_tracks) == 2
    assert plan.ordered_tracks[0].artifact == paths.film1_wrapped_video
    assert plan.ordered_tracks[1].artifact == film1
    assert plan.segment_title == "My Movie"


def test_step_remux_passes_dovi_record_to_native_video_muxer(tmp_path: Path) -> None:
    film1 = tmp_path / "film1.mkv"
    film1.write_bytes(b"film1")
    paths = _paths(tmp_path, film1)
    flags = HDRFlags(has_dovi=True, has_hdr10plus=False)
    paths.film1_with_dovi.write_bytes(b"hevc")
    paths.film2_rpu.write_bytes(b"rpu")

    wf = MergeDoviWorkflow()
    compat_ids: list[int | None] = []
    fake_record = object()

    def _fake_record_fn(_rpu: Path, *, forced_compat_id: int | None = None):
        compat_ids.append(forced_compat_id)
        return fake_record

    wf._build_dovi_record_from_rpu = _fake_record_fn  # type: ignore[method-assign]

    mux_records: list[object] = []
    wf._mux_native_video = lambda _fh, _f1, p, _vp, d_rec, _c: (  # type: ignore[method-assign]
        mux_records.append(d_rec) or p.film1_wrapped_video.write_bytes(b"wrapped")
    )
    wf._assemble_final_mkv = lambda p, _f, _d: p.output.write_bytes(b"out")  # type: ignore[method-assign]
    wf._read_video_track_props = lambda _f: {"pixel_width": 3840, "pixel_height": 2160}  # type: ignore[method-assign]

    class _FakeReader:
        def __init__(self, _p): pass
        def tracks(self): return []
        def raw_top_level(self, _id): return []
        def attachment_headers(self): return []
        def segment_title(self): return ""

    with patch("core.workflows.merge_dovi.MatroskaReader", _FakeReader):
        wf._step_remux(film1, paths, flags, dovi_profile=DoviProfile.P8_1)

    assert compat_ids == [1]
    assert mux_records == [fake_record]


def test_step_remux_incorporates_chapters_from_film2(tmp_path: Path) -> None:
    film1 = tmp_path / "film1.mkv"
    film1.write_bytes(b"film1")
    film2 = tmp_path / "film2.mkv"
    film2.write_bytes(b"film2")
    paths = _paths(tmp_path, film1)
    flags = HDRFlags(has_dovi=False, has_hdr10plus=False)
    paths.film1_hevc_input.write_bytes(b"hevc")

    wf = MergeDoviWorkflow()
    wf._mux_native_video = lambda _fh, _f1, p, _vp, _d, _c: p.film1_wrapped_video.write_bytes(b"wrapped")  # type: ignore[method-assign]
    wf._read_video_track_props = lambda _f: {"pixel_width": 3840, "pixel_height": 2160}  # type: ignore[method-assign]

    captured_plans = []
    wf._assemble_final_mkv = lambda p, _f, _d: captured_plans.append(p) or p.output.write_bytes(b"out")  # type: ignore[method-assign]

    class _FakeReader:
        def __init__(self, p): self.p = p
        def tracks(self): return []
        def raw_top_level(self, _id):
            return [b"chapter_bytes"] if self.p == film2 else []
        def attachment_headers(self): return []
        def segment_title(self): return ""

    with patch("core.workflows.merge_dovi.MatroskaReader", _FakeReader):
        wf._step_remux(film1, paths, flags, film2=film2)

    assert len(captured_plans) == 1
    assert captured_plans[0].chapter_source == film2


def test_patch_video_timecodes_invokes_patcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wf = MergeDoviWorkflow()
    patch_calls: list[tuple[Path, Path]] = []

    class _PatchResult:
        patched_blocks = 100
        last_pts_ms = 4200

    class _Patcher:
        def __init__(self, *, ffprobe_bin: str) -> None: pass
        def patch(self, *, target_mkv: Path, source_for_timestamps: Path):
            patch_calls.append((target_mkv, source_for_timestamps))
            return _PatchResult()

    monkeypatch.setattr("core.workflows.merge_dovi.MatroskaVideoTimecodePatcher", _Patcher)
    wf._patch_video_timecodes(tmp_path / "out.mkv", tmp_path / "src.mkv", WorkflowStep.REMUX)
    assert patch_calls == [(tmp_path / "out.mkv", tmp_path / "src.mkv")]


def test_patch_dovi_block_addition_invokes_editor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    film1 = tmp_path / "film1.mkv"
    paths = _paths(tmp_path, film1)
    paths.film2_rpu.write_bytes(b"rpu")
    wf = MergeDoviWorkflow()
    record = object()
    wf._build_dovi_record_from_rpu = lambda _rpu, **_kw: record  # type: ignore[method-assign]
    patched: list[tuple[Path, object]] = []

    class _PatchResult:
        applied = True
        skipped = False
        patched_track_number = 1
        bytes_delta = 42
        reason = ""

    class _Editor:
        def patch(self, output: Path, *, record: object):
            patched.append((output, record))
            return _PatchResult()

    monkeypatch.setattr("core.workflows.merge_dovi.MatroskaDoviBlockAdditionEditor", lambda: _Editor())
    wf._patch_dovi_block_addition(paths, dovi_profile=DoviProfile.P8_1, step=WorkflowStep.REMUX)
    assert patched == [(paths.output_mkv, record)]


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


def test_validate_sdr_film1_enables_assisted_hdr10_conversion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    film1 = tmp_path / "film1_sdr.mkv"
    film2 = tmp_path / "film2_hdr10.mkv"
    film1.write_bytes(b"film1")
    film2.write_bytes(b"film2")

    monkeypatch.setattr("core.workflows.merge_dovi.shutil.which", lambda _cmd: "/bin/tool")
    wf = MergeDoviWorkflow()

    def _mediainfo(path: Path, query: str) -> str:
        if query == "Video;%Format%":
            return "HEVC"
        if query == "Video;%HDR_Format%":
            return ""
        if query == "Video;%transfer_characteristics%":
            return "BT.709" if path == film1 else "PQ"
        return ""

    wf._mediainfo = _mediainfo  # type: ignore[method-assign, assignment]
    wf._read_static_hdr_metadata = lambda path: (  # type: ignore[method-assign]
        StaticHdrMetadata()
        if path == film1
        else StaticHdrMetadata(
            master_display="G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(10000000,50)",
            max_cll="1000,400",
        )
    )

    result = wf._step_validate(film1, film2, DoviProfile.DISABLED)

    assert result.film1_needs_sdr_to_hdr10 is True
    assert result.flags.has_hdr10plus is False
    assert result.film2_has_hdr10_reference is True


def test_validate_hdr10_film1_keeps_hdr10plus_injection_without_conversion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    film1 = tmp_path / "film1_hdr10.mkv"
    film2 = tmp_path / "film2_hdr10plus.mkv"
    film1.write_bytes(b"film1")
    film2.write_bytes(b"film2")

    monkeypatch.setattr("core.workflows.merge_dovi.shutil.which", lambda _cmd: "/bin/tool")
    wf = MergeDoviWorkflow()

    def _mediainfo(path: Path, query: str) -> str:
        if query == "Video;%Format%":
            return "HEVC"
        if query == "Video;%HDR_Format%":
            return "SMPTE ST 2094 App 4" if path == film2 else "HDR10"
        if query == "Video;%transfer_characteristics%":
            return "PQ"
        return ""

    wf._mediainfo = _mediainfo  # type: ignore[method-assign, assignment]
    wf._read_static_hdr_metadata = lambda _path: StaticHdrMetadata(  # type: ignore[method-assign, assignment]
        master_display="G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(10000000,50)",
        max_cll="1000,400",
    )

    result = wf._step_validate(film1, film2, DoviProfile.DISABLED)

    assert result.film1_needs_sdr_to_hdr10 is False
    assert result.flags.has_hdr10plus is True
    assert result.flags.has_dovi is False


def test_framecount_large_delta_allowed_for_sdr_conversion_only(tmp_path: Path) -> None:
    film1 = tmp_path / "film1_sdr.mkv"
    film2 = tmp_path / "film2_hdr10plus.mkv"
    film1.write_bytes(b"film1")
    film2.write_bytes(b"film2")

    wf = MergeDoviWorkflow()
    counts = {film1: 1000, film2: 1020}
    wf._get_framecount = lambda path: counts[path]  # type: ignore[method-assign]

    with pytest.raises(WorkflowError, match="Écart de 20 frames"):
        wf._step_framecount(film1, film2)

    result = wf._step_framecount(film1, film2, allow_large_delta=True)

    assert result.diff == 20
    assert result.compatible is False


def test_run_sdr_large_frame_delta_converts_without_metadata_injection(tmp_path: Path) -> None:
    film1 = tmp_path / "film1_sdr.mkv"
    film2 = tmp_path / "film2_hdr10plus_dovi.mkv"
    film1.write_bytes(b"film1")
    film2.write_bytes(b"film2")
    paths = _paths(tmp_path, film1)
    static = StaticHdrMetadata(
        master_display="G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(10000000,50)",
        max_cll="1000,400",
    )
    wf = MergeDoviWorkflow()
    calls: list[str] = []
    remux_flags: list[HDRFlags] = []

    wf._step_validate = lambda *_args: ValidationContext(  # type: ignore[method-assign]
        flags=HDRFlags(has_dovi=True, has_hdr10plus=True),
        static_film1=StaticHdrMetadata(),
        static_film2=static,
        film1_needs_sdr_to_hdr10=True,
        film2_has_hdr10_reference=True,
    )
    wf._step_detect_dovi = lambda *_args: None  # type: ignore[method-assign]
    wf._step_framecount = lambda *_args, **_kwargs: FrameCountResult(1000, 1020, 20)  # type: ignore[method-assign]

    def _extract_hevc(_film1, _film2, step_paths, flags, routing) -> None:
        calls.append("extract_hevc")
        assert flags.has_dovi is False
        assert flags.has_hdr10plus is False
        assert routing is None
        step_paths.film1_hevc.write_bytes(b"hevc")

    def _convert_sdr(step_paths, _static_hdr) -> None:
        calls.append("convert_sdr")
        step_paths.film1_hdr10_hevc.write_bytes(b"hdr10")

    def _extract_metadata(*_args) -> None:
        calls.append("extract_metadata")

    def _inject_dovi(*_args) -> None:
        calls.append("inject_dovi")

    def _inject_hdr10plus(*_args) -> None:
        calls.append("inject_hdr10plus")

    def _remux(_film1, _paths, flags, **_kwargs) -> None:
        calls.append("remux")
        remux_flags.append(flags)

    wf._step_extract_hevc = _extract_hevc  # type: ignore[method-assign, assignment]
    wf._step_convert_sdr_to_hdr10 = _convert_sdr  # type: ignore[method-assign, assignment]
    wf._step_extract_metadata = _extract_metadata  # type: ignore[method-assign, assignment]
    wf._step_inject_dovi = _inject_dovi  # type: ignore[method-assign, assignment]
    wf._step_inject_hdr10plus = _inject_hdr10plus  # type: ignore[method-assign, assignment]
    wf._step_inject_static_hdr = lambda *_args: False  # type: ignore[method-assign]
    wf._step_verify = lambda *_args, **_kwargs: calls.append("verify")  # type: ignore[method-assign]
    wf._step_remux = _remux  # type: ignore[method-assign, assignment]
    wf._step_cleanup = lambda *_args: calls.append("cleanup")  # type: ignore[method-assign]

    wf._run(film1, film2, paths, DoviProfile.P8_1)

    assert "convert_sdr" in calls
    assert "remux" in calls
    assert "extract_metadata" not in calls
    assert "inject_dovi" not in calls
    assert "inject_hdr10plus" not in calls
    assert remux_flags
    assert remux_flags[0].has_dovi is False
    assert remux_flags[0].has_hdr10plus is False


def test_step_convert_sdr_to_hdr10_builds_ffmpeg_hdr10_command(tmp_path: Path) -> None:
    film1 = tmp_path / "film1.mkv"
    film1.write_bytes(b"film1")
    paths = _paths(tmp_path, film1)
    paths.film1_hevc.write_bytes(b"hevc")
    static = StaticHdrMetadata(
        master_display="G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(10000000,50)",
        max_cll="1000,400",
    )
    wf = MergeDoviWorkflow(ffmpeg_bin="ffmpeg")
    calls: list[list[str]] = []

    def _fake_run_cmd(cmd: list[str], _step: WorkflowStep) -> str:
        calls.append(cmd)
        Path(cmd[-1]).write_bytes(b"hdr10")
        return ""

    wf._run_cmd = _fake_run_cmd  # type: ignore[method-assign, assignment]

    wf._step_convert_sdr_to_hdr10(paths, static)

    cmd = calls[0]
    assert cmd[:3] == ["ffmpeg", "-hide_banner", "-y"]
    assert "-vf" in cmd
    assert "smpte2084" in cmd[cmd.index("-vf") + 1]
    assert "-x265-params" in cmd
    params = cmd[cmd.index("-x265-params") + 1]
    assert "master-display=" + static.master_display in params
    assert "max-cll=" + static.max_cll in params
    assert cmd[-1] == str(paths.film1_hdr10_hevc)


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

    def _fake_run_cmd(cmd: list[str], step: WorkflowStep) -> str:
        calls.append(cmd)
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"x")
        return ""

    wf._run_cmd = _fake_run_cmd  # type: ignore[method-assign]

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
        paths.film1_hdr10_hevc,
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


class TestDoviRpuFallback:
    """mediainfo ne voit pas le RPU d'un HEVC brut : repli dovi_tool."""

    @staticmethod
    def _run(rpu_bytes: bytes):
        def fake_run(cmd, **kwargs):
            Path(cmd[cmd.index("-o") + 1]).write_bytes(rpu_bytes)
            assert cmd[1:2] == ["extract-rpu"] and "-l" in cmd
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return fake_run

    @pytest.mark.parametrize("rpu,expected", [(b"\x7c\x01rpu", True), (b"", False)])
    def test_raw_hevc_rpu_probe(self, tmp_path, rpu, expected):
        wf = MergeDoviWorkflow()
        with patch("core.workflows.merge_dovi.subprocess.run", side_effect=self._run(rpu)):
            assert wf._has_dovi_rpu(tmp_path / "film2.hevc") is expected

    def test_unsupported_container_is_not_probed(self, tmp_path):
        with patch("core.workflows.merge_dovi.subprocess.run") as run:
            assert MergeDoviWorkflow()._has_dovi_rpu(tmp_path / "film2.mp4") is False
        run.assert_not_called()
