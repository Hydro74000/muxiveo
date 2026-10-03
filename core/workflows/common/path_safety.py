"""Comparaison des chemins d'entrée et de sortie avant écriture."""

from pathlib import Path


def same_filesystem_target(source: Path, output: Path) -> bool:
    """Détecte un même fichier, y compris chemins relatifs, liens et hardlinks."""
    if source == output:
        return True
    try:
        if source.resolve() == output.resolve():
            return True
    except OSError:
        pass
    try:
        return source.exists() and output.exists() and source.samefile(output)
    except OSError:
        return False
