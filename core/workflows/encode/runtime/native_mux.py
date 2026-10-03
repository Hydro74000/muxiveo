"""Assemblage final Matroska natif des workflows encode (lot 2).

Les services de préparation matérialisent les pistes vidéo (artefacts MKV
NVEncC/multi-vidéo/encode découpé) et audio (encodage isolé) ; ce module
compile ensuite le contrat partagé :class:`MatroskaAssemblyPlan` et écrit la
sortie via le writer natif — commit atomique, validation sémantique interne
puis ffprobe en second validateur, sans aucun post-patch conteneur.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from core.bluray import ffprobe_input_args
from core.runner import TaskCancelledError, TaskSignals
from core.subprocess_utils import subprocess_text_kwargs
from core.subtitle_codec import plan_subtitle_codec
from core.workdir import remove_path
from core.workflows.common.metadata import matroska_video_tag_args
from core.workflows.encode.domain.codecs import audio_codec_args
from core.workflows.encode.models import EncodeConfig, EncodeError
from core.matroska.assembly import (
    MatroskaAssemblyPlan,
    MatroskaAssemblyTrack,
    MatroskaTrackFlags,
    assembly_output_contract,
    compile_assembly_plan,
)
from core.matroska.mux_plan import deterministic_source_identity
from core.matroska.validation import MatroskaPacketValidation, validate_matroska_output
from core.matroska.reader import MatroskaReader
from core.matroska.writer import (
    MatroskaWriteCancelled,
    MatroskaWriteProgress,
    MatroskaWriter,
)
from core.workflows.encode.planning.sources import resolve_source_layout
from core.workflows.encode.planning.plan_models import PlannedTrackMetadata
from core.workflows.encode.planning.track_metadata import resolve_track_metadata
from core.workflows.remux_plan import MATROSKA_EXTENSIONS


#: Type Matroska « subtitle » dans les TrackEntry natifs.
_SUBTITLE_TRACK_TYPE = 17


@dataclass(frozen=True)
class NativeVideoArtifactRef:
    """Artefact vidéo Matroska prêt pour l'assemblage final."""

    path: Path
    track_index: int = 0
    offset_ms: int = 0


@dataclass(frozen=True)
class NativeEncodePreparation:
    """Artefacts nécessaires à un assemblage final Matroska natif.

    Une piste copiée depuis MP4/MOV/TS ne peut pas être lue directement par
    :class:`MatroskaReader`.  Chaque source non Matroska est donc remballée
    *une seule fois* en MKV, limitée aux flux réellement utilisés (vidéo
    copiée, audio copié, sous-titres, porteur de métadonnées).  Ce n'est pas
    un mux final FFmpeg : le writer interne reste l'unique producteur de la
    sortie demandée.
    """

    #: (source, index ffprobe) → (artefact MKV, position de piste dans l'artefact).
    track_artifacts: dict[tuple[Path, int], tuple[Path, int]] = field(default_factory=dict)
    container_artifacts: dict[Path, Path] = field(default_factory=dict)
    resolved_subtitles: tuple[tuple[Path, int], ...] = ()
    subtitles_prepared: bool = False
    cleanup_paths: tuple[Path, ...] = ()

    def artifact_for_track(self, source: Path, stream_index: int) -> tuple[Path, int]:
        """Artefact lisible et position de piste ; une source Matroska se lit telle quelle."""
        key = (Path(source), int(stream_index))
        return self.track_artifacts.get(key, key)

    def artifact_for_container(self, source: Path) -> Path:
        return self.container_artifacts.get(Path(source), Path(source))


@dataclass(frozen=True)
class ProbedStream:
    """Flux d'une source vu par ffprobe."""

    codec_type: str
    codec_name: str
    attached_pic: bool = False


def _is_matroska(path: Path) -> bool:
    return path.suffix.lower() in MATROSKA_EXTENSIONS


