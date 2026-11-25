"""Merge DoVi — mode Automatique de bout en bout sur médias synthétiques (320×180, sans perte).

Film 1 : HEVC 10 bits PQ, barres 16/16 ; Film 2 : même contenu, barres 32/32, RPU par plan (L1 propre, L5 de son
cadre) et HDR10+ par image. Chaque cas construit lui-même la correspondance attendue (image de Film 2 montrée par
chaque image de Film 1) : RPU et HDR10+ produits sont comparés image par image aux métadonnées lues dans Film 2
et projetées par cette correspondance (seuls coupes, L5 et L6 sont volontairement réécrits).
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest

from core.fel.hdr10plus import FrameRecord, FrameStatistics, SceneRecord, hdr10plus_document
from core.matroska.editors.dovi import DolbyVisionConfigRecord, MatroskaDoviBlockAdditionEditor
from core.matroska.hevc.access_units import iter_hevc_access_units
from core.workflows.merge_dovi import DoviProfile, MergeDoviWorkflow, WorkflowStep
from core.workflows.merge_dovi_align import align as align_module
from core.workflows.merge_dovi_align import session as session_module
from core.workflows.merge_dovi_align.active_area import l5_per_frame
from core.workflows.merge_dovi_align.session import AlignmentMode, AlignmentOptions

W, H = 320, 180
FPS = "24000/1001"
FRAME_S = 1001 / 24000
_TOOLS = ("ffmpeg", "ffprobe", "mediainfo", "dovi_tool", "hdr10plus_tool")
_T35 = b"\xb5\x00\x3c\x00\x01\x04"


def _has_x265() -> bool:
    return subprocess.run(["ffmpeg", "-hide_banner", "-h", "encoder=libx265"], capture_output=True).returncode == 0


pytestmark = [
    pytest.mark.skipif(not all(shutil.which(t) for t in _TOOLS) or not _has_x265(), reason="outils ou libx265 absents"),
    pytest.mark.usefixtures("french_messages"),
]


# --- Contenu --------------------------------------------------------------------------------------------


class Shot:
    """Plan : texture lisse « open matte » qui glisse ; ``black`` : plan noir (codes 64)."""

    def __init__(self, seed: int, length: int, *, black: bool = False) -> None:
        rng = np.random.default_rng(seed)
        self.length, self.black = length, black
        low = rng.uniform(0, 1, size=(10, 24))
        self.level = float(rng.uniform(250, 650))
        self.texture = self.level + 260 * (np.kron(low, np.ones((20, 20))) - 0.5)

    def frame(self, index: int) -> np.ndarray:
        if self.black:
            return np.full((H, W), 64.0)
        return self.texture[10:10 + H, index:index + W]


class Fade(Shot):
    """Fondu enchaîné entre deux plans : une image différente à chaque rang."""

    def __init__(self, first: Shot, second: Shot, length: int) -> None:
        self.length, self.black, self.first, self.second = length, False, first, second
        self.level = (first.level + second.level) / 2

    def frame(self, index: int) -> np.ndarray:
        weight = (index + 1) / (self.length + 1)
        return (1 - weight) * self.first.frame(index) + weight * self.second.frame(index)


class Hold(Shot):
    """Plan dont les ``extra`` dernières images répètent la dernière image de ``shot``."""

    def __init__(self, shot: Shot, extra: int) -> None:
        self.length, self.black, self.shot, self.level = shot.length + extra, False, shot, shot.level

    def frame(self, index: int) -> np.ndarray:
        return self.shot.frame(min(index, self.shot.length - 1))


class Cut(Shot):
    """Plan amputé de ses ``missing`` dernières images."""

    def __init__(self, shot: Shot, missing: int) -> None:
        self.length, self.black, self.shot, self.level = shot.length - missing, False, shot, shot.level

    def frame(self, index: int) -> np.ndarray:
        return self.shot.frame(index)


def _shots(seed: int, count: int) -> list[Shot]:
    rng = np.random.default_rng(seed)
    return [Shot(seed * 1000 + k, int(rng.integers(18, 34))) for k in range(count)]


def _starts(shots: list[Shot]) -> list[int]:
    return np.cumsum([0] + [shot.length for shot in shots[:-1]]).tolist()


def _total(shots: list[Shot]) -> int:
    return sum(shot.length for shot in shots)


def _raw(shots: list[Shot], bars: int, *, pillar: int = 0, scale: int = 1) -> bytes:
    out = bytearray()
    chroma = np.full((H * scale // 2, W * scale // 2), 512, dtype="<u2").tobytes() * 2
    for shot in shots:
        for index in range(shot.length):
            luma = shot.frame(index).copy()
            if not shot.black:
                luma[:bars] = 64
                luma[H - bars:] = 64
                if pillar:
                    luma[:, :pillar] = 64
                    luma[:, W - pillar:] = 64
            if scale > 1:
                luma = np.kron(luma, np.ones((scale, scale)))
            out += np.clip(luma, 0, 1023).astype("<u2").tobytes() + chroma
    return bytes(out)


def _run(cmd: list[str], **kwargs) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    assert result.returncode == 0, f"{' '.join(map(str, cmd))}\n{result.stderr[-2000:]}"
    return result.stdout


def _transfer(path: Path) -> str:
    return _run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=color_transfer",
                 "-of", "csv=p=0", str(path)]).strip().split(",")[0]


_PQ = ("transfer=smpte2084:colormatrix=bt2020nc:colorprim=bt2020:master-display="
       "G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(10000000,1):max-cll=650,300")


def _encode(raw: bytes, output: Path, *, audio: bool = True, bframes: int = 3, scale: int = 1,
            extra: tuple[str, ...] = ()) -> Path:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "yuv420p10le",
           "-s", f"{W * scale}x{H * scale}", "-r", FPS, "-i", "-"]
    if audio:
        cmd += ["-f", "lavfi", "-i", "sine=f=440:sample_rate=48000", "-shortest", "-c:a", "aac"]
    cmd += ["-c:v", "libx265", "-preset", "ultrafast", "-pix_fmt", "yuv420p10le", "-x265-params",
            f"log-level=error:lossless=1:bframes={bframes}:keyint=24:repeat-headers=1:{_PQ}", *extra, str(output)]
    subprocess.run(cmd, input=raw, check=True, capture_output=True)
    return output


def _convert(source: Path, output: Path, *, target: str) -> Path:
    """Conversion colorimétrique réelle d'un film PQ : « sdr » (BT.709, tone mapping Hable) ou « hlg »."""
    if target == "sdr":
        chain = ("zscale=tin=smpte2084:min=bt2020nc:pin=bt2020:t=linear:npl=1000,format=gbrpf32le,"
                 "zscale=p=bt709,tonemap=hable,zscale=t=bt709:m=bt709:r=tv,format=yuv420p10le")
        params = "transfer=bt709:colormatrix=bt709:colorprim=bt709"
    else:
        chain = "zscale=tin=smpte2084:min=bt2020nc:pin=bt2020:t=arib-std-b67:npl=1000,format=yuv420p10le"
        params = "transfer=arib-std-b67:colormatrix=bt2020nc:colorprim=bt2020"
    _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source), "-map", "0:v:0", "-map", "0:a?",
          "-vf", chain, "-c:v", "libx265", "-preset", "ultrafast", "-x265-params",
          f"log-level=error:lossless=1:bframes=0:keyint=24:repeat-headers=1:{params}", "-c:a", "copy", str(output)])
    return output


