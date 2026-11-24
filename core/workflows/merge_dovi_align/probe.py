"""Décodage FFmpeg et signatures par image : barres exactes, vignette de luma, statistiques.

Une seule sortie ``gray16le`` de 64 colonnes par image, empilée verticalement :

- ``H`` lignes : masque de dépassement réduit horizontalement (une ligne est non noire si **un** pixel
  dépasse le seuil, calculé à pleine résolution avant toute réduction) ;
- ``W`` lignes : idem par colonne (réduction verticale puis transposition) ;
- 16 lignes : vignette 24 × 16 de luma (réduction ``area``), complétée à 64 colonnes.

Seuil : noir vidéo + 16 codes 10 bits (≈ 0,02 nit en PQ). Le masque binaire rend impossible toute
compensation par des pixels sous le noir et toute dilution d'un pixel isolé.
"""
from __future__ import annotations

import io
import re
import subprocess
import sys
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import cast

import numpy as np

from core.subprocess_utils import (
    kill_process_tree,
    subprocess_windows_no_window_kwargs,
    watch_process_cancellation,
)

from .frames import FrameTable
from .process import ProcessHooks

#: Largeur de la sortie empilée (cellules des profils de lignes / colonnes).
STRIP = 64
VIGNETTE_W, VIGNETTE_H = 24, 16
#: Marge au-dessus du noir vidéo (codes 10 bits) : au-delà, un pixel est « visible ».
BLACK_MARGIN = 16

_SHOWINFO = re.compile(r"\bn:\s*(\d+)\s+pts:\s*(-?\d+)")


class ProbeError(RuntimeError):
    """Décodage impossible ou fenêtre non concluante."""


def black_threshold(full_range: bool) -> int:
    """Code 10 bits au-delà duquel un pixel de luma n'est plus noir."""
    return (0 if full_range else 64) + BLACK_MARGIN


def filter_graph(width: int, height: int, threshold: int, *, cuda_format: str | None = None) -> str:
    """Graphe FFmpeg produisant la sortie empilée ``STRIP × (H + W + 16)`` (``gray16le``)."""
    # Luma extraite telle quelle (``extractplanes``) : une conversion vers un format gris ramènerait la
    # plage limitée en plage pleine et fausserait le seuil.
    download = f"hwdownload,format={cuda_format},format=yuv420p10le," if cuda_format else ""
    return (
        f"[0:v:0]{download}showinfo,format=yuv420p10le|yuv422p10le|yuv444p10le,extractplanes=y,split=2[m][v];"
        f"[m]lut=c0='gt(val,{threshold})*1023',format=gray16le,split=2[mr][mc];"
        f"[mr]scale={STRIP}:{height}:flags=area[rows];"
        f"[mc]scale={width}:{STRIP}:flags=area,transpose=dir=clock[cols];"
        f"[v]scale={VIGNETTE_W}:{VIGNETTE_H}:flags=area,format=gray16le,pad={STRIP}:{VIGNETTE_H}[vig];"
        "[rows][cols][vig]vstack=inputs=3[out]"
    )


@dataclass
class Signatures:
    """Signatures d'images consécutives (ordre d'affichage)."""

    width: int
    height: int
    #: Rang d'affichage de chaque image.
    ranks: np.ndarray
    #: Lignes/colonnes noires depuis chaque bord ; -1 pour une image entièrement noire.
    top: np.ndarray
    bottom: np.ndarray
    left: np.ndarray
    right: np.ndarray
    #: Vignettes ``(N, 16, 24)`` : luma 10 bits ramenée sur 8 bits.
    vignettes: np.ndarray
    #: Moyenne, écart type et maximum de la vignette (codes 10 bits).
    mean: np.ndarray
    std: np.ndarray
    peak: np.ndarray
    decoder: str = "logiciel"
    pts: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int64))

    def __len__(self) -> int:
        return int(self.ranks.size)

    def slice(self, start: int, stop: int) -> "Signatures":
        """Sous-ensemble des positions ``[start, stop)`` (indices dans cet objet)."""
        return Signatures(
            self.width, self.height, self.ranks[start:stop], self.top[start:stop], self.bottom[start:stop],
            self.left[start:stop], self.right[start:stop], self.vignettes[start:stop], self.mean[start:stop],
            self.std[start:stop], self.peak[start:stop], self.decoder, self.pts[start:stop],
        )


