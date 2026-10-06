"""
Helpers de décodage texte pour les outils externes.

Sous Windows, plusieurs outils émettent une sortie UTF-8, mais
`subprocess.run(..., text=True)` la décode sinon avec la page de code locale
du système. On force donc l'UTF-8 pour éviter le mojibake du type `FranÃ§ais`.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any


_TOOL_TEXT_ENCODING = "utf-8"
_TOOL_TEXT_ERRORS = "replace"


def subprocess_windows_no_window_kwargs(*, include_stdin: bool = True) -> dict[str, Any]:
    """
    Return subprocess kwargs that prevent a console window from flashing on Windows.

    Inclut systématiquement stdin=DEVNULL : sous Linux, ffmpeg/dovi_tool/etc.
    héritent sinon du tty parent et peuvent altérer ses flags termios (echo
    désactivé, mode raw) — ce qui casse le terminal après fermeture de l'app.
    Sous Windows, évite aussi WinError 50 en mode GUI sans console.

    Safe to pass to both subprocess.run() and subprocess.Popen().
    Set include_stdin=False when the caller provides their own stdin (e.g. pipe).
    """
    kwargs: dict[str, Any] = {}
    if include_stdin:
        kwargs["stdin"] = subprocess.DEVNULL

    if sys.platform != "win32":
        return kwargs

    create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if create_no_window:
        kwargs["creationflags"] = create_no_window

    startupinfo_cls = getattr(subprocess, "STARTUPINFO", None)
    if startupinfo_cls is not None:
        startupinfo = startupinfo_cls()
        startf_use_showwindow = getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
        if startf_use_showwindow:
            startupinfo.dwFlags |= startf_use_showwindow
        startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
        kwargs["startupinfo"] = startupinfo

    return kwargs


def subprocess_text_kwargs() -> dict[str, Any]:
    """
    Retourne les kwargs à injecter dans subprocess.run/Popen pour lire du texte.

    Sous Windows, on force l'UTF-8 ; ailleurs, on conserve le comportement
    standard de Python pour limiter le périmètre du changement.
    """
    # stdin=DEVNULL est inclus systématiquement (cf. subprocess_windows_no_window_kwargs)
    # pour empêcher les outils externes d'altérer le tty parent.
    kwargs: dict[str, Any] = {"text": True}
    kwargs.update(subprocess_windows_no_window_kwargs())
    if sys.platform == "win32":
        kwargs["encoding"] = _TOOL_TEXT_ENCODING
        kwargs["errors"] = _TOOL_TEXT_ERRORS
    return kwargs


def decode_subprocess_output(raw: bytes) -> str:
    """Décode un buffer brut provenant d'un outil externe."""
    return raw.decode(_TOOL_TEXT_ENCODING, errors=_TOOL_TEXT_ERRORS)


class ProbeCancelledError(RuntimeError):
    """Sonde interrompue par une annulation (fermeture de fenêtre, nouveau fichier)."""


def run_probe(
    command: list[str],
    *,
    timeout: float,
    cancel_event: threading.Event | None = None,
    **kwargs: Any,
) -> subprocess.CompletedProcess:
    """Sonde texte à sortie capturée, bornée et annulable.

    Le processus est tué (avec ses descendants) à l'expiration du délai ou à
    l'annulation. Erreurs distinctes : ``FileNotFoundError`` (outil absent),
    ``subprocess.TimeoutExpired`` (délai), :class:`ProbeCancelledError`
    (annulation) ; un code de retour non nul est rendu à l'appelant.
    """
    def check_cancelled() -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise ProbeCancelledError("Sonde annulée.")

    options = {**subprocess_text_kwargs(), **kwargs}
    return run_cancellable_capture(
        list(command),
        cancel_cb=cancel_event.is_set if cancel_event is not None else None,
        check_cancelled=check_cancelled,
        timeout=timeout,
        **options,
    )


def run_cancellable_capture(
    command: list[str],
    *,
    cancel_cb: Callable[[], bool] | None,
    check_cancelled: Callable[[], None],
    on_start: Callable[[subprocess.Popen], None] | None = None,
    on_end: Callable[[subprocess.Popen], None] | None = None,
    capture_output: bool = True,
    check: bool = False,
    **kwargs: Any,
) -> subprocess.CompletedProcess:
    """Sonde avec sortie capturée, tuable même pendant une lecture silencieuse."""
    check_cancelled()
    timeout = kwargs.pop("timeout", None)
    # Session propre : les descendants gardant stdout ouvert sont également
    # arrêtés, sans toucher au groupe du GUI ou de la CLI.
    if sys.platform != "win32":
        kwargs.setdefault("start_new_session", True)
    # Commande fournie par les workflows sous forme d'argv, sans interprétation shell.
    # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
    with subprocess.Popen(  # nosec B603
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs,
    ) as proc:
        if sys.platform != "win32" and kwargs.get("start_new_session"):
            proc._muxiveo_process_group = proc.pid  # type: ignore[attr-defined]
        try:
            if on_start is not None:
                on_start(proc)
            with watch_process_cancellation(proc, cancel_cb):
                stdout, stderr = proc.communicate(timeout=timeout)
            check_cancelled()
            # Simple objet résultat : aucun lancement de processus.
            # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
            result = subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)
            if check:
                result.check_returncode()
            return result
        except BaseException:
            kill_process_tree(proc, timeout=0.2)
            raise
        finally:
            if on_end is not None:
                on_end(proc)


