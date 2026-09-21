"""Stable XiaoYou institution-workspace boundary.

Hermes owns execution state such as sessions, provider credentials and plugin
discovery.  XiaoYou owns institution facts, memories, receipts, learning and
its provider-health observation.  They must survive replacement of a Hermes
installation, therefore new product installations resolve them through this
module rather than treating ``HERMES_HOME`` as the data root.

``XIAOYOU_INSTITUTION_WORKSPACE`` is intentionally a product-level location,
not an Hermes configuration key.  The capability installer can alternatively
write a small pointer file in the *host* home.  That file contains no
institution data and is safe to recreate for a new Hermes version.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home


WORKSPACE_ENV = "XIAOYOU_INSTITUTION_WORKSPACE"
HOST_POINTER_NAME = "xiaoyou-runtime.json"
WORKSPACE_SCHEMA_VERSION = 1


def _read_pointer(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def resolve_institution_workspace() -> Path | None:
    """Return the product-owned workspace, without creating or mutating it.

    An explicit process setting is useful for tests, containers and a staged
    upgrade.  A host-local pointer is a deployment convenience only; the
    workspace it names remains outside the Hermes installation.
    """

    configured = str(os.getenv(WORKSPACE_ENV) or "").strip()
    if configured:
        return Path(configured).expanduser()
    try:
        pointer = _read_pointer(Path(get_hermes_home()) / HOST_POINTER_NAME)
    except Exception:
        return None
    configured = str(pointer.get("institution_workspace") or "").strip()
    return Path(configured).expanduser() if configured else None


def workspace_data_dir() -> Path | None:
    workspace = resolve_institution_workspace()
    return workspace / "data" if workspace is not None else None


def workspace_state_dir() -> Path | None:
    workspace = resolve_institution_workspace()
    return workspace / "state" if workspace is not None else None


def workspace_descriptor(workspace: Path) -> dict[str, Any]:
    """Return a deliberately non-sensitive descriptor for a new workspace."""

    return {
        "schema_version": WORKSPACE_SCHEMA_VERSION,
        "product": "xiaoyou",
        "data_layout": "data/",
        "state_layout": "state/",
        "institution_workspace": str(workspace.resolve()),
    }