class _Accumulator:
    """Analyse image par image de la sortie empilée, sans conserver les profils."""

    def __init__(self, width: int, height: int) -> None:
        self.width, self.height = width, height
        self.frame_bytes = STRIP * (height + width + VIGNETTE_H) * 2
        self.bars: list[tuple[int, int, int, int]] = []
        self.vignettes: list[np.ndarray] = []
        self.stats: list[tuple[float, float, float]] = []

    @staticmethod
    def _edges(profile: np.ndarray) -> tuple[int, int]:
        hits = np.flatnonzero(profile)
        if hits.size == 0:
            return -1, -1
        return int(hits[0]), int(profile.size - 1 - hits[-1])

    def add(self, buffer: bytes | bytearray | memoryview) -> None:
        plane = np.frombuffer(buffer, dtype="<u2").reshape(self.height + self.width + VIGNETTE_H, STRIP)
        top, bottom = self._edges(plane[:self.height].any(axis=1))
        left, right = self._edges(plane[self.height:self.height + self.width].any(axis=1))
        self.bars.append((top, bottom, left, right))
        luma = plane[self.height + self.width:, :VIGNETTE_W].astype(np.float32) / 64.0
        self.vignettes.append((luma / 4.0).clip(0, 255).astype(np.uint8))
        self.stats.append((float(luma.mean()), float(luma.std()), float(luma.max())))

    def result(self, ranks: list[int], pts: list[int], decoder: str) -> Signatures:
        count = len(self.bars)
        bars = np.asarray(self.bars, dtype=np.int16).reshape(count, 4)
        stats = np.asarray(self.stats, dtype=np.float32).reshape(count, 3)
        vignettes = (np.stack(self.vignettes) if self.vignettes
                     else np.zeros((0, VIGNETTE_H, VIGNETTE_W), dtype=np.uint8))
        return Signatures(
            self.width, self.height, np.asarray(ranks, dtype=np.int64), bars[:, 0], bars[:, 1], bars[:, 2],
            bars[:, 3], vignettes, stats[:, 0], stats[:, 1], stats[:, 2], decoder,
            np.asarray(pts[:count], dtype=np.int64),
        )


def parse_showinfo(lines: list[str]) -> list[int]:
    """PTS des images dans l'ordre de sortie, d'après les lignes ``showinfo`` de FFmpeg."""
    out: list[int] = []
    for line in lines:
        match = _SHOWINFO.search(line)
        if match is not None and int(match.group(1)) == len(out):
            out.append(int(match.group(2)))
    return out


#: Format de téléchargement des images NVDEC selon (chrominance, profondeur) de la source.
_CUDA_FORMATS: dict[tuple[str, int], str] = {
    ("420", 8): "nv12", ("420", 10): "p010le", ("420", 12): "p016le",
    ("422", 8): "nv16", ("422", 10): "p210le", ("422", 12): "p216le",
    ("444", 8): "yuv444p", ("444", 10): "yuv444p16le", ("444", 12): "yuv444p16le",
}
_PIX_FMT = re.compile(r"^yuv(420|422|444)p(?:(10|12)(?:le|be))?$")


def cuda_format(pix_fmt: str) -> str | None:
    """Format transférable depuis NVDEC pour ``pix_fmt`` ; None si NVDEC ne le produit pas (repli logiciel)."""
    match = _PIX_FMT.match(pix_fmt or "")
    if match is None:
        return None
    return _CUDA_FORMATS.get((match.group(1), int(match.group(2) or 8)))


