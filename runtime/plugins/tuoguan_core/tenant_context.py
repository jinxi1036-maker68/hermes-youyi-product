"""Lightweight tenant context helpers for the tutoring-center runtime.

This module is intentionally small: it exposes tenant identity and the
institution operating model as facts for runtime records. It does not route
messages, decide business actions, or change write permissions.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


DEFAULT_TENANT_ID = "example_institution"
DEFAULT_OPERATING_MODEL_FILE = "institution_operating_model.json"
LEGACY_OPERATING_MODEL_FILE = "youyi_operating_model.json"


def current_tenant_id() -> str:
    """Return the configured tenant id, preserving the Youyi default."""

    value = str(os.getenv("HERMES_TENANT_ID") or "").strip()
    return value or DEFAULT_TENANT_ID


def configured_operating_model_file() -> str:
    """Return the configured institution model filename, scoped to data_dir."""

    value = str(os.getenv("HERMES_TENANT_OPERATING_MODEL_FILE") or "").strip()
    if not value:
        return DEFAULT_OPERATING_MODEL_FILE
    name = Path(value).name
    return name if name == value and name else DEFAULT_OPERATING_MODEL_FILE


def operating_model_candidates() -> tuple[str, ...]:
    """Return preferred operating model filenames in lookup order."""

    configured = configured_operating_model_file()
    candidates = [configured, DEFAULT_OPERATING_MODEL_FILE, LEGACY_OPERATING_MODEL_FILE]
    result: list[str] = []
    for item in candidates:
        if item and item not in result:
            result.append(item)
    return tuple(result)


def read_institution_operating_model(store: Any) -> dict[str, Any]:
    """Read the first available institution operating model from the store."""

    for filename in operating_model_candidates():
        try:
            data = store.read_json(filename, {})
        except Exception:
            continue
        if isinstance(data, dict) and data:
            return data
    return {}
