"""Single-owner arbitration and verified business-claim enforcement."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import re
from typing import Any


_CLOSED = {"closed", "completed", "done", "cancelled", "canceled"}
_CLAIMS = {
    "workflow_closed": re.compile(
        r"(?:已|已经)(?:正式)?(?:完成(?:了)?|进入)?(?:安全事件|处理流程|事件)?(?:正式)?闭环"
        r"|全过程已闭环|安全测试通过|没有遗漏"
    ),
    "task_completed": re.compile(r"任务.{0,8}(已完成|完成了|已处理完)"),
    "workflow_stage_changed": re.compile(r"(已进入|已经进入|已推进到|当前进入).{0,12}(观察|复核|审核|闭环|下一阶段)"),
    "record_created": re.compile(r"(已记录|记录好了|已录入|已写入).{0,12}(档案|记录|系统)?"),
    "points_changed": re.compile(r"已给.+(加|扣|减)\s*\d+\s*分"),
    "review_approved": re.compile(r"(审核已通过|已审核通过|审核通过并闭环)"),
    "notification_sent": re.compile(r"(已通知|通知已发送|已经提醒).{0,12}(老板|店长|老师|负责人|家长)?"),
}


@dataclass(frozen=True)
class OwnershipDecision:
    ownership_type: str
    owner_capability: str
    message_owner_id: str
    context_source: str
    active_business_object: str


def owner_id(tenant_id: str, source_message_id: str) -> str:
    digest = hashlib.sha256(f"{tenant_id}|{source_message_id}".encode("utf-8")).hexdigest()[:24]
    return f"owner_{digest}"


def active_business_owner(store: Any, identity: Any, source_message_id: str) -> OwnershipDecision | None:
    payload = store.read_json("core_workflow_contexts.json", {})
    if not isinstance(payload, dict):
        return None
    tenant_id = "youyi_tuoguan"
    prefix = f"{tenant_id}:{identity.canonical_user_id}:"
    candidates: list[tuple[int, str, dict[str, Any]]] = []
    for key, row in payload.items():
        if not str(key).startswith(prefix) or not isinstance(row, dict):
            continue
        try:
            if datetime.fromisoformat(str(row.get("expires_at") or "")) < datetime.now().astimezone():
                continue
        except ValueError:
            continue
        context_type = str(row.get("context_type") or str(key).rsplit(":", 1)[-1])
        priority = {"safety_workflow": 30, "ordinary_task": 20, "student_record_draft": 10}.get(context_type, 0)
        if priority:
            candidates.append((priority, context_type, row))
    if not candidates:
        return None
    _, context_type, row = sorted(candidates, key=lambda item: item[0], reverse=True)[0]
    capability = {
        "safety_workflow": "safety_workflow_coach",
        "ordinary_task": "teacher_task_guidance",
        "student_record_draft": "student_record_coach_guidance",
    }[context_type]
    object_id = str(row.get("event_id") or row.get("task_id") or row.get("draft_id") or "")
    return OwnershipDecision("business_owned", capability, owner_id(tenant_id, source_message_id), context_type, object_id)


def business_claims(text: str) -> list[str]:
    value = str(text or "")
    value = re.sub(r"(?:比如|例如)[:：]?[^\n。！？!?]*", "", value)
    value = re.sub(r"[^\n。！？!?]*(?:是否|能否)[^\n。！？!?]*[。！？!?]?", "", value)
    return [name for name, pattern in _CLAIMS.items() if pattern.search(value)]


def guard_business_claims(text: str, *, allowed_claims: set[str] | None = None) -> tuple[str, list[str]]:
    blocked = [claim for claim in business_claims(text) if claim not in (allowed_claims or set())]
    if not blocked:
        return str(text or ""), []
    return "我看到了你补充的信息，但当前没有经过可信业务结果确认，所以不能说已经完成或闭环。我会继续按当前业务状态处理。", blocked


def allowed_claims_from_result(tool_name: str, result: dict[str, Any]) -> set[str]:
    data = result.get("data") if isinstance(result.get("data"), dict) else result
    if not (result.get("ok") and isinstance(data, dict)):
        return set()
    writeback = bool(result.get("writeback_verified") or data.get("writeback_verified"))
    claims: set[str] = set()
    if writeback and data.get("write_intent") is True: claims.add("record_created")
    if writeback and tool_name in {"tuoguan_record_student", "tuoguan_record_student_daily", "tuoguan_record_summer_lesson", "task_workflow_command", "safety_workflow_command"}: claims.add("record_created")
    if writeback and tool_name == "tuoguan_update_task": claims.add("record_created")
    if writeback and tool_name == "tuoguan_change_summer_points": claims.add("points_changed")
    task_status = str((data.get("task") or {}).get("status") or data.get("task_status") or "").lower()
    if writeback and task_status in _CLOSED: claims.add("task_completed")
    if writeback and data.get("workflow_state"): claims.add("workflow_stage_changed")
    if writeback and str(data.get("workflow_state") or data.get("status") or "").lower() == "closed": claims.update({"workflow_closed", "review_approved"})
    if data.get("notification_sent") is True: claims.add("notification_sent")
    return claims
