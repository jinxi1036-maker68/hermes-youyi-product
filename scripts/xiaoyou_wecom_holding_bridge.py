#!/usr/bin/env python3
"""Exact-candidate entry point for the bootstrap WeCom holding bridge."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import ModuleType


ROOT = Path(__file__).resolve().parents[1]
BRIDGE_PATH = (
    ROOT
    / "runtime"
    / "plugins"
    / "platforms"
    / "wecom"
    / "holding_bridge.py"
)


def _load_candidate_bridge_module(
    bridge_path: Path = BRIDGE_PATH,
) -> ModuleType:
    """Load the Bridge implementation by exact staged file path.

    Do not import through plugins.platforms.wecom here. On bootstrap servers
    that package name may already belong to the legacy Hermes installation,
    which would silently defeat the staged-candidate boundary.
    """

    path = bridge_path.resolve()
    if not path.is_file():
        raise RuntimeError(f"candidate_holding_bridge_missing:{path}")

    module_name = (
        "_xiaoyou_staged_holding_bridge_"
        + str(abs(hash(str(path))))
    )
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"candidate_holding_bridge_spec_failed:{path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise

    loaded = Path(str(getattr(module, "__file__", ""))).resolve()
    if loaded != path:
        sys.modules.pop(module_name, None)
        raise RuntimeError(
            "candidate_holding_bridge_origin_mismatch:"
            f"expected={path}:actual={loaded}"
        )
    return module


def main() -> int:
    module = _load_candidate_bridge_module()
    entry = getattr(module, "main", None)
    if not callable(entry):
        raise RuntimeError("candidate_holding_bridge_main_missing")
    return int(entry())


if __name__ == "__main__":
    raise SystemExit(main())
