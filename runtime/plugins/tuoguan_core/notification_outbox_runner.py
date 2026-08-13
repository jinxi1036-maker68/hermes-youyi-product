"""Drain the trusted Enterprise WeChat outbox from a v0.20 oneshot job."""

from __future__ import annotations

import asyncio
import json

from .daily_reporter import drain_notification_outbox_once


def main() -> int:
    try:
        result = asyncio.run(drain_notification_outbox_once())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}:{exc}"}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
