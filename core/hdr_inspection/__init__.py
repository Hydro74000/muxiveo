"""Inspection HDR à la demande (HDR Studio, CLI) : en-têtes, métadonnées dynamiques, cohérence."""
from core.hdr_inspection.audit import Finding, Severity, audit, scene_alignment
from core.hdr_inspection.cache import InspectionCache
from core.hdr_inspection.dovi import ActiveArea, DoviAnalysis, L6Set, parse_dovi_export
from core.hdr_inspection.hdr10p import Hdr10PlusAnalysis, parse_hdr10plus_json
from core.hdr_inspection.inspector import (
    DynamicInspector,
    InspectionCancelled,
    InspectionReport,
    snapshot_report,
)
from core.hdr_inspection.snapshot import DoviRecord, HdrSnapshot, MasteringDisplay, build_snapshot

__all__ = [
    "ActiveArea",
    "DoviAnalysis",
    "DoviRecord",
    "DynamicInspector",
    "Finding",
    "Hdr10PlusAnalysis",
    "HdrSnapshot",
    "InspectionCache",
    "InspectionCancelled",
    "InspectionReport",
    "L6Set",
    "MasteringDisplay",
    "Severity",
    "audit",
    "build_snapshot",
    "parse_dovi_export",
    "parse_hdr10plus_json",
    "scene_alignment",
    "snapshot_report",
]
