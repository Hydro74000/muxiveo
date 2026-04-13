"""
core/runner.py — Exécution d'outils externes et parallélisation.

Classes publiques :
    ToolChecker   — vérifie la disponibilité des outils via shutil.which
    TaskSignals   — signaux Qt émis par chaque tâche (QObject standalone)
    ToolRunner    — lance des commandes subprocess, séquentielles ou parallèles

Conventions :
    - Jamais shell=True
    - pathlib.Path pour tous les chemins
    - ThreadPoolExecutor pour les tâches I/O parallèles
    - Signaux Qt thread-safe : connexion via Qt.ConnectionType.QueuedConnection
      depuis les workers vers l'UI
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Sequence

from PySide6.QtCore import QObject, Signal
from core.subprocess_utils import decode_subprocess_output, subprocess_windows_no_window_kwargs


# ---------------------------------------------------------------------------
# ToolChecker
# ---------------------------------------------------------------------------

#: Outils requis par défaut pour le projet
DEFAULT_TOOLS: tuple[str, ...] = (
    "dovi_tool",
    "hdr10plus_tool",
    "ffmpeg",
    "mediainfo",
)


class ToolChecker:
    """
    Vérifie la disponibilité des outils externes dans le PATH.

    Usage :
        checker = ToolChecker()
        checker.check_all()          # dict[str, bool]
        checker.missing()            # list[str]  — outils absents
        checker.available("ffmpeg")  # bool
        checker.require(["ffmpeg", "dovi_tool"])  # lève ToolNotFoundError si manquant
    """

    def __init__(self, tools: Sequence[str] = DEFAULT_TOOLS) -> None:
        self._tools: tuple[str, ...] = tuple(tools)

    # ------------------------------------------------------------------
    # API publique
    # ------------------------------------------------------------------

    def check_all(self) -> dict[str, bool]:
        """Retourne un dict {nom_outil: disponible} pour tous les outils."""
        return {tool: self.available(tool) for tool in self._tools}

    def available(self, tool: str) -> bool:
        """Retourne True si l'outil est trouvable dans le PATH."""
        return shutil.which(tool) is not None

    def path_of(self, tool: str) -> Path | None:
        """Retourne le Path absolu de l'outil, ou None s'il est absent."""
        found = shutil.which(tool)
        return Path(found) if found else None

    def missing(self) -> list[str]:
        """Retourne la liste des outils absents du PATH."""
        return [t for t in self._tools if not self.available(t)]

    def require(self, tools: Sequence[str]) -> None:
        """
        Vérifie que tous les outils listés sont disponibles.

        Lève :
            ToolNotFoundError : si un ou plusieurs outils sont absents.
        """
        absent = [t for t in tools if not self.available(t)]
        if absent:
            raise ToolNotFoundError(absent)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ToolNotFoundError(RuntimeError):
    """Un ou plusieurs outils requis sont absents du PATH."""

    def __init__(self, tools: list[str]) -> None:
        self.tools = tools
        super().__init__(
            f"Outil(s) introuvable(s) dans PATH : {', '.join(tools)}"
        )


class TaskCancelledError(BaseException):
    """L'opération a été annulée par l'utilisateur."""


class CommandError(RuntimeError):
    """Une commande externe s'est terminée avec un code de retour non nul."""

    def __init__(
        self,
        cmd: list[str],
        returncode: int,
        stderr: str,
    ) -> None:
        self.cmd = cmd
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(
            f"Commande échouée (code {returncode}) : {' '.join(cmd)}\n{stderr}"
        )


# ---------------------------------------------------------------------------
# TaskSignals — signaux Qt pour une tâche individuelle
# ---------------------------------------------------------------------------

