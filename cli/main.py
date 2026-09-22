"""Headless CLI entrypoint for Muxiveo."""

from __future__ import annotations

import sys

from PySide6.QtCore import QCoreApplication

from core.config import AppConfig
from core.runner import ToolNotFoundError

from cli.constants import EXIT_TOOL, EXIT_WORKFLOW
from cli.errors import CliError
from cli.logging import Logger

__all__ = ["main"]


def _ensure_qcore_app(argv: list[str] | None = None) -> QCoreApplication:
    app = QCoreApplication.instance()
    if isinstance(app, QCoreApplication):
        return app
    return QCoreApplication(argv or [sys.argv[0]])


def _configure_io_encoding() -> None:
    for stream in (sys.stdout, sys.stderr):
        if stream and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def main(argv: list[str] | None = None) -> int:
    _configure_io_encoding()
    argv = list(sys.argv[1:] if argv is None else argv)
    _ensure_qcore_app([sys.argv[0], *argv])
    from cli.parser import build_parser

    parser = build_parser()
    args = parser.parse_args(argv)
    logger = Logger(fmt=args.log_format)
    try:
        config = AppConfig()
        for warning in getattr(config, "load_warnings", []):
            logger.emit("warning", warning)
        return int(args.func(args, config, logger))
    except ToolNotFoundError as exc:
        logger.emit("error", str(exc))
        return EXIT_TOOL
    except CliError as exc:
        logger.emit("error", str(exc))
        return exc.exit_code
    except KeyboardInterrupt:
        logger.emit("error", "Interrompu.")
        return EXIT_WORKFLOW
    except Exception as exc:
        logger.emit("error", str(exc), exception=repr(exc))
        return EXIT_WORKFLOW


if __name__ == "__main__":
    raise SystemExit(main())
