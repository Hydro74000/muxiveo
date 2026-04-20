"""
core/workflows/remux_timeline_sync.py — utilitaire de synchronisation timeline
pour le remux FFmpeg multi-source.

But:
    - isoler les flux audio/sous-titres "étrangers" (hors source vidéo primaire),
    - les normaliser en entrées mono-flux dédiées,
    - faciliter un mux final plus stable côté seeking (Plex/clients DASH/HLS).
"""

from __future__ import annotations

import os
import mmap
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol, Sequence

from core.runner import TaskCancelledError
from core.subprocess_utils import subprocess_text_kwargs
from core.workflows.remux_models import RemuxError, SourceInput


def _cli_path(path: Path | str) -> str:
    if isinstance(path, str):
        return path
    text = str(path)
    if text.startswith("\\\\.\\pipe\\"):
        return text
    return path.as_posix()


class _TrackLike(Protocol):
    @property
    def track_type(self) -> str:
        ...


class MappedTrackLike(Protocol):
    @property
    def source_file_index(self) -> int:
        ...

    @property
    def stream_index(self) -> int:
        ...

    @property
    def track(self) -> _TrackLike:
        ...


@dataclass(frozen=True)
class SyncPreparedInput:
    key: tuple[int, int, str]  # (source_file_index, stream_index, track_type)
    path: Path | str
    input_idx: int
    container_format: str = "matroska"  # "nut" pour les FIFOs POSIX live, "matroska" pour fichiers


