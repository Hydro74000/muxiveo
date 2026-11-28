"""Merge DoVi — alignement : contrats des éditeurs externes et audits de présence par image.

Les plans d'édition (lot 3) reposent sur ces comportements, vérifiés ici sur des métadonnées marquées
image par image : si une nouvelle version d'outil les change, ces tests échouent avant toute édition fausse.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from core.fel.hdr10plus import FrameRecord, FrameStatistics, SceneRecord, hdr10plus_document
from core.matroska.hevc.access_units import iter_hevc_access_units
from core.matroska.hevc.poc import iter_pictures
from core.matroska.native_muxer import MatroskaNativeMuxer
from core.workflows.merge_dovi_align.presence import (
    has_hdr10plus,
    has_rpu,
    scan_annexb_presence,
    scan_matroska_presence,
)

_DOVI = shutil.which("dovi_tool")
_HDR10P = shutil.which("hdr10plus_tool")


def _run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 0, f"{' '.join(cmd)}\n{result.stderr}"


# --- dovi_tool editor -----------------------------------------------------------------------------


def _marked_rpu(root: Path, frames: int) -> Path:
    """RPU de ``frames`` images, image i marquée par un L5 haut = i (coupe toutes les 2 images)."""
    config = root / "gen.json"
    config.write_text(json.dumps({
        "cm_version": "V29", "length": frames,
        "level6": {"max_display_mastering_luminance": 1000, "min_display_mastering_luminance": 1,
                   "max_content_light_level": 0, "max_frame_average_light_level": 0},
        "shots": [{"start": start, "duration": min(2, frames - start)} for start in range(0, frames, 2)],
    }), encoding="utf-8")
    raw = root / "raw.bin"
    _run(["dovi_tool", "generate", "-j", str(config), "-o", str(raw)])
    marks = root / "marks.json"
    marks.write_text(json.dumps({"active_area": {
        "crop": False,
        "presets": [{"id": i, "left": 0, "right": 0, "top": i, "bottom": 0} for i in range(frames)],
        "edits": {f"{i}-{i}": i for i in range(frames)},
    }}), encoding="utf-8")
    marked = root / "marked.bin"
    _run(["dovi_tool", "editor", "-i", str(raw), "-j", str(marks), "-o", str(marked)])
    return marked


def _rpu_marks(root: Path, rpu: Path) -> tuple[list[int], list[int]]:
    """(marques par image, images à coupe) d'un RPU."""
    export = root / f"{rpu.stem}.export.json"
    scenes = root / f"{rpu.stem}.scenes.txt"
    _run(["dovi_tool", "export", "-i", str(rpu), "-d", f"all={export},scenes={scenes}"])
    marks = []
    for entry in json.loads(export.read_text(encoding="utf-8")):
        blocks = entry["vdr_dm_data"]["cmv29_metadata"]["ext_metadata_blocks"]
        marks.append(next(b["Level5"]["active_area_top_offset"] for b in blocks if "Level5" in b))
    return marks, [int(value) for value in scenes.read_text(encoding="utf-8").split()]


def _dovi_edit(root: Path, rpu: Path, name: str, edit: dict) -> Path:
    config = root / f"{name}.json"
    config.write_text(json.dumps(edit), encoding="utf-8")
    out = root / f"{name}.bin"
    _run(["dovi_tool", "editor", "-i", str(rpu), "-j", str(config), "-o", str(out)])
    return out


@pytest.mark.skipif(_DOVI is None, reason="dovi_tool absent")
def test_dovi_editor_index_spaces(tmp_path: Path) -> None:
    rpu = _marked_rpu(tmp_path, 10)
    assert _rpu_marks(tmp_path, rpu)[0] == list(range(10))
    # Retraits : toutes les plages en indices d'entrée, quel que soit leur ordre.
    removed = _dovi_edit(tmp_path, rpu, "remove", {"remove": ["0-1", "6-6"]})
    assert _rpu_marks(tmp_path, removed)[0] == [2, 3, 4, 5, 7, 8, 9]
    # Retraits puis duplications dans la même passe : sources et offsets dans l'espace post-retrait.
    both = _dovi_edit(tmp_path, rpu, "both", {"remove": ["0-0"], "duplicate": [{"source": 0, "offset": 0, "length": 1}]})
    assert _rpu_marks(tmp_path, both)[0] == [1, 1, 2, 3, 4, 5, 6, 7, 8, 9]


