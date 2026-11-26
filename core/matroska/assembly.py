"""Contrat d'assemblage Matroska partagé entre remux et encode.

``MatroskaAssemblyPlan`` décrit la sortie finale : pistes ordonnées (chacune
adossée à un artefact Matroska lisible), chapitres, tags, attachments, titre
de segment et contrat de validation. Le remux et l'encode compilent vers ce
même plan ; :func:`compile_assembly_plan` le traduit en
:class:`MatroskaMuxPlan` consommé par le writer bas niveau.

L'assembleur final ne lance jamais implicitement un encode : les services de
préparation restent responsables de transformer une source non-Matroska ou
une piste encodée en artefact Matroska.
"""

from __future__ import annotations

import hashlib
import heapq
import mimetypes
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from core.version import APP_VERSION_LABEL, WRITING_APPLICATION_TAG

from .contract import (
    ExpectedMatroskaAttachment,
    ExpectedMatroskaTrack,
    ExpectedTrackFlags,
    MatroskaOutputContract,
)
from .chapter_tracks import remap_chapter_tracks
from .ids import CHAPTERS_ID, DEFAULT_TIMESTAMP_SCALE_NS, INFO_ID, TAGS_ID
from .language import bcp47_for_language, matroska_legacy_language
from .mux_plan import (
    MatroskaMuxPacket,
    MatroskaMuxPlan,
    MatroskaMuxTrack,
    deterministic_source_identity,
    deterministic_uid,
    deterministic_uid128,
)
from .reader import MatroskaAttachment, MatroskaBlock, MatroskaReader, MatroskaTrack
from .validation import TRACK_TYPE_LABELS
from .writer import (
    build_attachments_element,
    build_chapters_element,
    build_tags_element,
    rewrite_tag_target_uids,
)


