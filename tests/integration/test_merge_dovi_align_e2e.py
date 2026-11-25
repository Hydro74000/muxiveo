"""Merge DoVi — mode Automatique de bout en bout sur médias synthétiques (320×180, quelques centaines d'images).

Film 1 : HEVC 10 bits PQ sans perte (barres franches), cadre « IMAX » (barres 16/16) ; Film 2 : même contenu,
barres 32/32, RPU
généré par plans (L1 propre à chaque plan, L5 du cadre de Film 2) et HDR10+ par plans, avec les différences de
chaque cas (images en tête / en fin, plans ajoutés ou retirés, conteneurs, chapitres…).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import numpy as np
import pytest

from core.fel.hdr10plus import FrameRecord, FrameStatistics, SceneRecord, hdr10plus_document
from core.workflows.merge_dovi import DoviProfile, MergeDoviWorkflow, WorkflowStep
from core.workflows.merge_dovi_align.session import AlignmentMode, AlignmentOptions

W, H = 320, 180
FPS = "24000/1001"
_TOOLS = ("ffmpeg", "ffprobe", "mediainfo", "dovi_tool", "hdr10plus_tool")


def _has_x265() -> bool:
    return subprocess.run(["ffmpeg", "-hide_banner", "-h", "encoder=libx265"], capture_output=True).returncode == 0


pytestmark = [
    pytest.mark.skipif(not all(shutil.which(t) for t in _TOOLS) or not _has_x265(), reason="outils ou libx265 absents"),
    pytest.mark.usefixtures("french_messages"),
]


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


def _shots(seed: int, count: int) -> list[Shot]:
    rng = np.random.default_rng(seed)
    return [Shot(seed * 100 + k, int(rng.integers(18, 34))) for k in range(count)]


def _raw(shots: list[Shot], bars: int, *, sdr: bool = False, hlg: bool = False) -> bytes:
    out = bytearray()
    chroma = np.full((H // 2, W // 2), 512, dtype="<u2").tobytes() * 2
    for shot in shots:
        for index in range(shot.length):
            luma = shot.frame(index).copy()
            if not shot.black:
                luma[:bars] = 64
                luma[H - bars:] = 64
            if sdr:
                luma = 64 + 876 * ((luma - 64) / 876).clip(0, 1) ** 0.6   # autre transfert, même structure
            elif hlg:
                luma = 64 + 876 * ((luma - 64) / 876).clip(0, 1) ** 0.45
            out += np.clip(luma, 0, 1023).astype("<u2").tobytes() + chroma
    return bytes(out)


def _starts(shots: list[Shot]) -> list[int]:
    return np.cumsum([0] + [shot.length for shot in shots[:-1]]).tolist()


def _run(cmd: list[str], **kwargs) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    assert result.returncode == 0, f"{' '.join(map(str, cmd))}\n{result.stderr[-2000:]}"
    return result.stdout


def _encode(raw: bytes, output: Path, *, sdr: bool = False, hlg: bool = False, audio: bool = True,
            bframes: int = 3, extra: tuple[str, ...] = ()) -> Path:
    transfer = "bt709:colormatrix=bt709:colorprim=bt709" if sdr else "arib-std-b67:colormatrix=bt2020nc:colorprim=bt2020" \
        if hlg else (
        "smpte2084:colormatrix=bt2020nc:colorprim=bt2020:master-display="
        "G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(10000000,1):max-cll=650,300")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "yuv420p10le",
           "-s", f"{W}x{H}", "-r", FPS, "-i", "-"]
    if audio:
        cmd += ["-f", "lavfi", "-i", "sine=f=440:sample_rate=48000", "-shortest", "-c:a", "aac"]
    cmd += ["-c:v", "libx265", "-preset", "ultrafast", "-pix_fmt", "yuv420p10le", "-x265-params",
            f"log-level=error:lossless=1:bframes={bframes}:keyint=24:repeat-headers=1:transfer={transfer}", *extra,
            str(output)]
    subprocess.run(cmd, input=raw, check=True, capture_output=True)
    return output


def _film2(root: Path, shots: list[Shot], *, bars: int = 32, container: str = "mkv", hlg: bool = False,
           chapters: list[tuple[float, float, str]] = ()) -> Path:
    """Film 2 : RPU par plan (L1 du plan, L5 de son cadre), HDR10+ par plan, muxé en ``container``."""
    root.mkdir(parents=True, exist_ok=True)
    frames = sum(shot.length for shot in shots)
    base = _encode(_raw(shots, bars, hlg=hlg), root / "film2_base.hevc", audio=False, bframes=0, hlg=hlg)
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
    rpu = root / "rpu.bin"
    _run(["dovi_tool", "generate", "-j", str(config), "-o", str(rpu)])
    l5 = root / "l5.json"
    l5.write_text(json.dumps({"active_area": {"crop": False, "presets": [
        {"id": 0, "left": 0, "right": 0, "top": bars, "bottom": bars}], "edits": {"all": 0}}}), encoding="utf-8")
    rpu_l5 = root / "rpu_l5.bin"
    _run(["dovi_tool", "editor", "-i", str(rpu), "-j", str(l5), "-o", str(rpu_l5)])
    with_rpu = root / "film2_dv.hevc"
    _run(["dovi_tool", "inject-rpu", "-i", str(base), "-r", str(rpu_l5), "-o", str(with_rpu)])
    records, scenes = [], []
    for start, shot in zip(_starts(shots), shots):
        scenes.append(SceneRecord(start, shot.length))
        for index in range(shot.length):
            value = 0.5 if shot.black else shot.level / 4
            records.append(FrameRecord(index == 0, (value * 3, value * 3, value * 3), value, value * 2, value,
                                       tuple(value * k / 7 for k in range(1, 8))))
    json_path = root / "h.json"
    json_path.write_text(json.dumps(hdr10plus_document(FrameStatistics(W, H, tuple(records), tuple(scenes)))),
                         encoding="utf-8")
    final = root / "film2_final.hevc"
    _run(["hdr10plus_tool", "inject", "-i", str(with_rpu), "-j", str(json_path), "-o", str(final)])
    if container == "hevc":
        return final
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
    return output


def _merge(qt_app, root: Path, film1: Path, film2: Path, options: AlignmentOptions | None,
           *, cancel_on_align: bool = False) -> dict:
    wf = MergeDoviWorkflow()
    result: dict = {"logs": []}
    wf.step_progress.connect(lambda step, message: result["logs"].append(message))
    wf.workflow_finished.connect(lambda out: result.update(status="finished", out=out))
    wf.workflow_failed.connect(lambda step, msg: result.update(status="failed", step=step.name, msg=msg))
    wf.workflow_cancelled.connect(lambda: result.update(status="cancelled"))
    if cancel_on_align:
        wf.step_started.connect(lambda step: wf.cancel() if step is WorkflowStep.ALIGN else None)
    wf.start(film1, film2, root / "work", root / "out", dovi_profile=DoviProfile.P8_1, alignment=options)
    start = time.monotonic()
    while "status" not in result and time.monotonic() - start < 600:
        qt_app.processEvents()
        time.sleep(0.02)
    qt_app.processEvents()
    result["workflow"] = wf
    return result


def _rpu_info(output: Path, root: Path) -> tuple[int, list[int], np.ndarray]:
    """(images RPU, coupes, L5 par image) du fichier produit."""
    rpu = root / "check.bin"
    _run(["dovi_tool", "extract-rpu", "-i", str(output), "-o", str(rpu)])
    scenes = root / "check_scenes.txt"
    l5 = root / "check_l5.json"
    _run(["dovi_tool", "export", "-i", str(rpu), "-d", f"scenes={scenes},level5={l5}"])
    from core.workflows.merge_dovi_align.active_area import l5_per_frame
    from core.workflows.merge_dovi_align.signals import rpu_digests

    frames = len(rpu_digests(rpu))
    return frames, sorted(int(v) for v in scenes.read_text().split()), l5_per_frame(l5, frames)


def _hdr10plus_frames(output: Path, root: Path) -> int:
    json_path = root / "check_h.json"
    _run(["hdr10plus_tool", "extract", "-i", str(output), "-o", str(json_path)])
    return len(json.loads(json_path.read_text())["SceneInfo"])


@pytest.fixture
def content() -> list[Shot]:
    return _shots(7, 16)


def test_imax_like_head_tail_and_framing(qt_app, tmp_path: Path, content: list[Shot]) -> None:
    film1 = _encode(_raw(content, 16), tmp_path / "film1.mkv")
    film2 = _film2(tmp_path, [Shot(1, 16, black=True), *content, Shot(2, 24, black=True)])
    result = _merge(qt_app, tmp_path, film1, film2, AlignmentOptions())
    assert result.get("status") == "finished", result.get("msg")
    n1 = sum(shot.length for shot in content)
    frames, cuts, l5 = _rpu_info(Path(result["out"]), tmp_path)
    assert frames == n1 and _hdr10plus_frames(Path(result["out"]), tmp_path) == n1
    assert cuts == _starts(content)                          # coupes de Film 2 ramenées sur Film 1
    assert {tuple(row) for row in l5.tolist()} == {(0, 0, 16, 16)}   # L5 du cadre de Film 1, jamais 32/32
    assert any("Alignement" in line for line in result["logs"])
    assert not list((tmp_path / "work").glob("*/align_*"))   # dossier process nettoyé


def test_shot_missing_from_film1_and_shot_only_in_film1(qt_app, tmp_path: Path, content: list[Shot]) -> None:
    extra = Shot(99, 26)
    film2 = _film2(tmp_path, [*content[:5], extra, *content[5:]])           # plan en plus dans Film 2
    film1 = _encode(_raw(content, 16), tmp_path / "film1.mkv")
    result = _merge(qt_app, tmp_path / "a", film1, film2, AlignmentOptions())
    assert result.get("status") == "finished", result.get("msg")
    assert _rpu_info(Path(result["out"]), tmp_path)[0] == sum(shot.length for shot in content)
    inserted = _encode(_raw([*content[:9], Shot(98, 22), *content[9:]], 16), tmp_path / "film1_ins.mkv")
    refused = _merge(qt_app, tmp_path / "b", inserted, _film2(tmp_path / "f2b", content), AlignmentOptions())
    assert refused.get("status") == "failed" and "sans correspondance" in refused.get("msg", "")
    filled = _merge(qt_app, tmp_path / "c", inserted, _film2(tmp_path / "f2c", content),
                    AlignmentOptions(fill_unmatched=True))
    assert filled.get("status") == "finished", filled.get("msg")
    assert any("recopiées" in line for line in filled["logs"])


def test_equal_counts_but_shifted_are_corrected(qt_app, tmp_path: Path, content: list[Shot]) -> None:
    """Comptes égaux, contenu décalé de 5 images : le mode Strict injecte tel quel, l'Automatique corrige."""
    film1 = _encode(_raw([Shot(3, 3, black=True), *content, Shot(4, 7, black=True)], 16), tmp_path / "film1.mkv")
    film2 = _film2(tmp_path, [Shot(5, 8, black=True), *content, Shot(6, 2, black=True)])
    strict = _merge(qt_app, tmp_path / "s", film1, film2, AlignmentOptions(mode=AlignmentMode.STRICT))
    assert strict.get("status") == "finished"            # comptes égaux : le mode Strict ne voit rien
    assert _rpu_info(Path(strict["out"]), tmp_path)[1][:2] == [0, 8]
    auto = _merge(qt_app, tmp_path / "a", film1, film2, AlignmentOptions())
    assert auto.get("status") == "finished", auto.get("msg")
    cuts = _rpu_info(Path(auto["out"]), tmp_path)[1]
    assert {3 + start for start in _starts(content)} <= set(cuts)


