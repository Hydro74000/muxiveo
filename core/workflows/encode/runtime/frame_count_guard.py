"""
core/workflows/encode/runtime/frame_count_guard.py

Audit d'alignement frame count entre source / encoded HEVC / RPU DoVi /
HDR10+ JSON.

Objectif : empêcher l'injection silencieuse de RPU/HDR10+ sur un stream
encodé qui aurait drop/dup une frame (NVENC en mode rapide, NVDEC sur
sources DV P7, filtres non-frame-preserving). Sans cette garde, le pipeline
produit un fichier décalé, les scènes-cuts DV/HDR10+ ne tombent plus aux bons
endroits.

Politique (explicite, choisie par l'appelant via :class:`MetadataAdjustment`) :
- Écart encoded vs source > 0 → abort (ré-encodage non frame-preserving).
- Métadonnées (RPU / HDR10+) de même nombre de trames que la vidéo → OK.
  Ce contrôle est un **comptage** : il ne prouve pas l'alignement temporel.
- ``EXACT`` : tout écart de métadonnées → abort.
- ``TRIM_TAIL`` : surplus ≤ tolérance (4) retiré **en fin de flux** (hypothèse
  affichée : début aligné) ; surplus > tolérance → abort.
- Métadonnées plus courtes que la vidéo → abort, quelle que soit la politique :
  aucune trame de métadonnées n'est fabriquée (pas de duplication).

Le trim HDR10+ tronque le JSON ; le trim RPU délègue à ``dovi_tool editor``
avec une plage ``remove`` exacte (dovi_tool ≥ 2.3 refuse une borne de fin
au-delà du nombre de trames).

Fallback frame count
====================
mediainfo est l'outil préféré (rapide, lit l'index sans décoder), mais on ne
veut pas en dépendre exclusivement. Cascade utilisée :

  1. mediainfo --Inform="Video;%FrameCount%"   (instantané si dispo)
  2. ffprobe -show_streams nb_frames           (instantané si déclaré dans
                                                le conteneur, ex MP4)
  3. ffprobe -count_packets -show_streams      (lecture des paquets sans décodage,
                                                équivalent au nb de frames
                                                pour HEVC vidéo)
  4. ffprobe -count_frames                     (lent — dernier recours,
                                                décode tout le stream)

Les étapes #3 et #4 parcourent le média entier ; #4 décode aussi la vidéo.
Si les comptes rapides divergent, on repart de #3 avant de refuser l'encodage.
"""

from __future__ import annotations

import json
import math
import re
import subprocess
from dataclasses import dataclass
from enum import Enum
from fractions import Fraction
from pathlib import Path
from typing import Callable

from core.frame_count import ffprobe_packet_count, frame_count_is_plausible, probe_duration_and_fps
from core.subprocess_utils import subprocess_text_kwargs


_FRAME_COUNT_FIELD = "Video;%FrameCount%"
_DEFAULT_TOLERANCE = 4
#: Flux HEVC bruts (annexB) : aucun index, un compte exact exige une lecture complète.
_RAW_HEVC_SUFFIXES = frozenset({".hevc", ".h265", ".265", ".x265"})


def _is_raw_hevc(path: Path) -> bool:
    return Path(path).suffix.lower() in _RAW_HEVC_SUFFIXES


class FrameCountAuditError(RuntimeError):
    """Erreur fatale d'alignement de frames — abort de l'injection."""


class MetadataAdjustment(Enum):
    """Ajustement autorisé quand RPU / HDR10+ n'ont pas le nombre de trames de la vidéo."""

    EXACT = "exact"          # aucun ajustement : égalité stricte des comptes
    TRIM_TAIL = "trim_tail"  # surplus ≤ tolérance retiré en fin de flux ; jamais de trame fabriquée


_METADATA_LABELS = {"rpu": "RPU Dolby Vision", "hdr10p": "HDR10+"}


