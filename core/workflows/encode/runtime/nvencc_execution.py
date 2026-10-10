"""NVEncC runtime execution services for encode workflows."""

from __future__ import annotations

import math
import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from fractions import Fraction
from pathlib import Path
from typing import Callable

from core.bluray import append_ffmpeg_input_args, is_bluray_playlist
from core.pipeline_command import command_preview_tokens, command_stages, is_broken_pipe_exit, pipeline_root_failure
from core.workflows.encode.backends.progress import parse_nvencc_progress
from core.workflows.encode.interpolation import (
    expand_dynamic_hdr_metadata as _expand_dynamic_hdr_metadata,
    extract_hdr10plus_metadata as _extract_hdr10plus_metadata,
    ffprobe_beside as _ffprobe_beside,
    multiply_fps_expr as _multiply_fps_expr,
    probe_interpolation_source as _probe_interpolation_source,
    resolve_frame_ratio as _resolve_frame_ratio,
    stream_start_offset as _stream_start_offset,
)
from core.runner import TaskCancelledError, TaskSignals
from core.subprocess_utils import (
    decode_subprocess_output,
    subprocess_windows_no_window_kwargs,
)
from core.workflows.encode.models import EncodeConfig, EncodeError, VideoEncodeSettings
from core.workflows.encode.planning.offsets import track_offset_ms as _track_offset_ms_plan
from core.workflows.encode.planning.offsets import build_offset_specs as _build_offset_specs_plan
from core.workflows.encode.planning.plan_models import EncodePlan, MaterializedContainerMetadataPlan
from core.workflows.encode.planning.track_assembly import (
    build_track_input_paths as _build_track_input_paths_plan,
    resolve_track_assembly as _resolve_track_assembly_plan,
)
from core.workflows.encode.domain.codecs import (
    is_option_token as _is_option_token,
    option_items as _option_items,
)
from core.workflows.encode.runtime.nvencc import (
    nvencc_option_name as _nvencc_option_name,
    build_decode_pipe_cmd as _build_decode_pipe_cmd_runtime,
    build_nvencc_command as _build_nvencc_command_runtime,
    is_nvencc_codec as _is_nvencc_codec_runtime,
    is_expected_nvencc_pipe_producer_exit,
    nvencc_ffmpeg_filter_vf as _nvencc_ffmpeg_filter_vf_runtime,
    nvencc_intermediate_path as _nvencc_intermediate_path_runtime,
    nvencc_pipe_encode_video as _nvencc_pipe_encode_video_runtime,
    nvencc_requires_ffmpeg_filter_pipe as _nvencc_requires_ffmpeg_filter_pipe_runtime,
)
from core.workflows.encode.runtime.nvencc_routing import NvenccInputRouting
from core.workflows.encode.domain.codecs import vulkan_filter_device_args as _vulkan_filter_device_args
from core.workflows.encode.runtime.frame_count_guard import FrameCountAuditError
from core.workflows.common.validation_override import ValidationOverride, accept_validation_override
from core.matroska.editors.dovi import sanitize_dovi_mkv
from core.workflows.encode.dovi_policy import dovi_output_compat_id_for, rpu_extract_mode
from core.workflows.common.timeline_sync import sync_cleanup_paths as _common_sync_cleanup_paths
from core.workflows.remux_timeline_sync import LiveSyncSession
from core.fel.engine import FelError


def nvencc_needs_ffmpeg_pipe(routing: NvenccInputRouting, video: VideoEncodeSettings | None = None) -> bool:
    """Images décodées et filtrées par FFmpeg (NUT ou y4m) plutôt que par NVEncC.

    Préfiltres sans équivalent NVEncC, RIFE, playlist Blu-ray, source P5 sans
    conversion native, tone-mapping sans libplacebo dans NVEncC.
    """
    video = routing.video if video is None else video
    return bool(
        video.fel_context is not None
        or
        _nvencc_requires_ffmpeg_filter_pipe_runtime(video)
        or is_bluray_playlist(routing.input_path)
        or (video.p5_to_hdr10 and not routing.p5_native)
        or (video.tonemap_to_sdr and not routing.native_tonemap)
    )


_MISSING_RPU_MARKER = "failed to get dovi rpu"
_ENCODED_FRAMES_RE = re.compile(r"encoded (\d+) frames", re.IGNORECASE)


class NvenccDoviRpuMonitor:
    """Lignes NVEncC « Failed to get dovi rpu » (trames encodées sans RPU, code retour 0).

    Alimenté par les lecteurs de sortie (threads), lu par le thread principal
    après la fin des processus : aucune exception n'est levée dans les lecteurs.
    """

    def __init__(self) -> None:
        self.missing = 0
        self.encoded_frames: int | None = None

    def feed(self, line: str) -> None:
        if _MISSING_RPU_MARKER in line.lower():
            self.missing += 1
        match = _ENCODED_FRAMES_RE.search(line)
        if match is not None:
            self.encoded_frames = int(match.group(1))

    def raise_if_failed(self, *, expected_frames: int | None = None) -> None:
        if self.missing:
            raise EncodeError(
                f"NVEncC : {self.missing} trame(s) encodée(s) sans RPU Dolby Vision "
                "(« Failed to get dovi rpu ») ; sortie refusée."
            )
        if expected_frames is not None and self.encoded_frames is not None and self.encoded_frames != expected_frames:
            raise EncodeError(
                f"NVEncC : {self.encoded_frames} trames encodées pour {expected_frames} RPU Dolby Vision ; "
                "sortie refusée (alignement impossible)."
            )


