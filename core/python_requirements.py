"""
core/python_requirements.py — Dépendances Python d'exécution (source unique : requirements.txt).

Le nom de distribution (pip) et le module importable peuvent différer
(``PySide6`` s'importe sous ce nom exact, avec sa casse). Une borne minimale,
des versions exclues (``!=x.y.z``) et une borne haute (``<x.y``) sont vérifiées
sur la version installée de la distribution (``importlib.metadata``) : un import
réussi ne prouve pas la version. Les conditions ``python_version < "x.y"``
et ``python_version >= "x.y"`` sélectionnent les dépendances de l'interpréteur.

Aucun verrouillage (décision DE, 2026-11-26) : les versions supérieures sont admises ; seule une
incompatibilité connue est déclarée, par exclusion ou borne haute, avec sa raison dans un commentaire
juste au-dessus de la ligne : ``# != 6.12.0 : raison`` ou ``# < 7 : raison`` (une ligne par contrainte).
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

REQUIREMENTS_FILE = Path(__file__).resolve().parents[1] / "requirements.txt"
#: Outils de build des paquets (PyInstaller, Pillow), mêmes règles que ``requirements.txt``.
BUILD_REQUIREMENTS_FILE = REQUIREMENTS_FILE.with_name("requirements-build.txt")

# Module importable quand il diffère du nom de distribution (clé en minuscules).
_MODULE_NAMES = {"pyside6": "PySide6"}
_LINE_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:>=\s*(?P<minimum>[0-9][0-9.]*))?"
    r"(?P<excluded>(?:\s*,?\s*!=\s*[0-9][0-9.]*)*)"
    r"(?:\s*,?\s*<\s*(?P<upper>[0-9][0-9.]*))?$"
)
#: Raison d'une contrainte (commentaire au-dessus de la ligne) : ``# != 6.12.0 : …`` ou ``# < 7 : …``.
_REASON_RE = re.compile(r"^#\s*(?P<op>!=|<)\s*(?P<version>[0-9][0-9.]*)\s*:\s*(?P<reason>.+)$")
_MARKER_RE = re.compile(
    r"""^python_version\s*(?P<operator><|>=)\s*(?P<quote>["'])(?P<version>\d+\.\d+)(?P=quote)$"""
)


@dataclass(frozen=True)
class PythonRequirement:
    """Ligne de requirements.txt : distribution, borne minimale, versions exclues et module importable."""

    distribution: str
    spec: str
    minimum: tuple[int, ...] | None
    module: str
    excluded: tuple[tuple[int, ...], ...] = ()
    #: Borne haute exclue (``<x.y``) : première version connue incompatible.
    upper: tuple[int, ...] | None = None
    #: Raison de chaque contrainte d'incompatibilité (``"!=6.12.0"`` / ``"<7"`` → texte du commentaire).
    reasons: tuple[tuple[str, str], ...] = ()

    def reason_for(self, constraint: str) -> str:
        return dict(self.reasons).get(constraint, "")


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


def read_requirements(
    path: Path = REQUIREMENTS_FILE, *, python_version: tuple[int, int] | None = None,
) -> list[PythonRequirement]:
    """Dépendances actives : borne minimale, exclusions et condition optionnelle sur Python."""
    if python_version is None:
        python_version = (sys.version_info.major, sys.version_info.minor)
    requirements: list[PythonRequirement] = []
    pending_reasons: dict[str, str] = {}
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        reason = _REASON_RE.match(stripped)
        if reason is not None:
            pending_reasons[f"{reason.group('op')}{reason.group('version')}"] = reason.group("reason").strip()
            continue
        line = raw.split("#", 1)[0].strip()
        if not line:
            if not stripped:
                pending_reasons = {}  # ligne vide : les raisons précédentes ne valent pas pour la suite
            continue
        spec, separator, marker = line.partition(";")
        match = _LINE_RE.fullmatch(spec.strip())
        if match is None:
            raise ValueError(f"Dépendance non prise en charge dans {path.name} : {line}")
        if separator:
            condition = _MARKER_RE.fullmatch(marker.strip())
            if condition is None:
                raise ValueError(f"Condition non prise en charge dans {path.name} : {marker.strip()}")
            boundary = parse_version(condition.group("version"))
            below = python_version < boundary
            if below != (condition.group("operator") == "<"):
                pending_reasons = {}
                continue
        name = match.group("name")
        minimum = match.group("minimum")
        excluded = re.findall(r"!=\s*([0-9][0-9.]*)", match.group("excluded"))
        upper = match.group("upper")
        constraints = [f"!={version}" for version in excluded] + ([f"<{upper}"] if upper else [])
        clauses = ([f">={minimum}"] if minimum else []) + constraints
        requirements.append(PythonRequirement(
            distribution=name,
            spec=name + ",".join(clauses),
            minimum=parse_version(minimum) if minimum else None,
            module=_MODULE_NAMES.get(name.lower(), name.replace("-", "_")),
            excluded=tuple(parse_version(version) for version in excluded),
            upper=parse_version(upper) if upper else None,
            reasons=tuple((constraint, pending_reasons[constraint]) for constraint in constraints
                          if constraint in pending_reasons),
        ))
        pending_reasons = {}
    return requirements


def undeclared_constraints(requirement: PythonRequirement) -> list[str]:
    """Contraintes d'incompatibilité sans raison déclarée (règle DE : toute exclusion ou borne haute est
    justifiée)."""
    constraints = [f"!={'.'.join(map(str, version))}" for version in requirement.excluded]
    if requirement.upper is not None:
        constraints.append(f"<{'.'.join(map(str, requirement.upper))}")
    declared = {constraint for constraint, _reason in requirement.reasons}
    return [constraint for constraint in constraints if constraint not in declared]


def installed_version(distribution: str) -> str | None:
    """Version installée de la distribution, None si absente."""
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return None


def unsatisfied_requirements(
    requirements: list[PythonRequirement],
) -> list[tuple[PythonRequirement, str | None]]:
    """Dépendances absentes, trop anciennes, exclues ou trop récentes, avec la version trouvée (None si absente)."""
    missing: list[tuple[PythonRequirement, str | None]] = []
    for requirement in requirements:
        version = installed_version(requirement.distribution)
        if (
            version is None
            or (requirement.minimum and not _meets_minimum(version, requirement.minimum))
            or is_excluded(version, requirement)
            or is_too_recent(version, requirement)
        ):
            missing.append((requirement, version))
    return missing


def is_too_recent(version: str, requirement: PythonRequirement) -> bool:
    """Vrai si la version installée atteint la borne haute (``<x.y`` : x.y et suivantes refusées)."""
    if requirement.upper is None:
        return False
    return _meets_minimum(version, requirement.upper)


def rejection_reason(version: str | None, requirement: PythonRequirement) -> str:
    """Motif du refus de la version installée, raison déclarée comprise (setup, packaging)."""
    if version is None:
        return "missing"
    if requirement.minimum and not _meets_minimum(version, requirement.minimum):
        return f"{version} too old"
    release = parse_version(version)
    for excluded in requirement.excluded:
        width = max(len(release), len(excluded))
        if release + (0,) * (width - len(release)) == excluded + (0,) * (width - len(excluded)):
            constraint = f"!={'.'.join(map(str, excluded))}"
            reason = requirement.reason_for(constraint)
            return f"{version} excluded (known incompatibility{': ' + reason if reason else ''})"
    if is_too_recent(version, requirement):
        assert requirement.upper is not None
        constraint = f"<{'.'.join(map(str, requirement.upper))}"
        reason = requirement.reason_for(constraint)
        return f"{version} too recent (known incompatibility{': ' + reason if reason else ''})"
    return f"{version} accepted"


def rejection_lines(rejected: list[tuple[PythonRequirement, str | None]]) -> list[str]:
    """Une ligne par dépendance refusée, motif et raison déclarée compris (journal du packaging)."""
    return [f"{requirement.distribution} : {rejection_reason(version, requirement)}" for requirement, version in rejected]


def is_excluded(version: str, requirement: PythonRequirement) -> bool:
    """Vrai si la version installée est exclue (``!=x.y.z`` : composantes manquantes égales à zéro)."""
    release = parse_version(version)
    for excluded in requirement.excluded:
        width = max(len(release), len(excluded))
        if release + (0,) * (width - len(release)) == excluded + (0,) * (width - len(excluded)):
            return True
    return False


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
    "BUILD_REQUIREMENTS_FILE", "PythonRequirement", "REQUIREMENTS_FILE", "installed_version", "is_excluded", "is_too_recent", "parse_version",
    "read_requirements", "rejection_lines", "rejection_reason", "undeclared_constraints", "unsatisfied_requirements",
]