def _offset_ms(config: EncodeConfig, track_type: str, source: Path, stream_index: int) -> int:
    """Return the final-mux offset for one logical output track.

    The video runners already materialise their own offset in
    ``NativeVideoArtifactRef``.  Audio and subtitle tracks reach this module
    unchanged, so applying their offset here keeps a native mux from silently
    losing the setting.
    """
    source = Path(source)
    for offset in config.track_time_offsets:
        if (
            str(offset.track_type) == track_type
            and Path(offset.source_path) == source
            and int(offset.stream_index) == int(stream_index)
        ):
            return int(offset.offset_ms or 0)
    return 0


def probe_source_streams(source: Path, ffprobe_bin: str) -> dict[int, ProbedStream] | None:
    """Flux ffprobe de ``source`` (index → type, codec, attached_pic).

    Retourne ``None`` si la sonde échoue (erreur, timeout, sortie illisible),
    à distinguer d'une source sans flux : l'appelant décide d'interrompre ou
    d'un repli explicite.
    """
    try:
        result = subprocess.run(
            [
                ffprobe_bin, "-v", "quiet", "-print_format", "json",
                "-show_entries",
                "stream=index,codec_type,codec_name:stream_disposition=attached_pic",
                *ffprobe_input_args(source),
            ],
            capture_output=True,
            check=False,
            timeout=20,
            **subprocess_text_kwargs(),
        )
        if result.returncode != 0:
            return None
        payload = json.loads(result.stdout or "{}")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None
    raw_streams = payload.get("streams") if isinstance(payload, dict) else None
    if not isinstance(raw_streams, list):
        return None
    streams: dict[int, ProbedStream] = {}
    for stream in raw_streams:
        try:
            streams[int(stream.get("index"))] = ProbedStream(
                codec_type=str(stream.get("codec_type") or "").lower(),
                codec_name=str(stream.get("codec_name") or "").lower(),
                attached_pic=bool((stream.get("disposition") or {}).get("attached_pic")),
            )
        except (TypeError, ValueError):
            continue
    return streams


def _metadata_carrier(streams: dict[int, ProbedStream]) -> int | None:
    """Flux le plus léger pour porter chapitres/tags quand aucune piste n'est copiée."""
    for codec_type in ("subtitle", "audio", "video"):
        for index, stream in sorted(streams.items()):
            if stream.codec_type != codec_type or stream.attached_pic:
                continue
            if codec_type == "subtitle":
                try:
                    plan_subtitle_codec(stream.codec_name)
                except ValueError:
                    continue
            return index
    return None


def _subtitle_conversion_args(codecs: list[str]) -> list[str]:
    """Args ``-c:s:N <codec>`` pour les sous-titres refusés tels quels par Matroska.

    ``codecs`` suit l'ordre de sortie des sous-titres. Un codec non supporté
    reste en copie : FFmpeg signalera l'erreur s'il est réellement refusé.
    """
    args: list[str] = []
    for out_index, codec in enumerate(codecs):
        try:
            codec_arg, _warning = plan_subtitle_codec(codec)
        except ValueError:
            continue
        if codec_arg != "copy":
            args.extend([f"-c:s:{out_index}", codec_arg])
    return args


