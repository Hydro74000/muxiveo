"""Étiquettes de langue : région déduite du titre des pistes."""
from __future__ import annotations


def test_vfq_title_is_quebec_french_and_named_vfq():
    """« VFQ » (version française québécoise) : fr-CA, et {audio-fr-tag} VFQ / VF2."""
    from cli.output_template import _audio_fr_tag
    from core.lang_tags import Rfc5646LanguageTags
    from core.workflows.remux_models import TrackEntry

    assert Rfc5646LanguageTags.regionalize_track_language("fre", "VFQ") == "fr-CA"
    assert Rfc5646LanguageTags.regionalize_track_language("fr", "Français VFQ") == "fr-CA"
    assert Rfc5646LanguageTags.regionalize_track_language("fre", "VFF") == "fr-FR"
    vff = TrackEntry(1, "audio", "EAC3", "", "fre", "VFF")
    vfq = TrackEntry(2, "audio", "EAC3", "", "fre", "VFQ")
    assert _audio_fr_tag([vfq]) == "VFQ"
    assert _audio_fr_tag([vff, vfq]) == "VF2"


def test_legacy_language_only_for_vfq_derived_quebec():
    from core.lang_tags import Rfc5646LanguageTags as L
    assert L.legacy_track_languages("fr-CA", "VFQ") == {"fr-FR"}
    assert L.legacy_track_languages("fre", "Français VFQ 5.1") == {"fr-FR"}
    assert L.legacy_track_languages("fre", "Français (Québec)") == set()
    assert L.legacy_track_languages("fre", "VFF") == set()
