"""Inspection HDR : lecture en flux, analyses RPU / HDR10+, instantané, audit, cache, orchestrateur."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np
import pytest

import core.hdr_inspection.json_stream as json_stream
from core.hdr_inspection import (
    DynamicInspector,
    InspectionCache,
    InspectionCancelled,
    Severity,
    audit,
    build_snapshot,
    parse_dovi_export,
    parse_hdr10plus_json,
    scene_alignment,
    snapshot_report,
)
from core.hdr_inspection.json_stream import JsonStreamError, iter_json_array
from core.hdr_inspection.pq import nice_nits, nits_to_pq, pq12_to_nits, pq_to_nits
from core.i18n import current_language, set_current_language
from core.inspector import FileInfo, HDRType, VideoTrack


@pytest.fixture(autouse=True)
def _french():
    """Libellés comparés en français (langue source du catalogue)."""
    previous = current_language()
    set_current_language("fra")
    yield
    set_current_language(previous)


# ── Données synthétiques ────────────────────────────────────────────────────
def _rpu(frame: int, *, scene: bool = False, max_pq: int = 3000, area=(0, 0, 280, 280), l6=(1000, 50, 1000, 400),
         el_type: str | None = None, residual_off: bool = True, l1: bool = True, cm40: bool = False,
         l2=(2081,)) -> dict:
    blocks: list[dict] = []
    if l1:
        blocks.append({"Level1": {"min_pq": 7, "max_pq": max_pq, "avg_pq": 1200}})
    blocks += [{"Level2": {"target_max_pq": t}} for t in l2]
    blocks.append({"Level5": dict(zip(("active_area_left_offset", "active_area_right_offset",
                                        "active_area_top_offset", "active_area_bottom_offset"), area))})
    blocks.append({"Level6": dict(zip(("max_display_mastering_luminance", "min_display_mastering_luminance",
                                        "max_content_light_level", "max_frame_average_light_level"), l6))})
    dm = {"scene_refresh_flag": int(scene), "source_min_pq": 62, "source_max_pq": 3696,
          "cmv29_metadata": {"num_ext_blocks": len(blocks), "ext_metadata_blocks": blocks}}
    if cm40:
        dm["cmv40_metadata"] = {"ext_metadata_blocks": [{"Level8": {"target_display_index": 1}},
                                                        {"Level9": {"source_primary_index": 0}},
                                                        {"Level11": {"content_type": 1}},
                                                        {"Level254": {"dm_mode": 0, "dm_version_index": 2}}]}
    entry = {"dovi_profile": 7 if el_type else 8, "header": {"disable_residual_flag": residual_off},
             "vdr_dm_data": dm}
    if el_type:
        entry["el_type"] = el_type
    return entry


def _write_export(path: Path, rpus: list[dict]) -> Path:
    path.write_text(json.dumps(rpus, separators=(",", ":")), encoding="utf-8")
    return path


def _hdr10plus_entry(frame: int, scene_frame: int, maxscl=(4000, 3900, 3800), avg=385, tsdml=400) -> dict:
    return {
        "BezierCurveData": {"Anchors": [102] * 9, "KneePointX": 0, "KneePointY": 0},
        "LuminanceParameters": {
            "AverageRGB": avg,
            "LuminanceDistributions": {"DistributionIndex": [1, 5, 10, 25, 50, 75, 90, 95, 99],
                                       "DistributionValues": [1, 3700, 87, 57, 195, 563, 1111, 1299, 3594]},
            "MaxScl": list(maxscl),
        },
        "NumberOfWindows": 1,
        "TargetedSystemDisplayMaximumLuminance": tsdml,
        "SceneFrameIndex": scene_frame,
        "SceneId": 0,
        "SequenceFrameIndex": frame,
    }


def _write_hdr10plus(path: Path, scene_lengths: list[int], **kwargs) -> Path:
    infos = []
    frame = 0
    for length in scene_lengths:
        for offset in range(length):
            infos.append(_hdr10plus_entry(frame, offset, **kwargs))
            frame += 1
    document = {"JSONInfo": {"HDR10plusProfile": "B", "Version": "1.0"}, "SceneInfo": infos,
                "SceneInfoSummary": {"SceneFirstFrameIndex": [], "SceneFrameNumbers": []},
                "ToolInfo": {"Tool": "hdr10plus_tool", "Version": "1.7.2"}}
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return path


def _video(**raw_fields) -> VideoTrack:
    side_data = raw_fields.pop("side_data", [])
    return VideoTrack(
        index=raw_fields.pop("index", 0), codec="hevc", codec_long="hevc", width=3840, height=2160,
        frame_rate="24000/1001", bit_depth=10, color_space="yuv420p10le", color_primaries="bt2020",
        color_transfer=raw_fields.pop("transfer", "smpte2084"), color_matrix="bt2020nc",
        hdr_type=raw_fields.pop("hdr_type", HDRType.DOLBY_VISION_HDR10PLUS),
        duration_s=raw_fields.pop("duration", None), raw={"side_data_list": side_data},
    )


_DOVI_RECORD = {"side_data_type": "DOVI configuration record", "dv_profile": 7, "dv_level": 6,
                "rpu_present_flag": 1, "el_present_flag": 1, "bl_present_flag": 1,
                "dv_bl_signal_compatibility_id": 6}
_MASTERING = {"side_data_type": "Mastering display metadata", "red_x": "34000/50000", "red_y": "16000/50000",
              "green_x": "13250/50000", "green_y": "34500/50000", "blue_x": "7500/50000", "blue_y": "3000/50000",
              "white_point_x": "15635/50000", "white_point_y": "16450/50000",
              "min_luminance": "50/10000", "max_luminance": "10000000/10000"}
_CLL = {"side_data_type": "Content light level metadata", "max_content": 1000, "max_average": 400}


def _info(path: Path, *, frames: int | None = 10, side_data=None, mediainfo=None, **video) -> FileInfo:
    track = _video(side_data=side_data if side_data is not None else [_DOVI_RECORD, _MASTERING, _CLL], **video)
    info = FileInfo(path=path, format="matroska", duration_s=10 / (24000 / 1001), size_bytes=1, bit_rate=None,
                    video_tracks=[track], hdr_type=track.hdr_type)
    info.frame_count = frames
    info.mediainfo_json = mediainfo or {}
    return info


# ── PQ ───────────────────────────────────────────────────────────────────────
def test_pq_round_trip_and_nice_rounding():
    for nits in (0.005, 1.0, 100.0, 1000.0, 4000.0, 10000.0):
        assert float(pq_to_nits(nits_to_pq(nits))) == pytest.approx(nits, rel=1e-9)
    assert float(pq12_to_nits(4095)) == pytest.approx(10000.0)
    assert nice_nits(1000.6) == 1000 and nice_nits(599.2) == 600 and nice_nits(48.6) == 49
    # Hors du code PQ 12 bits de la dizaine voisine : valeur conservée.
    assert nice_nits(1185.0) == 1185


# ── Lecture en flux ──────────────────────────────────────────────────────────
def test_iter_json_array_across_chunk_boundaries(tmp_path, monkeypatch):
    monkeypatch.setattr(json_stream, "_CHUNK", 7)
    path = tmp_path / "a.json"
    items = [{"a": i, "text": "x" * (i * 5)} for i in range(40)]
    path.write_text(json.dumps(items, indent=1), encoding="utf-8")
    assert list(iter_json_array(path)) == items
    keyed = tmp_path / "b.json"
    keyed.write_text(json.dumps({"Head": {"SceneInfo": 1}, "SceneInfo": items, "SceneInfoSummary": {"x": [9]}}, indent=2))
    assert list(iter_json_array(keyed, key="SceneInfo")) == items


def test_iter_json_array_rejects_truncated_and_missing(tmp_path):
    path = tmp_path / "t.json"
    path.write_text('[{"a": 1}, {"a":', encoding="utf-8")
    with pytest.raises(JsonStreamError):
        list(iter_json_array(path))
    path.write_text('{"Other": []}', encoding="utf-8")
    with pytest.raises(JsonStreamError):
        list(iter_json_array(path, key="SceneInfo"))
    path.write_text("[]", encoding="utf-8")
    assert list(iter_json_array(path)) == []


# ── RPU ──────────────────────────────────────────────────────────────────────
def test_parse_dovi_export_levels_scenes_and_areas(tmp_path):
    rpus = [_rpu(0, scene=True, el_type="FEL", residual_off=False, cm40=True, l2=(2081, 2851)),
            _rpu(1, el_type="FEL", residual_off=False, max_pq=3500),
            _rpu(2, scene=True, el_type="FEL", area=(0, 0, 0, 0), l1=False),
            _rpu(3, el_type="FEL", area=(0, 0, 0, 0), l6=(4000, 50, 3000, 600))]
    analysis = parse_dovi_export(_write_export(tmp_path / "rpu.json", rpus))
    assert analysis.frames == 4 and analysis.profile == 7 and analysis.el_type == "FEL"
    assert analysis.residual_active_frames == 2 and analysis.cm_version == "4.0"
    assert analysis.scene_starts.tolist() == [0, 2] and analysis.scene_lengths().tolist() == [2, 2]
    # L1 absent : valeurs de l'image précédente reprises et comptées.
    assert analysis.l1_missing == 1 and analysis.l1_max.tolist() == [3000, 3500, 3500, 3000]
    assert [(a.first_frame, a.frames, a.top) for a in analysis.active_areas] == [(0, 2, 280), (2, 2, 0)]
    assert [(s.max_cll, s.max_fall, s.frames) for s in analysis.l6] == [(1000, 400, 3), (3000, 600, 1)]
    assert analysis.l6[0].min_mastering == pytest.approx(0.005)
    assert analysis.l2_targets == (100, 600) and analysis.l8_targets == (1,)
    assert analysis.l9_primaries == (0,) and analysis.l11_content == (1,)
    assert analysis.source_display == ((0.005, 4000),)
    assert analysis.l1_peak_nits() == pytest.approx(float(pq12_to_nits(3500)))


def test_parse_dovi_export_cancel(tmp_path):
    path = _write_export(tmp_path / "rpu.json", [_rpu(i) for i in range(3)])
    with pytest.raises(InterruptedError):
        parse_dovi_export(path, cancelled=lambda: True)


# ── HDR10+ ───────────────────────────────────────────────────────────────────
def test_parse_hdr10plus_json(tmp_path):
    analysis = parse_hdr10plus_json(_write_hdr10plus(tmp_path / "h.json", [3, 2]))
    assert analysis.frames == 5 and analysis.profile == "B" and analysis.scene_starts.tolist() == [0, 3]
    assert analysis.peak_nits() == pytest.approx(400.0) and analysis.max_average_nits() == pytest.approx(38.5)
    assert float(analysis.p99.max()) == pytest.approx(359.4)
    assert analysis.windows == (1,) and analysis.targeted_display == (400,) and analysis.bezier_frames == 5


# ── Instantané ───────────────────────────────────────────────────────────────
def test_snapshot_from_side_data(tmp_path):
    snapshot = build_snapshot(_info(tmp_path / "a.mkv"))
    assert snapshot.formats == ("Dolby Vision", "HDR10+", "HDR10")
    assert snapshot.dovi.label == "P7" and snapshot.dovi.el and snapshot.dovi.level == 6
    assert snapshot.mastering.primaries == "Display P3"
    assert snapshot.mastering.min_nits == pytest.approx(0.005) and snapshot.mastering.max_nits == 1000
    assert (snapshot.max_cll, snapshot.max_fall) == (1000, 400)
    assert snapshot.frames == 10 and snapshot.frames_exact


def test_snapshot_mediainfo_fallback_and_estimated_frames(tmp_path):
    mediainfo = {"media": {"track": [{"@type": "Video", "StreamOrder": "0", "HDR_Format": "SMPTE ST 2094 App 4",
                                      "MasteringDisplay_ColorPrimaries": "BT.2020",
                                      "MasteringDisplay_Luminance_Min": "0.0001", "MasteringDisplay_Luminance_Max": "4000",
                                      "MaxCLL": "1500 cd/m2", "MaxFALL": "300 cd/m2"}]}}
    info = _info(tmp_path / "a.mkv", frames=None, side_data=[], mediainfo=mediainfo, hdr_type=HDRType.HDR10,
                 duration=100.0)
    snapshot = build_snapshot(info)
    assert snapshot.hdr10plus and snapshot.dovi is None
    assert snapshot.mastering.primaries == "BT.2020" and snapshot.mastering.max_nits == 4000
    assert (snapshot.max_cll, snapshot.max_fall) == (1500, 300)
    assert snapshot.frames == 2398 and not snapshot.frames_exact


def test_snapshot_sdr_and_hlg(tmp_path):
    sdr = build_snapshot(_info(tmp_path / "s.mkv", side_data=[], transfer="bt709", hdr_type=HDRType.NONE))
    assert sdr.formats == ("SDR",)
    assert any("SDR" in f.title for f in audit(sdr))
    hlg = build_snapshot(_info(tmp_path / "h.mkv", side_data=[], transfer="arib-std-b67", hdr_type=HDRType.HLG))
    assert hlg.formats == ("HLG",)
    assert [f.severity for f in audit(hlg)] == [Severity.INFO]


# ── Audit ────────────────────────────────────────────────────────────────────
def _analyses(tmp_path, *, rpu_frames=10, hdr10plus_scenes=(5, 5), max_pq=3000, **rpu_kwargs):
    rpus = [_rpu(i, scene=i in (0, 5), max_pq=max_pq, **rpu_kwargs) for i in range(rpu_frames)]
    dovi = parse_dovi_export(_write_export(tmp_path / "rpu.json", rpus))
    hdr10plus = parse_hdr10plus_json(_write_hdr10plus(tmp_path / "h.json", list(hdr10plus_scenes)))
    return dovi, hdr10plus


def test_audit_missing_rpu_only_on_exact_count(tmp_path):
    dovi, hdr10plus = _analyses(tmp_path, rpu_frames=8, hdr10plus_scenes=(5, 5))
    exact = build_snapshot(_info(tmp_path / "a.mkv", frames=10))
    findings = audit(exact, dovi, hdr10plus)
    missing = [f for f in findings if f.category == "dv" and f.severity is Severity.ERROR]
    assert missing and "2" in missing[0].title
    assert any(f.category == "coherence" and f.severity is Severity.WARN for f in findings)
    estimated = build_snapshot(_info(tmp_path / "a.mkv", frames=None, duration=10 / (24000 / 1001)))
    assert not [f for f in audit(estimated, dovi, hdr10plus) if f.category == "dv" and f.severity is Severity.ERROR]


def test_audit_static_light_levels_against_dynamic_peaks(tmp_path):
    # Pic L1 ≈ 1789 nits (code 3300) : MaxCLL 1000 trop bas ; 1000/400 = valeurs par défaut probables.
    dovi, hdr10plus = _analyses(tmp_path, max_pq=3300)
    findings = audit(build_snapshot(_info(tmp_path / "a.mkv")), dovi, hdr10plus)
    titles = [f.title for f in findings]
    assert any("MaxCLL (1000 nits) sous le pic L1" in t for t in titles)
    assert any("1000/400" in t for t in titles)
    assert any(f.category == "coherence" and f.severity is Severity.OK for f in findings)


def test_audit_skips_static_checks_without_hdr10_layer(tmp_path):
    record = dict(_DOVI_RECORD, dv_profile=5, dv_bl_signal_compatibility_id=0, el_present_flag=0)
    snapshot = build_snapshot(_info(tmp_path / "p5.mkv", side_data=[record]))
    assert not [f for f in audit(snapshot) if f.category == "hdr10"]
    hdr10 = build_snapshot(_info(tmp_path / "h.mkv", side_data=[], hdr_type=HDRType.HDR10))
    assert {f.title for f in audit(hdr10)} >= {"MDCV absent (SMPTE ST 2086)", "MaxCLL/MaxFALL absents"}


def test_audit_l6_mismatch_fel_empty_and_level(tmp_path):
    dovi, _ = _analyses(tmp_path, el_type="FEL", residual_off=True, l6=(4000, 50, 4000, 622))
    record = dict(_DOVI_RECORD, dv_level=3)
    findings = audit(build_snapshot(_info(tmp_path / "a.mkv", side_data=[record, _MASTERING, _CLL])), dovi)
    titles = [f.title for f in findings]
    assert any(t.startswith("L6 Dolby Vision (4000/622)") for t in titles)
    assert any(t.startswith("FEL vide") for t in titles)
    assert any(t.startswith("Niveau Dolby Vision 3 insuffisant") for t in titles)


def test_scene_alignment_detects_shift():
    first = np.array([0, 100, 250, 400, 610, 800], dtype=np.int32)
    assert scene_alignment(first, first) == (0, 1.0)
    assert scene_alignment(first, np.array([0, 103, 253, 403, 613, 803])) == (-3, 1.0)
    shift, share = scene_alignment(first, np.array([0, 50, 333, 777]))
    assert share < 0.5


# ── Cache ────────────────────────────────────────────────────────────────────
def test_cache_round_trip_invalidation_and_prune(tmp_path, monkeypatch):
    dovi, hdr10plus = _analyses(tmp_path, el_type="FEL", residual_off=False, cm40=True)
    media = tmp_path / "film.mkv"
    media.write_bytes(b"x")
    cache = InspectionCache(tmp_path / "cache")
    assert cache.load(media, 0) is None
    cache.store(media, 0, dovi, hdr10plus)
    loaded = cache.load(media, 0)
    assert loaded is not None
    cached_dovi, cached_hdr10plus = loaded
    assert np.array_equal(cached_dovi.l1_max, dovi.l1_max) and cached_dovi.l6 == dovi.l6
    assert cached_dovi.active_areas == dovi.active_areas and cached_dovi.source_display == dovi.source_display
    assert cached_dovi.cm_version == "4.0" and cached_dovi.l2_targets == dovi.l2_targets
    assert np.array_equal(cached_hdr10plus.max_scl, hdr10plus.max_scl)
    assert cached_hdr10plus.targeted_display == hdr10plus.targeted_display
    assert cache.load(media, 1) is None  # autre piste
    media.write_bytes(b"xy")  # fichier modifié : clé différente
    assert cache.load(media, 0) is None
    import core.hdr_inspection.cache as cache_module
    monkeypatch.setattr(cache_module, "_MAX_ENTRIES", 2)
    for name in ("b.mkv", "c.mkv", "d.mkv"):
        other = tmp_path / name
        other.write_bytes(b"1")
        cache.store(other, 0, None, hdr10plus)
    assert len(list((tmp_path / "cache").glob("*.npz"))) == 2


# ── Orchestrateur ────────────────────────────────────────────────────────────
class _FakeTools:
    """Simule dovi_tool / hdr10plus_tool : écrit les sorties attendues."""

    def __init__(self, export: Path, hdr10plus: Path, *, fail_hdr10plus: bool = False) -> None:
        self.export, self.hdr10plus, self.fail = export, hdr10plus, fail_hdr10plus
        self.commands: list[list[str]] = []

    def __call__(self, cmd):
        self.commands.append(cmd)
        if cmd[0] == "dovi_tool" and "extract-rpu" in cmd:
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"rpu")
        elif cmd[0] == "dovi_tool" and "export" in cmd:
            shutil.copy(self.export, cmd[cmd.index("-d") + 1].split("=", 1)[1])
        elif cmd[0] == "hdr10plus_tool":
            if self.fail:
                raise RuntimeError("File doesn't contain dynamic metadata")
            shutil.copy(self.hdr10plus, cmd[cmd.index("-o") + 1])
        return ""


def _inspector(tmp_path, tools, **kwargs) -> DynamicInspector:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    return DynamicInspector(ffmpeg_bin="ffmpeg", dovi_tool_bin="dovi_tool", hdr10plus_bin="hdr10plus_tool",
                            run_cmd=tools, work_dir=work, **kwargs)


def test_dynamic_inspector_reads_both_and_uses_cache(tmp_path):
    export = _write_export(tmp_path / "src_rpu.json", [_rpu(i, scene=i in (0, 5)) for i in range(10)])
    hdr10plus = _write_hdr10plus(tmp_path / "src_h.json", [5, 5])
    media = tmp_path / "film.hevc"
    media.write_bytes(b"\x00\x00\x01")
    tools = _FakeTools(export, hdr10plus)
    steps: list[str] = []
    cache = InspectionCache(tmp_path / "cache")
    report = _inspector(tmp_path, tools, cache=cache, on_step=lambda label, start, span: steps.append(label)).analyze(
        _info(media))
    assert report.dynamic and not report.from_cache and not report.errors
    assert report.dovi.frames == 10 and report.hdr10plus.frames == 10
    assert [c[1] for c in tools.commands] == ["extract-rpu", "export", "extract"]
    assert len(steps) == 4
    assert not list((tmp_path / "work").iterdir())  # intermédiaires supprimés
    again = _inspector(tmp_path, _FakeTools(export, hdr10plus), cache=cache).analyze(_info(media))
    assert again.from_cache and again.dovi.frames == 10
    refreshed_tools = _FakeTools(export, hdr10plus)
    _inspector(tmp_path, refreshed_tools, cache=cache).analyze(_info(media), refresh=True)
    assert refreshed_tools.commands


def test_dynamic_inspector_pipes_other_containers(tmp_path):
    from core.pipeline_command import PipelineCommand

    export = _write_export(tmp_path / "src_rpu.json", [_rpu(i) for i in range(10)])
    hdr10plus = _write_hdr10plus(tmp_path / "src_h.json", [10])
    media = tmp_path / "film.mp4"
    media.write_bytes(b"\x00")
    tools = _FakeTools(export, hdr10plus)
    report = _inspector(tmp_path, tools).analyze(_info(media))
    assert report.dovi.frames == 10 and report.hdr10plus.frames == 10
    piped = [c for c in tools.commands if isinstance(c, PipelineCommand)]
    assert len(piped) == 2 and all(c.upstream[0][0] == "ffmpeg" and c.upstream[0][-1] == "-" for c in piped)
    assert all("-" in list(c) for c in piped)


def test_dynamic_inspector_keeps_partial_result_on_tool_error(tmp_path):
    export = _write_export(tmp_path / "src_rpu.json", [_rpu(i) for i in range(10)])
    media = tmp_path / "film.hevc"
    media.write_bytes(b"\x00")
    cache = InspectionCache(tmp_path / "cache")
    report = _inspector(tmp_path, _FakeTools(export, export, fail_hdr10plus=True), cache=cache).analyze(_info(media))
    assert report.dovi is not None and report.hdr10plus is None
    assert report.errors and "HDR10+ illisible" in report.errors[0]
    assert cache.load(media, 0) is None  # résultat partiel jamais mis en cache


def test_dynamic_inspector_cancel(tmp_path):
    export = _write_export(tmp_path / "src_rpu.json", [_rpu(i) for i in range(10)])
    media = tmp_path / "film.hevc"
    media.write_bytes(b"\x00")
    with pytest.raises(InspectionCancelled):
        _inspector(tmp_path, _FakeTools(export, export), cancelled=lambda: True).analyze(_info(media))


def test_snapshot_report_level_one(tmp_path):
    report = snapshot_report(_info(tmp_path / "a.mkv"))
    assert not report.dynamic and report.dovi is None
    assert any("1000/400" in f.title for f in report.findings)


def test_audit_flags_saturated_maxscl_and_ignores_it_for_maxcll(tmp_path):
    path = tmp_path / "h.json"
    infos = [_hdr10plus_entry(i, i, maxscl=(5000, 4000, 4000)) for i in range(8)]
    infos[0]["SceneFrameIndex"] = 0
    for offset in range(1, 8):
        infos[offset]["SceneFrameIndex"] = offset
    infos += [_hdr10plus_entry(8 + i, i, maxscl=(100000, 100000, 100000)) for i in range(2)]
    path.write_text(json.dumps({"JSONInfo": {"HDR10plusProfile": "A"}, "SceneInfo": infos}), encoding="utf-8")
    hdr10plus = parse_hdr10plus_json(path)
    assert hdr10plus.saturated().tolist() == [8, 9] and hdr10plus.credible_peak_nits() == pytest.approx(500.0)
    findings = audit(build_snapshot(_info(tmp_path / "a.mkv")), None, hdr10plus)
    titles = [f.title for f in findings]
    assert any(t.startswith("MaxScl à 10 000 nits sur 2 images (1 plan(s)") for t in titles)
    assert not any("sous le pic" in t for t in titles)


def test_audit_reports_shifted_hdr10plus_scenes(tmp_path):
    rpus = [_rpu(i, scene=i in (0, 10, 20, 30, 40)) for i in range(50)]
    dovi = parse_dovi_export(_write_export(tmp_path / "rpu.json", rpus))
    # Plans HDR10+ 3 images plus tard que ceux du RPU.
    hdr10plus = parse_hdr10plus_json(_write_hdr10plus(tmp_path / "h.json", [13, 10, 10, 10, 7]))
    findings = audit(build_snapshot(_info(tmp_path / "a.mkv", frames=50)), dovi, hdr10plus)
    shifted = [f for f in findings if f.category == "coherence" and f.severity is Severity.WARN]
    assert shifted and "+3" in shifted[0].title