class TaskSignals(QObject):
    """
    Signaux Qt émis par une tâche ToolRunner.

    Signaux :
        progress(message: str)
            Émis à chaque ligne de sortie de la commande.

        finished(result: str)
            Émis quand la tâche se termine avec succès.

        failed(message: str, exception: Exception)
            Émis si la commande échoue ou lève une exception.

        cancelled()
            Émis si cancel() a été appelé et que la tâche s'est arrêtée.

    Annulation :
        signals.cancel()   — tue le(s) processus actif(s) immédiatement.
        Connexion thread-safe recommandée :
            signals.progress.connect(slot, Qt.ConnectionType.QueuedConnection)
    """

    progress  = Signal(str)
    finished  = Signal(str)
    failed    = Signal(str, object)   # (message_court, exception)
    cancelled = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._cancel_event = threading.Event()
        self._active_procs: list[subprocess.Popen] = []
        self._procs_lock   = threading.Lock()

    # ------------------------------------------------------------------
    # API publique
    # ------------------------------------------------------------------

    def cancel(self) -> None:
        """Annule l'opération : marque l'événement et tue les processus actifs."""
        self._cancel_event.set()
        with self._procs_lock:
            for proc in list(self._active_procs):
                try:
                    proc.kill()
                except OSError:
                    pass

    # ------------------------------------------------------------------
    # Usage interne par ToolRunner._run_cmd
    # ------------------------------------------------------------------

    def _register_proc(self, proc: subprocess.Popen) -> None:
        with self._procs_lock:
            self._active_procs.append(proc)

    def _unregister_proc(self, proc: subprocess.Popen) -> None:
        with self._procs_lock:
            try:
                self._active_procs.remove(proc)
            except ValueError:
                pass


# ---------------------------------------------------------------------------
# ToolRunner
# ---------------------------------------------------------------------------