class NvenccPipeExecutor:
    """Exécute ``décodage [| étages intermédiaires] | NVEncC``.

    ``decode_cmd`` peut être un ``PipelineCommand`` (ex. ``ffmpeg | muxiveo-rife``) :
    tous ses étages sont chaînés avant NVEncC.
    """

    def run(
        self,
        *,
        decode_cmd: list[str],
        encode_cmd: list[str],
        cwd: Path,
        signals: TaskSignals,
        monitor: NvenccDoviRpuMonitor | None = None,
    ) -> str:
        producer_stages = command_stages(decode_cmd)
        from core.pipeline_command import PipelineCommand
        native = decode_cmd.producer if isinstance(decode_cmd, PipelineCommand) else None
        fel_producer = None
        # Étage intermédiaire (muxiveo-rife) : c'est lui qui rapporte la
        # position dans la source ; les compteurs NVEncC (trames ×N) sont écartés.
        has_intermediate = len(producer_stages) > 1

        def _emit(label: str, line: str, sink: list[str]) -> None:
            if label == "nvencc" and monitor is not None:
                monitor.feed(line)
            if label == "nvencc" and parse_nvencc_progress(line) is not None and not line.lower().startswith("encoded"):
                if not has_intermediate:
                    # sans préfixe : reconnue par le parseur de progression NVEncC
                    signals.progress.emit(line)
                return
            sink.append(line)
            signals.progress.emit(f"[{label}] {line}")

        def _reader(stream, label: str, sink: list[str]) -> None:
            if stream is None:
                return
            # NVEncC rafraîchit sa progression avec \r : découpage sur \r et \n.
            buf = b""
            while chunk := stream.read1(4096) if hasattr(stream, "read1") else stream.read(4096):
                buf += chunk.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
                *complete, buf = buf.split(b"\n")
                for raw in complete:
                    line = decode_subprocess_output(raw).rstrip()
                    if line:
                        _emit(label, line, sink)
            line = decode_subprocess_output(buf).rstrip()
            if line:
                _emit(label, line, sink)

        producer_labels = ["ffmpeg-decode", *(Path(stage[0]).stem for stage in producer_stages[1:])]
        producer_lines: list[list[str]] = [[] for _ in producer_stages]
        encode_lines: list[str] = []
        producers: list[subprocess.Popen] = []
        encode_proc: subprocess.Popen | None = None

        try:
            prev_stdout = None
            for stage in producer_stages:
                popen_kwargs = subprocess_windows_no_window_kwargs(include_stdin=prev_stdout is None)
                if prev_stdout is not None:
                    popen_kwargs["stdin"] = prev_stdout
                elif native is not None:
                    popen_kwargs["stdin"] = subprocess.PIPE
                # Étages construits par le planificateur : argv séparés, aucun shell.
                # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
                proc = subprocess.Popen(  # nosec B603
                    stage,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=str(cwd),
                    **popen_kwargs,
                )
                if prev_stdout is not None:
                    prev_stdout.close()
                producers.append(proc)
                signals._register_proc(proc)
                prev_stdout = proc.stdout

            encode_proc = subprocess.Popen(
                encode_cmd,
                stdin=prev_stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=str(cwd),
                **subprocess_windows_no_window_kwargs(include_stdin=False),
            )
        except Exception:
            for proc in producers:
                signals._unregister_proc(proc)
                try:
                    proc.kill()
                except OSError:
                    pass
            raise
        signals._register_proc(encode_proc)
        if prev_stdout is not None:
            prev_stdout.close()

        readers = ThreadPoolExecutor(max_workers=len(producers) + 1)
        for proc, label, sink in zip(producers, producer_labels, producer_lines):
            readers.submit(_reader, proc.stderr, label, sink)
        readers.submit(_reader, encode_proc.stdout, "nvencc", encode_lines)
        try:
            if native is not None:
                assert producers[0].stdin is not None
                fel_producer = native.start(producers[0].stdin, signals._cancel_event)
            encode_rc = encode_proc.wait()
            producer_rcs = [proc.wait() for proc in producers]
            readers.shutdown(wait=True)
            if signals._cancel_event.is_set():
                raise TaskCancelledError()
            if fel_producer is not None:
                from core.fel.engine import FelCancelled
                fel_producer.finish()
                if isinstance(fel_producer.error, FelCancelled):
                    raise TaskCancelledError()
                fel_producer.check_error()
            # Cause première (ex. VRAM insuffisante pour muxiveo-rife) : le
            # décodage tué par le pipe fermé et NVEncC privé de flux en découlent.
            results = [
                *((stage, rc, "\n".join(lines)) for stage, rc, lines in zip(producer_stages, producer_rcs, producer_lines)),
                (list(encode_cmd), encode_rc, "\n".join(encode_lines)),
            ]
            root = pipeline_root_failure(results)
            if root == len(producers):
                tail = "\n".join(encode_lines[-40:])
                raise EncodeError(f"NVEncC a échoué.\n{tail}")
            if root is not None:
                stage, rc, err = results[root]
                # Pipe refermé par NVEncC après un encodage réussi : arrêt attendu.
                expected = encode_rc == 0 and (
                    is_expected_nvencc_pipe_producer_exit(rc, err) if root == 0 else is_broken_pipe_exit(stage, rc, err)
                )
                if not expected:
                    tail = "\n".join(producer_lines[root][-40:])
                    if root == 0:
                        raise EncodeError(f"FFmpeg decode a échoué.\n{tail}")
                    raise EncodeError(f"{producer_labels[root]} a échoué (code {rc}).\n{tail}")
            return "\n".join(encode_lines[-400:])
        finally:
            if fel_producer is not None:
                fel_producer.cancel()
            for proc in [*producers, encode_proc]:
                if proc.poll() is None:
                    proc.kill()
                proc.wait()
            if fel_producer is not None:
                fel_producer.finish()
            readers.shutdown(wait=True)
            signals._unregister_proc(encode_proc)
            for proc in producers:
                signals._unregister_proc(proc)


