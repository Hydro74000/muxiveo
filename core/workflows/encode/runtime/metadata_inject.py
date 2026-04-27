from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from core.subprocess_utils import subprocess_text_kwargs

from core.runner import TaskCancelledError, TaskSignals
from core.workdir import remove_path
from core.workflows.encode.models import EncodeConfig, QualityMode
from core.workflows.encode.planning.track_assembly import build_track_input_paths, resolve_track_assembly
from core.workflows.encode.planning.plan_models import EncodePlan
from core.workflows.encode.runtime.frame_count_guard import (
    FrameCountAuditError,
    FrameCountGuard,
)
from core.workflows.encode.runtime.dovi_p7_router import DoviP7Router
from core.workflows.matroska_dovi_block_addition import (
    DolbyVisionConfigRecord,
    MatroskaDoviBlockAdditionEditor,
)
from core.workflows.matroska_native_muxer import MatroskaNativeMuxer
from core.workflows.remux_timeline_sync import LiveSyncSession

import dataclasses


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
    source_is_vfr: Callable[[Path], bool]
    source_video_dimensions: Callable[[Path], tuple[int, int]]
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
                video = config.video
                if video is None:
                    raise ValueError("EncodeConfig.video is required for metadata injection")

                # STEP 4-bis : routage P7/P5 → P8.1 si besoin.
                # Pour les sources DoVi P7 (FEL/MEL) ou P5, on convertit
                # le bitstream en P8.1 mono-layer avant l'extraction RPU
                # et l'encode. Sans ça, le BL+EL pose problème côté NVDEC
                # et le RPU extrait n'est pas cohérent avec le BL encodé.
                # Pour P8.x → no-op, le config reste tel quel.
                effective_config = config
                p7_router_decision = None
                if video.copy_dv:
                    p7_router = DoviP7Router()
                    p7_router_decision = p7_router.analyze(
                        source=config.source,
                        # mi_video pourrait être passé ici si déjà parsé en
                        # amont ; sans, le routeur tombe sur dovi_tool info.
                        mi_video=None,
                        fallback_to_dovi_tool=True,
                    )
                    cb.log_info(
                        f"Routage DV : {p7_router_decision.reason}"
                    )
                    if p7_router_decision.conversion_needed:
                        cb.log_step(
                            4,
                            f"Conversion {p7_router_decision.sub_profile.label} "
                            f"→ P8.1 (dovi_tool convert)",
                        )
                        signals.progress.emit(
                            f"Conversion {p7_router_decision.sub_profile.label} → P8.1…"
                        )
                        converted = p7_router.execute_conversion(
                            source=config.source,
                            output_dir=tmp,
                            run_cmd=lambda c: _run(c),
                            dovi_tool_bin=cb.bins["dovi_tool"],
                            decision=p7_router_decision,
                        )
                        ext_files.append(converted)
                        # On clone EncodeConfig en redirigeant `source` vers
                        # le HEVC P8.1. Les STEPs 5 (extract RPU/HDR10+) et
                        # 6 (encode) liront cette source convertie via
                        # `cb.video_source_path(effective_config)` /
                        # `cb.build_video_only_cmd(effective_config, ...)`.
                        # `config` original reste intact pour STEP 9 (audio,
                        # subs, chapitres).
                        effective_config = dataclasses.replace(
                            config, source=converted,
                        )
                        _check()

                cb.log_step(5, "Extraction des métadonnées dynamiques (DoVi/HDR10+)")
                # dovi_tool / hdr10plus_tool n'acceptent nativement que MKV ou
                # HEVC annexB brut. Pour MP4/MOV/TS/..., il faut extraire d'abord
                # en HEVC annexB via ffmpeg + bsf hevc_mp4toannexb, sinon le
                # parser HEVC panique sur le VPS.
                _RAW_HEVC_EXT = {".hevc", ".h265", ".265", ".x265"}
                meta_src = cb.video_source_path(effective_config)
                meta_src_ext = meta_src.suffix.lower()
                needs_annexb = meta_src_ext not in _RAW_HEVC_EXT and meta_src_ext != ".mkv"
                if needs_annexb and (video.copy_dv or video.copy_hdr10plus):
                    annexb_src = _alloc("source.hevc", src_size_est)
                    signals.progress.emit("Extraction HEVC annexB pour outillage DoVi/HDR10+…")
                    _run([
                        cb.ffmpeg_bin, "-nostdin", "-y",
                        "-i", str(meta_src),
                        "-map", "0:v:0", "-c", "copy",
                        "-bsf:v", "hevc_mp4toannexb",
                        "-f", "hevc", str(annexb_src),
                    ])
                    _check()
                    meta_input = annexb_src
                else:
                    meta_input = meta_src

                rpu_bin = tmp / "rpu.bin"
                if video.copy_dv:
                    signals.progress.emit("Extraction RPU Dolby Vision…")
                    _run([
                        cb.bins["dovi_tool"], "extract-rpu",
                        "-i", str(meta_input), "-o", str(rpu_bin),
                    ])
                    _check()

                hdr10p_json = tmp / "hdr10p.json"
                if video.copy_hdr10plus:
                    signals.progress.emit("Extraction métadonnées HDR10+…")
                    _run([
                        cb.bins["hdr10plus_tool"], "extract",
                        str(meta_input), "-o", str(hdr10p_json),
                    ])
                    _check()

                if needs_annexb and (video.copy_dv or video.copy_hdr10plus):
                    _free(meta_input)

                cb.log_step(6, "Encodage vidéo seule (HEVC brut)")
                enc_hevc = _alloc("enc.hevc", src_size_est)
                signals.progress.emit("Encodage vidéo…")
                if video.quality_mode == QualityMode.SIZE:
                    v_cmds = cb.build_video_only_two_pass(effective_config, enc_hevc)
                    cb.log_info("Passe 1/2 (analyse)…")
                    _run(v_cmds[0])
                    _check()
                    cb.log_info("Passe 2/2 (encodage)…")
                    _run(v_cmds[1])
                else:
                    _run(cb.build_video_only_cmd(effective_config, enc_hevc))
                _check()
                current_hevc = enc_hevc

                # Audit frame count : empêche l'injection sur un stream qui
                # aurait dérapé en frame count (NVENC drop, NVDEC dup, filtre
                # non frame-preserving). Sans ça, l'injection RPU/HDR10+ se
                # désaligne silencieusement et on produit un fichier où les
                # scènes-cuts DV ne tombent plus aux bons endroits.
                if video.copy_dv or video.copy_hdr10plus:
                    cb.log_info("Audit alignement frame count (source/encoded/RPU/HDR10+)…")
                    guard = FrameCountGuard(
                        mediainfo_bin=cb.bins.get("mediainfo", "mediainfo"),
                        ffprobe_bin=cb.bins.get("ffprobe", "ffprobe"),
                        dovi_tool_bin=cb.bins["dovi_tool"],
                    )
                    audit = guard.audit(
                        source=cb.video_source_path(effective_config),
                        encoded=current_hevc,
                        rpu_bin=rpu_bin if (video.copy_dv and rpu_bin.exists()) else None,
                        hdr10p_json=hdr10p_json if (video.copy_hdr10plus and hdr10p_json.exists()) else None,
                    )
                    try:
                        guard.enforce(
                            audit,
                            rpu_bin=rpu_bin if (video.copy_dv and rpu_bin.exists()) else None,
                            hdr10p_json=hdr10p_json if (video.copy_hdr10plus and hdr10p_json.exists()) else None,
                            on_warn=lambda msg: signals.progress.emit(f"[WARN] {msg}"),
                            on_info=lambda msg: signals.progress.emit(msg),
                        )
                    except FrameCountAuditError as exc:
                        raise RuntimeError(f"Audit frame count : {exc}") from exc
                    _check()

                cb.log_step(7, "Injection HDR10+ puis DoVi (si demandé)")
                if video.copy_hdr10plus and hdr10p_json.exists():
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

                if video.copy_dv and rpu_bin.exists():
                    cur_size = current_hevc.stat().st_size
                    out_dv = _alloc("enc_dv.hevc", cur_size)
                    signals.progress.emit("Injection RPU Dolby Vision…")
                    _run([
                        cb.bins["dovi_tool"],
                        "-m", video.dovi_profile,
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
                source_is_vfr = bool(cb.source_is_vfr(cb.video_source_path(config)))
                if source_is_vfr:
                    # Source VFR : ffmpeg en wrap CFR détruirait les
                    # timestamps. On utilise le muxer Matroska natif qui
                    # réutilise les PTS source frame-à-frame.
                    cb.log_info(
                        "Source VFR détectée → muxer Matroska natif Python."
                    )
                    pixel_width, pixel_height = cb.source_video_dimensions(
                        cb.video_source_path(config),
                    )
                    record_for_muxer = None
                    if video.copy_dv and rpu_bin.exists():
                        record_for_muxer = _build_dovi_record_from_rpu(
                            rpu_bin=rpu_bin,
                            dovi_tool_bin=cb.bins["dovi_tool"],
                        )
                    try:
                        mux_result = MatroskaNativeMuxer(
                            ffprobe_bin=cb.bins.get("ffprobe", "ffprobe"),
                        ).mux(
                            hevc_input=current_hevc,
                            source_for_timestamps=cb.video_source_path(config),
                            output=wrapped_video,
                            pixel_width=pixel_width,
                            pixel_height=pixel_height,
                            dovi_record=record_for_muxer,
                        )
                        signals.progress.emit(
                            f"Muxage natif : {mux_result.frames_written} frames "
                            f"sur {mux_result.cluster_count} clusters, "
                            f"durée {mux_result.duration_ms} ms."
                        )
                    except Exception as exc:
                        # Fallback ffmpeg si le muxer natif échoue (ne devrait
                        # pas arriver mais on garde le pipeline résilient).
                        signals.progress.emit(
                            f"[WARN] Muxer natif échoué ({exc}), fallback ffmpeg."
                        )
                        _run(
                            cb.wrap_injected_hevc_for_reconstruction(
                                source=config.source,
                                hevc_input=current_hevc,
                                mkv_output=wrapped_video,
                            )
                        )
                else:
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

                # Post-mux : injection du BlockAdditionMapping DOVI au niveau
                # conteneur. ffmpeg ne l'écrit pas quand il copie un HEVC brut
                # (-f hevc → mkv via -c copy). Sans cette signalisation, les
                # players (mpv gpu-next, Plex, certains TV) ne déclenchent
                # pas le mode DV même si les NALs RPU sont dans le bytestream.
                if video.copy_dv and rpu_bin.exists():
                    cb.log_info("Injection signal Dolby Vision au niveau Matroska…")
                    record = _build_dovi_record_from_rpu(
                        rpu_bin=rpu_bin,
                        dovi_tool_bin=cb.bins["dovi_tool"],
                    )
                    if record is not None:
                        try:
                            patch_result = MatroskaDoviBlockAdditionEditor().patch(
                                config.output, record=record,
                            )
                            if patch_result.applied:
                                signals.progress.emit(
                                    f"BlockAdditionMapping DOVI ajouté "
                                    f"(track #{patch_result.patched_track_number}, "
                                    f"Δ {patch_result.bytes_delta:+d} octets)."
                                )
                            elif patch_result.skipped:
                                signals.progress.emit(
                                    f"Signal DOVI Matroska non modifié : {patch_result.reason}"
                                )
                        except Exception as patch_exc:
                            # On loggue mais on n'échoue pas le workflow : le
                            # fichier reste lisible, juste sans signal DV.
                            signals.progress.emit(
                                f"[WARN] Patch BlockAdditionMapping DOVI échoué : {patch_exc}"
                            )
                    else:
                        signals.progress.emit(
                            "[WARN] Impossible d'extraire le profil DOVI du RPU "
                            "pour le signal Matroska."
                        )

                signals.finished.emit(f"Encodage terminé → {config.output.name}")

            except TaskCancelledError:
                signals.cancelled.emit()
            except Exception as exc:
                signals.failed.emit(str(exc), exc)
            finally:
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
            executor.shutdown(wait=False)
        else:
            _task()
        return signals


# ----------------------------------------------------------------------------
# Helpers de bas niveau
# ----------------------------------------------------------------------------


def _build_dovi_record_from_rpu(
    *,
    rpu_bin: Path,
    dovi_tool_bin: str,
) -> DolbyVisionConfigRecord | None:
    """
    Interroge ``dovi_tool info -i RPU --summary`` et construit le record DOVI
    correspondant pour le ``BlockAdditionMapping`` Matroska.

    Renvoie None si dovi_tool est indisponible ou si le summary ne contient
    pas les champs attendus.
    """
    try:
        result = subprocess.run(
            [dovi_tool_bin, "info", "-i", str(rpu_bin), "--summary"],
            capture_output=True,
            check=False,
            **subprocess_text_kwargs(),
        )
    except (FileNotFoundError, OSError):
        return None
    text = (result.stdout or "") + (result.stderr or "")

    profile_match = re.search(r"Profile\s*:\s*(\d+)(?:\.(\d+))?", text)
    if not profile_match:
        return None
    profile = int(profile_match.group(1))
    sub_profile = int(profile_match.group(2) or 0)

    # bl_signal_compat_id : 1 pour P8.1 (HDR10), 0 pour P8.0, 2 pour P8.2
    # (P8.4/HLG). On l'extrait préférentiellement de la ligne dédiée si
    # présente (dovi_tool ≥ 2.x), sinon on déduit du sub_profile.
    compat_match = re.search(r"compatibility\s*id\s*:\s*(\d+)", text, re.IGNORECASE)
    if compat_match:
        compat_id = int(compat_match.group(1))
    else:
        compat_id = sub_profile if profile == 8 else 0

    level_match = re.search(r"DV\s+Level\s*:\s*(\d+)", text, re.IGNORECASE)
    level = int(level_match.group(1)) if level_match else 6

    # En sortie de pipeline metadata_inject, on a forcément un stream
    # mono-layer (le BL est ce que NVENC a encodé) avec RPU réinjecté.
    return DolbyVisionConfigRecord(
        profile=profile,
        level=level,
        rpu_present=True,
        el_present=False,
        bl_present=True,
        bl_signal_compat_id=max(0, min(15, compat_id)),
    )
