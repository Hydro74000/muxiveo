#!/usr/bin/env python3
"""Refuse une dépendance glibc supérieure à 2.28 dans tout le paquet Linux."""
import re
import subprocess
import sys
from pathlib import Path

for path in Path(sys.argv[1]).rglob("*"):
    if path.is_file() and (path.suffix == ".so" or path.name == "muxiveo-mvtools"):
        result = subprocess.run(["objdump", "-T", str(path)], check=True, capture_output=True, text=True)
        versions = [tuple(map(int, v.split("."))) for v in re.findall(r"GLIBC_([0-9.]+)", result.stdout)]
        if versions and max(versions) > (2, 28):
            raise SystemExit(f"{path} : glibc {max(versions)} > 2.28")
        print(path.name, max(versions, default=(0, 0)))