# --- Film 2 ---------------------------------------------------------------------------------------------


@dataclass
class Film2:
    path: Path
    dovi: bool
    frames: int


def _hdr10plus_json(path: Path, shots: list[Shot], per_frame: frozenset[int]) -> Path:
    """HDR10+ : valeurs par plan, et image par image pour les plans d'indice ``per_frame`` (fondus)."""
    records, scenes = [], []
    for number, (start, shot) in enumerate(zip(_starts(shots), shots)):
        scenes.append(SceneRecord(start, shot.length))
        for index in range(shot.length):
            value = 0.5 if shot.black else shot.level / 4 + (index * 3 if number in per_frame else 0)
            records.append(FrameRecord(index == 0, (value * 3, value * 3, value * 3), value, value * 2, value,
                                       tuple(value * k / 7 for k in range(1, 8))))
    path.write_text(json.dumps(hdr10plus_document(FrameStatistics(W, H, tuple(records), tuple(scenes)))),
                    encoding="utf-8")
    return path


def _strip(source: Path, target: Path, drops: dict[int, str]) -> Path:
    """Copie Annex B sans le RPU (« rpu ») ou le SEI HDR10+ (« hdr10plus ») des images indiquées (ordre de
    décodage = ordre d'affichage : Film 2 est codé sans B-frames)."""
    with target.open("wb") as out:
        for index, unit in enumerate(iter_hevc_access_units(source)):
            kind = drops.get(index)
            for nal in unit.nal_units:
                if kind == "rpu" and nal.nal_type == 62:
                    continue
                if kind == "hdr10plus" and nal.nal_type in (39, 40) and _T35 in nal.payload:
                    continue
                out.write(b"\x00\x00\x00\x01" + nal.payload)
    return target


