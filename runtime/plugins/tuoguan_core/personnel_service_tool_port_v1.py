"""Receipt-producing public Tool port for the governance capability candidate.

The caller has already selected a declared Tool through Hermes. This port is
not a dispatcher: it neither inspects natural language nor maps keywords to an
operation. It simply converts one explicit candidate write result into the
same Receipt/writeback shape consumed by the existing XiaoYou runtime.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable

from .execution_receipts import build_execution_receipt


def receipt_for_candidate_write(*, operation: str, operation_id: str, invoke: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    result = invoke()
    if not isinstance(result, dict):
        result = {"writeback_verified": False, "error": "invalid_candidate_result"}
    data = deepcopy(result)
    # `event_id` is the immutable operation itself.  It is an auditable target
    # for relation/employment writes whose object type predates the core receipt
    # vocabulary; it is never user-facing reply content.
    data.setdefault("event_id", str(operation_id))
    payload = {
        "ok": bool(data.get("writeback_verified")),
        "writeback_verified": bool(data.get("writeback_verified")),
        "data": data,
    }
    return build_execution_receipt(payload, operation_id=str(operation_id), operation=str(operation), idempotency_result="replayed" if bool(data.get("already_applied")) else "applied")
