"""Safe maintenance drain primitives for the WeCom HTTP callback adapter."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any


DRAIN_STATE = "draining"
SCHEMA_VERSION = "xiaoyou_wecom_callback_drain_v1"


class WecomCallbackDrainGate:
    """Filesystem-backed fail-closed gate.

    Presence of the drain file pauses dispatch. Invalid/corrupt content remains
    paused rather than silently reopening ingress execution.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def snapshot(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "active": False,
                "state": "open",
                "valid": True,
                "path": str(self.path),
            }
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {
                "active": True,
                "state": DRAIN_STATE,
                "valid": False,
                "path": str(self.path),
                "error": "drain_state_unreadable",
            }
        if not isinstance(payload, dict):
            return {
                "active": True,
                "state": DRAIN_STATE,
                "valid": False,
                "path": str(self.path),
                "error": "drain_state_invalid",
            }
        state = str(payload.get("state") or "").strip().lower()
        valid = (
            payload.get("schema_version") == SCHEMA_VERSION
            and state == DRAIN_STATE
        )
        return {
            "active": True,
            "state": DRAIN_STATE,
            "valid": valid,
            "path": str(self.path),
            "requested_at": payload.get("requested_at"),
            "reason": str(payload.get("reason") or ""),
            **({} if valid else {"error": "drain_state_invalid"}),
        }

    def is_draining(self) -> bool:
        return bool(self.snapshot().get("active"))

    async def wait_until_open(self, *, poll_seconds: float = 0.1) -> None:
        delay = max(0.02, float(poll_seconds))
        while self.is_draining():
            await asyncio.sleep(delay)