def _film2(root: Path, shots: list[Shot], *, bars: int = 32, container: str = "mkv", dovi: bool = True,
           profile: str = "8.1", hlg: bool = False, per_frame: frozenset[int] = frozenset(),
           drops: dict[int, str] | None = None, chapters: tuple[tuple[float, float, str], ...] = ()) -> Film2:
    """Film 2 : RPU par plan (profil ``profile``, L1 du plan, L5 de son cadre), HDR10+ par image."""
    root.mkdir(parents=True, exist_ok=True)
    frames = _total(shots)
    base = _encode(_raw(shots, bars), root / "film2_master.mkv", audio=False, bframes=0)
    if hlg:
        base = _convert(base, root / "film2_hlg.mkv", target="hlg")
    current = root / "film2_base.hevc"
    _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(base), "-map", "0:v:0", "-c", "copy",
          "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", str(current)])
    if dovi:
        config = root / "gen.json"
        config.write_text(json.dumps({
            "cm_version": "V40", "length": frames,
            "level6": {"max_display_mastering_luminance": 1000, "min_display_mastering_luminance": 1,
                       "max_content_light_level": 1000, "max_frame_average_light_level": 400},
            "shots": [{"start": start, "duration": shot.length, "metadata_blocks": [{"Level1": {
                "min_pq": 0, "max_pq": 64 if shot.black else int(shot.level * 4 + 400),
                "avg_pq": 0 if shot.black else int(shot.level * 3)}}]}
                for start, shot in zip(_starts(shots), shots)],
        }), encoding="utf-8")
        generated = root / "rpu_generated.bin"
        _run(["dovi_tool", "generate", "-p", profile, "-j", str(config), "-o", str(generated)])
        l5 = root / "l5.json"
        l5.write_text(json.dumps({"active_area": {"crop": False, "presets": [
            {"id": 0, "left": 0, "right": 0, "top": bars, "bottom": bars}], "edits": {"all": 0}}}), encoding="utf-8")
        rpu = root / "rpu.bin"
        _run(["dovi_tool", "editor", "-i", str(generated), "-j", str(l5), "-o", str(rpu)])
        with_rpu = root / "film2_dv.hevc"
        _run(["dovi_tool", "inject-rpu", "-i", str(current), "-r", str(rpu), "-o", str(with_rpu)])
        current = with_rpu
    final = root / "film2_final.hevc"
    _run(["hdr10plus_tool", "inject", "-i", str(current), "-j", str(_hdr10plus_json(root / "h.json", shots, per_frame)),
          "-o", str(final)])
    if drops:
        final = _strip(final, root / "film2_holed.hevc", drops)
    if container == "hevc":
        return Film2(final, dovi, frames)
    output = root / f"film2.{container}"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-fflags", "+genpts", "-r", FPS, "-i", str(final)]
    if chapters:
        meta = root / "chapters.txt"
        lines = [";FFMETADATA1"]
        for start, end, title in chapters:
            lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(start * 1000)}", f"END={int(end * 1000)}",
                      f"title={title}"]
        meta.write_text("\n".join(lines) + "\n", encoding="utf-8")
        cmd += ["-i", str(meta), "-map", "0", "-map_metadata", "1", "-map_chapters", "1"]
    _run([*cmd, "-c", "copy", str(output)])
    if dovi and container == "mkv":                # record Dolby Vision d'un vrai fichier (profil annoncé)
        MatroskaDoviBlockAdditionEditor().patch(output, record=DolbyVisionConfigRecord(
            profile=8, level=1, rpu_present=True, el_present=False, bl_present=True,
            bl_signal_compat_id=int(profile.split(".")[1])))
    return Film2(output, dovi, frames)


