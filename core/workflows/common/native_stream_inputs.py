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

import json
import subprocess
import sys
import threading
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import Any, BinaryIO, cast

from core.bluray import append_ffmpeg_input_args, ffprobe_input_args
from core.matroska.reader import MatroskaBlock, MatroskaReader
from core.matroska.stream_source import MatroskaStreamSource, StreamedMatroskaReader
from core.runner import TaskCancelledError
from core.subprocess_utils import (
    ProbeCancelledError,
    format_returncode,
    kill_process_tree,
    run_probe,
    subprocess_windows_no_window_kwargs,
)
from core.subtitle_codec import plan_subtitle_codec
from core.workflows.common.ffmpeg_runtime import matroska_output_args
from core.workflows.common.metadata import matroska_video_tag_args

#: Délai accordé à un producteur pour se terminer après sa fin de flux.
_EXIT_TIMEOUT_S = 60.0
#: Lignes de stderr gardées pour expliquer un échec.
_STDERR_TAIL_LINES = 40
#: Avance de lecture d'un producteur (octets en attente, par route) : FFmpeg lit
#: sa source pendant que l'assemblage traite et écrit.
PREFETCH_BYTES = 16 * 1024 * 1024
#: Lecture unitaire du fil d'avance.
_PREFETCH_CHUNK = 1024 * 1024
#: Capacité demandée au pipe sous Linux (``pipe-max-size`` par défaut, sans privilège).
_PIPE_BYTES = 1024 * 1024


@dataclass(frozen=True)
class StreamTrackSpec:
    """Piste d'une source à transposer en Matroska sur un pipe."""

    source: Path
    stream_index: int
    kind: str  # "video" | "audio" | "subtitle"
    codec: str = ""  # nom de codec FFprobe (conversion des sous-titres, tag vidéo)
    #: Porte les chapitres et tags globaux de la source (fournisseur de contexte).
    carry_context: bool = False
    #: Origine temporelle de la source (``start_time`` du conteneur, tous flux), en s.
    #: Renseignée par :class:`StreamInputs` : identique pour tous les producteurs d'une source.
    origin_s: float | None = None


def _as_group(spec: StreamTrackSpec | Sequence[StreamTrackSpec]) -> tuple[StreamTrackSpec, ...]:
    group = (spec,) if isinstance(spec, StreamTrackSpec) else tuple(spec)
    if not group or len({Path(item.source) for item in group}) != 1:
        raise ValueError("Producteur en flux : une ou plusieurs pistes d'une même source attendues.")
    return group


def group_stream_specs(specs: Iterable[StreamTrackSpec]) -> list[tuple[StreamTrackSpec, ...]]:
    """Producteurs d'un ensemble de pistes : les pistes denses d'une source ensemble, chaque sous-titre seul.

    Un producteur par piste relisait toute la source pour chacune (MP4 : le
    démultiplexeur saute d'échantillon en échantillon ; audio seul d'un film
    2160p : 6,5 Go lus, 16,5 s, contre 7,6 s pour vidéo + audio d'un seul
    FFmpeg). Vidéo et audio, entrelacés par FFmpeg, tiennent dans les files de
    la lecture partagée ; un sous-titre, dont le bloc suivant peut être des
    minutes plus loin, garde son producteur (quelques sauts dans la source).
    Ordre : celui de la première apparition ; doublons retirés.
    """
    groups: dict[Path, list[StreamTrackSpec]] = {}
    seen: set[tuple[Path, int]] = set()
    result: list[tuple[StreamTrackSpec, ...] | list[StreamTrackSpec]] = []
    for spec in specs:
        key = (Path(spec.source), int(spec.stream_index))
        if key in seen:
            continue
        seen.add(key)
        if spec.kind == "subtitle":
            result.append((spec,))
            continue
        group = groups.get(key[0])
        if group is None:
            group = groups[key[0]] = []
            result.append(group)
        group.append(spec)
    return [tuple(group) for group in result]


