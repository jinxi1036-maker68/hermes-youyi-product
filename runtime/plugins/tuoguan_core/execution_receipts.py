"""Canonical receipts for business writes and outbound operations."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
from typing import Any
import uuid


@dataclass(frozen=True)
class ExecutionReceipt:
    receipt_id: str
    operation_id: str
    operation: str
    status: str
    object_type: str
    object_id: str
    object_version: str
    idempotency_result: str
    writeback_verified: bool
    delivery_status: str
    error_layer: str
    error_code: str
    created_at: str


_OBJECT_ID_KEYS = (
    ("task_id", "task"),
    ("goal_action_id", "goal_action"),
    ("goal_id", "goal"),
    ("candidate_id", "candidate"),
    ("attention_id", "attention_thread"),
    ("notification_id", "notification"),
    ("event_id", "event"),
    ("record_id", "record"),
    ("student_id", "student"),
    ("student_name", "student"),
)


def _data(result: dict[str, Any]) -> dict[str, Any]:
    value = result.get("data")
    return value if isinstance(value, dict) else {}


def _first(mapping: dict[str, Any], key: str) -> str:
    value = mapping.get(key)
    if value not in (None, ""):
        return str(value)
    for nested_key in ("task", "goal", "action", "candidate", "item", "receipt"):
        nested = mapping.get(nested_key)
        if isinstance(nested, dict) and nested.get(key) not in (None, ""):
            return str(nested[key])
    return ""


def _object_identity(result: dict[str, Any], operation: str) -> tuple[str, str]:
    data = _data(result)
    for key, object_type in _OBJECT_ID_KEYS:
        value = _first(data, key) or _first(result, key)
        if value:
            return object_type, value
    normalized = str(operation or "business_object").removeprefix("submit_").removeprefix("update_")
    return normalized, ""


def _object_version(result: dict[str, Any]) -> str:
    data = _data(result)
    for key in ("object_version", "data_version", "version", "updated_at", "completed_at", "cancelled_at"):
        value = _first(data, key) or _first(result, key)
        if value:
            return str(value)
    if not data:
        return ""
    stable = {
        key: value for key, value in data.items()
        if key not in {"rendered_text", "message", "source_text", "raw_text"}
    }
    try:
        serialized = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return ""
    return "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:20]


def error_layer(error_code: str) -> str:
    code = str(error_code or "")
    if code.startswith("wrong_tool"):
        return "tool_selection"
    if "permission" in code or "unauthorized" in code or "forbidden" in code:
        return "authorization"
    if "ambiguous" in code or "not_found" in code or "missing" in code:
        return "target_resolution"
    if "writeback" in code or "consistency" in code:
        return "writeback"
    if "idempot" in code or "already_in_progress" in code:
        return "idempotency"
    if "deliver" in code or "outbox" in code or "send" in code:
        return "delivery"
    if code:
        return "execution"
    return ""


def build_execution_receipt(
    result: dict[str, Any],
    *,
    operation_id: str,
    operation: str,
    idempotency_result: str = "applied",
) -> dict[str, Any]:
    payload = deepcopy(result) if isinstance(result, dict) else {"ok": False, "error": "invalid_operation_result"}
    data = _data(payload)
    explicit_writeback = payload.get("writeback_verified")
    if explicit_writeback is None:
        explicit_writeback = data.get("writeback_verified")
    delivery_status = str(
        data.get("delivery_status") or payload.get("delivery_status") or ""
    )
    error_code = str(payload.get("error") or "")
    status = "completed" if payload.get("ok") and explicit_writeback is True else "failed"
    if idempotency_result == "in_progress":
        status = "in_progress"
    elif idempotency_result == "replayed" and payload.get("ok"):
        status = "completed"
    object_type, object_id = _object_identity(payload, operation)
    receipt = ExecutionReceipt(
        receipt_id=f"receipt_{uuid.uuid4().hex}",
        operation_id=str(operation_id or ""),
        operation=str(operation or ""),
        status=status,
        object_type=object_type,
        object_id=object_id,
        object_version=_object_version(payload),
        idempotency_result=str(idempotency_result or "applied"),
        writeback_verified=bool(explicit_writeback),
        delivery_status=delivery_status,
        error_layer=error_layer(error_code),
        error_code=error_code,
        created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
    )
    receipt_payload = asdict(receipt)
    payload["execution_receipt"] = receipt_payload
    if isinstance(payload.get("data"), dict):
        payload["data"] = {**payload["data"], "execution_receipt": receipt_payload}
    return payload


def delivery_execution_receipt(item: dict[str, Any]) -> dict[str, Any]:
    """Build a canonical receipt from a persisted outbox state."""

    status = str(item.get("status") or "result_unknown")
    error_code = str(item.get("last_error") or item.get("suppressed_reason") or "")
    receipt = ExecutionReceipt(
        receipt_id=f"receipt_{uuid.uuid4().hex}",
        operation_id=str(item.get("id") or ""),
        operation="send_notification",
        status=status,
        object_type="notification",
        object_id=str(item.get("id") or ""),
        object_version=str(item.get("sent_at") or item.get("failed_at") or item.get("last_attempt_at") or ""),
        idempotency_result="applied",
        writeback_verified=True,
        delivery_status=status,
        error_layer=error_layer(error_code),
        error_code=error_code,
        created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
    )
    return asdict(receipt)