def test_transport_stream_film1_keeps_audio_sync(qt_app, tmp_path: Path, content: list[Shot]) -> None:
    film1 = _encode(_raw(content, 16), tmp_path / "film1.ts")
    film2 = _film2(tmp_path, [Shot(1, 8, black=True), *content])
    result = _merge(qt_app, tmp_path, film1, film2, AlignmentOptions())
    assert result.get("status") == "finished", result.get("msg")

    def first_pts(path: Path, stream: str) -> float:
        out = _run(["ffprobe", "-v", "error", "-select_streams", stream, "-read_intervals", "%+#16",
                    "-show_entries", "packet=pts_time", "-of", "csv=p=0", str(path)])
        return min(float(v.strip().rstrip(",")) for v in out.splitlines() if v.strip())

    source_gap = first_pts(film1, "a:0") - first_pts(film1, "v:0")
    output = Path(result["out"])
    assert first_pts(output, "a:0") - first_pts(output, "v:0") == pytest.approx(source_gap, abs=0.002)


def test_raw_hevc_film2_is_read_entirely(qt_app, tmp_path: Path, content: list[Shot]) -> None:
    film1 = _encode(_raw(content, 16), tmp_path / "film1.mkv")
    film2 = _film2(tmp_path, [Shot(1, 10, black=True), *content], container="hevc")
    result = _merge(qt_app, tmp_path, film1, film2, AlignmentOptions())
    assert result.get("status") == "finished", result.get("msg")
    assert any("Passe complète de Film 2" in line for line in result["logs"])
    assert _rpu_info(Path(result["out"]), tmp_path)[0] == sum(shot.length for shot in content)


