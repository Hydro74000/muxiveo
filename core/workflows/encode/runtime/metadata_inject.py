from __future__ import annotations

import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from core.runner import TaskCancelledError, TaskSignals
from core.workdir import remove_path
from core.workflows.encode.models import EncodeConfig, QualityMode
from core.workflows.encode.planning.track_assembly import build_track_input_paths, resolve_track_assembly
from core.workflows.encode.planning.plan_models import EncodePlan
from core.workflows.remux_timeline_sync import LiveSyncSession


@dataclass(frozen=True)
class MetadataInjectRunnerCallbacks:
    ffmpeg_bin: str
    bins: dict[str, str]
    log_step: Callable[[int, str], None]
    log_info: Callable[[str], None]
    check_cancelled: Callable[[TaskSignals | None], None]
    video_source_path: Callable[[EncodeConfig], Path]
    build_video_only_two_pass: Callable[[EncodeConfig, Path], list[list[str]]]
    build_video_only_cmd: Callable[[EncodeConfig, Path], list[str]]
    wrap_injected_hevc_for_reconstruction: Callable[..., list[str]]
    build_encode_plan: Callable[[EncodeConfig], EncodePlan]
    source_input_index_map: Callable[[list[Path]], dict[Path, int]]
    prepare_multisource_sync: Callable[..., tuple[dict[tuple[Path, int, str], tuple[int, int]], list[Path | str], LiveSyncSession | None, bool]]
    sync_cleanup_paths: Callable[[list[Path | str]], list[Path]]
    append_sync_inputs: Callable[[list[str], list[Path | str]], None]
    prepare_container_metadata_inputs: Callable[..., tuple[int, int | None, int | None]]
    ffmpeg_thread_args: Callable[[], list[str]]
    ffmpeg_progress_args: Callable[[], list[str]]
    video_map_key: Callable[[EncodeConfig], tuple[Path, int, str]]
    append_offset_aux_inputs: Callable[..., tuple[int, dict[tuple[Path, int, str], tuple[int, int]]]]
    build_offset_specs: Callable[..., object]
    video_map_arg: Callable[..., str]
    append_stream_maps_and_attachments: Callable[..., None]
    append_strict_interleave_mux_flags: Callable[[list[str]], None]
    append_container_metadata_args: Callable[..., None]
    run_cmd: Callable[[list[str], TaskSignals, Path | None, Callable[[str], None] | None], str]
    bind_matroska_segment_muxing_patch: Callable[[TaskSignals, Path], None]
    bind_nfo_write: Callable[[TaskSignals, Path], None]