def metadata_count_verdict(
    name: str,
    value: int,
    target: int,
    *,
    adjustment: MetadataAdjustment,
    tolerance: int = _DEFAULT_TOLERANCE,
) -> str | None:
    """Motif de refus pour des métadonnées de ``value`` trames face à ``target`` ; None si acceptable.

    Un surplus accepté (``TRIM_TAIL``) reste à retirer en fin de flux par l'appelant.
    """
    label = _METADATA_LABELS.get(name, name)
    delta = value - target
    if delta == 0:
        return None
    if delta < 0:
        return (
            f"{label} plus court que la vidéo : {value} trames contre {target} "
            f"({delta}) — aucune trame de métadonnées n'est fabriquée."
        )
    if adjustment is MetadataAdjustment.EXACT:
        return (
            f"{label} : {value} trames contre {target} (+{delta}) — aucun ajustement "
            "autorisé (politique exacte)."
        )
    if delta > tolerance:
        return (
            f"{label} : {value} trames contre {target} (+{delta}) — surplus au-delà de "
            f"la tolérance ({tolerance})."
        )
    return None


@dataclass(frozen=True)
class FrameCountAudit:
    source: int | None
    encoded: int | None
    rpu: int | None
    hdr10p: int | None
    #: Provenance du compte source : « video » (lu sur la source), « known »
    #: (fourni par l'appelant) ou « metadata » (RPU / HDR10+ extraits de la
    #: même source et égaux au flux encodé : source non relue).
    source_basis: str = "video"

    def deltas(self) -> dict[str, int]:
        """Renvoie les écarts vs source pour chaque flux disponible."""
        if self.source is None:
            return {}
        out: dict[str, int] = {}
        for name, value in (
            ("encoded", self.encoded),
            ("rpu", self.rpu),
            ("hdr10p", self.hdr10p),
        ):
            if value is not None:
                out[name] = value - self.source
        return out

    def is_aligned(
        self,
        *,
        adjustment: MetadataAdjustment = MetadataAdjustment.EXACT,
        tolerance: int = _DEFAULT_TOLERANCE,
    ) -> tuple[bool, str]:
        """
        Renvoie (ok, message) selon la même politique que :meth:`FrameCountGuard.enforce`
        (sans effet de bord) :
          - encoded == source (aucune tolérance sur le réencodage) ;
          - métadonnées acceptées par :func:`metadata_count_verdict`.
        Si source ou encoded sont inconnus → ok=False (audit incomplet).
        """
        if self.source is None or self.encoded is None:
            return (False, "frame count source ou encoded indéterminé")
        if self.encoded != self.source:
            return (
                False,
                f"frame count divergent : source={self.source} encoded={self.encoded} "
                f"(écart {self.encoded - self.source})",
            )
        for name, value in (("rpu", self.rpu), ("hdr10p", self.hdr10p)):
            if value is None:
                continue
            reason = metadata_count_verdict(
                name, value, self.source, adjustment=adjustment, tolerance=tolerance,
            )
            if reason is not None:
                return (False, reason)
        return (True, "comptages identiques (alignement temporel non vérifié)")


