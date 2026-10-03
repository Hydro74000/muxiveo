"""Tests pour le garde-fou frame count et helpers metadata_inject."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from core.workflows.encode.runtime.frame_count_guard import (
    FrameCountAudit,
    FrameCountAuditError,
    FrameCountGuard,
    MetadataAdjustment,
)
from core.workflows.encode.runtime.metadata_inject import _build_dovi_record_from_rpu

_TRIM = MetadataAdjustment.TRIM_TAIL
_EXACT = MetadataAdjustment.EXACT


def _make_completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class TestFrameCountAudit:
    def test_aligned_strict(self):
        audit = FrameCountAudit(source=1000, encoded=1000, rpu=1000, hdr10p=1000)
        ok, _ = audit.is_aligned()
        assert ok

    def test_rpu_surplus_within_tolerance_needs_trim_tail(self):
        audit = FrameCountAudit(source=1000, encoded=1000, rpu=1002, hdr10p=1000)
        assert audit.is_aligned(adjustment=_TRIM, tolerance=4)[0]
        ok, msg = audit.is_aligned(adjustment=_EXACT, tolerance=4)
        assert not ok and "politique exacte" in msg

    def test_rpu_shortfall_is_never_accepted(self):
        audit = FrameCountAudit(source=1000, encoded=1000, rpu=998, hdr10p=1000)
        ok, msg = audit.is_aligned(adjustment=_TRIM, tolerance=4)
        assert not ok and "aucune trame" in msg

    def test_encoded_mismatch_blocks(self):
        audit = FrameCountAudit(source=1000, encoded=999, rpu=1000, hdr10p=1000)
        ok, msg = audit.is_aligned()
        assert not ok
        assert "encoded" in msg

    def test_rpu_beyond_tolerance_blocks(self):
        audit = FrameCountAudit(source=1000, encoded=1000, rpu=1007, hdr10p=1000)
        ok, msg = audit.is_aligned(adjustment=_TRIM, tolerance=4)
        assert not ok
        assert "tolérance" in msg

    def test_unknown_source_blocks(self):
        audit = FrameCountAudit(source=None, encoded=1000, rpu=1000, hdr10p=1000)
        ok, _ = audit.is_aligned()
        assert not ok


class TestFrameCountGuardAudit:
    def test_audit_combines_all_sources(self, tmp_path):
        rpu = tmp_path / "rpu.bin"
        rpu.write_bytes(b"x")
        hdr = tmp_path / "hdr10p.json"
        hdr.write_text(json.dumps({"SceneInfo": [{"a": 1}, {"a": 2}, {"a": 3}]}))

        guard = FrameCountGuard()
        # mediainfo répond directement → pas de fallback ffprobe nécessaire.
        with patch("subprocess.run") as run:
            run.side_effect = [
                _make_completed(stdout="3\n"),         # mediainfo source
                _make_completed(stdout="{}"),          # ffprobe durée/cadence (plausibilité)
                _make_completed(stdout="3\n"),         # mediainfo encoded
                _make_completed(stdout="{}"),          # ffprobe durée/cadence (plausibilité)
                _make_completed(stdout="Frames: 3\n"), # dovi_tool info
            ]
            audit = guard.audit(
                source=tmp_path / "src.mkv",
                encoded=tmp_path / "enc.hevc",
                rpu_bin=rpu,
                hdr10p_json=hdr,
            )
        assert audit == FrameCountAudit(source=3, encoded=3, rpu=3, hdr10p=3)

    def test_audit_ignores_stale_matroska_statistics(self, tmp_path):
        """mediainfo annonce 288 frames pour 4,2 s à 23,976 fps : comptage ffprobe."""
        guard = FrameCountGuard()
        probe = json.dumps({"streams": [{"avg_frame_rate": "24000/1001"}], "format": {"duration": "4.212"}})
        with patch("subprocess.run") as run:
            run.side_effect = [
                _make_completed(stdout="288\n"),   # mediainfo source (tags périmés)
                _make_completed(stdout=probe),     # ffprobe durée/cadence
                _make_completed(stdout="\n"),      # ffprobe nb_frames (absent en MKV)
                _make_completed(stdout="98\n"),    # ffprobe count_packets
                _make_completed(stdout="98\n"),    # mediainfo encoded
                _make_completed(stdout="{}"),      # HEVC brut : durée absente
            ]
            audit = guard.audit(source=tmp_path / "src.mkv", encoded=tmp_path / "enc.hevc",
                                rpu_bin=None, hdr10p_json=None)
        assert audit.source == 98 and audit.encoded == 98

    @pytest.mark.parametrize("duration", ["39.96", None])
    def test_audit_recounts_small_mismatch_even_when_statistics_look_plausible(self, tmp_path, duration):
        source, encoded = tmp_path / "src.mkv", tmp_path / "enc.hevc"

        def run(cmd, **_kwargs):
            if "--Inform=Video;%FrameCount%" in cmd:
                return _make_completed(stdout="1000" if cmd[-1] == str(source) else "999")
            if "-count_packets" in cmd:
                return _make_completed(stdout="999")
            return _make_completed(stdout=json.dumps({
                "streams": [{"avg_frame_rate": "25/1"}], "format": {"duration": duration},
            }))

        guard = FrameCountGuard()
        with patch("subprocess.run", side_effect=run):
            audit = guard.audit(source=source, encoded=encoded)
        assert audit.source == audit.encoded == 999
        assert guard.enforce(audit, adjustment=_TRIM) == audit

    @pytest.mark.parametrize("counted", [(1000, 999), (None, None)])
    def test_audit_does_not_hide_real_frame_loss_or_failed_recount(self, tmp_path, counted):
        guard = FrameCountGuard()
        with patch.object(guard, "_read_video_frame_count", side_effect=[1000, 999]), \
             patch.object(guard, "_ffprobe_count_packets", side_effect=counted), \
             patch.object(guard, "_ffprobe_count_frames", return_value=None):
            audit = guard.audit(source=tmp_path / "src.mkv", encoded=tmp_path / "enc.hevc")
        with pytest.raises(FrameCountAuditError, match="non frame-preserving"):
            guard.enforce(audit, adjustment=_TRIM)

    def test_failed_recount_with_partial_stdout_does_not_hide_frame_loss(self, tmp_path):
        guard = FrameCountGuard()
        partial = _make_completed(stdout="999\n", stderr="read error", returncode=1)
        with patch.object(guard, "_read_video_frame_count", side_effect=[1000, 999]), \
             patch("subprocess.run", return_value=partial):
            audit = guard.audit(source=tmp_path / "src.mkv", encoded=tmp_path / "enc.hevc")
        assert (audit.source, audit.encoded) == (1000, 999)
        with pytest.raises(FrameCountAuditError, match="non frame-preserving"):
            guard.enforce(audit, adjustment=_TRIM)

    @pytest.mark.parametrize("reader", ["_mediainfo_frame_count", "_ffprobe_nb_frames", "_ffprobe_count_frames"])
    def test_count_readers_reject_failed_processes(self, tmp_path, reader):
        guard = FrameCountGuard()
        with patch("subprocess.run", return_value=_make_completed(stdout="999", returncode=1)):
            assert getattr(guard, reader)(tmp_path / "damaged.mkv") is None

    def test_audit_falls_back_to_ffprobe_nb_frames(self, tmp_path):
        guard = FrameCountGuard()
        # mediainfo absent (FileNotFoundError) → bascule ffprobe nb_frames OK.
        responses = [
            FileNotFoundError(),                # mediainfo source
            _make_completed(stdout="500\n"),    # ffprobe nb_frames source
            FileNotFoundError(),                # mediainfo encoded
            _make_completed(stdout="500\n"),    # ffprobe nb_frames encoded
        ]

        def fake_run(*_args, **_kwargs):
            r = responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        with patch("subprocess.run", side_effect=fake_run):
            audit = guard.audit(source=tmp_path / "a", encoded=tmp_path / "b")
        assert audit.source == 500 and audit.encoded == 500

    def test_audit_uses_known_encoded_frames(self, tmp_path):
        guard = FrameCountGuard()
        with patch.object(guard, "_read_video_frame_count", return_value=1200) as mock_read:
            audit = guard.audit(
                source=tmp_path / "src.mkv",
                encoded=tmp_path / "enc.hevc",
                known_encoded_frames=1200,
            )
        assert audit.source == 1200
        assert audit.encoded == 1200
        mock_read.assert_called_once_with(tmp_path / "src.mkv")

    def test_audit_falls_back_to_count_packets_when_nb_frames_empty(self, tmp_path):
        guard = FrameCountGuard()
        # mediainfo OK pour la source, mais nb_frames vide pour l'encoded
        # (typique d'un HEVC brut sans index) → on tombe sur count_packets.
        responses = [
            _make_completed(stdout="1000\n"),   # mediainfo source
            _make_completed(stdout="{}"),      # ffprobe durée/cadence source
            _make_completed(stdout="\n"),       # mediainfo encoded (vide)
            _make_completed(stdout="N/A\n"),    # ffprobe nb_frames encoded
            _make_completed(stdout="1000\n"),   # ffprobe count_packets encoded
        ]
        with patch("subprocess.run", side_effect=responses) as run:
            audit = guard.audit(source=tmp_path / "a", encoded=tmp_path / "b")
        assert audit.source == 1000 and audit.encoded == 1000
        assert "-count_packets" in run.call_args.args[0]

    def test_audit_handles_all_readers_missing(self, tmp_path):
        guard = FrameCountGuard()
        with patch("subprocess.run", side_effect=FileNotFoundError):
            audit = guard.audit(source=tmp_path / "a", encoded=tmp_path / "b")
        assert audit.source is None
        assert audit.encoded is None

    def test_audit_skips_missing_optional_inputs(self, tmp_path):
        guard = FrameCountGuard()
        with patch("subprocess.run") as run:
            run.side_effect = [
                _make_completed(stdout="100\n"),
                _make_completed(stdout="{}"),
                _make_completed(stdout="100\n"),
                _make_completed(stdout="{}"),
            ]
            audit = guard.audit(
                source=tmp_path / "src.mkv",
                encoded=tmp_path / "enc.hevc",
                rpu_bin=None,
                hdr10p_json=None,
            )
        assert audit.rpu is None and audit.hdr10p is None
        assert audit.source == 100 and audit.encoded == 100


class TestFrameCountGuardEnforce:
    def test_enforce_passes_when_aligned(self):
        guard = FrameCountGuard()
        audit = FrameCountAudit(source=1000, encoded=1000, rpu=1000, hdr10p=1000)
        result = guard.enforce(audit, adjustment=_EXACT)
        assert result == audit

    def test_enforce_aborts_when_encoded_mismatch(self):
        guard = FrameCountGuard()
        audit = FrameCountAudit(source=1000, encoded=999, rpu=1000, hdr10p=1000)
        with pytest.raises(FrameCountAuditError, match="non frame-preserving"):
            guard.enforce(audit, adjustment=_TRIM)

    def test_enforce_warns_when_all_readers_failed(self):
        guard = FrameCountGuard()
        audit = FrameCountAudit(source=None, encoded=1000, rpu=1000, hdr10p=1000)
        warnings: list[str] = []
        result = guard.enforce(audit, adjustment=_EXACT, on_warn=warnings.append)
        # Mode dégradé : on log et on laisse passer, plutôt que d'échouer
        # quand aucun lecteur n'a pu déterminer la frame count.
        assert result == audit
        assert any("frame count" in w for w in warnings)

    def test_enforce_aborts_when_rpu_beyond_tolerance(self, tmp_path):
        guard = FrameCountGuard(tolerance=4)
        rpu = tmp_path / "rpu.bin"
        rpu.write_bytes(b"x")
        audit = FrameCountAudit(source=1000, encoded=1000, rpu=1010, hdr10p=1000)
        with pytest.raises(FrameCountAuditError, match="RPU"):
            guard.enforce(audit, adjustment=_TRIM, rpu_bin=rpu)

    @pytest.mark.parametrize("adjustment", [_EXACT, _TRIM])
    def test_enforce_never_fills_short_metadata(self, tmp_path, adjustment):
        guard = FrameCountGuard(tolerance=4)
        rpu = tmp_path / "rpu.bin"
        rpu.write_bytes(b"x")
        audit = FrameCountAudit(source=1000, encoded=1000, rpu=998, hdr10p=None)
        with patch("subprocess.run") as run:
            with pytest.raises(FrameCountAuditError, match="aucune trame de métadonnées n'est fabriquée"):
                guard.enforce(audit, adjustment=adjustment, rpu_bin=rpu)
        run.assert_not_called()

    def test_enforce_exact_refuses_surplus_without_touching_files(self, tmp_path):
        guard = FrameCountGuard(tolerance=4)
        hdr = tmp_path / "hdr10p.json"
        hdr.write_text(json.dumps({"SceneInfo": [{"i": i} for i in range(1002)]}))
        before = hdr.read_text()
        audit = FrameCountAudit(source=1000, encoded=1000, rpu=None, hdr10p=1002)
        with pytest.raises(FrameCountAuditError, match="politique exacte"):
            guard.enforce(audit, adjustment=_EXACT, hdr10p_json=hdr)
        assert hdr.read_text() == before

    def test_enforce_refuses_unreadable_metadata_count(self, tmp_path):
        guard = FrameCountGuard()
        rpu = tmp_path / "rpu.bin"
        rpu.write_bytes(b"x")
        audit = FrameCountAudit(source=1000, encoded=1000, rpu=None, hdr10p=None)
        with pytest.raises(FrameCountAuditError, match="illisible"):
            guard.enforce(audit, adjustment=_TRIM, rpu_bin=rpu)

    def test_enforce_trims_hdr10p_within_tolerance(self, tmp_path):
        guard = FrameCountGuard(tolerance=4)
        hdr = tmp_path / "hdr10p.json"
        hdr.write_text(json.dumps({
            "SceneInfo": [{"i": i} for i in range(1003)],
            "SceneInfoSummary": {
                "SceneFirstFrameIndex": [0, 500, 1001, 1002],
            },
        }))
        audit = FrameCountAudit(source=1000, encoded=1000, rpu=None, hdr10p=1003)
        warnings: list[str] = []
        guard.enforce(
            audit,
            adjustment=_TRIM,
            hdr10p_json=hdr,
            on_warn=warnings.append,
        )
        new_data = json.loads(hdr.read_text(encoding="utf-8"))
        assert len(new_data["SceneInfo"]) == 1000
        # SceneFirstFrameIndex doit être nettoyé des refs >= 1000.
        assert all(idx < 1000 for idx in new_data["SceneInfoSummary"]["SceneFirstFrameIndex"])
        assert any("HDR10+" in w for w in warnings)

    def test_enforce_trims_rpu_via_dovi_tool_editor(self, tmp_path):
        guard = FrameCountGuard(tolerance=4)
        rpu = tmp_path / "rpu.bin"
        rpu.write_bytes(b"original")
        audit = FrameCountAudit(source=1000, encoded=1000, rpu=1003, hdr10p=None)

        # Mock subprocess.run pour simuler dovi_tool editor + relecture du nouveau count.
        call_log: list[list[str]] = []
        edit_payloads: list[dict] = []

        def fake_run(cmd, **kwargs):
            call_log.append(list(cmd))
            if "editor" in cmd:
                edit_payloads.append(json.loads(Path(cmd[cmd.index("-j") + 1]).read_text()))
                # Simule la création du fichier trimmed.
                idx_o = cmd.index("-o")
                Path(cmd[idx_o + 1]).write_bytes(b"trimmed")
                return _make_completed()
            if "info" in cmd:
                return _make_completed(stdout="Frames: 1000\n")
            return _make_completed()

        warnings: list[str] = []
        with patch("subprocess.run", side_effect=fake_run):
            new_audit = guard.enforce(
                audit,
                adjustment=_TRIM,
                rpu_bin=rpu,
                on_warn=warnings.append,
            )

        # dovi_tool editor a bien été appelé avec un edit JSON ``remove``.
        editor_cmd = next(c for c in call_log if "editor" in c)
        idx_j = editor_cmd.index("-j")
        edit_json_path = Path(editor_cmd[idx_j + 1])
        # Le fichier d'edit a été nettoyé après usage : on vérifie via le contenu
        # via le fait que dovi_tool a été appelé.
        assert "-i" in editor_cmd and "-o" in editor_cmd
        assert edit_payloads == [{"remove": ["1000-1002"]}]
        # RPU remplacé par la version trimmée (contenu = b"trimmed").
        assert rpu.read_bytes() == b"trimmed"
        assert new_audit.rpu == 1000
        assert any("RPU" in w for w in warnings)
        # Le edit.json temporaire a été nettoyé.
        assert not edit_json_path.exists()


class TestFrameCountGuardReaders:
    def test_dovi_rpu_frame_count_total_frames_pattern(self, tmp_path):
        rpu = tmp_path / "rpu.bin"
        rpu.write_bytes(b"x")
        guard = FrameCountGuard()
        with patch("subprocess.run", return_value=_make_completed(stdout="Total frames: 12345\n")):
            assert guard._dovi_rpu_frame_count(rpu) == 12345

    def test_hdr10p_json_frame_count_handles_invalid_json(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("not-json")
        guard = FrameCountGuard()
        assert guard._hdr10p_json_frame_count(bad) is None

    def test_mediainfo_returns_none_on_non_numeric_output(self, tmp_path):
        guard = FrameCountGuard()
        with patch("subprocess.run", return_value=_make_completed(stdout="N/A\n")):
            assert guard._mediainfo_frame_count(tmp_path / "x") is None

    def test_ffprobe_nb_frames_reads_stream_tags_number_of_frames(self, tmp_path):
        guard = FrameCountGuard()
        output = "nb_frames=N/A\nTAG:NUMBER_OF_FRAMES=262524\n"
        with patch("subprocess.run", return_value=_make_completed(stdout=output)):
            assert guard._ffprobe_nb_frames(tmp_path / "src.mkv") == 262524


class TestBuildDoviRecordFromRpu:
    def test_extracts_p8_1_with_compat_id(self, tmp_path):
        rpu = tmp_path / "rpu.bin"
        rpu.write_bytes(b"x")
        summary = (
            "Summary:\n"
            "Profile: 8.1\n"
            "DV Level: 6\n"
            "compatibility id: 1\n"
            "Frames: 1000\n"
        )
        with patch(
            "core.workflows.encode.runtime.metadata_inject.subprocess.run",
            return_value=_make_completed(stdout=summary),
        ):
            record = _build_dovi_record_from_rpu(rpu_bin=rpu, dovi_tool_bin="dovi_tool")
        assert record is not None
        assert record.profile == 8
        assert record.level == 6
        assert record.bl_signal_compat_id == 1
        assert record.rpu_present is True
        assert record.el_present is False
        assert record.bl_present is True

    def test_falls_back_to_sub_profile_when_no_compat_id(self, tmp_path):
        rpu = tmp_path / "rpu.bin"
        rpu.write_bytes(b"x")
        summary = "Profile: 8.1\nDV Level: 4\nFrames: 100\n"
        with patch(
            "core.workflows.encode.runtime.metadata_inject.subprocess.run",
            return_value=_make_completed(stdout=summary),
        ):
            record = _build_dovi_record_from_rpu(rpu_bin=rpu, dovi_tool_bin="dovi_tool")
        assert record is not None
        assert record.profile == 8
        assert record.level == 4
        assert record.bl_signal_compat_id == 1  # déduit du sub-profile

    def test_returns_none_when_dovi_tool_missing(self, tmp_path):
        rpu = tmp_path / "rpu.bin"
        rpu.write_bytes(b"x")
        with patch(
            "core.workflows.encode.runtime.metadata_inject.subprocess.run",
            side_effect=FileNotFoundError,
        ):
            record = _build_dovi_record_from_rpu(rpu_bin=rpu, dovi_tool_bin="dovi_tool")
        assert record is None

    def test_returns_none_when_no_profile_in_output(self, tmp_path):
        rpu = tmp_path / "rpu.bin"
        rpu.write_bytes(b"x")
        with patch(
            "core.workflows.encode.runtime.metadata_inject.subprocess.run",
            return_value=_make_completed(stdout="garbage output\n"),
        ):
            record = _build_dovi_record_from_rpu(rpu_bin=rpu, dovi_tool_bin="dovi_tool")
        assert record is None


class TestHdr10PlusTrimSummary:
    def test_trim_recomputes_scene_frame_numbers(self, tmp_path):
        guard = FrameCountGuard(tolerance=4)
        hdr = tmp_path / "hdr10p.json"
        hdr.write_text(json.dumps({
            "SceneInfo": [{"i": i} for i in range(12)],
            "SceneInfoSummary": {
                "SceneFirstFrameIndex": [0, 5, 11],
                "SceneFrameNumbers": [5, 6, 1],
            },
        }))
        guard._trim_hdr10p_json(hdr, target_frames=10)
        summary = json.loads(hdr.read_text())["SceneInfoSummary"]
        assert summary == {"SceneFirstFrameIndex": [0, 5], "SceneFrameNumbers": [5, 5]}


class TestMetadataAsSourceReference:
    """RPU / HDR10+ extraits de la même source = compte source quand ils égalent l'encodé."""

    @staticmethod
    def _meta(tmp_path, frames: int):
        rpu = tmp_path / "rpu.bin"
        rpu.write_bytes(b"x")
        hdr = tmp_path / "hdr10p.json"
        hdr.write_text(json.dumps({"SceneInfo": [{"i": i} for i in range(frames)]}))
        return rpu, hdr

    def test_raw_source_is_not_read_when_metadata_match_encoded(self, tmp_path):
        rpu, hdr = self._meta(tmp_path, 1000)
        guard = FrameCountGuard()
        with patch.object(guard, "_read_video_frame_count") as fast, \
             patch.object(guard, "_recount_video_frames") as exact, \
             patch.object(guard, "_dovi_rpu_frame_count", return_value=1000):
            audit = guard.audit(
                source=tmp_path / "source_annexb.hevc", encoded=tmp_path / "enc.hevc",
                rpu_bin=rpu, hdr10p_json=hdr, known_encoded_frames=1000,
            )
        fast.assert_not_called()
        exact.assert_not_called()
        assert (audit.source, audit.source_basis) == (1000, "metadata")
        assert guard.enforce(audit, adjustment=_TRIM) == audit

    def test_raw_source_is_counted_exactly_on_disagreement(self, tmp_path):
        rpu, _hdr = self._meta(tmp_path, 1000)
        guard = FrameCountGuard()
        with patch.object(guard, "_read_video_frame_count") as fast, \
             patch.object(guard, "_recount_video_frames", return_value=1000) as exact, \
             patch.object(guard, "_dovi_rpu_frame_count", return_value=1000):
            audit = guard.audit(
                source=tmp_path / "source.hevc", encoded=tmp_path / "enc.hevc",
                rpu_bin=rpu, known_encoded_frames=998,
            )
        fast.assert_not_called()  # pas d'estimation mediainfo sur un flux brut
        exact.assert_called_once_with(tmp_path / "source.hevc")
        assert (audit.source, audit.encoded, audit.source_basis) == (1000, 998, "video")
        with pytest.raises(FrameCountAuditError, match="non frame-preserving"):
            guard.enforce(audit, adjustment=_TRIM, rpu_bin=rpu)

    def test_container_inexact_estimate_avoids_full_recount(self, tmp_path):
        """MKV sans statistiques : estimation durée × cadence fausse, métadonnées = encodé."""
        rpu, _hdr = self._meta(tmp_path, 288048)
        guard = FrameCountGuard()
        with patch.object(guard, "_read_video_frame_count", return_value=291213), \
             patch.object(guard, "_recount_video_frames") as exact, \
             patch.object(guard, "_dovi_rpu_frame_count", return_value=288048):
            audit = guard.audit(
                source=tmp_path / "src.mkv", encoded=tmp_path / "enc.hevc",
                rpu_bin=rpu, known_encoded_frames=288048,
            )
        exact.assert_not_called()
        assert (audit.source, audit.source_basis) == (288048, "metadata")

    def test_container_normal_path_is_unchanged(self, tmp_path):
        rpu, _hdr = self._meta(tmp_path, 1000)
        guard = FrameCountGuard()
        with patch.object(guard, "_read_video_frame_count", return_value=1000) as fast, \
             patch.object(guard, "_recount_video_frames") as exact, \
             patch.object(guard, "_dovi_rpu_frame_count", return_value=1000):
            audit = guard.audit(
                source=tmp_path / "src.mkv", encoded=tmp_path / "enc.hevc",
                rpu_bin=rpu, known_encoded_frames=1000,
            )
        fast.assert_called_once()  # contrôle indépendant conservé (≈ 90 Mo lus)
        exact.assert_not_called()
        assert audit.source_basis == "video"

    def test_estimated_encoded_count_never_serves_as_reference(self, tmp_path):
        """Sans compte encodé exact, les métadonnées ne remplacent pas la source."""
        rpu, _hdr = self._meta(tmp_path, 1000)
        guard = FrameCountGuard()
        with patch.object(guard, "_read_video_frame_count", return_value=1000), \
             patch.object(guard, "_recount_video_frames", return_value=1000) as exact, \
             patch.object(guard, "_dovi_rpu_frame_count", return_value=1000):
            audit = guard.audit(source=tmp_path / "source.hevc", encoded=tmp_path / "enc.hevc", rpu_bin=rpu)
        exact.assert_called_once_with(tmp_path / "source.hevc")
        assert audit.source_basis == "video"

    def test_interpolated_raw_source_uses_expanded_metadata(self, tmp_path):
        """RIFE x2 : RPU déjà étendu (2000) = encodé (2000) ; source brute non relue."""
        rpu, _hdr = self._meta(tmp_path, 2000)
        guard = FrameCountGuard()
        with patch.object(guard, "_recount_video_frames") as exact, \
             patch.object(guard, "_dovi_rpu_frame_count", return_value=2000):
            audit = guard.audit(
                source=tmp_path / "source.hevc", encoded=tmp_path / "enc.hevc",
                rpu_bin=rpu, known_encoded_frames=2000, frame_ratio=2,
            )
        exact.assert_not_called()
        assert (audit.source, audit.source_basis) == (2000, "metadata")
