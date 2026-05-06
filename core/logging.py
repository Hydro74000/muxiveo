"""
core/logging.py — Moteur de logging applicatif (UI + fichiers verbose).

Centralise :
    - les niveaux de logs applicatifs
    - la normalisation des niveaux reçus via signaux
    - l'écriture des logs verbose avec rotation
    - la reprise de session depuis le dernier fichier existant
"""

from __future__ import annotations

import logging as _stdlib_logging
import re
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Callable


class LogLevel(str, Enum):
    INFO = "INFO"
    OK = "OK"
    WARN = "WARN"
    ERROR = "ERROR"


def get_logger(name: str) -> _stdlib_logging.Logger:
    """Point d'entrée unique pour les loggers stdlib du projet."""
    return _stdlib_logging.getLogger(name)


def parse_log_level(value: str, default: LogLevel = LogLevel.INFO) -> LogLevel:
    """Convertit une chaîne en LogLevel avec fallback robuste."""
    try:
        return LogLevel(str(value).strip().upper())
    except ValueError:
        return default


VERBOSE_LOG_MAX_BYTES = 50 * 1024 * 1024
VERBOSE_LOG_MAX_FILES = 3
_VERBOSE_LOG_FILE_RE = re.compile(r"^mediarecode-verbose-(\d{8}-\d{6})-(\d+)\.log$")


def _latest_verbose_log_file(
    logs_dir: Path,
    *,
    max_files: int,
) -> tuple[str, int, Path] | None:
    latest: tuple[str, int, Path] | None = None
    for path in logs_dir.glob("mediarecode-verbose-*.log"):
        match = _VERBOSE_LOG_FILE_RE.match(path.name)
        if match is None:
            continue
        stamp = match.group(1)
        try:
            index = int(match.group(2))
        except ValueError:
            continue
        if index < 1 or index > max_files:
            continue
        candidate = (stamp, index, path)
        if latest is None or candidate > latest:
            latest = candidate
    if latest is None:
        return None
    stamp, index, path = latest
    return stamp, index, path