class FrameCountGuard:
    """
    Garde-fou d'alignement frame count post-encode.

    Utilisation :

        guard = FrameCountGuard(mediainfo_bin="mediainfo", dovi_tool_bin="dovi_tool")
        audit = guard.audit(
            source=source_mkv,
            encoded=enc_hevc,
            rpu_bin=rpu_bin if has_dv else None,
            hdr10p_json=hdr10p_json if has_hdr10p else None,
        )
        guard.enforce(audit, adjustment=MetadataAdjustment.TRIM_TAIL, on_warn=log_fn)
        # → lève FrameCountAuditError si désaligné, sinon trim auto.
    """

    def __init__(
        self,
        *,
        mediainfo_bin: str = "mediainfo",
        ffprobe_bin: str = "ffprobe",
        dovi_tool_bin: str = "dovi_tool",
        tolerance: int = _DEFAULT_TOLERANCE,
        run_command: Callable | None = None,
    ) -> None:
        self._mediainfo = mediainfo_bin
        self._ffprobe = ffprobe_bin
        self._dovi_tool = dovi_tool_bin
        self._tolerance = tolerance
        self._run_command = run_command

    def _run(self, command: list[str], **kwargs):
        return (self._run_command or subprocess.run)(command, **kwargs)

    # ------------------------------------------------------------------
    # Audit
    # ------------------------------------------------------------------

    def audit(
        self,
        *,
        source: Path,
        encoded: Path,
        rpu_bin: Path | None = None,
        hdr10p_json: Path | None = None,
        known_encoded_frames: int | None = None,
        known_source_frames: int | None = None,
        source_stream_index: int | None = None,
        frame_ratio: Fraction | int = 1,
    ) -> FrameCountAudit:
        """Compte les trames de chaque flux.

        ``frame_ratio`` (interpolation RIFE) : le compte source est rapporté
        à la cadence encodée (``ceil(source × rapport)``, règle de muxiveo-rife).
        ``known_source_frames`` : compte exact déjà établi (aucune relecture de
        la source, utile pour un flux brut volumineux).
        ``source_stream_index`` : index absolu de la piste sélectionnée ; une
        piste secondaire est comptée explicitement si les métadonnées ne
        prouvent pas déjà son compte (aucun repli sur la première vidéo).
        """
        ratio = Fraction(frame_ratio)
        source_known = known_source_frames is not None and known_source_frames > 0
        # HEVC brut ou piste secondaire : le lecteur rapide générique ne
        # prouve pas le compte ; attendre les métadonnées avant un scan complet.
        raw_source = not source_known and _is_raw_hevc(source)
        selected_source = source_stream_index is not None and source_stream_index != 0
        deferred_source = not source_known and (raw_source or selected_source)
        source_count: int | None
        if source_known:
            assert known_source_frames is not None
            source_count = math.ceil(known_source_frames * ratio)
        elif deferred_source:
            source_count = None
        else:
            source_count = self._read_video_frame_count(source)
            if source_count is not None:
                source_count = math.ceil(source_count * ratio)
        encoded_known = known_encoded_frames is not None and known_encoded_frames > 0
        encoded_count: int | None
        if encoded_known:
            encoded_count = known_encoded_frames
        else:
            encoded_count = self._read_video_frame_count(encoded)
        rpu_count = self._dovi_rpu_frame_count(rpu_bin) if rpu_bin else None
        hdr10p_count = self._hdr10p_json_frame_count(hdr10p_json) if hdr10p_json else None

        # RPU / HDR10+ extraits de la même source (lecture complète déjà faite
        # par dovi_tool / hdr10plus_tool) et égaux au flux encodé — compté
        # exactement (squelette de timing), jamais estimé : ils valent compte
        # source. Évite une lecture complète de la source quand son compte
        # rapide est impossible (HEVC brut) ou inexact (estimation durée ×
        # cadence d'un conteneur sans statistiques NUMBER_OF_FRAMES).
        metadata = [count for count in (rpu_count, hdr10p_count) if count is not None]
        metadata_agree = encoded_known and bool(metadata) and all(
            count == encoded_count for count in metadata
        )
        basis = "known" if source_known else "video"

        if deferred_source:
            if metadata_agree:
                source_count, basis = encoded_count, "metadata"
            else:
                # Désaccord ou pas de métadonnées : seul le comptage des
                # paquets est exact sur un flux brut.
                exact = (
                    ffprobe_packet_count(self._ffprobe, source, run_command=self._run_command, stream_index=source_stream_index)
                    if selected_source else self._recount_video_frames(source)
                )
                source_count = math.ceil(exact * ratio) if exact else None
        elif (
            not source_known
            and source_count is not None
            and encoded_count is not None
            and source_count != encoded_count
        ):
            if metadata_agree:
                # Compte rapide du conteneur contredit par les métadonnées de
                # la même source : estimation inexacte, pas de relecture complète.
                source_count, basis = encoded_count, "metadata"
            else:
                # Une estimation durée × cadence n'est pas une preuve exacte : une
                # coupe d'une image, une durée audio plus longue ou une durée absente
                # peuvent laisser passer des statistiques périmées. Recompter les
                # deux vidéos avant de conclure à une perte d'images à l'encodage.
                recounted = self._recount_video_frames(source)
                source_count = math.ceil(recounted * ratio) if recounted else source_count
                if not encoded_known:
                    encoded_count = self._recount_video_frames(encoded) or encoded_count
        if (
            (source_known or deferred_source)
            and not encoded_known
            and source_count is not None
            and encoded_count is not None
            and source_count != encoded_count
        ):
            # Compte source sûr (fourni ou compté exactement) : le compte rapide
            # du flux encodé qui le contredit est recompté exactement.
            encoded_count = self._recount_video_frames(encoded) or encoded_count
        return FrameCountAudit(
            source=source_count,
            encoded=encoded_count,
            rpu=rpu_count,
            hdr10p=hdr10p_count,
            source_basis=basis,
        )

    # ------------------------------------------------------------------
    # Application de la politique
    # ------------------------------------------------------------------

    def enforce(
        self,
        audit: FrameCountAudit,
        *,
        adjustment: MetadataAdjustment,
        rpu_bin: Path | None = None,
        hdr10p_json: Path | None = None,
        on_warn: Callable[[str], None] | None = None,
        on_info: Callable[[str], None] | None = None,
    ) -> FrameCountAudit:
        """
        Applique la politique ``adjustment`` (obligatoire : chaque appelant la choisit) :
          - audit vidéo incomplet (mediainfo et ffprobe en échec) → WARN, pas de blocage ;
          - HEVC encodé ≠ source → abort (frame-preserving requis) ;
          - métadonnées fournies mais comptage illisible → abort ;
          - métadonnées refusées par :func:`metadata_count_verdict` → abort ;
          - surplus accepté (``TRIM_TAIL``) → retiré en fin de flux puis recompté.

        Renvoie l'audit mis à jour après éventuel trim.
        """
        if audit.source is None or audit.encoded is None:
            # Aucun lecteur n'a réussi (mediainfo + ffprobe tous indisponibles
            # ou source illisible). Mode dégradé "best effort" avec WARN
            # plutôt que d'échouer : un drop NVENC silencieux ne sera pas
            # détecté, mais le workflow peut continuer.
            if on_warn:
                on_warn(
                    "Audit frame count ignoré : mediainfo et ffprobe ont tous "
                    "deux échoué à lire la frame count. Risque de "
                    "désalignement non détecté."
                )
            return audit
        if audit.encoded != audit.source:
            raise FrameCountAuditError(
                f"Encodage non frame-preserving : source={audit.source} frames, "
                f"encodé={audit.encoded} frames. Réessayez avec un preset "
                f"NVENC plus lent (p4/p5) ou un encodeur software."
            )

        target = audit.source
        counts = {"rpu": audit.rpu, "hdr10p": audit.hdr10p}
        files = {"rpu": rpu_bin, "hdr10p": hdr10p_json}
        for name, path in files.items():
            if path is not None and counts[name] is None:
                raise FrameCountAuditError(
                    f"Comptage {_METADATA_LABELS[name]} illisible ({path.name}) : "
                    "contrôle impossible, injection refusée."
                )
            value = counts[name]
            if value is None:
                continue
            reason = metadata_count_verdict(
                name, value, target, adjustment=adjustment, tolerance=self._tolerance,
            )
            if reason is not None:
                raise FrameCountAuditError(reason)

        if all(value is None or value == target for value in counts.values()):
            if on_info:
                basis = (
                    "flux encodé et métadonnées extraites de la source (source non relue)"
                    if audit.source_basis == "metadata"
                    else "vidéo et métadonnées"
                )
                on_info(
                    f"Comptage identique ({target} trames) : {basis}. "
                    "L'alignement temporel n'est pas vérifié par ce contrôle."
                )
            return audit

        # Surplus accepté par TRIM_TAIL : retrait en fin de flux puis recomptage.
        for name, value in counts.items():
            if value is None or value == target:
                continue
            path = files[name]
            if path is None:
                raise FrameCountAuditError(
                    f"{_METADATA_LABELS[name]} excédentaire mais fichier non fourni à enforce()."
                )
            if on_warn:
                on_warn(
                    f"{_METADATA_LABELS[name]} : {value - target} trame(s) excédentaire(s) "
                    f"retirée(s) en fin de flux ({value} → {target}). Hypothèse : début aligné."
                )
            if name == "rpu":
                self._trim_rpu(path, target_frames=target, current_frames=value)
                counts[name] = self._dovi_rpu_frame_count(path)
            else:
                self._trim_hdr10p_json(path, target_frames=target)
                counts[name] = self._hdr10p_json_frame_count(path)
            if counts[name] != target:
                raise FrameCountAuditError(
                    f"{_METADATA_LABELS[name]} : coupe en fin de flux sans effet "
                    f"({counts[name]} trames après coupe, {target} attendues)."
                )

        return FrameCountAudit(
            source=audit.source,
            encoded=audit.encoded,
            rpu=counts["rpu"],
            hdr10p=counts["hdr10p"],
            source_basis=audit.source_basis,
        )

    def rpu_frame_count(self, rpu_bin: Path) -> int | None:
        """Nombre de trames d'un RPU binaire (``dovi_tool info --summary``)."""
        return self._dovi_rpu_frame_count(rpu_bin)

    def hdr10p_frame_count(self, hdr10p_json: Path) -> int | None:
        """Nombre de trames d'un JSON ``hdr10plus_tool extract``."""
        return self._hdr10p_json_frame_count(hdr10p_json)

    # ------------------------------------------------------------------
    # Lecteurs de frame count
    # ------------------------------------------------------------------

    def _recount_video_frames(self, path: Path) -> int | None:
        """Recompte sans utiliser les statistiques des en-têtes (par paquets conteneur)."""
        count = self._ffprobe_count_packets(path)
        if count is not None and count > 0:
            return count
        return None

    def _read_video_frame_count(self, path: Path) -> int | None:
        """
        Cascade : mediainfo → ffprobe nb_frames → ffprobe count_packets →
        ffprobe count_frames. Renvoie la première valeur cohérente trouvée,
        None si tout a échoué.
        """
        for reader in (
            self._mediainfo_frame_count,
            self._ffprobe_nb_frames,
            self._ffprobe_count_packets,
            self._ffprobe_count_frames,
        ):
            value = reader(path)
            if value is not None and value > 0:
                return value
        return None

    def _mediainfo_frame_count(self, path: Path) -> int | None:
        try:
            result = self._run(
                [self._mediainfo, f"--Inform={_FRAME_COUNT_FIELD}", str(path)],
                capture_output=True,
                check=False,
                **subprocess_text_kwargs(),
            )
        except (FileNotFoundError, OSError):
            return None
        if result.returncode != 0:
            return None
        raw = (result.stdout or "").strip()
        if re.fullmatch(r"\d+", raw):
            count = int(raw)
            # Tags de statistiques Matroska périmés (fichier coupé/remuxé) :
            # valeur écartée, la cascade passe au comptage ffprobe.
            duration, fps = probe_duration_and_fps(self._ffprobe, path, run_command=self._run_command)
            return count if frame_count_is_plausible(count, duration, fps) else None
        return None

    def _ffprobe_nb_frames(self, path: Path) -> int | None:
        """
        Lecture directe de ``nb_frames`` dans le conteneur ou tags de statistiques.
        Instantané quand le muxer le déclare (toujours en MP4 ; en MKV via tags
        NUMBER_OF_FRAMES ou header — on tente, on saute si absent).
        """
        try:
            result = self._run(
                [
                    self._ffprobe, "-v", "error",
                    "-select_streams", "v:0",
                    "-show_entries", "stream=nb_frames:stream_tags=NUMBER_OF_FRAMES",
                    "-of", "default=noprint_wrappers=1",
                    str(path),
                ],
                capture_output=True,
                check=False,
                **subprocess_text_kwargs(),
            )
        except (FileNotFoundError, OSError):
            return None
        if result.returncode != 0:
            return None
        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            val = line.split("=", 1)[-1].strip() if "=" in line else line
            if re.fullmatch(r"\d+", val):
                count = int(val)
                if count > 0:
                    return count
        return None

    def _ffprobe_count_packets(self, path: Path) -> int | None:
        """
        Parcourt les paquets sans décoder (pas uniquement l'index).
        Pour HEVC, un paquet correspond à une access unit, donc une image.
        Le lecteur partagé rejette un compte partiel si ffprobe échoue.
        """
        return ffprobe_packet_count(self._ffprobe, path, run_command=self._run_command)

    def _ffprobe_count_frames(self, path: Path) -> int | None:
        """
        Dernier recours : ``-count_frames`` décode tout le stream. Lent (du
        même ordre que l'encode lui-même). Activé seulement si les méthodes
        plus rapides ont échoué. Un échec du processus invalide aussi le compte.
        """
        try:
            result = self._run(
                [
                    self._ffprobe, "-v", "error",
                    "-select_streams", "v:0",
                    "-count_frames",
                    "-show_entries", "stream=nb_read_frames",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ],
                capture_output=True,
                check=False,
                **subprocess_text_kwargs(),
            )
        except (FileNotFoundError, OSError):
            return None
        if result.returncode != 0:
            return None
        raw = (result.stdout or "").strip()
        if re.fullmatch(r"\d+", raw):
            return int(raw)
        return None

    def _dovi_rpu_frame_count(self, rpu_bin: Path) -> int | None:
        try:
            result = self._run(
                [self._dovi_tool, "info", "-i", str(rpu_bin), "--summary"],
                capture_output=True,
                check=False,
                **subprocess_text_kwargs(),
            )
        except (FileNotFoundError, OSError):
            return None
        text = (result.stdout or "") + (result.stderr or "")
        # dovi_tool affiche typiquement "Frames: 191733" ou "Total frames: 191733".
        match = re.search(r"(?:Total\s+frames|Frames)\s*:\s*(\d+)", text, re.IGNORECASE)
        if match:
            return int(match.group(1))
        return None

    def _hdr10p_json_frame_count(self, hdr10p_json: Path) -> int | None:
        try:
            data = json.loads(hdr10p_json.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        scene_info = data.get("SceneInfo")
        if isinstance(scene_info, list):
            return len(scene_info)
        return None

    # ------------------------------------------------------------------
    # Trims
    # ------------------------------------------------------------------

    def _trim_hdr10p_json(self, hdr10p_json: Path, *, target_frames: int) -> None:
        """Retire les trames HDR10+ au-delà de ``target_frames`` (fin de flux) et met le résumé à jour."""
        data = json.loads(hdr10p_json.read_text(encoding="utf-8"))
        scene_info = data.get("SceneInfo")
        if not isinstance(scene_info, list):
            return
        if len(scene_info) <= target_frames:
            return
        data["SceneInfo"] = scene_info[:target_frames]
        # Le résumé ne doit référencer que des trames conservées : premières
        # trames de scène filtrées, longueurs de scène recalculées.
        summary = data.get("SceneInfoSummary")
        if isinstance(summary, dict):
            firsts = summary.get("SceneFirstFrameIndex")
            if isinstance(firsts, list):
                kept = [idx for idx in firsts if isinstance(idx, int) and idx < target_frames]
                summary["SceneFirstFrameIndex"] = kept
                if isinstance(summary.get("SceneFrameNumbers"), list):
                    bounds = [*kept, target_frames]
                    summary["SceneFrameNumbers"] = [bounds[i + 1] - bounds[i] for i in range(len(kept))]
        hdr10p_json.write_text(
            json.dumps(data, separators=(",", ":")),
            encoding="utf-8",
        )

    def _trim_rpu(self, rpu_bin: Path, *, target_frames: int, current_frames: int) -> None:
        """
        Retire les trames RPU ``target_frames`` … ``current_frames - 1`` (fin de flux)
        via ``dovi_tool editor``.

        La plage est exacte : dovi_tool ≥ 2.3 refuse une borne de fin au-delà
        du nombre de trames (``invalid end range``).
        """
        if current_frames <= target_frames:
            return
        edit_path = rpu_bin.with_suffix(".edit.json")
        edit_payload = {"remove": [f"{target_frames}-{current_frames - 1}"]}
        edit_path.write_text(
            json.dumps(edit_payload, separators=(",", ":")),
            encoding="utf-8",
        )
        out_path = rpu_bin.with_suffix(".trimmed.bin")
        try:
            self._run(
                [
                    self._dovi_tool,
                    "editor",
                    "-i", str(rpu_bin),
                    "-j", str(edit_path),
                    "-o", str(out_path),
                ],
                capture_output=True,
                check=True,
                **subprocess_text_kwargs(),
            )
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            out_path.unlink(missing_ok=True)
            raise FrameCountAuditError(
                f"dovi_tool editor a échoué pour le trim RPU : {stderr}"
            ) from exc
        finally:
            edit_path.unlink(missing_ok=True)
        # Remplacement atomique du RPU par la version trimmée.
        out_path.replace(rpu_bin)


__all__ = [
    "FrameCountAudit",
    "FrameCountAuditError",
    "FrameCountGuard",
    "MetadataAdjustment",
    "metadata_count_verdict",
]