# --- Exécution et contrôles -----------------------------------------------------------------------------


def _merge(qt_app, root: Path, film1: Path, film2: Film2, options: AlignmentOptions | None, *,
           prepare=None) -> dict:
    wf = MergeDoviWorkflow()
    result: dict = {"logs": []}
    wf.step_progress.connect(lambda step, message: result["logs"].append(message))
    wf.workflow_finished.connect(lambda out: result.update(status="finished", out=out))
    wf.workflow_failed.connect(lambda step, msg: result.update(status="failed", step=step.name, msg=msg))
    wf.workflow_cancelled.connect(lambda: result.update(status="cancelled"))
    if prepare is not None:
        prepare(wf)
    wf.start(film1, film2.path, root / "work", root / "out", dovi_profile=DoviProfile.P8_1, alignment=options)
    start = time.monotonic()
    while "status" not in result and time.monotonic() - start < 900:
        qt_app.processEvents()
        time.sleep(0.02)
    qt_app.processEvents()
    result["workflow"] = wf
    return result


def _logged(result: dict, text: str) -> bool:
    return any(text in line for line in result["logs"])


def _rpu_entries(media: Path, root: Path, name: str) -> list[dict]:
    rpu, export = root / f"{name}.bin", root / f"{name}.json"
    _run(["dovi_tool", "extract-rpu", "-i", str(media), "-o", str(rpu)])
    _run(["dovi_tool", "export", "-i", str(rpu), "-d", f"all={export}"])
    return json.loads(export.read_text())


def _rendering(entry: dict) -> bytes:
    """Empreinte d'une image RPU hors champs volontairement réécrits (CRC, coupe, L5, L6)."""
    entry = json.loads(json.dumps(entry))
    entry.pop("rpu_data_crc32", None)
    dm = entry.get("vdr_dm_data") or {}
    dm.pop("scene_refresh_flag", None)
    for name in ("cmv29_metadata", "cmv40_metadata"):
        meta = dm.get(name)
        if isinstance(meta, dict):
            meta.pop("num_ext_blocks", None)
            blocks = [b for b in meta.get("ext_metadata_blocks") or [] if not ({"Level5", "Level6"} & set(b))]
            meta["ext_metadata_blocks"] = sorted(blocks, key=lambda b: json.dumps(b, sort_keys=True))
    return hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).digest()


def _hdr10plus_entries(media: Path, root: Path, name: str) -> list[bytes]:
    path = root / f"{name}_h.json"
    _run(["hdr10plus_tool", "extract", "-i", str(media), "-o", str(path)])
    out = []
    for entry in json.loads(path.read_text())["SceneInfo"]:
        kept = {k: v for k, v in entry.items() if k not in ("SceneFrameIndex", "SceneId", "SequenceFrameIndex")}
        out.append(hashlib.sha256(json.dumps(kept, sort_keys=True).encode()).digest())
    return out


def _check_output(result: dict, root: Path, film2: Film2, mapping: list[int], *, cuts: list[int] | None = None,
                  l5: tuple[int, int, int, int] | None = None) -> None:
    """Métadonnées produites = celles lues dans Film 2, projetées par ``mapping`` (image de Film 2 de chaque
    image de Film 1), image par image ; coupes et L5 (gauche, droite, haut, bas) attendus si fournis."""
    assert result.get("status") == "finished", result.get("msg")
    output = Path(result["out"])
    if film2.dovi:
        source = [_rendering(entry) for entry in _rpu_entries(film2.path, root, "source_rpu")]
        produced = _rpu_entries(output, root, "output_rpu")
        assert [_rendering(entry) for entry in produced] == [source[j] for j in mapping]
        if cuts is not None:
            assert [i for i, e in enumerate(produced) if e["vdr_dm_data"]["scene_refresh_flag"]] == cuts
        if l5 is not None:
            level5 = root / "output_l5.json"
            _run(["dovi_tool", "export", "-i", str(root / "output_rpu.bin"), "-d", f"level5={level5}"])
            assert {tuple(row) for row in l5_per_frame(level5, len(produced)).tolist()} == {l5}
    source_h = _hdr10plus_entries(film2.path, root, "source")
    assert _hdr10plus_entries(output, root, "output") == [source_h[j] for j in mapping]


