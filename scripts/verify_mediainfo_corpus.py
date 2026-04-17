#!/usr/bin/env python3
"""
Verify corpus files (sha256 + size) against manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify MediaInfo parity corpus files")
    parser.add_argument("--manifest", type=Path, required=True, help="Corpus manifest JSON")
    parser.add_argument("--root", type=Path, required=True, help="Corpus root directory")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    ns = parse_args(list(sys.argv[1:] if argv is None else argv))
    manifest = json.loads(ns.manifest.read_text(encoding="utf-8"))
    files = manifest.get("files", [])
    if not isinstance(files, list):
        print("Invalid manifest: 'files' must be a list", file=sys.stderr)
        return 2

    failed = 0
    for item in files:
        if not isinstance(item, dict):
            failed += 1
            print("Invalid item in files[]: not an object")
            continue
        rel = Path(str(item.get("relative_path", "")))
        expected_hash = str(item.get("sha256", "")).lower()
        expected_size = int(item.get("size_bytes", 0))
        path = (ns.root / rel).resolve()

        if not path.exists():
            failed += 1
            print(f"[MISSING] {rel}")
            continue

        actual_size = path.stat().st_size
        actual_hash = _sha256(path)

        ok = True
        if expected_size and actual_size != expected_size:
            ok = False
            print(f"[SIZE]    {rel} expected={expected_size} actual={actual_size}")
        if expected_hash and expected_hash != "replace_with_sha256" and actual_hash.lower() != expected_hash:
            ok = False
            print(f"[SHA256]  {rel} expected={expected_hash} actual={actual_hash}")
        if ok:
            print(f"[OK]      {rel}")
        else:
            failed += 1

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
