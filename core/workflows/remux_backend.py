"""Backend selection for Matroska remuxing.

The public contract deliberately lives here instead of in the FFmpeg runner:
an exact-job can request a backend without coupling its JSON shape to an
implementation.  The native backend is capability-gated; ``auto`` remains
backwards compatible by selecting FFmpeg when a plan needs a feature that has
not yet been materialised by the native writer.

La planification (préflight scopé, canonicalisation sélective, variantes
audio, rapport structuré) vit dans :mod:`core.workflows.remux_plan` ; ce
module conserve les backends et le runner natif.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Protocol

from core.runner import TaskCancelledError, TaskSignals
from core.workflows.common.validation_override import ValidationOverride, accept_validation_override, validate_final_output
from core.matroska.assembly import (
    MatroskaAssemblyAttachment,
    MatroskaAssemblyPlan,
    MatroskaAssemblyTrack,
    MatroskaTrackFlags,
    compile_assembly_plan,
)
from core.matroska.mux_plan import (
    MatroskaMuxPlan,
    MatroskaMuxTrack,
    deterministic_source_identity,
)
from core.matroska.ids import CHAPTERS_ID
from core.matroska.reader import MatroskaReader
from core.matroska.assembly import canonical_attachment_output_name
from core.matroska.contract import without_expected_attachment
from core.matroska.validation import MatroskaPacketValidation, validate_matroska_output
from core.matroska.progress import native_mux_progress_callback
from core.matroska.writer import (
    MatroskaWriteCancelled, MatroskaWriter,
)
from core.workflows.remux_mapping import resolve_mapped_tracks, track_order_parts
from core.workflows.remux_mapping import normalized_language_value, resolved_global_tags
from core.workflows.remux_models import RemuxConfig, RemuxError, SourceInput
from core.workflows.remux_plan import (
    MATROSKA_EXTENSIONS,
    MuxBackendDecision,
    MuxExecutionPlan,
    build_audio_variant_command,
    build_canonicalization_command,
    mux_backend_report,
    native_capability_reasons,
    native_preparation_commands,
    participating_source_indexes,
    plan_remux,
    select_mux_backend,
)
from core.workflows.remux_plan import _metadata_carrier_stream
from core.subprocess_utils import subprocess_text_kwargs
from core.workdir import (
    download_tmdb_cover,
    normalized_tmdb_cover_filename,
    prepare_process_work_dir,
    relocate_tmdb_covers_to_process_dir,
    remove_path,
)
from core.workflows.common.ffmpeg_runtime import ffmpeg_progress_args, ffmpeg_version_log_line
from core.workflows.common.sync_rewrite import SyncRewriteService
from core.workflows.remux_sync_rewrite import (
    NativeRewrite,
    SyncRewriteSettings,
    ffmpeg_input_origin,
    finalize_native_rewrite_shifts,
    prepare_native_sync_rewrites,
    probe_origin_payload,
)
from core.matroska.validation import interleave_warning_lines, mux_track_labels
from core.workflows.common.native_stream_inputs import (
    StreamArtifacts,
    StreamInputs,
    StreamTrackSpec,
    is_streamable_source,
    probe_start_time,
)


def _measured_canonical_origin(
    original: Path,
    canonical_source: SourceInput,
    index_map: dict[int, int],
    ffprobe_bin: str,
    *,
    cancel_cb: Callable[[], bool] | None = None,
) -> float:
    """Origine retirée par la canonicalisation, mesurée sur un même paquet (s).

    PTS du premier paquet d'un flux retenu dans la source d'origine moins celui
    de son premier bloc dans l'artefact canonical : couvre une sélection
    partielle, le rebasage et ``avoid_negative_ts`` de FFmpeg sans hypothèse.
    """
    import json

    from core.bluray import ffprobe_input_args
    from core.subprocess_utils import run_cancellable_capture

    def _check_cancelled() -> None:
        if cancel_cb is not None and cancel_cb():
            raise TaskCancelledError()

    canonical_to_original = {canonical: original_index for original_index, canonical in index_map.items()}
    reader = MatroskaReader(canonical_source.path)
    tracks = reader.tracks()
    # Audio de préférence (paquets tous indépendants), sinon le premier flux retenu.
    candidates = sorted(
        canonical_to_original, key=lambda index: (tracks[index].track_type != 2, index),
    ) if tracks else []
    for canonical_index in candidates:
        if not 0 <= canonical_index < len(tracks):
            continue
        command = [
            ffprobe_bin, "-v", "error", "-select_streams", str(canonical_to_original[canonical_index]),
            "-show_entries", "packet=pts_time", "-read_intervals", "%+#1", "-of", "json",
            *ffprobe_input_args(original),
        ]
        result = run_cancellable_capture(
            command, cancel_cb=cancel_cb, check_cancelled=_check_cancelled, timeout=30, **subprocess_text_kwargs(),
        )
        try:
            packets = json.loads(result.stdout or "{}").get("packets") or []
            source_pts = float(packets[0]["pts_time"])
        except (ValueError, KeyError, IndexError, TypeError):
            continue
        blocks = reader.blocks(track_numbers={tracks[canonical_index].number}, first_lace_only=True)
        try:
            first = next(blocks, None)
        finally:
            close = getattr(blocks, "close", None)
            if callable(close):
                close()
        if first is None:
            continue
        canonical_ns = first.timestamp_ns if first.timestamp_ns is not None else first.timestamp_ms * 1_000_000
        return source_pts - canonical_ns / 1e9
    raise RemuxError(f"Origine temporelle de {original.name} illisible après canonicalisation.")


class RemuxBackend(Protocol):
    """Execution contract shared by native and external remux backends."""

    name: str

    def validate(self, config: RemuxConfig) -> tuple[str, ...]: ...
    def preview(self, config: RemuxConfig) -> dict[str, object]: ...
    def execute(self, config: RemuxConfig) -> TaskSignals: ...


@dataclass
class NativeMatroskaBackend:
    log: Callable[[str, str], None]
    log_step: Callable[[int, str], None]
    ffmpeg_bin: str
    ffprobe_bin: str = "ffprobe"
    finalize: Callable[[Path], None] = lambda _path: None
    plan: MuxExecutionPlan | None = None
    name: str = "native"
    validation_override: ValidationOverride | None = None
    sync_rewrite: SyncRewriteSettings | None = None

    def validate(self, config: RemuxConfig) -> tuple[str, ...]:
        return native_capability_reasons(config)

    def preview(self, config: RemuxConfig) -> dict[str, object]:
        return {
            "backend": self.name,
            "plan_version": 1,
            "action": "internal_matroska_write",
            "preparation_commands": native_preparation_commands(config, self.ffmpeg_bin),
        }

    def execute(self, config: RemuxConfig) -> TaskSignals:
        return run_native_remux(
            config, log=self.log, log_step=self.log_step,
            ffmpeg_bin=self.ffmpeg_bin, ffprobe_bin=self.ffprobe_bin,
            finalize=self.finalize, plan=self.plan,
            validation_override=self.validation_override,
            sync_rewrite=self.sync_rewrite,
        )


@dataclass
class FfmpegRemuxBackend:
    execute_callback: Callable[[RemuxConfig], TaskSignals]
    preview_callback: Callable[[RemuxConfig], str]
    command_callback: Callable[[RemuxConfig], list[str]]
    name: str = "ffmpeg"

    def validate(self, config: RemuxConfig) -> tuple[str, ...]:
        return ()

    def preview(self, config: RemuxConfig) -> dict[str, object]:
        return {
            "backend": self.name,
            "plan_version": 1,
            "action": "external_ffmpeg",
            "command": self.command_callback(config),
            "command_text": self.preview_callback(config),
            "preparation_commands": [],
        }

    def execute(self, config: RemuxConfig) -> TaskSignals:
        return self.execute_callback(config)


def remux_assembly_plan(config: RemuxConfig) -> MatroskaAssemblyPlan:
    """Compile la configuration remux vers le contrat d'assemblage partagé."""
    mapped = resolve_mapped_tracks(config)
    participating = participating_source_indexes(config)
    source_by_index = {source.file_index: source for source in config.sources}
    identities = {
        source.file_index: source.origin_identity or deterministic_source_identity(source.path)
        for source in config.sources
        if source.file_index in participating
    }

    ordered_tracks: list[MatroskaAssemblyTrack] = []
    for item in mapped:
        track = item.track
        ordered_tracks.append(MatroskaAssemblyTrack(
            artifact=Path(item.source_path),
            artifact_track_index=item.stream_index,
            source_identity=identities[item.source_file_index],
            language_value=normalized_language_value(track),
            name=track.title,
            flags=MatroskaTrackFlags(
                enabled=track.flag_enabled,
                default=track.flag_default,
                forced=track.flag_forced,
                hearing_impaired=track.flag_hearing_impaired,
                visual_impaired=track.flag_visual_impaired,
                original=track.flag_original,
                commentary=track.flag_commentary,
            ),
            time_shift_ms=int(track.time_shift_ms or 0),
            remove_closed_captions=bool(track.remove_closed_captions) if track.is_video else False,
            # Piste réécrite : tags et TrackEntry de la piste d'origine suivent la sortie.
            origin=track.rewrite_origin,
        ))

    attachments = tuple(
        MatroskaAssemblyAttachment(
            artifact=source.path,
            local_index=selected.local_index,
            source_identity=identities[source.file_index],
        )
        for source in config.sources
        for selected in source.selected_attachments
    )

    chapter_entries = tuple(config.chapter_overrides) if config.chapter_overrides is not None else None
    chapter_source: Path | None = None
    if chapter_entries is None and config.keep_chapters:
        chapter_index = config.chapter_source_index
        if chapter_index is None or chapter_index not in participating:
            chapter_index = next(
                (source.file_index for source in config.sources if source.has_chapters),
                config.sources[0].file_index,
            )
        if chapter_index not in participating:
            chapter_index = config.sources[0].file_index
        chapter_source = source_by_index[chapter_index].path

    return MatroskaAssemblyPlan(
        output=config.output,
        ordered_tracks=tuple(ordered_tracks),
        attachments=attachments,
        extra_attachment_files=tuple(Path(path) for path in config.extra_attachments),
        chapter_entries=chapter_entries,
        chapter_source=chapter_source,
        tag_overrides=resolved_global_tags(config) if config.tag_overrides is not None else None,
        tag_copy_sources=tuple(source.path for source in config.sources if source.copy_tags),
        segment_title=(
            config.file_title
            if config.file_title.strip() or config.tag_overrides is not None
            else None
        ),
        title_tag_value=config.file_title,
        artifact_order=tuple(
            source.path for source in config.sources if source.file_index in participating
        ),
        segment_info_source=config.sources[0].path,
    )


