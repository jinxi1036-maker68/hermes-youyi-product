"""Minimal Hermes path shim for XiaoYou repository-local tests only.

This module is intentionally incomplete and MUST NOT be overlaid onto a
production Hermes Core release. Production candidates must use the
`hermes_constants` module shipped by their Hermes Core runtime.
"""

from __future__ import annotations

import os
from pathlib import Path


XIAOYOU_TEST_SHIM = True


def get_hermes_home() -> Path:
    """Return the configured Hermes home directory for local tests."""

    configured = str(os.getenv("HERMES_HOME") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".hermes"