class ToolRunner(QObject):
    """
    Lance des commandes externes, séquentiellement ou en parallèle.

    Chaque appel retourne un objet `TaskSignals` auquel l'appelant peut
    connecter ses slots avant que la tâche ne démarre.

    Usage séquentiel :
        runner = ToolRunner()
        sig = runner.run(["ffmpeg", "-i", "in.mkv", "out.mkv"])
        sig.finished.connect(on_done)
        sig.failed.connect(on_error)

    Usage parallèle :
        tasks = [
            ["dovi_tool", "extract-rpu", "-i", "film2.mkv", "-o", "rpu.bin"],
            ["hdr10plus_tool", "extract", "-i", "film2.mkv", "-o", "meta.json"],
        ]
        sig = runner.run_parallel(tasks, label="extraction")
        sig.progress.connect(on_progress)
        sig.finished.connect(on_done)

    Notes :
        - Toutes les commandes sont lancées dans des threads secondaires.
        - L'objet ToolRunner lui-même vit dans le thread principal.
        - Les signaux traversent la frontière de threads via QueuedConnection.
        - max_workers contrôle le parallélisme du ThreadPoolExecutor.
    """

    def __init__(
        self,
        max_workers: int = 4,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._max_workers = max_workers

    # ------------------------------------------------------------------
    # Exécution séquentielle
    # ------------------------------------------------------------------

    def run(
        self,
        cmd: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        label: str = "",
        on_progress: Callable[[str], None] | None = None,
    ) -> TaskSignals:
        """
        Lance une commande dans un thread secondaire.

        Args :
            cmd         : commande sous forme de liste (jamais shell=True)
            cwd         : dossier de travail du sous-processus
            env         : variables d'environnement (None = héritage)
            label       : préfixe pour les messages de progression
            on_progress : callback optionnel appelé sur chaque ligne stdout

        Returns :
            TaskSignals — connecter les slots avant que la tâche ne démarre.
        """
        signals = TaskSignals()
        executor = ThreadPoolExecutor(max_workers=1)

        def _task() -> None:
            try:
                output = self._run_cmd(
                    cmd,
                    cwd=cwd,
                    env=env,
                    label=label or cmd[0],
                    progress_cb=lambda line: (
                        signals.progress.emit(line)
                        or (on_progress(line) if on_progress else None)
                    ),
                    signals=signals,
                )
                signals.finished.emit(output)
            except TaskCancelledError:
                signals.cancelled.emit()
            except Exception as exc:
                signals.failed.emit(str(exc), exc)
            finally:
                executor.shutdown(wait=False)

        executor.submit(_task)
        return signals

    # ------------------------------------------------------------------
    # Exécution parallèle
    # ------------------------------------------------------------------

    def run_parallel(
        self,
        tasks: list[list[str]],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        label: str = "parallel",
    ) -> TaskSignals:
        """
        Lance plusieurs commandes en parallèle dans un ThreadPoolExecutor.

        Toutes les tâches sont indépendantes. Le signal `finished` est émis
        quand TOUTES les tâches ont réussi. Le signal `failed` est émis dès
        qu'une tâche échoue (les autres continuent jusqu'à leur fin naturelle).

        Args :
            tasks  : liste de commandes, chacune étant une liste de strings
            cwd    : dossier de travail commun
            env    : variables d'environnement communes
            label  : préfixe pour les messages de progression

        Returns :
            TaskSignals — connecter les slots avant que la tâche ne démarre.
        """
        signals = TaskSignals()
        executor = ThreadPoolExecutor(max_workers=self._max_workers)

        def _parallel() -> None:
            t_start = time.monotonic()
            errors: list[str] = []
            outputs: list[str] = []

            # Cas liste vide : rien à exécuter, succès immédiat
            if not tasks:
                signals.finished.emit("0 tâche(s) terminée(s) en 0.0s")
                executor.shutdown(wait=False)
                return

            future_to_label: dict[Future[str], str] = {}
            for i, cmd in enumerate(tasks):
                task_label = f"{label}[{i}] {cmd[0]}"
                fut = executor.submit(
                    self._run_cmd,
                    cmd,
                    cwd=cwd,
                    env=env,
                    label=task_label,
                    progress_cb=lambda line, lbl=task_label: signals.progress.emit(
                        f"[{lbl}] {line}"
                    ),
                    signals=signals,
                )
                future_to_label[fut] = task_label

            cancelled = False
            for future in as_completed(future_to_label):
                lbl = future_to_label[future]
                try:
                    out = future.result()
                    outputs.append(out)
                    signals.progress.emit(f"[{lbl}] terminé")
                except TaskCancelledError:
                    cancelled = True
                except Exception as exc:
                    errors.append(f"[{lbl}] {exc}")

            executor.shutdown(wait=False)
            elapsed = time.monotonic() - t_start

            if cancelled:
                signals.cancelled.emit()
            elif errors:
                msg = f"Échec de {len(errors)}/{len(tasks)} tâche(s) en {elapsed:.1f}s :\n" + "\n".join(errors)
                signals.failed.emit(msg, RuntimeError(msg))
            else:
                summary = f"{len(tasks)} tâche(s) terminée(s) en {elapsed:.1f}s"
                signals.finished.emit(summary)

        outer = ThreadPoolExecutor(max_workers=1)
        outer.submit(_parallel)
        outer.shutdown(wait=False)
        return signals

    # ------------------------------------------------------------------
    # Commande bas niveau
    # ------------------------------------------------------------------

    def _run_cmd(
        self,
        cmd: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        label: str = "",
        progress_cb: Callable[[str], None] | None = None,
        signals: TaskSignals | None = None,
    ) -> str:
        """
        Lance une commande subprocess et retourne stdout+stderr combinés.

        Chaque ligne de sortie est transmise à progress_cb si fourni.
        Lève CommandError si le code de retour est non nul.
        Lève TaskCancelledError si signals.cancel() est appelé pendant l'exécution.
        """
        import os

        proc_env = {**os.environ, **(env or {})} if env else None

        if progress_cb:
            progress_cb("$ " + " ".join(cmd))

        with subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            # Mode binaire : on gère \r et \r\n nous-mêmes
            cwd=str(cwd) if cwd else None,
            env=proc_env,
            # shell=True JAMAIS
            **subprocess_windows_no_window_kwargs(),
        ) as proc:
            if signals is not None:
                signals._register_proc(proc)
            try:
                lines: list[str] = []
                assert proc.stdout is not None

                buf = b""
                while chunk := proc.stdout.read(256):
                    # Annulation : le processus a été tué par cancel(), read() retourne b""
                    # ou on le tue ici si le signal arrive entre deux lectures.
                    if signals is not None and signals._cancel_event.is_set():
                        proc.kill()
                        raise TaskCancelledError()
                    # Normalise \r\n et \r solitaire en \n pour un split uniforme
                    buf += chunk.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
                    *complete, buf = buf.split(b"\n")
                    for raw in complete:
                        stripped = decode_subprocess_output(raw).rstrip()
                        lines.append(stripped)
                        if progress_cb and stripped:
                            progress_cb(stripped)

                # Vide le tampon résiduel (dernière ligne sans \n terminal)
                if buf.strip():
                    stripped = decode_subprocess_output(buf.strip())
                    lines.append(stripped)
                    if progress_cb and stripped:
                        progress_cb(stripped)

                proc.wait()

                # Si le processus a été tué par cancel() et qu'on sort de la boucle
                # parce que stdout s'est fermé (b"" retourné) avant la vérification
                if signals is not None and signals._cancel_event.is_set():
                    raise TaskCancelledError()

                output = "\n".join(lines)

                if proc.returncode != 0:
                    raise CommandError(
                        cmd=cmd,
                        returncode=proc.returncode,
                        stderr=output[-2000:],
                    )

                return output
            finally:
                if signals is not None:
                    signals._unregister_proc(proc)