@dataclass(frozen=True)
class NvenccRuntimeRemuxBuilderCallbacks:
    ffmpeg_bin: str
    ffmpeg_progress_args: Callable[[], list[str]]
    ffmpeg_thread_args: Callable[[int | None], list[str]]
    offset_input_args: Callable[[int], list[str]]
    build_encode_plan: Callable[[EncodeConfig], EncodePlan]
    prepare_multisource_sync: Callable[..., tuple[dict[tuple[Path, int, str], tuple[int, int]], list[Path | str], LiveSyncSession | None, bool]]
    append_sync_inputs: Callable[[list[str], list[Path | str]], None]
    materialize_container_metadata_inputs: Callable[..., MaterializedContainerMetadataPlan]
    append_offset_aux_inputs: Callable[..., tuple[int, dict[tuple[Path, int, str], tuple[int, int]]]]
    append_stream_maps_and_attachments: Callable[..., None]
    append_strict_interleave_mux_flags: Callable[[list[str]], None]
    append_container_metadata_args: Callable[..., None]


class NvenccRuntimeRemuxBuilder:
    def __init__(self, callbacks: NvenccRuntimeRemuxBuilderCallbacks) -> None:
        self._cb = callbacks

    def build(
        self,
        config: EncodeConfig,
        encoded_video: Path,
        *,
        video_offset_ms: int = 0,
        chapter_materialize_dir: Path | None = None,
        signals: TaskSignals | None = None,
        plan: EncodePlan | None = None,
    ) -> tuple[list[str], LiveSyncSession | None, list[Path]]:
        cb = self._cb
        plan = plan or cb.build_encode_plan(config)
        all_sources = list(plan.all_sources)
        source_idx_shifted = {source: index + 1 for source, index in dict(plan.source_idx).items()}
        work_dir = config.work_dir or config.source.parent

        sync_remap, sync_inputs, live_session, strict_interleave = cb.prepare_multisource_sync(
            config=config,
            all_sources=all_sources,
            sync_base_input_idx=1 + len(all_sources),
            work_dir=work_dir,
            signals=signals,
            allow_live=True,
            plan=plan,
        )

        cmd: list[str] = [cb.ffmpeg_bin, "-hide_banner", "-y"]
        cmd.extend(cb.ffmpeg_progress_args())
        cmd.extend(cb.offset_input_args(video_offset_ms))
        cmd.extend(["-i", str(encoded_video)])
        for src in all_sources:
            append_ffmpeg_input_args(cmd, src)
        cb.append_sync_inputs(cmd, sync_inputs)

        metadata_inputs = cb.materialize_container_metadata_inputs(
            config,
            source_idx=source_idx_shifted,
            next_input_index=1 + len(all_sources) + len(sync_inputs),
            plan=plan,
            chapter_materialize_dir=chapter_materialize_dir,
            chapter_probe_source=config.source,
        )
        cmd.extend(metadata_inputs.input_args)
        cmd.extend(cb.ffmpeg_thread_args(None))

        track_input_paths = _build_track_input_paths_plan(
            leading_inputs=(encoded_video,),
            all_sources=all_sources,
            sync_inputs=sync_inputs,
        )
        track_assembly = _resolve_track_assembly_plan(
            config,
            plan,
            source_idx=source_idx_shifted,
            track_input_paths=track_input_paths,
            sync_remap=sync_remap,
            include_video=False,
        )
        _next_input_index, offset_remap = cb.append_offset_aux_inputs(
            cmd,
            _build_offset_specs_plan(
                config,
                track_mappings=list(track_assembly.track_mappings),
                offset_lookup=dict(plan.offset_lookup),
            ),
            start_input_index=metadata_inputs.next_input_index,
        )
        _ = _next_input_index

        cmd.extend(["-map", "0:v:0"])
        cmd.extend(["-c:v", "copy"])
        cb.append_stream_maps_and_attachments(
            cmd,
            config,
            source_idx=source_idx_shifted,
            subtitle_copy_input_indices=[index + 1 for index in range(len(all_sources))],
            sync_remap=sync_remap,
            offset_remap=offset_remap,
            subtitle_tracks_override=list(plan.resolved_subtitle_tracks),
            force_copy_subtitles_wildcard=(config.copy_subtitles and not plan.subtitles_resolved),
        )
        if strict_interleave:
            cb.append_strict_interleave_mux_flags(cmd)

        default_source_input_index = 1 + int(plan.video_input_idx)
        cb.append_container_metadata_args(
            cmd,
            config,
            default_metadata_input_index=default_source_input_index,
            default_chapter_input_index=default_source_input_index,
            chapter_input_index=metadata_inputs.chapter_input_index,
            tag_input_index=metadata_inputs.tag_input_index,
            include_copy_video_stream_passthrough=True,
            plan=plan,
        )
        cmd.append(str(config.output))
        return cmd, live_session, _common_sync_cleanup_paths(sync_inputs)


