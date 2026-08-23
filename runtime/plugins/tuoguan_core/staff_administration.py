"""Audited owner-only staff offboarding without deleting business history."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any
import uuid

from .models import UserIdentity
from .staff_directory import query_staff_directory
from .store import TuoguanStore


STAFF_OFFBOARDING_EVENTS_FILE = "staff_offboarding_events.jsonl"
_INACTIVE_STATUSES = {"inactive", "left", "offboarded", "terminated", "离职", "停用"}
_UNSENT_STATUSES = {"pending", "retry_pending"}


def offboard_staff(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    target_name: str = "",
    target_user_id: str = "",
    reason: str = "",
    source_text: str = "",
) -> dict[str, Any]:
    """Revoke one employee's business access and preserve their history."""

    if identity.role != "boss":
        return {
            "ok": False,
            "error": "permission_denied",
            "message": "只有老板可以办理人员离职停用。",
            "writeback_verified": False,
        }
    requested = str(target_user_id or target_name or "").strip()
    if not requested:
        return {
            "ok": False,
            "error": "missing_target",
            "message": "请明确要办理离职停用的老师或店长姓名。",
            "writeback_verified": False,
        }

    resolution = query_staff_directory(store, query=requested, include_inactive=True, limit=100)
    candidates = [item for item in resolution.get("staff") or [] if isinstance(item, dict)]
    if target_user_id:
        candidates = [item for item in candidates if str(item.get("user_id") or "") == str(target_user_id)]
    if candidates:
        best_score = max(int(item.get("match_score") or 0) for item in candidates)
        candidates = [item for item in candidates if int(item.get("match_score") or 0) == best_score]
    if not candidates:
        return {
            "ok": False,
            "error": "staff_not_found",
            "message": "当前人员目录没有找到唯一匹配对象，本轮没有修改任何权限。",
            "writeback_verified": False,
        }
    if len(candidates) != 1:
        return {
            "ok": False,
            "error": "ambiguous_target",
            "message": "找到多名匹配人员，请补充企业微信 user_id 或更准确的姓名。",
            "data": {
                "candidates": [
                    {
                        "user_id": str(item.get("user_id") or ""),
                        "business_name": str(item.get("business_name") or ""),
                        "role": str(item.get("role") or ""),
                        "membership_status": str(item.get("membership_status") or ""),
                    }
                    for item in candidates[:10]
                ]
            },
            "writeback_verified": False,
        }

    target = candidates[0]
    user_id = str(target.get("user_id") or "")
    if not user_id:
        return {
            "ok": False,
            "error": "staff_not_found",
            "message": "匹配人员缺少可信企业微信 user_id，本轮没有修改任何权限。",
            "writeback_verified": False,
        }
    whitelist_before = _dict(store.read_json("wecom_whitelist.json", {}))
    super_users = {str(value) for value in whitelist_before.get("super_users") or [] if str(value)}
    if user_id == identity.canonical_user_id or user_id == identity.platform_user_id or user_id in super_users:
        return {
            "ok": False,
            "error": "protected_owner_account",
            "message": "老板最高管理账号不能通过人员离职工具停用。",
            "writeback_verified": False,
        }

    now = datetime.now().astimezone().isoformat(timespec="seconds")
    normalized_reason = str(reason or "老板确认该员工已经离职").strip()[:500]
    event_id = f"staff_offboarding_{uuid.uuid4().hex}"
    previous_role = str(target.get("role") or "staff")
    previous_status = str(target.get("employment_status") or target.get("membership_status") or "active")

    def revoke_whitelist(value: Any) -> dict[str, Any]:
        data = _dict(value)
        for key in ("allowed_users", "manager_ids", "summer_manager_ids", "pending_users"):
            data[key] = [item for item in data.get(key) or [] if str(item) != user_id]
        if str(data.get("manager_id") or "") == user_id:
            data["manager_id"] = ""
        roles = _dict(data.get("user_roles"))
        roles.pop(user_id, None)
        data["user_roles"] = roles
        applications = data.get("pending_applications") or []
        data["pending_applications"] = [
            item for item in applications
            if not isinstance(item, dict) or str(item.get("user_id") or "") != user_id
        ]
        rejected = {str(item) for item in data.get("rejected_users") or [] if str(item)}
        rejected.add(user_id)
        data["rejected_users"] = sorted(rejected)
        offboarded = [item for item in data.get("offboarded_users") or [] if isinstance(item, dict)]
        offboarded = [item for item in offboarded if str(item.get("user_id") or "") != user_id]
        offboarded.append({
            "user_id": user_id,
            "name": str(target.get("business_name") or target_name or user_id),
            "previous_role": previous_role,
            "offboarded_at": now,
            "offboarded_by": identity.canonical_user_id,
            "reason": normalized_reason,
            "event_id": event_id,
        })
        data["offboarded_users"] = offboarded[-500:]
        return data

    def mark_staff(value: Any) -> dict[str, Any]:
        data = _dict(value)
        profile = deepcopy(data.get(user_id)) if isinstance(data.get(user_id), dict) else {}
        profile.update({
            "user_id": user_id,
            "name": str(profile.get("name") or target.get("business_name") or target_name or user_id),
            "role": str(profile.get("role") or previous_role),
            "status": "left",
            "previous_status": str(profile.get("status") or previous_status),
            "offboarded_at": now,
            "offboarded_by": identity.canonical_user_id,
            "offboarding_reason": normalized_reason,
            "updated_at": now,
            "updated_by": identity.canonical_user_id,
        })
        data[user_id] = profile
        return data

    suppressed_count = 0
    result_unknown_count = 0

    def suppress_outbox(value: Any) -> list[dict[str, Any]]:
        nonlocal suppressed_count, result_unknown_count
        rows = value if isinstance(value, list) else []
        for item in rows:
            if not isinstance(item, dict):
                continue
            recipient = str(
                item.get("touser") or item.get("target_user_id")
                or item.get("recipient_user_id") or item.get("to_user_id") or ""
            )
            if recipient != user_id:
                continue
            status = str(item.get("status") or "")
            if status in _UNSENT_STATUSES:
                item.update({
                    "status": "suppressed",
                    "suppressed_reason": "staff_offboarded",
                    "suppressed_at": now,
                    "staff_offboarding_event_id": event_id,
                })
                item.pop("retry_at", None)
                suppressed_count += 1
            elif status == "sending":
                item.update({
                    "status": "result_unknown",
                    "last_error": "staff_offboarded_during_send_lease",
                    "result_unknown_at": now,
                    "staff_offboarding_event_id": event_id,
                })
                item.pop("retry_at", None)
                result_unknown_count += 1
        return rows

    whitelist_after = store.update_json("wecom_whitelist.json", {}, revoke_whitelist)
    staff_after = store.update_json("staff.json", {}, mark_staff)
    outbox_after = store.update_json("notification_outbox.json", [], suppress_outbox)
    event = {
        "event_id": event_id,
        "action": "staff_offboarded",
        "target_user_id": user_id,
        "target_name": str(target.get("business_name") or target_name or user_id),
        "previous_role": previous_role,
        "previous_status": previous_status,
        "reason": normalized_reason,
        "source_text": str(source_text or "")[:1000],
        "actor_user_id": identity.canonical_user_id,
        "actor_role": identity.role,
        "created_at": now,
        "history_preserved": True,
        "wecom_directory_modified": False,
        "suppressed_outbox_count": suppressed_count,
        "result_unknown_outbox_count": result_unknown_count,
    }
    event_verified = store.append_jsonl_verified(STAFF_OFFBOARDING_EVENTS_FILE, event)

    active_sets = {
        str(item)
        for key in ("allowed_users", "manager_ids", "summer_manager_ids", "super_users")
        for item in whitelist_after.get(key) or []
    }
    rejected_after = {str(item) for item in whitelist_after.get("rejected_users") or []}
    profile_after = _dict(staff_after.get(user_id))
    unsent_after = [
        item for item in outbox_after
        if isinstance(item, dict)
        and str(item.get("touser") or item.get("target_user_id") or item.get("recipient_user_id") or "") == user_id
        and str(item.get("status") or "") in (_UNSENT_STATUSES | {"sending"})
    ]
    verified = bool(
        user_id not in active_sets
        and user_id in rejected_after
        and str(profile_after.get("status") or "").lower() in _INACTIVE_STATUSES
        and not unsent_after
        and event_verified
    )
    return {
        "ok": verified,
        "error": "" if verified else "writeback_failed",
        "message": (
            f"已将{event['target_name']}标记为离职并撤销托管业务访问；历史任务和服务记录已保留。"
            if verified else "离职停用写后反查没有完全通过，暂时不能确认完成。"
        ),
        "data": {
            "event_id": event_id,
            "target_user_id": user_id,
            "target_name": event["target_name"],
            "status": str(profile_after.get("status") or ""),
            "access_revoked": user_id not in active_sets and user_id in rejected_after,
            "history_preserved": True,
            "wecom_directory_modified": False,
            "suppressed_outbox_count": suppressed_count,
            "result_unknown_outbox_count": result_unknown_count,
            "writeback_verified": verified,
        },
        "writeback_verified": verified,
    }


def staff_is_offboarded(store: TuoguanStore, user_id: str) -> bool:
    """Return whether a recipient is explicitly inactive or access-revoked."""

    wanted = str(user_id or "").strip()
    if not wanted:
        return False
    whitelist = _dict(store.read_json("wecom_whitelist.json", {}))
    if wanted in {str(item) for item in whitelist.get("rejected_users") or []}:
        return True
    staff = _dict(store.read_json("staff.json", {}))
    profile = _dict(staff.get(wanted))
    return str(profile.get("status") or "").strip().lower() in _INACTIVE_STATUSES


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
