"""Images sans RPU (règle de répétition), RPU préparé réutilisé, Master Display du master DV et aperçus FEL."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.workflows.encode import EncodeConfig, EncodeWorkflow, VideoEncodeSettings
from core.workflows.encode.models import EncodeError
from core.workflows.encode.runtime import dovi_rpu_gaps
from core.workflows.encode.runtime.dovi_rpu_gaps import RpuGaps, fill_rpu_gaps, gap_refusal, scan_rpu_gaps

_P3_1000 = "G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,1)"


def _nal(kind: int, body: bytes = b"\x01\x02") -> bytes:
    payload = bytes([kind << 1, 1]) + body
    return len(payload).to_bytes(4, "big") + payload


def _fake_reader(monkeypatch, frames: list[tuple[int, tuple[int, ...]]]) -> None:
    """Lecteur Matroska simulé : (horodatage ms, types de NAL) par bloc, en ordre de décodage."""
    track = SimpleNamespace(number=1, track_type=1, codec_private=b"")

    class Reader:
        def __init__(self, _path: Path) -> None:
            pass

        def segment(self) -> None:
            return None

        def tracks(self) -> list[SimpleNamespace]:
            return [track]

        def blocks(self, *, track_numbers: set[int]):
            assert track_numbers == {1}
            for stamp, kinds in frames:
                yield SimpleNamespace(timestamp_ns=stamp * 1_000_000, timestamp_ms=stamp,
                                      payload=b"".join(_nal(kind) for kind in kinds))

    monkeypatch.setattr("core.matroska.reader.MatroskaReader", Reader)


def test_scan_rpu_gaps_reports_display_rank_and_trailing(monkeypatch, tmp_path):
    source = tmp_path / "film.mkv"
    source.write_bytes(b"\0" * 64)
    # Ordre de décodage I P B B ; la dernière AU décodée (B, affichée en 2e) n'a ni RPU ni EL.
    _fake_reader(monkeypatch, [(0, (1, 63, 62)), (125, (1, 63, 62)), (83, (1, 63, 62)), (42, (1,))])
    gaps = scan_rpu_gaps(source, 0)
    assert gaps == RpuGaps(frames=4, missing=(1,), with_el=(), trailing=True)


def test_scan_rpu_gaps_flags_el_without_rpu(monkeypatch, tmp_path):
    source = tmp_path / "film.mkv"
    source.write_bytes(b"\0" * 64)
    _fake_reader(monkeypatch, [(0, (1, 63)), (42, (1, 63, 62)), (83, (1, 63, 62))])
    gaps = scan_rpu_gaps(source, 0)
    assert gaps.missing == (0,) and gaps.with_el == (0,) and not gaps.trailing
    assert gap_refusal(gaps, fel=False) == ""
    assert "couche EL" in gap_refusal(gaps, fel=True)


@pytest.mark.parametrize("gaps,refused", [
    (RpuGaps(frames=10, missing=(4,), with_el=(), trailing=False), False),        # image unique
    (RpuGaps(frames=10, missing=(7, 8, 9), with_el=(), trailing=True), False),    # fin du flux
    (RpuGaps(frames=10, missing=(2, 6), with_el=(), trailing=False), True),       # réparties : refus
])
def test_gap_rule(gaps, refused):
    assert bool(gap_refusal(gaps, fel=True)) is refused


def test_fill_rpu_gaps_duplicates_previous_in_display_order(tmp_path):
    commands: list[list[str]] = []
    edits: list[dict] = []

    def run(cmd: list[str]) -> None:
        commands.append(cmd)
        edits.append(json.loads(Path(cmd[cmd.index("-j") + 1]).read_text()))
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"rpu")

    out = fill_rpu_gaps(dovi_tool_bin="dovi_tool", rpu_input=tmp_path / "in.bin", output_rpu=tmp_path / "out.bin",
                        positions=(5, 0), run_cmd=run)
    assert out == tmp_path / "out.bin" and out.is_file()
    # Rangs croissants : le rang 0 répète l'image suivante, le rang 5 l'image 4 de la liste déjà complétée.
    assert [e["duplicate"][0] for e in edits] == [{"source": 0, "offset": 0, "length": 1},
                                                   {"source": 4, "offset": 5, "length": 1}]
    assert commands[0][commands[0].index("-i") + 1] == str(tmp_path / "in.bin")
    assert commands[1][commands[1].index("-i") + 1] == commands[0][commands[0].index("-o") + 1]
    assert not list(tmp_path.glob("*.json")) and not list(tmp_path.glob("out.fill0.bin"))


def _workflow_with_counts(monkeypatch, rpu_frames: int, video_frames: int) -> EncodeWorkflow:
    wf = EncodeWorkflow()
    guard = SimpleNamespace(rpu_frame_count=Mock(side_effect=lambda path: video_frames if "complete" in path.name
                                                 else rpu_frames),
                            source_frame_count=Mock(return_value=video_frames))
    monkeypatch.setattr(wf, "_frame_count_guard", lambda: guard)
    return wf


def _config(tmp_path: Path, video: VideoEncodeSettings, *, override: bool = False) -> EncodeConfig:
    return EncodeConfig(source=video.source_path or tmp_path / "film.mkv", output=tmp_path / "out.mkv", video=video,
                        audio_tracks=[], allow_validation_override=override)


def test_complete_source_rpu_repeats_previous_rpu(monkeypatch, tmp_path):
    source = tmp_path / "film.mkv"
    wf = _workflow_with_counts(monkeypatch, 216, 217)
    monkeypatch.setattr(dovi_rpu_gaps, "scan_rpu_gaps",
                        lambda *_a, **_k: RpuGaps(frames=217, missing=(214,), with_el=(), trailing=True))
    filled = Mock(side_effect=lambda **kw: kw["output_rpu"])
    monkeypatch.setattr(dovi_rpu_gaps, "fill_rpu_gaps", filled)
    logs: list[tuple[str, str]] = []
    wf.log_message.connect(lambda level, text: logs.append((level, text)))
    video = VideoEncodeSettings(codec="libx265", copy_dv=True, dovi_source_profile="p7_fel", source_path=source)
    result = wf._complete_source_rpu(_config(tmp_path, video), video, tmp_path / "source_rpu_1.bin", index=1,
                                     run=Mock(), signals=None, source=source)
    assert result.name == "source_rpu_1.complete.bin"
    assert filled.call_args.kwargs["positions"] == (214,)
    assert any(level == "WARN" and "rang d'affichage 214" in text for level, text in logs)


def test_complete_source_rpu_refuses_scattered_gaps_before_encoding(monkeypatch, tmp_path):
    source = tmp_path / "film.mkv"
    wf = _workflow_with_counts(monkeypatch, 8, 10)
    monkeypatch.setattr(dovi_rpu_gaps, "scan_rpu_gaps",
                        lambda *_a, **_k: RpuGaps(frames=10, missing=(2, 6), with_el=(), trailing=False))
    video = VideoEncodeSettings(codec="nvencc_hevc", copy_dv=True, dovi_source_profile="p8_1", source_path=source)
    with pytest.raises(EncodeError, match="réparties"):
        wf._complete_source_rpu(_config(tmp_path, video), video, tmp_path / "r.bin", index=1, run=Mock(),
                                signals=None, source=source)
    # Dérogation acceptée : RPU laissé tel quel, décision reprise par les contrôles de fin d'encodage.
    wf.set_validation_override(lambda _path, _message, _cancelled: True)
    assert wf._complete_source_rpu(_config(tmp_path, video, override=True), video, tmp_path / "r.bin", index=1,
                                   run=Mock(), signals=None, source=source) == tmp_path / "r.bin"


def test_complete_source_rpu_outside_matroska_is_refused(monkeypatch, tmp_path):
    source = tmp_path / "film.mp4"
    wf = _workflow_with_counts(monkeypatch, 9, 10)
    video = VideoEncodeSettings(codec="hevc_qsv", copy_dv=True, dovi_source_profile="p8_1", source_path=source)
    with pytest.raises(EncodeError, match="MKV"):
        wf._complete_source_rpu(_config(tmp_path, video), video, tmp_path / "r.bin", index=1, run=Mock(),
                                signals=None, source=source)


def test_complete_source_rpu_keeps_complete_rpu_without_scan(monkeypatch, tmp_path):
    source = tmp_path / "film.mkv"
    wf = _workflow_with_counts(monkeypatch, 10, 10)
    monkeypatch.setattr(dovi_rpu_gaps, "scan_rpu_gaps", Mock(side_effect=AssertionError("relevé inutile")))
    video = VideoEncodeSettings(codec="libx265", copy_dv=True, dovi_source_profile="p7_mel", source_path=source)
    assert wf._complete_source_rpu(_config(tmp_path, video), video, tmp_path / "r.bin", index=1, run=Mock(),
                                   signals=None, source=source) == tmp_path / "r.bin"


@pytest.mark.parametrize("video,expected", [
    (VideoEncodeSettings(codec="libx265", copy_dv=True, dovi_source_profile="p7_fel"), True),
    (VideoEncodeSettings(codec="copy", copy_dv=True, dovi_source_profile="p7_fel"), False),
    (VideoEncodeSettings(codec="libx265", copy_dv=False, dovi_source_profile="p7_fel"), False),
    (VideoEncodeSettings(codec="libx265", copy_dv=True, dovi_source_profile=""), False),
    (VideoEncodeSettings(codec="libx265", copy_dv=True, dovi_source_profile="p5", p5_to_hdr10=True), False),
])
def test_prepare_source_rpu_scope(monkeypatch, tmp_path, video, expected):
    from core.workflows.encode.runtime import dovi_geometry

    wf = EncodeWorkflow()
    extract = Mock()
    monkeypatch.setattr(dovi_geometry, "extract_dovi_rpu", extract)
    monkeypatch.setattr(wf, "_complete_source_rpu", lambda _c, _v, rpu, **_k: rpu)
    result = wf._prepare_source_rpu(_config(tmp_path, video), video, index=2, work_dir=tmp_path, run=Mock(),
                                    signals=None)
    assert (result.source_rpu_path == tmp_path / "source_rpu_2.bin") is expected
    assert extract.called is expected
    if expected:
        assert extract.call_args.kwargs["mode"] == "0"


def test_prepared_source_rpu_derives_without_rereading(tmp_path):
    from core.workflows.encode.runtime.dovi_geometry import prepared_source_rpu

    prepared = tmp_path / "source_rpu_1.complete.bin"
    prepared.write_bytes(b"RPU")
    video = VideoEncodeSettings(source_rpu_path=prepared)
    run = Mock()
    assert prepared_source_rpu(VideoEncodeSettings(), dovi_tool_bin="d", target=tmp_path / "x.bin", mode=None,
                               run_cmd=run) is None
    assert prepared_source_rpu(video, dovi_tool_bin="d", target=tmp_path / "p5.bin", mode="3", run_cmd=run) is None
    copy = prepared_source_rpu(video, dovi_tool_bin="d", target=tmp_path / "copy.bin", mode=None, run_cmd=run)
    assert copy == tmp_path / "copy.bin" and copy.read_bytes() == b"RPU" and prepared.is_file()
    p81 = prepared_source_rpu(video, dovi_tool_bin="d", target=tmp_path / "p81.bin", mode="2", run_cmd=run)
    cmd = run.call_args.args[0]
    assert p81 == tmp_path / "p81.bin" and cmd[:3] == ["d", "editor", "-i"] and cmd[3] == str(prepared)
    assert not list(tmp_path.glob("*.json"))


@pytest.mark.parametrize("current,provenance,expected", [
    (_P3_1000, "source", _P3_1000.replace("L(10000000,1)", "L(40000000,50)")),  # primaires gardées, L6
    (_P3_1000, "manual", ""),                                                    # saisie conservée
    (_P3_1000, "", ""),                                                          # provenance inconnue
    ("", "source", ""),                                                          # champ vide : inchangé
])
def test_dv_master_display_from_l6(monkeypatch, current, provenance, expected):
    import core.fel.preparation as preparation

    estimate = SimpleNamespace(luminance_source="rpu_l6",
                               master_display="G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(40000000,50)")
    monkeypatch.setattr(preparation, "estimate_static_hdr_from_rpu", Mock(return_value=estimate))
    video = VideoEncodeSettings(codec="libx265", inject_hdr_meta=True, master_display=current,
                                static_hdr_master_display_source=provenance)
    assert preparation.dv_master_display(video, Path("rpu.bin"), "dovi_tool", Mock()) == expected


def test_fel_fallback_restores_source_master_display(tmp_path):
    from core.fel.context import FelExecution
    from core.fel.preparation import fallback_video

    context = FelExecution(source=Mock(), original_rpu=tmp_path / "r.bin", original_max_cll="1000,400",
                           original_light_source="source", original_master_display=_P3_1000)
    video = VideoEncodeSettings(master_display=_P3_1000.replace("L(10000000,1)", "L(40000000,50)"), max_cll="",
                                fel_context=context, bake_dovi_fel=True)
    restored = fallback_video(video)
    assert restored.master_display == _P3_1000 and restored.max_cll == "1000,400" and restored.fel_context is None


def test_preview_segment_start_reads_first_copied_packet(monkeypatch, tmp_path):
    from core.workflows.encode import workflow as module

    outputs = iter([
        "#tb 0: 1/1000\n#media_type 0: video\n0,    3645225,    3645350,       41,   528286, 0xc0eb0d70\n",
        "0.000000\n",
    ])
    calls: list[list[str]] = []

    def run(cmd, **_kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=next(outputs), stderr="")

    monkeypatch.setattr(module.subprocess, "run", run)
    wf = EncodeWorkflow()
    assert wf._preview_segment_start(tmp_path / "film.mkv", 0, 3646.351) == pytest.approx(3645.35)
    assert calls[0][calls[0].index("-ss") + 1] == "3646.351" and "-copyts" in calls[0]


def test_fel_image_preview_renders_with_output_metadata(monkeypatch, tmp_path):
    """Image du plugin encodée sans perte avec le HDR10 statique et le RPU P8.1 de l'image, puis rendue."""
    from core.fel import pipeline

    rpu = tmp_path / "fel_original_1.bin"
    rpu.write_bytes(b"RPU")
    video = VideoEncodeSettings(codec="libx265", copy_dv=True, inject_hdr_meta=True, master_display=_P3_1000,
                                max_cll="997,19", fel_context=SimpleNamespace(original_rpu=rpu))
    wf = EncodeWorkflow()
    monkeypatch.setattr(pipeline, "with_fel_input", lambda cmd, _video: ["FEL", *cmd])
    monkeypatch.setattr(wf, "_preview_image_tonemap_filter", lambda _kind: "libplacebo=x")
    monkeypatch.setattr(wf, "_preview_frame_index", lambda *_a: 87425)
    monkeypatch.setattr(wf, "_preview_rpu_frames", lambda _rpu: 198635)
    commands: list[list[str]] = []
    edits: list[dict] = []

    def run(cmd, **_kw):
        commands.append(list(cmd))
        if "editor" in cmd:
            edits.append(json.loads(Path(cmd[cmd.index("-j") + 1]).read_text()))
        Path(cmd[-1]).write_bytes(b"x")

    monkeypatch.setattr(wf._runner, "_run_cmd", run)
    output = tmp_path / "capture_01.png"
    assert wf._render_fel_preview_image(video, tmp_path / "film.mkv", 0, 3646.351, output, "pq", Mock())
    frame, editor, inject, render = commands
    params = frame[frame.index("-x265-params") + 1]
    assert frame[0] == "FEL" and "lossless=1" in params and "master-display=" + _P3_1000 in params
    assert "max-cll=997,19" in params
    assert edits == [{"mode": 2, "remove": ["0-87424", "87426-198634"]}]
    assert inject[1] == "inject-rpu" and render[render.index("-vf") + 1] == "libplacebo=x"
    assert render[render.index("-i") + 1] == inject[inject.index("-o") + 1]
    assert output.is_file() and sorted(p.name for p in tmp_path.iterdir()) == ["capture_01.png", "fel_original_1.bin"]


def test_fel_image_preview_not_applicable_without_fel_or_for_sdr(tmp_path):
    wf = EncodeWorkflow()
    signals = Mock()
    assert not wf._render_fel_preview_image(VideoEncodeSettings(codec="libx265"), tmp_path / "f.mkv", 0, 1.0,
                                            tmp_path / "o.png", "pq", signals)
    sdr = VideoEncodeSettings(codec="libx265", tonemap_to_sdr=True, fel_context=SimpleNamespace(original_rpu=tmp_path))
    assert not wf._render_fel_preview_image(sdr, tmp_path / "f.mkv", 0, 1.0, tmp_path / "o.png", "pq", signals)
