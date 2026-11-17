"""Headless CLI entrypoint for Muxiveo."""

from __future__ import annotations

import configparser
import sys

from PySide6.QtCore import QCoreApplication

from core.config import AppConfig
from core.version import APP_NAME
from core.runner import ToolNotFoundError
from core.service.errors import ApiError

from cli.constants import EXIT_TOOL, EXIT_WORKFLOW
from cli.errors import CliError
from cli.logging import Logger

__all__ = ["main"]


def _ensure_qcore_app(argv: list[str] | None = None) -> QCoreApplication:
    app = QCoreApplication.instance()
    if isinstance(app, QCoreApplication):
        return app
    app = QCoreApplication(argv or [sys.argv[0]])
    # Mêmes noms que l'interface : dossiers de données partagés (caches HDR Studio, journaux).
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_NAME)
    return app


def _configure_io_encoding() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # nosec B110
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
        try:
            config = AppConfig()
        except (configparser.Error, UnicodeError) as exc:
            # Entrées de l'API sans autorisation (call, serve) : config.ini illisible rendu sans son contenu, qui
            # peut contenir un secret ; commandes historiques et autorisées : diagnostic d'origine.
            if args.command in ("call", "serve") and not getattr(args, "allow_privileged", False):
                from core.config import config_syntax_message

                raise CliError(config_syntax_message(exc), EXIT_WORKFLOW) from None
            raise
        for warning in getattr(config, "load_warnings", []):
            logger.emit("warning", warning)
        return int(args.func(args, config, logger))
    except ToolNotFoundError as exc:
        logger.emit("error", str(exc))
        return EXIT_TOOL
    except CliError as exc:
        if not exc.logged:
            logger.emit("error", str(exc))
        return exc.exit_code
    except ApiError as exc:
        from cli.service_adapter import render_api_error

        return render_api_error(logger, exc)
    except KeyboardInterrupt:
        logger.emit("error", "Interrompu.")
        return EXIT_WORKFLOW
    except Exception as exc:
        logger.emit("error", str(exc), exception=repr(exc))
        return EXIT_WORKFLOW


if __name__ == "__main__":
    raise SystemExit(main())