def _shifted(head: int, count: int) -> list[int]:
    return [head + i for i in range(count)]


@pytest.fixture
def content() -> list[Shot]:
    return _shots(7, 16)


# --- Cas ------------------------------------------------------------------------------------------------


def test_imax_like_head_tail_and_framing(qt_app, tmp_path: Path, content: list[Shot]) -> None:
    film1 = _encode(_raw(content, 16), tmp_path / "film1.mkv")
    film2 = _film2(tmp_path / "f2", [Shot(1, 16, black=True), *content, Shot(2, 24, black=True)])
    result = _merge(qt_app, tmp_path, film1, film2, AlignmentOptions())
    _check_output(result, tmp_path, film2, _shifted(16, _total(content)), cuts=_starts(content), l5=(0, 0, 16, 16))
    assert not [p for p in (tmp_path / "work").iterdir() if p.name != ".muxiveo-workdir"]


def test_shot_removed_and_shot_added(qt_app, tmp_path: Path, content: list[Shot]) -> None:
    extra = Shot(99, 26)
    film2 = _film2(tmp_path / "f2a", [*content[:5], extra, *content[5:]])
    film1 = _encode(_raw(content, 16), tmp_path / "film1.mkv")
    split = _total(content[:5])
    result = _merge(qt_app, tmp_path / "a", film1, film2, AlignmentOptions())
    _check_output(result, tmp_path / "a", film2, [i if i < split else i + extra.length for i in range(_total(content))])

    added = Shot(98, 22)
    inserted = _encode(_raw([*content[:9], added, *content[9:]], 16), tmp_path / "film1_ins.mkv")
    plain = _film2(tmp_path / "f2b", content)
    refused = _merge(qt_app, tmp_path / "b", inserted, plain, AlignmentOptions())
    assert refused.get("status") == "failed" and "sans correspondance" in refused.get("msg", "")
    filled = _merge(qt_app, tmp_path / "c", inserted, plain, AlignmentOptions(fill_unmatched=True))
    cut = _total(content[:9])
    mapping = [i if i < cut else (cut - 1 if i < cut + added.length else i - added.length)
               for i in range(_total(content) + added.length)]
    _check_output(filled, tmp_path / "c", plain, mapping)
    assert _logged(filled, "recopiées")


def test_compensated_ruptures_inside_shots(qt_app, tmp_path: Path, content: list[Shot]) -> None:
    """Deux images de moins à la fin du plan 4, deux images répétées à la fin du plan 10 : comptes égaux."""
    film2 = _film2(tmp_path / "f2", content)
    shots1 = list(content)
    shots1[4] = Cut(content[4], 2)
    shots1[10] = Hold(content[10], 2)
    film1 = _encode(_raw(shots1, 16), tmp_path / "film1.mkv")
    strict = _merge(qt_app, tmp_path / "s", film1, film2, AlignmentOptions(mode=AlignmentMode.STRICT))
    assert strict.get("status") == "finished"            # comptes égaux : le mode Strict ne voit rien
    result = _merge(qt_app, tmp_path / "a", film1, film2, AlignmentOptions(fill_unmatched=True))
    end4, end10 = _total(content[:5]), _total(content[:11])
    mapping: list[int] = []
    for j in range(film2.frames):
        if end4 - 2 <= j < end4:
            continue                                     # images absentes de Film 1
        mapping.append(j)
        if j == end10 - 1:
            mapping += [j, j]                            # images répétées : métadonnées de l'image montrée
    _check_output(result, tmp_path / "a", film2, mapping)