@dataclass
class LiveSyncSession:
    inputs: list[SyncPreparedInput]
    processes: list[subprocess.Popen]
    fifo_paths: list[Path] = field(default_factory=list)
    named_pipe_paths: list[str] = field(default_factory=list)
    _cleanup_callbacks: list[Callable[[], None]] = field(default_factory=list)
    _threads: list[threading.Thread] = field(default_factory=list)

    def close(self) -> None:
        for proc in self.processes:
            if proc.poll() is not None:
                continue
            try:
                proc.terminate()
                proc.wait(timeout=1.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        for callback in self._cleanup_callbacks:
            try:
                callback()
            except Exception:
                pass
        for thread in self._threads:
            try:
                thread.join(timeout=1.0)
            except Exception:
                pass
        for fifo in self.fifo_paths:
            try:
                fifo.unlink(missing_ok=True)
            except Exception:
                pass


class LiveSyncNotSupportedError(RemuxError):
    """Le mode live FIFO n'est pas disponible sur la plateforme courante."""


@dataclass(frozen=True)
class TimelineSyncPrepareResult:
    prepared_inputs: list[SyncPreparedInput]
    live_session: LiveSyncSession | None = None


class TimelineSyncFallbackHelper:
    """
    Orchestration partagée du sync timeline :
      1) live FIFO / named pipe
      2) fallback memory-mapped (RAM prioritaire)
      3) fallback fichier temporaire (dernier recours)
    """

    def __init__(
        self,
        *,
        syncer: "FfmpegTimelineSync",
        work_dir: Path,
        ram_dir: Path | None = None,
        log_cb: Callable[[str], None] | None = None,
    ) -> None:
        self._syncer = syncer
        self._work_dir = work_dir
        self._ram_dir = ram_dir
        self._log = log_cb or (lambda _: None)

    @staticmethod
    def default_ram_dir() -> Path | None:
        if sys.platform.startswith("win"):
            return None
        shm = Path("/dev/shm")
        if shm.is_dir() and os.access(shm, os.W_OK | os.X_OK):
            return shm
        return None

    @staticmethod
    def _dedupe_dirs(paths: Sequence[Path]) -> list[Path]:
        out: list[Path] = []
        for path in paths:
            if path not in out:
                out.append(path)
        return out

    def prepare(
        self,
        *,
        mapped_tracks: Sequence[MappedTrackLike],
        sources: Sequence[SourceInput],
        base_input_idx: int,
        allow_live: bool = True,
        cancel_cb: Callable[[], bool] | None = None,
    ) -> TimelineSyncPrepareResult:
        """
        Stratégie hybride :
        - Audio étranger  → live FIFO NUT (streamable, pas de seek arrière).
        - Subtitle étranger → toujours fallback fichier (NUT ne supporte pas SRT/ASS).
        Les deux groupes sont combinés en respectant l'ordre d'apparition dans mapped_tracks.
        """
        # Identifier les pistes étrangères (hors source vidéo primaire)
        source_by_index = {src.file_index: src for src in sources}
        foreign_keys: set[tuple[int, int, str]] = set(
            FfmpegTimelineSync._collect_foreign_targets(
                mapped_tracks=mapped_tracks,
                source_by_index=source_by_index,
                cancel_cb=cancel_cb,
            )
        )

        # Quand on ne peut pas identifier de pistes étrangères (pas de vidéo primaire,
        # ou sources vides) → déléguer tout au syncer sans filtrer par type.
        if not foreign_keys:
            return self._prepare_unified(
                mapped_tracks=mapped_tracks,
                sources=sources,
                base_input_idx=base_input_idx,
                allow_live=allow_live,
                cancel_cb=cancel_cb,
            )

        audio_tracks = [
            mt for mt in mapped_tracks
            if mt.track.track_type == "audio"
            and (mt.source_file_index, mt.stream_index, mt.track.track_type) in foreign_keys
        ]
        subtitle_tracks = [
            mt for mt in mapped_tracks
            if mt.track.track_type == "subtitle"
            and (mt.source_file_index, mt.stream_index, mt.track.track_type) in foreign_keys
        ]

        live_session: LiveSyncSession | None = None
        live_prepared: list[SyncPreparedInput] = []

        # Audio étranger : live FIFO NUT si autorisé
        if allow_live:
            try:
                live_session = self._syncer.start_live_demux_session(
                    mapped_tracks=audio_tracks,
                    sources=sources,
                    tmp_dir=self._work_dir,
                    base_input_idx=base_input_idx,
                    cancel_cb=cancel_cb,
                )
                live_prepared = live_session.inputs
            except TaskCancelledError:
                raise
            except Exception as exc:
                self._log(f"Sync live audio indisponible ({exc}); fallback RAM puis disque.")

        # Subtitle étranger : toujours fallback fichier (NUT ne supporte pas SRT/ASS)
        # Audio étranger aussi en fallback si le live a échoué
        fallback_tracks: list[MappedTrackLike] = (
            ([] if live_prepared else list(audio_tracks)) + list(subtitle_tracks)
        )
        file_prepared: list[SyncPreparedInput] = []
        if fallback_tracks:
            file_prepared = self._prepare_fallback(
                mapped_tracks=fallback_tracks,
                sources=sources,
                base_input_idx=base_input_idx + len(live_prepared),
                cancel_cb=cancel_cb,
            )

        prepared = live_prepared + file_prepared
        return TimelineSyncPrepareResult(prepared_inputs=prepared, live_session=live_session)

    def _prepare_unified(
        self,
        *,
        mapped_tracks: Sequence[MappedTrackLike],
        sources: Sequence[SourceInput],
        base_input_idx: int,
        allow_live: bool,
        cancel_cb: Callable[[], bool] | None,
    ) -> TimelineSyncPrepareResult:
        """Délégation sans filtrage par type — utilisé quand sources est vide."""
        live_session: LiveSyncSession | None = None
        prepared: list[SyncPreparedInput] = []
        if allow_live:
            try:
                live_session = self._syncer.start_live_demux_session(
                    mapped_tracks=mapped_tracks,
                    sources=sources,
                    tmp_dir=self._work_dir,
                    base_input_idx=base_input_idx,
                    cancel_cb=cancel_cb,
                )
                prepared = live_session.inputs
            except TaskCancelledError:
                raise
            except Exception as exc:
                self._log(f"Sync live indisponible ({exc}); fallback RAM puis disque.")
        if not prepared:
            prepared = self._prepare_fallback(
                mapped_tracks=mapped_tracks,
                sources=sources,
                base_input_idx=base_input_idx,
                cancel_cb=cancel_cb,
            )
        return TimelineSyncPrepareResult(prepared_inputs=prepared, live_session=live_session)

    def _prepare_fallback(
        self,
        *,
        mapped_tracks: Sequence[MappedTrackLike],
        sources: Sequence[SourceInput],
        base_input_idx: int,
        cancel_cb: Callable[[], bool] | None = None,
    ) -> list[SyncPreparedInput]:
        fallback_dirs = self._dedupe_dirs(
            ([self._ram_dir] if self._ram_dir is not None else []) + [self._work_dir]
        )
        last_exc: Exception | None = None

        for idx, candidate_dir in enumerate(fallback_dirs):
            target_label = "RAM" if self._ram_dir is not None and candidate_dir == self._ram_dir else "disque"
            try:
                prepared = self._syncer.prepare_from_mapped_tracks_mmap(
                    mapped_tracks=mapped_tracks,
                    sources=sources,
                    tmp_dir=candidate_dir,
                    base_input_idx=base_input_idx,
                    cancel_cb=cancel_cb,
                )
                self._log(f"Sync fallback memory-mapped utilisé ({target_label}).")
                return prepared
            except TaskCancelledError:
                raise
            except Exception as mmap_exc:
                try:
                    prepared = self._syncer.prepare_from_mapped_tracks(
                        mapped_tracks=mapped_tracks,
                        sources=sources,
                        tmp_dir=candidate_dir,
                        base_input_idx=base_input_idx,
                        cancel_cb=cancel_cb,
                    )
                    self._log(f"Sync fallback fichier utilisé ({target_label}).")
                    return prepared
                except TaskCancelledError:
                    raise
                except Exception as file_exc:
                    last_exc = file_exc
                    if idx < len(fallback_dirs) - 1:
                        self._log(
                            "Sync fallback "
                            f"{target_label} indisponible (mmap={mmap_exc}; file={file_exc}), "
                            "tentative suivante."
                        )
                        continue
                    raise

        if last_exc is not None:
            raise last_exc
        return []


class FfmpegTimelineSync:
    """
    Prépare des entrées normalisées pour les flux multi-source à risque.
    """

    def __init__(
        self,
        *,
        ffmpeg_bin: str,
        ffmpeg_thread_args: Sequence[str] | None = None,
        log_cb: Callable[[str], None] | None = None,
    ) -> None:
        self._ffmpeg = ffmpeg_bin
        self._thread_args = list(ffmpeg_thread_args or [])
        self._log = log_cb or (lambda _: None)

    def prepare_from_mapped_tracks(
        self,
        *,
        mapped_tracks: Sequence[MappedTrackLike],
        sources: Sequence[SourceInput],
        tmp_dir: Path,
        base_input_idx: int,
        cancel_cb: Callable[[], bool] | None = None,
    ) -> list[SyncPreparedInput]:
        source_by_index = {src.file_index: src for src in sources}
        ordered_keys = self._collect_foreign_targets(
            mapped_tracks=mapped_tracks,
            source_by_index=source_by_index,
            cancel_cb=cancel_cb,
        )
        dedup: dict[tuple[int, int, str], Path] = {}

        for key in ordered_keys:
            src_file_index, stream_index, track_type = key
            src = source_by_index[src_file_index]
            ext = ".mka" if track_type == "audio" else ".mks"
            out_name = f"sync_f{src_file_index}_s{stream_index}_{track_type}{ext}"
            out_path = self._unique_path(tmp_dir, out_name)
            self._extract_stream(source=src.path, stream_index=stream_index, destination=out_path)
            dedup[key] = out_path

        prepared: list[SyncPreparedInput] = []
        for i, key in enumerate(ordered_keys):
            prepared.append(
                SyncPreparedInput(
                    key=key,
                    path=dedup[key],
                    input_idx=base_input_idx + i,
                )
            )

        if prepared:
            self._log(
                "Timeline sync (multi-source):"
                f"{len(prepared)} flux normalisé(s) avant remux final."
            )

        return prepared

    def prepare_from_mapped_tracks_mmap(
        self,
        *,
        mapped_tracks: Sequence[MappedTrackLike],
        sources: Sequence[SourceInput],
        tmp_dir: Path,
        base_input_idx: int,
        cancel_cb: Callable[[], bool] | None = None,
    ) -> list[SyncPreparedInput]:
        """
        Fallback Windows: normalisation via bridge memory-mapped file.
        On reste en mode intermédiaire disque, mais l'écriture se fait via mmap.
        """
        source_by_index = {src.file_index: src for src in sources}
        ordered_keys = self._collect_foreign_targets(
            mapped_tracks=mapped_tracks,
            source_by_index=source_by_index,
            cancel_cb=cancel_cb,
        )
        outputs: dict[tuple[int, int, str], Path] = {}

        for key in ordered_keys:
            if cancel_cb is not None and cancel_cb():
                raise TaskCancelledError()
            src_file_index, stream_index, track_type = key
            src = source_by_index[src_file_index]
            ext = ".mka" if track_type == "audio" else ".mks"
            out_name = f"sync_mmap_f{src_file_index}_s{stream_index}_{track_type}{ext}"
            out_path = self._unique_path(tmp_dir, out_name)
            self._extract_stream_via_mmap(
                source=src.path,
                stream_index=stream_index,
                destination=out_path,
            )
            outputs[key] = out_path

        prepared: list[SyncPreparedInput] = []
        for i, key in enumerate(ordered_keys):
            prepared.append(
                SyncPreparedInput(
                    key=key,
                    path=outputs[key],
                    input_idx=base_input_idx + i,
                )
            )

        if prepared:
            self._log(
                "Timeline sync fallback (memory-mapped): "
                f"{len(prepared)} flux normalisé(s) avant remux final."
            )

        return prepared

    def start_live_demux_session(
        self,
        *,
        mapped_tracks: Sequence[MappedTrackLike],
        sources: Sequence[SourceInput],
        tmp_dir: Path,
        base_input_idx: int,
        cancel_cb: Callable[[], bool] | None = None,
    ) -> LiveSyncSession:
        if os.name == "nt":
            return self._start_windows_named_pipe_session(
                mapped_tracks=mapped_tracks,
                sources=sources,
                tmp_dir=tmp_dir,
                base_input_idx=base_input_idx,
                cancel_cb=cancel_cb,
            )
        if not hasattr(os, "mkfifo"):
            raise LiveSyncNotSupportedError(
                "Le mode sync live nécessite mkfifo (non disponible sur cette plateforme)."
            )
        return self._start_posix_fifo_session(
            mapped_tracks=mapped_tracks,
            sources=sources,
            tmp_dir=tmp_dir,
            base_input_idx=base_input_idx,
            cancel_cb=cancel_cb,
        )

    def _start_posix_fifo_session(
        self,
        *,
        mapped_tracks: Sequence[MappedTrackLike],
        sources: Sequence[SourceInput],
        tmp_dir: Path,
        base_input_idx: int,
        cancel_cb: Callable[[], bool] | None = None,
    ) -> LiveSyncSession:

        source_by_index = {src.file_index: src for src in sources}
        ordered_keys = self._collect_foreign_targets(
            mapped_tracks=mapped_tracks,
            source_by_index=source_by_index,
            cancel_cb=cancel_cb,
        )
        if not ordered_keys:
            return LiveSyncSession(inputs=[], processes=[], fifo_paths=[])

        inputs: list[SyncPreparedInput] = []
        processes: list[subprocess.Popen] = []
        fifo_paths: list[Path] = []
        keepalive_fds: list[int] = []
        try:
            for i, key in enumerate(ordered_keys):
                if cancel_cb is not None and cancel_cb():
                    raise TaskCancelledError()

                src_file_index, stream_index, track_type = key
                src = source_by_index.get(src_file_index)
                if src is None:
                    raise RemuxError(
                        "Source introuvable pour sync live timeline : "
                        f"file_index={src_file_index}"
                    )

                suffix = ".mka" if track_type == "audio" else ".mks"
                fifo_name = f"sync_live_f{src_file_index}_s{stream_index}_{track_type}{suffix}"
                fifo_path = self._unique_path(tmp_dir, fifo_name)
                try:
                    os.mkfifo(fifo_path, 0o600)
                except OSError as exc:
                    raise LiveSyncNotSupportedError(
                        f"Création FIFO impossible sur cette plateforme/FS: {exc}"
                    ) from exc
                fifo_paths.append(fifo_path)
                try:
                    # Reader keepalive non-bloquant: évite qu'un writer live reste
                    # bloqué sur open() en attente du reader ffmpeg final.
                    keepalive_fd = os.open(fifo_path, os.O_RDONLY | os.O_NONBLOCK)
                except OSError as exc:
                    raise LiveSyncNotSupportedError(
                        f"Ouverture keepalive FIFO impossible: {exc}"
                    ) from exc
                keepalive_fds.append(keepalive_fd)

                cmd = [
                    self._ffmpeg,
                    "-hide_banner",
                    "-y",
                    "-loglevel", "error",
                    "-nostdin",
                    "-i", _cli_path(src.path),
                    "-map", f"0:{stream_index}",
                    *self._thread_args,
                    "-c", "copy",
                    *self._timeline_rebase_args(track_type),
                    "-f", "nut",  # NUT est streamable (pas de seek arrière), requis pour FIFO
                    _cli_path(fifo_path),
                ]
                self._log("$ " + " ".join(str(c) for c in cmd))
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                processes.append(proc)
                inputs.append(SyncPreparedInput(
                    key=key,
                    path=fifo_path,
                    input_idx=base_input_idx + i,
                    container_format="nut",
                ))
        except Exception:
            LiveSyncSession(
                inputs=inputs,
                processes=processes,
                fifo_paths=fifo_paths,
                _cleanup_callbacks=[
                    lambda fd=fd: os.close(fd)
                    for fd in keepalive_fds
                ],
            ).close()
            raise

        self._log(
            "Timeline sync live (multi-source):"
            f"{len(inputs)} FIFO(s) actives pour le remux final."
        )
        return LiveSyncSession(
            inputs=inputs,
            processes=processes,
            fifo_paths=fifo_paths,
            _cleanup_callbacks=[
                lambda fd=fd: os.close(fd)
                for fd in keepalive_fds
            ],
        )

    def _start_windows_named_pipe_session(
        self,
        *,
        mapped_tracks: Sequence[MappedTrackLike],
        sources: Sequence[SourceInput],
        tmp_dir: Path,
        base_input_idx: int,
        cancel_cb: Callable[[], bool] | None = None,
    ) -> LiveSyncSession:
        # Import local: évite de charger ctypes/wintypes hors Windows.
        import ctypes
        from ctypes import wintypes

        source_by_index = {src.file_index: src for src in sources}
        ordered_keys = self._collect_foreign_targets(
            mapped_tracks=mapped_tracks,
            source_by_index=source_by_index,
            cancel_cb=cancel_cb,
        )
        if not ordered_keys:
            return LiveSyncSession(inputs=[], processes=[], named_pipe_paths=[])

        windll_factory = getattr(ctypes, "WinDLL", None)
        if windll_factory is None:
            raise LiveSyncNotSupportedError(
                "ctypes.WinDLL indisponible sur cette plateforme."
            )
        kernel32 = windll_factory("kernel32", use_last_error=True)
        invalid_handle = ctypes.c_void_p(-1).value
        error_pipe_connected = 535
        pipe_access_outbound = 0x00000002
        pipe_type_byte = 0x00000000
        pipe_readmode_byte = 0x00000000
        pipe_wait = 0x00000000

        def _create_named_pipe(pipe_name: str):
            handle = kernel32.CreateNamedPipeW(
                pipe_name,
                pipe_access_outbound,
                pipe_type_byte | pipe_readmode_byte | pipe_wait,
                1,
                1 << 20,
                1 << 20,
                0,
                None,
            )
            if handle == invalid_handle:
                raise LiveSyncNotSupportedError(
                    f"Création named pipe impossible sur cette plateforme/FS: {pipe_name}"
                )
            return handle

        def _close_handle(handle) -> None:
            try:
                kernel32.CloseHandle(handle)
            except Exception:
                pass

        inputs: list[SyncPreparedInput] = []
        processes: list[subprocess.Popen] = []
        threads: list[threading.Thread] = []
        handles: list = []
        pipe_paths: list[str] = []

        try:
            for i, key in enumerate(ordered_keys):
                if cancel_cb is not None and cancel_cb():
                    raise TaskCancelledError()

                src_file_index, stream_index, track_type = key
                src = source_by_index.get(src_file_index)
                if src is None:
                    raise RemuxError(
                        "Source introuvable pour sync live timeline : "
                        f"file_index={src_file_index}"
                    )

                # Exemple final: \\.\pipe\mediarecode_sync_<uuid>
                pipe_name = rf"\\.\pipe\mediarecode_sync_{uuid.uuid4().hex}"
                handle = _create_named_pipe(pipe_name)
                handles.append(handle)
                pipe_paths.append(pipe_name)

                cmd = [
                    self._ffmpeg,
                    "-hide_banner",
                    "-loglevel", "error",
                    "-i", _cli_path(src.path),
                    "-map", f"0:{stream_index}",
                    *self._thread_args,
                    "-c", "copy",
                    *self._timeline_rebase_args(track_type),
                    "-f", "matroska",
                    "pipe:1",
                ]
                self._log("$ " + " ".join(str(c) for c in cmd))
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    bufsize=0,
                )
                processes.append(proc)

                def _pump_stdout_to_named_pipe(
                    process: subprocess.Popen,
                    pipe_handle,
                    pipe_label: str,
                ) -> None:
                    stdout = process.stdout
                    if stdout is None:
                        _close_handle(pipe_handle)
                        return
                    try:
                        connected = kernel32.ConnectNamedPipe(pipe_handle, None)
                        if not connected:
                            err = int(kernel32.GetLastError())
                            if err != error_pipe_connected:
                                return
                        while True:
                            chunk = stdout.read(64 * 1024)
                            if not chunk:
                                break
                            written = wintypes.DWORD(0)
                            ok = kernel32.WriteFile(
                                pipe_handle,
                                chunk,
                                len(chunk),
                                ctypes.byref(written),
                                None,
                            )
                            if not ok:
                                break
                    finally:
                        try:
                            kernel32.FlushFileBuffers(pipe_handle)
                        except Exception:
                            pass
                        try:
                            kernel32.DisconnectNamedPipe(pipe_handle)
                        except Exception:
                            pass
                        _close_handle(pipe_handle)
                        try:
                            stdout.close()
                        except Exception:
                            pass
                        self._log(f"Timeline sync live: fermeture pipe {pipe_label}")

                thread = threading.Thread(
                    target=_pump_stdout_to_named_pipe,
                    args=(proc, handle, pipe_name),
                    daemon=True,
                    name=f"mrecode-pipe-{i}",
                )
                thread.start()
                threads.append(thread)

                inputs.append(SyncPreparedInput(
                    key=key,
                    path=pipe_name,
                    input_idx=base_input_idx + i,
                ))

        except Exception:
            session = LiveSyncSession(
                inputs=inputs,
                processes=processes,
                named_pipe_paths=pipe_paths,
                _threads=threads,
                _cleanup_callbacks=[lambda h=h: _close_handle(h) for h in handles],
            )
            session.close()
            raise

        self._log(
            "Timeline sync live (multi-source):"
            f"{len(inputs)} named pipe(s) actives pour le remux final."
        )
        return LiveSyncSession(
            inputs=inputs,
            processes=processes,
            named_pipe_paths=pipe_paths,
            _threads=threads,
            _cleanup_callbacks=[lambda h=h: _close_handle(h) for h in handles],
        )

    @staticmethod
    def _collect_foreign_targets(
        *,
        mapped_tracks: Sequence[MappedTrackLike],
        source_by_index: dict[int, SourceInput],
        cancel_cb: Callable[[], bool] | None = None,
    ) -> list[tuple[int, int, str]]:
        primary_video = next((mt for mt in mapped_tracks if mt.track.track_type == "video"), None)
        if primary_video is None:
            return []

        seen: set[tuple[int, int, str]] = set()
        ordered: list[tuple[int, int, str]] = []

        for mt in mapped_tracks:
            if mt.track.track_type not in {"audio", "subtitle"}:
                continue
            if mt.source_file_index == primary_video.source_file_index:
                continue

            if cancel_cb is not None and cancel_cb():
                raise TaskCancelledError()

            key = (mt.source_file_index, mt.stream_index, mt.track.track_type)
            if key in seen:
                continue
            if mt.source_file_index not in source_by_index:
                raise RemuxError(
                    "Source introuvable pour normalisation timeline : "
                    f"file_index={mt.source_file_index}"
                )
            seen.add(key)
            ordered.append(key)

        return ordered

    @staticmethod
    def _unique_path(base_dir: Path, filename: str) -> Path:
        candidate = base_dir / filename
        if not candidate.exists():
            return candidate
        stem = candidate.stem
        suffix = candidate.suffix
        i = 1
        while True:
            alt = base_dir / f"{stem}_{i}{suffix}"
            if not alt.exists():
                return alt
            i += 1

    @staticmethod
    def _timeline_rebase_args(track_type: str) -> list[str]:
        """
        Audio étranger : conserve le rebase historique à 0.
        Sous-titres étrangers : conserve la timeline d'origine.
        """
        if track_type == "audio":
            return ["-start_at_zero", "-avoid_negative_ts", "make_zero"]
        return []

    @staticmethod
    def _track_type_from_sync_destination(destination: Path) -> str:
        if destination.suffix.lower() == ".mks":
            return "subtitle"
        return "audio"

    def _extract_stream(self, *, source: Path, stream_index: int, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        track_type = self._track_type_from_sync_destination(destination)
        cmd = [
            self._ffmpeg,
            "-hide_banner", "-y",
            "-i", _cli_path(source),
            "-map", f"0:{stream_index}",
            *self._thread_args,
            "-c", "copy",
            *self._timeline_rebase_args(track_type),
            _cli_path(destination),
        ]
        self._log("$ " + " ".join(str(c) for c in cmd))
        result = subprocess.run(
            cmd,
            capture_output=True,
            check=False,
            timeout=1200,
            **subprocess_text_kwargs(),
        )
        if result.returncode != 0 or not destination.exists() or destination.stat().st_size == 0:
            stderr = (result.stderr or "").strip()
            raise RemuxError(
                "Normalisation timeline échouée "
                f"(source={source.name}, stream={stream_index}): {stderr}"
            )

    def _extract_stream_via_mmap(self, *, source: Path, stream_index: int, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        track_type = self._track_type_from_sync_destination(destination)
        cmd = [
            self._ffmpeg,
            "-hide_banner", "-loglevel", "error",
            "-i", _cli_path(source),
            "-map", f"0:{stream_index}",
            *self._thread_args,
            "-c", "copy",
            *self._timeline_rebase_args(track_type),
            "-f", "matroska",
            "pipe:1",
        ]
        self._log("$ " + " ".join(str(c) for c in cmd))

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )

        initial_size = 8 * 1024 * 1024
        max_chunk = 64 * 1024
        written = 0

        try:
            with open(destination, "w+b") as fh:
                fh.truncate(initial_size)
                mm = mmap.mmap(fh.fileno(), initial_size, access=mmap.ACCESS_WRITE)
                allocated = initial_size

                stdout = proc.stdout
                if stdout is None:
                    raise RemuxError("Flux stdout indisponible pour l'extraction mmap.")

                while True:
                    chunk = stdout.read(max_chunk)
                    if not chunk:
                        break
                    needed = written + len(chunk)
                    if needed > allocated:
                        new_size = allocated
                        while new_size < needed:
                            new_size *= 2
                        mm.flush()
                        mm.close()
                        fh.truncate(new_size)
                        mm = mmap.mmap(fh.fileno(), new_size, access=mmap.ACCESS_WRITE)
                        allocated = new_size
                    mm[written:written + len(chunk)] = chunk
                    written += len(chunk)

                mm.flush()
                mm.close()
                fh.truncate(written)
        finally:
            if proc.poll() is None:
                proc.wait(timeout=1200)

        stderr = (proc.stderr.read() if proc.stderr else b"").decode("utf-8", errors="replace").strip()
        if proc.returncode != 0 or written == 0 or not destination.exists():
            raise RemuxError(
                "Normalisation timeline mmap échouée "
                f"(source={source.name}, stream={stream_index}): {stderr}"
            )
