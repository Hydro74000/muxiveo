"""Validation sémantique d'une sortie Matroska (contrat minimal de succès).

Le validateur interne (structure EBML, pistes, paquets, durée, métadonnées)
est obligatoire ; ``ffprobe`` reste un second validateur côté appelant, pas
la source unique de vérité.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import contract as _contract
from .ids import CHAPTERS_ID, CLUSTER_ID, INFO_ID, TAGS_ID, TRACKS_ID
from .language import matroska_legacy_language
from .reader import MatroskaAttachmentHeader, MatroskaReader, MatroskaTrack


#: Types de piste Matroska → libellés publics du remux.
TRACK_TYPE_LABELS: dict[int, str] = {1: "video", 2: "audio", 17: "subtitle"}

#: Types de piste considérés comme média (au moins un paquet exigé).
_MEDIA_TRACK_TYPES = frozenset({1, 2})


@dataclass(frozen=True)
class MatroskaPacketValidation:
    """Résumé des blocs effectivement écrits par :class:`MatroskaWriter`.

    Le writer construit ce résumé pendant son unique parcours des paquets.
    Le réutiliser évite de relire et décoder chaque bloc du fichier juste
    après son écriture, tout en préservant les contrôles de présence des
    paquets et de cohérence de durée.
    """

    track_numbers: frozenset[int]
    max_packet_timestamp_ns: int | None
    last_delta_by_track: dict[int, int]
    #: Entrelacement mesuré pendant l'écriture (avertissement seulement).
    interleave: "InterleaveReport | None" = None


#: Retard d'écriture au-delà duquel l'entrelacement est signalé : un lecteur
#: qui se place à un instant donné (Cues vidéo) ne retrouve plus les paquets
#: d'une piste écrits beaucoup plus tôt ou plus tard dans le fichier.
INTERLEAVE_WARN_LAG_NS = 2_000_000_000


@dataclass(frozen=True)
class InterleaveReport:
    """Entrelacement d'une sortie : retard d'écriture des paquets, par piste.

    Retard d'un paquet = plus grand timestamp déjà écrit (toutes pistes) − son
    timestamp. Une piste écrite d'un bloc en avance fait apparaître les autres
    en retard : la piste signalée est celle dont les paquets arrivent trop tard.
    """

    max_lag_ns: dict[int, int] = field(default_factory=dict)
    lag_at_ns: dict[int, int] = field(default_factory=dict)
    lagged_packets: dict[int, int] = field(default_factory=dict)
    backward_clusters: int = 0
    threshold_ns: int = INTERLEAVE_WARN_LAG_NS


class InterleaveTracker:
    """Accumule l'entrelacement dans l'ordre d'écriture (writer natif ou relecture)."""

    def __init__(self, threshold_ns: int = INTERLEAVE_WARN_LAG_NS) -> None:
        self._threshold = threshold_ns
        self._highest: int | None = None
        self._highest_cluster: int | None = None
        self._max_lag: dict[int, int] = {}
        self._lag_at: dict[int, int] = {}
        self._lagged: dict[int, int] = {}
        self._backward_clusters = 0

    def packet(self, track_number: int, timestamp_ns: int) -> None:
        if self._highest is None or timestamp_ns > self._highest:
            self._highest = timestamp_ns
        lag = self._highest - timestamp_ns
        if lag > self._max_lag.get(track_number, -1):
            self._max_lag[track_number] = lag
            self._lag_at[track_number] = timestamp_ns
        if lag > self._threshold:
            self._lagged[track_number] = self._lagged.get(track_number, 0) + 1

    def cluster(self, timestamp_ns: int) -> None:
        if self._highest_cluster is not None and timestamp_ns < self._highest_cluster - self._threshold:
            self._backward_clusters += 1
        if self._highest_cluster is None or timestamp_ns > self._highest_cluster:
            self._highest_cluster = timestamp_ns

    def report(self) -> InterleaveReport:
        return InterleaveReport(
            max_lag_ns=dict(self._max_lag), lag_at_ns=dict(self._lag_at),
            lagged_packets=dict(self._lagged), backward_clusters=self._backward_clusters,
            threshold_ns=self._threshold,
        )


def _seconds(value_ns: int) -> str:
    return f"{value_ns / 1_000_000_000:.1f}".replace(".", ",") + " s"


