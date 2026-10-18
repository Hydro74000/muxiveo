"""FEL Analyzer : apport de la couche d'amélioration Dolby Vision (FEL) image par image.

Comparaison de l'image reconstruite (RPU + EL) à la couche de base passée par les courbes du RPU
sans résiduel (« mapped » : ce qu'affiche une TV Dolby Vision sans FEL, P8.1). Les deux images
sont produites par le plugin mvo-fel en Y'CbCr 4:2:0 10 bits, sans tramage, aux mêmes instants
échantillonnés (rafales de deux images pour la stabilité temporelle). Sept catégories, seuils
provisoires (à étalonner sur le corpus FEL) ; survie à l'encodage mesurée par un vrai encodage.
"""
from __future__ import annotations

import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

import numpy as np

from core.hdr_inspection.fel_layer import LayerFacts
from core.hdr_inspection.pq import pq_to_nits
from core.i18n import translate_text
from core.subprocess_utils import subprocess_windows_no_window_kwargs

#: Catégorie : libellé, unité, seuils faible/moyen et moyen/fort (valeur absolue).
CATEGORIES: tuple[tuple[str, str, str, tuple[float, float]], ...] = (
    ("tone", "Ton / exposition", "codes", (2.0, 8.0)),
    ("highlights", "Hautes lumières", "%", (5.0, 30.0)),
    ("detail", "Détail / texture", "codes RMS", (0.5, 2.0)),
    ("grain", "Grain", "%", (3.0, 10.0)),
    ("colour", "Couleur", "codes", (0.5, 2.0)),
    ("shadows", "Ombres", "codes", (1.0, 4.0)),
    ("temporal", "Stabilité temporelle", "codes", (0.5, 2.0)),
)
LEVELS = ("faible", "moyen", "fort")
#: Plage limitée 10 bits : noir et blanc nominaux de la luma.
_BLACK, _RANGE = 64.0, 876.0
#: Hautes lumières mesurées seulement si le percentile 99,9 % atteint ce niveau.
_HIGHLIGHT_NITS = 100.0


@dataclass(frozen=True)
class Frame:
    """Image Y'CbCr 4:2:0 10 bits (codes)."""

    y: np.ndarray
    cb: np.ndarray
    cr: np.ndarray


@dataclass(frozen=True)
class FelSample:
    time_s: float
    metrics: dict[str, float]


@dataclass(frozen=True)
class CategorySummary:
    key: str
    median: float
    p90: float
    level: str


@dataclass(frozen=True, eq=False)
class Moment:
    time_s: float
    score: float
    #: Vignettes RGB 8 bits (H × W × 3) : couche mapped, reconstruite, carte de l'écart.
    mapped: np.ndarray
    reconstructed: np.ndarray
    difference: np.ndarray


@dataclass(frozen=True)
class Survival:
    encoder: str
    bitrate_kbps: int
    #: Part de l'apport conservée après encodage (projection), basses et hautes fréquences.
    tone_kept: float
    detail_kept: float
    #: Erreur d'encodage (RMS haute fréquence, codes) face à l'apport de détail.
    encode_error: float
    detail: float


@dataclass(frozen=True, eq=False)
class FelReport:
    facts: LayerFacts | None
    samples: tuple[FelSample, ...]
    summary: tuple[CategorySummary, ...]
    moments: tuple[Moment, ...]
    survival: Survival | None = None
    notes: tuple[str, ...] = field(default=())

    def level(self, key: str) -> str:
        return next((s.level for s in self.summary if s.key == key), "faible")


def _box(image: np.ndarray, radius: int) -> np.ndarray:
    """Moyenne glissante (2r + 1)² par sommes cumulées, bords répliqués."""
    padded = np.pad(image.astype(np.float64), radius + 1, mode="edge")
    c = padded.cumsum(0).cumsum(1)
    k = 2 * radius + 1
    return (c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k])[:image.shape[0], :image.shape[1]] / (k * k)


def _nits(codes: np.ndarray | float) -> np.ndarray:
    return pq_to_nits(np.clip((np.asarray(codes, dtype=np.float64) - _BLACK) / _RANGE, 0, 1))