@dataclass(frozen=True)
class NvenccDirectOutputRunnerCallbacks:
    ffmpeg_bin: str
    nvencc_bin: str | None
    check_cancelled: Callable[[TaskSignals | None], None]
    log_step: Callable[[int, str], None]
    log_info: Callable[[str], None]
    primary_video_settings: Callable[[EncodeConfig], VideoEncodeSettings]
    video_source_path: Callable[[EncodeConfig], Path]
    video_stream_index: Callable[[EncodeConfig], int]
    build_encode_plan: Callable[[EncodeConfig], EncodePlan]
    resolve_input_routing: Callable[[EncodeConfig], NvenccInputRouting]
    build_runtime_remux_cmd: Callable[..., tuple[list[str], LiveSyncSession | None, list[Path]]]
    run_cmd: Callable[[list[str], Path | None, str, Callable[[str], None], TaskSignals], str]
    finalize_ffmpeg: Callable[..., str]
    #: Assemblage final Matroska natif (lot 2). None → remux final FFmpeg.
    native_assemble: Callable[..., None] | None = None
    bins: dict[str, str] = field(default_factory=dict)
    #: Interpolation RIFE : chaîne ``muxiveo-rife`` après le décodage y4m.
    wrap_decode_with_interpolation: Callable[[list[str], VideoEncodeSettings, Path], list[str]] | None = None
    #: Garde stricte d'un RPU externe : (source, flux, RPU) → trames retenues.
    check_external_rpu: Callable[[Path, int, Path], int] | None = None
    #: Dérogation explicite (interface) à un refus de la garde stricte.
    validation_override: ValidationOverride | None = None
    #: Trames d'un fichier vidéo produit (repli si NVEncC n'affiche pas son compte).
    count_video_frames: Callable[[Path], int | None] | None = None