def _quantized_ns(value_ns: int, scale_ns: int) -> int:
    """Instant arrondi au tick le plus proche (demi-tick vers le haut), en ns."""
    return ((value_ns + scale_ns // 2) // scale_ns) * scale_ns


def quantize_block(block: MatroskaBlock, scale_ns: int) -> MatroskaBlock:
    """Bloc exprimé en ticks entiers de ``scale_ns`` (identique s'il l'est déjà).

    Les extrémités sont quantifiées, jamais les écarts : une durée vaut
    ``q(t + d) − q(t)`` (au moins un tick si elle n'est pas nulle) et une
    référence ``q(t + r) − q(t)`` (au moins un tick, de même signe), pour que
    la cible d'une référence ou la fin d'un bloc restent celles de la source
    (une référence peut donc valoir 0 si les deux blocs tombent sur le même
    tick ; la forcer à ±1 changerait sa cible).
    """
    timestamp_ns = block.timestamp_ns if block.timestamp_ns is not None else block.timestamp_ms * 1_000_000
    duration_ns = block.duration_ns
    if duration_ns is None and block.duration_ms is not None:
        duration_ns = block.duration_ms * 1_000_000
    references_ns = block.references_ns or tuple(value * 1_000_000 for value in block.references)
    if (
        timestamp_ns % scale_ns == 0
        and (duration_ns is None or duration_ns % scale_ns == 0)
        and all(value % scale_ns == 0 for value in references_ns)
    ):
        return block
    start_ns = _quantized_ns(timestamp_ns, scale_ns)
    changes: dict[str, object] = {
        "timestamp_ns": start_ns,
        "timestamp_ms": round(start_ns / 1_000_000),
    }
    if duration_ns is not None:
        quantized_duration = _quantized_ns(timestamp_ns + duration_ns, scale_ns) - start_ns
        if duration_ns > 0:
            quantized_duration = max(scale_ns, quantized_duration)
        changes["duration_ns"] = quantized_duration
        changes["duration_ms"] = round(quantized_duration / 1_000_000)
    if references_ns:
        quantized_refs = [_quantized_ns(timestamp_ns + value, scale_ns) - start_ns for value in references_ns]
        changes["references_ns"] = tuple(quantized_refs)
        changes["references"] = tuple(round(value / 1_000_000) for value in quantized_refs)
    return replace(block, **changes)  # type: ignore[arg-type]


def _append_uid(mapping: dict[int, list[int]], source_uid: int, output_uid: int) -> None:
    targets = mapping.setdefault(source_uid, [])
    if output_uid not in targets:
        targets.append(output_uid)


def _packet_timestamp_ns(packet: MatroskaMuxPacket) -> int:
    block = packet.block
    return block.timestamp_ns if block.timestamp_ns is not None else block.timestamp_ms * 1_000_000


#: File d'attente d'une lecture partagée (octets de payload) : au-delà, la route
#: la plus chargée passe sur sa propre lecture. Mémoire bornée quelle que soit la
#: disposition de la source ; une source bien entrelacée reste lue une seule fois.
SHARED_READ_QUEUE_BYTES = 64 * 1024 * 1024


class _PacketSource(Protocol):
    def next_packet(self) -> MatroskaMuxPacket | None: ...

    def finish(self) -> None: ...

    def close(self) -> None: ...


class _IteratorSource:
    def __init__(self, stream: Iterator[MatroskaMuxPacket]) -> None:
        self._stream = stream

    def next_packet(self) -> MatroskaMuxPacket | None:
        return next(self._stream, None)

    def finish(self) -> None:
        return None

    def close(self) -> None:
        close = getattr(self._stream, "close", None)
        if callable(close):
            close()


class _Route:
    """Piste source → piste de sortie (décalage compris), avec sa file d'attente."""

    def __init__(self, track_number: int, output_index: int, offset_ns: int, scale_ns: int) -> None:
        self.track_number = track_number
        self.output_index = output_index
        self.offset_ns = offset_ns
        self.scale_ns = scale_ns
        # (rang du bloc dans la piste source, paquet, octets)
        self.queue: deque[tuple[int, MatroskaMuxPacket, int]] = deque()
        self.queued_bytes = 0
        self.own: Iterator[MatroskaMuxPacket] | None = None

    def packet(self, sequence: int, block: MatroskaBlock) -> MatroskaMuxPacket | None:
        block = quantize_block(block, self.scale_ns)
        source_timestamp_ns = block.timestamp_ns if block.timestamp_ns is not None else block.timestamp_ms * 1_000_000
        shifted_timestamp_ns = source_timestamp_ns + self.offset_ns
        if shifted_timestamp_ns < 0:
            return None
        if self.offset_ns:
            block = replace(
                block, timestamp_ms=round(shifted_timestamp_ns / 1_000_000), timestamp_ns=shifted_timestamp_ns,
            )
        return MatroskaMuxPacket(self.output_index, block, sequence)


class _SharedArtifactRead:
    """Lecture séquentielle unique d'un artefact, distribuée aux files de ses routes.

    Pistes clairsemées (sous-titres) : leur prochain bloc peut se trouver des
    minutes plus loin dans le fichier ; l'attendre ferait déborder les files des
    autres pistes. Quand les Cues indexent chacun de leurs blocs (nombre égal aux
    statistiques de la piste), ils sont lus directement aux positions indexées,
    dans l'ordre de présentation (CueTime, puis position) : un sous-titre rangé
    en retard par la source est écrit à sa place. La lecture partagée vérifie ce
    compte jusqu'à la fin du fichier.
    """

    def __init__(
        self, reader: MatroskaReader, routes: list[_Route], budget: int | None = None,
        *, sparse_tracks: dict[int, int] | None = None,
        reopen: Callable[[], MatroskaReader] | None = None,
    ) -> None:
        self._reader = reader
        # Lecture propre d'une route détachée : nouveau lecteur (flux) ou le même fichier.
        self._reopen = reopen
        self._routes = routes
        self._budget = SHARED_READ_QUEUE_BYTES if budget is None else budget
        self._by_track: dict[int, list[_Route]] = {}
        for route in routes:
            self._by_track.setdefault(route.track_number, []).append(route)
        self._dispatched: dict[int, int] = dict.fromkeys(self._by_track, 0)
        self._stream: Iterator[MatroskaBlock] | None = None
        self._queued = 0
        # Piste clairsemée → UID (statistiques) ; lecture par les Cues décidée au premier paquet.
        self._sparse_tracks = dict(sparse_tracks or {})
        self._prepared = False
        self._indexed_counts: dict[int, int] = {}

    def _prepare(self) -> None:
        self._prepared = True
        sparse = {number: uid for number, uid in self._sparse_tracks.items() if number in self._by_track}
        # Vérification garantie : au moins une piste reste lue par la passe partagée.
        if not sparse or set(sparse) == set(self._by_track):
            return
        index = self._reader.cue_block_index(set(sparse))
        if index is None:
            return
        frame_counts = _statistics_frame_counts(self._reader)
        for number, uid in sparse.items():
            indexed = index.get(number) or {}
            if not indexed or frame_counts.get(uid) != len(indexed):
                continue
            self._indexed_counts[number] = len(indexed)
            # Sous-titres sans dépendance entre blocs : ordre de présentation.
            ordered = tuple(sorted(indexed, key=lambda position: (indexed[position], position)))
            for route in self._by_track[number]:
                # Lecture à la demande (un bloc en mémoire), une par route.
                route.own = _indexed_route_stream(self._reader, route, ordered)

    def next_packet(self, route: _Route) -> MatroskaMuxPacket | None:
        if not self._prepared:
            self._prepare()
        while True:
            if route.own is not None:
                return next(route.own, None)
            if route.queue:
                _sequence, packet, size = route.queue.popleft()
                route.queued_bytes -= size
                self._queued -= size
                return packet
            if not self._pull():
                return None

    def finish(self) -> None:
        """Lecture partagée menée jusqu'à la fin du fichier : comptes des pistes indexées vérifiés.

        Toutes les routes lues par la passe partagée peuvent l'avoir quittée
        (détachées) avant la fin : la vérification doit pourtant avoir lieu
        avant la publication.
        """
        if not self._indexed_counts:
            return
        while self._pull():
            pass

    def _pull(self) -> bool:
        if self._stream is None:
            self._stream = self._reader.blocks(track_numbers=set(self._by_track), first_lace_only=True)
        block = next(self._stream, None)
        if block is None:
            for number, count in self._indexed_counts.items():
                if self._dispatched[number] != count:
                    raise ValueError(
                        f"Index Matroska (Cues) incomplet pour la piste #{number} : {count} bloc(s) indexé(s), "
                        f"{self._dispatched[number]} dans le fichier."
                    )
            self._indexed_counts.clear()
            return False
        routes = self._by_track.get(block.track_number)
        if not routes:
            return True
        sequence = self._dispatched[block.track_number]
        self._dispatched[block.track_number] = sequence + 1
        if block.track_number in self._indexed_counts:
            return True
        for route in routes:
            if route.own is not None:
                continue
            packet = route.packet(sequence, block)
            if packet is None:
                # Écarté (avant zéro après décalage) : rien en file, le rang reste compté.
                continue
            size = len(packet.block.encoded_frames_payload or packet.block.payload)
            route.queue.append((sequence, packet, size))
            route.queued_bytes += size
            self._queued += size
        while self._queued > self._budget:
            loaded = [route for route in self._routes if route.own is None and route.queued_bytes]
            if not loaded:
                break
            self._detach(max(loaded, key=lambda route: route.queued_bytes))
        return True

    def _detach(self, route: _Route) -> None:
        """La route lit désormais sa piste elle-même, à partir de son premier bloc non consommé."""
        start = route.queue[0][0] if route.queue else self._dispatched[route.track_number]
        self._queued -= route.queued_bytes
        route.queue.clear()
        route.queued_bytes = 0
        route.own = self._own_stream(route, start)

    def _own_stream(self, route: _Route, start: int) -> Iterator[MatroskaMuxPacket]:
        reader = self._reopen() if self._reopen is not None else self._reader
        blocks = reader.blocks(track_numbers={route.track_number}, first_lace_only=True)
        try:
            for sequence, block in enumerate(blocks):
                if sequence < start:
                    continue
                packet = route.packet(sequence, block)
                if packet is not None:
                    yield packet
        finally:
            close = getattr(blocks, "close", None)
            if callable(close):
                close()

    def close(self) -> None:
        for route in self._routes:
            if route.own is not None:
                close_own = getattr(route.own, "close", None)
                if callable(close_own):
                    close_own()
        close_stream = getattr(self._stream, "close", None)
        if callable(close_stream):
            close_stream()


def _indexed_route_stream(
    reader: MatroskaReader, route: _Route, positions: tuple[tuple[int, int], ...],
) -> Iterator[MatroskaMuxPacket]:
    """Blocs d'une piste lus aux positions de ses Cues, dans l'ordre donné ; une Cue visant une autre piste est refusée."""
    blocks = reader.indexed_blocks(positions, first_lace_only=True, ordered=True)
    try:
        for sequence, block in enumerate(blocks):
            if block.track_number != route.track_number:
                raise ValueError(
                    f"Index Matroska (Cues) incohérent : une entrée de la piste #{route.track_number} "
                    f"désigne un bloc de la piste #{block.track_number}."
                )
            packet = route.packet(sequence, block)
            if packet is not None:
                yield packet
    finally:
        close = getattr(blocks, "close", None)
        if callable(close):
            close()


def _statistics_frame_counts(reader: MatroskaReader) -> dict[int, int]:
    """``NUMBER_OF_FRAMES`` des statistiques de chaque piste (UID → nombre), quand il est lisible."""
    counts: dict[int, int] = {}
    for tag in reader.tags():
        uid = tag.targets.get("63c5")
        if not uid:
            continue
        for name, value in tag.values:
            if name.upper() == "NUMBER_OF_FRAMES" and value.strip().isdigit():
                counts[uid] = int(value.strip())
    return counts


class _SharedRouteSource:
    def __init__(self, shared: _SharedArtifactRead, route: _Route) -> None:
        self._shared = shared
        self._route = route

    def next_packet(self) -> MatroskaMuxPacket | None:
        return self._shared.next_packet(self._route)

    def finish(self) -> None:
        self._shared.finish()

    def close(self) -> None:
        self._shared.close()


def merge_packet_sources(sources: Sequence[_PacketSource]) -> Iterator[MatroskaMuxPacket]:
    """Fusion des routes par timestamp, sans jamais réordonner une route.

    Clé de chaque route = maximum courant de ses timestamps (B-frames comprises) :
    non décroissante, elle rend la fusion exacte. À clé égale, l'ordre des routes
    (ordre des pistes de sortie) départage. Chaque source est fermée en fin de
    fusion, d'erreur ou d'annulation (fichiers et fenêtres mmap libérés).
    """
    def generate() -> Iterator[MatroskaMuxPacket]:
        keys: list[int | None] = [None] * len(sources)
        heap: list[tuple[int, int, MatroskaMuxPacket]] = []

        def push(index: int) -> None:
            packet = sources[index].next_packet()
            if packet is None:
                return
            timestamp = _packet_timestamp_ns(packet)
            previous = keys[index]
            key = timestamp if previous is None or timestamp > previous else previous
            keys[index] = key
            heapq.heappush(heap, (key, index, packet))

        try:
            for index in range(len(sources)):
                push(index)
            while heap:
                _key, index, packet = heapq.heappop(heap)
                yield packet
                push(index)
            # Fin normale : contrôles différés (comptes des Cues) avant que le writer publie.
            for source in sources:
                source.finish()
        finally:
            for source in sources:
                source.close()

    return generate()


def merge_route_streams(streams: list[Iterator[MatroskaMuxPacket]]) -> Iterator[MatroskaMuxPacket]:
    """:func:`merge_packet_sources` sur des flux de paquets déjà par route."""
    return merge_packet_sources([_IteratorSource(stream) for stream in streams])


def canonical_attachment_output_name(path: Path) -> str:
    """Nom de sortie d'un attachment fichier — commun à TOUS les backends.

    Les covers gardent leur extension avec un stem canonique en minuscules
    (convention cover-art Matroska : ``cover.jpg``/``cover.png``). Commandes
    de muxage, assembleur natif et contrats de validation doivent produire
    et attendre exactement le même nom, quel que soit le backend.
    """
    if path.stem.lower() == "cover" and path.suffix:
        return f"cover{path.suffix.lower()}"
    return path.name


@dataclass(frozen=True)
class MatroskaTrackFlags:
    """Flags MKV explicites d'une piste de sortie."""

    enabled: bool = True
    default: bool = False
    forced: bool = False
    hearing_impaired: bool = False
    visual_impaired: bool = False
    original: bool = False
    commentary: bool = False


@dataclass(frozen=True)
class MatroskaAssemblyTrack:
    """Piste préparée : artefact Matroska concret + identité + métadonnées.

    ``language_value``, ``name`` et ``flags`` à ``None`` préservent les
    valeurs du TrackEntry de l'artefact (copie sans édition) ; une valeur
    explicite est écrite dans le TrackEntry de sortie.
    """

    artifact: Path
    artifact_track_index: int
    source_identity: str
    language_value: str | None = None
    name: str | None = None
    flags: MatroskaTrackFlags | None = None
    time_shift_ms: int = 0
    #: Provenance complémentaire pour reconstruire les UIDs de façon stable
    #: (ex. ``"audio:2:aac"`` pour une variante matérialisée).
    provenance: str = ""
    #: Piste d'origine (artefact Matroska, index) remplacée par ``artifact``
    #: (audio réencodé, sync réelle) : langue, nom et flags non fixés en
    #: sont repris, et ses tags ciblés suivent la piste de sortie.
    origin: tuple[Path, int] | None = None
    remove_closed_captions: bool = False


def _origin_track(track: MatroskaAssemblyTrack, tracks_of) -> MatroskaTrack | None:
    """TrackEntry d'origine d'une piste remplacée (``None`` si aucune)."""
    if track.origin is None:
        return None
    origin_path, origin_index = track.origin
    origin_tracks = tracks_of(origin_path)
    if not 0 <= origin_index < len(origin_tracks):
        raise ValueError(f"Piste d'origine introuvable : {origin_path} #{origin_index}")
    return origin_tracks[origin_index]


def _with_origin_metadata(track: MatroskaAssemblyTrack, origin: MatroskaTrack | None) -> MatroskaAssemblyTrack:
    """Langue, nom et flags non fixés repris explicitement de la piste d'origine."""
    if origin is None:
        return track
    return replace(
        track,
        language_value=(
            track.language_value
            if track.language_value is not None
            else (origin.language_bcp47 or origin.language or None)
        ),
        name=track.name if track.name is not None else origin.name,
        flags=track.flags if track.flags is not None else MatroskaTrackFlags(
            enabled=origin.flag_enabled,
            default=origin.flag_default,
            forced=origin.flag_forced,
            hearing_impaired=origin.flag_hearing_impaired,
            visual_impaired=origin.flag_visual_impaired,
            original=origin.flag_original,
            commentary=origin.flag_commentary,
        ),
    )


@dataclass(frozen=True)
class MatroskaAssemblyAttachment:
    """Attachment copié depuis un artefact Matroska existant."""

    artifact: Path
    local_index: int
    source_identity: str


@dataclass(frozen=True)
class MatroskaAssemblyPlan:
    """Plan d'assemblage final commun remux/encode."""

    output: Path
    ordered_tracks: tuple[MatroskaAssemblyTrack, ...]
    attachments: tuple[MatroskaAssemblyAttachment, ...] = ()
    extra_attachment_files: tuple[Path, ...] = ()
    #: None → chapitres copiés depuis ``chapter_source`` (si défini) ;
    #: tuple → chapitres reconstruits depuis ces entrées (vide = aucun).
    chapter_entries: tuple | None = None
    chapter_source: Path | None = None
    tag_overrides: dict[str, str] | None = None
    tag_copy_sources: tuple[Path, ...] = ()
    #: Sources dont seuls les tags ciblant une piste retenue sont copiés (en-têtes de
    #: producteurs en flux, origines de pistes réécrites).
    track_tag_sources: tuple[Path, ...] = ()
    #: ``None`` préserve le titre source ; une chaîne vide le supprime.
    segment_title: str | None = None
    title_tag_value: str = ""
    writing_app: str = ""
    #: Ordre d'énumération des artefacts pour la fusion des paquets (stabilité
    #: inter-sources) ; vide → premier-vu dans ``ordered_tracks``.
    artifact_order: tuple[Path, ...] = ()
    segment_info_source: Path | None = None
    expected_output_contract: MatroskaOutputContract | None = None
    #: Copie fidèle : enfants d'Info de ``segment_info_source`` hors champs gérés
    #: recopiés tels quels (DateUTC…) ; liens de segments → refus (ValueError).
    segment_info_passthrough: bool = False


def copied_chapters(plan: MatroskaAssemblyPlan, reader: MatroskaReader | None,
                    uid_map: Mapping[int, Sequence[int]]) -> tuple[bytes, int]:
    """Chapitres écrits pour le plan, fonction unique du contrat et de l'écriture : chapitres fournis
    (``chapter_entries``), sinon ceux de ``chapter_source`` dont les ``ChapterTrack`` sont remappés par ``uid_map``
    (UID source → UID de sortie) et les chapitres sans cible retirés. Renvoie (élément ``Chapters`` ou ``b""``,
    nombre de chapitres retirés)."""
    if plan.chapter_entries is not None:
        return build_chapters_element(list(plan.chapter_entries)), 0
    if plan.chapter_source is None or reader is None:
        return b"", 0
    parts: list[bytes] = []
    removed = 0
    for raw in reader.raw_top_level(CHAPTERS_ID):
        rewritten, count = remap_chapter_tracks(MatroskaReader._raw_payload(raw), uid_map)
        removed += count
        if rewritten:
            parts.append(rewritten)
    return b"".join(parts), removed


def _chapter_source_uids(plan: MatroskaAssemblyPlan, readers: dict[Path, MatroskaReader]) -> dict[int, list[int]]:
    """Pistes de la source des chapitres présentes dans la sortie (UID source → lui-même) : chapitres qui seront
    écrits, sans les UID de sortie (contrat calculé avant la compilation)."""
    present: dict[int, list[int]] = {}
    source = plan.chapter_source
    if source is None:
        return present
    for track in plan.ordered_tracks:
        candidates = [(Path(track.artifact), track.artifact_track_index)]
        if track.origin is not None:
            candidates.append((Path(track.origin[0]), int(track.origin[1])))
        for path, index in candidates:
            if path == Path(source):
                tracks = readers.setdefault(path, MatroskaReader(path)).tracks()
                if 0 <= index < len(tracks):
                    present[tracks[index].uid] = [tracks[index].uid]
    return present


def assembly_output_contract(
    plan: MatroskaAssemblyPlan,
    *,
    require_block_addition_mapping: bool | set[int] = False,
) -> MatroskaOutputContract:
    """Contrat de sortie dérivé du plan (types dans l'ordre d'assemblage) ; chapitres attendus d'après les chapitres
    réellement écrits (``ChapterTrack`` remappés, chapitres sans cible retirés : un retrait, jusqu'au dernier, ne
    fait jamais rejeter la sortie), même pour un contrat précalculé."""
    readers: dict[Path, MatroskaReader] = {}
    chapter_reader = (readers.setdefault(Path(plan.chapter_source), MatroskaReader(Path(plan.chapter_source)))
                      if plan.chapter_entries is None and plan.chapter_source is not None else None)
    written_chapters, _removed = copied_chapters(plan, chapter_reader, _chapter_source_uids(plan, readers))
    if plan.expected_output_contract is not None:
        return replace(plan.expected_output_contract, expects_chapters=bool(written_chapters))
    types: list[str] = []
    expected_tracks: list[ExpectedMatroskaTrack] = []
    required_mapping_indexes = (
        set(require_block_addition_mapping)
        if isinstance(require_block_addition_mapping, set)
        else set()
    )
    for output_index, track in enumerate(plan.ordered_tracks):
        reader = readers.setdefault(track.artifact, MatroskaReader(track.artifact))
        native_tracks = reader.tracks()
        if not 0 <= track.artifact_track_index < len(native_tracks):
            raise ValueError(
                f"Piste d'assemblage introuvable : {track.artifact} #{track.artifact_track_index}"
            )
        source_track = native_tracks[track.artifact_track_index]
        track = _with_origin_metadata(track, _origin_track(
            track, lambda path: readers.setdefault(path, MatroskaReader(path)).tracks(),
        ))
        track_type = TRACK_TYPE_LABELS.get(source_track.track_type, str(source_track.track_type))
        types.append(track_type)
        flags = track.flags
        expected_tracks.append(ExpectedMatroskaTrack(
            track_type=track_type,
            name=track.name if track.name is not None else source_track.name,
            language=(
                track.language_value
                if track.language_value is not None
                else (source_track.language_bcp47 or source_track.language)
            ),
            flags=ExpectedTrackFlags(
                enabled=flags.enabled if flags is not None else source_track.flag_enabled,
                default=flags.default if flags is not None else source_track.flag_default,
                forced=flags.forced if flags is not None else source_track.flag_forced,
                hearing_impaired=(flags.hearing_impaired if flags is not None else source_track.flag_hearing_impaired),
                visual_impaired=(flags.visual_impaired if flags is not None else source_track.flag_visual_impaired),
                original=flags.original if flags is not None else source_track.flag_original,
                commentary=flags.commentary if flags is not None else source_track.flag_commentary,
            ),
            require_packets=source_track.track_type in (1, 2),
            require_block_addition_mapping=(
                (bool(require_block_addition_mapping) and not required_mapping_indexes and source_track.track_type == 1)
                or output_index in required_mapping_indexes
            ),
        ))
    # Chapitres attendus seulement s'ils sont réellement écrits (source sans chapitres, ou chapitres tous retirés
    # faute de piste ciblée : contrat atteignable).
    expects_chapters = bool(written_chapters)
    expected_attachments: list[ExpectedMatroskaAttachment] = []
    for extra in plan.extra_attachment_files:
        path = Path(extra)
        output_name = canonical_attachment_output_name(path)
        expected_attachments.append(ExpectedMatroskaAttachment(
            name=output_name,
            media_type=mimetypes.guess_type(output_name)[0],
            size=path.stat().st_size if path.is_file() else None,
        ))
    for attachment in plan.attachments:
        headers = readers.setdefault(
            attachment.artifact, MatroskaReader(attachment.artifact),
        ).attachment_headers()
        if 0 <= attachment.local_index < len(headers):
            header = headers[attachment.local_index]
            expected_attachments.append(ExpectedMatroskaAttachment(
                name=header.name,
                media_type=header.media_type,
                size=header.size,
            ))
    expects_tags = bool(plan.tag_overrides)
    if plan.tag_overrides is None:
        expects_tags = any(
            bool(readers.setdefault(source, MatroskaReader(source)).raw_top_level(TAGS_ID))
            for source in plan.tag_copy_sources
        ) or bool(plan.title_tag_value)
    return MatroskaOutputContract(
        track_types=tuple(types),
        expected_tracks=tuple(expected_tracks),
        expects_chapters=expects_chapters,
        expects_tags=expects_tags,
        attachment_names=tuple(item.name for item in expected_attachments),
        expected_attachments=tuple(expected_attachments),
        require_block_addition_mapping=bool(require_block_addition_mapping),
        strict_attachment_names=True,
    )


def compile_assembly_plan(
    plan: MatroskaAssemblyPlan,
    *,
    readers: Mapping[Path, MatroskaReader] | None = None,
    reopen: Mapping[Path, Callable[[], MatroskaReader]] | None = None,
) -> MatroskaMuxPlan:
    """Compile le plan d'assemblage vers le plan bas niveau du writer natif.

    ``readers`` : lecteurs fournis pour certains artefacts (flux lus par pipe :
    métadonnées de leur en-tête, blocs à usage unique). ``reopen`` : nouvelle
    lecture d'un tel artefact, pour une route qui quitte la lecture partagée
    (un flux ne se relit pas). Sans eux, chaque artefact est un fichier Matroska.
    """
    if not plan.ordered_tracks:
        raise ValueError("Plan d'assemblage sans piste.")
    if plan.expected_output_contract is None:
        raise ValueError(
            "MatroskaAssemblyPlan.expected_output_contract doit être compilé avant écriture."
        )

    # Lecteurs partagés : artefacts de pistes, chapitres, tags, attachments.
    reader_order: list[Path] = []

    def _register(path: Path | None) -> None:
        if path is not None and path not in reader_order:
            reader_order.append(path)

    for artifact in plan.artifact_order:
        _register(artifact)
    for track in plan.ordered_tracks:
        _register(track.artifact)
    _register(plan.segment_info_source)
    _register(plan.chapter_source if plan.chapter_entries is None else None)
    for source in (*plan.tag_copy_sources, *plan.track_tag_sources):
        _register(source)
    for attachment in plan.attachments:
        _register(attachment.artifact)

    provided = dict(readers or {})
    readers = {path: provided.get(path) or MatroskaReader(path) for path in reader_order}
    native_tracks = {path: reader.tracks() for path, reader in readers.items()}

    # Échelle de sortie fixe (1 ms, valeur Matroska usuelle) : une entrée plus
    # fine (artefact FFmpeg à 10 µs, fichier tiers) est quantifiée bloc par bloc
    # (``quantize_block``) au lieu d'imposer son échelle à toute la sortie.
    timestamp_scale_ns = DEFAULT_TIMESTAMP_SCALE_NS

    origin_tracks: dict[Path, list[MatroskaTrack]] = {}

    def _tracks_of(path: Path) -> list[MatroskaTrack]:
        # Pistes d'origine lues hors ``readers`` : aucun effet sur la fusion
        # des paquets ni sur le TimestampScale de sortie.
        if path in native_tracks:
            return native_tracks[path]
        if path not in origin_tracks:
            origin_tracks[path] = MatroskaReader(path).tracks()
        return origin_tracks[path]

    output_tracks: list[MatroskaMuxTrack] = []
    # artefact → UID source → UID de sortie (plusieurs pour une piste dupliquée).
    track_uid_maps: dict[Path, dict[int, list[int]]] = {}
    # Routes de sortie, dans l'ordre des pistes : (artefact, piste source, piste sortie, offset ms).
    packet_routes: list[tuple[Path, int, int, int]] = []
    for output_index, track in enumerate(plan.ordered_tracks, start=1):
        artifact_tracks = native_tracks[track.artifact]
        if not 0 <= track.artifact_track_index < len(artifact_tracks):
            raise ValueError(
                f"Piste d'assemblage introuvable : {track.artifact} #{track.artifact_track_index}"
            )
        source_track = artifact_tracks[track.artifact_track_index]
        origin_track = _origin_track(track, _tracks_of)
        track = _with_origin_metadata(track, origin_track)
        identity = (
            f"{track.source_identity}:{track.provenance}"
            if track.provenance
            else track.source_identity
        )
        uid_language = (
            track.language_value
            if track.language_value is not None
            else (source_track.language_bcp47 or source_track.language)
        )
        uid_name = track.name if track.name is not None else source_track.name
        uid_default = track.flags.default if track.flags is not None else source_track.flag_default
        uid_forced = track.flags.forced if track.flags is not None else source_track.flag_forced
        # Piste lue en flux (MP4/MOV via un producteur FFmpeg) : son UID change à chaque lecture ; l'UID de sortie
        # dérive alors de l'identité de la source et de l'index du flux, jamais de l'UID du producteur.
        producer_uid: object = (origin_track or source_track).uid
        if origin_track is None and Path(track.artifact) in provided:
            producer_uid = f"flux:{track.artifact_track_index}"
        uid = deterministic_uid(
            identity, producer_uid, output_index,
            uid_language, uid_name, uid_default, uid_forced,
        )
        _append_uid(track_uid_maps.setdefault(track.artifact, {}), source_track.uid, uid)
        if track.origin is not None and origin_track is not None:
            # Tags de la source ciblant la piste remplacée : suivent la sortie.
            _append_uid(track_uid_maps.setdefault(track.origin[0], {}), origin_track.uid, uid)

        if track.language_value is not None:
            legacy_language = matroska_legacy_language(track.language_value)
            source_bcp47 = source_track.language_bcp47 or ""
            if source_bcp47 and matroska_legacy_language(source_bcp47) == legacy_language:
                # Même langue de base : le BCP-47 source (plus précis) est
                # conservé tel quel.
                language_bcp47 = source_bcp47
            else:
                # Langue changée ou source sans BCP-47 : balise régénérée
                # depuis la valeur demandée — jamais supprimée silencieusement.
                language_bcp47 = bcp47_for_language(track.language_value)
            patch_language = True
        else:
            legacy_language, language_bcp47, patch_language = "und", "", False
        flags = track.flags or MatroskaTrackFlags()
        output_tracks.append(MatroskaMuxTrack(
            source=track.artifact, source_track=source_track,
            output_number=output_index, output_uid=uid,
            language=legacy_language,
            language_bcp47=language_bcp47,
            name=track.name if track.name is not None else "",
            flag_enabled=flags.enabled,
            flag_default=flags.default,
            flag_forced=flags.forced,
            flag_hearing_impaired=flags.hearing_impaired,
            flag_visual_impaired=flags.visual_impaired,
            flag_original=flags.original,
            flag_commentary=flags.commentary,
            patch_language=patch_language,
            patch_name=track.name is not None,
            patch_flags=track.flags is not None,
            remove_closed_captions=track.remove_closed_captions,
        ))
        packet_routes.append((track.artifact, source_track.number, output_index, int(track.time_shift_ms or 0)))

    # Une route par piste de sortie. Les routes d'un même artefact partagent une
    # lecture séquentielle unique (une seule passe d'E/S) ; une route dont la file
    # d'attente dépasse le budget (source mal entrelacée, duplication décalée)
    # passe sur sa propre lecture. L'ordre de sortie ne dépend jamais de la
    # disposition des sources.
    routes_by_artifact: dict[Path, list[_Route]] = {}
    ordered_routes: list[tuple[Path, _Route]] = []
    for artifact, track_number, output_index, offset_ms in packet_routes:
        route = _Route(track_number, output_index, offset_ms * 1_000_000, timestamp_scale_ns)
        routes_by_artifact.setdefault(artifact, []).append(route)
        ordered_routes.append((artifact, route))
    shared_reads = {
        artifact: _SharedArtifactRead(readers[artifact], routes, sparse_tracks={
            track.number: track.uid for track in native_tracks[artifact] if track.track_type == 17
        }, reopen=(reopen or {}).get(artifact))
        for artifact, routes in routes_by_artifact.items()
    }
    packet_stream = merge_packet_sources([
        _SharedRouteSource(shared_reads[artifact], route) for artifact, route in ordered_routes
    ])

    opaque: list[bytes] = []
    attachments: list[MatroskaAttachment] = []
    attachment_uid_maps: dict[Path, dict[int, int]] = {}
    for selected in plan.attachments:
        available = readers[selected.artifact].attachments()
        if not 0 <= selected.local_index < len(available):
            raise ValueError(
                f"Attachment d'assemblage introuvable : {selected.artifact} #{selected.local_index}"
            )
        item = available[selected.local_index]
        output_uid = deterministic_uid(selected.source_identity, item.uid, item.name)
        attachment_uid_maps.setdefault(selected.artifact, {})[item.uid] = output_uid
        attachments.append(MatroskaAttachment(
            uid=output_uid, name=item.name,
            media_type=item.media_type, description=item.description, data=item.data,
        ))
    for path in plan.extra_attachment_files:
        attachment_path = Path(path)
        output_name = canonical_attachment_output_name(attachment_path)
        attachments.append(MatroskaAttachment(
            uid=deterministic_uid(deterministic_source_identity(attachment_path), output_name),
            name=output_name,
            media_type=mimetypes.guess_type(attachment_path.name)[0] or "application/octet-stream",
            description="", data=attachment_path.read_bytes(),
        ))
    attachment_element = build_attachments_element(attachments)
    if attachment_element:
        opaque.append(attachment_element)

    chapter_element, chapters_removed = copied_chapters(
        plan, readers[plan.chapter_source] if plan.chapter_entries is None and plan.chapter_source is not None
        else None, track_uid_maps.get(Path(plan.chapter_source), {}) if plan.chapter_source is not None else {})
    if chapter_element:
        opaque.append(chapter_element)

    if plan.tag_overrides is not None:
        tag_element = build_tags_element(dict(plan.tag_overrides))
        if tag_element:
            opaque.append(tag_element)
    else:
        for source in plan.tag_copy_sources:
            opaque.extend(
                rewrite_tag_target_uids(
                    raw,
                    track_uids=track_uid_maps.get(source, {}),
                    attachment_uids=attachment_uid_maps.get(source, {}),
                    drop_chapter_targets=plan.chapter_entries is not None,
                    drop_track_statistics=True,
                )
                for raw in readers[source].raw_top_level(TAGS_ID)
            )
        for source in plan.track_tag_sources:
            if source in plan.tag_copy_sources:
                continue
            opaque.extend(
                rewrite_tag_target_uids(
                    raw,
                    track_uids=track_uid_maps.get(source, {}),
                    attachment_uids={},
                    drop_track_statistics=True,
                    track_targets_only=True,
                )
                for raw in readers[source].raw_top_level(TAGS_ID)
            )
        if plan.title_tag_value:
            title_tag = build_tags_element({"title": plan.title_tag_value.strip()})
            if title_tag:
                opaque.append(title_tag)

    # La date de génération des statistiques varie à chaque muxage. Elle ne
    # doit pas rendre le SegmentUID instable pour un plan sémantiquement
    # identique ; les données de piste et les autres métadonnées sont déjà
    # couvertes par ce digest.
    opaque_digest = hashlib.sha256(b"".join(opaque)).hexdigest()
    info_source = plan.segment_info_source or plan.ordered_tracks[0].artifact
    info_reader = readers[info_source]
    segment_title = (
        info_reader.segment_title()
        if plan.segment_title is None
        else plan.segment_title.strip()
    )
    # SegmentUID déterministe dérivé de tout ce qui change le contenu écrit : pistes (UID/numéros dérivés du
    # contenu source), décalages, provenance (réécriture de synchro), origine, retrait des sous-titres embarqués,
    # titre, digest des top-level opaques (chapitres/tags/attachments) et TimestampScale. Deux plans qui écrivent
    # des contenus différents (même d'un seul décalage) ne partagent jamais un UID ; un même plan reste stable.
    segment_uid = deterministic_uid128(
        "segment",
        tuple((track.output_uid, track.output_number) for track in output_tracks),
        tuple((int(track.time_shift_ms or 0), str(track.provenance or ""),
               None if track.origin is None else (str(track.origin[0]), int(track.origin[1])),
               bool(track.remove_closed_captions)) for track in plan.ordered_tracks),
        segment_title,
        opaque_digest,
        timestamp_scale_ns,
    )
    # Estimation pour la progression seulement : aucun bloc n'est lu pendant
    # la compilation. Le writer calcule la durée et les statistiques exactes
    # dans l'unique passe d'écriture (sélection, lacing et offsets compris).
    max_duration_ns = max((
        max(0, duration + int(track.time_shift_ms or 0) * 1_000_000)
        for track in plan.ordered_tracks
        if (duration := readers[track.artifact].segment_duration_ns()) is not None
    ), default=0)
    info_passthrough: tuple[bytes, ...] = ()
    if plan.segment_info_passthrough:
        info_passthrough = segment_info_passthrough(info_reader)
    return MatroskaMuxPlan(
        plan.output, tuple(output_tracks), packet_stream,
        duration_ms=round(max_duration_ns / 1_000_000),
        duration_ns=max_duration_ns,
        timestamp_scale_ns=timestamp_scale_ns,
        segment_uid=segment_uid,
        muxing_app=f"Muxiveo {APP_VERSION_LABEL.removeprefix('v')}",
        writing_app=plan.writing_app or WRITING_APPLICATION_TAG,
        title=segment_title,
        opaque_top_level=tuple(opaque),
        regenerate_statistics=True,
        info_passthrough=info_passthrough,
        chapters_removed=chapters_removed,
    )


#: Champs d'Info écrits par le writer (jamais recopiés).
_MANAGED_INFO_IDS = frozenset({
    bytes.fromhex("2ad7b1"),  # TimestampScale
    bytes.fromhex("4489"),    # Duration
    bytes.fromhex("73a4"),    # SegmentUID
    bytes.fromhex("7ba9"),    # Title
    bytes.fromhex("4d80"),    # MuxingApp
    bytes.fromhex("5741"),    # WritingApp
    bytes.fromhex("ec"),      # Void
    bytes.fromhex("bf"),      # CRC-32
})
#: Liens entre segments : une copie isolée les rendrait faux.
SEGMENT_LINK_INFO_IDS = frozenset({
    bytes.fromhex("7384"),    # SegmentFilename
    bytes.fromhex("3cb923"),  # PrevUID
    bytes.fromhex("3c83ab"),  # PrevFilename
    bytes.fromhex("3eb923"),  # NextUID
    bytes.fromhex("3e83bb"),  # NextFilename
    bytes.fromhex("4444"),    # SegmentFamily
    bytes.fromhex("6924"),    # ChapterTranslate
})


def segment_info_passthrough(reader: MatroskaReader) -> tuple[bytes, ...]:
    """Enfants bruts d'Info hors champs gérés ; liens de segments refusés (``ValueError``)."""
    children: list[bytes] = []
    for raw in reader.raw_top_level(INFO_ID):
        for child_id, child_raw in _raw_info_children(raw):
            if child_id in SEGMENT_LINK_INFO_IDS:
                raise ValueError("Segment Info : liens entre segments non transposables par une copie isolée.")
            if child_id not in _MANAGED_INFO_IDS:
                children.append(child_raw)
    return tuple(children)


def _raw_info_children(raw_info: bytes) -> list[tuple[bytes, bytes]]:
    """(ID, élément brut) de chaque enfant d'un élément Info brut."""
    from io import BytesIO

    from .reader import read_element

    stream = BytesIO(raw_info)
    info = read_element(stream, limit=len(raw_info))
    if info is None or info.size is None:
        return []
    children: list[tuple[bytes, bytes]] = []
    stream.seek(info.payload_offset)
    end = info.payload_offset + info.size
    while stream.tell() < end:
        child = read_element(stream, limit=end)
        if child is None or child.size is None:
            break
        children.append((child.element_id, raw_info[child.offset:child.payload_offset + child.size]))
        stream.seek(child.payload_offset + child.size)
    return children


__all__ = [
    "MatroskaAssemblyAttachment",
    "MatroskaAssemblyPlan",
    "MatroskaAssemblyTrack",
    "MatroskaTrackFlags",
    "assembly_output_contract",
    "canonical_attachment_output_name",
    "compile_assembly_plan",
]
