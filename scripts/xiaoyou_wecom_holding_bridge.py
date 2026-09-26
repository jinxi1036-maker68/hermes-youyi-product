#!/usr/bin/env python3
"""Repository entry point for the bootstrap WeCom holding bridge."""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))

from plugins.platforms.wecom.holding_bridge import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