def _command(ffmpeg: str, table: FrameTable, *, seek: str | None, frames: int | None, hwaccel: str | None) -> list[str]:
    props = table.props
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-v", "info"]
    if hwaccel == "cuda":
        cmd += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
    # PTS d'origine conservés : chaque image décodée se rattache à la table par son PTS exact.
    cmd += ["-copyts"]
    if seek is not None:
        cmd += ["-ss", seek]
    cmd += ["-i", str(table.path), "-an", "-sn", "-dn"]
    graph = filter_graph(props.width, props.height, black_threshold(props.full_range),
                         cuda_format=cuda_format(props.pix_fmt) if hwaccel == "cuda" else None)
    cmd += ["-filter_complex", graph, "-map", "[out]", "-fps_mode", "passthrough"]
    if frames is not None:
        cmd += ["-frames:v", str(frames)]
    cmd += ["-f", "rawvideo", "-pix_fmt", "gray16le", "pipe:1"]
    return cmd


def _popen(command: list[str]) -> subprocess.Popen:
    kwargs: dict = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "stdin": subprocess.DEVNULL}
    kwargs.update(subprocess_windows_no_window_kwargs(include_stdin=False))
    if sys.platform != "win32":
        kwargs["start_new_session"] = True
    # Commande argv construite ici, sans interprétation shell.
    # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
    return subprocess.Popen(command, **kwargs)


def _run(command: list[str], table: FrameTable, hooks: ProcessHooks,
         progress: Callable[[int], None] | None) -> tuple[_Accumulator, list[int], int, str]:
    """Lance FFmpeg, analyse chaque image en flux ; (accumulateur, PTS showinfo, code retour, erreurs).

    Annulation surveillée pendant les lectures bloquantes (processus tué, ``InterruptedError``) ; flux
    fermés, processus attendu et ``on_end`` appelé dans tous les cas.
    """
    hooks.check()
    acc = _Accumulator(table.props.width, table.props.height)
    proc = _popen(command)
    lines: list[str] = []
    errors: deque[str] = deque(maxlen=40)
    reader: threading.Thread | None = None
    try:
        if hooks.on_start is not None:
            hooks.on_start(proc)

        def drain() -> None:
            assert proc.stderr is not None
            for raw in proc.stderr:
                text = raw.decode("utf-8", "replace")
                if "pts:" in text and "n:" in text:
                    lines.append(text)
                else:
                    errors.append(text.rstrip())

        reader = threading.Thread(target=drain, name="merge-dovi-align-stderr", daemon=True)
        reader.start()
        buffer = bytearray(acc.frame_bytes)
        view = memoryview(buffer)
        stdout = cast(io.BufferedReader, proc.stdout)
        with watch_process_cancellation(proc, hooks.cancelled):
            while True:
                got = 0
                while got < acc.frame_bytes:
                    chunk = stdout.readinto(view[got:])
                    if not chunk:
                        break
                    got += chunk
                if got == 0:
                    break
                if got < acc.frame_bytes:
                    hooks.check()
                    raise ProbeError("Image tronquée dans la sortie de FFmpeg.")
                acc.add(buffer)
                if progress is not None and len(acc.bars) % 256 == 0:
                    progress(len(acc.bars))
            returncode = proc.wait()
        hooks.check()
    except BaseException:
        kill_process_tree(proc, timeout=0.2)
        raise
    finally:
        proc.wait()
        if reader is not None:
            reader.join(timeout=5)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()
        if hooks.on_end is not None:
            hooks.on_end(proc)
    return acc, parse_showinfo(lines), returncode, "\n".join(list(errors)[-8:])