class VerboseFileLogger:
    """Gestionnaire de fichier verbose avec rotation circulaire."""

    def __init__(
        self,
        *,
        app_data_dir: Path,
        verbose_log_dir: Path | str | None,
        enabled: bool,
        on_write_error: Callable[[], None] | None = None,
        max_bytes: int | None = None,
        max_files: int | None = None,
    ) -> None:
        self._app_data_dir = Path(app_data_dir)
        self._configured_verbose_log_dir = (
            Path(verbose_log_dir) if verbose_log_dir not in (None, "") else None
        )
        self._enabled = bool(enabled)
        self._on_write_error = on_write_error
        self._max_bytes = int(max_bytes if max_bytes is not None else VERBOSE_LOG_MAX_BYTES)
        self._max_files = max(1, int(max_files if max_files is not None else VERBOSE_LOG_MAX_FILES))

        self._session_file_path: Path | None = None
        self._session_stamp: str | None = None
        self._file_index = 1
        self._session_bootstrapped = False
        self._error_reported = False
        self._open_handle = None
        self._open_handle_path: Path | None = None
        self._open_handle_size: int = 0

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def session_stamp(self) -> str | None:
        return self._session_stamp

    @property
    def file_index(self) -> int:
        return self._file_index

    def configure(
        self,
        *,
        app_data_dir: Path,
        verbose_log_dir: Path | str | None,
        enabled: bool,
    ) -> None:
        previous_dir = self._resolve_logs_dir_path()
        self._app_data_dir = Path(app_data_dir)
        self._configured_verbose_log_dir = (
            Path(verbose_log_dir) if verbose_log_dir not in (None, "") else None
        )
        self._enabled = bool(enabled)
        current_dir = self._resolve_logs_dir_path()
        if current_dir != previous_dir:
            self.reset_session()
            self._error_reported = False
        elif not self._enabled:
            self._close_handle()

    def reset_session(self) -> None:
        self._close_handle()
        self._session_file_path = None
        self._session_stamp = None
        self._file_index = 1
        self._session_bootstrapped = False

    def _close_handle(self) -> None:
        if self._open_handle is not None:
            try:
                self._open_handle.close()
            except OSError:
                pass
        self._open_handle = None
        self._open_handle_path = None
        self._open_handle_size = 0

    def close(self) -> None:
        self._close_handle()

    def part_path(self, index: int) -> Path:
        logs_dir = self._logs_dir()
        if self._session_stamp is None:
            self._session_stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe_index = max(1, min(self._max_files, int(index)))
        return logs_dir / f"mediarecode-verbose-{self._session_stamp}-{safe_index:02d}.log"

    def session_path(self) -> Path:
        if self._session_file_path is None:
            if not self._session_bootstrapped:
                self._session_bootstrapped = True
                if self._session_stamp is None:
                    latest = _latest_verbose_log_file(self._logs_dir(), max_files=self._max_files)
                    if latest is not None:
                        stamp, index, path = latest
                        self._session_stamp = stamp
                        self._file_index = index
                        self._session_file_path = path
            if self._session_file_path is None:
                self._session_file_path = self.part_path(self._file_index)
        return self._session_file_path

    def prepare_target(self, incoming_bytes: int) -> Path:
        path = self.session_path()
        # Détermine la taille courante. La rotation ne se déclenche que si le
        # fichier cible existe déjà — sinon on l'écrit tel quel (préserve le
        # comportement historique : un fichier neuf accepte toujours sa 1re ligne).
        current_size: int | None = None
        if self._open_handle is not None and self._open_handle_path == path:
            current_size = self._open_handle_size
        elif path.exists():
            try:
                current_size = path.stat().st_size
            except OSError:
                current_size = 0
        if current_size is not None and current_size + max(0, incoming_bytes) > self._max_bytes:
            next_index = self._file_index + 1
            if next_index > self._max_files:
                next_index = 1
            self._file_index = next_index
            path = self.part_path(self._file_index)
            self._session_file_path = path
            self._close_handle()
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        return path

    def append_application_message(self, message: str, level: LogLevel) -> None:
        if not self._enabled:
            return
        # Keep stdlib logging importable in headless paths such as CLI smoke tests.
        from core.i18n import translate_text

        rendered = translate_text(message)
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [{level.value}] {rendered}\n"
        self._append_raw_line(line)

    def append_tool_output(self, line: str, *, label: str | None = None) -> None:
        if not self._enabled:
            return
        rendered = str(line).rstrip()
        if not rendered:
            return
        prefix = f"[{label}] " if label else ""
        entry = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [TOOL] {prefix}{rendered}\n"
        self._append_raw_line(entry)

    def _append_raw_line(self, line: str) -> None:
        encoded = line.encode("utf-8")
        path = self.prepare_target(len(encoded))
        try:
            if self._open_handle is None or self._open_handle_path != path:
                self._close_handle()
                # Buffering ligne (1) : flush sur newline, évite open/close par ligne
                # tout en gardant les logs visibles en quasi temps réel.
                self._open_handle = path.open("a", encoding="utf-8", buffering=1)
                self._open_handle_path = path
                try:
                    self._open_handle_size = path.stat().st_size
                except OSError:
                    self._open_handle_size = 0
            self._open_handle.write(line)
            self._open_handle_size += len(encoded)
        except OSError:
            self._close_handle()
            if not self._error_reported:
                self._error_reported = True
                if self._on_write_error is not None:
                    self._on_write_error()

    def _resolve_logs_dir_path(self) -> Path:
        if self._configured_verbose_log_dir is not None:
            return Path(self._configured_verbose_log_dir)
        return self._app_data_dir / "logs"

    def _logs_dir(self) -> Path:
        path = self._resolve_logs_dir_path()
        path.mkdir(parents=True, exist_ok=True)
        return path
