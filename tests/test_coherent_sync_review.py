"""Synchro cohérente en partie non vérifiée : proposée (GUI), acceptée (Hybrid, CLI) ou refusée."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import numpy as np
import pytest

from core.workflows.audio_sync import AudioSyncError, AudioSyncReviewRequired, AudioSyncTrack, format_sync_ranges
from core.workflows.audio_sync_scan import AudioSyncScanner, coherent_sync_accepted
from core.workflows.audio_sync_segments import scan_envelopes
from core.workflows.sync_calibration import SyncCalibration
from tests.test_audio_sync_segments import _edited_pair


def _stable_gap_pair():
    """Même décalage (+200 ms) partout ; 60 s du donneur sans correspondance (passage doublé)."""
    ref, donor = _edited_pair([], [200])
    donor[60000:120000] = np.random.default_rng(419).uniform(0.01, 1, 60000)
    return ref, donor


def test_long_stable_gap_is_collected_for_review():
    ref, donor = _stable_gap_pair()
    review: list[tuple[float, float]] = []
    messages: list[str] = []
    segments, _, _ = scan_envelopes(ref, donor, log=messages.append, review=review)
    assert [s.shift_ms for s in segments] == [200]
    assert len(review) == 1 and review[0][0] < 60000 + 4000 and review[0][1] > 120000 - 4000
    assert any("à relire" in message for message in messages)


def test_long_gap_with_shift_change_is_still_refused():
    ref, donor = _edited_pair([90000], [200, 900])
    donor[60000:120000] = 0
    with pytest.raises(AudioSyncError, match="non vérifié"):
        scan_envelopes(ref, donor, review=[])


def test_unverified_active_start_is_collected_for_review():
    ref, donor = _edited_pair([], [200])
    ref[:50000] = 0
    review: list[tuple[float, float]] = []
    segments, _, _ = scan_envelopes(ref, donor, review=review)
    assert [s.shift_ms for s in segments] == [200]
    assert review[0][0] == 0.0 and review[0][1] > 40000


def _scanner(monkeypatch, *, accept_coherent, downmix_pair=None):
    """Analyse continue forcée (vérification rapide désactivée) sur la paire à trou stable."""
    scanner = AudioSyncScanner(accept_coherent=accept_coherent)
    monkeypatch.setattr(scanner, "measure", lambda *args, **kwargs: (200.0, .95))
    monkeypatch.setattr(scanner, "duration", lambda track: 180.0)
    ref, donor = _stable_gap_pair()
    mixes = ("pan=mono|c0=SL+SR", "pan=mono|c0=SL+SR") if downmix_pair is not None else None
    monkeypatch.setattr(scanner, "_dialogue_free_downmixes", lambda *_a: mixes)

    def envelope(track, cadence_filter=None, downmix=None, abort=None):
        pair = downmix_pair if downmix is not None and downmix_pair is not None else (ref, donor)
        return pair[0] if track.stream_index == 1 else pair[1]

    monkeypatch.setattr(scanner, "envelope", envelope)
    return scanner


def _scan(scanner, warnings=None):
    return scanner.scan(AudioSyncTrack(Path("ref.mkv"), 1), AudioSyncTrack(Path("don.mkv"), 2),
                        detect_cuts=True, warn=None if warnings is None else warnings.append)


def test_scanner_proposes_coherent_sync_for_review(monkeypatch):
    with pytest.raises(AudioSyncReviewRequired) as exc:
        _scan(_scanner(monkeypatch, accept_coherent=False))
    proposal = exc.value
    assert [s.shift_ms for s in proposal.calibration.segments] == [200]
    assert len(proposal.unverified_ms) == 1
    # Axe de sortie : instants de la piste synchronisée + décalage.
    start, stop = proposal.unverified_ms[0]
    assert proposal.output_ranges_ms() == ((start + 200, stop + 200),)
    assert format_sync_ranges(proposal.output_ranges_ms()) in str(proposal)
    # Un appelant qui ne la traite pas la refuse comme toute erreur de synchro.
    assert isinstance(proposal, AudioSyncError)


def test_scanner_accepts_coherent_sync_with_warning(monkeypatch):
    warnings: list[str] = []
    calibration = _scan(_scanner(monkeypatch, accept_coherent=True), warnings)
    assert [s.shift_ms for s in calibration.segments] == [200]
    assert len(warnings) == 1 and "Synchro cohérente acceptée" in warnings[0]


def test_scanner_prefers_fully_verified_dialogue_free_pass(monkeypatch):
    # Mono : passage doublé sans correspondance ; surrounds identiques partout.
    full_pair = _edited_pair([], [200])
    calibration = _scan(_scanner(monkeypatch, accept_coherent=False, downmix_pair=full_pair))
    assert [s.shift_ms for s in calibration.segments] == [200]


def test_unknown_layout_keeps_mono_proposal(monkeypatch):
    # Disposition non reconnue (6.0…) : la sonde du mixage échoue, la proposition reste.
    scanner = _scanner(monkeypatch, accept_coherent=False)

    def unknown_layout(*_a):
        raise AudioSyncError("Disposition multicanal non reconnue ; revue manuelle de la synchro nécessaire.")

    monkeypatch.setattr(scanner, "_dialogue_free_downmixes", unknown_layout)
    with pytest.raises(AudioSyncReviewRequired) as exc:
        _scan(scanner)
    assert [s.shift_ms for s in exc.value.calibration.segments] == [200]


def test_coherent_sync_accepted_unless_rejected():
    assert coherent_sync_accepted(SimpleNamespace())
    assert not coherent_sync_accepted(SimpleNamespace(reject_coherent_sync=True))
    from cli.parser import build_parser
    from cli.options import CommonOptions
    parser = build_parser()
    args = parser.parse_args(["hybrid", "-o", "out", "--reject-coherent-sync"])
    assert not coherent_sync_accepted(args) and CommonOptions.from_namespace(args).reject_coherent_sync
    assert coherent_sync_accepted(parser.parse_args(["hybrid", "-o", "out"]))


def _review_panel(choice):
    from ui.panels.remux_panel import panel
    target = SimpleNamespace(entry_id="target", mkv_tid=2, file_id="f1")
    reference = SimpleNamespace(entry_id="reference", mkv_tid=1, file_id="f0")
    calls: dict[str, list] = {"log": [], "finished": [], "done": [], "studio": []}
    fake = SimpleNamespace(
        _track_table=SimpleNamespace(current_tracks=lambda: [target, reference]),
        log_message=SimpleNamespace(emit=lambda level, text: calls["log"].append((level, text))),
        audio_sync_finished=SimpleNamespace(emit=lambda ok, _meta: calls["finished"].append(ok)),
        _confirm_coherent_sync=lambda *_a: choice,
        _on_audio_sync_done=lambda *args: calls["done"].append(args),
        _review_sync_proposal=lambda *args: calls["studio"].append(args),
        _log_sync_proposal_dropped=lambda _target: calls["log"].append(("INFO", "non appliquée")),
    )
    calibration = SyncCalibration.linear(-5466)
    ranges = ((155998.0, 188000.0),)
    panel.RemuxPanel._on_audio_sync_review(cast(Any, fake), "target", "reference", calibration, ranges)
    return calls, target, reference, calibration, ranges


def test_panel_applies_confirmed_proposal_and_logs_user_validation():
    calls, _target, _reference, calibration, ranges = _review_panel("apply")
    assert calls["done"] == [("target", "reference", -5466, calibration.confidence, calibration)]
    assert [level for level, _text in calls["log"]] == ["WARN", "INFO"]
    assert format_sync_ranges(ranges) in calls["log"][1][1]
    # Terminée par _on_audio_sync_done (non simulé ici).
    assert calls["finished"] == []


def test_panel_review_opens_studio_with_proposal():
    calls, target, reference, calibration, ranges = _review_panel("review")
    # L'opération de synchro reste ouverte pendant la relecture.
    assert calls["finished"] == []
    assert calls["studio"] == [(target, reference, calibration, ranges)]
    assert calls["done"] == []


class _AcceptingStudio:
    """Synchro Studio validé tel quel ; mémorise ses paramètres."""

    opened: list[dict] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        _AcceptingStudio.opened.append(kwargs)

    def exec(self):
        from PySide6.QtWidgets import QDialog
        return QDialog.DialogCode.Accepted

    def result_calibration(self):
        calibration = self.kwargs["calibration"]
        return calibration, round(calibration.segments[0].shift_ms)


@pytest.mark.parametrize("choice", ["apply", "review"])
def test_apply_and_review_share_the_same_application(qt_app, tmp_path, monkeypatch, choice):
    """Audit : référence à +250 ms, proposition à −320 ms ; les deux parcours donnent le même résultat :
    la cible suit la position de la référence dans la sortie (−320 + 250)."""
    from core.config import AppConfig
    from tests.test_remux import _COLOR_A, _COLOR_B, _track
    from ui.panels.remux_panel.panel import RemuxPanel
    from ui.panels.remux_panel.widgets import sync_studio_dialog as mod

    _AcceptingStudio.opened.clear()
    monkeypatch.setattr(mod, "SyncStudioDialog", _AcceptingStudio)
    panel = RemuxPanel(AppConfig())
    try:
        target = _track(1, "audio", file_id="target")
        reference = _track(1, "audio", file_id="reference")
        panel._track_table.append_tracks(_COLOR_A, [target])
        panel._track_table.append_tracks(_COLOR_B, [reference])
        monkeypatch.setattr(panel, "_find_source", lambda file_id: SimpleNamespace(path=tmp_path / f"{file_id}.mkv"))
        monkeypatch.setattr(panel, "_confirm_coherent_sync", lambda *_a: choice)
        finished: list[bool] = []
        panel.audio_sync_finished.connect(lambda ok, _meta: finished.append(ok))
        panel._apply_source_sync_offset("reference", 250)
        assert reference.time_shift_ms == 250

        ranges = ((1000.0, 40000.0),)
        panel._on_audio_sync_review(target.entry_id, reference.entry_id, SyncCalibration.linear(-320), ranges)

        assert (reference.time_shift_ms, target.time_shift_ms) == (250, -70)
        assert finished == [True]
        assert [kwargs["review_ranges_ms"] for kwargs in _AcceptingStudio.opened] == ([ranges] if choice == "review" else [])
    finally:
        panel.close()


def test_manual_studio_validation_follows_a_shifted_reference(qt_app, tmp_path, monkeypatch):
    """Studio ouvert à la main : réglage face à l'audio d'origine de la référence, cible placée comme
    pour une synchro auto (elle suit la position de la référence dans la sortie)."""
    from core.config import AppConfig
    from core.i18n import translate_text
    from tests.test_remux import _COLOR_A, _COLOR_B, _track
    from ui.panels.remux_panel.panel import RemuxPanel
    from ui.panels.remux_panel.widgets import sync_studio_dialog as mod

    class _ManualStudio(_AcceptingStudio):
        def result_calibration(self):
            return SyncCalibration.linear(-320), -320

    monkeypatch.setattr(mod, "SyncStudioDialog", _ManualStudio)
    panel = RemuxPanel(AppConfig())
    try:
        target = _track(1, "audio", file_id="target")
        reference = _track(1, "audio", file_id="reference")
        panel._track_table.append_tracks(_COLOR_A, [target])
        panel._track_table.append_tracks(_COLOR_B, [reference])
        monkeypatch.setattr(panel, "_find_source", lambda file_id: SimpleNamespace(path=tmp_path / f"{file_id}.mkv"))
        monkeypatch.setattr(panel, "_audio_sync_reference_choices", lambda _entry: [("VO", reference)])
        panel._apply_source_sync_offset("reference", 250)
        logs: list[tuple[str, str]] = []
        panel.log_message.connect(lambda level, text: logs.append((level, text)))

        _AcceptingStudio.opened.clear()
        panel._on_sync_studio_requested(target)

        # Affichage relatif à l'audio d'origine de la référence : cible non décalée = −250 ms.
        assert [s.shift_ms for s in _AcceptingStudio.opened[0]["calibration"].segments] == [-250]
        assert (reference.time_shift_ms, target.time_shift_ms) == (250, -70)
        assert ("INFO", translate_text(
            "La référence « {reference} » est elle-même synchronisée : la cible suit sa position dans la sortie.",
            reference="reference",
        )) in logs
    finally:
        panel.close()


def test_review_abandoned_in_studio_leaves_track_unchanged(qt_app, tmp_path):
    from PySide6.QtWidgets import QDialog
    from ui.panels.remux_panel import panel as panel_mod

    class _RejectingStudio(_AcceptingStudio):
        def exec(self):
            return QDialog.DialogCode.Rejected

    target = SimpleNamespace(entry_id="target", mkv_tid=2, file_id="f1")
    reference = SimpleNamespace(entry_id="reference", mkv_tid=1, file_id="f0")
    finished: list[bool] = []
    done: list[tuple] = []
    fake = SimpleNamespace(
        _find_source=lambda file_id: SimpleNamespace(path=tmp_path / f"{file_id}.mkv"),
        log_message=SimpleNamespace(emit=lambda *_a: None),
        audio_sync_finished=SimpleNamespace(emit=lambda ok, _meta: finished.append(ok)),
        _on_audio_sync_done=lambda *args: done.append(args),
        _sync_studio_dialog=lambda *_a, **_k: _RejectingStudio(),
        _log_sync_proposal_dropped=lambda _target: None,
    )
    panel_mod.RemuxPanel._review_sync_proposal(
        cast(Any, fake), cast(Any, target), cast(Any, reference), SyncCalibration.linear(-320), ((0.0, 1.0),),
    )
    assert finished == [False] and done == []


def test_panel_cancelled_proposal_leaves_track_unchanged():
    calls, *_ = _review_panel("cancel")
    assert calls["finished"] == [False] and calls["done"] == [] and calls["studio"] == []


def test_studio_shows_review_zones_until_a_new_result(qt_app, tmp_path, monkeypatch):
    from core.i18n import translate_text
    from tests.test_sync_calibration_edits import _studio
    from ui.panels.remux_panel.widgets import sync_studio_dialog as mod

    monkeypatch.setattr(mod.SyncStudioDialog, "_load_segment_audio_async", lambda *_a, **_k: None)
    ranges = ((155998.0, 188000.0), (811999.0, 815000.0))
    _mod, dialog = _studio(tmp_path, monkeypatch, calibration=SyncCalibration.linear(-5466))
    dialog._on_auto_sync_review(SyncCalibration.linear(-5466), ranges)
    assert dialog.review_ranges_ms == ranges
    assert dialog._review_row.isVisibleTo(dialog)
    assert dialog._apply_btn is not None
    assert dialog._apply_btn.text() == translate_text("Valider la synchro des pistes")
    # Zone longue : 20 s affichées depuis son début ; zone courte : centrée.
    assert dialog.goto_edit.text() == SyncCalibration.format_timestamp(165998)
    dialog._goto_review_range(1)
    assert dialog.goto_edit.text() == SyncCalibration.format_timestamp(813500)
    dialog._on_auto_sub_sync_ready(SyncCalibration.linear(120))
    assert dialog.review_ranges_ms == () and not dialog._review_row.isVisibleTo(dialog)
    assert dialog._apply_btn.text() == translate_text("Appliquer la synchronisation")
    dialog.close()
