"""Deterministic record repository used by the production CommandBus canary."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any
import uuid

from .models import UserIdentity
from .programs import SUMMER_PROGRAM_ID, canonical_program_id
from .store import TuoguanStore
from .student_resolver import resolve_student_for_record


TENANT_ID = "youyi_tuoguan"
CHANNEL = "wecom_callback"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def classify_record_type(text: str) -> str:
    value = str(text or "")
    if "午休" in value:
        return "nap_behavior"
    if any(word in value for word in ("纪律", "排队", "安静", "说话")):
        return "discipline_behavior"
    if any(word in value for word in ("吃饭", "喝水", "整理", "书包", "生活")):
        return "life_behavior"
    if any(word in value for word in ("表扬", "主动", "认真", "进步", "表现好", "不错")):
        return "positive_behavior"
    if any(word in value for word in ("不牢", "错题", "需要", "继续跟进", "薄弱")):
        return "learning_follow_up"
    return "learning_behavior"


def normalized_summary(original_text: str, student_name: str) -> str:
    text = str(original_text or "").strip().rstrip("。！？!?；;")
    if text.startswith(student_name):
        text = text[len(student_name):].strip("：:，, ")
    return text


def create_daily_record(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    requested_student_name: str,
    original_text: str,
    source_message_id: str,
    operation_id: str,
) -> dict[str, Any]:
    name, profile = resolve_student_for_record(store, identity, requested_student_name)
    if not name:
        code = str(profile.get("reason_code") or "student_not_found")
        messages = {
            "student_name_ambiguous": "这个姓名对应多名学生，请补充完整姓名或其他识别信息。",
            "permission_denied": "当前账号无权记录这名学生，请联系金总确认负责范围。",
            "cross_tenant_denied": "这名学生不属于当前机构，不能写入记录。",
        }
        return {"ok": False, "reason_code": code, "message": messages.get(code, "没有查到这个学生，请确认姓名。"), "writeback_verified": False}
    summary = normalized_summary(original_text, name)
    if len(summary) < 6:
        return {"ok": False, "reason_code": "record_content_insufficient", "message": f"请再具体说一下{name}今天的表现或老师做了什么。", "writeback_verified": False}

    records = store.read_json("records.json", [])
    if not isinstance(records, list):
        records = []
    existing = next((item for item in records if isinstance(item, dict) and str(item.get("operation_id") or item.get("source_message_id") or "") == operation_id), None)
    if existing:
        return {
            "ok": True,
            "reason_code": "already_applied",
            "already_applied": True,
            "record_id": str(existing.get("record_id") or existing.get("id") or ""),
            "record": deepcopy(existing),
            "writeback_verified": True,
            "rendered_text": f"{name}的这条记录已经保存过，不需要重复记录。",
        }

    stamp = _now()
    record_id = f"record_{uuid.uuid4().hex[:12]}"
    program_id = SUMMER_PROGRAM_ID if canonical_program_id(profile.get("program_id"), default=SUMMER_PROGRAM_ID) == SUMMER_PROGRAM_ID else canonical_program_id(profile.get("program_id"))
    record = {
        "record_id": record_id,
        "id": record_id,
        "tenant_id": TENANT_ID,
        "student_id": str(profile.get("student_id") or ""),
        "student_name": name,
        "program_id": program_id,
        "operator_user_id": identity.canonical_user_id,
        "operator_role": identity.role,
        "source_message_id": str(source_message_id),
        "operation_id": str(operation_id),
        "original_text": str(original_text or "").strip(),
        "normalized_summary": summary,
        "record_type": classify_record_type(summary),
        "created_at": stamp,
        "channel": CHANNEL,
        "content": summary,
        "source_text": str(original_text or "").strip(),
        "teacher": identity.canonical_user_id,
        "timestamp": stamp,
        "should_create_task": False,
    }
    records.append(record)
    store.write_json("records.json", records)
    persisted = store.read_json("records.json", [])
    saved = next((item for item in persisted if isinstance(item, dict) and str(item.get("record_id") or item.get("id") or "") == record_id), None) if isinstance(persisted, list) else None
    verified = bool(saved and str(saved.get("operation_id") or "") == operation_id and str(saved.get("student_name") or "") == name)
    return {
        "ok": verified,
        "reason_code": "success" if verified else "writeback_consistency_failed",
        "record_id": record_id,
        "record": deepcopy(saved or record),
        "writeback_verified": verified,
        "rendered_text": f"已记录到{name}档案：{summary}。" if verified else "记录写入反查未通过，暂时不能确认成功。",
    }


def query_recent_records(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    requested_student_name: str,
    limit: int = 5,
) -> dict[str, Any]:
    name, profile = resolve_student_for_record(store, identity, requested_student_name)
    if not name:
        code = str(profile.get("reason_code") or "student_not_found")
        return {"ok": False, "reason_code": code, "message": "没有查到这个学生，或当前账号没有查看权限。"}
    records = store.read_json("records.json", [])
    matched = [
        deepcopy(item) for item in (records if isinstance(records, list) else [])
        if isinstance(item, dict) and str(item.get("student_name") or item.get("student") or "") == name
    ]
    matched.sort(key=lambda item: str(item.get("created_at") or item.get("timestamp") or ""), reverse=True)
    shown = matched[:max(1, min(int(limit or 5), 10))]
    lines = [f"{name}最近记录共 {len(matched)} 条，以下展示最近 {len(shown)} 条："]
    if not shown:
        lines.append("当前还没有记录。")
    else:
        for index, item in enumerate(shown, 1):
            when = str(item.get("created_at") or item.get("timestamp") or "")[:10]
            summary = str(item.get("normalized_summary") or item.get("content") or item.get("source_text") or "").strip()
            lines.append(f"{index}. {when or '未标日期'}：{summary}")
    return {
        "ok": True,
        "reason_code": "success",
        "result_count": len(matched),
        "rendered_count": len(shown),
        "records": shown,
        "rendered_text": "\n".join(lines),
        "render_verified": True,
    }