def test_fade_with_metadata_changing_on_every_frame(qt_app, tmp_path: Path, content: list[Shot]) -> None:
    shots = [*content[:7], Fade(content[6], content[7], 20), *content[7:]]
    film2 = _film2(tmp_path / "f2", [Shot(1, 12, black=True), *shots], per_frame=frozenset({8}))
    film1 = _encode(_raw(shots, 16), tmp_path / "film1.mkv")
    result = _merge(qt_app, tmp_path, film1, film2, AlignmentOptions())
    _check_output(result, tmp_path, film2, _shifted(12, _total(shots)))


def test_pillarbox_and_higher_resolution_film1(qt_app, tmp_path: Path, content: list[Shot]) -> None:
    film2 = _film2(tmp_path / "f2", [Shot(1, 8, black=True), *content])
    film1 = _encode(_raw(content, 16, pillar=24, scale=2), tmp_path / "film1.mkv", scale=2)
    result = _merge(qt_app, tmp_path, film1, film2, AlignmentOptions())
    # L5 en pixels de Film 1 (640×360) : piliers 48, barres 32 ; jamais le cadre 32/32 de Film 2 mis à l'échelle.
    _check_output(result, tmp_path, film2, _shifted(8, _total(content)), l5=(48, 48, 32, 32))


@pytest.mark.parametrize("variant", ["ts", "mp4", "mp4_offset"])
def test_container_origins_keep_audio_sync(qt_app, tmp_path: Path, content: list[Shot], variant: str) -> None:
    """TS (départ 1,4 s), MP4 (B-frames et amorçage AAC : départs négatifs par liste d'édition), MP4 décalé
    de +2 s : écart audio / vidéo de Film 1 conservé."""
    extra = ("-output_ts_offset", "2") if variant == "mp4_offset" else ()
    film1 = _encode(_raw(content, 16), tmp_path / f"film1.{'ts' if variant == 'ts' else 'mp4'}", extra=extra)
    film2 = _film2(tmp_path / "f2", [Shot(1, 8, black=True), *content])
    result = _merge(qt_app, tmp_path, film1, film2, AlignmentOptions())
    _check_output(result, tmp_path, film2, _shifted(8, _total(content)))

    def first_pts(path: Path, stream: str) -> float:
        out = _run(["ffprobe", "-v", "error", "-select_streams", stream, "-read_intervals", "%+#16",
                    "-show_entries", "packet=pts_time", "-of", "csv=p=0", str(path)])
        return min(float(v.strip().rstrip(",")) for v in out.splitlines() if v.strip())

    source_gap = first_pts(film1, "a:0") - first_pts(film1, "v:0")
    output = Path(result["out"])
    assert first_pts(output, "a:0") - first_pts(output, "v:0") == pytest.approx(source_gap, abs=0.002)


def test_raw_hevc_and_hdr10plus_only_film2(qt_app, tmp_path: Path, content: list[Shot]) -> None:
    film1 = _encode(_raw(content, 16), tmp_path / "film1.mkv")
    raw = _film2(tmp_path / "f2r", [Shot(1, 10, black=True), *content], container="hevc")
    result = _merge(qt_app, tmp_path / "r", film1, raw, AlignmentOptions())
    _check_output(result, tmp_path / "r", raw, _shifted(10, _total(content)))
    assert _logged(result, "Passe complète de Film 2 : entrée non indexable")
    only = _film2(tmp_path / "f2h", [Shot(1, 10, black=True), *content], dovi=False)
    result = _merge(qt_app, tmp_path / "h", film1, only, AlignmentOptions())
    _check_output(result, tmp_path / "h", only, _shifted(10, _total(content)))


