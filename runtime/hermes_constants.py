"""Shared runtime path helpers for the local Hermes package."""

from __future__ import annotations

import os
from pathlib import Path


def get_hermes_home() -> Path:
    """Return the configured Hermes home directory."""

    configured = str(os.getenv("HERMES_HOME") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".hermes"
