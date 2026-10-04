"""Garde-fou frame count avec le vrai dovi_tool (RPU généré, skip si l'outil manque)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from core.workflows.encode.runtime.frame_count_guard import (
    FrameCountAudit,
    FrameCountAuditError,
    FrameCountGuard,
    MetadataAdjustment,
)

pytestmark = pytest.mark.skipif(shutil.which("dovi_tool") is None, reason="dovi_tool requis")


def _generate_rpu(path: Path, frames: int) -> Path:
    config = path.with_suffix(".json")
    config.write_text(json.dumps({
        "cm_version": "V40",
        "length": frames,
        "level6": {
            "max_display_mastering_luminance": 1000,
            "min_display_mastering_luminance": 1,
            "max_content_light_level": 1000,
            "max_frame_average_light_level": 400,
        },
    }), encoding="utf-8")
    subprocess.run(["dovi_tool", "generate", "-j", str(config), "-o", str(path)], check=True, capture_output=True)
    return path


def test_trim_tail_removes_rpu_surplus_with_real_dovi_tool(tmp_path: Path) -> None:
    rpu = _generate_rpu(tmp_path / "rpu.bin", 12)
    guard = FrameCountGuard()
    assert guard.rpu_frame_count(rpu) == 12

    audit = FrameCountAudit(source=10, encoded=10, rpu=12, hdr10p=None)
    result = guard.enforce(audit, adjustment=MetadataAdjustment.TRIM_TAIL, rpu_bin=rpu)

    assert result.rpu == 10
    assert guard.rpu_frame_count(rpu) == 10


def test_short_rpu_is_refused_and_left_untouched(tmp_path: Path) -> None:
    rpu = _generate_rpu(tmp_path / "rpu.bin", 8)
    before = rpu.read_bytes()
    audit = FrameCountAudit(source=10, encoded=10, rpu=8, hdr10p=None)

    with pytest.raises(FrameCountAuditError, match="aucune trame"):
        FrameCountGuard().enforce(audit, adjustment=MetadataAdjustment.TRIM_TAIL, rpu_bin=rpu)
    assert rpu.read_bytes() == before
