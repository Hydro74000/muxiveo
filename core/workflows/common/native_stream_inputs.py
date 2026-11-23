"""
core/workflows/common/native_stream_inputs.py — Pistes lues par pipe pour l'assemblage natif.

Une piste copiée d'une source que le lecteur Matroska natif ne lit pas (MP4,
MOV…) est transposée en Matroska par un FFmpeg dédié, sur sa sortie standard,
et consommée au fil de l'eau par l'assembleur : aucun fichier intermédiaire.

- **Un producteur par route** : chaque processus n'écrit que sur son propre
  pipe ; l'assembleur lit celui dont il a besoin, les autres attendent (pression
  naturelle du pipe). Aucun interblocage entre producteurs.
- **Origine temporelle commune** : rebasage FFmpeg par défaut de l'entrée
  (``start_time`` du conteneur, calculé sur tous ses flux avant la sélection :
  identique pour tous les producteurs d'une source) et aucun décalage propre à
  une sortie (``-avoid_negative_ts disabled``).
- **Cycle de vie** : processus enregistrés pour l'annulation, stderr vidé par un
  fil, code de sortie vérifié en fin de flux (avant la publication) ; un
  producteur non lu jusqu'au bout est arrêté explicitement.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, cast

from core.bluray import append_ffmpeg_input_args
from core.matroska.reader import MatroskaBlock
from core.matroska.stream_source import MatroskaStreamSource, StreamedMatroskaReader
from core.runner import TaskCancelledError
from core.subprocess_utils import format_returncode, kill_process_tree, subprocess_windows_no_window_kwargs
from core.subtitle_codec import plan_subtitle_codec
from core.workflows.common.ffmpeg_runtime import matroska_output_args
from core.workflows.common.metadata import matroska_video_tag_args

#: Délai accordé à un producteur pour se terminer après sa fin de flux.
_EXIT_TIMEOUT_S = 60.0
#: Lignes de stderr gardées pour expliquer un échec.
_STDERR_TAIL_LINES = 40


@dataclass(frozen=True)
class StreamTrackSpec:
    """Piste d'une source à transposer en Matroska sur un pipe."""

    source: Path
    stream_index: int
    kind: str  # "video" | "audio" | "subtitle"
    codec: str = ""  # nom de codec FFprobe (conversion des sous-titres, tag vidéo)
    #: Porte les chapitres et tags globaux de la source (fournisseur de contexte).
    carry_context: bool = False


def producer_command(ffmpeg_bin: str, spec: StreamTrackSpec) -> list[str]:
    """Commande FFmpeg d'un producteur : une piste, copiée, en Matroska sur ``pipe:1``."""
    command = [ffmpeg_bin, "-nostdin", "-hide_banner", "-loglevel", "error"]
    append_ffmpeg_input_args(command, spec.source)
    command.extend(["-map", f"0:{int(spec.stream_index)}", "-c", "copy"])
    if spec.kind == "video":
        command.extend(matroska_video_tag_args([spec.codec.upper()]))
    elif spec.kind == "subtitle" and spec.codec:
        try:
            codec_arg, _warning = plan_subtitle_codec(spec.codec)
        except ValueError:
            codec_arg = "copy"
        if codec_arg != "copy":
            command.extend(["-c:s:0", codec_arg])
    context = "0" if spec.carry_context else "-1"
    command.extend(["-map_metadata", context, "-map_chapters", context])
    command.extend(matroska_output_args(ffmpeg_bin))
    command.extend(["-avoid_negative_ts", "disabled", "-write_crc32", "0", "-f", "matroska", "pipe:1"])
    return command


def _start_process(command: list[str]) -> subprocess.Popen:
    """Processus dans son propre groupe (POSIX) ou job (Windows) : arbre tué d'un coup."""
    kwargs: dict[str, Any] = dict(subprocess_windows_no_window_kwargs())
    job = None
    if sys.platform == "win32":
        from core.windows_process_job import WindowsProcessJob

        job = WindowsProcessJob()
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | 0x4  # CREATE_SUSPENDED
    else:
        kwargs["start_new_session"] = True
    # Commande construite par Muxiveo (argv), sans interprétation shell.
    # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)  # nosec B603
    if job is not None:
        try:
            job.attach_and_resume(int(getattr(proc, "_handle")), proc.pid)
            setattr(proc, "_muxiveo_job", job)
        except BaseException:
            proc.kill()
            proc.wait()
            job.close()
            raise
    else:
        proc._muxiveo_process_group = proc.pid  # type: ignore[attr-defined]
    return proc


