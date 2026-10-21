"""Nom de sortie rendu par le template d'un profil décisionnel (panneau Conteneur)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cli.output_template import build_output_context, render_output_filename
from core.i18n import translate_text
from core.profiles.decision import profile_output_template
from core.workflows.remux_models import TrackEntry

if TYPE_CHECKING:
    from ui.panels.remux_panel.panel import RemuxPanel

_TMDB_FIELDS = ("{title", "{year", "{episode_title", "{season", "{episode")


@dataclass
class ProfileOutputName:
    """Template suivi tant que le chemin de sortie est celui qu'il a rendu."""

    template: str
    variables: dict[str, Any] = field(default_factory=dict)
    folder: Path = Path()
    path: str = ""


def profile_variables(profile: Mapping[str, Any]) -> dict[str, Any]:
    variables = profile.get("variables")
    return dict(variables) if isinstance(variables, dict) else {}


def uses_tmdb_fields(template: str) -> bool:
    return any(token in template for token in _TMDB_FIELDS)


def output_context(panel: RemuxPanel, tracks: list[TrackEntry], variables: dict[str, Any]) -> dict[str, Any] | None:
    """Contexte de nommage : première source, pistes finales dans l'ordre du tableau, fiche TMDB choisie."""
    if not panel._source_files:
        return None
    index_by_file_id = {source.id: index for index, source in enumerate(panel._source_files)}
    order = [
        (index_by_file_id[track.file_id], track.mkv_tid, track.entry_id)
        for track in tracks
        if track.enabled and track.file_id in index_by_file_id
    ]
    return build_output_context(
        panel._source_files[0].path, panel._tmdb_details, tracks=tracks, track_order=order, variables=variables,
    )


def render_profile_output_name(panel: RemuxPanel, profile: Mapping[str, Any], tracks: list[TrackEntry]) -> str:
    """Nom relatif rendu pour ``tracks`` (aperçu) ; vide sans template, sans source ou template invalide."""
    template = profile_output_template(profile)
    context = output_context(panel, tracks, profile_variables(profile)) if template else None
    if context is None:
        return ""
    try:
        return render_output_filename(template, context)
    except ValueError:
        return ""


def track_profile_output(panel: RemuxPanel, profile: Mapping[str, Any]) -> None:
    """Nom du profil appliqué au chemin de sortie, dans le dossier de sortie courant."""
    template = profile_output_template(profile)
    if not template:
        panel._profile_output = None
        return
    current = panel._output_edit.text().strip()
    previous = panel._profile_output
    # Chemin encore piloté par un profil : sa base est gardée (un modèle à sous-dossiers ne s'imbrique pas).
    if previous is not None and current == previous.path:
        folder = previous.folder
    else:
        folder = Path(current).parent if current else panel._config.output_dir
    panel._profile_output = ProfileOutputName(template, profile_variables(profile), folder)
    refresh_profile_output(panel, force=True)
    if panel._profile_output is not None and panel._tmdb_details is None and uses_tmdb_fields(template):
        panel.log_message.emit("INFO", translate_text(
            "Nom de sortie du profil : sélectionnez la fiche TMDB pour renseigner titre, année et épisode."))


def refresh_profile_output(panel: RemuxPanel, *, force: bool = False) -> None:
    """Le nom suit pistes et fiche TMDB ; un chemin modifié à la main n'est plus remplacé."""
    state = panel._profile_output
    if state is None or not panel._source_files:
        return
    current = panel._output_edit.text().strip()
    if not force and current != state.path:
        panel._profile_output = None
        return
    context = output_context(panel, panel._track_table.current_tracks(), state.variables)
    if context is None:
        return
    try:
        name = render_output_filename(state.template, context)
    except ValueError as exc:
        panel._profile_output = None
        panel.log_message.emit("WARN", translate_text(
            "Nom de sortie du profil ignoré : {err}", err=translate_text(str(exc))))
        return
    state.path = str(state.folder / name)
    if state.path != current:
        panel._output_edit.setText(state.path)


def stop_profile_output(panel: RemuxPanel) -> None:
    panel._profile_output = None


__all__ = [
    "ProfileOutputName",
    "output_context",
    "refresh_profile_output",
    "render_profile_output_name",
    "stop_profile_output",
    "track_profile_output",
    "uses_tmdb_fields",
]