class NvenccDirectOutputRunner:
    def __init__(self, callbacks: NvenccDirectOutputRunnerCallbacks) -> None:
        self._cb = callbacks

    def run(
        self,
        config: EncodeConfig,
        cleanup_paths: list[Path],
        *,
        prep_signals: TaskSignals | None = None,
        plan: EncodePlan | None = None,
    ) -> TaskSignals:
        cb = self._cb
        signals = prep_signals or TaskSignals()
        cwd = config.work_dir or config.source.parent
        plan = plan or cb.build_encode_plan(config)

        def _task() -> None:
            nonlocal config
            attempt_start = len(cleanup_paths)
            chapter_dir: Path | None = None
            live_sync_session: LiveSyncSession | None = None
            sync_cleanup_paths: list[Path] = []
            try:
                cb.check_cancelled(signals)
                if config.chapter_overrides:
                    chapter_dir = Path(
                        tempfile.mkdtemp(
                            prefix="enc_chapters_",
                            dir=str(config.work_dir) if config.work_dir else None,
                        )
                    )
                    cleanup_paths.append(chapter_dir)

                video = cb.primary_video_settings(config)
                cb.log_step(5, "Préparation de l'encode NVEncC natif")
                intermediate = _nvencc_intermediate_path_runtime(cwd, video.codec)
                cleanup_paths.append(intermediate)
                routing = cb.resolve_input_routing(config)
                runtime_video = routing.video
                if video.copy_dv and not runtime_video.copy_dv:
                    cb.log_info("Redimensionnement NVEncC : copie Dolby Vision désactivée ; aucun alignement DV imposé.")

                if routing.rebased_to_source:
                    cb.log_info(
                        "NVEncC HDR dynamique : rebascule sur la source d'origine "
                        "pour préserver les timestamps et la copie native DoVi/HDR10+."
                    )
                if routing.forced_reader == "avsw":
                    cb.log_info(
                        "NVEncC HDR dynamique : --avsw forcé pour une entrée sans "
                        "timestamps compatibles avec le chemin copy natif."
                    )

                video_offset_ms = _track_offset_ms_plan(
                    dict(plan.offset_lookup),
                    track_type="video",
                    source_path=cb.video_source_path(config),
                    stream_index=cb.video_stream_index(config),
                )
                # L'assemblage ramène l'intermédiaire à zéro, avec ou sans pipe
                # RIFE : rétablir le départ du flux dans la source d'origine.
                video_offset_ms += round(1000 * _stream_start_offset(
                    (cb.bins.get("ffprobe") or _ffprobe_beside(cb.ffmpeg_bin)),
                    cb.video_source_path(config),
                    cb.video_stream_index(config),
                ))
                # Source P5 : conversion native NVEncC (routage) sinon pipe FFmpeg libplacebo.
                needs_ffmpeg_pipe = nvencc_needs_ffmpeg_pipe(routing, runtime_video)
                if runtime_video.p5_to_hdr10:
                    cb.log_info(
                        "Dolby Vision P5 : couleurs converties en HDR10 par "
                        + ("NVEncC (libplacebo)." if routing.p5_native else "FFmpeg (libplacebo), pipe y4m.")
                    )
                from core.workflows.encode.runtime.dovi_geometry import (
                    align_dovi_rpu_geometry, convert_p7_rpu_to_p81, dovi_geometry_edit_json, extract_dovi_rpu,
                )

                dovi_bin = (cb.bins.get("dovi_tool") if cb.bins else None) or "dovi_tool"
                hdr10plus_bin = (cb.bins.get("hdr10plus_tool") if cb.bins else None) or "hdr10plus_tool"

                def run_metadata(cmd: list[str]) -> object:
                    cb.check_cancelled(signals)
                    return cb.run_cmd(
                        cmd,
                        cwd,
                        "dovi-metadata",
                        lambda line: signals.progress.emit(line),
                        signals,
                    )

                def source_rpu(target: Path) -> Path:
                    """RPU de la source : celui d'une P5 extrait à la préparation, sinon extraction."""
                    prepared = runtime_video.p5_rpu_path
                    if runtime_video.p5_to_hdr10 and prepared is not None and Path(prepared).is_file():
                        return Path(prepared)
                    cleanup_paths.append(target)
                    signals.progress.emit("Extraction RPU Dolby Vision…")
                    mode = rpu_extract_mode(runtime_video)
                    rpu = extract_dovi_rpu(
                        source=routing.input_path, stream_index=routing.stream_index,
                        ffmpeg_bin=cb.ffmpeg_bin, dovi_tool_bin=dovi_bin,
                        output_rpu=target, work_dir=cwd, run_cmd=run_metadata,
                        cleanup_paths=cleanup_paths,
                        mode=mode,
                    )
                    # Source au record P8 portant un RPU P7 (fichier mal étiqueté).
                    converted = target.with_name(f"{target.stem}.p81.bin")
                    cleanup_paths.append(converted)
                    if not mode and convert_p7_rpu_to_p81(
                        dovi_tool_bin=dovi_bin, rpu_bin=rpu, output_rpu=converted, run_cmd=run_metadata,
                    ):
                        cb.log_info("Dolby Vision : RPU P7 sur une source sans couche d'amélioration, converti en P8.1.")
                        return converted
                    return rpu

                dovi_rpu_path: Path | None = None
                if routing.needs_rpu_alignment:
                    aligned_rpu = cwd / "rpu_aligned.bin"
                    cleanup_paths.extend([aligned_rpu, dovi_geometry_edit_json(aligned_rpu)])
                    cb.log_info(
                        f"Dolby Vision : recadrage {routing.crop_offsets} (gauche, haut, droite, bas). "
                        "Réalignement des offsets L5 par scène, sans réinitialisation globale du RPU."
                    )
                    raw_rpu = source_rpu(cwd / "source_rpu.bin")
                    signals.progress.emit("Réalignement RPU Dolby Vision…")
                    align_dovi_rpu_geometry(
                        dovi_tool_bin=dovi_bin,
                        rpu_input=raw_rpu,
                        output_rpu=aligned_rpu,
                        crop_offsets=routing.crop_offsets or (0, 0, 0, 0),
                        run_cmd=run_metadata,
                    )
                    dovi_rpu_path = aligned_rpu

                # Interpolation RIFE : NVEncC lit un pipe y4m, la copie DoVi /
                # HDR10+ depuis la source est impossible ; les métadonnées sont
                # extraites, étendues à la cadence interpolée et fournies en fichiers.
                frame_ratio = (
                    _resolve_frame_ratio(
                        runtime_video,
                        ffprobe_bin=cb.bins.get("ffprobe") or _ffprobe_beside(cb.ffmpeg_bin),
                        source=routing.input_path,
                        stream_index=routing.stream_index,
                    )
                    if needs_ffmpeg_pipe
                    else Fraction(1)
                )
                # RPU indispensable en fichier : converti à l'extraction (P7, P5, FEL ;
                # NVEncC ne convertit pas le profil du RPU) ou pipe y4m.
                if runtime_video.copy_dv and dovi_rpu_path is None and (
                    rpu_extract_mode(runtime_video) is not None or frame_ratio > 1
                ):
                    dovi_rpu_path = source_rpu(cwd / "source_rpu.bin")
                expected_frames: int | None = None
                if runtime_video.copy_dv and dovi_rpu_path is not None and cb.check_external_rpu is not None:
                    try:
                        source_frames = cb.check_external_rpu(routing.input_path, routing.stream_index, dovi_rpu_path)
                    except FrameCountAuditError as exc:
                        cb.check_cancelled(signals)
                        if not accept_validation_override(
                            cb.validation_override, dovi_rpu_path, f"Audit trames : {exc}",
                            signals._cancel_event.is_set, cb.log_info,
                        ):
                            cb.check_cancelled(signals)
                            raise EncodeError(f"Audit trames : {exc}") from exc
                        source_frames = 0
                    if source_frames:
                        expected_frames = math.ceil(source_frames * frame_ratio)
                hdr10plus_json_path: Path | None = None
                if (frame_ratio > 1 or runtime_video.fel_context) and (runtime_video.copy_dv or runtime_video.copy_hdr10plus):
                    def run_interp_metadata(cmd: list[str]) -> object:
                        cb.check_cancelled(signals)
                        return cb.run_cmd(cmd, cwd, "interpolation-metadata",
                                          lambda line: signals.progress.emit(line), signals)

                    if runtime_video.copy_hdr10plus:
                        hdr10plus_json_path = cwd / "source_hdr10plus.json"
                        cleanup_paths.append(hdr10plus_json_path)
                        signals.progress.emit("Extraction métadonnées HDR10+…")
                        _extract_hdr10plus_metadata(
                            source=routing.input_path, stream_index=routing.stream_index,
                            ffmpeg_bin=cb.ffmpeg_bin, hdr10plus_bin=hdr10plus_bin,
                            output_json=hdr10plus_json_path, work_dir=cwd,
                            run_cmd=run_interp_metadata, cleanup_paths=cleanup_paths,
                        )
                    _expand_dynamic_hdr_metadata(
                        ratio=frame_ratio,
                        rpu_bin=dovi_rpu_path if runtime_video.copy_dv else None,
                        hdr10p_json=hdr10plus_json_path,
                        dovi_tool_bin=dovi_bin,
                        run_cmd=run_interp_metadata,
                        log=cb.log_info,
                    )
                output_fps = _multiply_fps_expr(routing.source_fps or routing.input_fps, frame_ratio)

                encode_cmd = _build_nvencc_command_runtime(
                    cb.nvencc_bin or "",
                    (
                        _nvencc_pipe_encode_video_runtime(runtime_video)
                        if needs_ffmpeg_pipe
                        else runtime_video
                    ),
                    intermediate,
                    input_path=None if needs_ffmpeg_pipe else routing.input_path,
                    stream_index=None if needs_ffmpeg_pipe else routing.stream_index,
                    input_reader=None if needs_ffmpeg_pipe else routing.input_reader,
                    input_fps=None if needs_ffmpeg_pipe else routing.input_fps,
                    source_fps=output_fps or routing.source_fps or routing.input_fps,
                    input_avsync=None if needs_ffmpeg_pipe else routing.input_avsync,
                    hdr10plus_json=hdr10plus_json_path,
                    # Réinjection uniquement si la copie DV reste active après routage.
                    dovi_rpu=dovi_rpu_path if runtime_video.copy_dv else None,
                    dovi_rpu_prm=None if needs_ffmpeg_pipe else routing.dovi_rpu_prm,
                    source_dimensions=routing.source_dimensions,
                )
                if "--colorprim" not in encode_cmd and "-o" in encode_cmd:
                    # Rétablir le marquage source : perdu en y4m et non repris
                    # automatiquement par NVEncC sans pipe. Le HDR PQ est déjà explicite.
                    probed = _probe_interpolation_source(
                        (cb.bins.get("ffprobe") or _ffprobe_beside(cb.ffmpeg_bin)), routing.input_path, routing.stream_index,
                        tonemap_to_sdr=bool(runtime_video.tonemap_to_sdr),
                        p5_to_hdr10=bool(runtime_video.p5_to_hdr10),
                    )
                    color_args = probed.nvencc_color_args() if probed is not None else []
                    # Une valeur saisie en paramètres avancés (ex. --colorrange) l'emporte.
                    present = {
                        _nvencc_option_name(token) for token in encode_cmd if _is_option_token(token)
                    }
                    color_args = [
                        token
                        for item in _option_items(color_args)
                        if _nvencc_option_name(item[0]) not in present
                        for token in item
                    ]
                    out_at = encode_cmd.index("-o")
                    encode_cmd = [*encode_cmd[:out_at], *color_args, *encode_cmd[out_at:]]
                decode_cmd: list[str] | None = None
                if needs_ffmpeg_pipe:
                    decode_cmd = _build_decode_pipe_cmd_runtime(
                        cb.ffmpeg_bin,
                        routing.input_path,
                        stream_index=routing.stream_index,
                        extra_input_args=_vulkan_filter_device_args(runtime_video),
                        vf=_nvencc_ffmpeg_filter_vf_runtime(runtime_video),
                        frame_exact=bool(
                            (runtime_video.copy_dv and dovi_rpu_path is not None) or hdr10plus_json_path is not None
                        ),
                        nut=runtime_video.fel_context is not None and not runtime_video.interpolates(),
                    )
                    if cb.wrap_decode_with_interpolation is not None:
                        decode_cmd = cb.wrap_decode_with_interpolation(
                            decode_cmd, runtime_video, routing.input_path,
                        )
                    from core.fel.pipeline import with_fel_input
                    decode_cmd = with_fel_input(decode_cmd, runtime_video)
                remux_cmd: list[str] | None = None
                if cb.native_assemble is None:
                    remux_cmd, live_sync_session, sync_cleanup_paths = cb.build_runtime_remux_cmd(
                        config,
                        intermediate,
                        video_offset_ms=video_offset_ms,
                        chapter_materialize_dir=chapter_dir,
                        signals=signals,
                        plan=plan,
                    )
                    cleanup_paths.extend(sync_cleanup_paths)
                    if live_sync_session is not None:
                        for proc in live_sync_session.processes:
                            signals._register_proc(proc)

                cb.check_cancelled(signals)
                cb.log_step(6, "Encodage NVEncC")
                monitor = NvenccDoviRpuMonitor()
                if decode_cmd is not None:
                    NvenccPipeExecutor().run(
                        decode_cmd=decode_cmd,
                        encode_cmd=encode_cmd,
                        cwd=cwd,
                        signals=signals,
                        monitor=monitor,
                    )
                else:
                    def _progress(line: str) -> None:
                        monitor.feed(line)
                        signals.progress.emit(line)

                    cb.run_cmd(encode_cmd, cwd, "nvencc", _progress, signals)
                # Après encodage, avant assemblage : toutes les trames ont reçu leur RPU.
                if expected_frames is not None and monitor.encoded_frames is None and cb.count_video_frames is not None:
                    monitor.encoded_frames = cb.count_video_frames(intermediate)
                if expected_frames is not None and monitor.encoded_frames is None:
                    message = "Audit trames : compte des trames encodées illisible, couverture RPU invérifiable."
                    if not accept_validation_override(
                        cb.validation_override, intermediate, message, signals._cancel_event.is_set, cb.log_info,
                    ):
                        cb.check_cancelled(signals)
                        raise EncodeError(message)
                monitor.raise_if_failed(expected_frames=expected_frames)
                if runtime_video.fel_context is not None:
                    cb.log_info("Piste vidéo 1 : reconstruction FEL réussie.")
                effective_fps = output_fps or routing.source_fps or routing.input_fps
                # Record DOVI reconstruit (perdu au muxage) : compatibilité de la matrice V30.
                dovi_compat = dovi_output_compat_id_for(runtime_video)
                if runtime_video.copy_dv and intermediate.is_file():
                    cb.log_info(
                        "Dolby Vision : validation MaxBlockAdditionID=1 et niveau (Level 6/9 au lieu de 10) sur l'artefact NVEncC."
                    )
                    sanitize_dovi_mkv(intermediate, fps=effective_fps, target_compat_id=dovi_compat)

                if cb.native_assemble is not None:
                    cb.log_step(7, "Assemblage final Matroska natif")
                    cb.native_assemble(
                        config,
                        intermediate=intermediate,
                        video_offset_ms=video_offset_ms,
                        signals=signals,
                        plan=plan,
                        work_dir=cwd,
                    )
                    if runtime_video.copy_dv and config.output.is_file():
                        sanitize_dovi_mkv(config.output, fps=effective_fps, target_compat_id=dovi_compat)
                    signals.finished.emit(str(config.output))
                else:
                    cb.log_step(7, "Remux final ffmpeg")
                    if remux_cmd is None:
                        raise RuntimeError("remux_cmd is unexpectedly None for final ffmpeg remux")
                    output = cb.finalize_ffmpeg(
                        config,
                        remux_cmd,
                        cwd,
                        "ffmpeg-remux",
                        signals,
                        plan=plan,
                    )
                    if runtime_video.copy_dv and config.output.is_file():
                        sanitize_dovi_mkv(config.output, fps=effective_fps, target_compat_id=dovi_compat)
                    signals.finished.emit(output)
            except FelError as exc:
                from core.fel.preparation import fallback_video
                from core.workdir import remove_path
                original = cb.primary_video_settings(config)
                if signals._cancel_event.is_set():
                    signals.cancelled.emit()
                elif original.fel_context is None:
                    signals.failed.emit(str(exc), exc)
                else:
                    if live_sync_session is not None:
                        for proc in live_sync_session.processes:
                            signals._unregister_proc(proc)
                        live_sync_session.close()
                        live_sync_session = None
                    for path in cleanup_paths[attempt_start:]:
                        remove_path(path)
                    del cleanup_paths[attempt_start:]
                    fallback = fallback_video(original)
                    config = replace(config, video=fallback, video_tracks=[fallback])
                    signals.progress.emit(f"[WARN] Reconstruction FEL : {exc} ; reprise complète NVEncC sur le BL.")
                    _task()
            except TaskCancelledError:
                signals.cancelled.emit()
            except Exception as exc:
                signals.failed.emit(str(exc), exc)
            finally:
                if live_sync_session is not None:
                    for proc in live_sync_session.processes:
                        signals._unregister_proc(proc)
                    live_sync_session.close()

        if prep_signals is not None:
            _task()
            return signals

        executor = ThreadPoolExecutor(max_workers=1)
        signals.watch_future(executor.submit(_task))
        executor.shutdown(wait=False)
        return signals