def interleave_warnings(
    report: InterleaveReport | None,
    *,
    track_labels: Mapping[int, str] | None = None,
) -> list[str]:
    """Avertissements d'entrelacement (jamais bloquants : la sortie reste publiée)."""
    if report is None:
        return []
    labels = track_labels or {}
    messages = []
    for track_number in sorted(report.lagged_packets):
        label = labels.get(track_number, f"piste #{track_number}")
        messages.append(
            f"Entrelacement : {label} écrite jusqu'à {_seconds(report.max_lag_ns[track_number])} après les "
            f"autres pistes (à {_seconds(report.lag_at_ns[track_number])} du film, "
            f"{report.lagged_packets[track_number]} paquet(s) au-delà de {_seconds(report.threshold_ns)}) : "
            "après un déplacement, un lecteur peut perdre cette piste."
        )
    if report.backward_clusters:
        messages.append(
            f"Entrelacement : {report.backward_clusters} Cluster(s) revenant de plus de "
            f"{_seconds(report.threshold_ns)} en arrière dans le temps."
        )
    return messages


_TRACK_KIND_LABELS = {1: "vidéo", 2: "audio", 17: "sous-titres"}


def track_label(number: int, track_type: int, language: str = "", name: str = "") -> str:
    """Libellé d'une piste de sortie pour les messages (numéro, nature, langue, nom)."""
    details = " ".join(part for part in (_TRACK_KIND_LABELS.get(track_type, ""), language) if part and part != "und")
    if name:
        details = f"{details} « {name} »" if details else f"« {name} »"
    return f"piste #{number}" + (f" ({details})" if details else "")


def mux_track_labels(tracks: Iterable[Any]) -> dict[int, str]:
    """Libellés des pistes d'un plan d'écriture natif (``MatroskaMuxTrack``)."""
    labels: dict[int, str] = {}
    for track in tracks:
        source = track.source_track
        language = (track.language_bcp47 or track.language) if track.patch_language else (
            source.language_bcp47 or source.language
        )
        name = track.name if track.patch_name else source.name
        labels[track.output_number] = track_label(track.output_number, source.track_type, language or "", name or "")
    return labels


def file_track_labels(path: Path) -> dict[int, str]:
    """Libellés des pistes d'un fichier écrit (sorties FFmpeg)."""
    try:
        tracks = MatroskaReader(path).tracks()
    except (OSError, ValueError):
        return {}
    return {
        track.number: track_label(track.number, track.track_type, track.language_bcp47 or track.language, track.name)
        for track in tracks
    }


def scan_interleave(path: Path) -> InterleaveReport:
    """Entrelacement d'un fichier existant (sorties FFmpeg) : en-têtes seuls, sans payload."""
    reader = MatroskaReader(path)
    tracker = InterleaveTracker()
    with path.open("rb") as fh:
        for item in reader.top_level():
            if item.element_id != CLUSTER_ID:
                continue
            fh.seek(item.payload_offset)
            head = fh.read(16)
            if len(head) >= 3 and head[0] == 0xE7:
                size_length = 9 - head[1].bit_length()
                size = int.from_bytes(head[1:1 + size_length], "big") & ((1 << (7 * size_length)) - 1)
                start = 1 + size_length
                if 0 < size <= 8 and start + size <= len(head):
                    tracker.cluster(int.from_bytes(head[start:start + size], "big") * reader.timestamp_scale_ns())
    for block in reader.blocks(read_payload=False):
        timestamp = block.timestamp_ns if block.timestamp_ns is not None else block.timestamp_ms * 1_000_000
        tracker.packet(block.track_number, timestamp)
    return tracker.report()


#: Clusters de fin sondés pour l'horodatage du dernier paquet. Les Clusters
#: étant ordonnés par horodatage croissant, le paquet le plus tardif est dans
#: les tout derniers ; la marge couvre l'entrelacement des pistes.
_TAIL_CLUSTER_PROBE = 8