def stream_assembly_plan(
    assembly: MatroskaAssemblyPlan,
    config: RemuxConfig,
    streams: StreamArtifacts,
) -> MatroskaAssemblyPlan:
    """Pistes des sources ISO BMFF lues en flux, contexte sur un en-tête dédié.

    Vidéo et audio d'une source partagent un producteur (pistes dans l'ordre de
    sortie), chaque sous-titre a le sien (:func:`group_stream_specs`) ;
    chapitres, tags globaux, Segment Info et ordre des artefacts pointent vers
    le producteur de contexte de la source ; les tags propres à chaque piste
    viennent des en-têtes de ses producteurs.
    """
    streamed = {Path(source.path): source for source in config.sources if is_streamable_source(source.path)}
    if not streamed:
        return assembly

    def spec_for(source: SourceInput, stream_index: int, *, context: bool = False) -> StreamTrackSpec:
        entry = next((track for track in source.tracks if track.mkv_tid == stream_index), None)
        kind = entry.track_type if entry is not None else ""
        codec = (entry.codec if entry is not None else "") or ""
        return StreamTrackSpec(Path(source.path), stream_index, kind, codec, carry_context=context)

    used: dict[Path, list[int]] = {}
    for track in assembly.ordered_tracks:
        if Path(track.artifact) in streamed:
            used.setdefault(Path(track.artifact), []).append(int(track.artifact_track_index))

    def context_of(path: Path | None) -> Path | None:
        source = streamed.get(Path(path)) if path is not None else None
        if source is None:
            return path
        # Porteur du contexte : une piste déjà lue de la source, sinon le flux le plus léger.
        selected = used.get(Path(source.path)) or []
        carrier = min(selected) if selected else _metadata_carrier_stream(source)
        if carrier is None:
            raise RemuxError(f"{source.path.name} : aucun flux pour porter chapitres et tags.")
        return streams.context(spec_for(source, int(carrier), context=True))

    # Vidéo et audio d'une même source sur un seul producteur, chaque sous-titre sur le sien.
    streams.declare(
        spec_for(streamed[Path(track.artifact)], int(track.artifact_track_index))
        for track in assembly.ordered_tracks if Path(track.artifact) in streamed
    )
    tracks = []
    for track in assembly.ordered_tracks:
        source = streamed.get(Path(track.artifact))
        if source is None:
            tracks.append(track)
            continue
        header, position = streams.route(spec_for(source, int(track.artifact_track_index)))
        tracks.append(replace(track, artifact=header, artifact_track_index=position, origin=None))
    tag_sources: list[Path] = []
    track_tag_sources: list[Path] = list(assembly.track_tag_sources)
    for path in assembly.tag_copy_sources:
        context = context_of(path)
        if context is not None:
            tag_sources.append(context)
        if Path(path) in streamed:
            # Tags propres aux pistes ; les tags globaux viennent du contexte.
            track_tag_sources.extend(streams.route_headers(path))
    return replace(
        assembly,
        ordered_tracks=tuple(tracks),
        chapter_source=context_of(assembly.chapter_source),
        tag_copy_sources=tuple(dict.fromkeys(tag_sources)),
        track_tag_sources=tuple(dict.fromkeys(track_tag_sources)),
        segment_info_source=context_of(assembly.segment_info_source),
        artifact_order=tuple(dict.fromkeys(
            item for item in (context_of(path) for path in assembly.artifact_order) if item is not None
        )),
    )


