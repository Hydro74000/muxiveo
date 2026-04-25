#!/usr/bin/env python3
"""
main.py — Point d'entrée de l'application Mediarecode.

Lance la MainWindow PySide6 après initialisation de la configuration.
"""

import sys
from pathlib import Path

# Assure que le dossier racine du projet est dans sys.path
ROOT = Path(__file__).parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QMessageBox, QPushButton

from core.config import AppConfig
from core.i18n import set_current_language, translate_text
from core.version import APP_VERSION
from ui.design_system import DesignSystem


def _prompt_work_dir_cleanup(config: AppConfig) -> None:
    """
    Au démarrage, demande quoi faire si le work_dir contient des restes.
    """
    if not config.work_dir_has_leftovers():
        return

    entries = config.work_dir_entries()
    preview = ", ".join(p.name for p in entries[:6])
    if len(entries) > 6:
        preview += ", ..."

    box = QMessageBox()
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle(translate_text("Répertoire de travail non nettoyé"))
    box.setText(
        translate_text(
            "Le dossier de travail contient des fichiers ou dossiers résiduels.\n"
            "Souhaitez-vous les conserver ou nettoyer le work_dir ?"
        )
    )
    box.setInformativeText(
        translate_text(
            "Work dir : {path}\nContenu détecté : {preview}",
            path=str(config.work_dir),
            preview=preview or "-",
        )
    )

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


def main() -> int:
    startup_paths = _startup_paths_from_argv(sys.argv)
    app_instance = QApplication.instance()
    if not isinstance(app_instance, QApplication):
        # High-DPI : activé par défaut sous Qt 6, mais on force le scaling exact
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps)
        app = QApplication(sys.argv)
    else:
        app = app_instance
    app.setApplicationName("Mediarecode")
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName("mediarecode")

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
    _prompt_work_dir_cleanup(config)

    # Fenêtre principale
    from ui.main_window import MainWindow
    window = MainWindow(config)
    if startup_paths:
        startup_items: list[Path | str] = list(startup_paths)
        QTimer.singleShot(0, lambda: window.open_startup_paths(startup_items))
    window.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
