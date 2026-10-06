"""
core/python_requirements.py — Dépendances Python d'exécution (source unique : requirements.txt).

Le nom de distribution (pip) et le module importable peuvent différer
(``PySide6`` s'importe sous ce nom exact, avec sa casse). Une borne minimale
est vérifiée sur la version installée de la distribution
(``importlib.metadata``) : un import réussi ne prouve pas la version.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

REQUIREMENTS_FILE = Path(__file__).resolve().parents[1] / "requirements.txt"

# Module importable quand il diffère du nom de distribution (clé en minuscules).
_MODULE_NAMES = {"pyside6": "PySide6"}
_LINE_RE = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:>=\s*(?P<minimum>[0-9][0-9.]*))?$")


@dataclass(frozen=True)
class PythonRequirement:
    """Ligne de requirements.txt : distribution, borne minimale et module importable."""

    distribution: str
    spec: str
    minimum: tuple[int, ...] | None
    module: str


def parse_version(text: str) -> tuple[int, ...]:
    """Composantes numériques de tête (``6.10.0`` → (6, 10, 0) ; ``2.3.0rc1`` → (2, 3, 0))."""
    parts: list[int] = []
    for piece in str(text).split("."):
        match = re.match(r"\d+", piece)
        if match is None:
            break
        parts.append(int(match.group()))
        if match.end() != len(piece):
            break
    return tuple(parts)


def read_requirements(path: Path = REQUIREMENTS_FILE) -> list[PythonRequirement]:
    """Dépendances déclarées ; seules les formes ``Nom`` et ``Nom>=x.y`` sont admises."""
    requirements: list[PythonRequirement] = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        match = _LINE_RE.match(line)
        if match is None:
            raise ValueError(f"Dépendance non prise en charge dans {path.name} : {line}")
        name = match.group("name")
        minimum = match.group("minimum")
        requirements.append(PythonRequirement(
            distribution=name,
            spec=f"{name}>={minimum}" if minimum else name,
            minimum=parse_version(minimum) if minimum else None,
            module=_MODULE_NAMES.get(name.lower(), name.replace("-", "_")),
        ))
    return requirements


def installed_version(distribution: str) -> str | None:
    """Version installée de la distribution, None si absente."""
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return None


def unsatisfied_requirements(
    requirements: list[PythonRequirement],
) -> list[tuple[PythonRequirement, str | None]]:
    """Dépendances absentes ou trop anciennes, avec la version trouvée (None si absente)."""
    missing: list[tuple[PythonRequirement, str | None]] = []
    for requirement in requirements:
        version = installed_version(requirement.distribution)
        if version is None or (requirement.minimum and not _meets_minimum(version, requirement.minimum)):
            missing.append((requirement, version))
    return missing


def _meets_minimum(version: str, minimum: tuple[int, ...]) -> bool:
    """Composantes manquantes égales à zéro ; préversion inférieure à sa finale."""
    release = parse_version(version)
    width = max(len(release), len(minimum))
    installed = release + (0,) * (width - len(release))
    expected = minimum + (0,) * (width - len(minimum))
    if installed != expected:
        return installed > expected
    public_version = version.split("+", 1)[0]
    return re.search(r"(?:a|b|rc|dev|pre)\d*", public_version, re.IGNORECASE) is None


__all__ = [
    "PythonRequirement", "REQUIREMENTS_FILE", "installed_version", "parse_version",
    "read_requirements", "unsatisfied_requirements",
]
