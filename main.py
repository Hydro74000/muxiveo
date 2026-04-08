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
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont

from core.config import AppConfig
from core.i18n import set_current_language
from core.version import APP_VERSION
from ui.design_system import DesignSystem


def main() -> int:
    app = QApplication.instance()
    if app is None:
        # High-DPI : activé par défaut sous Qt 6, mais on force le scaling exact
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps)
        app = QApplication(sys.argv)
    app.setApplicationName("Mediarecode")
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName("mediarecode")

    # Police par défaut propre
    default_font = QFont("Segoe UI", 10) if sys.platform == "win32" else QFont("SF Pro Text", 10)
    default_font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    app.setFont(default_font)

    # Chargement de la configuration globale
    config = AppConfig()
    DesignSystem.set_theme(config.theme)
    DesignSystem.apply_to_application(app)
    set_current_language(config.language)

    # Fenêtre principale
    from ui.main_window import MainWindow
    window = MainWindow(config)
    window.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