@pytest.mark.skipif(_DOVI is None, reason="dovi_tool absent")
def test_dovi_editor_duplicate_reads_sources_from_the_edited_list(tmp_path: Path) -> None:
    rpu = _dovi_edit(tmp_path, _marked_rpu(tmp_path, 10), "abcd", {"remove": ["4-9"]})
    # Contre-exemple (source > offset d'une autre insertion) : ABCD → AABCAD.
    bad = _dovi_edit(tmp_path, rpu, "bad", {"duplicate": [{"source": 3, "offset": 1, "length": 1},
                                                           {"source": 0, "offset": 3, "length": 1}]})
    assert _rpu_marks(tmp_path, bad)[0] == [0, 0, 1, 2, 0, 3]
    # Règle d'émission (source ≤ offset, une entrée par offset) : sources jamais déplacées.
    good = _dovi_edit(tmp_path, rpu, "good", {"duplicate": [{"source": 0, "offset": 1, "length": 1},
                                                             {"source": 2, "offset": 3, "length": 2}]})
    assert _rpu_marks(tmp_path, good)[0] == [0, 0, 1, 2, 2, 2, 3]


@pytest.mark.skipif(_DOVI is None, reason="dovi_tool absent")
def test_dovi_editor_scene_cuts_apply_before_removals(tmp_path: Path) -> None:
    rpu = _marked_rpu(tmp_path, 10)
    assert _rpu_marks(tmp_path, rpu)[1] == [0, 2, 4, 6, 8]
    out = _dovi_edit(tmp_path, rpu, "cuts", {"remove": ["2-2"], "scene_cuts": {"2-2": True, "5-5": False}})
    # « 5-5 » vise l'image d'entrée 5 (sans coupe) : la coupe de l'image d'entrée 6 reste, en position 5.
    marks, cuts = _rpu_marks(tmp_path, out)
    assert marks == [0, 1, 3, 4, 5, 6, 7, 8, 9] and cuts == [0, 3, 5, 7]
    # Une copie hérite du drapeau de coupe de sa source.
    copied = _dovi_edit(tmp_path, rpu, "copied", {"duplicate": [{"source": 2, "offset": 2, "length": 1}]})
    assert _rpu_marks(tmp_path, copied)[1] == [0, 2, 3, 5, 7, 9]


# --- hdr10plus_tool editor ------------------------------------------------------------------------


def _marked_hdr10plus(path: Path, frames: int) -> Path:
    """JSON HDR10+ de ``frames`` images, image i de moyenne i + 1 nits (une scène par image)."""
    records = tuple(FrameRecord(True, (100.0, 100.0, 100.0), float(i + 1), 100.0, 0.0,
                                (1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0)) for i in range(frames))
    document = hdr10plus_document(FrameStatistics(64, 64, records, tuple(SceneRecord(i, 1) for i in range(frames))))
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _hdr10plus_marks(path: Path) -> list[int]:
    return [entry["LuminanceParameters"]["AverageRGB"] for entry in
            json.loads(path.read_text(encoding="utf-8"))["SceneInfo"]]


def _hdr10plus_edit(root: Path, source: Path, name: str, edit: dict) -> list[int]:
    config = root / f"{name}.json"
    config.write_text(json.dumps(edit), encoding="utf-8")
    out = root / f"{name}.out.json"
    _run(["hdr10plus_tool", "editor", "-i", str(source), "-j", str(config), "-o", str(out)])
    return _hdr10plus_marks(out)


@pytest.mark.skipif(_HDR10P is None, reason="hdr10plus_tool absent")
def test_hdr10plus_editor_index_spaces(tmp_path: Path) -> None:
    source = _marked_hdr10plus(tmp_path / "marked.json", 10)
    marks = _hdr10plus_marks(source)
    assert len(set(marks)) == 10
    order = {value: index for index, value in enumerate(marks)}

    def ranks(name: str, edit: dict) -> list[int]:
        return [order[value] for value in _hdr10plus_edit(tmp_path, source, name, edit)]

    # Plages appliquées l'une après l'autre (indices décalés) : différent de dovi_tool…
    assert ranks("seq", {"remove": ["0-1", "6-6"]}) == [2, 3, 4, 5, 6, 7, 9]
    # … sauf en ordre décroissant, règle d'émission commune aux deux outils.
    assert ranks("desc", {"remove": ["6-6", "0-1"]}) == [2, 3, 4, 5, 7, 8, 9]
    _hdr10plus_edit(tmp_path, source, "abcd", {"remove": ["4-9"]})
    four = tmp_path / "abcd.out.json"
    order4 = {value: index for index, value in enumerate(_hdr10plus_marks(four))}
    bad = _hdr10plus_edit(tmp_path, four, "bad", {"duplicate": [{"source": 3, "offset": 1, "length": 1},
                                                                 {"source": 0, "offset": 3, "length": 1}]})
    assert [order4[value] for value in bad] == [0, 0, 1, 2, 0, 3]


