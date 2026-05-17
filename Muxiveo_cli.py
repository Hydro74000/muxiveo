#!/usr/bin/env python3
"""Muxiveo CLI launcher."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cli.main import main


if __name__ == "__main__":
    raise SystemExit(main())

