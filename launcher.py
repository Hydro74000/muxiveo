#!/usr/bin/env python3
"""
launcher.py — Point d'entrée packagé de Mediarecode.

Vérifie la présence de config.ini dans le dossier de configuration utilisateur :
  Linux / macOS  → $XDG_CONFIG_HOME/mediarecode/config.ini  (défaut : ~/.config/…)
  Windows frozen → %APPDATA%\\mediarecode\\config.ini
  Windows dev    → racine du projet

Si absent → lance le setup système, puis démarre l'application Qt.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _ensure_text_stream(name: str, mode: str) -> None:
    """Provide stdout/stderr when frozen without a console window."""
    if getattr(sys, name, None) is None:
        setattr(sys, name, open(os.devnull, mode, encoding="utf-8"))


_ensure_text_stream("stdout", "w")
_ensure_text_stream("stderr", "w")


# ---------------------------------------------------------------------------
# Résolution du dossier de référence (là où config.ini doit vivre)
# ---------------------------------------------------------------------------

def _is_allinc() -> bool:
    """
    Retourne True si on tourne dans un AppImage all-inclusive.
    Détecté via le fichier marqueur _ALLINC placé dans le bundle par package_appimage.py.
    """
    if not getattr(sys, "frozen", False):
        return False
    # Le marqueur est à côté de l'exécutable (dist/mediarecode/_ALLINC)
    return (Path(sys.executable).parent / "_ALLINC").exists()


def _windows_config_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "mediarecode"
    return Path.home() / "AppData" / "Roaming" / "mediarecode"


def _get_config_path() -> Path:
    """
    Retourne le chemin de config.ini selon la plateforme :
    - Linux / macOS  → ~/.config/mediarecode/config.ini  (XDG)
    - Windows frozen → %APPDATA%\\mediarecode\\config.ini
    - Windows dev    → racine du projet
    """
    if sys.platform != "win32":
        xdg = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        return xdg / "mediarecode" / "config.ini"
    if getattr(sys, "frozen", False):
        return _windows_config_dir() / "config.ini"
    return Path(__file__).parent / "config.ini"


def _restart_current_app() -> bool:
    """
    Restart the current launcher as a fresh process.

    This is required after updating Windows Controlled Folder Access allowlists:
    the newly allowed executable only gains write access on its next start.
    """
    try:
        if getattr(sys, "frozen", False):
            cmd = [sys.executable]
        else:
            cmd = [sys.executable, str(Path(__file__).resolve())]
        subprocess.Popen(cmd)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Setup premier lancement
# ---------------------------------------------------------------------------

def _run_first_time_setup(install_dir: Path) -> int:
    """
    Lance le setup système (outils externes + config.ini initial).
    Retourne 0 en succès, 1 en échec bloquant.
    """
    import platform

    allinc = _is_allinc()

    print("\n" + "=" * 60)
    if allinc:
        print("  Mediarecode — Initialisation (all-inclusive)")
    else:
        print("  Mediarecode — Première installation")
    print("=" * 60)
    print(f"\n  config.ini introuvable dans : {install_dir}")
    if allinc:
        print("  Les outils sont déjà embarqués — initialisation de la configuration...\n")
    else:
        print("  Lancement du setup...\n")

    # Dans un bundle PyInstaller, les sources sont accessibles via sys._MEIPASS
    if getattr(sys, "frozen", False):
        bundle_root = Path(getattr(sys, "_MEIPASS", ""))
        if bundle_root and str(bundle_root) not in sys.path:
            sys.path.insert(0, str(bundle_root))

    try:
        import setup as _setup  # bundled alongside launcher or in project root
    except ImportError as exc:
        print(f"\n  ERREUR : impossible d'importer setup.py — {exc}", file=sys.stderr)
        return 1

    _os = platform.system()
    prefix = _setup._default_prefix()
    dry_run = False
    force = False
    cfa_result: dict[str, object] = {"status": "not_run"}

    try:
        install_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    try:
        if allinc:
            # Mode all-inclusive : outils déjà embarqués dans l'AppImage,
            # on initialise uniquement la langue / config.ini.
            _setup.initialize_config_ini_language(
                dry_run, force=force, ini_path=install_dir / "config.ini"
            )
        else:
            if _os == "Linux":
                distro = _setup.detect_linux_distro()
                print(f"  Distribution détectée : {distro}")
                if distro == "debian":
                    _setup.install_apt(dry_run, force=force)
                elif distro == "fedora":
                    _setup.install_dnf(dry_run, force=force)
                else:
                    print(
                        "  Distribution non reconnue — installez manuellement :\n"
                        "    ffmpeg  mkvtoolnix  mediainfo",
                        file=sys.stderr,
                    )
                _setup.install_github_tools(prefix, dry_run, force=force)
                _setup.check_tools_presence()

            elif _os == "Darwin":
                _setup.install_brew(dry_run, force=force)
                _setup.install_github_tools(prefix, dry_run, force=force)
                _setup.check_tools_presence()

            elif _os == "Windows":
                _setup.install_winget(dry_run, force=force)
                _setup.install_github_tools(prefix, dry_run, force=force)
                _setup.autofill_windows_config_ini(prefix, dry_run, force=force)
                _setup.check_tools_presence(prefix)
                cfa_result = _setup.offer_windows_controlled_folder_access_setup(
                    prefix, dry_run, force=force
                )

            else:
                print(f"  Plateforme inconnue '{_os}' — setup système ignoré.", file=sys.stderr)

            # Initialise la langue dans QSettings (et config.ini si absents)
            _setup.initialize_config_ini_language(
                dry_run, force=force, ini_path=install_dir / "config.ini"
            )

            # Packages Python : inutiles dans un bundle (déjà embarqués)
            if not getattr(sys, "frozen", False):
                _setup.install_python_packages(dry_run, force=force)

    except Exception as exc:
        print(f"\n  ERREUR pendant le setup : {exc}", file=sys.stderr)
        try:
            answer = input("\n  Continuer quand même ? [o/N] ").strip().lower()
        except (EOFError, RuntimeError):
            answer = ""
        if answer not in ("o", "oui", "y", "yes"):
            return 1

    # Crée un config.ini marqueur dans install_dir pour signaler que le setup
    # a été effectué (évite de relancer le setup à chaque démarrage).
    marker = install_dir / "config.ini"
    if not marker.exists():
        try:
            install_dir.mkdir(parents=True, exist_ok=True)
            marker.write_text(
                "# Mediarecode — configuration locale\n"
                "# Décommentez et modifiez les clés pour surcharger les valeurs par défaut.\n"
                "# Voir la section Configuration dans CLAUDE.md pour la liste complète.\n",
                encoding="utf-8",
            )
        except OSError:
            # Dossier en lecture seule (AppImage dans /opt, /usr …) — non bloquant
            pass

    if _os == "Windows" and str(cfa_result.get("status") or "") == "updated":
        print(
            "\n  Windows Security a été mis à jour."
            "\n  Redémarrage de Mediarecode pour appliquer l'autorisation...\n"
        )
        if _restart_current_app():
            return 0
        print(
            "  Impossible de relancer automatiquement l'application."
            "\n  Fermez cette fenêtre puis relancez Mediarecode manuellement.\n",
            file=sys.stderr,
        )
        return 1

    print("\n  Setup terminé. Démarrage de l'application...\n")
    return 0


# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------

def main() -> int:
    config_path = _get_config_path()

    if not config_path.exists():
        rc = _run_first_time_setup(config_path.parent)
        if rc != 0:
            try:
                input("Appuyez sur Entrée pour quitter...")
            except (EOFError, RuntimeError):
                pass
            return rc

    # Lance l'application Qt
    from main import main as _app_main  # noqa: PLC0415
    return _app_main()


if __name__ == "__main__":
    sys.exit(main())
