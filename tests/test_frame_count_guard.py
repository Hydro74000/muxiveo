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
)
from core.workflows.encode.runtime.metadata_inject import _build_dovi_record_from_rpu


def _make_completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class TestFrameCountAudit:
    def test_aligned_strict(self):
        audit = FrameCountAudit(source=1000, encoded=1000, rpu=1000, hdr10p=1000)
        ok, _ = audit.is_aligned()
        assert ok

    def test_aligned_within_tolerance_on_rpu(self):
        audit = FrameCountAudit(source=1000, encoded=1000, rpu=998, hdr10p=1000)
        ok, _ = audit.is_aligned(tolerance=4)
        assert ok

    def test_encoded_mismatch_blocks(self):
        audit = FrameCountAudit(source=1000, encoded=999, rpu=1000, hdr10p=1000)
        ok, msg = audit.is_aligned()
        assert not ok
        assert "encoded" in msg

    def test_rpu_beyond_tolerance_blocks(self):
        audit = FrameCountAudit(source=1000, encoded=1000, rpu=993, hdr10p=1000)
        ok, msg = audit.is_aligned(tolerance=4)
        assert not ok
        assert "rpu" in msg

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
                _make_completed(stdout="3\n"),         # mediainfo encoded
                _make_completed(stdout="Frames: 3\n"), # dovi_tool info
            ]
            audit = guard.audit(
                source=tmp_path / "src.mkv",
                encoded=tmp_path / "enc.hevc",
                rpu_bin=rpu,
                hdr10p_json=hdr,
            )
        assert audit == FrameCountAudit(source=3, encoded=3, rpu=3, hdr10p=3)

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

    def test_audit_falls_back_to_count_packets_when_nb_frames_empty(self, tmp_path):
        guard = FrameCountGuard()
        # mediainfo OK pour la source, mais nb_frames vide pour l'encoded
        # (typique d'un HEVC brut sans index) → on tombe sur count_packets.
        responses = [
            _make_completed(stdout="1000\n"),   # mediainfo source
            _make_completed(stdout="\n"),       # mediainfo encoded (vide)
            _make_completed(stdout="N/A\n"),    # ffprobe nb_frames encoded
            _make_completed(stdout="1000\n"),   # ffprobe count_packets encoded
        ]
        with patch("subprocess.run", side_effect=responses):
            audit = guard.audit(source=tmp_path / "a", encoded=tmp_path / "b")
        assert audit.source == 1000 and audit.encoded == 1000

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
                _make_completed(stdout="100\n"),
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
        result = guard.enforce(audit)
        assert result == audit

    def test_enforce_aborts_when_encoded_mismatch(self):
        guard = FrameCountGuard()
        audit = FrameCountAudit(source=1000, encoded=999, rpu=1000, hdr10p=1000)
        with pytest.raises(FrameCountAuditError, match="non frame-preserving"):
            guard.enforce(audit)

    def test_enforce_warns_when_all_readers_failed(self):
        guard = FrameCountGuard()
        audit = FrameCountAudit(source=None, encoded=1000, rpu=1000, hdr10p=1000)
        warnings: list[str] = []
        result = guard.enforce(audit, on_warn=warnings.append)
        # Mode dégradé : on log et on laisse passer, plutôt que d'échouer
        # quand aucun lecteur n'a pu déterminer la frame count.
        assert result == audit
        assert any("frame count" in w for w in warnings)

    def test_enforce_aborts_when_rpu_beyond_tolerance(self, tmp_path):
        guard = FrameCountGuard(tolerance=4)
        rpu = tmp_path / "rpu.bin"
        rpu.write_bytes(b"x")
        audit = FrameCountAudit(source=1000, encoded=1000, rpu=990, hdr10p=1000)
        with pytest.raises(FrameCountAuditError, match="RPU"):
            guard.enforce(audit, rpu_bin=rpu)

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

        def fake_run(cmd, **kwargs):
            call_log.append(list(cmd))
            if "editor" in cmd:
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