def _decode(table: FrameTable, ffmpeg: str, hooks: ProcessHooks, *, seek: str | None, frames: int | None,
            hwaccel: str | None, progress: Callable[[int], None] | None) -> tuple[_Accumulator, list[int], str]:
    """Décodage CUDA si demandé et possible, repli logiciel si le décodeur matériel échoue avant toute image."""
    if hwaccel == "cuda" and cuda_format(table.props.pix_fmt) is None:
        hwaccel = None
    attempts = [hwaccel, None] if hwaccel else [None]
    last_error = ""
    for attempt in attempts:
        decoder = "CUDA" if attempt == "cuda" else "logiciel"
        acc, pts, code, errors = _run(_command(ffmpeg, table, seek=seek, frames=frames, hwaccel=attempt),
                                      table, hooks, progress)
        if code == 0 or (frames is not None and len(acc.bars) >= frames):
            return acc, pts, decoder
        last_error = errors
        if acc.bars:
            break
    raise ProbeError(f"Décodage impossible ({table.path.name}) : {last_error.strip()[-300:]}")


def decode_all(table: FrameTable, *, ffmpeg: str = "ffmpeg", hooks: ProcessHooks | None = None,
               hwaccel: str | None = None, progress: Callable[[int], None] | None = None) -> Signatures:
    """Passe complète : signatures de toutes les images en ordre d'affichage.

    Entrée indexable : chaque image est rattachée à son rang par son PTS, toutes doivent être présentes
    une fois. Non indexable : rang = ordre de sortie du décodeur.
    """
    hooks = hooks or ProcessHooks()
    acc, pts, decoder = _decode(table, ffmpeg, hooks, seek=None, frames=None, hwaccel=hwaccel, progress=progress)
    count = len(acc.bars)
    if not table.indexable:
        return acc.result(list(range(count)), pts, decoder)
    ranks = _ranks(table, pts, count)
    if ranks != list(range(len(table.pts))):
        raise ProbeError(
            f"Passe complète de {table.path.name} : {count} images décodées pour {len(table.pts)} paquets "
            "ou horodatages inattendus.")
    return acc.result(ranks, pts, decoder)


def _ranks(table: FrameTable, pts: list[int], count: int) -> list[int]:
    if len(pts) < count:
        raise ProbeError("Horodatages des images décodées incomplets (showinfo).")
    ranks: list[int] = []
    for value in pts[:count]:
        rank = table.rank_of(value)
        if rank is None:
            raise ProbeError(f"Image décodée au PTS {value} absente de la table de {table.path.name}.")
        ranks.append(rank)
    return ranks


def decode_window(table: FrameTable, start: int, count: int, *, ffmpeg: str = "ffmpeg",
                  hooks: ProcessHooks | None = None, hwaccel: str | None = None) -> Signatures:
    """Images ``[start, start + count)`` d'une entrée indexable, chacune identifiée par son PTS exact.

    Recherche une demi-image avant l'image visée ; toute anomalie (départ trop tardif, PTS inconnu, image
    manquante ou dupliquée) relance la recherche plus tôt ; échec de toutes les relances : :class:`ProbeError`.
    """
    hooks = hooks or ProcessHooks()
    if not table.indexable:
        raise ProbeError(f"{table.path.name} n'est pas indexable ({table.reason}).")
    total = len(table.pts)
    start = max(0, start)
    stop = min(total, start + count)
    if stop <= start:
        raise ProbeError("Fenêtre vide.")
    expected = list(range(start, stop))
    reason = "introuvable par recherche"
    tried: set[int] = set()
    for back in (0, 1, 8, 48):
        first = max(0, start - back)
        if first in tried:
            continue
        tried.add(first)
        acc, pts, decoder = _decode(table, ffmpeg, hooks, seek=table.seek_seconds(first),
                                    frames=stop - first, hwaccel=hwaccel, progress=None)
        try:
            ranks = _ranks(table, pts, len(acc.bars))
        except ProbeError as exc:
            reason = str(exc)
            continue
        offset = ranks.index(start) if start in ranks else -1
        if offset < 0 or ranks[offset:offset + len(expected)] != expected:
            reason = "images manquantes, dupliquées ou départ trop tardif"
            continue
        return acc.result(ranks, pts, decoder).slice(offset, offset + len(expected))
    raise ProbeError(f"Fenêtre {start}–{stop - 1} de {table.path.name} : {reason}.")