def test_metadata_holes(qt_app, tmp_path: Path, content: list[Shot]) -> None:
    film1 = _encode(_raw(content, 16), tmp_path / "film1.mkv")
    shots2 = [Shot(1, 6, black=True), *content]
    last = _total(shots2) - 1
    tail = _film2(tmp_path / "f2t", shots2, drops={last: "rpu"})        # dernière image sans RPU
    result = _merge(qt_app, tmp_path / "t", film1, tail, AlignmentOptions())
    assert result.get("status") == "finished", result.get("msg")
    assert _logged(result, "complétée(s) par le RPU précédent")
    produced = [_rendering(e) for e in _rpu_entries(Path(result["out"]), tmp_path / "t", "output_rpu")]
    source = [_rendering(e) for e in _rpu_entries(tail.path, tmp_path / "t", "source_rpu")]
    assert produced == source[6:] + [source[-1]]               # image finale : RPU de l'image précédente
    inner = _film2(tmp_path / "f2i", shots2, drops={100: "rpu"})
    refused = _merge(qt_app, tmp_path / "i", film1, inner, AlignmentOptions())
    assert refused.get("status") == "failed" and "sans RPU réparties" in refused.get("msg", "")
    hole = _film2(tmp_path / "f2h", shots2, drops={100: "hdr10plus"})
    refused = _merge(qt_app, tmp_path / "h", film1, hole, AlignmentOptions())
    assert refused.get("status") == "failed" and "sans HDR10+" in refused.get("msg", "")


def test_film2_chapters_projected_and_dropped_in_removed_ranges(qt_app, tmp_path: Path, content: list[Shot]) -> None:
    film1 = _encode(_raw(content, 16), tmp_path / "film1.mkv", audio=False)
    tail_start = (24 + _total(content)) * FRAME_S
    film2 = _film2(tmp_path / "f2", [Shot(1, 24, black=True), *content, Shot(2, 24, black=True)],
                   chapters=((0.0, 1.0, "Logo"), (3.0, 6.0, "Scène"), (tail_start + 0.1, tail_start + 0.9, "Fin")))
    result = _merge(qt_app, tmp_path, film1, film2, AlignmentOptions())
    assert result.get("status") == "finished", result.get("msg")
    chapters = json.loads(_run(["ffprobe", "-v", "error", "-show_chapters", "-of", "json", result["out"]]))["chapters"]
    # « Logo » tient dans la tête retirée, « Fin » dans la fin retirée ; « Scène » recalée de 24 images.
    assert [chapter.get("tags", {}).get("title") for chapter in chapters] == ["Scène"]
    assert float(chapters[0]["start_time"]) == pytest.approx(3.0 - 24 * FRAME_S, abs=0.05)


def test_sdr_film1_after_real_tone_mapping(qt_app, tmp_path: Path, content: list[Shot]) -> None:
    film1 = _convert(_encode(_raw(content, 16), tmp_path / "film1_pq.mkv"), tmp_path / "film1_sdr.mkv", target="sdr")
    assert _transfer(film1) == "bt709"
    film2 = _film2(tmp_path / "f2", [Shot(1, 12, black=True), *content])
    result = _merge(qt_app, tmp_path, film1, film2, AlignmentOptions())
    _check_output(result, tmp_path, film2, _shifted(12, _total(content)))
    assert _transfer(Path(result["out"])) == "smpte2084"           # Film 1 converti en HDR10


def test_hlg_base_profile_8_4(qt_app, tmp_path: Path, content: list[Shot]) -> None:
    """Image de base HLG réellement convertie, RPU généré en profil 8.4 (record compat 4) : refusé sur un Film 1
    PQ (courbes HLG→PQ inapplicables) ; sur un Film 1 HLG, aligné sur la structure, courbes recopiées telles
    quelles et sortie annoncée P8.4."""
    film2 = _film2(tmp_path / "f2", [Shot(1, 12, black=True), *content], hlg=True, profile="8.4")
    assert _transfer(film2.path) == "arib-std-b67"
    pq = _encode(_raw(content, 16), tmp_path / "film1.mkv")
    refused = _merge(qt_app, tmp_path / "pq", pq, film2, AlignmentOptions())
    assert refused.get("status") == "failed" and refused.get("step") == "DETECT_DOVI", refused.get("msg")
    assert "image de base HLG" in refused["msg"] and "« Désactivé »" in refused["msg"]
    hlg = _convert(pq, tmp_path / "film1_hlg.mkv", target="hlg")
    result = _merge(qt_app, tmp_path / "hlg", hlg, film2, AlignmentOptions())
    assert _logged(result, "Source déjà compatible (P8.4)")
    _check_output(result, tmp_path / "hlg", film2, _shifted(12, _total(content)))
    record = json.loads(_run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "stream_side_data", "-of", "json", result["out"]]))["streams"][0]["side_data_list"]
    assert [d.get("dv_bl_signal_compatibility_id") for d in record if "dv_profile" in d] == [4]
    assert _transfer(Path(result["out"])) == "arib-std-b67"


