#!/usr/bin/env python3
"""
scripts/mediainfo_parity_gate.py

Validate one or more parity report JSON files and fail fast on gate violations.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _summary(report: dict[str, Any]) -> dict[str, int]:
    raw = report.get("summary", {})
    return {
        "total": int(raw.get("total", 0) or 0),
        "ok": int(raw.get("ok", 0) or 0),
        "failed": int(raw.get("failed", 0) or 0),
        "skipped": int(raw.get("skipped", 0) or 0),
    }


def _load_report(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Unable to parse report '{path}': {exc}") from exc


def _check_report(
    path: Path,
    *,
    allow_skipped: bool,
    require_non_empty: bool,
) -> list[str]:
    report = _load_report(path)
    s = _summary(report)
    issues: list[str] = []
    if require_non_empty and s["total"] <= 0:
        issues.append("empty report (total=0)")
    if s["failed"] > 0:
        issues.append(f"failed={s['failed']}")
    if not allow_skipped and s["skipped"] > 0:
        issues.append(f"skipped={s['skipped']}")
    if s["ok"] + s["failed"] + s["skipped"] != s["total"]:
        issues.append(
            "inconsistent summary counters "
            f"(ok={s['ok']} failed={s['failed']} skipped={s['skipped']} total={s['total']})"
        )
    return issues


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Gate parity reports (failed/skipped/empty checks)")
    parser.add_argument(
        "--report",
        action="append",
        type=Path,
        required=True,
        help="Parity report JSON path. Repeat for multiple reports.",
    )
    parser.add_argument(
        "--allow-skipped",
        action="store_true",
        help="Allow skipped cases in summary (default: false).",
    )
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="Allow empty reports (total=0).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    ns = parse_args(list(sys.argv[1:] if argv is None else argv))
    exit_code = 0
    for report_path in ns.report:
        if not report_path.exists():
            print(f"[gate] missing report: {report_path}")
            exit_code = 1
            continue
        issues = _check_report(
            report_path,
            allow_skipped=bool(ns.allow_skipped),
            require_non_empty=not bool(ns.allow_empty),
        )
        summary = _summary(_load_report(report_path))
        print(
            f"[gate] {report_path}: total={summary['total']} ok={summary['ok']} "
            f"failed={summary['failed']} skipped={summary['skipped']}"
        )
        if issues:
            exit_code = 1
            for issue in issues:
                print(f"[gate] {report_path}: {issue}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