def build_nvencc_pipeline_commands(
    config: EncodeConfig,
    *,
    nvencc_bin: str | None,
    ffmpeg_bin: str,
    video_tracks: Callable[[EncodeConfig], list[VideoEncodeSettings]],
    resolve_input_routing: Callable[[EncodeConfig], NvenccInputRouting],
    wrap_decode_with_interpolation: Callable[[list[str], VideoEncodeSettings, Path], list[str]] | None = None,
) -> list[list[str]] | None:
    if len(video_tracks(config)) != 1:
        return None
    videos = [v for v in (config.video_tracks or []) if v.codec != "copy"]
    if config.video and config.video.codec != "copy" and not videos:
        videos = [config.video]
    if len(videos) != 1:
        return None
    video = videos[0]
    if not _is_nvencc_codec_runtime(video.codec):
        return None
    if not nvencc_bin:
        return None

    work_dir = (config.work_dir or Path(tempfile.gettempdir())).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    intermediate = _nvencc_intermediate_path_runtime(work_dir, video.codec)
    routing = resolve_input_routing(config)
    needs_ffmpeg_pipe = nvencc_needs_ffmpeg_pipe(routing)
    dovi_rpu_preview: Path | None = None
    if routing.video.copy_dv:
        if routing.needs_rpu_alignment:
            dovi_rpu_preview = work_dir / "rpu_aligned.bin"
        elif routing.video.p5_to_hdr10 or routing.video.interpolates():
            dovi_rpu_preview = work_dir / "source_rpu.bin"
    # Cadence encodée comme à l'exécution (interpolation : GOP Dolby Vision borné sur la cadence de sortie).
    preview_fps = routing.source_fps or routing.input_fps
    if needs_ffmpeg_pipe and routing.video.interpolates() and preview_fps:
        preview_fps = _multiply_fps_expr(preview_fps, routing.video.frame_ratio(str(preview_fps))) or preview_fps
    encode = _build_nvencc_command_runtime(
        nvencc_bin,
        (
            _nvencc_pipe_encode_video_runtime(routing.video)
            if needs_ffmpeg_pipe
            else routing.video
        ),
        intermediate,
        input_path=None if needs_ffmpeg_pipe else routing.input_path,
        stream_index=None if needs_ffmpeg_pipe else routing.stream_index,
        input_reader=None if needs_ffmpeg_pipe else routing.input_reader,
        input_fps=None if needs_ffmpeg_pipe else routing.input_fps,
        source_fps=preview_fps,
        input_avsync=None if needs_ffmpeg_pipe else routing.input_avsync,
        dovi_rpu=dovi_rpu_preview,
        dovi_rpu_prm=None if needs_ffmpeg_pipe else routing.dovi_rpu_prm,
        source_dimensions=routing.source_dimensions,
    )
    if needs_ffmpeg_pipe:
        decode = _build_decode_pipe_cmd_runtime(
            ffmpeg_bin,
            routing.input_path,
            stream_index=routing.stream_index,
            extra_input_args=_vulkan_filter_device_args(routing.video),
            vf=_nvencc_ffmpeg_filter_vf_runtime(routing.video),
            frame_exact=dovi_rpu_preview is not None,
            nut=routing.video.fel_context is not None and not routing.video.interpolates(),
        )
        if wrap_decode_with_interpolation is not None:
            # aperçu : décodage | muxiveo-rife sur une seule ligne de jetons
            decode = command_preview_tokens(
                wrap_decode_with_interpolation(decode, routing.video, routing.input_path)
            )
    else:
        decode = None
    remux = [
        ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(intermediate),
    ]
    append_ffmpeg_input_args(remux, config.source)
    remux.extend([
        "-map", "0:v:0",
        "-map", "1:a?",
        "-map", "1:s?",
        "-map_chapters", "1",
        "-c", "copy",
        str(config.output),
    ])
    return [decode, encode, remux] if decode is not None else [encode, remux]
