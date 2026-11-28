"""Progression d'un encodage (pourcentage, fps, ETA) déduite des lignes des outils — partagée GUI / API, sans Qt.

La GUI (``ui/main_window.py``) affiche ces instantanés (barre, libellés traduits, minuteur de réévaluation) ;
l'API les publie en événements ``progress``. Aucune sérialisation interne : l'appelant appelle le suivi depuis un
seul fil à la fois (GUI : fil Qt ; API : verrou couvrant lecture de la ligne, calcul et publication).

Deux sources :
- encodage d'une seule commande : lignes FFmpeg / NVEncC / muxiveo-rife, normalisées par le backend
  (``EncodeWorkflow.parse_progress``) ou, à défaut, lues ici (``time=``, ``out_time=``, ``frame=``, ``fps=``) ;
- encodage multi-pistes : lignes internes ``__MRE_PROGRESS__ {json}`` (``kind`` = ``encode_ffmpeg``, ``label`` =
  ``ffmpeg-video-N[-passP]``), une progression par piste / passe ; la piste affichée est celle qui finira le plus tard.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

#: Préfixe des lignes internes de progression multi-pistes (``core/workflows/encode/runtime_helpers.py``).
ENCODE_INTERNAL_PROGRESS_PREFIX = "__MRE_PROGRESS__ "

_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")
FPS_RE = re.compile(r"\bfps=\s*([\d.]+)")
_OUT_TIME_RE = re.compile(r"\bout_time=(\d+):(\d+):(\d+(?:\.\d+)?)")
_OUT_TIME_TICKS_RE = re.compile(r"\bout_time_(?:ms|us)=(\d+)")
FRAME_RE = re.compile(r"\bframe=\s*(\d+)")
_MULTI_ENCODE_LABEL_RE = re.compile(r"^ffmpeg-video-(\d+)(?:-pass(\d+))?$")
#: En-tête FFmpeg de la première entrée (``  Duration: 01:23:45.67, start: …``) : durée de référence d'un remux.
_INPUT_DURATION_RE = re.compile(r"^\s*Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_PERCENT_RE = re.compile(r"(\d+)%")
#: Pourcentage des outils lus sur PTY (dovi_tool, hdr10plus_tool) relayé en ligne (``core/runner.py``).
TOOL_PERCENT_PREFIX = "__MRE_PCT__ "


def _hms_to_seconds(match: re.Match[str]) -> float:
    return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))


def ffmpeg_progress_seconds(line: str) -> float | None:
    """
    Retourne le temps média écoulé pour une ligne de progression FFmpeg.

    Gère à la fois :
      - les stats texte historiques (`time=...`)
      - la sortie machine de `-progress pipe:1` (`out_time=...`)
      - le fallback numérique `out_time_ms` / `out_time_us`

    Note : malgré son nom, `out_time_ms` est souvent exprimé en microsecondes
    par FFmpeg ; on traite donc ces compteurs comme des microsecondes.
    """
    match = _TIME_RE.search(line)
    if match:
        return _hms_to_seconds(match)

    match = _OUT_TIME_RE.search(line)
    if match:
        return _hms_to_seconds(match)

    match = _OUT_TIME_TICKS_RE.search(line)
    if match:
        return int(match.group(1)) / 1_000_000.0

    return None


def fmt_progress_percent(percent: float | int) -> str:
    """Pourcentage affiché : entier s'il l'est à 0,05 près, sinon une décimale."""
    value = float(percent)
    if abs(value - round(value)) < 0.05:
        return f"{int(round(value))}%"
    return f"{value:.1f}%"


class EtaTracker:
    """
    Lisse la vitesse de progression pour produire un ETA stable.

    Calcule une vitesse instantanée (delta progrès / delta wall-time) entre
    échantillons consécutifs, puis applique une moyenne exponentielle (EWMA).
    Cela évite l'effet "ETA énorme au début, qui chute brutalement à la fin"
    causé par une vitesse moyenne calculée depuis le début (qui inclut
    l'init lente de ffmpeg).

    Le warmup ignore les premières mesures tant que peu de progrès média a
    été produit — pendant ce temps, ``eta()`` retourne ``None``.
    """

    # Why: alpha=0.3 → la vitesse instantanée pèse 30 %, l'historique 70 %.
    # Assez réactif pour suivre une vraie variation, assez lissé pour ne pas
    # osciller à chaque ligne de ffmpeg.
    _ALPHA = 0.3
    # Why: en-dessous, le delta est trop court pour donner une vitesse fiable
    # (jitter de l'I/O, ligne ffmpeg agrégée). On accumule jusqu'à ce seuil.
    _MIN_DELTA_WALL = 0.5
    _MIN_DELTA_PROGRESS = 0.0

    def __init__(self, *, warmup_progress: float = 1.0) -> None:
        self._warmup = float(warmup_progress)
        self._smoothed_speed: float | None = None
        self._last_progress: float | None = None
        self._last_wall: float | None = None
        self._initial_progress: float | None = None

    def reset(self) -> None:
        self._smoothed_speed = None
        self._last_progress = None
        self._last_wall = None
        self._initial_progress = None

    def update(self, progress: float, wall_time: float) -> None:
        """Enregistre un nouvel échantillon (progrès média et wall-time absolu)."""
        if progress < 0 or wall_time < 0:
            return
        if self._initial_progress is None:
            self._initial_progress = progress
            self._last_progress = progress
            self._last_wall = wall_time
            return
        if self._last_progress is None or self._last_wall is None:
            self._last_progress = progress
            self._last_wall = wall_time
            return

        # Pendant la phase de warmup (init ffmpeg), on ne pollue pas l'EWMA
        # avec des vitesses non représentatives. On suit juste la position
        # pour pouvoir mesurer le delta dès la sortie de warmup.
        if (progress - self._initial_progress) < self._warmup:
            self._last_progress = progress
            self._last_wall = wall_time
            return

        delta_progress = progress - self._last_progress
        delta_wall = wall_time - self._last_wall
        if delta_wall < self._MIN_DELTA_WALL or delta_progress <= self._MIN_DELTA_PROGRESS:
            # Échantillon trop court : on attend, sans écraser le précédent.
            return

        instant_speed = delta_progress / delta_wall
        if instant_speed <= 0:
            return

        if self._smoothed_speed is None:
            self._smoothed_speed = instant_speed
        else:
            self._smoothed_speed = (
                self._ALPHA * instant_speed + (1.0 - self._ALPHA) * self._smoothed_speed
            )
        self._last_progress = progress
        self._last_wall = wall_time

    def eta(self, total: float, current: float) -> float | None:
        """Retourne l'ETA en secondes, ou ``None`` si indéterminé (warmup)."""
        if total <= 0 or current < 0 or current >= total:
            return None
        if self._smoothed_speed is None or self._smoothed_speed <= 0:
            return None
        return (total - current) / self._smoothed_speed

    def eta_from_speed(self, total: float, current: float, speed: float) -> float | None:
        """ETA basé sur une vitesse externe (ex: fps ffmpeg) — sans EWMA interne."""
        if total <= 0 or current < 0 or current >= total or speed <= 0:
            return None
        return (total - current) / speed


# ---------------------------------------------------------------------------
# Lignes internes multi-pistes
# ---------------------------------------------------------------------------

def parse_encode_internal_progress(line: str) -> dict[str, object] | None:
    """Charge utile d'une ligne interne ``__MRE_PROGRESS__ {json}`` ; ``None`` pour toute autre ligne."""
    if not line.startswith(ENCODE_INTERNAL_PROGRESS_PREFIX):
        return None
    payload = line[len(ENCODE_INTERNAL_PROGRESS_PREFIX):].strip()
    if not payload:
        return None
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def parse_multi_encode_label(label: str) -> tuple[int, int | None, int] | None:
    """``ffmpeg-video-N[-passP]`` → (rang de la piste, passe ou ``None``, nombre de passes)."""
    match = _MULTI_ENCODE_LABEL_RE.match(str(label).strip())
    if match is None:
        return None
    order = int(match.group(1))
    pass_index_raw = match.group(2)
    if pass_index_raw is None:
        return order, None, 1
    pass_index = int(pass_index_raw)
    return order, pass_index, 2


def _state_float(state: dict[str, object], key: str, default: float = 0.0) -> float:
    value = state.get(key, default)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return default
    return default


def _state_int(state: dict[str, object], key: str, default: int) -> int:
    value = state.get(key)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def multi_encode_remaining_seconds(state: dict[str, object], now: float) -> float | None:
    """
    ETA d'un encode multi-stream basé sur la vitesse lissée (EWMA), pas sur
    la moyenne depuis le début. Évite l'ETA gonflé pendant l'init de ffmpeg.
    """
    tracker = state.get("eta_tracker")
    if not isinstance(tracker, EtaTracker):
        tracker = EtaTracker(warmup_progress=1.0)
        state["eta_tracker"] = tracker

    duration_s = state.get("duration_s")
    elapsed_video = state.get("elapsed_video")
    if (
        isinstance(duration_s, (int, float))
        and duration_s > 0
        and isinstance(elapsed_video, (int, float))
        and elapsed_video >= 0
    ):
        tracker.update(float(elapsed_video), now)
        return tracker.eta(float(duration_s), float(elapsed_video))

    total_frames = state.get("total_frames")
    frame = state.get("frame")
    fps_value = state.get("fps")
    if (
        isinstance(total_frames, int)
        and total_frames > 0
        and isinstance(frame, int)
        and frame > 0
        and total_frames > frame
    ):
        # Si ffmpeg a déjà fourni un fps, on l'utilise tel quel — c'est le
        # même chiffre que l'utilisateur voit dans le label, donc cohérent.
        if isinstance(fps_value, (int, float)) and fps_value > 0:
            return tracker.eta_from_speed(float(total_frames), float(frame), float(fps_value))
        tracker.update(float(frame), now)
        return tracker.eta(float(total_frames), float(frame))
    return None


def select_multi_encode_label(
    states: dict[str, dict[str, object]],
    active_label: str | None,
    now: float,
) -> str | None:
    """Piste / passe à afficher : celle dont le temps restant connu est le plus long (puis la plus récente)."""
    candidates = [
        (label, state)
        for label, state in states.items()
        if not bool(state.get("done", False))
    ]
    if not candidates:
        return None

    scored: list[tuple[int, float, float, int, str]] = []
    for label, state in candidates:
        remaining_s = multi_encode_remaining_seconds(state, now)
        known_remaining = 1 if remaining_s is not None else 0
        scored.append(
            (
                known_remaining,
                float(remaining_s if remaining_s is not None else -1.0),
                _state_float(state, "last_update", 0.0),
                1 if label == active_label else 0,
                label,
            )
        )
    scored.sort(reverse=True)
    return scored[0][4]


# ---------------------------------------------------------------------------
# Cibles de progression
# ---------------------------------------------------------------------------

def progress_target(info: Any, video_track: Any, fallback_duration_s: Any, source_name: str) -> dict[str, object]:
    """Durée et nombre d'images de référence d'une piste vidéo encodée (piste, sinon fichier, sinon configuration)."""
    duration_s: Any
    total_frames: int | None = None
    if info is not None:
        duration_s = (
            getattr(video_track, "duration_s", None)
            or getattr(info, "duration_s", None)
            or fallback_duration_s
        )
        frames_obj = getattr(info, "frame_count", None)
        if isinstance(frames_obj, int) and frames_obj > 0:
            total_frames = frames_obj
    else:
        duration_s = fallback_duration_s
    return {
        "duration_s": duration_s if isinstance(duration_s, (int, float)) and duration_s > 0 else None,
        "total_frames": total_frames,
        "source_name": source_name,
    }


def progress_targets(config: Any, info_for: Callable[[Path], Any]) -> dict[int, dict[str, object]]:
    """Cibles par piste vidéo routée (clé : rang 1-based dans ``config.video_tracks``) d'après les inspections.

    ``info_for(chemin)`` : fiche d'inspection de la source (``None`` si indisponible).
    """
    from core.workflows.encode.planning.track_settings import routing_video_tracks

    targets: dict[int, dict[str, object]] = {}
    for index, video in enumerate(routing_video_tracks(config), start=1):
        source_path = Path(getattr(video, "source_path", None) or config.source)
        stream_index = int(getattr(video, "stream_index", 0) or 0)
        info = info_for(source_path)
        track = None
        if info is not None:
            track = next((item for item in info.video_tracks if item.index == stream_index), info.primary_video)
        targets[index] = progress_target(info, track, getattr(config, "duration_s", None), source_path.name)
    return targets


# ---------------------------------------------------------------------------
# Suivi
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProgressSnapshot:
    """Progression à afficher ou publier (``None`` : inconnu)."""

    pct: int | None = None  # barre : 0..99 (100 réservé à la fin réelle)
    pct_label: str = ""  # texte du pourcentage (une décimale si le backend la fournit)
    fps: float | None = None
    frame: int | None = None
    eta_s: float | None = None
    track: int | None = None  # multi-pistes : rang de la piste affichée
    track_count: int | None = None
    pass_index: int | None = None
    pass_count: int | None = None
    stage: str | None = None  # étape annexe identifiée : « assembly » (mux Matroska), « metadata » (outils DV/HDR10+)


def stage_percent(line: str) -> ProgressSnapshot | None:
    """Pourcentage d'une étape annexe, comme la barre de la GUI : assemblage Matroska natif, outils DV / HDR10+."""
    from core.matroska.progress import native_mux_progress_label

    if native_mux_progress_label(line) is not None:
        match = _PERCENT_RE.search(line)
        if match is None:
            return None
        pct = max(0, min(100, int(match.group(1))))
        return ProgressSnapshot(pct=pct, pct_label=f"{pct}%", stage="assembly")
    if line.startswith(TOOL_PERCENT_PREFIX):
        try:
            pct = max(0, min(100, int(line[len(TOOL_PERCENT_PREFIX):].strip())))
        except ValueError:
            return None
        return ProgressSnapshot(pct=pct, pct_label=f"{pct}%", stage="metadata")
    return None


class TimelineProgress:
    """Progression d'une commande de remux : pourcentage du muxer natif, ou temps écrit / durée de la première entrée.

    La durée vient de l'en-tête FFmpeg de chaque commande (``Duration:`` de son entrée 0) quand elle n'est pas
    fournie : aucune inspection supplémentaire. Chaque commande (ligne ``$ …``, réécritures audio préalables puis
    remux final) repart de zéro : sa durée et son ETA lui sont propres.
    """

    def __init__(self, duration_s: float | None = None) -> None:
        self._fixed_duration = duration_s
        self.duration_s = duration_s
        self._eta = EtaTracker(warmup_progress=1.0)

    def command_started(self) -> None:
        """Nouvelle commande : durée relue dans son en-tête (sauf durée fournie), ETA remis à zéro."""
        self.duration_s = self._fixed_duration
        self._eta.reset()

    def on_line(self, line: str, now: float) -> ProgressSnapshot | None:
        if line.startswith("$ "):
            self.command_started()
            return None
        snapshot = stage_percent(line)
        if snapshot is not None:
            return snapshot
        if self.duration_s is None:
            match = _INPUT_DURATION_RE.match(line)
            if match is not None:
                self.duration_s = _hms_to_seconds(match) or None
                return None
        elapsed = ffmpeg_progress_seconds(line)
        if elapsed is None or not self.duration_s:
            return None
        pct = min(99, int(elapsed / self.duration_s * 100))
        self._eta.update(elapsed, now)
        return ProgressSnapshot(pct=pct, pct_label=f"{pct}%", eta_s=self._eta.eta(self.duration_s, elapsed))


@dataclass(frozen=True)
class LineOutcome:
    """Ligne FFmpeg lue sans normalisation du backend : ``handled`` (rien d'autre à en faire), ``elapsed_seen``."""

    handled: bool
    elapsed_seen: bool = False
    snapshot: ProgressSnapshot | None = None


class EncodeProgressTracker:
    """État de progression d'un encodage (une commande, ou plusieurs pistes / passes en parallèle).

    ``duration_s`` / ``total_frames`` : référence d'un encodage à une commande ; ``targets`` : cibles par piste
    (``progress_targets``) des encodages multi-pistes. ``now`` : horloge monotone fournie par l'appelant.
    """

    def __init__(self, *, duration_s: float | None = None, total_frames: int | None = None,
                 targets: dict[int, dict[str, object]] | None = None) -> None:
        self.duration_s = duration_s
        self.total_frames = total_frames
        self.targets: dict[int, dict[str, object]] = dict(targets or {})
        self.fps: float | None = None
        self.frame: int | None = None
        self._eta_video = EtaTracker(warmup_progress=1.0)
        self._eta_frame = EtaTracker(warmup_progress=24.0)
        self.states: dict[str, dict[str, object]] = {}
        self.active_label: str | None = None

    # --- Encodage à une commande -------------------------------------------------------

    def command_started(self) -> None:
        """Nouvelle commande (ligne ``$ …``) ou nouvelle étape : compteurs et ETA repartent de zéro."""
        self.fps = None
        self.frame = None
        self._eta_video.reset()
        self._eta_frame.reset()

    def stage_changed(self) -> None:
        """Message d'étape de l'encodage : fps et image courante oubliés (ETA conservés)."""
        self.fps = None
        self.frame = None

    def on_event(self, event: Any, now: float) -> ProgressSnapshot:
        """Ligne normalisée par le backend (``ProgressEvent`` : ``percent``, ``elapsed_seconds``, ``frame``…)."""
        if event.fps is not None:
            self.fps = event.fps
        if event.frame is not None:
            self.frame = event.frame

        pct: int | None = None
        pct_label = ""
        if event.percent is not None:
            pct = max(0, min(99, int(event.percent)))
            pct_label = fmt_progress_percent(event.percent)
        elif event.elapsed_seconds is not None:
            if self.duration_s and self.duration_s > 0:
                pct = min(99, int(event.elapsed_seconds / self.duration_s * 100))
                pct_label = f"{pct}%"
        elif event.frame is not None:
            if self.total_frames and self.total_frames > 0:
                pct = min(99, int(event.frame / self.total_frames * 100))
                pct_label = f"{pct}%"

        eta_s = event.eta_seconds
        if eta_s is None and event.elapsed_seconds is not None:
            if self.duration_s and self.duration_s > 0:
                self._eta_video.update(event.elapsed_seconds, now)
                eta_s = self._eta_video.eta(self.duration_s, event.elapsed_seconds)
        if eta_s is None and event.frame is not None:
            total = self.total_frames
            if total and total > 0:
                if event.fps is not None and event.fps > 0 and total > event.frame:
                    eta_s = self._eta_frame.eta_from_speed(float(total), float(event.frame), float(event.fps))
                else:
                    self._eta_frame.update(float(event.frame), now)
                    eta_s = self._eta_frame.eta(float(total), float(event.frame))
        return ProgressSnapshot(pct=pct, pct_label=pct_label, fps=event.fps, frame=event.frame, eta_s=eta_s)

    def on_line(self, line: str, now: float) -> LineOutcome:
        """Ligne FFmpeg brute (backend sans normalisation) : ``time=`` / ``out_time=`` sinon ``frame=``."""
        fps_m = FPS_RE.search(line)
        if fps_m:
            try:
                self.fps = float(fps_m.group(1))
            except ValueError:
                self.fps = None
        frame_m = FRAME_RE.search(line)
        if frame_m:
            try:
                self.frame = int(frame_m.group(1))
            except ValueError:
                self.frame = None
        elapsed_video = ffmpeg_progress_seconds(line)
        if elapsed_video is not None:
            snapshot = None
            if self.duration_s and self.duration_s > 0:
                pct = min(99, int(elapsed_video / self.duration_s * 100))
                self._eta_video.update(elapsed_video, now)
                eta_s = self._eta_video.eta(self.duration_s, elapsed_video)
                snapshot = ProgressSnapshot(pct=pct, pct_label=f"{pct}%", fps=self.fps, frame=self.frame, eta_s=eta_s)
            return LineOutcome(handled=True, elapsed_seen=True, snapshot=snapshot)
        if frame_m is not None and self.frame is not None:
            snapshot = None
            total = self.total_frames
            if total and total > 0:
                pct = min(99, int(self.frame / total * 100))
                if self.fps is not None and self.fps > 0 and total > self.frame:
                    eta_s = self._eta_frame.eta_from_speed(float(total), float(self.frame), float(self.fps))
                else:
                    # Repli : EWMA sur la vitesse instantanée mesurée.
                    self._eta_frame.update(float(self.frame), now)
                    eta_s = self._eta_frame.eta(float(total), float(self.frame))
                snapshot = ProgressSnapshot(pct=pct, pct_label=f"{pct}%", fps=self.fps, frame=self.frame, eta_s=eta_s)
            return LineOutcome(handled=True, snapshot=snapshot)
        return LineOutcome(handled=False)

    # --- Encodage multi-pistes ---------------------------------------------------------

    def state(self, label: str, now: float) -> dict[str, object]:
        """État d'une piste / passe (créé à sa première ligne, avec sa cible)."""
        state = self.states.get(label)
        if state is not None:
            return state
        parsed = parse_multi_encode_label(label)
        order = 0
        pass_index: int | None = None
        pass_count = 1
        if parsed is not None:
            order, pass_index, pass_count = parsed
        target = self.targets.get(order, {})
        state = {
            "label": label,
            "order": order,
            "pass_index": pass_index,
            "pass_count": pass_count,
            "duration_s": target.get("duration_s"),
            "total_frames": target.get("total_frames"),
            "source_name": target.get("source_name"),
            "started_at": now,
            "last_update": 0.0,
            "fps": None,
            "frame": None,
            "elapsed_video": None,
            "done": False,
        }
        self.states[label] = state
        return state

    def internal_line(self, label: str, raw_line: str, now: float) -> None:
        """Ligne de progression d'une piste / passe (``fps=``, ``frame=``, ``out_time=``)."""
        state = self.state(label, now)
        fps_m = FPS_RE.search(raw_line)
        if fps_m:
            try:
                state["fps"] = float(fps_m.group(1))
            except ValueError:
                state["fps"] = None
        frame_m = FRAME_RE.search(raw_line)
        if frame_m:
            try:
                state["frame"] = int(frame_m.group(1))
            except ValueError:
                state["frame"] = None
        elapsed_video = ffmpeg_progress_seconds(raw_line)
        if elapsed_video is not None:
            state["elapsed_video"] = elapsed_video
        state["last_update"] = now

    def internal_done(self, label: str, now: float) -> None:
        """Fin d'une piste / passe."""
        state = self.state(label, now)
        state["done"] = True
        state["last_update"] = now

    def multi_snapshot(self, now: float) -> ProgressSnapshot | None:
        """Piste / passe affichée (temps restant le plus long) et sa progression ; ``None`` si toutes finies."""
        active_label = select_multi_encode_label(self.states, self.active_label, now)
        self.active_label = active_label
        if active_label is None:
            return None
        state = self.states.get(active_label)
        if state is None:
            return None
        order = _state_int(state, "order", 0)
        pass_count = _state_int(state, "pass_count", 1)
        pass_index = state.get("pass_index")

        pct: int | None = None
        duration_s = state.get("duration_s")
        elapsed_video = state.get("elapsed_video")
        if isinstance(duration_s, (int, float)) and duration_s > 0 and isinstance(elapsed_video, (int, float)):
            pct = min(99, int(float(elapsed_video) / float(duration_s) * 100))
        else:
            total_frames = state.get("total_frames")
            frame = state.get("frame")
            if isinstance(total_frames, int) and total_frames > 0 and isinstance(frame, int):
                pct = min(99, int(frame / total_frames * 100))
        fps_value = state.get("fps")
        frame_value = state.get("frame")
        return ProgressSnapshot(
            pct=pct,
            pct_label=f"{pct}%" if pct is not None else "",
            fps=float(fps_value) if isinstance(fps_value, (int, float)) else None,
            frame=frame_value if isinstance(frame_value, int) else None,
            eta_s=multi_encode_remaining_seconds(state, now),
            track=order or None,
            track_count=max(self.targets) if self.targets else None,
            pass_index=pass_index if isinstance(pass_index, int) else None,
            pass_count=pass_count,
        )


__all__ = [
    "ENCODE_INTERNAL_PROGRESS_PREFIX",
    "EncodeProgressTracker",
    "EtaTracker",
    "FPS_RE",
    "FRAME_RE",
    "LineOutcome",
    "ProgressSnapshot",
    "TOOL_PERCENT_PREFIX",
    "TimelineProgress",
    "ffmpeg_progress_seconds",
    "fmt_progress_percent",
    "multi_encode_remaining_seconds",
    "parse_encode_internal_progress",
    "parse_multi_encode_label",
    "progress_target",
    "progress_targets",
    "select_multi_encode_label",
    "stage_percent",
]