def test_film2_chapters_are_projected(qt_app, tmp_path: Path, content: list[Shot]) -> None:
    film1 = _encode(_raw(content, 16), tmp_path / "film1.mkv", audio=False)
    head = Shot(1, 24, black=True)                          # ≈ 1,001 s en tête de Film 2
    film2 = _film2(tmp_path, [head, *content], chapters=[(0.0, 1.0, "Logo"), (3.0, 6.0, "Scène")])
    result = _merge(qt_app, tmp_path, film1, film2, AlignmentOptions())
    assert result.get("status") == "finished", result.get("msg")
    out = json.loads(_run(["ffprobe", "-v", "error", "-show_chapters", "-of", "json", result["out"]]))
    starts = [float(chapter["start_time"]) for chapter in out["chapters"]]
    # « Logo » tient entièrement dans la tête retirée : supprimé ; « Scène » recalé de 24 images.
    assert len(starts) == 1 and starts[0] == pytest.approx(3.0 - 24 * 1001 / 24000, abs=0.05)


def test_sdr_film1_aligns_on_structure(qt_app, tmp_path: Path, content: list[Shot]) -> None:
    film1 = _encode(_raw(content, 16, sdr=True), tmp_path / "film1.mkv", sdr=True)
    film2 = _film2(tmp_path, [Shot(1, 12, black=True), *content])
    result = _merge(qt_app, tmp_path, film1, film2, AlignmentOptions())
    assert result.get("status") == "finished", result.get("msg")
    assert _rpu_info(Path(result["out"]), tmp_path)[0] == sum(shot.length for shot in content)