class StreamProducer:
    """Un FFmpeg producteur, son pipe et son lecteur Matroska séquentiel."""

    def __init__(
        self,
        command: list[str],
        header_path: Path,
        *,
        label: str,
        duration_ns: int | None = None,
        cancelled: Callable[[], bool] = lambda: False,
        register: Callable[[subprocess.Popen], None] | None = None,
        unregister: Callable[[subprocess.Popen], None] | None = None,
    ) -> None:
        self.label = label
        self._cancelled = cancelled
        self._unregister = unregister
        self._stderr: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
        self._stopped = False
        self._done = False
        self._process = _start_process(command)
        if register is not None:
            register(self._process)
        self._stderr_thread = threading.Thread(target=self._drain_stderr, name=f"stderr-{label}", daemon=True)
        self._stderr_thread.start()
        assert self._process.stdout is not None
        source = MatroskaStreamSource(cast(BinaryIO, self._process.stdout), header_path)
        try:
            source.read_header()
        except ValueError as exc:
            raise self.failure(exc) from exc
        except BaseException:
            self.close()
            raise
        self.reader: StreamedMatroskaReader = _ProducerReader(self, source, header_path, duration_ns=duration_ns)

    def _drain_stderr(self) -> None:
        stream = self._process.stderr
        if stream is None:
            return
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                self._stderr.append(line)

    def stderr_tail(self) -> str:
        return "\n".join(self._stderr)

    def failure(self, cause: BaseException | None = None) -> BaseException:
        """Exception à lever pour un flux interrompu : annulation, échec du producteur, sinon ``cause``."""
        if self._cancelled():
            self.close()
            return TaskCancelledError()
        try:
            code = self._process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            code = None
        self.close()
        if code not in (None, 0):
            detail = self.stderr_tail()
            return RuntimeError(
                f"Lecture en flux de {self.label} : FFmpeg a échoué ({format_returncode(code)})"
                + (f" : {detail}" if detail else ".")
            )
        return cause if cause is not None else RuntimeError(f"Lecture en flux de {self.label} interrompue.")

    def finished(self) -> None:
        """Fin de flux atteinte : le producteur doit se terminer avec succès (vérifié avant publication)."""
        try:
            code = self._process.wait(timeout=_EXIT_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            self.close()
            raise RuntimeError(f"Lecture en flux de {self.label} : FFmpeg ne s'est pas terminé après son flux.")
        self._stderr_thread.join(timeout=5)
        if self._cancelled():
            self.close()
            raise TaskCancelledError()
        if code != 0:
            detail = self.stderr_tail()
            self.close()
            raise RuntimeError(
                f"Lecture en flux de {self.label} : FFmpeg a échoué ({format_returncode(code)})"
                + (f" : {detail}" if detail else ".")
            )
        self._done = True
        self.close()

    def stop(self) -> None:
        """Arrêt volontaire (en-tête seul lu, flux plus nécessaire) : code de sortie ignoré."""
        self._stopped = True
        self.close()

    def close(self) -> None:
        """Libère le processus : tué s'il n'a pas fini, pipes fermés, désenregistré (idempotent)."""
        process = self._process
        if process.poll() is None:
            kill_process_tree(process, timeout=0.5)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        job = getattr(process, "_muxiveo_job", None)
        if job is not None:
            job.close()
        if self._unregister is not None:
            self._unregister(process)
            self._unregister = None

    @property
    def stopped(self) -> bool:
        return self._stopped

    @property
    def done(self) -> bool:
        return self._done


class _ProducerReader(StreamedMatroskaReader):
    """Lecteur du flux d'un producteur : fin de flux = producteur vérifié ; interruption expliquée."""

    def __init__(
        self, producer: StreamProducer, source: MatroskaStreamSource, header_path: Path,
        *, duration_ns: int | None,
    ) -> None:
        super().__init__(source, header_path, duration_ns=duration_ns)
        self._producer = producer

    def blocks(
        self,
        *,
        track_numbers: set[int] | None = None,
        read_payload: bool = True,
        first_lace_only: bool = False,
    ) -> Iterator[MatroskaBlock]:
        stream = super().blocks(
            track_numbers=track_numbers, read_payload=read_payload, first_lace_only=first_lace_only,
        )
        try:
            yield from stream
        except (ValueError, OSError) as exc:
            raise self._producer.failure(exc) from exc
        self._producer.finished()


class StreamInputs:
    """Producteurs d'un assemblage : démarrage, lecteurs, fermeture garantie (erreur, annulation)."""

    def __init__(
        self,
        ffmpeg_bin: str,
        work_dir: Path,
        *,
        cancelled: Callable[[], bool] = lambda: False,
        register: Callable[[subprocess.Popen], None] | None = None,
        unregister: Callable[[subprocess.Popen], None] | None = None,
        log: Callable[[str, str], None] | None = None,
    ) -> None:
        self._ffmpeg = ffmpeg_bin
        self._work_dir = Path(work_dir)
        self._cancelled = cancelled
        self._register = register
        self._unregister = unregister
        self._log = log
        self._producers: list[StreamProducer] = []

    def open(
        self, spec: StreamTrackSpec, *, duration_ns: int | None = None, header_only: bool = False,
    ) -> StreamedMatroskaReader:
        """Démarre le producteur de ``spec`` et renvoie son lecteur (en-tête déjà lu).

        ``header_only`` : seules les métadonnées comptent (contexte, piste
        réécrite ailleurs) ; le producteur est arrêté aussitôt l'en-tête lu.
        """
        if self._cancelled():
            raise TaskCancelledError()
        ordinal = len(self._producers)
        label = f"{spec.source.name} #{spec.stream_index}"
        header = self._work_dir / f"stream_{ordinal}_{spec.stream_index}.header.mkv"
        producer = StreamProducer(
            producer_command(self._ffmpeg, spec), header, label=label, duration_ns=duration_ns,
            cancelled=self._cancelled, register=self._register, unregister=self._unregister,
        )
        self._producers.append(producer)
        if header_only:
            producer.stop()
            if self._log is not None:
                self._log("INFO", f"Lecture en flux de {label} : en-tête seul lu, producteur arrêté volontairement.")
        return producer.reader

    def close(self) -> None:
        """Arrête tout producteur encore actif (fin d'assemblage, erreur ou annulation)."""
        for producer in self._producers:
            producer.close()

    def __enter__(self) -> "StreamInputs":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


__all__ = [
    "StreamInputs", "StreamProducer", "StreamTrackSpec", "producer_command",
]