def _probe_packet_validation(
    reader: MatroskaReader,
    media_numbers: set[int],
    *,
    workers: int,
) -> MatroskaPacketValidation:
    """Résume les paquets sans relire l'intégralité du fichier.

    Deux sondages suffisent au contrat : les Clusters de fin donnent
    l'horodatage du dernier paquet (et l'écart final servant de marge), le
    parcours depuis le début s'arrête dès que chaque piste média attendue a
    livré un paquet. Une piste réellement vide fait donc parcourir tout le
    fichier — la détection reste exacte, seul le cas nominal est raccourci.
    """
    clusters = reader.cluster_elements()
    max_packet_timestamp_ns: int | None = None
    last_delta_by_track: dict[int, int] = {}
    last_timestamp_by_track: dict[int, int] = {}
    seen: set[int] = set()

    def account(block) -> None:
        nonlocal max_packet_timestamp_ns
        seen.add(block.track_number)
        previous = last_timestamp_by_track.get(block.track_number)
        if previous is not None and block.timestamp_ns > previous:
            # Marge conservée au maximum des écarts observés : un sondage de
            # début ne doit jamais réduire la tolérance déduite de la fin.
            last_delta_by_track[block.track_number] = max(
                last_delta_by_track.get(block.track_number, 0),
                block.timestamp_ns - previous,
            )
        last_timestamp_by_track[block.track_number] = block.timestamp_ns
        packet_end = block.timestamp_ns + (block.duration_ns or 0)
        max_packet_timestamp_ns = max(max_packet_timestamp_ns or packet_end, packet_end)

    for block in reader.block_summaries(clusters=clusters[-_TAIL_CLUSTER_PROBE:], workers=workers):
        account(block)

    remaining = set(media_numbers) - seen
    if remaining:
        last_timestamp_by_track.clear()
        for block in reader.block_summaries(clusters=clusters, workers=workers):
            account(block)
            remaining.discard(block.track_number)
            if not remaining:
                break

    return MatroskaPacketValidation(
        track_numbers=frozenset(seen),
        max_packet_timestamp_ns=max_packet_timestamp_ns,
        last_delta_by_track=last_delta_by_track,
    )