def prepare_native_encode_inputs(
    config: EncodeConfig,
    *,
    work_dir: Path,
    ffmpeg_bin: str,
    run_cmd: Callable[[list[str], str], str],
    resolved_subtitles: list[tuple[Path, int]] | None = None,
    ffprobe_bin: str = "ffprobe",
    video_refs: list[tuple[Path, int]] | None = None,
) -> NativeEncodePreparation:
    """Matérialise en MKV les entrées non Matroska de l'assemblage natif.

    Une seule canonicalisation par source : seuls les flux utilisés
    (``video_refs`` copiées, audio copié, sous-titres) sont transposés, les
    paquets et horodatages étant conservés.  Chapitres et tags globaux ne
    sont repris que si la source en est le fournisseur ; à défaut de flux
    utilisé, le flux le plus léger sert de porteur.  Les sous-titres que
    Matroska refuse en copie (``mov_text``…) sont convertis en SRT au
    passage (:func:`plan_subtitle_codec`), les tags vidéo MP4 Dolby Vision
    neutralisés (:func:`matroska_video_tag_args`).
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    tracks: dict[tuple[Path, int], tuple[Path, int]] = {}
    containers: dict[Path, Path] = {}
    cleanup: list[Path] = []
    probed: dict[Path, dict[int, ProbedStream] | None] = {}

    def _streams(source: Path) -> dict[int, ProbedStream] | None:
        if source not in probed:
            probed[source] = probe_source_streams(source, ffprobe_bin)
        return probed[source]

    def _run(command: list[str], label: str, target: Path) -> None:
        try:
            run_cmd(command, label)
        except Exception:
            # Un outil peut laisser un MKV partiel avant de signaler son
            # erreur : il n'a pas encore rejoint ``cleanup`` à ce stade.
            remove_path(target)
            raise
        if not target.is_file():
            raise EncodeError(f"Préparation native incomplète : {target.name} absent")
        cleanup.append(target)

    try:
        context_sources: set[Path] = set()
        if config.keep_chapters and config.chapter_overrides is None:
            context_sources.add(Path(config.source))
        if config.tag_overrides is None:
            context_sources.update(Path(path) for path in config.tag_sources)

        if resolved_subtitles is not None:
            subtitles = [(Path(path), int(index)) for path, index in resolved_subtitles]
        elif not config.copy_subtitles:
            subtitles = []
        else:
            subtitles = []
            for source in resolve_source_layout(config).sources:
                source = Path(source)
                if _is_matroska(source):
                    for index, track in enumerate(MatroskaReader(source).tracks()):
                        if track.track_type == _SUBTITLE_TRACK_TYPE:
                            subtitles.append((source, index))
                    continue
                source_streams = _streams(source)
                if source_streams is None:
                    # « Aucun sous-titre » et « sonde en échec » ne doivent pas
                    # se confondre : la copie demandée serait perdue en silence.
                    raise EncodeError(
                        f"{source.name} : sonde ffprobe en échec, sous-titres à "
                        "copier indéterminables."
                    )
                subtitles.extend(
                    (source, index)
                    for index, stream in sorted(source_streams.items())
                    if stream.codec_type == "subtitle"
                )

        # Rôle connu de chaque flux sélectionné : la sonde peut échouer.
        roles: dict[tuple[Path, int], str] = {}
        for path, index in video_refs or ():
            roles.setdefault((Path(path), int(index)), "video")
        for audio in config.audio_tracks:
            if str(audio.codec or "copy").strip().lower() == "copy" and not audio.extract_truehd_core:
                roles.setdefault(
                    (Path(audio.source_path or config.source), int(audio.stream_index)), "audio",
                )
        for key in subtitles:
            roles.setdefault(key, "subtitle")

        wanted: dict[Path, set[int]] = {
            source: set() for source in context_sources if not _is_matroska(source)
        }
        for source, index in roles:
            if not _is_matroska(source):
                wanted.setdefault(source, set()).add(index)

        for ordinal, source in enumerate(sorted(wanted)):
            probed_streams = _streams(source)
            streams = probed_streams or {}
            indexes = sorted(wanted[source])
            if not indexes:
                # Sonde en échec : repli explicite sur le flux 0, présent dans
                # tout média (porteur plus lourd, contexte conservé).
                carrier = _metadata_carrier(streams) if probed_streams is not None else 0
                if carrier is None:
                    raise EncodeError(
                        f"{source.name} : aucun flux pour porter chapitres et tags."
                    )
                indexes = [carrier]

            def _kind(index: int) -> str:
                role = roles.get((source, index))
                return role or (streams[index].codec_type if index in streams else "")

            def _codec(index: int) -> str:
                return streams[index].codec_name if index in streams else ""

            keep_context = source in context_sources
            target = work_dir / f"native_source_{ordinal}.mkv"
            command = [
                ffmpeg_bin, "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
                "-i", str(source),
            ]
            for index in indexes:
                command.extend(["-map", f"0:{index}"])
            command.extend(["-c", "copy"])
            command.extend(matroska_video_tag_args(
                [_codec(index).upper() for index in indexes if _kind(index) == "video"]
            ))
            command.extend(_subtitle_conversion_args(
                [_codec(index) for index in indexes if _kind(index) == "subtitle"]
            ))
            context_arg = "0" if keep_context else "-1"
            command.extend(["-map_metadata", context_arg, "-map_chapters", context_arg, str(target)])
            _run(command, f"ffmpeg-native-canonical-source-{ordinal}", target)
            for position, index in enumerate(indexes):
                tracks[(source, index)] = (target, position)
            if keep_context:
                containers[source] = target
    except Exception:
        for path in cleanup:
            remove_path(path)
        raise

    return NativeEncodePreparation(
        track_artifacts=tracks,
        container_artifacts=containers,
        resolved_subtitles=tuple(subtitles),
        subtitles_prepared=(resolved_subtitles is not None or config.copy_subtitles),
        cleanup_paths=tuple(cleanup),
    )


def resolve_native_subtitle_tracks(config: EncodeConfig) -> list[tuple[Path, int]]:
    """Sous-titres à copier, résolus sur TOUTES les sources du layout.

    Miroir natif de la résolution du plan (``copy_subtitles`` sans sélection
    explicite) : chaque source Matroska du layout est inspectée. Une source
    non Matroska lève — le sélecteur de backend doit avoir bloqué en amont ;
    échouer bruyamment vaut mieux qu'une perte silencieuse de pistes.
    """
    if config.subtitle_tracks:
        return [(Path(path), int(index)) for path, index in config.subtitle_tracks]
    if not config.copy_subtitles:
        return []
    resolved: list[tuple[Path, int]] = []
    for source in resolve_source_layout(config).sources:
        source = Path(source)
        if not source.is_file():
            continue
        if source.suffix.lower() not in MATROSKA_EXTENSIONS:
            raise EncodeError(
                f"Assemblage natif : sous-titres à copier depuis une source non "
                f"Matroska ({source.name}) — chemin FFmpeg requis."
            )
        for position, track in enumerate(MatroskaReader(source).tracks()):
            if track.track_type == _SUBTITLE_TRACK_TYPE:
                resolved.append((source, position))
    return resolved


def build_encode_assembly_plan(
    config: EncodeConfig,
    *,
    video_artifacts: list[NativeVideoArtifactRef],
    materialized_audio: dict[int, Path],
    resolved_subtitles: list[tuple[Path, int]] | None = None,
    track_metadata: tuple[PlannedTrackMetadata, ...] | None = None,
    preparation: NativeEncodePreparation | None = None,
) -> MatroskaAssemblyPlan:
    """Compile la configuration encode vers le contrat d'assemblage partagé.

    ``materialized_audio`` mappe l'index de la piste audio de la config vers
    son artefact MKV mono-piste (pistes réencodées) ; les pistes ``copy``
    restent référencées directement dans leur source Matroska.
    ``resolved_subtitles`` transporte la résolution du plan d'encodage
    (toutes sources) ; à ``None``, la résolution native équivalente
    (:func:`resolve_native_subtitle_tracks`) est appliquée.
    """
    preparation = preparation or NativeEncodePreparation()
    identities: dict[Path, str] = {}

    def _identity(path: Path) -> str:
        return identities.setdefault(path, deterministic_source_identity(path))

    ordered: list[MatroskaAssemblyTrack] = []
    for ref in video_artifacts:
        artifact, track_index = preparation.artifact_for_track(ref.path, ref.track_index)
        ordered.append(MatroskaAssemblyTrack(
            artifact=artifact,
            artifact_track_index=track_index,
            source_identity=_identity(ref.path),
            time_shift_ms=int(ref.offset_ms or 0),
        ))

    for audio_index, audio in enumerate(config.audio_tracks):
        source = Path(audio.source_path or config.source)
        artifact = materialized_audio.get(audio_index)
        if artifact is not None:
            ordered.append(MatroskaAssemblyTrack(
                artifact=artifact,
                artifact_track_index=0,
                source_identity=_identity(source),
                provenance=f"audio:{audio.stream_index}:{audio.codec}",
            ))
        else:
            track_artifact, track_index = preparation.artifact_for_track(source, audio.stream_index)
            ordered.append(MatroskaAssemblyTrack(
                artifact=track_artifact,
                artifact_track_index=track_index,
                source_identity=_identity(source),
                time_shift_ms=_offset_ms(config, "audio", source, audio.stream_index),
            ))

    if resolved_subtitles is not None:
        subtitles = [(Path(path), int(index)) for path, index in resolved_subtitles]
    elif preparation.subtitles_prepared:
        subtitles = list(preparation.resolved_subtitles)
    else:
        subtitles = resolve_native_subtitle_tracks(config)
    for subtitle_path, subtitle_index in subtitles:
        track_artifact, track_index = preparation.artifact_for_track(subtitle_path, subtitle_index)
        ordered.append(MatroskaAssemblyTrack(
            artifact=track_artifact,
            artifact_track_index=track_index,
            source_identity=_identity(subtitle_path),
            time_shift_ms=_offset_ms(config, "subtitle", subtitle_path, subtitle_index),
        ))

    if track_metadata is None:
        videos = list(config.video_tracks or ([config.video] if config.video is not None else []))
        track_metadata = resolve_track_metadata(
            config,
            video_refs=(
                (
                    Path(getattr(video, "source_path", None) or config.source),
                    int(getattr(video, "stream_index", 0) or 0),
                )
                for video in videos
            ),
            subtitle_refs=subtitles,
        )

    # Force les valeurs de la source logique sur les artefacts préparés : un
    # HEVC brut remballé ou un audio réencodé ne doit pas devenir la nouvelle
    # source de vérité de la langue, du titre ou des dispositions.
    for position, metadata in enumerate(track_metadata):
        if not 0 <= position < len(ordered):
            continue
        planned_flags = metadata.flags
        flags = None
        if planned_flags is not None and all(
            value is not None
            for value in (
                planned_flags.enabled,
                planned_flags.default,
                planned_flags.forced,
                planned_flags.hearing_impaired,
                planned_flags.visual_impaired,
                planned_flags.original,
                planned_flags.commentary,
            )
        ):
            flags = MatroskaTrackFlags(
                enabled=bool(planned_flags.enabled),
                default=bool(planned_flags.default),
                forced=bool(planned_flags.forced),
                hearing_impaired=bool(planned_flags.hearing_impaired),
                visual_impaired=bool(planned_flags.visual_impaired),
                original=bool(planned_flags.original),
                commentary=bool(planned_flags.commentary),
            )
        ordered[position] = replace(
            ordered[position],
            language_value=metadata.language,
            name=metadata.name,
            flags=flags,
        )

    chapter_entries = tuple(config.chapter_overrides) if config.chapter_overrides is not None else None
    chapter_source: Path | None = None
    if (
        chapter_entries is None
        and config.keep_chapters
        and _is_matroska(preparation.artifact_for_container(Path(config.source)))
    ):
        chapter_source = preparation.artifact_for_container(Path(config.source))

    # Repli sur l'artefact (déjà redirigé) de la première vidéo : une vidéo
    # copiée depuis un MP4 n'est lisible que via son MKV canonique.
    segment_info_source = (
        preparation.artifact_for_container(Path(config.source))
        if _is_matroska(preparation.artifact_for_container(Path(config.source)))
        and preparation.artifact_for_container(Path(config.source)).is_file()
        else (ordered[0].artifact if video_artifacts else None)
    )

    return MatroskaAssemblyPlan(
        output=config.output,
        ordered_tracks=tuple(ordered),
        extra_attachment_files=tuple(Path(path) for path in config.extra_attachments),
        chapter_entries=chapter_entries,
        chapter_source=chapter_source,
        tag_overrides=dict(config.tag_overrides) if config.tag_overrides is not None else None,
        tag_copy_sources=tuple(
            preparation.artifact_for_container(Path(path)) for path in config.tag_sources
        ) if config.tag_overrides is None else (),
        segment_title=(
            config.file_title
            if config.file_title.strip() or config.tag_overrides is not None
            else None
        ),
        title_tag_value=config.file_title,
        segment_info_source=segment_info_source,
    )


def materialize_audio_artifacts(
    config: EncodeConfig,
    *,
    work_dir: Path,
    ffmpeg_bin: str,
    run_cmd: Callable[[list[str], str], str],
    cleanup_paths: list[Path] | None = None,
) -> dict[int, Path]:
    """Matérialise en MKV mono-piste les pistes audio réencodées.

    Les pistes ``copy`` sans BSF restent copiées directement depuis leur
    source Matroska par l'assembleur (aucun artefact nécessaire).
    """
    artifacts: dict[int, Path] = {}
    for audio_index, audio in enumerate(config.audio_tracks):
        codec = str(audio.codec or "copy").strip().lower()
        if codec == "copy" and not audio.extract_truehd_core:
            continue
        source = Path(audio.source_path or config.source)
        target = work_dir / f"native_audio_{audio_index}.mkv"
        if cleanup_paths is not None:
            cleanup_paths.append(target)
        command = [
            ffmpeg_bin, "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-i", str(source), "-map", f"0:{int(audio.stream_index)}",
        ]
        command.extend(audio_codec_args(0, audio))
        command.append(str(target))
        run_cmd(command, f"ffmpeg-native-audio-{audio_index}")
        artifacts[audio_index] = target
    return artifacts


def assemble_encode_output_native(
    config: EncodeConfig,
    *,
    video_artifacts: list[NativeVideoArtifactRef],
    work_dir: Path,
    signals: TaskSignals | None,
    run_cmd: Callable[[list[str], str], str],
    log: Callable[[str, str], None],
    ffmpeg_bin: str = "ffmpeg",
    ffprobe_bin: str = "ffprobe",
    resolved_subtitles: list[tuple[Path, int]] | None = None,
    track_metadata: tuple[PlannedTrackMetadata, ...] | None = None,
) -> Path:
    """Assemblage final natif : matérialisation audio, contrat, écriture atomique.

    Aucun post-patch MuxingApp/langue/Dolby Vision : la signalisation est
    écrite dans les ``TrackEntry`` lors de l'assemblage.
    ``resolved_subtitles`` : résolution des sous-titres issue du plan
    d'encodage (toutes sources) ; à ``None``, résolution native équivalente.
    """
    if not video_artifacts:
        raise EncodeError("Assemblage natif sans artefact vidéo.")
    log("INFO", "Assemblage final Matroska natif (plan partagé remux/encode).")
    audio_cleanup_paths: list[Path] = []
    preparation_cleanup_paths: tuple[Path, ...] = ()
    try:
        materialized_audio = materialize_audio_artifacts(
            config,
            work_dir=work_dir,
            ffmpeg_bin=ffmpeg_bin,
            run_cmd=run_cmd,
            cleanup_paths=audio_cleanup_paths,
        ) if config.audio_tracks else {}
        # Seules les pistes vidéo copiées depuis une source déclarée sont
        # canonicalisables : un artefact encodé non Matroska doit échouer,
        # pas être remuxé silencieusement (cadence d'un flux brut perdue).
        declared_sources = {Path(path) for path in resolve_source_layout(config).sources}
        preparation = prepare_native_encode_inputs(
            config,
            work_dir=work_dir,
            ffmpeg_bin=ffmpeg_bin,
            run_cmd=run_cmd,
            resolved_subtitles=resolved_subtitles,
            ffprobe_bin=ffprobe_bin,
            video_refs=[
                (ref.path, ref.track_index)
                for ref in video_artifacts
                if Path(ref.path) in declared_sources
            ],
        )
        preparation_cleanup_paths = preparation.cleanup_paths
        assembly = build_encode_assembly_plan(
            config,
            video_artifacts=video_artifacts,
            materialized_audio=materialized_audio,
            resolved_subtitles=resolved_subtitles,
            track_metadata=track_metadata,
            preparation=preparation,
        )
        dovi_video_indexes = {
            index
            for index, video in enumerate(config.video_tracks)
            if bool(getattr(video, "copy_dv", False))
        }
        contract = assembly_output_contract(
            assembly,
            require_block_addition_mapping=dovi_video_indexes,
        )
        assembly = replace(assembly, expected_output_contract=contract)
        mux_plan = compile_assembly_plan(assembly)

        def _validate(path: Path, packet_validation: MatroskaPacketValidation) -> None:
            errors = validate_matroska_output(
                path, contract, packet_validation=packet_validation,
            )
            if errors:
                raise EncodeError(
                    "Validation sémantique de la sortie native échouée : "
                    + " ; ".join(errors)
                )
            run_cmd(
                [
                    ffprobe_bin, "-v", "error", "-show_entries",
                    "format=format_name", "-of", "json", str(path),
                ],
                "ffprobe-native-validation",
            )

        progress_state = {"packets": 0, "bytes": 0}

        def _on_progress(progress: MatroskaWriteProgress) -> None:
            if signals is None:
                return
            if progress.percent is not None and hasattr(signals, "progress_pct"):
                signals.progress_pct.emit(progress.percent)
            if progress.stage != "clusters":
                pct_str = f"{progress.percent}% " if progress.percent is not None else ""
                signals.progress.emit(
                    f"Assemblage Matroska ({progress.stage}) : {pct_str}"
                    f"{progress.packets_written} paquets, "
                    f"{progress.bytes_written / (1024 * 1024):.1f} Mio"
                )
                return
            if (
                progress.packets_written - progress_state["packets"] >= 2000
                or progress.bytes_written - progress_state["bytes"] >= 64 * 1024 * 1024
            ):
                progress_state["packets"] = progress.packets_written
                progress_state["bytes"] = progress.bytes_written
                pct_str = f"{progress.percent}% " if progress.percent is not None else ""
                signals.progress.emit(
                    f"Assemblage Matroska : {pct_str}"
                    f"({progress.packets_written} paquets, "
                    f"{progress.bytes_written / (1024 * 1024):.1f} Mio)"
                )

        try:
            MatroskaWriter().write(
                mux_plan,
                external_validator=_validate,
                cancel_cb=(signals._cancel_event.is_set if signals is not None else None),
                progress_cb=_on_progress,
            )
        except MatroskaWriteCancelled as exc:
            # Annulation coopérative : convertie vers le contrat des runners
            # (qui n'interceptent que TaskCancelledError).
            raise TaskCancelledError() from exc
        log("INFO", "Assemblage Matroska natif terminé (aucun post-patch conteneur).")
        return config.output
    finally:
        for path in (*audio_cleanup_paths, *preparation_cleanup_paths):
            remove_path(path)


__all__ = [
    "NativeVideoArtifactRef",
    "NativeEncodePreparation",
    "assemble_encode_output_native",
    "build_encode_assembly_plan",
    "materialize_audio_artifacts",
    "prepare_native_encode_inputs",
    "probe_source_streams",
    "resolve_native_subtitle_tracks",
]
