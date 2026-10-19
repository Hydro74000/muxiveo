#!/usr/bin/env python3
"""
main.py — Point d'entrée de l'application Muxiveo.

Lance la MainWindow PySide6 après initialisation de la configuration.
"""

import sys
from pathlib import Path

# Assure que le dossier racine du projet est dans sys.path
ROOT = Path(__file__).parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

APP_ICON_PATH = ROOT / "ui" / "assets" / "muxiveo.png"

# Imports différés volontairement : la racine du projet doit être dans
# sys.path avant les paquets core/ui (lancement direct du script).
from PySide6.QtWidgets import QApplication  # noqa: E402
from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import QMessageBox, QPushButton, QSplashScreen  # noqa: E402

from core.config import AppConfig  # noqa: E402
from core.i18n import set_current_language, translate_text  # noqa: E402
from core.version import APP_NAME, APP_VERSION  # noqa: E402
from ui.design_system import DesignSystem, colors  # noqa: E402


def _show_startup_splash(app: QApplication) -> QSplashScreen:
    """Retour visuel immédiat pendant la construction de la fenêtre principale.

    Au premier lancement (antivirus analysant les binaires fraîchement
    installés), plusieurs secondes peuvent s'écouler sans aucune fenêtre.
    """
    width, height = DesignSystem.scale(360), DesignSystem.scale(220)
    pixmap = QPixmap(width, height)
    pixmap.fill(QColor(colors.BG_DEEP))
    icon = QPixmap(str(APP_ICON_PATH))
    if not icon.isNull():
        size = DesignSystem.scale(112)
        icon = icon.scaled(
            size, size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        painter = QPainter(pixmap)
        painter.drawPixmap((width - icon.width()) // 2, DesignSystem.scale(24), icon)
        painter.end()
    splash = QSplashScreen(pixmap)
    splash.showMessage(
        f"{APP_NAME} {APP_VERSION} — " + translate_text("Chargement…"),
        Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom,
        QColor(colors.TEXT_PRI),
    )
    splash.show()
    app.processEvents()
    return splash


def _prompt_work_dir_cleanup(config: AppConfig) -> None:
    """
    Au démarrage, demande quoi faire si le work_dir contient des restes.
    """
    if not config.work_dir_has_leftovers():
        return

    # Liste = exactement ce que « Nettoyer » supprimera (éléments Muxiveo uniquement).
    entries = config.work_dir_entries()
    preview = ", ".join(p.name for p in entries[:6])
    if len(entries) > 6:
        preview += translate_text(" … (+{count} autres)", count=len(entries) - 6)

    box = QMessageBox()
    box.setWindowFlags(box.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle(translate_text("Répertoire de travail non nettoyé"))
    box.setText(
        translate_text(
            "Le dossier de travail contient des fichiers ou dossiers résiduels.\n"
            "Souhaitez-vous les conserver ou nettoyer le work_dir ?"
        )
    )
    informative = translate_text(
        "Work dir : {path}\nÉléments Muxiveo à supprimer ({count}) : {preview}",
        path=str(config.work_dir),
        count=len(entries),
        preview=preview or "-",
    )
    active = config.work_dir_active_jobs()
    if active:
        # Jobs d'une autre instance (GUI, CLI) : conservés quel que soit le choix.
        informative += "\n" + translate_text(
            "Traitements en cours conservés ({count}) : {names}",
            count=len(active),
            names=", ".join(p.name for p in active[:6]),
        )
    box.setInformativeText(informative)

    clean_btn: QPushButton = box.addButton(
        translate_text("Nettoyer"),
        QMessageBox.ButtonRole.DestructiveRole,
    )
    keep_btn: QPushButton = box.addButton(
        translate_text("Conserver"),
        QMessageBox.ButtonRole.AcceptRole,
    )
    box.setDefaultButton(keep_btn)
    box.exec()

    if box.clickedButton() is clean_btn:
        config.clear_work_dir()


def _startup_paths_from_argv(argv: list[str]) -> list[Path]:
    """Extrait les chemins de fichiers existants passés au lancement."""
    paths: list[Path] = []
    for raw in argv[1:]:
        if not raw or raw.startswith("-"):
            continue
        path = Path(raw).expanduser()
        if path.exists():
            paths.append(path)
    return paths


def _is_cli_invocation(argv: list[str]) -> bool:
    return "--cli" in argv[1:]


def _cli_args_from_argv(argv: list[str]) -> list[str]:
    args = list(argv[1:])
    try:
        args.remove("--cli")
    except ValueError:
        pass
    return args


def _run_cli_entrypoint(argv: list[str]) -> int:
    from cli.main import main as _cli_main  # noqa: PLC0415

    return _cli_main(_cli_args_from_argv(argv))


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    if _is_cli_invocation(argv):
        return _run_cli_entrypoint(argv)

    startup_paths = _startup_paths_from_argv(argv)
    app_instance = QApplication.instance()
    if not isinstance(app_instance, QApplication):
        # High-DPI : activé par défaut sous Qt 6, mais on force le scaling exact
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps)
        app = QApplication(argv)
    else:
        app = app_instance
    app.setApplicationName(APP_NAME)
    if APP_ICON_PATH.is_file():
        app.setWindowIcon(QIcon(str(APP_ICON_PATH)))
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName(APP_NAME)

    # Police par défaut propre
    default_font = QFont("Segoe UI", 10) if sys.platform == "win32" else QFont("SF Pro Text", 10)
    default_font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)

    # Chargement de la configuration globale
    config = AppConfig()
    DesignSystem.set_theme(config.theme)
    DesignSystem.set_ui_scale(config.ui_scale_percent)
    default_font.setPointSizeF(max(8.0, 10.0 * DesignSystem.scale_factor()))
    app.setFont(default_font)
    DesignSystem.apply_to_application(app)
    set_current_language(config.language)
    try:
        _prompt_work_dir_cleanup(config)
    except OSError:
        QMessageBox.warning(
            None,
            translate_text("Dossier de travail inaccessible"),
            translate_text(
                "Le dossier de travail configuré n'est pas accessible au démarrage :\n{path}",
                path=str(config.work_dir),
            ),
        )
    splash = _show_startup_splash(app)

    # Fenêtre principale
    from ui.main_window import MainWindow
    window = MainWindow(config)
    if startup_paths:
        startup_items: list[Path | str] = list(startup_paths)
        QTimer.singleShot(0, lambda: window.open_startup_paths(startup_items))
    window.show()
    splash.finish(window)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
