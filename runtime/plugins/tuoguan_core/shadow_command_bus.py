"""Read-only shadow command bus used during the Hermes foundation migration.

The module never executes a business command. It observes the completed reply
ledger, compiles it into a stable CommandEnvelope, and writes a comparison row
to a separate architecture ledger.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import threading
import uuid
from typing import Any


SHADOW_LEDGER = "command_shadow_ledger.jsonl"
_LOCK = threading.RLock()


CAPABILITY_CONTRACTS: dict[str, dict[str, Any]] = {
    "老师本人任务查询": {"capability_id": "query_my_tasks", "allowed_tools": ["tuoguan_query_tasks"], "write": False, "deterministic": True},
    "学生日常记录写入": {"capability_id": "record_student", "allowed_tools": ["tuoguan_record_student"], "write": True, "deterministic": False},
    "老师任务反馈与完成": {"capability_id": "update_task", "allowed_tools": ["tuoguan_update_task"], "write": True, "deterministic": False},
    "老师任务完成": {"capability_id": "update_task", "allowed_tools": ["tuoguan_update_task"], "write": True, "deterministic": False},
    "暑假班积分加减": {"capability_id": "change_summer_points", "allowed_tools": ["tuoguan_change_summer_points"], "write": True, "deterministic": True},
    "暑假班学生积分查询": {"capability_id": "query_summer_points", "allowed_tools": ["tuoguan_query_summer_points"], "write": False, "deterministic": True},
    "暑假班积分排行榜查询": {"capability_id": "query_summer_points_ranking", "allowed_tools": ["tuoguan_query_summer_points_ranking"], "write": False, "deterministic": True},
    "老板经营查询": {"capability_id": "query_operations", "allowed_tools": ["tuoguan_query_operations_report"], "write": False, "deterministic": True},
    "经营日报生成": {"capability_id": "generate_daily_report", "allowed_tools": ["tuoguan_query_operations_report"], "write": False, "deterministic": True},
}


@dataclass(frozen=True)
class CommandEnvelope:
    command_id: str
    tenant_id: str
    actor_user_id: str
    actor_role: str
    channel: str
    source_message_id: str
    operation_id: str
    trace_id: str
    capability_id: str
    payload: dict[str, Any]
    requested_at: str


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _enabled(data_dir: Path, config_path: str | Path | None = None) -> bool:
    path = Path(config_path) if config_path else data_dir.parent.parent / "config" / "shadow_command_bus_config.json"
    try:
        config = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return False
    return bool(config.get("enabled")) and config.get("mode") == "shadow" and config.get("allow_business_write") is False


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _redact(item) for key, item in value.items() if str(key).lower() not in {"token", "secret", "api_key", "password"}}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return re.sub(r"(?<!\d)1\d{10}(?!\d)", "1**********", value)
    return value


def _tool_name(call: Any) -> str:
    if isinstance(call, dict):
        return str(call.get("tool") or call.get("tool_name") or call.get("name") or "")
    return str(call or "")


def _tool_args(call: Any) -> dict[str, Any]:
    if not isinstance(call, dict):
        return {}
    args = call.get("args") or call.get("arguments") or {}
    return _redact(args) if isinstance(args, dict) else {}


def _latest_ledger(data_dir: Path, message_id: str) -> dict[str, Any] | None:
    path = data_dir / "reply_ledger.jsonl"
    if not path.exists() or not message_id:
        return None
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in reversed(lines[-500:]):
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if str(item.get("message_id") or "") == message_id:
            return item
    return None


def observe_completed_message(
    *,
    data_dir: str | Path,
    message_id: str,
    tenant_id: str,
    channel: str,
    config_path: str | Path | None = None,
) -> dict[str, Any] | None:
    """Record a non-executing comparison after the normal runtime has replied."""

    root = Path(data_dir)
    if not _enabled(root, config_path):
        return None
    ledger = _latest_ledger(root, str(message_id or ""))
    if ledger is None:
        return None
    shadow_path = root / SHADOW_LEDGER
    with _LOCK:
        if shadow_path.exists():
            for line in reversed(shadow_path.read_text(encoding="utf-8", errors="replace").splitlines()[-300:]):
                try:
                    if str(json.loads(line).get("source_message_id") or "") == str(message_id):
                        return None
                except ValueError:
                    continue

        card = str(ledger.get("selected_capability_card") or "")
        if not card:
            cards = ledger.get("used_manual_cards") or []
            card = str(cards[0]) if cards else ""
        contract = CAPABILITY_CONTRACTS.get(card)
        calls = ledger.get("tool_calls") or []
        actual_tools = [_tool_name(call) for call in calls if _tool_name(call)]
        expected_tools = list((contract or {}).get("allowed_tools") or [])
        payload = _tool_args(calls[-1]) if calls else {}
        operation_id = str(payload.get("operation_id") or ledger.get("operation_id") or message_id)
        trace_id = str(ledger.get("trace_id") or f"trace_{uuid.uuid4().hex}")
        envelope = CommandEnvelope(
            command_id=f"cmd_{uuid.uuid4().hex}",
            tenant_id=str(tenant_id),
            actor_user_id=str(ledger.get("user_id") or ""),
            actor_role=str(ledger.get("role") or "unbound"),
            channel=str(channel),
            source_message_id=str(message_id),
            operation_id=operation_id,
            trace_id=trace_id,
            capability_id=str((contract or {}).get("capability_id") or "unclassified"),
            payload=payload,
            requested_at=str(ledger.get("created_at") or _now()),
        )
        tool_match = bool(contract) and set(actual_tools).issubset(set(expected_tools)) and bool(actual_tools)
        row = {
            **asdict(envelope),
            "shadow_only": True,
            "business_write_executed": False,
            "selected_capability_card": card,
            "expected_tools": expected_tools,
            "actual_tools": actual_tools,
            "tool_match": tool_match,
            "permission_match": ledger.get("guard_result") not in {"permission_denied", "denied"},
            "legacy_handler_intercepted": bool(ledger.get("legacy_handler_intercepted")),
            "writeback_verified_observed": ledger.get("writeback_verified"),
            "render_verified_observed": ledger.get("render_verified"),
            "comparison_status": "matched" if tool_match else ("not_in_stage1_contract" if not contract else "mismatch"),
            "ledger_id": str(ledger.get("ledger_id") or ""),
            "raw_text_sha256": hashlib.sha256(str(ledger.get("raw_text") or "").encode("utf-8")).hexdigest(),
            "observed_at": _now(),
        }
        shadow_path.parent.mkdir(parents=True, exist_ok=True)
        with shadow_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        try:
            from .ai_operator import schedule_shadow

            schedule_shadow(
                data_dir=root,
                ledger=ledger,
                config_path=root.parent.parent / "config" / "ai_operator_config.json",
            )
        except Exception:
            # Shadow observation must never affect the production response.
            pass
        return row
