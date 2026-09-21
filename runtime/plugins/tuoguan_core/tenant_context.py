"""Lightweight tenant context helpers for the tutoring-center runtime.

This module is intentionally small: it exposes tenant identity and the
institution operating model as facts for runtime records. It does not route
messages, decide business actions, or change write permissions.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


DEFAULT_TENANT_ID = "default_tenant"
DEFAULT_OPERATING_MODEL_FILE = "institution_operating_model.json"
LEGACY_OPERATING_MODEL_FILE = "youyi_operating_model.json"


def current_tenant_id() -> str:
    """Return the server-configured tenant id, with a safe generic fallback."""

    value = str(os.getenv("HERMES_TENANT_ID") or os.getenv("XIAOYOU_DEFAULT_TENANT_ID") or "").strip()
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


def institution_display_names(store: Any) -> tuple[str, ...]:
    """Return server-owned institution labels suitable for read-only matching.

    A tenant label is presentation and directory-normalisation data, never an
    authority source.  It therefore comes only from a deployment setting or
    the tenant's existing operating-model fact; source code must not embed a
    particular institution's brand as a matching rule.
    """

    values: list[str] = []
    configured = str(os.getenv("XIAOYOU_INSTITUTION_DISPLAY_NAME") or "").strip()
    if configured:
        values.append(configured)
    model = read_institution_operating_model(store)
    value = _first_institution_label(model)
    if value:
        values.append(value)
    expanded: list[str] = []
    for value in values:
        if value and value not in expanded:
            expanded.append(value)
        combined = f"{value}托管" if value and not value.endswith("托管") else value
        if combined and combined not in expanded:
            expanded.append(combined)
    return tuple(expanded)


def _first_institution_label(value: Any) -> str:
    """Read a label from a flexible, server-owned operating-model schema."""

    keys = ("institution_name", "tenant_name", "brand_name", "brand", "name")
    if isinstance(value, dict):
        for key in keys:
            candidate = str(value.get(key) or "").strip()
            if candidate:
                return candidate
        # Do not recurse through arbitrary program/staff records: their
        # ordinary ``name`` fields are not institution labels.  Nested
        # envelope keys are deliberately narrow and schema-neutral.
        for container in ("institution", "tenant", "organization", "profile", "identity", "metadata"):
            candidate = _first_institution_label(value.get(container))
            if candidate:
                return candidate
    if isinstance(value, (list, tuple)):
        for nested in value:
            candidate = _first_institution_label(nested)
            if candidate:
                return candidate
    return ""
