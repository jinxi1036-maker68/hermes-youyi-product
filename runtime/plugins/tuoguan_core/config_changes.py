"""Confirmation-first personnel and project configuration changes."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from typing import Any

from .models import UserIdentity
from .store import TuoguanStore


PENDING_CONFIG_CHANGES_FILE = "pending_config_changes.json"
CONFIG_CHANGE_AUDIT_FILE = "config_change_audit.jsonl"

_CONFIRM_WORDS = {"确认执行", "确认变更", "执行变更"}
_CANCEL_WORDS = {"取消执行", "取消变更", "先不执行"}


def is_config_change_request(text: str) -> bool:
    compact = str(text or "").replace(" ", "")
    if not compact:
        return False
    triggers = (
        "新增老师",
        "添加老师",
        "开通老师",
        "停用老师",
        "禁用老师",
        "老师离职",
        "离职老师",
        "交接",
        "接手",
        "替换老师",
        "学生转给",
        "转给老师",
        "转给店长",
        "暑假班老师",
        "暑假老师",
        "暑假班负责人",
        "项目负责人",
        "项目关闭",
        "关闭暑假班",
        "批量反馈",
    )
    return any(term in compact for term in triggers)


def is_config_confirmation(text: str) -> bool:
    compact = str(text or "").replace(" ", "")
    return compact in _CONFIRM_WORDS


def is_config_cancel(text: str) -> bool:
    compact = str(text or "").replace(" ", "")
    return compact in _CANCEL_WORDS


def classify_config_change(text: str) -> str:
    compact = str(text or "").replace(" ", "")
    if any(term in compact for term in ("交接", "接手", "替换老师", "学生转给", "转给老师", "转给店长", "老师离职", "离职老师")):
        return "teacher_handover"
    if any(term in compact for term in ("新增老师", "添加老师", "开通老师")):
        return "teacher_onboarding"
    if any(term in compact for term in ("停用老师", "禁用老师")):
        return "teacher_deactivation"
    if any(term in compact for term in ("暑假班", "暑假老师", "项目负责人", "项目关闭", "关闭暑假班")):
        return "summer_project_config"
    if "批量反馈" in compact:
        return "bulk_feedback_config"
    return "config_change"


def _extract_effective_date(text: str) -> str:
    compact = str(text or "").replace(" ", "")
    match = re.search(r"(\d{1,2})月(\d{1,2})日", compact)
    if match:
        month, day = match.groups()
        year = datetime.now().year
        return f"{year:04d}-{int(month):02d}-{int(day):02d}"
    if "明天" in compact:
        return "明天"
    if "今天" in compact or "即日起" in compact:
        return "今天"
    return ""


def build_config_change_proposal(
    *,
    store: TuoguanStore,
    identity: UserIdentity,
    text: str,
) -> dict[str, Any]:
    kind = classify_config_change(text)
    now = datetime.now().isoformat(timespec="seconds")
    proposal = {
        "id": f"cfg_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}",
        "kind": kind,
        "status": "waiting_confirmation",
        "requested_by": identity.canonical_user_id,
        "requested_by_name": identity.person_name or identity.platform_user_id,
        "requested_by_role": identity.role,
        "source_text": text,
        "effective_date": _extract_effective_date(text),
        "created_at": now,
        "updated_at": now,
        "safety_rules": [
            "企业微信 userid 是唯一身份来源，不按姓名或消息内容猜身份。",
            "交接只影响生效日后的当前负责人，历史记录、工资和旧老师归属不覆盖。",
            "暑假临时老师工资默认线下人工结算，不进入普通老师绩效工资。",
            "家长反馈、批量发送和项目关闭必须先进入审核队列，不能自动外发。",
        ],
    }
    items = store.read_json(PENDING_CONFIG_CHANGES_FILE, [])
    if not isinstance(items, list):
        items = []
    items.append(proposal)
    store.write_json(PENDING_CONFIG_CHANGES_FILE, items[-500:])
    return proposal


def latest_pending_config_change(
    *,
    store: TuoguanStore,
    identity: UserIdentity,
) -> dict[str, Any] | None:
    items = store.read_json(PENDING_CONFIG_CHANGES_FILE, [])
    if not isinstance(items, list):
        return None
    for item in reversed(items):
        if not isinstance(item, dict):
            continue
        if str(item.get("status") or "") != "waiting_confirmation":
            continue
        if str(item.get("requested_by") or "") == identity.canonical_user_id:
            return item
    return None


def confirm_config_change(
    *,
    store: TuoguanStore,
    identity: UserIdentity,
    text: str,
) -> dict[str, Any] | None:
    items = store.read_json(PENDING_CONFIG_CHANGES_FILE, [])
    if not isinstance(items, list):
        return None
    target: dict[str, Any] | None = None
    for item in reversed(items):
        if isinstance(item, dict) and str(item.get("status") or "") == "waiting_confirmation" and str(item.get("requested_by") or "") == identity.canonical_user_id:
            target = item
            break
    if target is None:
        return None
    now = datetime.now().isoformat(timespec="seconds")
    target["status"] = "confirmed_pending_execution"
    target["confirmed_by"] = identity.canonical_user_id
    target["confirmed_by_name"] = identity.person_name or identity.platform_user_id
    target["confirmed_at"] = now
    target["confirm_text"] = text
    target["updated_at"] = now
    store.write_json(PENDING_CONFIG_CHANGES_FILE, items[-500:])
    _append_config_change_audit(store, target, action="confirmed")
    return target


def cancel_config_change(
    *,
    store: TuoguanStore,
    identity: UserIdentity,
    text: str,
) -> dict[str, Any] | None:
    item = latest_pending_config_change(store=store, identity=identity)
    if item is None:
        return None
    items = store.read_json(PENDING_CONFIG_CHANGES_FILE, [])
    if not isinstance(items, list):
        return None
    now = datetime.now().isoformat(timespec="seconds")
    for existing in items:
        if isinstance(existing, dict) and existing.get("id") == item.get("id"):
            existing["status"] = "cancelled"
            existing["cancelled_by"] = identity.canonical_user_id
            existing["cancel_text"] = text
            existing["updated_at"] = now
            item = existing
            break
    store.write_json(PENDING_CONFIG_CHANGES_FILE, items[-500:])
    _append_config_change_audit(store, item, action="cancelled")
    return item


def _append_config_change_audit(store: TuoguanStore, item: dict[str, Any], *, action: str) -> None:
    path = store.path_for(CONFIG_CHANGE_AUDIT_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "action": action,
        "change_id": str(item.get("id") or ""),
        "kind": str(item.get("kind") or ""),
        "requested_by": str(item.get("requested_by") or ""),
        "source_text": str(item.get("source_text") or ""),
        "status": str(item.get("status") or ""),
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def proposal_reply(item: dict[str, Any]) -> str:
    kind_label = {
        "teacher_handover": "老师交接/学生归属变更",
        "teacher_onboarding": "新增老师/开通身份",
        "teacher_deactivation": "停用老师身份",
        "summer_project_config": "暑假班项目配置",
        "bulk_feedback_config": "批量反馈配置",
    }.get(str(item.get("kind") or ""), "配置变更")
    effective = str(item.get("effective_date") or "待确认")
    return (
        f"我识别到这是【{kind_label}】，这类会影响身份、权限、学生归属或项目范围，不能直接执行。\n"
        f"待确认编号：{item.get('id')}\n"
        f"生效时间：{effective}\n"
        "执行前保护：不覆盖历史记录、不改历史工资、不按姓名猜身份，暑假临时老师工资不进入普通绩效。\n"
        "请机构负责人核对无误后回复“确认执行”；如不执行，回复“取消执行”。"
    )


def confirmation_reply(item: dict[str, Any]) -> str:
    return (
        f"已确认配置变更：{item.get('id')}。\n"
        "我已写入配置变更审计，状态为“已确认待执行”。\n"
        "为保护历史记录和工资证据链，本次不会自动覆盖旧老师历史归属；后续执行器只应从生效日开始变更当前负责人。"
    )