def producer_command(ffmpeg_bin: str, spec: StreamTrackSpec | Sequence[StreamTrackSpec]) -> list[str]:
    """Commande FFmpeg d'un producteur : pistes d'une source, copiées, en Matroska sur ``pipe:1``.

    Pistes de sortie dans l'ordre de ``spec`` ; contexte et origine pris de la première.
    """
    group = _as_group(spec)
    spec = group[0]
    command = [ffmpeg_bin, "-nostdin", "-hide_banner", "-loglevel", "error"]
    if spec.origin_s is not None:
        # Horodatages de la source, rebasés sur une origine commune explicite :
        # jamais un départ recalculé d'après la seule piste sélectionnée.
        command.append("-copyts")
    append_ffmpeg_input_args(command, spec.source)
    for item in group:
        command.extend(["-map", f"0:{int(item.stream_index)}"])
    command.extend(["-c", "copy"])
    command.extend(matroska_video_tag_args([item.codec.upper() for item in group if item.kind == "video"]))
    subtitles = [item for item in group if item.kind == "subtitle"]
    for position, item in enumerate(subtitles):
        if not item.codec:
            continue
        try:
            codec_arg, _warning = plan_subtitle_codec(item.codec)
        except ValueError:
            codec_arg = "copy"
        if codec_arg != "copy":
            command.extend([f"-c:s:{position}", codec_arg])
    if spec.carry_context:
        # Fournisseur de contexte : tags globaux et chapitres de la source.
        command.extend(["-map_metadata", "0", "-map_chapters", "0"])
    else:
        # Tags et langue de la piste conservés, tags globaux et chapitres laissés au fournisseur.
        command.extend(["-map_metadata:g", "-1", "-map_chapters", "-1"])
    if spec.origin_s is not None:
        command.extend(["-output_ts_offset", f"{-spec.origin_s:.6f}"])
    command.extend(matroska_output_args(ffmpeg_bin))
    command.extend(["-avoid_negative_ts", "disabled", "-write_crc32", "0", "-f", "matroska", "pipe:1"])
    return command


def probe_start_time(source: Path, ffprobe_bin: str) -> float:
    """``start_time`` du conteneur (minimum sur tous ses flux), en s ; erreur explicite si illisible."""
    try:
        result = run_probe(
            [ffprobe_bin, "-v", "error", "-show_entries", "format=start_time", "-of", "json",
             *ffprobe_input_args(source)],
            timeout=30,
        )
        payload = json.loads(result.stdout or "{}")
        value = (payload.get("format") or {}).get("start_time")
        if result.returncode == 0 and value not in (None, "N/A"):
            return float(value)
    except (OSError, subprocess.SubprocessError, ValueError, ProbeCancelledError):
        pass
    raise RuntimeError(f"Origine temporelle de {Path(source).name} illisible (ffprobe start_time).")


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
    try:
        # Commande construite par Muxiveo (argv), sans interprétation shell.
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)  # nosec B603
    except BaseException:
        if job is not None:
            job.close()
        raise
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


def _enlarge_pipe(stream: BinaryIO) -> None:
    """Pipe de 1 Mio sous Linux (moins de réveils) ; sans effet ailleurs ou si refusé."""
    if not sys.platform.startswith("linux"):
        return
    try:
        import fcntl

        fcntl.fcntl(stream.fileno(), fcntl.F_SETPIPE_SZ, _PIPE_BYTES)
    except (OSError, AttributeError, ValueError):
        pass


