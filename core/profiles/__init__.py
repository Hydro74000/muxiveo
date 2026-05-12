"""Reusable profile helpers for Mediarecode."""

from __future__ import annotations

from core.profiles.decision import (
    DECISION_KEYWORDS,
    DECISION_PROFILE_KIND,
    DECISION_PROFILE_VERSION,
    DecisionProfileManager,
    DecisionProfileResult as DecisionProfileV1Result,
    apply_decision_profile as apply_decision_profile_v1,
    build_video_flags_hex,
    remux_config_to_decision_profile as remux_config_to_decision_profile_v1,
    render_title_pattern,
    validate_decision_profile,
    video_flags_hex,
)
from core.profiles.hybrid import (
    DecisionProfileResult,
    HybridProfileManager,
    HybridResolutionError,
    apply_decision_profile,
    apply_track_spec,
    decision_profile_from_legacy,
    match_track_selector,
    remux_config_to_decision_profile,
    remux_config_to_hybrid_job,
    resolve_track_selector,
    track_selector_for_entry,
)

__all__ = [
    "DECISION_PROFILE_KIND",
    "DECISION_PROFILE_VERSION",
    "DECISION_KEYWORDS",
    "DecisionProfileManager",
    "DecisionProfileResult",
    "DecisionProfileV1Result",
    "HybridProfileManager",
    "HybridResolutionError",
    "apply_decision_profile",
    "apply_decision_profile_v1",
    "apply_track_spec",
    "build_video_flags_hex",
    "decision_profile_from_legacy",
    "match_track_selector",
    "remux_config_to_decision_profile",
    "remux_config_to_decision_profile_v1",
    "remux_config_to_hybrid_job",
    "render_title_pattern",
    "resolve_track_selector",
    "track_selector_for_entry",
    "validate_decision_profile",
    "video_flags_hex",
]