def test_hlg_film2_base_aligns_on_structure(qt_app, tmp_path: Path, content: list[Shot]) -> None:
    """Image de base HLG (pixels réellement convertis, P8.4) face à un Film 1 PQ : structure seule."""
    film1 = _encode(_raw(content, 16), tmp_path / "film1.mkv")
    film2 = _film2(tmp_path, [Shot(1, 10, black=True), *content], hlg=True)
    result = _merge(qt_app, tmp_path, film1, film2, AlignmentOptions())
    assert result.get("status") == "finished", result.get("msg")
    assert _rpu_info(Path(result["out"]), tmp_path)[1] == _starts(content)


def test_cancellation_during_alignment_leaves_nothing(qt_app, tmp_path: Path, content: list[Shot]) -> None:
    film1 = _encode(_raw(content, 16), tmp_path / "film1.mkv")
    film2 = _film2(tmp_path, [Shot(1, 16, black=True), *content])
    result = _merge(qt_app, tmp_path, film1, film2, AlignmentOptions(), cancel_on_align=True)
    assert result.get("status") == "cancelled"
    workflow = result["workflow"]
    assert all(proc.poll() is not None for proc in workflow._run_procs)
    assert not [p for p in (tmp_path / "work").iterdir() if p.name != ".muxiveo-workdir"]