class _PrefetchedPipe:
    """Lecture anticipée du stdout d'un producteur, dans un fil.

    Un pipe ne garde que 64 Kio : sans avance, FFmpeg reste bloqué pendant chaque
    traitement et chaque écriture de l'assemblage, et ses lectures disque ne
    recouvrent rien (Remux réel : 11,6 s d'attente sur les pipes). Le fil lit par
    morceaux tant que moins de ``limit`` octets attendent ; ``read`` sert les
    petites lectures (en-têtes EBML) depuis le morceau courant, sans verrou.
    """

    def __init__(self, stream: BinaryIO, *, label: str, limit: int = PREFETCH_BYTES) -> None:
        self._stream = stream
        self._limit = limit
        self._chunks: deque[bytes] = deque()
        self._pending = 0
        self._current = b""
        self._offset = 0
        self._eof = False
        self._error: BaseException | None = None
        self._closed = False
        self._cond = threading.Condition()
        _enlarge_pipe(stream)
        self._thread = threading.Thread(target=self._fill, name=f"stdout-{label}", daemon=True)
        self._thread.start()

    def _fill(self) -> None:
        read = getattr(self._stream, "read1", None) or self._stream.read
        try:
            while True:
                with self._cond:
                    while self._pending >= self._limit and not self._closed:
                        self._cond.wait()
                    if self._closed:
                        return
                chunk = read(_PREFETCH_CHUNK)
                if not chunk:
                    return
                with self._cond:
                    self._chunks.append(chunk)
                    self._pending += len(chunk)
                    self._cond.notify_all()
        except (OSError, ValueError) as exc:
            with self._cond:
                self._error = exc
        finally:
            with self._cond:
                self._eof = True
                self._cond.notify_all()

    def _next_chunk(self) -> bool:
        with self._cond:
            while not self._chunks and not self._eof:
                self._cond.wait()
            if not self._chunks:
                if self._error is not None:
                    raise self._error
                return False
            self._current = self._chunks.popleft()
            self._pending -= len(self._current)
            self._offset = 0
            self._cond.notify_all()
            return True

    def read(self, size: int = -1) -> bytes:
        """Au plus ``size`` octets déjà reçus (au moins un), ``b""`` en fin de flux."""
        if self._offset >= len(self._current) and not self._next_chunk():
            return b""
        start = self._offset
        end = len(self._current) if size < 0 else min(len(self._current), start + size)
        self._offset = end
        if start == 0 and end == len(self._current):
            return self._current
        return self._current[start:end]

    def close(self) -> None:
        """Arrête le fil (après la fin du producteur, qui débloque sa lecture en cours)."""
        with self._cond:
            self._closed = True
            self._chunks.clear()
            self._pending = 0
            self._cond.notify_all()
        self._thread.join(timeout=5)


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
        header_only: bool = False,
    ) -> None:
        self.label = label
        self._cancelled = cancelled
        self._unregister = unregister
        self._stderr: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
        self._stopped = False
        self._done = False
        self._closed = False
        self._stderr_thread: threading.Thread | None = None
        self._stdout: _PrefetchedPipe | None = None
        self._process = _start_process(command)
        try:
            if register is not None:
                register(self._process)
            self._stderr_thread = threading.Thread(target=self._drain_stderr, name=f"stderr-{label}", daemon=True)
            self._stderr_thread.start()
            assert self._process.stdout is not None
            self._stdout = _PrefetchedPipe(cast(BinaryIO, self._process.stdout), label=label)
            source = MatroskaStreamSource(cast(BinaryIO, self._stdout), header_path)
            try:
                # En-tête seul (métadonnées) : CodecPrivate non exigé (TS lu pour son TrackEntry).
                source.read_header(require_codec_private=not header_only)
            except (ValueError, OSError) as exc:
                raise self.failure(exc) from exc
            if self._cancelled():
                raise TaskCancelledError()
            self.reader: StreamedMatroskaReader = _ProducerReader(
                self, source, header_path, duration_ns=duration_ns,
            )
        except BaseException:
            self.close()
            raise

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
        if self._cancelled():
            return TaskCancelledError()
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
        if self._stderr_thread is not None:
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
        """Libère le processus et ses descendants, puis ses pipes (idempotent).

        Le groupe (POSIX) ou le job (Windows) est arrêté même si le processus
        principal est déjà sorti : un descendant gardant stderr ouvert bloquerait
        sinon le fil de lecture, et la fermeture du pipe derrière lui.
        """
        if self._closed:
            return
        self._closed = True
        process = self._process
        if not self._done:
            kill_process_tree(process, timeout=0.5)
        job = getattr(process, "_muxiveo_job", None)
        if job is not None:
            job.terminate()
        if self._stdout is not None:
            self._stdout.close()
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=5)
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
        ffprobe_bin: str | None = "ffprobe",
        cancelled: Callable[[], bool] = lambda: False,
        register: Callable[[subprocess.Popen], None] | None = None,
        unregister: Callable[[subprocess.Popen], None] | None = None,
        log: Callable[[str, str], None] | None = None,
    ) -> None:
        self._ffmpeg = ffmpeg_bin
        # ``None`` : rebasage FFmpeg par défaut (tests) ; sinon origine sondée une fois par source.
        self._ffprobe = ffprobe_bin
        self._origins: dict[Path, float] = {}
        self._work_dir = Path(work_dir)
        self._cancelled = cancelled
        self._register = register
        self._unregister = unregister
        self._log = log
        self._producers: list[StreamProducer] = []

    def open(
        self, spec: StreamTrackSpec | Sequence[StreamTrackSpec], *, duration_ns: int | None = None,
        header_only: bool = False,
    ) -> StreamedMatroskaReader:
        """Démarre le producteur de ``spec`` (une piste ou plusieurs d'une source) et renvoie son
        lecteur (en-tête déjà lu).

        ``header_only`` : seules les métadonnées comptent (contexte, piste
        réécrite ailleurs) ; le producteur est arrêté aussitôt l'en-tête lu.
        """
        group = _as_group(spec)
        if self._cancelled():
            raise TaskCancelledError()
        source = Path(group[0].source)
        if self._ffprobe is not None and group[0].origin_s is None:
            if source not in self._origins:
                try:
                    self._origins[source] = probe_start_time(source, self._ffprobe)
                except RuntimeError:
                    # En-tête seul (métadonnées, aucun bloc lu) : une source sans
                    # origine sondable (SRT externe) reste lisible ; une route l'exige.
                    if not header_only:
                        raise
            if source in self._origins:
                group = tuple(replace(item, origin_s=self._origins[source]) for item in group)
        ordinal = len(self._producers)
        indexes = ",".join(str(item.stream_index) for item in group)
        label = f"{source.name} #{indexes}"
        header = self._work_dir / f"stream_{ordinal}_{indexes.replace(',', '-')}.header.mkv"
        producer = StreamProducer(
            producer_command(self._ffmpeg, group), header, label=label, duration_ns=duration_ns,
            cancelled=self._cancelled, register=self._register, unregister=self._unregister,
            header_only=header_only,
        )
        self._producers.append(producer)
        if self._cancelled():
            raise TaskCancelledError()
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