def _readable_origins(
    assembly: MatroskaAssemblyPlan,
    config: RemuxConfig,
    streams: StreamArtifacts,
    origin_tag_sources: frozenset[Path] = frozenset(),
) -> MatroskaAssemblyPlan:
    """Origine non Matroska (MP4 en flux, TS…) d'une piste réécrite : en-tête seul de la piste.

    ``origin_tag_sources`` : sources d'origine dont les tags sont copiés ; l'en-tête
    devient fournisseur de tags (les tags de piste y ciblent l'UID de l'origine,
    jamais celui d'un artefact canonical où la piste ne figure pas).
    """
    entries = {
        track.rewrite_origin: track
        for source in config.sources for track in source.tracks
        if track.rewrite_origin is not None
    }
    tracks = []
    extra_tag_sources: list[Path] = []
    for track in assembly.ordered_tracks:
        origin = track.origin
        if origin is None or Path(origin[0]).suffix.lower() in MATROSKA_EXTENSIONS:
            tracks.append(track)
            continue
        entry = entries.get(origin)
        header = streams.metadata(StreamTrackSpec(
            Path(origin[0]), int(origin[1]),
            entry.track_type if entry is not None else "",
            (entry.orig_codec or entry.codec) if entry is not None else "",
        ))
        tracks.append(replace(track, origin=header))
        if Path(origin[0]) in origin_tag_sources:
            extra_tag_sources.append(header[0])
    return replace(
        assembly,
        ordered_tracks=tuple(tracks),
        track_tag_sources=tuple(dict.fromkeys([*assembly.track_tag_sources, *extra_tag_sources])),
    )