class MetadataInjectRunner:
    """Runner dédié au pipeline d'injection DoVi/HDR10+."""

    def __init__(self, callbacks: MetadataInjectRunnerCallbacks) -> None:
        self._callbacks = callbacks

    def run(
        self,
        config: EncodeConfig,
        *,
        prep_signals: TaskSignals | None = None,
        plan: EncodePlan | None = None,
    ) -> TaskSignals:
        cb = self._callbacks
        signals = prep_signals or TaskSignals()
        executor = None if prep_signals is not None else ThreadPoolExecutor(max_workers=1)

        def _task() -> None:
            work = config.work_dir or Path(tempfile.gettempdir())
            work.mkdir(parents=True, exist_ok=True)
            tmp_dir = tempfile.mkdtemp(
                prefix="mediarecode_encode_",
                dir=str(work),
            )
            tmp = Path(tmp_dir)
            ext_files: list[Path] = []
            live_sync_session: LiveSyncSession | None = None
            sync_cleanup_paths: list[Path] = []

            def _run(cmd: list[str]) -> str:
                return cb.run_cmd(
                    cmd,
                    signals,
                    tmp,
                    lambda line: signals.progress.emit(line),
                )

            def _check() -> None:
                if signals._cancel_event.is_set():
                    raise TaskCancelledError()

            def _alloc(name: str, ref_size: int) -> Path:
                _ = ref_size
                return tmp / name

            def _free(path: Path) -> None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
                try:
                    ext_files.remove(path)
                except ValueError:
                    pass

            try:
                src_size_est = config.source.stat().st_size

                cb.log_step(5, "Extraction des métadonnées dynamiques (DoVi/HDR10+)")
                rpu_bin = tmp / "rpu.bin"
                if config.video.copy_dv:
                    signals.progress.emit("Extraction RPU Dolby Vision…")
                    _run([
                        cb.bins["dovi_tool"], "extract-rpu",
                        "-i", str(cb.video_source_path(config)), "-o", str(rpu_bin),
                    ])
                    _check()

                hdr10p_json = tmp / "hdr10p.json"
                if config.video.copy_hdr10plus:
                    signals.progress.emit("Extraction métadonnées HDR10+…")
                    _run([
                        cb.bins["hdr10plus_tool"], "extract",
                        str(cb.video_source_path(config)), "-o", str(hdr10p_json),
                    ])
                    _check()

                cb.log_step(6, "Encodage vidéo seule (HEVC brut)")
                enc_hevc = _alloc("enc.hevc", src_size_est)
                signals.progress.emit("Encodage vidéo…")
                if config.video.quality_mode == QualityMode.SIZE:
                    v_cmds = cb.build_video_only_two_pass(config, enc_hevc)
                    cb.log_info("Passe 1/2 (analyse)…")
                    _run(v_cmds[0])
                    _check()
                    cb.log_info("Passe 2/2 (encodage)…")
                    _run(v_cmds[1])
                else:
                    _run(cb.build_video_only_cmd(config, enc_hevc))
                _check()
                current_hevc = enc_hevc

                cb.log_step(7, "Injection HDR10+ puis DoVi (si demandé)")
                if config.video.copy_hdr10plus and hdr10p_json.exists():
                    cur_size = current_hevc.stat().st_size
                    out_hdr10p = _alloc("enc_hdr10p.hevc", cur_size)
                    signals.progress.emit("Injection métadonnées HDR10+…")
                    _run([
                        cb.bins["hdr10plus_tool"], "inject",
                        "-i", str(current_hevc),
                        "-j", str(hdr10p_json),
                        "-o", str(out_hdr10p),
                    ])
                    _free(current_hevc)
                    current_hevc = out_hdr10p
                    _check()

                if config.video.copy_dv and rpu_bin.exists():
                    cur_size = current_hevc.stat().st_size
                    out_dv = _alloc("enc_dv.hevc", cur_size)
                    signals.progress.emit("Injection RPU Dolby Vision…")
                    _run([
                        cb.bins["dovi_tool"],
                        "-m", config.video.dovi_profile,
                        "inject-rpu",
                        "-i", str(current_hevc),
                        "-r", str(rpu_bin),
                        "-o", str(out_dv),
                    ])
                    _free(current_hevc)
                    current_hevc = out_dv
                    _check()

                cb.log_step(8, "Encapsulation timeline vidéo injectée")
                wrapped_video = _alloc("enc_wrapped.mkv", current_hevc.stat().st_size)
                signals.progress.emit("Encapsulation vidéo injectée…")
                _run(
                    cb.wrap_injected_hevc_for_reconstruction(
                        source=config.source,
                        hevc_input=current_hevc,
                        mkv_output=wrapped_video,
                    )
                )
                _free(current_hevc)
                current_video_input = wrapped_video
                _check()

                cb.log_step(9, "Reconstruction finale du conteneur MKV")
                signals.progress.emit("Reconstitution finale…")
                encode_plan = plan or cb.build_encode_plan(config)
                all_sources = list(encode_plan.all_sources)
                extra_sources = all_sources[1:]
                recon_source_idx = cb.source_input_index_map(all_sources)
                sync_remap: dict[tuple[Path, int, str], tuple[int, int]] = {}
                sync_inputs: list[Path | str] = []
                strict_interleave = False

                recon_cmd = [
                    cb.ffmpeg_bin,
                    "-hide_banner",
                    "-y",
                    *cb.ffmpeg_progress_args(),
                    "-i",
                    str(current_video_input),
                    "-i",
                    str(config.source),
                ]
                for sp in extra_sources:
                    recon_cmd.extend(["-i", str(sp)])

                sync_remap, sync_inputs, live_sync_session, strict_interleave = cb.prepare_multisource_sync(
                    config=config,
                    all_sources=all_sources,
                    sync_base_input_idx=2 + len(extra_sources),
                    work_dir=tmp,
                    signals=signals,
                    allow_live=True,
                    plan=encode_plan,
                )
                sync_cleanup_paths = cb.sync_cleanup_paths(sync_inputs)
                cb.append_sync_inputs(recon_cmd, sync_inputs)
                if live_sync_session is not None:
                    for proc in live_sync_session.processes:
                        signals._register_proc(proc)

                next_input_index, chapter_input_index, tag_input_index = cb.prepare_container_metadata_inputs(
                    recon_cmd,
                    config,
                    source_idx=recon_source_idx,
                    next_input_index=2 + len(extra_sources) + len(sync_inputs),
                    plan=encode_plan,
                    chapter_materialize_dir=tmp,
                    chapter_probe_source=config.source,
                )

                recon_cmd.extend(cb.ffmpeg_thread_args())
                resolved_subtitle_tracks = list(encode_plan.resolved_subtitle_tracks)
                video_key = cb.video_map_key(config)
                video_default_map = (0, 0)
                track_assembly = resolve_track_assembly(
                    config,
                    encode_plan,
                    source_idx=recon_source_idx,
                    track_input_paths=build_track_input_paths(
                        leading_inputs=[current_video_input],
                        all_sources=all_sources,
                        sync_inputs=sync_inputs,
                    ),
                    sync_remap=sync_remap,
                    video_default_map=video_default_map,
                    video_fallback_input=current_video_input,
                )

                next_input_index, offset_remap = cb.append_offset_aux_inputs(
                    recon_cmd,
                    cb.build_offset_specs(
                        config,
                        track_mappings=list(track_assembly.track_mappings),
                        offset_lookup=dict(encode_plan.offset_lookup),
                    ),
                    start_input_index=next_input_index,
                )
                _ = next_input_index

                recon_cmd.extend([
                    "-map",
                    cb.video_map_arg(
                        track_assembly.video_map,
                        offset_remap=offset_remap,
                        map_key=video_key,
                    ),
                    "-c:v",
                    "copy",
                ])
                cb.append_stream_maps_and_attachments(
                    recon_cmd,
                    config,
                    source_idx=recon_source_idx,
                    subtitle_copy_input_indices=list(range(1, 2 + len(extra_sources))),
                    sync_remap=sync_remap,
                    offset_remap=offset_remap,
                    subtitle_tracks_override=resolved_subtitle_tracks,
                    force_copy_subtitles_wildcard=(config.copy_subtitles and not encode_plan.subtitles_resolved),
                )
                if strict_interleave:
                    cb.append_strict_interleave_mux_flags(recon_cmd)

                cb.append_container_metadata_args(
                    recon_cmd,
                    config,
                    default_metadata_input_index=0,
                    default_chapter_input_index=1,
                    chapter_input_index=chapter_input_index,
                    tag_input_index=tag_input_index,
                    plan=encode_plan,
                )
                recon_cmd.append(str(config.output))
                _run(recon_cmd)

                signals.finished.emit(f"Encodage terminé → {config.output.name}")

            except TaskCancelledError:
                signals.cancelled.emit()
            except Exception as exc:
                signals.failed.emit(str(exc), exc)
            finally:
                if executor is not None:
                    executor.shutdown(wait=False)
                if live_sync_session is not None:
                    for proc in live_sync_session.processes:
                        signals._unregister_proc(proc)
                    live_sync_session.close()
                for path in sync_cleanup_paths:
                    try:
                        remove_path(path)
                    except OSError:
                        pass
                for p in list(ext_files):
                    try:
                        p.unlink(missing_ok=True)
                    except OSError:
                        pass
                shutil.rmtree(tmp_dir, ignore_errors=True)

        if prep_signals is None:
            cb.bind_matroska_segment_muxing_patch(signals, config.output)
            cb.bind_nfo_write(signals, config.output)
            assert executor is not None
            executor.submit(_task)
        else:
            _task()
        return signals