#: Conteneurs ISO BMFF : configuration des codecs (stsd) connue dès l'en-tête, lecture en flux possible.
STREAMABLE_SUFFIXES = frozenset({".mp4", ".m4v", ".mov", ".m4a"})


def is_streamable_source(path: Path | str) -> bool:
    """Source transposable en flux (sinon : canonicalisation fichier, ex. TS dont la configuration est in-band)."""
    return Path(path).suffix.lower() in STREAMABLE_SUFFIXES


class StreamArtifacts:
    """Artefacts « en flux » d'un assemblage, démarrés à la demande.

    Clé d'un artefact = fichier d'en-tête de son producteur (Matroska lisible
    pour les métadonnées). ``readers`` / ``reopen`` se passent à
    :func:`core.matroska.assembly.compile_assembly_plan` : blocs lus depuis le
    pipe, nouvelle lecture pour une route détachée.
    """

    def __init__(self, inputs: StreamInputs, *, duration_ns: int | None = None) -> None:
        self._inputs = inputs
        self._duration_ns = duration_ns
        #: (source, index) → (en-tête du producteur, position de la piste dans son flux).
        self._routes: dict[tuple[Path, int], tuple[Path, int]] = {}
        #: Pistes annoncées, pas encore démarrées : groupe de producteur de chacune.
        self._groups: dict[tuple[Path, int], tuple[StreamTrackSpec, ...]] = {}
        self._metadata: dict[tuple[Path, int], Path] = {}
        self._contexts: dict[Path, Path] = {}
        self._route_headers: dict[Path, list[Path]] = {}
        self.readers: dict[Path, MatroskaReader] = {}
        self.reopen: dict[Path, Callable[[], MatroskaReader]] = {}

    def _open(
        self, spec: StreamTrackSpec | Sequence[StreamTrackSpec], *, header_only: bool = False,
    ) -> StreamedMatroskaReader:
        return self._inputs.open(spec, duration_ns=self._duration_ns, header_only=header_only)

    def declare(self, specs: Iterable[StreamTrackSpec]) -> None:
        """Pistes que l'assemblage lira : regroupées par :func:`group_stream_specs` au premier ``route``.

        Facultatif : une piste non annoncée a son propre producteur.
        """
        for group in group_stream_specs(replace(spec, carry_context=False) for spec in specs):
            for spec in group:
                key = (Path(spec.source), int(spec.stream_index))
                if key not in self._routes:
                    self._groups[key] = group

    def route(self, spec: StreamTrackSpec) -> tuple[Path, int]:
        """Artefact lu par l'assemblage (sans contexte) et position de la piste dans son flux.

        Les pistes annoncées ensemble (:meth:`declare`) partagent un producteur,
        démarré une fois ; une route détachée de la lecture partagée relance le
        même groupe (même numérotation des pistes).
        """
        key = (Path(spec.source), int(spec.stream_index))
        found = self._routes.get(key)
        if found is not None:
            return found
        group = self._groups.get(key) or (replace(spec, carry_context=False),)
        group = tuple(
            member for member in group
            if (Path(member.source), int(member.stream_index)) not in self._routes
        )
        reader = self._open(group)
        header = Path(reader.path)
        self._route_headers.setdefault(key[0], []).append(header)
        self.readers[header] = reader
        self.reopen[header] = partial(self._open, group)
        for position, member in enumerate(group):
            member_key = (Path(member.source), int(member.stream_index))
            self._routes[member_key] = (header, position)
            self._groups.pop(member_key, None)
        return self._routes[key]

    def metadata(self, spec: StreamTrackSpec) -> tuple[Path, int]:
        """TrackEntry d'une piste sans la lire : celui de sa route, sinon un producteur d'en-tête seul."""
        key = (Path(spec.source), int(spec.stream_index))
        if key in self._routes:
            return self._routes[key]
        header = self._metadata.get(key)
        if header is None:
            header = Path(self._open(replace(spec, carry_context=False), header_only=True).path)
            self._metadata[key] = header
            self._route_headers.setdefault(key[0], []).append(header)
        return header, 0

    def context(self, spec: StreamTrackSpec) -> Path:
        """En-tête portant chapitres et tags globaux de ``spec.source`` (producteur d'en-tête seul)."""
        source = Path(spec.source)
        header = self._contexts.get(source)
        if header is None:
            header = Path(self._open(replace(spec, carry_context=True), header_only=True).path)
            self._contexts[source] = header
        return header

    def route_headers(self, source: Path | str) -> list[Path]:
        """En-têtes de pistes d'une source, routes et en-têtes seuls (tags propres à chaque piste)."""
        return list(self._route_headers.get(Path(source), ()))


__all__ = [
    "STREAMABLE_SUFFIXES", "StreamArtifacts", "StreamInputs", "StreamProducer", "StreamTrackSpec",
    "group_stream_specs", "is_streamable_source", "producer_command",
]
