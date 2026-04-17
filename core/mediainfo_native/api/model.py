"""Structured internal model for module-level consumption."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class FieldValue:
    name: str
    value: str


@dataclass(slots=True)
class Track:
    kind: str
    fields: dict[str, FieldValue] = field(default_factory=dict)


@dataclass(slots=True)
class MediaDocument:
    source: str
    tracks: list[Track] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class ReportTrackView:
    kind: str
    fields: OrderedDict[str, str] = field(default_factory=OrderedDict)


@dataclass(slots=True)
class ReportView:
    source: str
    tracks: list[ReportTrackView] = field(default_factory=list)

    def tracks_by_kind(self, kind: str) -> list[ReportTrackView]:
        kind_lower = kind.lower()
        return [track for track in self.tracks if track.kind.lower() == kind_lower]

    def first_track(self, kind: str) -> ReportTrackView | None:
        tracks = self.tracks_by_kind(kind)
        return tracks[0] if tracks else None


def from_report(report: Any, metadata: dict[str, str] | None = None) -> MediaDocument:
    """Convert a MediaReport-like object into the stable structured model."""
    tracks: list[Track] = []
    for t in getattr(report, "tracks", []):
        fields = {
            key: FieldValue(name=key, value=str(value))
            for key, value in getattr(t, "fields", {}).items()
            if key and not str(key).startswith("_")
        }
        tracks.append(Track(kind=str(getattr(t, "kind", "Other")), fields=fields))
    return MediaDocument(
        source=str(getattr(report, "source", "")),
        tracks=tracks,
        metadata=dict(metadata or {}),
    )


def to_report_view(document: MediaDocument) -> ReportView:
    """
    Convert the stable model to a renderer-oriented report view.
    """
    tracks: list[ReportTrackView] = []
    for track in document.tracks:
        ordered_fields: OrderedDict[str, str] = OrderedDict()
        for key, field_value in track.fields.items():
            ordered_fields[key] = str(field_value.value)
        tracks.append(ReportTrackView(kind=track.kind, fields=ordered_fields))
    return ReportView(source=document.source, tracks=tracks)


def from_legacy_report(report: Any, metadata: dict[str, str] | None = None) -> MediaDocument:
    """
    Backward-compatible alias.
    """
    return from_report(report, metadata=metadata)
