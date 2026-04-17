"""Dev/CI parity runner helper (oracle-only, non-runtime)."""

from __future__ import annotations

from pathlib import Path


def parity_command(*, manifest: Path, report: Path, oracle_bin: str = "mediainfo") -> list[str]:
    return [
        "python3",
        "scripts/mediainfo_parity_matrix.py",
        "--oracle-bin",
        oracle_bin,
        "--manifest",
        str(manifest),
        "--report",
        str(report),
    ]


def parity_gate_command(*reports: Path) -> list[str]:
    cmd = ["python3", "scripts/mediainfo_parity_gate.py"]
    for report in reports:
        cmd.extend(["--report", str(report)])
    return cmd