def test_windowed_path_with_luminance_relation_then_d6(qt_app, tmp_path: Path, monkeypatch) -> None:
    """Film assez long pour rester en fenêtres : relation de luminance étalonnée ; puis D6 (passe complète)."""
    shots = _shots(11, 110)
    monkeypatch.setattr(align_module, "SAMPLE_SECONDS", 4.0)
    film1 = _encode(_raw(shots, 16), tmp_path / "film1.mkv", audio=False)
    film2 = _film2(tmp_path / "f2", [Shot(1, 16, black=True), *shots])
    mapping = _shifted(16, _total(shots))
    result = _merge(qt_app, tmp_path / "w", film1, film2, AlignmentOptions())
    _check_output(result, tmp_path / "w", film2, mapping)
    assert _logged(result, "Relation de luminance (moyenne, pic) étalonnée")
    assert not _logged(result, "Passe complète de Film 2")
    d6 = _merge(qt_app, tmp_path / "d6", film1, film2, AlignmentOptions(verify_film2=True))
    _check_output(d6, tmp_path / "d6", film2, mapping)
    assert _logged(d6, "Passe complète de Film 2 : vérification image par image demandée")


def test_cancellation_while_film1_is_being_decoded(qt_app, tmp_path: Path, content: list[Shot], monkeypatch) -> None:
    film1 = _encode(_raw(content, 16), tmp_path / "film1.mkv")
    film2 = _film2(tmp_path / "f2", [Shot(1, 16, black=True), *content])
    original = session_module.Film1Pass._count
    decoding, released = threading.Event(), threading.Event()

    def held(self, frames: int) -> None:         # passe de Film 1 tenue active (FFmpeg bloqué sur son pipe)
        decoding.set()
        released.wait(60)
        original(self, frames)

    monkeypatch.setattr(session_module.Film1Pass, "_count", held)

    def cancel_during_wait(wf) -> None:
        def on_progress(step, _pct) -> None:
            if step is WorkflowStep.ALIGN and decoding.is_set() and not released.is_set():
                wf.cancel()
                released.set()
        wf.step_progress_pct.connect(on_progress)

    result = _merge(qt_app, tmp_path, film1, film2, AlignmentOptions(), prepare=cancel_during_wait)
    released.set()
    assert result.get("status") == "cancelled"
    assert decoding.is_set()
    assert all(proc.poll() is not None for proc in result["workflow"]._run_procs)
    deadline = time.monotonic() + 10
    while any(t.name.startswith("merge-dovi-film1") for t in threading.enumerate()) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not [t for t in threading.enumerate() if t.name.startswith("merge-dovi-film1")]
    assert not [p for p in (tmp_path / "work").iterdir() if p.name != ".muxiveo-workdir"]


def test_strict_injects_shifted_metadata_auto_corrects(qt_app, tmp_path: Path, content: list[Shot]) -> None:
    """Comptes égaux, contenu décalé : Strict injecte tel quel (décalé), Automatique corrige."""
    film1 = _encode(_raw([Shot(3, 3, black=True), *content, Shot(4, 7, black=True)], 16), tmp_path / "film1.mkv")
    film2 = _film2(tmp_path / "f2", [Shot(5, 8, black=True), *content, Shot(6, 2, black=True)])
    strict = _merge(qt_app, tmp_path / "s", film1, film2, AlignmentOptions(mode=AlignmentMode.STRICT))
    _check_output(strict, tmp_path / "s", film2, list(range(film2.frames)))
    auto = _merge(qt_app, tmp_path / "a", film1, film2, AlignmentOptions())
    # Décalage de 5 images ; les 5 dernières images noires de Film 1 recopient la dernière image (noire) de Film 2.
    _check_output(auto, tmp_path / "a", film2, _shifted(5, film2.frames - 5) + [film2.frames - 1] * 5)
