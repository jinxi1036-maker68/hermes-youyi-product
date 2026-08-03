"""Tool result serialization helpers."""

from __future__ import annotations

import json
from typing import Any


def tool_result(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