# Variables pouvant pointer dans le bundle figé (AppImage/PyInstaller) et casser
# les programmes de l'hôte (xdg-open, navigateur, nouvelle AppImage…).
_BUNDLE_PATH_ENV_VARS = (
    "LD_LIBRARY_PATH",
    "LD_PRELOAD",
    "PATH",
    "XDG_DATA_DIRS",
    "QT_PLUGIN_PATH",
    "QT_QPA_PLATFORM_PLUGIN_PATH",
    "QML2_IMPORT_PATH",
    "PYTHONHOME",
    "PYTHONPATH",
    "GIO_MODULE_DIR",
    "GDK_PIXBUF_MODULE_FILE",
    "GDK_PIXBUF_MODULEDIR",
    "GTK_PATH",
    "GSETTINGS_SCHEMA_DIR",
)


def host_environment(env: Mapping[str, str] | None = None, bundle_roots: tuple[str, ...] | None = None) -> dict[str, str]:
    """
    Environnement pour lancer un programme de l'hôte depuis un build figé.

    Retire des variables de chemins toute entrée située dans le bundle
    ($APPDIR, sys._MEIPASS) : sinon le programme lancé charge les bibliothèques
    embarquées (glib, ssl…) et échoue (ex. « undefined symbol » pour gio/kde-open).
    Les outils embarqués (ffmpeg…) doivent au contraire garder l'environnement courant.
    """
    result = dict(os.environ if env is None else env)
    if bundle_roots is None:
        bundle_roots = tuple(r for r in (result.get("APPDIR"), getattr(sys, "_MEIPASS", None)) if r)
    roots = tuple(str(r).rstrip("/") for r in bundle_roots if r)
    if not roots:
        return result
    result.pop("LD_LIBRARY_PATH_ORIG", None)
    for name in _BUNDLE_PATH_ENV_VARS:
        raw = result.get(name)
        if raw is None:
            continue
        kept = [
            entry
            for entry in raw.split(os.pathsep)
            if entry and not any(entry == root or entry.startswith(root + "/") for root in roots)
        ]
        if kept:
            result[name] = os.pathsep.join(kept)
        else:
            result.pop(name, None)
    return result


def format_returncode(returncode: int | None) -> str:
    """
    Code de retour lisible : sous Windows, les codes > 2^31 (NTSTATUS, AVERROR
    FFmpeg) sont remis en entier signé et complétés de leur forme hexadécimale.
    """
    if returncode is None:
        return "?"
    code = int(returncode)
    if 0x7FFFFFFF < code <= 0xFFFFFFFF:
        return f"{code - 0x100000000} / 0x{code:08X}"
    return str(code)


_WINDOWS_SYSTEM_DIR_FALLBACK = r"C:\Windows\System32"


def _windows_system_directory() -> str:
    """Dossier système Windows via GetSystemDirectoryW (ni PATH ni variable d'environnement)."""
    try:
        import ctypes

        buffer = ctypes.create_unicode_buffer(260)
        length = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))  # type: ignore[attr-defined]
        if 0 < length < len(buffer):
            return buffer.value
    except (AttributeError, OSError):
        pass
    return _WINDOWS_SYSTEM_DIR_FALLBACK


def _windows_taskkill_path() -> Path:
    """Chemin absolu de taskkill.exe, non influençable par l'environnement."""
    return Path(_windows_system_directory()) / "taskkill.exe"


def kill_process_tree(proc: subprocess.Popen | None, timeout: float = 0.5) -> None:
    """
    Tue le processus et son groupe dédié (POSIX) ou son arbre (Windows).

    Sous Windows :
      - `proc.kill()` (TerminateProcess) ne tue pas les enfants créés par l'outil.
      - `taskkill /F /T /PID <pid>` force l'arrêt récursif de tout l'arbre.
    Les flux appartiennent au worker : les fermer ici peut bloquer sur le verrou
    d'un lecteur ou lui faire lever ValueError. Le worker les ferme après EOF.
    """
    if proc is None:
        return

    pid = getattr(proc, "pid", None)
    group = getattr(proc, "_muxiveo_process_group", None)
    if sys.platform != "win32" and isinstance(group, int):
        with suppress(OSError):
            os.killpg(group, signal.SIGKILL)
    elif sys.platform != "win32" and isinstance(pid, int):
        with suppress(OSError):
            # Ne jamais tuer un groupe hérité. Un enfant peut garder les pipes
            # ouverts même après la fin du processus principal.
            if os.getpgid(pid) == pid:
                os.killpg(pid, signal.SIGKILL)

    poll = getattr(proc, "poll", None)
    if callable(poll):
        with suppress(Exception):
            if poll() is not None:
                return

    if sys.platform == "win32" and pid is not None:
        with suppress(Exception):
            # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
            subprocess.run(  # nosec B603  # nosemgrep  # argv fixe, binaire système absolu, PID entier
                [str(_windows_taskkill_path()), "/F", "/T", "/PID", str(int(pid))],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
                timeout=2.0,
            )

    kill = getattr(proc, "kill", None)
    if callable(kill):
        with suppress(OSError):
            kill()

    wait = getattr(proc, "wait", None)
    if callable(wait):
        with suppress(Exception):
            wait(timeout=timeout)


@contextmanager
def watch_process_cancellation(
    proc: subprocess.Popen,
    cancel_cb: Callable[[], bool] | None,
) -> Iterator[None]:
    """Interrompt aussi un processus silencieux pendant une lecture ou wait().

    Le surveillant ne touche jamais aux flux et est rejoint avant de rendre le
    processus à l'appelant : aucune annulation tardive après la sortie du bloc.
    """
    stopped = threading.Event()

    def watch() -> None:
        while not stopped.is_set():
            if cancel_cb is not None and cancel_cb():
                kill_process_tree(proc, timeout=0.2)
                return
            stopped.wait(0.05)

    thread = None
    if cancel_cb is not None:
        thread = threading.Thread(target=watch, name="process-cancellation", daemon=True)
        thread.start()
    try:
        yield
    finally:
        stopped.set()
        if thread is not None:
            thread.join()