def _normalized_language(value: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        return ""
    try:
        return matroska_legacy_language(cleaned)
    except Exception:
        return cleaned.lower()


def _attachment_matches(
    expected: _contract.ExpectedMatroskaAttachment,
    observed: MatroskaAttachmentHeader,
) -> bool:
    """True si un header satisfait toutes les contraintes de l'attente."""
    return (
        observed.name == expected.name
        and (expected.media_type is None or observed.media_type == expected.media_type)
        and (expected.uid is None or observed.uid == expected.uid)
        and (expected.size is None or observed.size == expected.size)
    )


def _attachment_mismatch_count(
    expected: _contract.ExpectedMatroskaAttachment,
    observed: MatroskaAttachmentHeader,
) -> int:
    return sum((
        expected.media_type is not None and observed.media_type != expected.media_type,
        expected.uid is not None and observed.uid != expected.uid,
        expected.size is not None and observed.size != expected.size,
    ))


def _match_expected_attachments(
    expected: list[_contract.ExpectedMatroskaAttachment],
    observed: list[MatroskaAttachmentHeader],
) -> dict[int, int]:
    """Appariement biparti attente→header, indispensable pour les homonymes."""
    candidates = {
        expected_index: [
            observed_index
            for observed_index, header in enumerate(observed)
            if _attachment_matches(item, header)
        ]
        for expected_index, item in enumerate(expected)
    }
    observed_owner: dict[int, int] = {}

    def _assign(expected_index: int, seen: set[int]) -> bool:
        for observed_index in candidates[expected_index]:
            if observed_index in seen:
                continue
            seen.add(observed_index)
            owner = observed_owner.get(observed_index)
            if owner is None or _assign(owner, seen):
                observed_owner[observed_index] = expected_index
                return True
        return False

    # Les attentes les plus contraintes (peu de candidats) passent d'abord ;
    # les chemins augmentants corrigent ensuite les choix ambigus.
    for expected_index in sorted(candidates, key=lambda index: len(candidates[index])):
        _assign(expected_index, set())
    return {
        expected_index: observed_index
        for observed_index, expected_index in observed_owner.items()
    }


def validate_matroska_output(
    path: Path,
    contract: _contract.MatroskaOutputContract | None = None,
    *,
    check_packets: bool = True,
    packet_validation: MatroskaPacketValidation | None = None,
    packet_scan_workers: int | None = None,
) -> list[str]:
    """Vérifie le contrat minimal de succès sur ``path``. Retourne les erreurs.

    Sans contrat, seule la validité structurelle (EBML lisible, au moins une
    piste, durée non négative) est vérifiée — c'est la validation « légère »
    utilisée par le backend FFmpeg.

    Sans ``packet_validation`` fourni par l'écrivain, les paquets sont sondés
    (voir :func:`_probe_packet_validation`) plutôt que relus intégralement.
    """
    if packet_scan_workers is None:
        packet_scan_workers = 1
    errors: list[str] = []
    if not path.is_file():
        return [f"Sortie candidate absente : {path}"]
    try:
        reader = MatroskaReader(path)
        reader.segment()
        tracks = reader.tracks()
    except (OSError, ValueError) as exc:
        return [f"Structure EBML illisible : {exc}"]
    if not tracks:
        errors.append("Aucune piste Matroska dans la sortie.")
        return errors

    try:
        top_level = reader.top_level()
        first_cluster_offset = next(
            (el.offset for el in top_level if el.element_id == CLUSTER_ID), None
        )
        if first_cluster_offset is not None:
            for el in top_level:
                if el.element_id in (INFO_ID, TRACKS_ID) and el.offset > first_cluster_offset:
                    errors.append(
                        f"Élément conteneur {el.element_id.hex()} situé après le premier Cluster "
                        f"(offset {el.offset} > {first_cluster_offset}) : disposition invalide pour les lecteurs."
                    )
    except (OSError, ValueError) as exc:
        errors.append(f"Structure top-level illisible : {exc}")

    try:
        duration_ns = reader.segment_duration_ns()
    except (OSError, ValueError) as exc:
        errors.append(f"Durée du segment illisible : {exc}")
        duration_ns = None
    if duration_ns is not None and duration_ns < 0:
        errors.append(f"Durée du segment négative : {duration_ns} ns")

    if contract is None:
        return errors

    observed_types = [TRACK_TYPE_LABELS.get(track.track_type, str(track.track_type)) for track in tracks]
    expected_track_contracts = list(contract.expected_tracks)
    expected_types = (
        [track.track_type for track in expected_track_contracts]
        if expected_track_contracts else list(contract.track_types)
    )
    if contract.allow_unexpected_subtitles:
        # Copie de sous-titres via mapping optionnel sans pré-scan complet :
        # les sous-titres observés ne sont pas prévisibles — seuls les autres
        # types de pistes restent comparés strictement.
        comparable_observed = [item for item in observed_types if item != "subtitle"]
        comparable_expected = [item for item in expected_types if item != "subtitle"]
    else:
        comparable_observed = observed_types
        comparable_expected = expected_types
    if comparable_observed != comparable_expected:
        errors.append(
            "Pistes de sortie inattendues : "
            f"attendu {expected_types}, obtenu {observed_types}"
        )

    # Appariement attendu → observé pour la validation détaillée. Avec
    # ``allow_unexpected_subtitles``, les pistes hors sous-titres restent
    # appariées et validées même quand des sous-titres inconnus s'intercalent
    # (une sortie DoVi sans BlockAdditionMapping ne doit jamais passer) ; les
    # sous-titres attendus sont appariés dans l'ordre aux premiers observés.
    paired_tracks: list[tuple[_contract.ExpectedMatroskaTrack, MatroskaTrack]] = []
    if expected_track_contracts:
        if contract.allow_unexpected_subtitles:
            expected_regular = [item for item in expected_track_contracts if item.track_type != "subtitle"]
            expected_subtitles = [item for item in expected_track_contracts if item.track_type == "subtitle"]
            observed_regular = [
                track for track in tracks
                if TRACK_TYPE_LABELS.get(track.track_type, str(track.track_type)) != "subtitle"
            ]
            observed_subtitles = [
                track for track in tracks
                if TRACK_TYPE_LABELS.get(track.track_type, str(track.track_type)) == "subtitle"
            ]
            if len(expected_regular) == len(observed_regular):
                paired_tracks.extend(zip(expected_regular, observed_regular))
                if len(observed_subtitles) >= len(expected_subtitles):
                    paired_tracks.extend(zip(expected_subtitles, observed_subtitles))
                elif expected_subtitles:
                    errors.append(
                        "Sous-titres attendus manquants : "
                        f"{len(expected_subtitles)} attendus, {len(observed_subtitles)} observés"
                    )
        elif len(expected_track_contracts) == len(tracks):
            paired_tracks.extend(zip(expected_track_contracts, tracks))

    if paired_tracks:
        for index, (expected, track) in enumerate(paired_tracks, start=1):
            if expected.name is not None and track.name != expected.name:
                errors.append(
                    f"Nom de piste #{index} inattendu : "
                    f"attendu {expected.name!r}, obtenu {track.name!r}"
                )
            if expected.language is not None:
                expected_normalized = _normalized_language(expected.language)
                observed_normalized = _normalized_language(track.language_bcp47 or track.language)
                if observed_normalized != expected_normalized:
                    errors.append(
                        f"Langue de piste #{index} inattendue : "
                        f"attendu {expected_normalized!r}, obtenu {observed_normalized!r}"
                    )
            if expected.flags is not None:
                for field_name in (
                    "enabled", "default", "forced", "hearing_impaired",
                    "visual_impaired", "original", "commentary",
                ):
                    wanted = getattr(expected.flags, field_name)
                    if wanted is None:
                        continue
                    observed = getattr(track, f"flag_{field_name}")
                    if observed != wanted:
                        errors.append(
                            f"Flag {field_name} de piste #{index} inattendu : "
                            f"attendu {wanted}, obtenu {observed}"
                        )
            if expected.require_block_addition_mapping and not track.block_addition_mappings:
                errors.append(f"BlockAdditionMapping requis mais absent de la piste #{index}.")
    elif contract.track_names and len(contract.track_names) == len(tracks):
        for index, (expected_name, track) in enumerate(zip(contract.track_names, tracks), start=1):
            if expected_name and track.name != expected_name:
                errors.append(
                    f"Nom de piste #{index} inattendu : "
                    f"attendu {expected_name!r}, obtenu {track.name!r}"
                )
    if not expected_track_contracts and contract.track_languages and len(contract.track_languages) == len(tracks):
        for index, (expected_language, track) in enumerate(zip(contract.track_languages, tracks), start=1):
            expected_normalized = _normalized_language(expected_language)
            if expected_normalized in {"", "und"}:
                continue
            observed_normalized = _normalized_language(track.language_bcp47 or track.language)
            if observed_normalized != expected_normalized:
                errors.append(
                    f"Langue de piste #{index} inattendue : "
                    f"attendu {expected_normalized!r}, obtenu {observed_normalized!r}"
                )

    if check_packets or contract.duration_coherent:
        if paired_tracks:
            media_numbers = {
                track.number
                for expected, track in paired_tracks
                if expected.require_packets
            }
            media_labels = {
                track.number: (
                    f"#{track.number} ({expected.track_type}, position de sortie #{position})"
                )
                for position, (expected, track) in enumerate(paired_tracks, start=1)
                if expected.require_packets
            }
        else:
            media_numbers = {
                track.number for track in tracks if track.track_type in _MEDIA_TRACK_TYPES
            }
            media_labels = {
                track.number: (
                    f"#{track.number} ({TRACK_TYPE_LABELS.get(track.track_type, str(track.track_type))}, "
                    f"position de sortie #{position})"
                )
                for position, track in enumerate(tracks, start=1)
                if track.track_type in _MEDIA_TRACK_TYPES
            }
        last_delta_by_track: dict[int, int]
        if packet_validation is not None:
            media_numbers.difference_update(packet_validation.track_numbers)
            max_packet_timestamp_ns = packet_validation.max_packet_timestamp_ns
            last_delta_by_track = packet_validation.last_delta_by_track
        else:
            max_packet_timestamp_ns = None
            last_delta_by_track = {}
            try:
                probe = _probe_packet_validation(
                    reader, set(media_numbers), workers=packet_scan_workers,
                )
                media_numbers.difference_update(probe.track_numbers)
                max_packet_timestamp_ns = probe.max_packet_timestamp_ns
                last_delta_by_track = probe.last_delta_by_track
            except (OSError, ValueError) as exc:
                errors.append(f"Blocs Matroska illisibles : {exc}")
        if check_packets and media_numbers:
            errors.append(
                "Aucun paquet écrit pour les pistes média : "
                + ", ".join(media_labels[number] for number in sorted(media_numbers))
            )
        final_packet_slack_ns = max(last_delta_by_track.values(), default=0)
        scale_ns = reader.timestamp_scale_ns()
        if (
            contract.duration_coherent
            and duration_ns is not None
            and max_packet_timestamp_ns is not None
            and not (
                max_packet_timestamp_ns - scale_ns
                <= duration_ns
                <= max_packet_timestamp_ns + final_packet_slack_ns + scale_ns
            )
        ):
            errors.append(
                "Durée du segment incohérente : "
                f"Info={duration_ns} ns, dernier paquet={max_packet_timestamp_ns} ns"
            )

    if contract.expects_chapters and not reader.raw_top_level(CHAPTERS_ID):
        errors.append("Chapitres attendus mais absents de la sortie.")
    if contract.expects_tags and not reader.raw_top_level(TAGS_ID):
        errors.append("Balises (Tags) attendues mais absentes de la sortie.")

    if contract.attachment_names or contract.expected_attachments or contract.strict_attachment_names:
        try:
            observed_headers = reader.attachment_headers()
            observed_attachments = [attachment.name for attachment in observed_headers]
        except (OSError, ValueError) as exc:
            observed_headers = []
            observed_attachments = []
            errors.append(f"Attachments illisibles : {exc}")
        expected_names = list(contract.attachment_names) or [
            attachment.name for attachment in contract.expected_attachments
        ]
        observed_counts = Counter(observed_attachments)
        expected_counts = Counter(expected_names)
        missing = list((expected_counts - observed_counts).elements())
        if missing:
            errors.append("Attachments attendus mais absents : " + ", ".join(missing))
        if contract.strict_attachment_names:
            unexpected = sorted((observed_counts - expected_counts).elements())
            if unexpected:
                errors.append("Attachments inattendus : " + ", ".join(unexpected))
        detailed_expectations = list(contract.expected_attachments)
        matched_attachments = _match_expected_attachments(
            detailed_expectations, observed_headers,
        )
        for expected_index, expected_attachment in enumerate(detailed_expectations):
            observed_index = matched_attachments.get(expected_index)
            if observed_index is None:
                same_name = [
                    index
                    for index, header in enumerate(observed_headers)
                    if header.name == expected_attachment.name
                ]
                if not same_name:
                    continue
                # Aucun appariement exact : comparer au candidat homonyme le
                # plus proche afin de conserver des diagnostics précis.
                observed_index = min(
                    same_name,
                    key=lambda index: _attachment_mismatch_count(
                        expected_attachment, observed_headers[index],
                    ),
                )
            observed = observed_headers[observed_index]
            if observed.name != expected_attachment.name:
                continue
            if (
                expected_attachment.media_type is not None
                and observed.media_type != expected_attachment.media_type
            ):
                errors.append(
                    f"MIME de l'attachment {expected_attachment.name!r} inattendu : "
                    f"attendu {expected_attachment.media_type!r}, "
                    f"obtenu {observed.media_type!r}"
                )
            if expected_attachment.uid is not None and observed.uid != expected_attachment.uid:
                errors.append(
                    f"UID de l'attachment {expected_attachment.name!r} inattendu : "
                    f"attendu {expected_attachment.uid}, obtenu {observed.uid}"
                )
            if expected_attachment.size is not None and observed.size != expected_attachment.size:
                errors.append(
                    f"Taille de l'attachment {expected_attachment.name!r} inattendue : "
                    f"attendu {expected_attachment.size}, obtenu {observed.size}"
                )

    if contract.require_block_addition_mapping:
        has_mapping = any(
            track.block_addition_mappings
            for track in tracks
            if track.track_type == 1
        )
        if not has_mapping:
            errors.append("BlockAdditionMapping requis mais absent des pistes vidéo.")

    return errors


__all__ = [
    "INTERLEAVE_WARN_LAG_NS",
    "InterleaveReport",
    "InterleaveTracker",
    "MatroskaPacketValidation",
    "TRACK_TYPE_LABELS",
    "file_track_labels",
    "interleave_warnings",
    "mux_track_labels",
    "scan_interleave",
    "track_label",
    "validate_matroska_output",
]