# --- Audits de présence ---------------------------------------------------------------------------


def _has_x265() -> bool:
    return subprocess.run(["ffmpeg", "-hide_banner", "-h", "encoder=libx265"], capture_output=True).returncode == 0


def _strip(source: Path, target: Path, drop: dict[int, int]) -> Path:
    """Copie Annex B sans les NAL de type ``drop[i]`` de l'unité i (ordre de décodage)."""
    with target.open("wb") as out:
        for index, unit in enumerate(iter_hevc_access_units(source)):
            for nal in unit.nal_units:
                if drop.get(index) == nal.nal_type or (drop.get(index) == 39 and has_hdr10plus([(39, nal.payload)])):
                    continue
                out.write(b"\x00\x00\x00\x01" + nal.payload)
    return target


@pytest.mark.skipif(not (_DOVI and _HDR10P and shutil.which("ffmpeg") and _has_x265()),
                    reason="outils DoVi / HDR10+ ou libx265 absents")
def test_presence_audits_see_holes_the_extractors_hide(tmp_path: Path) -> None:
    frames = 12
    base_mkv = tmp_path / "base.mkv"
    _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
          "testsrc2=s=64x64:r=24", "-frames:v", str(frames), "-pix_fmt", "yuv420p10le", "-c:v", "libx265",
          "-x265-params", "log-level=error:bframes=3:keyint=12", str(base_mkv)])
    base = tmp_path / "base.hevc"
    _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(base_mkv), "-c:v", "copy",
          "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", str(base)])
    config = tmp_path / "gen.json"
    config.write_text(json.dumps({"cm_version": "V29", "length": frames}), encoding="utf-8")
    rpu = tmp_path / "rpu.bin"
    _run(["dovi_tool", "generate", "-j", str(config), "-o", str(rpu)])
    with_rpu = tmp_path / "rpu.hevc"
    _run(["dovi_tool", "inject-rpu", "-i", str(base), "-r", str(rpu), "-o", str(with_rpu)])
    full = tmp_path / "full.hevc"
    _run(["hdr10plus_tool", "inject", "-i", str(with_rpu), "-j", str(_marked_hdr10plus(tmp_path / "h.json", frames)),
          "-o", str(full)])
    complete = scan_annexb_presence(full, has_rpu)
    assert complete.frames == frames and complete.complete

    # RPU retiré de l'unité 3 (décodage), HDR10+ retiré de l'unité 5 : rangs d'affichage par POC.
    holed = _strip(full, tmp_path / "holed.hevc", {0: 62, 3: 62, 5: 39})
    # Oracle : rang d'affichage de chaque unité d'après le POC (ordre de décodage → affichage).
    keys = [key for key, _kinds in iter_pictures(full)]
    display = {decode: rank for rank, decode in enumerate(sorted(range(len(keys)), key=lambda i: keys[i]))}
    rpu_gaps = scan_annexb_presence(holed, has_rpu)
    hdr_gaps = scan_annexb_presence(holed, has_hdr10plus)
    assert rpu_gaps.missing == tuple(sorted((display[0], display[3]))) and not rpu_gaps.trailing
    assert hdr_gaps.missing == (display[5],) and not hdr_gaps.trailing
    assert display[3] != 3 or display[5] != 5, "images B attendues : ordres de décodage et d'affichage distincts"
    # L'extracteur HDR10+ comble le trou : le compte extrait ne le révèle pas.
    extracted = tmp_path / "extracted.json"
    result = subprocess.run(["hdr10plus_tool", "extract", "-i", str(holed), "-o", str(extracted)],
                            capture_output=True, text=True)
    if result.returncode == 0:
        assert len(json.loads(extracted.read_text(encoding="utf-8"))["SceneInfo"]) == frames

    # Même relevé depuis le MKV (horodatages des blocs), et trou final en ordre de décodage.
    mkv = tmp_path / "holed.mkv"
    MatroskaNativeMuxer().mux(hevc_input=holed, source_for_timestamps=base_mkv, output=mkv, pixel_width=64,
                              pixel_height=64, timestamp_order="packet")
    assert scan_matroska_presence(mkv, 0, has_rpu).missing == rpu_gaps.missing
    assert scan_matroska_presence(mkv, 0, has_hdr10plus).missing == hdr_gaps.missing
    tail = _strip(full, tmp_path / "tail.hevc", {frames - 1: 62})
    assert scan_annexb_presence(tail, has_rpu).trailing
