"""Éditions des métadonnées de Film 2 d'après la carte d'alignement (plan § 1, § 3.6).

Contrat des éditeurs (vérifié par ``tests/integration/test_merge_dovi_align_contracts.py``) :

- ``remove`` : plages inclusives, émises en ordre **décroissant** (``dovi_tool`` les lit en indices d'entrée,
  ``hdr10plus_tool`` les applique l'une après l'autre : même effet dans cet ordre) ;
- ``duplicate`` : appliqué après ``remove``, dans l'espace post-retrait, ``source`` lue dans la liste en
  cours de modification → une entrée par offset et ``source ≤ offset`` (jamais déplacée par une insertion) ;
- ``scene_cuts`` appliqué **avant** ``remove`` : passe séparée, dans l'espace final, comme ``active_area`` et
  ``level6``. Une copie hérite de la coupe de sa source : toutes les coupes sont reposées.

Chaque passe est relue : empreinte de chaque image = celle de l'image de Film 2 prévue, coupes, L5 et L6.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .active_area import ActiveAreaPlan, l5_per_frame
from .align import AlignmentError, AlignmentMap, Uncovered
from core.hdr_inspection.json_stream import iter_json_array
from core.i18n import translate_text

from .signals import hdr10plus_digests, rpu_digests

Runner = Callable[[list[str]], str]


class EditRefused(AlignmentError):
    """Images de Film 1 sans correspondance dont les métadonnées seraient fabriquées sans accord (D1)."""


@dataclass(frozen=True)
class EditPlan:
    film1_frames: int
    film2_frames: int
    #: Plages ``(début, longueur)`` de Film 2 retirées.
    removed: tuple[tuple[int, int], ...]
    #: ``(source, offset, longueur)`` dans l'espace post-retrait.
    duplicates: tuple[tuple[int, int, int], ...]
    #: Image de Film 2 dont provient chaque image finale.
    sources: np.ndarray
    expected_cuts: np.ndarray
    active_area: ActiveAreaPlan | None
    level6: dict | None
    filled: tuple[Uncovered, ...]
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def positional(self) -> bool:
        return bool(self.removed or self.duplicates)

    def position_edit(self) -> dict | None:
        """Passe 1 : retraits (ordre décroissant) puis duplications."""
        if not self.positional:
            return None
        edit: dict = {}
        if self.removed:
            edit["remove"] = [f"{start}-{start + length - 1}" for start, length in sorted(self.removed, reverse=True)]
        if self.duplicates:
            edit["duplicate"] = [{"source": source, "offset": offset, "length": length}
                                 for source, offset, length in self.duplicates]
        return edit

    def rpu_finish_edit(self, current_cuts: np.ndarray | None) -> dict | None:
        """Passe 2 (RPU) : coupes reposées, L5 réécrit, L6 ; None si rien à changer."""
        edit: dict = {}
        if current_cuts is None or not np.array_equal(np.asarray(current_cuts), self.expected_cuts):
            edit["scene_cuts"] = _cut_ranges(self.expected_cuts, self.film1_frames)
        if self.active_area is not None and not self.active_area.keep:
            presets: dict[tuple[int, int, int, int], int] = {}
            edits: dict[str, int] = {}
            for start, stop, area in self.active_area.ranges:
                ident = presets.setdefault(area, len(presets))
                edits[f"{start}-{stop - 1}"] = ident
            edit["active_area"] = {
                "crop": False,
                "presets": [{"id": ident, "left": a[0], "right": a[1], "top": a[2], "bottom": a[3]}
                            for a, ident in presets.items()],
                "edits": edits,
            }
        if self.level6 is not None:
            edit["level6"] = dict(self.level6)
        return edit or None


def _cut_ranges(cuts: np.ndarray, frames: int) -> dict[str, bool]:
    """``scene_cuts`` couvrant toutes les images : vrai aux coupes, faux entre elles."""
    out: dict[str, bool] = {}
    marks = sorted({int(c) for c in np.asarray(cuts).tolist() if 0 <= int(c) < frames})
    cursor = 0
    for cut in marks:
        if cut > cursor:
            out[f"{cursor}-{cut - 1}"] = False
        out[f"{cut}-{cut}"] = True
        cursor = cut + 1
    if cursor < frames:
        out[f"{cursor}-{frames - 1}"] = False
    return out


def build_edit_plan(alignment: AlignmentMap, *, allow_fill: bool, active_area: ActiveAreaPlan | None,
                    level6: dict | None) -> EditPlan:
    """Plan d'édition ; images de Film 1 sans correspondance : automatiques seulement si noires et recopiant
    une image noire de Film 2, sinon refus sauf ``allow_fill`` (D1, avertissement par plage)."""
    warnings: list[str] = []
    refused: list[tuple[int, int]] = []
    for gap in alignment.uncovered:
        if gap.black and gap.source_black:
            continue
        if allow_fill:
            warnings.append(translate_text(
                "Images {first}–{last} de Film 1 sans correspondance : métadonnées de l'image {source} de Film 2 "
                "recopiées.", first=gap.start, last=gap.start + gap.length - 1, source=gap.source))
        else:
            refused.append((gap.start, gap.start + gap.length))
    if refused:
        shown = ", ".join(f"{a}–{b - 1}" for a, b in refused[:6]) + ("…" if len(refused) > 6 else "")
        raise EditRefused(translate_text(
            "{count} passage(s) de Film 1 sans correspondance dans Film 2 (images {ranges}) : aucune métadonnée "
            "n'est inventée sans l'option « Compléter les images sans correspondance ».", count=len(refused),
            ranges=shown), tuple(refused))

    n1 = alignment.film1_frames
    sources = np.full(n1, -1, dtype=np.int64)
    for seg in alignment.segments:
        sources[seg.film1_start:seg.film1_end] = np.arange(seg.film2_start, seg.film2_end)
    duplicates: list[tuple[int, int, int]] = []
    kept_before = 0          # images de Film 2 conservées avant la position courante (espace post-retrait)
    cursor = 0
    pieces: list[tuple[int, int, Uncovered | None]] = sorted(
        [(seg.film1_start, seg.length, None) for seg in alignment.segments]
        + [(gap.start, gap.length, gap) for gap in alignment.uncovered], key=lambda item: item[0])
    for start, length, piece in pieces:
        if start != cursor:
            raise AlignmentError("Carte d'alignement incohérente : segments et images sans correspondance ne "
                                 "couvrent pas Film 1 bout à bout.")
        cursor = start + length
        if piece is None:
            kept_before += length
            continue
        # Copie de l'image conservée qui précède (source < offset), sinon de celle qui suit (source = offset).
        source = kept_before - 1 if kept_before > 0 else kept_before
        duplicates.append((source, kept_before, length))
        sources[start:start + length] = piece.source
    if np.any(sources < 0) or cursor != n1:
        raise AlignmentError("Carte d'alignement incomplète : images de Film 1 sans source.")
    kept = alignment.film2_frames - sum(length for _start, length in alignment.removed)
    if kept + sum(length for _s, _o, length in duplicates) != n1:
        raise AlignmentError("Plan d'édition incohérent : compte final différent de Film 1.")
    return EditPlan(n1, alignment.film2_frames, tuple(alignment.removed), tuple(duplicates), sources,
                    np.asarray(alignment.expected_cuts, dtype=np.int64), active_area, level6, tuple(alignment.uncovered),
                    tuple(warnings))


# --- Application -------------------------------------------------------------------------------------------


def apply_rpu(plan: EditPlan, rpu_in: Path, work_dir: Path, *, dovi_tool: str, run: Runner) -> Path:
    """RPU de Film 2 → RPU de la sortie (passes séquentielles, chacune relue) ; rend le fichier final."""
    digests_in = rpu_digests(rpu_in)
    if len(digests_in) != plan.film2_frames:
        raise AlignmentError(f"RPU : {len(digests_in)} images pour {plan.film2_frames} attendues.")
    level5_in = _export_level5(rpu_in, work_dir / "align_in", dovi_tool, run, plan.film2_frames)
    current = rpu_in
    edit = plan.position_edit()
    if edit is not None:
        current = _run_editor(dovi_tool, current, edit, work_dir / "align_rpu_positions", run)
        got = rpu_digests(current)
        expected = [digests_in[int(j)] for j in plan.sources.tolist()]
        if got != expected:
            raise AlignmentError("RPU édité : images différentes de la projection prévue (contrat de dovi_tool ?).")
    cuts_now, _l5 = _export_cuts(current, work_dir / "align_mid", dovi_tool, run, plan.film1_frames)
    finish = plan.rpu_finish_edit(cuts_now)
    if finish is not None:
        before = _export_entries(current, work_dir / "align_mid_all.json", dovi_tool, run)
        current = _run_editor(dovi_tool, current, finish, work_dir / "align_rpu_final", run)
        after = _export_entries(current, work_dir / "align_final_all.json", dovi_tool, run)
        if [digest for digest, _l6 in after] != [digest for digest, _l6 in before]:
            raise AlignmentError("RPU final : données de rendu modifiées hors des champs édités (coupes, L5, L6).")
        wanted = None if plan.level6 is None else tuple(plan.level6[key] for key in _L6_KEYS)
        for index, ((_d, l6_after), (_e, l6_before)) in enumerate(zip(after, before)):
            if l6_after != (wanted if wanted is not None else l6_before):
                raise AlignmentError(f"RPU final : L6 inattendu à l'image {index} ({l6_after}).")
    cuts, level5 = _export_cuts(current, work_dir / "align_out", dovi_tool, run, plan.film1_frames)
    if len(rpu_digests(current)) != plan.film1_frames:
        raise AlignmentError("RPU final : nombre d'images différent de Film 1.")
    if not np.array_equal(cuts, plan.expected_cuts):
        raise AlignmentError("RPU final : coupes différentes de celles attendues.")
    if plan.active_area is not None and not plan.active_area.keep:
        expected_l5 = np.zeros((plan.film1_frames, 4), dtype=np.int32)
        for start, stop, area in plan.active_area.ranges:
            expected_l5[start:stop] = area
    else:
        expected_l5 = level5_in[plan.sources]
    if not np.array_equal(level5, expected_l5):
        raise AlignmentError("RPU final : zone active (L5) différente de celle prévue.")
    return current


_L6_KEYS = ("max_display_mastering_luminance", "min_display_mastering_luminance", "max_content_light_level",
            "max_frame_average_light_level")
#: Champs qu'une passe de finition a le droit de modifier (et qui ne participent donc pas à l'empreinte).
_EDITED_BLOCKS = frozenset({"Level5", "Level6"})


def normalized_entry(entry: dict) -> tuple[bytes, tuple[int, ...] | None]:
    """(empreinte d'une image d'export RPU sans les champs édités — coupe, L5, L6, nombre de blocs, CRC —,
    valeurs L6 de l'image ou None)."""
    entry = json.loads(json.dumps(entry))
    entry.pop("rpu_data_crc32", None)
    level6: tuple[int, ...] | None = None
    dm = entry.get("vdr_dm_data") or {}
    dm.pop("scene_refresh_flag", None)
    for name in ("cmv29_metadata", "cmv40_metadata"):
        meta = dm.get(name)
        if not isinstance(meta, dict):
            continue
        blocks = []
        for block in meta.get("ext_metadata_blocks") or []:
            if not isinstance(block, dict):
                continue
            if "Level6" in block and level6 is None:
                values = block["Level6"]
                level6 = tuple(int(values.get(key, 0)) for key in _L6_KEYS)
            if not (set(block) & _EDITED_BLOCKS):
                blocks.append(block)
        # dovi_tool retrie les blocs quand il remplace L5 / L6 : ordre canonique, contenu et multiplicité gardés.
        meta["ext_metadata_blocks"] = sorted(blocks, key=lambda item: json.dumps(item, sort_keys=True))
        meta.pop("num_ext_blocks", None)
    digest = hashlib.blake2b(json.dumps(entry, sort_keys=True).encode(), digest_size=16).digest()
    return digest, level6


def _export_entries(rpu: Path, target: Path, dovi_tool: str, run: Runner) -> list[tuple[bytes, tuple[int, ...] | None]]:
    """Export complet du RPU lu en flux (fichier supprimé ensuite)."""
    run([dovi_tool, "export", "-i", str(rpu), "-d", f"all={target}"])
    try:
        out = []
        for entry in iter_json_array(target):
            if not isinstance(entry, dict):
                raise AlignmentError("Export RPU illisible.")
            out.append(normalized_entry(entry))
        return out
    finally:
        target.unlink(missing_ok=True)


def apply_hdr10plus(plan: EditPlan, json_in: Path, work_dir: Path, *, hdr10plus_tool: str, run: Runner) -> Path:
    """JSON HDR10+ de Film 2 → JSON de la sortie (mêmes positions), relu image par image."""
    digests_in = hdr10plus_digests(json_in)
    if len(digests_in) != plan.film2_frames:
        raise AlignmentError(f"HDR10+ : {len(digests_in)} images pour {plan.film2_frames} attendues.")
    edit = plan.position_edit()
    if edit is None:
        return json_in
    config = work_dir / "align_hdr10plus_edit.json"
    config.write_text(json.dumps(edit), encoding="utf-8")
    out = work_dir / "align_hdr10plus.json"
    run([hdr10plus_tool, "editor", "-i", str(json_in), "-j", str(config), "-o", str(out)])
    config.unlink(missing_ok=True)
    if hdr10plus_digests(out) != [digests_in[int(j)] for j in plan.sources.tolist()]:
        raise AlignmentError("HDR10+ édité : images différentes de la projection prévue (contrat de hdr10plus_tool ?).")
    return out


def _run_editor(dovi_tool: str, rpu: Path, edit: dict, stem: Path, run: Runner) -> Path:
    config = stem.with_suffix(".json")
    config.write_text(json.dumps(edit), encoding="utf-8")
    out = stem.with_suffix(".bin")
    run([dovi_tool, "editor", "-i", str(rpu), "-j", str(config), "-o", str(out)])
    config.unlink(missing_ok=True)
    return out


def _export_cuts(rpu: Path, stem: Path, dovi_tool: str, run: Runner, frames: int) -> tuple[np.ndarray, np.ndarray]:
    scenes = stem.with_name(stem.name + "_scenes.txt")
    level5 = stem.with_name(stem.name + "_l5.json")
    run([dovi_tool, "export", "-i", str(rpu), "-d", f"scenes={scenes},level5={level5}"])
    cuts = np.asarray(sorted({int(v) for v in scenes.read_text(encoding="utf-8").split()}), dtype=np.int64)
    areas = l5_per_frame(level5, frames)
    scenes.unlink(missing_ok=True)
    level5.unlink(missing_ok=True)
    return cuts, areas


def _export_level5(rpu: Path, stem: Path, dovi_tool: str, run: Runner, frames: int) -> np.ndarray:
    return _export_cuts(rpu, stem, dovi_tool, run, frames)[1]