def build_native_plan(
    config: RemuxConfig,
    *,
    output_contract=None,
    streams: StreamArtifacts | None = None,
    origin_tag_sources: frozenset[Path] = frozenset(),
) -> MatroskaMuxPlan:
    """Plan bas niveau du writer, compilé via le contrat d'assemblage partagé.

    ``streams`` : sources ISO BMFF lues en flux (sinon déjà canonicalisées).
    """
    assembly = remux_assembly_plan(config)
    if streams is not None:
        assembly = _readable_origins(assembly, config, streams, origin_tag_sources)
        assembly = stream_assembly_plan(assembly, config, streams)
    if output_contract is None:
        from core.matroska.assembly import assembly_output_contract
        output_contract = assembly_output_contract(assembly)
    assembly = replace(assembly, expected_output_contract=output_contract)
    if streams is None:
        return compile_assembly_plan(assembly)
    return compile_assembly_plan(assembly, readers=streams.readers, reopen=streams.reopen,
                                 stream_keys=streams.stream_keys)


def run_native_remux(
    config: RemuxConfig,
    *,
    log: Callable[[str, str], None],
    log_step: Callable[[int, str], None],
    ffmpeg_bin: str,
    ffprobe_bin: str = "ffprobe",
    finalize: Callable[[Path], None] = lambda _path: None,
    plan: MuxExecutionPlan | None = None,
    validation_override: ValidationOverride | None = None,
    sync_rewrite: SyncRewriteSettings | None = None,
) -> TaskSignals:
    """Runner natif : mêmes signaux ``TaskSignals`` que le runner FFmpeg.

    ``sync_rewrite`` : réglages de la réécriture de synchro (mode container),
    même décision et mêmes artefacts que le backend FFmpeg ; ``None`` = aucune
    réécriture (appelants historiques).

    Toutes les commandes externes sont enregistrées dans les signaux (donc
    annulables), les fichiers temporaires sont nettoyés, et l'écriture native
    transmet une progression en compteurs (paquets/octets) sans pourcentage
    artificiel.
    """
    work_root = config.work_dir or Path(tempfile.gettempdir())
    process_work_dir = prepare_process_work_dir(
        work_root,
        output_path=config.output,
        fallback_name="remux_job",
    )
    signals = TaskSignals()

    def task() -> None:
        canonical_root: Path | None = None
        # Sources ISO BMFF lues en flux (producteurs FFmpeg sur pipes), fermées en fin de tâche.
        stream_inputs = StreamInputs(
            ffmpeg_bin, process_work_dir, ffprobe_bin=ffprobe_bin,
            cancelled=signals._cancel_event.is_set,
            register=signals._register_proc, unregister=signals._unregister_proc,
            log=log,
        )
        streams = StreamArtifacts(stream_inputs)

        def _check_cancel() -> None:
            if signals._cancel_event.is_set():
                raise TaskCancelledError()

        def _run_external(cmd: list[str], label: str) -> None:
            """Commande externe enregistrée dans TaskSignals, annulable proprement."""
            _check_cancel()
            # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
            proc = subprocess.Popen(  # nosec B603
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                **subprocess_text_kwargs(),
            )
            signals._register_proc(proc)
            try:
                output, _ = proc.communicate()
            finally:
                signals._unregister_proc(proc)
            _check_cancel()
            if proc.returncode:
                raise RuntimeError(f"{label}: {str(output or '').strip()}")

        def _ensure_canonical_root() -> Path:
            nonlocal canonical_root
            if canonical_root is None:
                canonical_root = Path(tempfile.mkdtemp(
                    prefix="Muxiveo_native_", dir=process_work_dir,
                ))
            return canonical_root

        try:
            _check_cancel()
            relocated_attachments = relocate_tmdb_covers_to_process_dir(
                [Path(path) for path in config.extra_attachments],
                work_root=work_root,
                process_dir=process_work_dir,
            )
            runtime_config = replace(config, extra_attachments=relocated_attachments)
            prepared_config = runtime_config
            if config.sync_mode == "physical":
                from core.workflows.physical_sync import prepare_physical
                prepared_config = prepare_physical(
                    runtime_config, _ensure_canonical_root(), ffmpeg_bin, _run_external, log=log,
                    ffprobe=ffprobe_bin, cancel_cb=signals._cancel_event.is_set,
                )
            native_rewrites: list[NativeRewrite] = []
            if sync_rewrite is not None and prepared_config.sync_mode == "container":
                rewrite_root = _ensure_canonical_root() / "sync_rewrite"
                rewrite_root.mkdir(parents=True, exist_ok=True)
                empty_count = [0]

                def _empty_subtitle(mapped) -> Path:
                    """Sous-titre vidé par la réécriture : piste conservée, TrackEntry de l'origine."""
                    origin_path = Path(mapped.source_path)
                    if origin_path.suffix.lower() in MATROSKA_EXTENSIONS:
                        header, index = origin_path, int(mapped.stream_index)
                    else:
                        header, index = streams.metadata(StreamTrackSpec(
                            origin_path, int(mapped.stream_index), "subtitle",
                            mapped.track.orig_codec or mapped.track.codec,
                        ))
                    origin_track = MatroskaReader(header).tracks()[index]
                    empty_count[0] += 1
                    target = rewrite_root / f"native_empty_subtitle_{empty_count[0]}.mks"
                    MatroskaWriter().write(MatroskaMuxPlan(
                        output=target,
                        tracks=(MatroskaMuxTrack(
                            header, origin_track, 1, origin_track.uid,
                            patch_language=False, patch_name=False, patch_flags=False,
                        ),),
                        packets=(),
                    ))
                    return target

                prepared_config, native_rewrites = prepare_native_sync_rewrites(
                    prepared_config, sync_rewrite,
                    service=SyncRewriteService(
                        ffmpeg_bin=ffmpeg_bin,
                        ffprobe_bin=ffprobe_bin,
                        ffmpeg_progress_args=ffmpeg_progress_args(),
                        ffmpeg_thread_args=[],
                        audio_bitrate_per_channel=dict(sync_rewrite.audio_bitrates),
                        advanced_audio_enabled=sync_rewrite.advanced_audio,
                        log_cb=lambda message: log("INFO", message),
                        progress_cb=signals.progress.emit,
                    ),
                    ffprobe_bin=ffprobe_bin,
                    tmp_dir=rewrite_root,
                    empty_subtitle_artifact=_empty_subtitle,
                    cancel_cb=signals._cancel_event.is_set,
                )
                if native_rewrites:
                    log_step(2, "Réécriture réelle des décalages audio/sous-titres (assemblage natif)")
                _check_cancel()
            execution_plan = (plan if prepared_config is runtime_config else None) or plan_remux(
                prepared_config, ffmpeg_bin=ffmpeg_bin, ffprobe_bin=ffprobe_bin,
            )
            log("INFO", "Backend Matroska natif multi-pistes sélectionné (plan v1).")
            # Sondé dans le fil de travail (``-version`` jamais sur le fil de l'interface).
            log("INFO", ffmpeg_version_log_line(ffmpeg_bin))
            run_config = replace(prepared_config, sources=[
                replace(
                    source,
                    origin_identity=source.origin_identity or deterministic_source_identity(source.path),
                )
                for source in prepared_config.sources
            ])
            output_contract = execution_plan.output_contract
            if config.tmdb_cover is not None:
                cover_url, cover_name = config.tmdb_cover
                cover_name = normalized_tmdb_cover_filename(cover_name)
                try:
                    cover = download_tmdb_cover(cover_url, cover_name, _ensure_canonical_root() / "attachments")
                    run_config = replace(run_config, extra_attachments=[*run_config.extra_attachments, cover], tmdb_cover=None)
                except Exception as exc:
                    # Parité avec le backend FFmpeg : la cover est optionnelle —
                    # warning, poursuite sans cover et attente retirée du contrat
                    # (clé = nom canonique, identique à l'attente).
                    log("WARN", f"Impossible de télécharger la cover TMDB : {exc}")
                    run_config = replace(run_config, tmdb_cover=None)
                    output_contract = without_expected_attachment(
                        output_contract,
                        canonical_attachment_output_name(Path(cover_name)),
                    )
            _check_cancel()

            # Attachments de type attached_pic provenant d'un conteneur non-MKV :
            # le plan les matérialise en fichiers, puis l'assembleur les traite
            # comme attachments externes (aucun local_index canonicalisé).
            attachment_actions = [
                action for action in execution_plan.preparation_actions
                if action.kind == "extract_attachment"
            ]
            if attachment_actions:
                root = _ensure_canonical_root() / "attachments"
                root.mkdir(parents=True, exist_ok=True)
                extracted: list[Path] = []
                extracted_sources = {
                    action.source_file_index for action in attachment_actions
                    if action.source_file_index is not None
                }
                for action in attachment_actions:
                    target = root / action.target_name
                    command = list(action.command)
                    command[-1] = str(target)
                    _run_external(command, action.description)
                    extracted.append(target)
                run_config = replace(
                    run_config,
                    extra_attachments=[*run_config.extra_attachments, *extracted],
                    sources=[
                        replace(
                            source,
                            selected_attachments=[
                                attachment for attachment in source.selected_attachments
                                if not (
                                    source.file_index in extracted_sources
                                    and bool(getattr(attachment, "is_attached_pic", False))
                                )
                            ],
                        )
                        for source in run_config.sources
                    ],
                )
                _check_cancel()

            canonical_source_indexes = {
                action.source_file_index
                for action in execution_plan.preparation_actions
                if action.kind == "canonicalize_source" and action.source_file_index is not None
            }
            non_matroska = [
                source for source in run_config.sources
                if source.file_index in canonical_source_indexes
            ]
            if non_matroska:
                log_step(2, "Canonicalisation Matroska des sources non-MKV")
                root = _ensure_canonical_root()
                replacements: dict[int, Path] = {}
                for source in non_matroska:
                    index_map = execution_plan.canonical_index_map(source.file_index)
                    target = root / f"source_{source.file_index}.mkv"
                    cmd = build_canonicalization_command(
                        source, target, ffmpeg_bin,
                        selected_streams=sorted(index_map),
                    )
                    _run_external(cmd, f"Canonicalisation impossible pour {source.path.name}")
                    replacements[source.file_index] = target
                # Correspondance explicite index source original → index canonique
                new_sources: list[SourceInput] = []
                for source in run_config.sources:
                    if source.file_index not in replacements:
                        new_sources.append(source)
                        continue
                    index_map = execution_plan.canonical_index_map(source.file_index)
                    kept_tracks = [
                        replace(track, mkv_tid=index_map[track.mkv_tid])
                        for track in source.tracks
                        if track.mkv_tid in index_map
                    ]
                    new_sources.append(replace(
                        source,
                        path=replacements[source.file_index],
                        tracks=kept_tracks or source.tracks,
                    ))
                new_order: list[tuple[int, int] | tuple[int, int, str]] = []
                for order_item in run_config.track_order:
                    file_index, mkv_tid, entry_id = track_order_parts(order_item)
                    if file_index in replacements:
                        mkv_tid = execution_plan.canonical_index_map(file_index).get(mkv_tid, mkv_tid)
                    new_order.append((file_index, mkv_tid, entry_id) if entry_id else (file_index, mkv_tid))
                run_config = replace(run_config, sources=new_sources, track_order=new_order)
                _check_cancel()

            if native_rewrites:
                # Origine native de chaque source d'origine (R(S)) : piste réécrite
                # placée par rapport à sa source comme le muxage final FFmpeg.
                # Routes directes restantes : une piste devenue variante n'en est plus une.
                variant_positions = {variant.order_index for variant in execution_plan.audio_variants}
                routed_sources = {
                    track_order_parts(item)[0]
                    for position, item in enumerate(run_config.track_order)
                    if position not in variant_positions
                }
                current_sources = {source.file_index: source for source in run_config.sources}

                def _native_origin(rewrite: NativeRewrite) -> float | None:
                    source = current_sources.get(rewrite.origin_source_index)
                    if source is None or rewrite.origin_source_index not in routed_sources:
                        return None
                    if rewrite.origin_path.suffix.lower() in MATROSKA_EXTENSIONS:
                        return 0.0
                    if is_streamable_source(rewrite.origin_path):
                        return probe_start_time(rewrite.origin_path, ffprobe_bin, signals._cancel_event.is_set)
                    return _measured_canonical_origin(
                        rewrite.origin_path, source, execution_plan.canonical_index_map(source.file_index),
                        ffprobe_bin, cancel_cb=signals._cancel_event.is_set,
                    )

                run_config = finalize_native_rewrite_shifts(run_config, native_rewrites, _native_origin)

            # Matérialisation des variantes audio planifiées (preview == exécution) ;
            # le writer natif garde la main sur le document multi-pistes final.
            if execution_plan.audio_variants:
                root = _ensure_canonical_root()
                source_by_index = {source.file_index: source for source in run_config.sources}
                next_source_index = max(source_by_index, default=-1) + 1
                new_sources = list(run_config.sources)
                variant_order: list[tuple[int, int] | tuple[int, int, str]] = []
                variants_by_index = {variant.order_index: variant for variant in execution_plan.audio_variants}
                for order_index, order_item in enumerate(run_config.track_order):
                    variant = variants_by_index.get(order_index)
                    if variant is None:
                        variant_order.append(order_item)
                        continue
                    file_index, mkv_tid, entry_id = track_order_parts(order_item)
                    source = source_by_index[file_index]
                    candidates = [track for track in source.tracks if track.mkv_tid == mkv_tid]
                    track = next(
                        (item for item in candidates if not entry_id or item.entry_id == entry_id),
                        None,
                    )
                    if track is None:
                        raise ValueError(
                            f"Piste de variante introuvable : source={file_index}, stream={mkv_tid}"
                        )
                    target_path = root / variant.target_name
                    rewritten = track.rewrite_origin is not None
                    # Variante d'une piste ordinaire : chronologie de sa piste d'origine telle que
                    # l'assemblage la lit (audio en retard sur la vidéo compris) : horodatages bruts
                    # d'un Matroska (-copyts), rebasés sur le départ du conteneur pour une source
                    # lue en flux (MP4/MOV, comme le lecteur en flux).
                    cmd = build_audio_variant_command(
                        variant, source.path, mkv_tid, target_path, ffmpeg_bin,
                        copyts=not rewritten and not is_streamable_source(source.path),
                    )
                    _run_external(cmd, f"Variante audio {variant.target_codec} impossible")
                    materialized_track = replace(track, mkv_tid=0, orig_codec=track.codec, is_new=False)
                    if rewritten:
                        # Variante d'une piste réécrite : V issue de A, placée comme A (Δ(A) − Δ(V)).
                        materialized_track = replace(materialized_track, time_shift_ms=int(track.time_shift_ms or 0) + round(
                            (ffmpeg_input_origin(probe_origin_payload(source.path, ffprobe_bin))
                             - ffmpeg_input_origin(probe_origin_payload(target_path, ffprobe_bin))) * 1000
                        ))
                    new_source = SourceInput(
                        target_path, next_source_index, [materialized_track],
                        origin_identity=f"{source.origin_identity}:audio:{variant.stream_index}:{variant.target_codec}",
                    )
                    new_sources.append(new_source)
                    variant_order.append((next_source_index, 0, materialized_track.entry_id))
                    next_source_index += 1
                run_config = replace(run_config, sources=new_sources, track_order=variant_order)
                _check_cancel()

            log_step(3, "Construction du plan Matroska natif")
            if any(action.kind == "stream_source" for action in execution_plan.preparation_actions):
                log("INFO", "Lecture en flux des sources MP4/MOV (sans fichier intermédiaire).")
            plan_matroska = build_native_plan(
                run_config, output_contract=output_contract, streams=streams,
                origin_tag_sources=frozenset(
                    rewrite.origin_path for rewrite in native_rewrites
                    if any(source.file_index == rewrite.origin_source_index and source.copy_tags
                           for source in config.sources)
                ),
            )
            # Contrat des chapitres fixé sur les chapitres réellement écrits (ChapterTrack remappés, chapitres sans
            # piste ciblée retirés, chapitres d'un MP4 lu en flux) : jamais sur le seul précalcul du plan.
            if output_contract is not None:
                output_contract = replace(output_contract, expects_chapters=any(
                    item.startswith(CHAPTERS_ID) for item in plan_matroska.opaque_top_level))
            if plan_matroska.chapters_removed:
                log("WARN", f"{plan_matroska.chapters_removed} chapitre(s) retiré(s) : toutes leurs pistes ciblées "
                            "(ChapterTrack) sont absentes de la sortie.")
            log_step(4, "Écriture Matroska native multi-pistes")
            if any(track.remove_closed_captions for track in plan_matroska.tracks):
                log("INFO", "Retrait ciblé des sous-titres embarqués EIA-608/708 (sans réencodage)")

            def validate_partial(path: Path, packet_validation: MatroskaPacketValidation) -> None:
                errors = validate_matroska_output(
                    path, output_contract, packet_validation=packet_validation,
                )
                validate_final_output(
                    path, errors, lambda: _run_external(
                    [
                        ffprobe_bin, "-v", "error", "-show_entries",
                        "format=format_name", "-of", "json", str(path),
                    ],
                    "Validation ffprobe de la sortie native impossible",
                    ),
                    message_prefix="Validation sémantique de la sortie native échouée : ",
                    override=validation_override,
                    cancelled=signals._cancel_event.is_set,
                    warn=lambda message: log("WARN", message),
                )
                for message in interleave_warning_lines(
                    packet_validation.interleave, lambda: mux_track_labels(plan_matroska.tracks),
                ):
                    log("WARN", message)

            MatroskaWriter().write(
                plan_matroska,
                external_validator=validate_partial,
                validation_error_handler=lambda path, message: accept_validation_override(
                    validation_override, path, message, signals._cancel_event.is_set,
                    lambda msg: log("WARN", msg),
                ),
                cancel_cb=signals._cancel_event.is_set,
                progress_cb=native_mux_progress_callback(signals.progress.emit, label="Écriture Matroska"),
            )
            log_step(5, "Validation structure native terminée")
            # Un échec NFO après commit ne transforme plus un média valide en
            # workflow échoué.
            try:
                finalize(runtime_config.output)
            except Exception as exc:
                log("WARN", f"Post-traitement (NFO) échoué après commit : {exc}")
            signals.finished.emit(str(runtime_config.output))
        except (TaskCancelledError, MatroskaWriteCancelled):
            # Le writer supprime lui-même son candidat réservé : aucun chemin
            # `.partial` calculé n'est supprimé ici (il pourrait être celui d'un autre job).
            signals.cancelled.emit()
        except Exception as exc:
            signals.failed.emit(str(exc), exc)
        finally:
            stream_inputs.close()
            if canonical_root is not None:
                shutil.rmtree(canonical_root, ignore_errors=True)
            remove_path(process_work_dir)

    executor = ThreadPoolExecutor(max_workers=1)
    signals.watch_future(executor.submit(task))
    executor.shutdown(wait=False)
    return signals


__all__ = [
    "FfmpegRemuxBackend", "MuxBackendDecision", "MuxExecutionPlan",
    "NativeMatroskaBackend",
    "RemuxBackend", "build_native_plan", "mux_backend_report", "stream_assembly_plan",
    "build_canonicalization_command",
    "MATROSKA_EXTENSIONS",
    "native_capability_reasons", "native_preparation_commands",
    "plan_remux",
    "run_native_remux", "select_mux_backend",
]
