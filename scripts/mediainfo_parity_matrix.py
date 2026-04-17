#!/usr/bin/env python3
"""
scripts/mediainfo_parity_matrix.py

Parity checker between:
- Oracle CLI (MediaInfo C++ binary)
- Native Python CLI (core.mediainfo_native)

The checker executes the same CLI argument sets against both engines and
compares return code, stdout and stderr bit-for-bit.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import platform
import shutil
import shlex
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


DEFAULT_MANIFEST = Path("scripts/mediainfo_parity_manifest.example.json")
DEFAULT_REPORT = Path("scripts/mediainfo_parity_report.json")
DEFAULT_ORACLE_CANDIDATES = (
    "/var/home/hydromel/dev/MediaInfo/MediaInfo_CLI_CPP/MediaInfo/Project/GNU/CLI/mediainfo",
)


@dataclass(slots=True)
class ParityCase:
    name: str
    path: Path
    arg_sets: list[list[str]]


@dataclass(slots=True)
class ParityResult:
    case_name: str
    file_path: str
    args: list[str]
    status: str
    reason: str = ""
    oracle_returncode: int | None = None
    native_returncode: int | None = None
    oracle_stdout_sha256: str = ""
    native_stdout_sha256: str = ""
    oracle_stderr_sha256: str = ""
    native_stderr_sha256: str = ""
    stdout_diff: str = ""
    stderr_diff: str = ""


def _sha256(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _frozen_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(base_env or os.environ)
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    env["TZ"] = "UTC"
    env["PYTHONHASHSEED"] = "0"
    return env


def _parse_arg_set(raw: Any) -> list[str]:
    if isinstance(raw, str):
        return shlex.split(raw)
    if isinstance(raw, list):
        return [str(x) for x in raw]
    raise ValueError(f"Invalid arg set: {raw!r}")


def _resolve_oracle_bin(raw_value: str) -> str:
    value = raw_value.strip()
    if value and value != "mediainfo":
        return value
    env_val = os.environ.get("MEDIAINFO_ORACLE_BIN", "").strip()
    if env_val:
        return env_val
    from_path = shutil.which("mediainfo")
    if from_path:
        return from_path
    for candidate in DEFAULT_ORACLE_CANDIDATES:
        path = Path(candidate)
        if path.exists() and path.is_file():
            return str(path)
    return value or "mediainfo"


def _load_manifest(path: Path) -> list[ParityCase]:
    data = json.loads(path.read_text(encoding="utf-8"))
    root = path.parent
    defaults_obj = data.get("defaults", {})
    default_arg_sets_raw = defaults_obj.get("arg_sets", [["--Output=Text"]])
    default_arg_sets = [_parse_arg_set(item) for item in default_arg_sets_raw]

    cases_obj = data.get("cases")
    if not isinstance(cases_obj, list) or not cases_obj:
        raise ValueError("Manifest must contain a non-empty 'cases' array")

    host_platform = platform.system().lower()
    out: list[ParityCase] = []
    for index, case_obj in enumerate(cases_obj):
        if not isinstance(case_obj, dict):
            raise ValueError(f"Case #{index} must be an object")
        enabled_platforms = case_obj.get("platforms")
        if isinstance(enabled_platforms, list):
            allowed = {str(x).strip().lower() for x in enabled_platforms if str(x).strip()}
            if allowed and host_platform not in allowed:
                continue

        rel_path = case_obj.get("path")
        if not isinstance(rel_path, str) or not rel_path.strip():
            raise ValueError(f"Case #{index} missing required string 'path'")
        case_path = Path(rel_path)
        if not case_path.is_absolute():
            case_path = (root / case_path).resolve()

        arg_sets_raw = case_obj.get("arg_sets", default_arg_sets)
        arg_sets = [_parse_arg_set(item) for item in arg_sets_raw]

        name = str(case_obj.get("name") or case_path.name)
        out.append(ParityCase(name=name, path=case_path, arg_sets=arg_sets))

    if not out:
        raise ValueError("No enabled parity case found for this platform")
    return out


def _needs_time_alignment(arg_set: list[str]) -> bool:
    lowered = [arg.lower() for arg in arg_set]
    if any(
        arg.startswith("--output=mpeg-7")
        or arg.startswith("--output=ebucore")
        or arg.startswith("--output=pbcore")
        for arg in lowered
    ):
        return True
    if any(arg.startswith("--inform_timestamp=") and arg != "--inform_timestamp=0" for arg in lowered):
        return True
    return False


def _align_second_boundary(min_headroom_s: float = 0.80) -> None:
    # Some output modes include the current second in the payload. We align
    # command start to the beginning of a second to reduce boundary flakiness.
    while True:
        now = time.time()
        frac = now - int(now)
        if frac <= 1.0 - min_headroom_s:
            return
        time.sleep((1.0 - frac) + 0.01)


def _run_cmd_pair(
    *,
    oracle_cmd: list[str],
    native_cmd: list[str],
    env: dict[str, str],
    timeout_s: float,
    align_seconds: bool,
) -> tuple[subprocess.CompletedProcess[str], subprocess.CompletedProcess[str]]:
    if align_seconds:
        _align_second_boundary()

    oracle_proc = subprocess.Popen(
        oracle_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    native_proc = subprocess.Popen(
        native_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )

    deadline = time.monotonic() + timeout_s
    try:
        oracle_remaining = max(0.1, deadline - time.monotonic())
        oracle_stdout, oracle_stderr = oracle_proc.communicate(timeout=oracle_remaining)
        native_remaining = max(0.1, deadline - time.monotonic())
        native_stdout, native_stderr = native_proc.communicate(timeout=native_remaining)
    except subprocess.TimeoutExpired as exc:
        for proc in (oracle_proc, native_proc):
            if proc.poll() is None:
                proc.kill()
        for proc in (oracle_proc, native_proc):
            try:
                proc.communicate(timeout=0.2)
            except Exception:
                pass
        raise subprocess.TimeoutExpired(exc.cmd, timeout_s) from exc

    oracle_res = subprocess.CompletedProcess(
        oracle_cmd,
        oracle_proc.returncode,
        oracle_stdout,
        oracle_stderr,
    )
    native_res = subprocess.CompletedProcess(
        native_cmd,
        native_proc.returncode,
        native_stdout,
        native_stderr,
    )
    return oracle_res, native_res


def _diff_text(label: str, left: str, right: str, max_lines: int = 120) -> str:
    diff = list(
        difflib.unified_diff(
            left.splitlines(),
            right.splitlines(),
            fromfile=f"{label}_oracle",
            tofile=f"{label}_native",
            lineterm="",
        )
    )
    if len(diff) > max_lines:
        diff = diff[:max_lines] + [f"... diff truncated ({len(diff) - max_lines} more lines)"]
    return "\n".join(diff)


def run_parity(
    *,
    manifest_path: Path,
    oracle_bin: str,
    native_cmd_prefix: list[str],
    timeout_s: float,
    stop_on_first: bool,
) -> dict[str, Any]:
    cases = _load_manifest(manifest_path)
    env = _frozen_env()
    results: list[ParityResult] = []

    total = 0
    failed = 0
    skipped = 0

    for case in cases:
        for arg_set in case.arg_sets:
            total += 1
            if not case.path.exists():
                skipped += 1
                results.append(
                    ParityResult(
                        case_name=case.name,
                        file_path=str(case.path),
                        args=arg_set,
                        status="skipped",
                        reason="file_not_found",
                    )
                )
                continue

            oracle_cmd = [oracle_bin, *arg_set, str(case.path)]
            native_cmd = [*native_cmd_prefix, *arg_set, str(case.path)]
            needs_time_alignment = _needs_time_alignment(arg_set)

            try:
                oracle_res, native_res = _run_cmd_pair(
                    oracle_cmd=oracle_cmd,
                    native_cmd=native_cmd,
                    env=env,
                    timeout_s=timeout_s,
                    align_seconds=needs_time_alignment,
                )
            except FileNotFoundError as exc:
                failed += 1
                results.append(
                    ParityResult(
                        case_name=case.name,
                        file_path=str(case.path),
                        args=arg_set,
                        status="error",
                        reason=f"tool_not_found: {exc}",
                    )
                )
                if stop_on_first:
                    break
                continue
            except subprocess.TimeoutExpired:
                failed += 1
                results.append(
                    ParityResult(
                        case_name=case.name,
                        file_path=str(case.path),
                        args=arg_set,
                        status="error",
                        reason=f"timeout_after_{timeout_s}s",
                    )
                )
                if stop_on_first:
                    break
                continue

            equal = (
                oracle_res.returncode == native_res.returncode
                and oracle_res.stdout == native_res.stdout
                and oracle_res.stderr == native_res.stderr
            )

            status = "ok" if equal else "mismatch"
            if not equal:
                failed += 1

            results.append(
                ParityResult(
                    case_name=case.name,
                    file_path=str(case.path),
                    args=arg_set,
                    status=status,
                    oracle_returncode=oracle_res.returncode,
                    native_returncode=native_res.returncode,
                    oracle_stdout_sha256=_sha256(oracle_res.stdout),
                    native_stdout_sha256=_sha256(native_res.stdout),
                    oracle_stderr_sha256=_sha256(oracle_res.stderr),
                    native_stderr_sha256=_sha256(native_res.stderr),
                    stdout_diff="" if equal else _diff_text("stdout", oracle_res.stdout, native_res.stdout),
                    stderr_diff="" if equal else _diff_text("stderr", oracle_res.stderr, native_res.stderr),
                )
            )
            if stop_on_first and not equal:
                break
        if stop_on_first and failed:
            break

    return {
        "manifest": str(manifest_path),
        "oracle_bin": oracle_bin,
        "native_cmd_prefix": native_cmd_prefix,
        "platform": platform.platform(),
        "summary": {
            "total": total,
            "ok": total - failed - skipped,
            "failed": failed,
            "skipped": skipped,
        },
        "results": [asdict(r) for r in results],
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MediaInfo parity matrix checker")
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help="Path to parity manifest JSON file",
    )
    parser.add_argument(
        "--oracle-bin",
        default=os.environ.get("MEDIAINFO_ORACLE_BIN", "mediainfo"),
        help="Oracle C++ mediainfo binary path (default: MEDIAINFO_ORACLE_BIN, PATH, then local source-tree candidate)",
    )
    parser.add_argument(
        "--native-cmd",
        default=f"{sys.executable} -m core.mediainfo_native",
        help="Native command prefix used to execute the Python shim",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=45.0,
        help="Timeout per command in seconds",
    )
    parser.add_argument(
        "--stop-on-first",
        action="store_true",
        help="Stop execution at first mismatch/error",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=DEFAULT_REPORT,
        help="Output JSON report path",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    ns = parse_args(list(sys.argv[1:] if argv is None else argv))
    manifest_path = ns.manifest.resolve()
    if not manifest_path.exists():
        print(f"Manifest not found: {manifest_path}", file=sys.stderr)
        return 2

    native_cmd_prefix = shlex.split(str(ns.native_cmd))
    oracle_bin = _resolve_oracle_bin(str(ns.oracle_bin))
    report = run_parity(
        manifest_path=manifest_path,
        oracle_bin=oracle_bin,
        native_cmd_prefix=native_cmd_prefix,
        timeout_s=float(ns.timeout),
        stop_on_first=bool(ns.stop_on_first),
    )

    ns.report.parent.mkdir(parents=True, exist_ok=True)
    ns.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    summary = report["summary"]
    print(
        "Parity summary:"
        f" total={summary['total']}"
        f" ok={summary['ok']}"
        f" failed={summary['failed']}"
        f" skipped={summary['skipped']}"
    )
    print(f"Report written: {ns.report}")
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