def active_rows(frame: Frame, area: tuple[int, int, int, int] | None = None) -> slice:
    """Lignes actives : zone L5 (haut, bas) si connue, sinon lignes dont la luma dépasse le noir."""
    height = frame.y.shape[0]
    if area is not None and area[2] + area[3] < height:
        return slice(area[2], height - area[3])
    bright = np.flatnonzero(frame.y.mean(axis=1) > _BLACK + 4)
    return slice(int(bright[0]), int(bright[-1]) + 1) if bright.size else slice(0, height)


def frame_metrics(reconstructed: Frame, mapped: Frame, rows: slice) -> dict[str, float]:
    """Mesures d'une paire d'images (même instant)."""
    r = reconstructed.y[rows].astype(np.float64)
    m = mapped.y[rows].astype(np.float64)
    d = r - m
    low = _box(d, 8)
    metrics = {
        "tone": float(np.abs(low).mean()),
        "detail": float(np.sqrt(((d - _box(d, 2)) ** 2).mean())),
        "mean": float(d.mean()),
    }
    # Hautes lumières : percentile 99,9 % de la luma, en nits (images sans hautes lumières écartées).
    pr, pm = float(_nits(np.percentile(r, 99.9))), float(_nits(np.percentile(m, 99.9)))
    metrics["highlights"] = 100.0 * (pr - pm) / pm if pm >= _HIGHLIGHT_NITS else 0.0
    # Grain : bruit haute fréquence dans les aplats (faible variance locale de la couche mapped).
    mean_m = _box(m, 2)
    variance = _box(m * m, 2) - mean_m ** 2
    flat = variance < 36.0
    if flat.mean() > 0.05:
        noise_r = float(np.std((r - _box(r, 2))[flat]))
        noise_m = float(np.std((m - mean_m)[flat]))
        metrics["grain"] = 100.0 * (noise_r / noise_m - 1.0) if noise_m > 0.05 else 0.0
    else:
        metrics["grain"] = 0.0
    chroma_rows = slice(rows.start // 2, (rows.stop + 1) // 2)
    colour = [float(np.abs(_box(a[chroma_rows].astype(np.float64) - b[chroma_rows].astype(np.float64), 4)).mean())
              for a, b in ((reconstructed.cb, mapped.cb), (reconstructed.cr, mapped.cr))]
    metrics["colour"] = sum(colour) / 2
    dark = m < 300  # ≈ 10 nits en PQ 10 bits plage limitée
    metrics["shadows"] = float(np.abs(low[dark]).mean()) if dark.mean() > 0.02 else 0.0
    return metrics


def classify(value: float, thresholds: tuple[float, float]) -> str:
    value = abs(value)
    return LEVELS[0] if value < thresholds[0] else LEVELS[1] if value < thresholds[1] else LEVELS[2]


def summarise(samples: Sequence[FelSample]) -> tuple[CategorySummary, ...]:
    """Médiane et p90 par catégorie (valeurs absolues) ; niveau d'après le p90 (moments où le FEL agit)."""
    summary = []
    for key, _label, _unit, thresholds in CATEGORIES:
        values = np.array([abs(s.metrics.get(key, 0.0)) for s in samples], dtype=np.float64)
        if not values.size:
            summary.append(CategorySummary(key, 0.0, 0.0, LEVELS[0]))
            continue
        p90 = float(np.percentile(values, 90))
        summary.append(CategorySummary(key, float(np.median(values)), p90, classify(p90, thresholds)))
    return tuple(summary)


def recommendation(report: FelReport) -> tuple[str, str]:
    """Verdict et explication (texte traduit)."""
    strong = [key for key, *_ in CATEGORIES if report.level(key) == LEVELS[2]]
    medium = [key for key, *_ in CATEGORIES if report.level(key) == LEVELS[1]]
    labels = {key: translate_text(label) for key, label, *_ in CATEGORIES}
    visible = [key for key in strong if key in ("tone", "highlights", "colour", "shadows")]
    if visible:
        return (translate_text("Intégrer le FEL : utile"),
                translate_text("Apport fort : {categories}.", categories=", ".join(labels[k] for k in strong)))
    if strong:
        survival = report.survival
        if survival is not None and survival.detail_kept < 0.5:
            return (translate_text("Intégrer le FEL : utile seulement à haut débit"),
                    translate_text("Apport de détail fin, à {kept} % conservé à {bitrate} kbit/s ({encoder}).",
                                   kept=round(survival.detail_kept * 100), bitrate=survival.bitrate_kbps,
                                   encoder=survival.encoder))
        return (translate_text("Intégrer le FEL : utile si le débit le permet"),
                translate_text("Apport fort en détail fin ({categories}) ; tester la survie à l'encodage.",
                               categories=", ".join(labels[k] for k in strong)))
    if medium:
        return (translate_text("Intégrer le FEL : apport modéré"),
                translate_text("Apport moyen : {categories}.", categories=", ".join(labels[k] for k in medium)))
    return (translate_text("Intégrer le FEL : inutile"),
            translate_text("Apport négligeable : ce FEL se comporte comme un MEL."))


# ── Vignettes ────────────────────────────────────────────────────────────────
def _downscale(plane: np.ndarray, factor: int) -> np.ndarray:
    h, w = (plane.shape[0] // factor) * factor, (plane.shape[1] // factor) * factor
    return plane[:h, :w].reshape(h // factor, factor, w // factor, factor).mean(axis=(1, 3))


def thumbnail(frame: Frame, rows: slice, width: int = 480) -> np.ndarray:
    """Vignette SDR (RGB 8 bits) d'une image PQ : BT.2020 → luminance compressée vers 100 nits, gamma 2,4."""
    factor = max(2, frame.y.shape[1] // width)
    factor -= factor % 2
    y = _downscale(frame.y[rows].astype(np.float64), factor)
    chroma_rows = slice(rows.start // 2, (rows.stop + 1) // 2)
    cb = _downscale(frame.cb[chroma_rows].astype(np.float64), factor // 2)[:y.shape[0], :y.shape[1]]
    cr = _downscale(frame.cr[chroma_rows].astype(np.float64), factor // 2)[:y.shape[0], :y.shape[1]]
    yn, cbn, crn = (y - _BLACK) / _RANGE, (cb - 512) / 896, (cr - 512) / 896
    rgb_pq = np.stack([yn + 1.4746 * crn, yn - 0.16455 * cbn - 0.57135 * crn, yn + 1.8814 * cbn], axis=-1)
    nits = pq_to_nits(np.clip(rgb_pq, 0, 1))
    sdr = nits / (1.0 + nits / 300.0) / 75.0  # compression douce des hautes lumières (aperçu)
    return (np.clip(sdr, 0, 1) ** (1 / 2.4) * 255).astype(np.uint8)


def difference_map(reconstructed: Frame, mapped: Frame, rows: slice, width: int = 480) -> np.ndarray:
    """Carte de l'écart (basses fréquences, valeur absolue) : noir → violet → jaune, 16 codes au maximum."""
    factor = max(2, reconstructed.y.shape[1] // width)
    factor -= factor % 2
    d = np.abs(_downscale(reconstructed.y[rows].astype(np.float64) - mapped.y[rows].astype(np.float64), factor))
    t = np.clip(d / 16.0, 0, 1)
    rgb = np.stack([np.clip(2 * t, 0, 1), np.clip(2 * t - 1, 0, 1) * 0.9, np.clip(1.2 * t, 0, 1) * (1 - t)], axis=-1)
    return (rgb * 255).astype(np.uint8)


# ── Capture par le plugin ────────────────────────────────────────────────────
CaptureLayer = Callable[[str, float, int], list[Frame]]


def plugin_capture(engine: object, path: Path, stream: int, *, ffmpeg_bin: str, width: int, height: int,
                   frame_rate: str, chroma_location: str = "topleft", device: str = "cpu",
                   precision: str = "fast", threads: int = 8,
                   cancelled: threading.Event | None = None) -> CaptureLayer:
    """Fonction de capture : ``count`` images de la couche ``layer`` à partir de ``time_s``.

    CPU par défaut : pour quelques images, l'initialisation d'un contexte GPU coûte plus que le
    rendu (mesuré 0,7 s contre 1,7 s pour deux images UHD) ; le CPU est le rendu certifié exact.
    """
    from core.fel.engine import FelSource

    frame_size = width * height * 3 // 2 * 2

    class _Plan:
        def acquire(self) -> tuple[str, Callable[[], None]]:
            return device, lambda: None

        def log(self, _level: str, _message: str) -> None:
            return None

    def capture(layer: str, time_s: float, count: int) -> list[Frame]:
        reader = subprocess.Popen(  # nosec B603 (outil configuré, arguments séparés)
            [ffmpeg_bin, "-v", "error", "-nostdin", "-f", "nut", "-i", "pipe:0", "-frames:v", str(count),
             "-f", "rawvideo", "-pix_fmt", "yuv420p10le", "pipe:1"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            **subprocess_windows_no_window_kwargs(include_stdin=False))
        assert reader.stdin is not None and reader.stdout is not None
        source = FelSource(engine=engine, path=path, stream=stream, threads=threads,  # type: ignore[arg-type]
                           frame_rate=frame_rate, device_plan=_Plan(),  # type: ignore[arg-type]
                           start_us=int(round(max(0.0, time_s) * 1e6)), pixel_format="yuv420p10le",
                           chroma_location=chroma_location, dither="none", timestamps="zero",
                           precision=precision, layer=layer)
        producer = source.start(reader.stdin, cancelled or threading.Event())
        frames: list[Frame] = []
        try:
            for _ in range(count):
                data = reader.stdout.read(frame_size)
                if len(data) < frame_size:
                    break
                frames.append(_frames(data, width, height))
        finally:
            producer.cancel()
            reader.kill()
            reader.wait()
            producer.finish()
        if len(frames) < count:
            producer.check_error()
            raise RuntimeError(translate_text("Couche {layer} : images manquantes à {time} s.", layer=layer,
                                              time=f"{time_s:.1f}"))
        return frames

    return capture


def analyse(capture: CaptureLayer, times: Sequence[float], *,
            area: Callable[[float], tuple[int, int, int, int] | None] = lambda _t: None,
            facts: LayerFacts | None = None, progress: Callable[[float], None] | None = None,
            cancelled: Callable[[], bool] | None = None, moments: int = 5) -> FelReport:
    """Mesure chaque instant (rafale de deux images par couche) ; vignettes des moments les plus marqués."""
    samples: list[FelSample] = []
    # Vignettes calculées dès l'entrée d'un instant parmi les plus marqués (images pleines libérées).
    kept: list[Moment] = []

    def pair(time_s: float) -> tuple[list[Frame], list[Frame]]:
        return capture("reconstructed", time_s, 2), capture("mapped", time_s, 2)

    # Capture de l'instant suivant pendant les mesures de l'instant courant.
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="fel-capture") as prefetch:
        pending = prefetch.submit(pair, times[0]) if times else None
        for index, time_s in enumerate(times):
            if cancelled is not None and cancelled():
                if pending is not None:
                    pending.cancel()
                raise InterruptedError
            assert pending is not None
            reconstructed, mapped = pending.result()
            pending = prefetch.submit(pair, times[index + 1]) if index + 1 < len(times) else None
            metrics, score, rows = _sample_metrics(reconstructed, mapped, area(time_s))
            samples.append(FelSample(time_s, metrics))
            if len(kept) < moments or score > kept[-1].score:
                recon, mapped_frame = reconstructed[0], mapped[0]
                kept.append(Moment(time_s, score, thumbnail(mapped_frame, rows), thumbnail(recon, rows),
                                   difference_map(recon, mapped_frame, rows)))
                kept = sorted(kept, key=lambda item: -item.score)[:moments]
            if progress is not None:
                progress((index + 1) / max(1, len(times)))
    return FelReport(facts, tuple(samples), summarise(samples), tuple(sorted(kept, key=lambda m: m.time_s)))


def _sample_metrics(reconstructed: list[Frame], mapped: list[Frame],
                    area: tuple[int, int, int, int] | None) -> tuple[dict[str, float], float, slice]:
    """Mesures d'un instant (première image de la rafale) et indice des moments marquants."""
    rows = active_rows(mapped[0], area)
    first = frame_metrics(reconstructed[0], mapped[0], rows)
    metrics = {key: first[key] for key in first if key != "mean"}
    # Stabilité : variation de l'écart moyen d'une image à la suivante (pompage corrigé par le FEL).
    following = float((reconstructed[1].y[rows].astype(np.float64) - mapped[1].y[rows].astype(np.float64)).mean())
    metrics["temporal"] = abs(following - first["mean"])
    score = metrics["tone"] / 8.0 + metrics["detail"] / 2.0 + abs(metrics["highlights"]) / 30.0
    return metrics, score, rows


def sample_times(duration_s: float, interval_s: float, margin_s: float = 5.0) -> list[float]:
    """Instants répartis sur le film (marges du début et de la fin écartées)."""
    if duration_s <= 2 * margin_s:
        return [max(0.0, duration_s / 2)]
    count = max(1, int((duration_s - 2 * margin_s) // max(1.0, interval_s)))
    return [margin_s + (index + 0.5) * (duration_s - 2 * margin_s) / count for index in range(count)]


# ── Survie à l'encodage ──────────────────────────────────────────────────────
def _encoder_args(encoder: str) -> tuple[list[str], list[str]]:
    """Options avant l'entrée (périphérique) et de sortie (format, préréglage) de l'encodeur de test."""
    if encoder in ("hevc_vaapi", "hevc_qsv"):
        from core.workflows.encode.hw_devices import select_linux_hwaccel_device

        node = select_linux_hwaccel_device(encoder) if sys.platform.startswith("linux") else None
        if encoder == "hevc_vaapi":
            return (["-vaapi_device", node] if node else []), ["-vf", "format=p010le,hwupload"]
        return (["-qsv_device", node] if node else []), ["-pix_fmt", "p010le"]
    if encoder == "hevc_nvenc":
        return [], ["-pix_fmt", "p010le", "-preset", "p5"]
    return [], ["-pix_fmt", "yuv420p10le", "-preset", "medium", "-x265-params", "log-level=error"]


def _frames(data: bytes, width: int, height: int) -> Frame:
    planes = np.frombuffer(data, dtype="<u2")
    luma = width * height
    return Frame(planes[:luma].reshape(height, width), planes[luma:luma * 5 // 4].reshape(height // 2, width // 2),
                 planes[luma * 5 // 4:].reshape(height // 2, width // 2))


def survival_indices(total: int) -> list[int]:
    """Huit images réparties dans la rafale (toutes si elle est plus courte) : base du calcul de survie."""
    return sorted({round(i * (total - 1) / 7) for i in range(8)}) if total > 8 else list(range(total))


def _streamed(cmd: list[str], feed: Callable[[IO[bytes]], object], consume: Callable[[IO[bytes]], object]) -> None:
    """Exécute ``cmd`` : entrée écrite par un fil, sortie lue par l'appelant, erreurs collectées."""
    process = subprocess.Popen(  # nosec B603 (outil configuré, arguments séparés)
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        **subprocess_windows_no_window_kwargs(include_stdin=False))
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    stdin, stderr = process.stdin, process.stderr
    errors: list[bytes] = []

    def write() -> None:
        try:
            feed(stdin)
        except (BrokenPipeError, OSError, ValueError):
            pass  # processus arrêté : son code de sortie et son journal font foi
        finally:
            try:
                stdin.close()
            except OSError:
                pass

    threads = [threading.Thread(target=write, name="fel-survival-in", daemon=True),
               threading.Thread(target=lambda: errors.append(stderr.read()), name="fel-survival-err", daemon=True)]
    for thread in threads:
        thread.start()
    try:
        consume(process.stdout)
    except BaseException:
        process.kill()
        raise
    finally:
        process.stdout.close()
        for thread in threads:
            thread.join()
        process.wait()
    if process.returncode:
        raise subprocess.CalledProcessError(process.returncode, cmd[0], stderr=b"".join(errors))


def encode_round_trip(frames: Sequence[Frame], *, ffmpeg_bin: str, encoder: str, bitrate_kbps: int,
                      frame_rate: str, keep: Sequence[int] | None = None) -> list[Frame]:
    """Encode puis décode les images (HEVC 10 bits au débit demandé) ; mêmes dimensions.

    ``keep`` : rangs des images décodées conservées (toutes par défaut) ; les autres sont lues puis
    oubliées, et les images d'entrée transmises une à une (mémoire bornée en UHD).
    """
    height, width = frames[0].y.shape
    device, output = _encoder_args(encoder)
    common = ["-v", "error", "-nostdin"]
    chunks: list[bytes] = []

    def feed_frames(pipe: IO[bytes]) -> None:
        for frame in frames:
            for plane in (frame.y, frame.cb, frame.cr):
                pipe.write(plane.astype("<u2").tobytes())

    _streamed([ffmpeg_bin, *common, *device, "-f", "rawvideo", "-pix_fmt", "yuv420p10le", "-s", f"{width}x{height}",
               "-r", frame_rate, "-i", "pipe:0", *output, "-c:v", encoder, "-b:v", f"{bitrate_kbps}k",
               "-maxrate", f"{bitrate_kbps * 2}k", "-bufsize", f"{bitrate_kbps * 2}k", "-f", "hevc", "pipe:1"],
              feed_frames, lambda pipe: chunks.append(pipe.read()))
    encoded = b"".join(chunks)
    wanted = set(range(len(frames)) if keep is None else keep)
    size = width * height * 3 // 2 * 2
    result: list[Frame] = []

    def read_frames(pipe: IO[bytes]) -> None:
        index = 0
        while len(data := pipe.read(size)) == size:
            if index in wanted:
                result.append(_frames(data, width, height))
            index += 1

    _streamed([ffmpeg_bin, *common, "-f", "hevc", "-i", "pipe:0", "-f", "rawvideo", "-pix_fmt", "yuv420p10le",
               "pipe:1"], lambda pipe: pipe.write(encoded), read_frames)
    return result


def survival(reconstructed: Sequence[Frame], mapped: Sequence[Frame], encoded: Sequence[Frame], *,
             encoder: str, bitrate_kbps: int, rows: slice) -> Survival:
    """Part de l'apport du FEL conservée après encodage (projection de E − M sur R − M).

    Images alignées rang par rang : en pratique celles de ``survival_indices`` (l'encodage, lui,
    porte sur la rafale entière).
    """
    tone_num = tone_den = detail_num = detail_den = error = 0.0
    count = min(len(reconstructed), len(mapped), len(encoded))
    if count == 0:
        raise ValueError("Aucune image décodée après l'encodage de test.")
    for r_frame, m_frame, e_frame in zip(reconstructed, mapped, encoded):
        r = r_frame.y[rows].astype(np.float64)
        m = m_frame.y[rows].astype(np.float64)
        e = e_frame.y[rows].astype(np.float64)
        contribution, kept = r - m, e - m
        low_c, low_k = _box(contribution, 8), _box(kept, 8)
        high_c, high_k = contribution - _box(contribution, 2), kept - _box(kept, 2)
        tone_num += float((low_c * low_k).sum())
        tone_den += float((low_c * low_c).sum())
        detail_num += float((high_c * high_k).sum())
        detail_den += float((high_c * high_c).sum())
        # Bruit d'encodage haute fréquence : (E − R) filtré = high_k − high_c (filtre linéaire).
        error += float(np.sqrt(((high_k - high_c) ** 2).mean()))
    detail = float(np.sqrt(detail_den / (count * reconstructed[0].y[rows].size)))
    return Survival(encoder, bitrate_kbps,
                    tone_kept=max(0.0, min(1.0, tone_num / tone_den)) if tone_den > 0 else 1.0,
                    detail_kept=max(0.0, min(1.0, detail_num / detail_den)) if detail_den > 0 else 1.0,
                    encode_error=error / count, detail=detail)
