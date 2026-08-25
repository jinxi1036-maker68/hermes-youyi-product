"""Bound a WeCom turn so late model work cannot mutate real business state.

The model provider may outlive an asyncio cancellation when its underlying
HTTP/client thread is blocked.  A callback timeout is therefore not enough:
the late result must be unable to call a business tool or create a delayed
reply.  This module owns only ephemeral process-local turn validity.  Durable
message idempotency remains in the WeCom inbound receipt store.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import threading
import time
from typing import Any


DEFAULT_TURN_BUDGET_SECONDS = 38.0
_LOCK = threading.RLock()
_BY_MESSAGE: dict[str, "TurnFence"] = {}
_BY_SESSION: dict[str, str] = {}
_BY_CHAT: dict[str, str] = {}


@dataclass
class TurnFence:
    message_id: str
    chat_id: str
    deadline_monotonic: float
    phase: str = "created"
    status: str = "active"
    session_ids: set[str] = field(default_factory=set)
    verified_write: bool = False
    delivery_unknown: bool = False
    created_monotonic: float = field(default_factory=time.monotonic)
    terminal_reason: str = ""


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _prune_locked(now: float | None = None) -> None:
    current = float(now if now is not None else time.monotonic())
    stale = [
        key for key, fence in _BY_MESSAGE.items()
        if current - fence.created_monotonic > 2 * 60 * 60
    ]
    for key in stale:
        fence = _BY_MESSAGE.pop(key, None)
        if fence is None:
            continue
        for session_id in fence.session_ids:
            if _BY_SESSION.get(session_id) == key:
                _BY_SESSION.pop(session_id, None)
        if fence.chat_id and _BY_CHAT.get(fence.chat_id) == key:
            _BY_CHAT.pop(fence.chat_id, None)


def begin_callback_turn(
    *,
    message_id: str,
    chat_id: str,
    budget_seconds: float = DEFAULT_TURN_BUDGET_SECONDS,
) -> TurnFence:
    """Create the sole current turn for a chat and supersede its model phase."""

    message_key = _clean(message_id)
    chat_key = _clean(chat_id)
    if not message_key:
        raise ValueError("message_id_required")
    budget = max(5.0, min(float(budget_seconds or DEFAULT_TURN_BUDGET_SECONDS), 120.0))
    with _LOCK:
        _prune_locked()
        previous_key = _BY_CHAT.get(chat_key) if chat_key else None
        previous = _BY_MESSAGE.get(previous_key or "")
        # Only a pure model phase may be replaced. Once a tool starts, the
        # new turn reads the verified result instead of cancelling a write.
        if previous and previous.status == "active" and previous.phase in {"created", "model"}:
            previous.status = "superseded"
            previous.terminal_reason = "newer_message"
        fence = TurnFence(
            message_id=message_key,
            chat_id=chat_key,
            deadline_monotonic=time.monotonic() + budget,
        )
        _BY_MESSAGE[message_key] = fence
        if chat_key:
            _BY_CHAT[chat_key] = message_key
        return fence


def bind_session(*, message_id: str, session_id: str, chat_id: str = "") -> bool:
    """Bind Hermes' resolved session id to the durable callback turn."""

    message_key = _clean(message_id)
    session_key = _clean(session_id)
    if not message_key or not session_key:
        return False
    with _LOCK:
        fence = _BY_MESSAGE.get(message_key)
        if fence is None and chat_id:
            candidate = _BY_MESSAGE.get(_BY_CHAT.get(_clean(chat_id), ""))
            if candidate and candidate.message_id == message_key:
                fence = candidate
        if fence is None:
            return False
        fence.session_ids.add(session_key)
        _BY_SESSION[session_key] = fence.message_id
        return True


def mark_phase(*, session_id: str = "", message_id: str = "", phase: str) -> None:
    with _LOCK:
        fence = _find_locked(session_id=session_id, message_id=message_id)
        if fence and fence.status == "active":
            fence.phase = _clean(phase) or fence.phase


def observe_tool_result(*, session_id: str, result: Any) -> None:
    """Remember verified work only to choose an honest timeout receipt."""

    with _LOCK:
        fence = _find_locked(session_id=session_id)
        if fence is None:
            return
        parsed = result if isinstance(result, dict) else {}
        data = parsed.get("data") if isinstance(parsed.get("data"), dict) else {}
        receipt = data.get("execution_receipt") if isinstance(data.get("execution_receipt"), dict) else {}
        fence.verified_write = bool(
            fence.verified_write
            or data.get("writeback_verified")
            or receipt.get("writeback_verified")
        )
        delivery = str(data.get("delivery_status") or receipt.get("delivery_status") or "")
        fence.delivery_unknown = bool(fence.delivery_unknown or delivery == "result_unknown")


def expire_turn(*, message_id: str = "", session_id: str = "", reason: str = "timeout") -> bool:
    with _LOCK:
        fence = _find_locked(session_id=session_id, message_id=message_id)
        if fence is None or fence.status != "active":
            return False
        fence.status = "expired"
        fence.terminal_reason = _clean(reason) or "timeout"
        return True


def finish_turn(*, session_id: str = "", message_id: str = "") -> None:
    with _LOCK:
        fence = _find_locked(session_id=session_id, message_id=message_id)
        if fence and fence.status == "active":
            fence.status = "finished"
            fence.terminal_reason = "completed"


def block_reason(*, session_id: str = "", message_id: str = "", now: float | None = None) -> str:
    """Return a safe reason when a late turn must not continue."""

    with _LOCK:
        fence = _find_locked(session_id=session_id, message_id=message_id)
        if fence is None:
            return ""
        current = float(now if now is not None else time.monotonic())
        if fence.status == "active" and current > fence.deadline_monotonic:
            fence.status = "expired"
            fence.terminal_reason = "deadline_exceeded"
        if fence.status in {"expired", "superseded"}:
            return f"turn_{fence.status}"
        return ""


def failure_reply(*, message_id: str = "", session_id: str = "") -> str:
    """Use only state we have actually verified; never invent delivery."""

    with _LOCK:
        fence = _find_locked(session_id=session_id, message_id=message_id)
        if fence and fence.delivery_unknown:
            return "这轮外发是否送达还不能确认，系统不会盲目重复发送。"
        if fence and fence.verified_write:
            return "刚才的事项已经完成并核验，但回复生成失败。"
    return "模型服务暂时不稳定，这一轮没有执行写入或发送，请稍后再试。"


def snapshot(*, session_id: str = "", message_id: str = "") -> dict[str, Any]:
    with _LOCK:
        fence = _find_locked(session_id=session_id, message_id=message_id)
        if fence is None:
            return {"known": False}
        return {
            "known": True,
            "message_id": fence.message_id,
            "phase": fence.phase,
            "status": fence.status,
            "terminal_reason": fence.terminal_reason,
            "verified_write": fence.verified_write,
            "delivery_unknown": fence.delivery_unknown,
            "remaining_ms": max(0, round((fence.deadline_monotonic - time.monotonic()) * 1000)),
        }


def clear_turn_fences() -> None:
    with _LOCK:
        _BY_MESSAGE.clear()
        _BY_SESSION.clear()
        _BY_CHAT.clear()


def _find_locked(*, session_id: str = "", message_id: str = "") -> TurnFence | None:
    message_key = _clean(message_id)
    if message_key:
        return _BY_MESSAGE.get(message_key)
    session_key = _clean(session_id)
    if session_key:
        return _BY_MESSAGE.get(_BY_SESSION.get(session_key, ""))
    return None
