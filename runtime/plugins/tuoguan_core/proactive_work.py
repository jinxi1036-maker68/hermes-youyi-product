"""Audited proactive authorization, outreach, and goal-action state.

The model chooses whether an action is useful.  This module only validates the
chosen action against trusted identity, current staff facts, authorization,
time/frequency limits, idempotency, and writeback evidence.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
import re
import uuid
from typing import Any

from .models import UserIdentity
from .store import JSON_NO_CHANGE, TuoguanStore
from .tenant_context import current_tenant_id


PROACTIVE_AUTHORIZATIONS_FILE = "proactive_authorizations.jsonl"
GOAL_ACTIONS_FILE = "goal_actions.jsonl"
RELATIONSHIP_TOUCH_FILE = "relationship_touch_candidates.jsonl"
OUTBOX_FILE = "notification_outbox.json"

PROACTIVE_ACTION_TYPES = {
    "owner_decision",
    "ask_work_fact",
    "ask_task_fact",
    "ask_operating_fact",
    "ask_task_result",
    "task_companion_followup",
    "ask_student_service_fact",
    "follow_up",
    "assign_low_risk_goal_task",
}
GOAL_ACTION_TYPES = {
    "query_internal_data",
    "fill_institution_fact",
    "ask_staff_fact",
    "prepare_material",
    "create_low_risk_task",
    "follow_up",
    "verify_evidence",
    "review_and_report",
}
GOAL_ACTION_STATUSES = {
    "planned",
    "due",
    "executing",
    "waiting_reply",
    "replied_partial",
    "replied_sufficient",
    "verified",
    "resolved",
    "blocked",
    "failed",
    "cancelled",
    "superseded",
    "escalated",
}
OPEN_GOAL_ACTION_STATUSES = {
    "planned", "due", "executing", "waiting_reply", "replied_partial", "replied_sufficient", "blocked", "failed", "escalated"
}
TERMINAL_RELATIONSHIP_STATUSES = {"resolved", "expired", "superseded", "suppressed"}
ACTIVE_STAFF_STATUSES = {"", "active", "approved", "employed", "on_duty", "在职", "正常"}
HIGH_RISK_TERMS = (
    "工资", "绩效", "处罚", "罚款", "制度", "权限", "删除", "开除", "辞退",
    "联系家长", "给家长发", "家长群", "安全事故闭环", "隐瞒事故",
)
GOAL_TASK_HIGH_RISK_TERMS = (
    "工资", "绩效", "处罚", "罚款", "制度", "权限", "删除", "开除", "辞退",
    "安全事故闭环", "隐瞒事故",
)


def _now(value: datetime | None = None) -> datetime:
    return (value or datetime.now().astimezone()).astimezone()


def _now_iso(value: datetime | None = None) -> str:
    return _now(value).isoformat(timespec="seconds")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _limit(value: Any, maximum: int = 700) -> str:
    return str(value or "").strip()[:maximum]


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone() if parsed.tzinfo else parsed.astimezone()


def _read_jsonl(store: TuoguanStore, name: str) -> list[dict[str, Any]]:
    path = store.path_for(name)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _append(store: TuoguanStore, name: str, row: dict[str, Any]) -> bool:
    return store.append_jsonl_verified(name, row)


def _identity_ids(identity: UserIdentity) -> set[str]:
    return {
        value for value in (
            str(identity.canonical_user_id or "").strip(),
            str(identity.platform_user_id or "").strip(),
        ) if value
    }


def _fold_events(store: TuoguanStore, filename: str, id_key: str, update_type: str) -> dict[str, dict[str, Any]]:
    folded: dict[str, dict[str, Any]] = {}
    for row in _read_jsonl(store, filename):
        record_id = str(row.get(id_key) or "")
        if not record_id:
            continue
        if str(row.get("record_type") or "") == update_type:
            if record_id not in folded:
                continue
            changed = row.get("changes") if isinstance(row.get("changes"), dict) else {}
            folded[record_id].update(deepcopy(changed))
            folded[record_id]["status"] = str(row.get("status") or folded[record_id].get("status") or "")
            folded[record_id]["updated_at"] = str(row.get("created_at") or "")
            folded[record_id].setdefault("events", []).append(deepcopy(row))
            continue
        if record_id in folded:
            continue
        folded[record_id] = deepcopy(row)
        folded[record_id].setdefault("events", [])
    return folded


def _authorization_rows(store: TuoguanStore) -> dict[str, dict[str, Any]]:
    return _fold_events(store, PROACTIVE_AUTHORIZATIONS_FILE, "authorization_id", "proactive_authorization_update")


def submit_proactive_authorization(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    operation_id: str,
    subject_role: str,
    subject_user_ids: list[str] | None,
    action_types: list[str] | None,
    status: str = "active",
    authorization_id: str = "",
    goal_ids: list[str] | None = None,
    daily_limit: int = 1,
    effective_at: str = "",
    expires_at: str = "",
    rollout_stage: str = "pilot",
    source_text: str = "",
) -> dict[str, Any]:
    if identity.role != "boss" and identity.platform != "system":
        return {"ok": False, "error": "permission_denied", "message": "只有老板可以创建、调整或撤销主动工作授权。"}
    role = str(subject_role or "").strip()
    if role not in {"boss", "manager", "teacher"}:
        return {"ok": False, "error": "invalid_subject_role", "message": "主动授权对象只能是老板、店长或老师。"}
    normalized_status = str(status or "active").strip()
    if normalized_status not in {"active", "revoked", "superseded"}:
        return {"ok": False, "error": "invalid_authorization_status", "message": "主动授权状态不合法。"}
    actions = sorted({str(item).strip() for item in action_types or [] if str(item).strip() in PROACTIVE_ACTION_TYPES})
    if normalized_status == "active" and not actions:
        return {"ok": False, "error": "action_types_required", "message": "主动授权必须明确允许的行动类型。"}
    users = sorted({str(item).strip() for item in subject_user_ids or [] if str(item).strip()})
    goals = sorted({str(item).strip() for item in goal_ids or [] if str(item).strip()})
    normalized_limit = max(1, min(int(daily_limit or 1), 3))
    existing = _authorization_rows(store)
    auth_id = str(authorization_id or "").strip()
    if normalized_status in {"revoked", "superseded"}:
        if not auth_id or auth_id not in existing:
            return {"ok": False, "error": "authorization_not_found", "message": "没有找到要撤销的主动授权。"}
        row = {
            "record_type": "proactive_authorization_update",
            "authorization_event_id": _new_id("proactive_auth_event"),
            "authorization_id": auth_id,
            "tenant_id": current_tenant_id(),
            "status": normalized_status,
            "changes": {"revoked_by": identity.canonical_user_id, "revoked_reason": _limit(source_text, 500)},
            "operation_id": str(operation_id or ""),
            "created_at": _now_iso(),
        }
    else:
        fingerprint = hashlib.sha256(json.dumps({
            "tenant": current_tenant_id(), "role": role, "users": users,
            "actions": actions, "goals": goals, "stage": rollout_stage,
            "daily_limit": normalized_limit, "effective_at": effective_at, "expires_at": expires_at,
        }, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        for item in existing.values():
            if item.get("semantic_fingerprint") == fingerprint and item.get("status") == "active":
                return {"ok": True, "authorization": item, "writeback_verified": True, "state_changed": False, "idempotent_replay": True}
        if not auth_id:
            matching_scope = next((
                item for item in existing.values()
                if str(item.get("status") or "") == "active"
                and str(item.get("subject_role") or "") == role
                and sorted(str(value) for value in item.get("subject_user_ids") or []) == users
                and sorted(str(value) for value in item.get("action_types") or []) == actions
                and sorted(str(value) for value in item.get("goal_ids") or []) == goals
                and str(item.get("rollout_stage") or "pilot") == str(rollout_stage or "pilot")
            ), None)
            auth_id = str((matching_scope or {}).get("authorization_id") or "")
        if auth_id:
            if auth_id not in existing:
                return {"ok": False, "error": "authorization_not_found", "message": "没有找到要调整的主动授权。"}
            row = {
                "record_type": "proactive_authorization_update",
                "authorization_event_id": _new_id("proactive_auth_event"),
                "authorization_id": auth_id,
                "tenant_id": current_tenant_id(),
                "status": "active",
                "changes": {
                    "subject_role": role,
                    "subject_user_ids": users,
                    "action_types": actions,
                    "goal_ids": goals,
                    "daily_limit": normalized_limit,
                    "effective_at": str(effective_at or existing[auth_id].get("effective_at") or _now_iso()),
                    "expires_at": str(expires_at or ""),
                    "rollout_stage": str(rollout_stage or "pilot"),
                    "authorized_by": identity.canonical_user_id,
                    "source_text": _limit(source_text, 1000),
                    "semantic_fingerprint": fingerprint,
                },
                "operation_id": str(operation_id or ""),
                "created_at": _now_iso(),
            }
        else:
            auth_id = _new_id("proactive_auth")
            row = {
                "record_type": "proactive_authorization",
                "authorization_id": auth_id,
                "tenant_id": current_tenant_id(),
                "status": "active",
                "subject_role": role,
                "subject_user_ids": users,
                "action_types": actions,
                "goal_ids": goals,
                "daily_limit": normalized_limit,
                "effective_at": str(effective_at or _now_iso()),
                "expires_at": str(expires_at or ""),
                "rollout_stage": str(rollout_stage or "pilot"),
                "authorized_by": identity.canonical_user_id,
                "source_text": _limit(source_text, 1000),
                "semantic_fingerprint": fingerprint,
                "operation_id": str(operation_id or ""),
                "created_at": _now_iso(),
                "updated_at": _now_iso(),
                "boundary": {"contacts_parent": False, "changes_salary": False, "changes_permissions": False},
            }
    _append(store, PROACTIVE_AUTHORIZATIONS_FILE, row)
    folded = _authorization_rows(store).get(auth_id, {})
    verified = str(folded.get("status") or "") == normalized_status
    stopped_delivery = {"suppressed": 0, "result_unknown": 0, "candidate_ids": []}
    if verified and normalized_status in {"revoked", "superseded"}:
        stopped_delivery = _stop_delivery_for_authorization(
            store,
            authorization_id=auth_id,
            identity=identity,
            operation_id=str(operation_id or ""),
            reason=_limit(source_text or "主动授权已撤销。", 500),
        )
    return {
        "ok": verified,
        "authorization": folded,
        "writeback_verified": verified,
        "state_changed": True,
        "stopped_delivery": stopped_delivery,
        "rendered_text": "主动工作授权已保存并完成反查。" if verified else "主动工作授权写入后的反查未通过。",
    }


def _stop_delivery_for_authorization(
    store: TuoguanStore,
    *,
    authorization_id: str,
    identity: UserIdentity,
    operation_id: str,
    reason: str,
) -> dict[str, Any]:
    affected: dict[str, Any] = {"suppressed": 0, "result_unknown": 0, "candidate_ids": []}
    now_iso = _now_iso()

    def stop(rows: Any) -> Any:
        rows = rows if isinstance(rows, list) else []
        changed = False
        for item in rows:
            if not isinstance(item, dict):
                continue
            authorization = item.get("authorization") if isinstance(item.get("authorization"), dict) else {}
            ids = {str(value) for value in authorization.get("authorization_ids") or []}
            if authorization_id not in ids:
                continue
            status = str(item.get("status") or "")
            if status in {"pending", "retry_pending"}:
                item.update({"status": "suppressed", "suppressed_at": now_iso, "suppressed_reason": reason})
                affected["suppressed"] += 1
            elif status == "sending":
                item.update({"status": "result_unknown", "result_unknown_at": now_iso, "last_error": "authorization_revoked_during_send"})
                affected["result_unknown"] += 1
            else:
                continue
            candidate_id = str(item.get("relationship_touch_candidate_id") or "")
            if candidate_id:
                affected["candidate_ids"].append(candidate_id)
            changed = True
        return rows if changed else JSON_NO_CHANGE

    store.update_json(OUTBOX_FILE, [], stop)
    if affected["candidate_ids"]:
        from .digital_employee_state import update_relationship_touch_candidate_status

        for candidate_id in sorted(set(affected["candidate_ids"])):
            current_outbox = store.read_json(OUTBOX_FILE, [])
            item = next((
                row for row in current_outbox if isinstance(row, dict)
                and str(row.get("relationship_touch_candidate_id") or "") == candidate_id
            ), {}) if isinstance(current_outbox, list) else {}
            status = "result_unknown" if str(item.get("status") or "") == "result_unknown" else "superseded"
            update_relationship_touch_candidate_status(
                store,
                identity=identity,
                candidate_id=candidate_id,
                status=status,
                operation_id=f"{operation_id}:stop:{candidate_id}",
                failure_reason=reason,
                source_text="主动授权撤销后的发送收口",
            )
    affected["candidate_ids"] = sorted(set(affected["candidate_ids"]))
    return affected


def query_proactive_authorizations(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    include_inactive: bool = False,
    now_at: str = "",
) -> dict[str, Any]:
    if identity.role not in {"boss", "manager"} and identity.platform != "system":
        return {"ok": False, "error": "permission_denied", "message": "当前账号无权查看全机构主动授权。"}
    now = _parse_time(now_at) or _now()
    rows: list[dict[str, Any]] = []
    for item in _authorization_rows(store).values():
        effective = _authorization_is_effective(item, now)
        if not include_inactive and not effective:
            continue
        copied = deepcopy(item)
        copied["effective"] = effective
        rows.append(copied)
    rows.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
    return {
        "ok": True,
        "authorization_count": len(rows),
        "authorizations": rows,
        "rendered_text": f"当前查到 {len(rows)} 条{'有效' if not include_inactive else ''}主动工作授权。",
        "render_verified": True,
    }


def _authorization_is_effective(item: dict[str, Any], now: datetime) -> bool:
    if str(item.get("status") or "") != "active":
        return False
    effective = _parse_time(item.get("effective_at"))
    expires = _parse_time(item.get("expires_at"))
    return not (effective and now < effective) and not (expires and now >= expires)


def _staff_role(store: TuoguanStore, user_id: str) -> str:
    whitelist = store.read_json("wecom_whitelist.json", {})
    if isinstance(whitelist, dict):
        roles = whitelist.get("user_roles") if isinstance(whitelist.get("user_roles"), dict) else {}
        role = str(roles.get(user_id) or "")
        if role:
            return role
        if user_id in {str(value) for value in whitelist.get("super_users") or []}:
            return "boss"
    staff = store.read_json("staff.json", {})
    profile = staff.get(user_id) if isinstance(staff, dict) else None
    return str(profile.get("role") or "") if isinstance(profile, dict) else ""


def _staff_is_active(store: TuoguanStore, user_id: str, role: str) -> bool:
    whitelist = store.read_json("wecom_whitelist.json", {})
    if not isinstance(whitelist, dict):
        return False
    blocked = {str(value) for value in whitelist.get("blocked_users") or []}
    allowed = {str(value) for value in whitelist.get("allowed_users") or []} | {str(value) for value in whitelist.get("super_users") or []}
    if user_id in blocked or (allowed and user_id not in allowed):
        return False
    if _staff_role(store, user_id) != role:
        return False
    staff = store.read_json("staff.json", {})
    profile = staff.get(user_id) if isinstance(staff, dict) else None
    if isinstance(profile, dict):
        status = str(profile.get("employment_status") or profile.get("status") or "").strip().lower()
        if status not in ACTIVE_STAFF_STATUSES:
            return False
    return True


_TASK_COLLABORATION_ACTIONS = {"ask_task_fact", "ask_task_result", "task_companion_followup"}


def _active_task_collaboration_allowed(
    store: TuoguanStore,
    *,
    role: str,
    user_id: str,
    action_type: str,
    related_task_id: str,
    now: datetime,
) -> bool:
    if role not in {"teacher", "manager"} or action_type not in _TASK_COLLABORATION_ACTIONS or not related_task_id:
        return False
    task = next(
        (
            item
            for item in store.load_tasks()
            if isinstance(item, dict)
            and str(item.get("id") or "") == str(related_task_id)
        ),
        None,
    )
    if not task or str(task.get("status") or "") in {"completed", "cancelled", "closed", "done", "superseded", "expired"}:
        return False
    if str(task.get("assignee_userid") or "") != str(user_id or ""):
        return False
    from .tasks import task_assignment_authority

    authority = task_assignment_authority(store, task)
    assigner_id = str(authority.get("assigner_user_id") or "")
    if not authority.get("trusted"):
        return False
    if not assigner_id and str(authority.get("assigner_role") or "") != "system":
        return False
    # A formal task may require same-evening collaboration after the ordinary
    # relationship window. It still fails closed overnight.
    return "07:00" <= now.strftime("%H:%M") <= "21:30"


def _policy_allows(
    store: TuoguanStore,
    role: str,
    user_id: str,
    action_type: str,
    now: datetime,
    *,
    related_task_id: str = "",
) -> tuple[bool, str, int]:
    from .digital_employee_state import relationship_touch_policy

    policy = relationship_touch_policy(store)
    role_policy = policy.get(role) if isinstance(policy.get(role), dict) else {}
    if str(role_policy.get("mode") or "candidate") != "direct":
        return False, "rollout_stage_not_direct", 0
    allowed_users = {str(value) for value in role_policy.get("allowed_target_user_ids") or []}
    if role != "boss" and (not allowed_users or user_id not in allowed_users):
        return False, "target_not_in_rollout_allowlist", 0
    if role == "boss" and allowed_users and user_id not in allowed_users:
        return False, "target_not_in_rollout_allowlist", 0
    start = str(role_policy.get("allowed_start") or "08:00")
    end = str(role_policy.get("allowed_end") or "19:00")
    current = now.strftime("%H:%M")
    if not start <= current <= end:
        if _active_task_collaboration_allowed(
            store,
            role=role,
            user_id=user_id,
            action_type=action_type,
            related_task_id=related_task_id,
            now=now,
        ):
            return True, "active_task_collaboration_window", max(1, int(role_policy.get("daily_limit") or 1))
        return False, "outside_contact_window", int(role_policy.get("daily_limit") or 1)
    return True, "relationship_policy", max(1, int(role_policy.get("daily_limit") or 1))


def effective_proactive_permission(
    store: TuoguanStore,
    *,
    target_role: str,
    target_user_id: str,
    action_type: str,
    goal_id: str = "",
    related_task_id: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    timestamp = _now(now)
    role = str(target_role or "")
    user_id = str(target_user_id or "")
    action = str(action_type or "ask_work_fact")
    if role == "parent" or not user_id:
        return {"allowed": False, "reason_code": "parent_or_missing_target"}
    if role not in {"boss", "manager", "teacher"} or not _staff_is_active(store, user_id, role):
        return {"allowed": False, "reason_code": "target_role_or_employment_invalid"}
    policy_allowed, policy_reason, policy_limit = _policy_allows(
        store,
        role,
        user_id,
        action,
        timestamp,
        related_task_id=related_task_id,
    )
    if not policy_allowed:
        return {"allowed": False, "reason_code": policy_reason}
    authorization_rows = _authorization_rows(store)
    matching: list[dict[str, Any]] = []
    for item in authorization_rows.values():
        if not _authorization_is_effective(item, timestamp):
            continue
        if str(item.get("subject_role") or "") != role:
            continue
        users = {str(value) for value in item.get("subject_user_ids") or []}
        if users and user_id not in users:
            continue
        actions = {str(value) for value in item.get("action_types") or []}
        legacy_task_fact_grant = action in _TASK_COLLABORATION_ACTIONS and "ask_work_fact" in actions
        if actions and action not in actions and not legacy_task_fact_grant:
            continue
        goals = {str(value) for value in item.get("goal_ids") or []}
        if goals and str(goal_id or "") not in goals:
            continue
        matching.append(item)
    if authorization_rows and not matching:
        return {"allowed": False, "reason_code": "formal_authorization_required"}
    # Existing explicit direct policy remains a backwards-compatible baseline;
    # formal grants add source evidence and may only narrow the daily limit.
    limits = [policy_limit] + [int(item.get("daily_limit") or policy_limit) for item in matching]
    return {
        "allowed": True,
        "reason_code": "formal_authorization" if matching else policy_reason,
        "daily_limit": max(1, min(limits)),
        "authorization_ids": [str(item.get("authorization_id") or "") for item in matching],
        "rollout_stage": str(matching[0].get("rollout_stage") or "policy") if matching else "policy",
    }


def _relationship_rows(store: TuoguanStore) -> dict[str, dict[str, Any]]:
    from .digital_employee_state import _fold_relationship_touch_candidates

    return _fold_relationship_touch_candidates(store)


def _unsafe_staff_message(text: str) -> bool:
    compact = re.sub(r"\s+", "", str(text or ""))
    return any(term in compact for term in ("联系家长", "给家长发", "家长群", "点赞", "评论", "私信"))


def execute_relationship_touch(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    candidate_id: str,
    operation_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    from .digital_employee_state import update_relationship_touch_candidate_status

    candidate = _relationship_rows(store).get(str(candidate_id or "").strip())
    if not candidate:
        return {"ok": False, "error": "relationship_touch_not_found", "message": "没有找到这条主动联系候选。"}
    if identity.role != "boss" and identity.platform != "system":
        return {"ok": False, "error": "permission_denied", "message": "当前账号无权执行主动联系候选。"}
    status = str(candidate.get("status") or "candidate")
    if status in TERMINAL_RELATIONSHIP_STATUSES:
        return {"ok": False, "error": "relationship_touch_closed", "message": "这条主动联系已关闭或过期。"}
    target_role = str(candidate.get("target_role") or "")
    target_user_id = str(candidate.get("target_user_id") or "")
    message = str(candidate.get("message") or "").strip()
    if target_role == "parent" or _unsafe_staff_message(message):
        return {"ok": False, "error": "parent_outreach_boundary", "message": "当前阶段禁止小优主动联系家长或借老师代发家长消息。"}
    if target_role in {"manager", "teacher"} and not bool(candidate.get("work_related")):
        return {"ok": False, "error": "work_fact_required", "message": "主动联系老师或店长必须围绕明确工作事实。"}
    timestamp = _now(now)
    permission = effective_proactive_permission(
        store,
        target_role=target_role,
        target_user_id=target_user_id,
        action_type=str(candidate.get("action_type") or "ask_work_fact"),
        goal_id=str(candidate.get("goal_id") or ""),
        related_task_id=str(candidate.get("related_task_id") or ""),
        now=timestamp,
    )
    if not permission.get("allowed"):
        return {"ok": False, "error": str(permission.get("reason_code") or "permission_denied"), "message": "主动联系没有通过当前授权、灰度、在职状态或时间窗校验。", "permission": permission}
    suggested_send_at = _parse_time(candidate.get("suggested_send_at"))
    if suggested_send_at is not None and timestamp < suggested_send_at:
        return {"ok": False, "error": "before_suggested_send_time", "message": "还没有到模型结合对方工作方式选择的发送时间。", "suggested_send_at": suggested_send_at.isoformat(timespec="seconds")}
    outbox = store.read_json(OUTBOX_FILE, [])
    rows = outbox if isinstance(outbox, list) else []
    notification_id = f"relationship_touch:{candidate_id}"
    existing = next((item for item in rows if isinstance(item, dict) and str(item.get("id") or "") == notification_id), None)
    if existing:
        existing_status = str(existing.get("status") or "pending")
        mapped = {
            "pending": "queued",
            "retry_pending": "retry_pending",
            "sending": "sending",
            "sent": "sent",
            "failed": "failed",
            "result_unknown": "result_unknown",
            "suppressed": "superseded",
        }.get(existing_status, "result_unknown")
        update_relationship_touch_candidate_status(
            store, identity=identity, candidate_id=str(candidate_id), status=mapped,
            operation_id=f"{operation_id}:existing", delivery_receipt={"outbox_id": notification_id, "status": existing_status},
            source_text="主动联系幂等反查",
        )
        return {"ok": True, "candidate": _relationship_rows(store).get(str(candidate_id), {}), "outbox_item": deepcopy(existing), "delivery_state": mapped, "writeback_verified": True, "state_changed": False, "idempotent_replay": True}
    day = timestamp.date().isoformat()
    sent_today = sum(
        1 for item in rows if isinstance(item, dict)
        and str(item.get("notification_type") or "") == "relationship_touch"
        and str(item.get("touser") or "") == target_user_id
        and str(item.get("created_at") or "").startswith(day)
        and str(item.get("status") or "") in {"pending", "retry_pending", "sending", "sent", "result_unknown"}
    )
    if sent_today >= int(permission.get("daily_limit") or 1):
        return {"ok": False, "error": "daily_frequency_limit", "message": "这个对象今天的主动联系频率已到上限。", "permission": permission}
    authorized_update = update_relationship_touch_candidate_status(
        store, identity=identity, candidate_id=str(candidate_id), status="authorized",
        operation_id=f"{operation_id}:authorized", delivery_receipt={"authorization_ids": permission.get("authorization_ids") or [], "authorized_at": _now_iso(timestamp)},
        source_text="主动联系执行前授权核验",
    )
    if not authorized_update.get("writeback_verified"):
        return {"ok": False, "error": "authorization_writeback_failed", "message": "主动联系授权状态反查失败，未进入发送队列。", "writeback_verified": False}
    row = {
        "id": notification_id,
        "status": "pending",
        "delivery_mode": "direct_wecom",
        "notification_type": "relationship_touch",
        "task_id": f"relationship_touch:{candidate_id}",
        "role": target_role,
        "action": "relationship_touch",
        "target_user_id": target_user_id,
        "recipient_user_id": target_user_id,
        "to_user_id": target_user_id,
        "touser": target_user_id,
        "content": message[:700],
        "summary": str(candidate.get("reason") or "")[:240],
        "relationship_touch_candidate_id": str(candidate_id),
        "proactive_action_type": str(candidate.get("action_type") or "ask_work_fact"),
        "goal_id": str(candidate.get("goal_id") or ""),
        "goal_action_id": str(candidate.get("goal_action_id") or ""),
        "created_at": _now_iso(timestamp),
        "attempt_count": 0,
        "authorization": permission,
        "auto_effects": {"sends_parent_messages": False, "sends_teacher_messages": target_role == "teacher", "sends_manager_messages": target_role == "manager", "creates_teacher_tasks": False},
    }
    appended = {"value": False}

    def enqueue(current: Any) -> Any:
        current = current if isinstance(current, list) else []
        if any(isinstance(item, dict) and str(item.get("id") or "") == notification_id for item in current):
            return JSON_NO_CHANGE
        current.append(deepcopy(row))
        appended["value"] = True
        return current[-2000:]

    store.update_json(OUTBOX_FILE, [], enqueue)
    reread = store.read_json(OUTBOX_FILE, [])
    verified_row = next((item for item in reread if isinstance(item, dict) and str(item.get("id") or "") == notification_id), None) if isinstance(reread, list) else None
    if not verified_row:
        return {"ok": False, "error": "writeback_failed", "message": "主动消息入队后的反查没有通过。", "writeback_verified": False}
    update = update_relationship_touch_candidate_status(
        store, identity=identity, candidate_id=str(candidate_id), status="queued",
        operation_id=f"{operation_id}:queued", delivery_receipt={"outbox_id": notification_id, "queued_at": row["created_at"]},
        source_text="主动消息已进入企业微信发送队列",
    )
    if not update.get("writeback_verified"):
        def suppress_unverified(current: Any) -> Any:
            current = current if isinstance(current, list) else []
            for item in current:
                if isinstance(item, dict) and str(item.get("id") or "") == notification_id and str(item.get("status") or "") == "pending":
                    item.update({
                        "status": "suppressed",
                        "suppressed_at": _now_iso(timestamp),
                        "suppressed_reason": "relationship_touch_state_writeback_failed",
                    })
                    return current[-2000:]
            return JSON_NO_CHANGE

        store.update_json(OUTBOX_FILE, [], suppress_unverified)
        return {"ok": False, "error": "relationship_state_writeback_failed", "message": "主动消息状态反查失败，发送项已停止。", "writeback_verified": False}
    return {
        "ok": bool(update.get("writeback_verified")),
        "candidate": update.get("candidate") or {},
        "outbox_item": deepcopy(verified_row),
        "delivery_state": "queued",
        "writeback_verified": bool(update.get("writeback_verified")),
        "state_changed": bool(appended["value"]),
        "rendered_text": "主动消息已安排发送；当前有入队回执，尚不能声称对方已经收到。",
    }


def update_relationship_touch(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    candidate_id: str,
    status: str,
    operation_id: str,
    reply_text: str = "",
    evidence_complete: bool | None = None,
    failure_reason: str = "",
) -> dict[str, Any]:
    from .digital_employee_state import update_relationship_touch_candidate_status

    candidate = _relationship_rows(store).get(str(candidate_id or ""))
    if not candidate:
        return {"ok": False, "error": "relationship_touch_not_found", "message": "没有找到这条主动联系。"}
    ids = _identity_ids(identity)
    if identity.role != "boss" and identity.platform != "system" and str(candidate.get("target_user_id") or "") not in ids:
        return {"ok": False, "error": "permission_denied", "message": "当前账号不能更新其他人的主动联系线程。"}
    normalized = str(status or "").strip()
    if normalized not in {"replied_partial", "replied_sufficient", "resolved", "failed", "result_unknown", "retry_pending", "expired", "superseded", "escalated"}:
        return {"ok": False, "error": "invalid_relationship_touch_status", "message": "主动联系状态不合法。"}
    is_target_staff = identity.role != "boss" and identity.platform != "system"
    if is_target_staff and normalized not in {"replied_partial", "replied_sufficient"}:
        return {"ok": False, "error": "staff_can_only_submit_reply", "message": "员工只能提交本人真实回复，不能自行把管理线程标记为解决、失败或升级。"}
    if normalized in {"replied_partial", "replied_sufficient"} and not str(reply_text or "").strip():
        return {"ok": False, "error": "reply_evidence_required", "message": "回复状态必须包含对方本轮真实回复，不能只改状态。"}
    if normalized == "replied_sufficient" and evidence_complete is not True:
        normalized = "replied_partial"
    goal_action_id = str(candidate.get("goal_action_id") or "")
    goal_id = str(candidate.get("goal_id") or "")
    if normalized == "resolved" and goal_action_id:
        current_action = _goal_action_rows(store).get(goal_action_id, {})
        if str(current_action.get("status") or "") != "verified":
            return {"ok": False, "error": "goal_evidence_not_verified", "message": "关联目标证据尚未核验，不能直接把主动线程标记为解决。"}
    touch_update = update_relationship_touch_candidate_status(
        store,
        identity=identity,
        candidate_id=str(candidate_id),
        status=normalized,
        operation_id=operation_id,
        delivery_receipt={"reply_text": _limit(reply_text, 1000), "evidence_complete": evidence_complete, "replied_by": identity.canonical_user_id} if reply_text else None,
        failure_reason=failure_reason,
        source_text=reply_text or failure_reason,
        evidence_summary=reply_text,
        evidence_complete=evidence_complete,
    )
    action_update: dict[str, Any] = {}
    if touch_update.get("writeback_verified") and goal_action_id and goal_id:
        mapped = {
            "replied_partial": "replied_partial",
            "replied_sufficient": "replied_sufficient",
            "resolved": "resolved",
            "failed": "failed",
            "result_unknown": "failed",
            "retry_pending": "planned",
            "expired": "blocked",
            "superseded": "superseded",
            "escalated": "escalated",
        }.get(normalized)
        if mapped:
            action = _goal_action_rows(store).get(goal_action_id, {})
            action_update = submit_goal_action(
                store,
                identity=UserIdentity(
                    platform="system",
                    platform_user_id="relationship_touch_reply",
                    canonical_user_id="relationship_touch_reply",
                    person_name="小优主动工作回执",
                    role="boss",
                    approval_state="approved",
                ),
                goal_id=goal_id,
                action_type=str(action.get("action_type") or "ask_staff_fact"),
                summary=str(action.get("summary") or "主动事实请求"),
                operation_id=f"{operation_id}:goal_action",
                goal_action_id=goal_action_id,
                status=mapped,
                source_text=reply_text or failure_reason,
            )
    return {**touch_update, "goal_action_update": action_update}


def _goal_action_rows(store: TuoguanStore) -> dict[str, dict[str, Any]]:
    return _fold_events(store, GOAL_ACTIONS_FILE, "goal_action_id", "goal_action_update")


def _goal_is_active(store: TuoguanStore, goal_id: str) -> dict[str, Any] | None:
    from .goal_operator import find_active_goal

    return find_active_goal(store, goal_id=str(goal_id or ""))


def verify_goal_task_responsibility(
    store: TuoguanStore,
    *,
    target_user_id: str,
    student_names: list[str] | None,
) -> dict[str, Any]:
    """Verify that a goal task is assigned only to the trusted responsible teacher."""

    from .responsibility_resolver import resolve_student_responsibility

    target = str(target_user_id or "").strip()
    names = sorted({str(value).strip() for value in student_names or [] if str(value).strip()})
    if not target or not names:
        return {
            "ok": False,
            "error": "responsibility_evidence_required",
            "message": "目标子任务必须明确学生，并有可信主责老师关系；关系不清时应先创建事实核实行动。",
            "evidence": [],
        }
    evidence: list[dict[str, Any]] = []
    for name in names:
        resolved = resolve_student_responsibility(store, name, purpose="parent_communication")
        evidence.append({
            "student_name": name,
            "resolution_status": str(resolved.get("resolution_status") or ""),
            "responsible_user_id": str(resolved.get("responsible_user_id") or ""),
            "missing_fields": list(resolved.get("missing_fields") or []),
            "reason": str(resolved.get("reason") or ""),
        })
        if not resolved.get("ok") or not resolved.get("in_scope"):
            return {
                "ok": False,
                "error": "student_responsibility_unavailable",
                "message": f"{name}不在当前正式托管责任范围或学生档案不可用，不能自动派任务。",
                "evidence": evidence,
            }
        if str(resolved.get("resolution_status") or "") != "resolved":
            return {
                "ok": False,
                "error": "student_responsibility_unresolved",
                "message": f"{name}的服务类型或主责老师尚未确认，应先找店长或老板补事实。",
                "evidence": evidence,
            }
        if str(resolved.get("responsible_user_id") or "") != target:
            return {
                "ok": False,
                "error": "responsible_teacher_mismatch",
                "message": f"当前证据显示{name}不由目标老师主责，不能把任务派给错误对象。",
                "evidence": evidence,
            }
    return {"ok": True, "evidence": evidence, "writeback_verified": True}


def submit_goal_action(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    goal_id: str,
    action_type: str,
    summary: str,
    operation_id: str,
    target_role: str = "",
    target_user_id: str = "",
    target_name: str = "",
    student_names: list[str] | None = None,
    planned_at: str = "",
    due_at: str = "",
    evidence_requirement: str = "",
    escalation_path: list[str] | None = None,
    status: str = "planned",
    goal_action_id: str = "",
    source_text: str = "",
    retry_count: int | None = None,
    last_attempt_at: str = "",
) -> dict[str, Any]:
    if identity.role != "boss" and identity.platform != "system":
        return {"ok": False, "error": "permission_denied", "message": "只有老板或小优的受控自主循环可以保存目标行动。"}
    goal = _goal_is_active(store, goal_id)
    if not goal:
        return {"ok": False, "error": "active_goal_required", "message": "目标未确认、已撤销或不存在，不能保存行动。"}
    action = str(action_type or "").strip()
    if action not in GOAL_ACTION_TYPES:
        return {"ok": False, "error": "invalid_goal_action_type", "message": "目标行动类型不受支持。"}
    normalized_status = str(status or "planned").strip()
    if normalized_status not in GOAL_ACTION_STATUSES:
        return {"ok": False, "error": "invalid_goal_action_status", "message": "目标行动状态不合法。"}
    text = str(summary or "").strip()
    evidence = str(evidence_requirement or "").strip()
    combined = f"{text}{evidence}"
    risk_terms = GOAL_TASK_HIGH_RISK_TERMS if action == "create_low_risk_task" else HIGH_RISK_TERMS
    if any(term in combined for term in risk_terms):
        return {"ok": False, "error": "high_risk_goal_action_requires_confirmation", "message": "该行动涉及高风险制度或家长外发边界，只能形成待老板确认材料。"}
    action_id = str(goal_action_id or "").strip()
    existing = _goal_action_rows(store)
    if action_id:
        if action_id not in existing:
            return {"ok": False, "error": "goal_action_not_found", "message": "没有找到要更新的目标行动。"}
        current_status = str(existing[action_id].get("status") or "planned")
        if normalized_status in {"replied_partial", "replied_sufficient"} and not str(source_text or "").strip():
            return {"ok": False, "error": "reply_evidence_required", "message": "目标行动的回复状态必须带真实证据。"}
        if normalized_status == "verified" and (
            current_status != "replied_sufficient"
            or not str(existing[action_id].get("last_result") or "").strip()
        ):
            return {"ok": False, "error": "verification_evidence_missing", "message": "只有带真实回复证据的行动才能进入已核验状态。"}
        if normalized_status == "resolved" and current_status != "verified":
            return {"ok": False, "error": "verification_required_before_resolve", "message": "目标行动必须先完成证据核验，不能从计划或回复状态直接跳到已解决。"}
        row = {
            "record_type": "goal_action_update",
            "goal_action_event_id": _new_id("goal_action_event"),
            "goal_action_id": action_id,
            "goal_id": str(goal_id),
            "tenant_id": current_tenant_id(),
            "status": normalized_status,
            "changes": {
                key: value for key, value in {
                    "summary": _limit(text, 700) if text else "",
                    "planned_at": planned_at,
                    "due_at": due_at,
                    "evidence_requirement": _limit(evidence, 700) if evidence else "",
                    "last_result": _limit(source_text, 1000),
                    "retry_count": max(0, int(retry_count)) if retry_count is not None else None,
                    "last_attempt_at": str(last_attempt_at or ""),
                }.items() if value
            },
            "operation_id": operation_id,
            "created_at": _now_iso(),
        }
    else:
        if normalized_status not in {"planned", "due"}:
            return {"ok": False, "error": "invalid_initial_goal_action_status", "message": "新目标行动只能从计划或到期状态开始，不能伪造为已回复、已核验或已完成。"}
        fingerprint = hashlib.sha256(json.dumps({
            "tenant": current_tenant_id(), "goal": goal_id, "type": action,
            "target": target_user_id, "students": sorted(student_names or []), "summary": re.sub(r"\s+", "", text),
        }, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        for item in existing.values():
            if item.get("semantic_fingerprint") == fingerprint and str(item.get("status") or "") in OPEN_GOAL_ACTION_STATUSES:
                return {"ok": True, "goal_action": item, "writeback_verified": True, "state_changed": False, "idempotent_replay": True}
        action_id = _new_id("goal_action")
        row = {
            "record_type": "goal_action",
            "goal_action_id": action_id,
            "goal_id": str(goal_id),
            "tenant_id": current_tenant_id(),
            "status": normalized_status,
            "action_type": action,
            "summary": _limit(text, 700),
            "target_role": str(target_role or ""),
            "target_user_id": str(target_user_id or ""),
            "target_name": _limit(target_name, 80),
            "student_names": sorted({str(item).strip() for item in student_names or [] if str(item).strip()}),
            "planned_at": str(planned_at or _now_iso()),
            "due_at": str(due_at or ""),
            "evidence_requirement": _limit(evidence or "拿到事实归属人的明确回复和可核验结果。", 700),
            "retry_count": 0,
            "max_retries": 2,
            "escalation_path": list(escalation_path or ["manager", "boss_if_blocked_or_high_risk"]),
            "semantic_fingerprint": fingerprint,
            "source_text": _limit(source_text, 1000),
            "operation_id": operation_id,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "boundary": {"contacts_parent": False, "low_risk_only": True, "model_decides_execution": True},
        }
    _append(store, GOAL_ACTIONS_FILE, row)
    folded = _goal_action_rows(store).get(action_id, {})
    verified = str(folded.get("status") or "") == normalized_status
    return {"ok": verified, "goal_action": folded, "writeback_verified": verified, "state_changed": True, "rendered_text": "目标行动已保存并完成反查。" if verified else "目标行动反查失败。"}


def query_goal_actions(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    goal_id: str = "",
    include_closed: bool = False,
    due_only: bool = False,
    now_at: str = "",
    limit: int = 30,
) -> dict[str, Any]:
    now = _parse_time(now_at) or _now()
    ids = _identity_ids(identity)
    rows: list[dict[str, Any]] = []
    for item in _goal_action_rows(store).values():
        if goal_id and str(item.get("goal_id") or "") != str(goal_id):
            continue
        if identity.role not in {"boss", "manager"} and identity.platform != "system" and str(item.get("target_user_id") or "") not in ids:
            continue
        if not include_closed and str(item.get("status") or "") not in OPEN_GOAL_ACTION_STATUSES:
            continue
        planned = _parse_time(item.get("planned_at") or item.get("due_at"))
        due = planned is None or planned <= now
        if due_only and not due:
            continue
        copied = deepcopy(item)
        copied["is_due"] = due
        rows.append(copied)
    rows.sort(key=lambda item: str(item.get("planned_at") or item.get("due_at") or item.get("created_at") or ""))
    rows = rows[: max(1, min(int(limit or 30), 100))]
    return {
        "ok": True,
        "goal_action_count": len(rows),
        "due_count": sum(bool(item.get("is_due")) for item in rows),
        "goal_actions": rows,
        "rendered_text": f"查到 {len(rows)} 条目标行动，其中 {sum(bool(item.get('is_due')) for item in rows)} 条已经到期。",
        "render_verified": True,
    }


def _collect_internal_goal_evidence(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    action: dict[str, Any],
    timestamp: datetime,
) -> dict[str, Any]:
    """Collect a compact, read-only evidence receipt for an internal goal action."""

    try:
        from .digital_employee_state import query_active_goal_work_state, query_employee_work_map

        goal_id = str(action.get("goal_id") or "")
        goal_state = query_active_goal_work_state(store, identity=identity, goal_id=goal_id, now=timestamp)
        work_map = query_employee_work_map(store, identity=identity, limit=8)
        goal_rows = goal_state.get("goals") if isinstance(goal_state.get("goals"), list) else []
        goal = goal_rows[0] if goal_rows and isinstance(goal_rows[0], dict) else {}
        current_work = goal.get("current_work_state") if isinstance(goal.get("current_work_state"), dict) else {}
        goal_evidence_count = sum(
            1 for item in _read_jsonl(store, "goal_evidence.jsonl")
            if str(item.get("goal_id") or "") == goal_id
        )

        task_data = store.read_json("tasks.json", [])
        task_rows = task_data.get("tasks") or task_data.get("items") or [] if isinstance(task_data, dict) else task_data
        if not isinstance(task_rows, list):
            task_rows = []
        related_tasks = [
            item for item in task_rows
            if isinstance(item, dict) and str(item.get("goal_id") or "") == goal_id
        ]
        task_status_counts: dict[str, int] = {}
        for item in related_tasks:
            status = str(item.get("status") or "unknown")
            task_status_counts[status] = task_status_counts.get(status, 0) + 1

        related_touches = [
            item for item in _relationship_rows(store).values()
            if str(item.get("goal_id") or "") == goal_id
            or str(item.get("goal_action_id") or "") == str(action.get("goal_action_id") or "")
        ]
        touch_status_counts: dict[str, int] = {}
        for item in related_touches:
            status = str(item.get("status") or "unknown")
            touch_status_counts[status] = touch_status_counts.get(status, 0) + 1

        priority_gaps = work_map.get("priority_gaps") if isinstance(work_map.get("priority_gaps"), list) else []
        unknowns = [
            _limit(item.get("gap_text") or item.get("text") or item.get("reason"), 140)
            for item in priority_gaps
            if isinstance(item, dict) and str(item.get("gap_text") or item.get("text") or item.get("reason") or "").strip()
        ][:3]
        fact_owners = [
            str(item.get("ask_role") or item.get("fact_owner_role") or "")
            for item in priority_gaps
            if isinstance(item, dict) and str(item.get("ask_role") or item.get("fact_owner_role") or "").strip()
        ][:3]
        known = [
            f"目标状态={str(goal.get('status') or 'unknown')}",
            f"当前阶段={str((current_work.get('current_phase') or {}).get('phase_key') or current_work.get('status') or '未记录') if isinstance(current_work.get('current_phase'), dict) else str(current_work.get('status') or '未记录')}",
            f"目标证据={goal_evidence_count}条",
            f"关联任务={len(related_tasks)}条{task_status_counts}",
            f"关联主动线程={len(related_touches)}条{touch_status_counts}",
        ]
        term_state = goal_state.get("term_state") if isinstance(goal_state.get("term_state"), dict) else {}
        if term_state:
            known.append(
                "数据口径="
                + str(term_state.get("roster_confidence") or term_state.get("data_term") or "需要重新确认")
            )
        receipt = {
            "collected_at": timestamp.isoformat(timespec="seconds"),
            "goal_id": goal_id,
            "known": known,
            "unknown": unknowns or ["机构地图当前没有列出高优先级缺口，仍需主模型核对目标证据要求。"],
            "fact_owner_roles": sorted(set(fact_owners)),
            "sources": [
                "goal_operator_goals.json",
                "goal_evidence.jsonl",
                "tasks.json",
                RELATIONSHIP_TOUCH_FILE,
                "institution_work_map",
            ],
            "read_only": True,
            "messages_sent": False,
            "tasks_created": False,
        }
        evidence_text = (
            f"内部事实反查（{receipt['collected_at']}）："
            f"已知：{'；'.join(receipt['known'])}。"
            f"未知：{'；'.join(receipt['unknown'])}。"
            f"事实归属角色：{'、'.join(receipt['fact_owner_roles']) or '待主模型判断'}。"
            f"来源：{'、'.join(receipt['sources'])}。本次只读，未外发、未派任务。"
        )
        return {"ok": True, "receipt": receipt, "evidence_text": _limit(evidence_text, 1000)}
    except Exception as exc:
        return {
            "ok": False,
            "error": "internal_goal_evidence_query_failed",
            "message": f"内部事实反查失败：{type(exc).__name__}:{str(exc)[:180]}",
        }


def execute_goal_action_decision(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    goal_action_id: str,
    decision: str,
    operation_id: str,
    message: str = "",
    decision_reason: str = "",
    next_attention_at: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    action = _goal_action_rows(store).get(str(goal_action_id or ""))
    if not action:
        return {"ok": False, "error": "goal_action_not_found", "message": "没有找到目标行动。"}
    if identity.role != "boss" and identity.platform != "system":
        return {"ok": False, "error": "permission_denied", "message": "当前账号无权执行目标行动。"}
    if not _goal_is_active(store, str(action.get("goal_id") or "")):
        return {"ok": False, "error": "active_goal_required", "message": "目标已经撤销或关闭，行动未执行。"}
    if str(action.get("status") or "") not in OPEN_GOAL_ACTION_STATUSES:
        return {"ok": False, "error": "goal_action_closed", "message": "目标行动已经关闭。"}
    choice = str(decision or "").strip()
    if choice not in {"execute", "wait", "adjust", "stop", "escalate"}:
        return {"ok": False, "error": "invalid_goal_action_decision", "message": "模型必须明确选择执行、等待、调整、停止或升级。"}
    timestamp = _now(now)
    if choice in {"wait", "adjust"}:
        return submit_goal_action(
            store, identity=identity, goal_id=str(action.get("goal_id") or ""),
            action_type=str(action.get("action_type") or "ask_staff_fact"),
            summary=str(action.get("summary") or "继续等待"), operation_id=operation_id,
            goal_action_id=str(goal_action_id), status="planned",
            planned_at=str(next_attention_at or (timestamp + timedelta(hours=4)).isoformat(timespec="seconds")),
            source_text=decision_reason or "模型结合当前事实选择等待或调整时间。",
        )
    if choice == "stop":
        return submit_goal_action(
            store, identity=identity, goal_id=str(action.get("goal_id") or ""),
            action_type=str(action.get("action_type") or "ask_staff_fact"),
            summary=str(action.get("summary") or "停止行动"), operation_id=operation_id,
            goal_action_id=str(goal_action_id), status="cancelled", source_text=decision_reason,
        )
    if choice == "escalate":
        return _escalate_goal_action(store, identity=identity, action=action, operation_id=operation_id, reason=decision_reason)
    if str(action.get("status") or "") == "replied_sufficient":
        evidence = str(action.get("last_result") or "").strip()
        if not evidence:
            return {
                "ok": False,
                "error": "verification_evidence_missing",
                "message": "行动被标记为回复充分，但没有可反查证据，不能确认完成。",
            }
        verified = submit_goal_action(
            store,
            identity=identity,
            goal_id=str(action.get("goal_id") or ""),
            action_type=str(action.get("action_type") or "verify_evidence"),
            summary=str(action.get("summary") or "核验目标证据"),
            operation_id=f"{operation_id}:verified",
            goal_action_id=str(goal_action_id),
            status="verified",
            source_text=f"模型核验理由：{decision_reason or '回复证据与要求一致'}；证据：{evidence}",
        )
        if not verified.get("writeback_verified"):
            return verified
        progress_updates: list[dict[str, Any]] = []
        student_names = [str(value) for value in action.get("student_names") or [] if str(value)]
        if student_names:
            from .goal_operator import update_goal_progress

            target_role = str(action.get("target_role") or "teacher")
            target_user_id = str(action.get("target_user_id") or identity.canonical_user_id)
            evidence_identity = UserIdentity(
                "system",
                target_user_id,
                target_user_id,
                str(action.get("target_name") or target_user_id),
                target_role if target_role in {"boss", "manager", "teacher"} else "teacher",
                "approved",
            )
            for index, student_name in enumerate(student_names):
                progress_updates.append(update_goal_progress(
                    store,
                    identity=evidence_identity,
                    goal_id=str(action.get("goal_id") or ""),
                    student_name=student_name,
                    update_text=evidence,
                    operation_id=f"{operation_id}:progress:{index}",
                ))
            if not all(item.get("writeback_verified") for item in progress_updates):
                return {
                    "ok": False,
                    "error": "goal_progress_writeback_failed",
                    "message": "行动证据已经核验，但目标进度写后反查失败，当前行动暂不关闭。",
                    "verification": verified.get("goal_action") or {},
                    "progress_updates": progress_updates,
                    "writeback_verified": False,
                }
        resolved = submit_goal_action(
            store,
            identity=identity,
            goal_id=str(action.get("goal_id") or ""),
            action_type=str(action.get("action_type") or "verify_evidence"),
            summary=str(action.get("summary") or "核验目标证据"),
            operation_id=f"{operation_id}:resolved",
            goal_action_id=str(goal_action_id),
            status="resolved",
            source_text="证据已由主模型核验，当前行动闭环；目标整体是否完成仍需按全部行动和经营结果判断。",
        )
        relationship_updates: list[dict[str, Any]] = []
        if resolved.get("writeback_verified"):
            from .digital_employee_state import update_relationship_touch_candidate_status

            for touch in _relationship_rows(store).values():
                if str(touch.get("goal_action_id") or "") != str(goal_action_id):
                    continue
                if str(touch.get("status") or "") not in {"replied_partial", "replied_sufficient"}:
                    continue
                relationship_updates.append(update_relationship_touch_candidate_status(
                    store,
                    identity=identity,
                    candidate_id=str(touch.get("candidate_id") or ""),
                    status="resolved",
                    operation_id=f"{operation_id}:relationship:{touch.get('candidate_id')}",
                    delivery_receipt={"goal_action_id": str(goal_action_id), "evidence_verified": True},
                    source_text="关联目标行动证据已由主模型核验并完成。",
                ))
        relationship_verified = all(item.get("writeback_verified") for item in relationship_updates)
        return {
            **resolved,
            "ok": bool(resolved.get("ok") and relationship_verified),
            "writeback_verified": bool(resolved.get("writeback_verified") and relationship_verified),
            "verification": verified.get("goal_action") or {},
            "progress_updates": progress_updates,
            "relationship_updates": relationship_updates,
            "rendered_text": "这项行动的回复证据已经核验并闭环；这不等于整个经营目标自动完成。",
        }
    action_type = str(action.get("action_type") or "")
    if action_type in {"ask_staff_fact", "follow_up"}:
        retry_count = int(action.get("retry_count") or 0)
        if str(action.get("status") or "") == "waiting_reply" and retry_count >= 2:
            return _escalate_goal_action(
                store,
                identity=identity,
                action=action,
                operation_id=operation_id,
                reason=decision_reason or "同一事实请求已经低频追问两次仍未获得足够回复。",
            )
        return _execute_goal_staff_question(
            store, identity=identity, action=action, operation_id=operation_id,
            message=message, decision_reason=decision_reason, timestamp=timestamp,
        )
    if action_type == "create_low_risk_task":
        return _execute_low_risk_goal_task(
            store, identity=identity, action=action, operation_id=operation_id,
            decision_reason=decision_reason, timestamp=timestamp,
        )
    if action_type in {"query_internal_data", "prepare_material", "review_and_report", "verify_evidence"}:
        evidence = _collect_internal_goal_evidence(
            store,
            identity=identity,
            action=action,
            timestamp=timestamp,
        )
        if not evidence.get("ok"):
            return evidence
        updated = submit_goal_action(
            store,
            identity=identity,
            goal_id=str(action.get("goal_id") or ""),
            action_type=action_type,
            summary=str(action.get("summary") or "核对目标内部事实"),
            operation_id=operation_id,
            goal_action_id=str(goal_action_id),
            status="replied_sufficient",
            source_text=str(evidence.get("evidence_text") or ""),
            last_attempt_at=timestamp.isoformat(timespec="seconds"),
        )
        return {
            **updated,
            "evidence_snapshot": evidence.get("receipt") or {},
            "rendered_text": (
                "内部事实已经真实读取并完成反查，当前行动等待主模型核验证据；"
                "这不等于行动或经营目标已经完成。"
            ),
        }
    return {
        "ok": False,
        "error": "goal_action_requires_specific_evidence_tool",
        "message": "这项行动需要对应的真实业务工具或人工证据，不能只靠选择 execute 改成执行中。",
        "goal_action": action,
        "writeback_verified": False,
    }


def _execute_goal_staff_question(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    action: dict[str, Any],
    operation_id: str,
    message: str,
    decision_reason: str,
    timestamp: datetime,
) -> dict[str, Any]:
    from .digital_employee_state import submit_relationship_touch_candidate

    role = str(action.get("target_role") or "")
    user_id = str(action.get("target_user_id") or "")
    question = str(message or action.get("summary") or "").strip()
    if not question or not role or not user_id:
        return {"ok": False, "error": "goal_action_target_or_message_missing", "message": "目标行动缺少明确对象或具体问题。"}
    permission = effective_proactive_permission(
        store, target_role=role, target_user_id=user_id,
        action_type="ask_work_fact", goal_id=str(action.get("goal_id") or ""), now=timestamp,
    )
    candidate = submit_relationship_touch_candidate(
        store,
        identity=identity,
        target_role=role,
        target_user_id=user_id,
        target_name=str(action.get("target_name") or ""),
        touch_type="owner_business" if role == "boss" else ("manager_assist" if role == "manager" else "record_relief"),
        message=question,
        reason=decision_reason or str(action.get("summary") or "目标推进需要补充事实。"),
        value=f"推进目标 {action.get('goal_id')}",
        work_related=True,
        private_emotional_support=False,
        requires_authorization=not bool(permission.get("allowed")),
        external_send_allowed=bool(permission.get("allowed")),
        suggested_send_at=_now_iso(timestamp),
        status="candidate",
        operation_id=f"{operation_id}:candidate",
        source_text=decision_reason,
        action_type="ask_work_fact",
        goal_id=str(action.get("goal_id") or ""),
        goal_action_id=str(action.get("goal_action_id") or ""),
        evidence_requirement=str(action.get("evidence_requirement") or ""),
    )
    if not candidate.get("ok"):
        return candidate
    execution = execute_relationship_touch(
        store,
        identity=identity,
        candidate_id=str((candidate.get("candidate") or {}).get("candidate_id") or ""),
        operation_id=f"{operation_id}:execute",
        now=timestamp,
    )
    if not execution.get("ok"):
        return {"ok": False, "error": execution.get("error"), "message": execution.get("message"), "candidate": candidate.get("candidate"), "execution": execution}
    if execution.get("idempotent_replay"):
        return {
            "ok": False,
            "error": "duplicate_followup_not_sent",
            "message": "这和同一目标行动今天已经发送的问法相同，本轮没有重复发送；请由模型换时间或只追问缺少的一个事实。",
            "candidate": candidate.get("candidate") or {},
            "execution": execution,
        }
    action_update = submit_goal_action(
        store, identity=identity, goal_id=str(action.get("goal_id") or ""),
        action_type=str(action.get("action_type") or "ask_staff_fact"),
        summary=str(action.get("summary") or question), operation_id=f"{operation_id}:waiting",
        goal_action_id=str(action.get("goal_action_id") or ""), status="waiting_reply",
        retry_count=(int(action.get("retry_count") or 0) + 1) if str(action.get("status") or "") == "waiting_reply" else int(action.get("retry_count") or 0),
        last_attempt_at=_now_iso(timestamp),
        source_text=f"主动联系已入队：{(candidate.get('candidate') or {}).get('candidate_id')}",
    )
    return {
        "ok": bool(action_update.get("writeback_verified")),
        "candidate": candidate.get("candidate") or {},
        "execution": execution,
        "goal_action": action_update.get("goal_action") or {},
        "delivery_state": execution.get("delivery_state"),
        "writeback_verified": bool(action_update.get("writeback_verified")),
        "rendered_text": "目标事实问题已安排发送，目标行动进入等待回复；尚不能声称对方已经回复。",
    }


def _escalate_goal_action(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    action: dict[str, Any],
    operation_id: str,
    reason: str,
) -> dict[str, Any]:
    current = submit_goal_action(
        store, identity=identity, goal_id=str(action.get("goal_id") or ""),
        action_type=str(action.get("action_type") or "ask_staff_fact"),
        summary=str(action.get("summary") or "行动升级"), operation_id=f"{operation_id}:current",
        goal_action_id=str(action.get("goal_action_id") or ""), status="escalated",
        source_text=reason or "两次低频追问后仍缺少事实，转向店长核实。",
    )
    manager_id, manager_name = _first_active_user_for_role(store, "manager")
    if not manager_id:
        return {
            "ok": bool(current.get("writeback_verified")),
            "goal_action": current.get("goal_action") or {},
            "escalation": "manager_unavailable",
            "writeback_verified": bool(current.get("writeback_verified")),
            "rendered_text": "原事实请求已标记升级，但当前没有可信在职店长对象；只有目标受阻或高风险时才应再找老板。",
        }
    manager_action = submit_goal_action(
        store,
        identity=identity,
        goal_id=str(action.get("goal_id") or ""),
        action_type="ask_staff_fact",
        summary=f"请店长协助核实：{str(action.get('summary') or '')}",
        operation_id=f"{operation_id}:manager",
        target_role="manager",
        target_user_id=manager_id,
        target_name=manager_name,
        student_names=list(action.get("student_names") or []),
        evidence_requirement=str(action.get("evidence_requirement") or "店长提供可核验事实。"),
        escalation_path=["boss_if_blocked_or_high_risk"],
        status="planned",
        source_text=reason or "两次低频追问后转向店长核实。",
    )
    return {
        "ok": bool(current.get("writeback_verified") and manager_action.get("writeback_verified")),
        "goal_action": current.get("goal_action") or {},
        "manager_goal_action": manager_action.get("goal_action") or {},
        "escalation": "manager_planned",
        "writeback_verified": bool(current.get("writeback_verified") and manager_action.get("writeback_verified")),
        "rendered_text": "同一对象两次低频追问仍无足够回复，已转成店长核实行动；尚未声称店长已收到。",
    }


def _execute_low_risk_goal_task(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    action: dict[str, Any],
    operation_id: str,
    decision_reason: str,
    timestamp: datetime,
) -> dict[str, Any]:
    from .youyi_batch_capabilities import create_assigned_task

    target_id = str(action.get("target_user_id") or "")
    permission = effective_proactive_permission(
        store, target_role="teacher", target_user_id=target_id,
        action_type="assign_low_risk_goal_task", goal_id=str(action.get("goal_id") or ""), now=timestamp,
    )
    if not permission.get("allowed"):
        return {"ok": False, "error": permission.get("reason_code") or "permission_denied", "message": "低风险目标子任务没有通过授权、灰度或在职状态校验。"}
    summary = str(action.get("summary") or "").strip()
    if any(term in f"{summary}{action.get('evidence_requirement') or ''}" for term in GOAL_TASK_HIGH_RISK_TERMS):
        return {"ok": False, "error": "high_risk_goal_task_requires_confirmation", "message": "高风险内容不能由小优自主派发。"}
    tasks = store.load_tasks()
    today = timestamp.date().isoformat()
    open_today = [
        task for task in tasks if isinstance(task, dict)
        and str(task.get("goal_id") or "") == str(action.get("goal_id") or "")
        and str(task.get("created_at") or "").startswith(today)
        and str(task.get("status") or "") not in {"completed", "cancelled", "closed", "done"}
    ]
    if sum(str(task.get("assignee_userid") or "") == target_id for task in open_today) >= 1:
        return {"ok": False, "error": "goal_task_person_daily_limit", "message": "该老师今天已有一个目标子任务。"}
    if len(open_today) >= 3:
        return {"ok": False, "error": "goal_task_institution_daily_limit", "message": "今天全机构目标子任务已到三个。"}
    students = [str(value) for value in action.get("student_names") or [] if str(value)]
    responsibility = verify_goal_task_responsibility(
        store,
        target_user_id=target_id,
        student_names=students,
    )
    if not responsibility.get("ok"):
        return responsibility
    student_name = students[0] if len(students) == 1 else ""
    created = create_assigned_task(
        store,
        title=summary,
        assignee_user_id=target_id,
        created_by=str((_goal_is_active(store, str(action.get("goal_id") or "")) or {}).get("owner_user_id") or identity.canonical_user_id),
        due_at=str(action.get("due_at") or (timestamp + timedelta(days=1)).isoformat(timespec="seconds")),
        level="A",
        student_name=student_name,
        channel="autonomous_goal_action",
        source_text=str(action.get("source_text") or summary),
        evidence_requirement=str(action.get("evidence_requirement") or ""),
        created_by_role="boss",
        created_by_name="小优（老板确认目标内执行）",
        business_goal=str((_goal_is_active(store, str(action.get("goal_id") or "")) or {}).get("goal_text") or summary),
        known_facts=[
            str(value) for value in action.get("known_facts") or [] if str(value)
        ],
    )
    if not created.get("ok"):
        return created
    task = created.get("task") if isinstance(created.get("task"), dict) else {}
    task_id = str(created.get("task_id") or task.get("id") or "")
    persisted = store.update_task(task_id, lambda current: {
        **current,
        "goal_id": str(action.get("goal_id") or ""),
        "goal_action_id": str(action.get("goal_action_id") or ""),
        "evidence_requirement": str(action.get("evidence_requirement") or ""),
        "student_names": students,
        "responsibility_evidence": responsibility.get("evidence") or [],
        "created_autonomously_within_goal": True,
        "model_decision_reason": _limit(decision_reason, 700),
    })
    if not isinstance(persisted, dict) or str(persisted.get("goal_action_id") or "") != str(action.get("goal_action_id") or ""):
        return {"ok": False, "error": "writeback_failed", "message": "目标子任务写后反查失败。", "writeback_verified": False}
    started_at = _now_iso(timestamp)
    expires_at = _now_iso(timestamp + timedelta(hours=36))

    def remember_active(value: Any) -> dict[str, Any]:
        contexts = value if isinstance(value, dict) else {}
        contexts[target_id] = {
            "user_id": target_id,
            "task_id": task_id,
            "task_title": str(persisted.get("title") or summary),
            "student_id": str(persisted.get("student_id") or student_name),
            "student_name": str(persisted.get("student_name") or student_name),
            "task_type": str(persisted.get("type") or "manual_assignment"),
            "status": "selected",
            "started_at": started_at,
            "expires_at": expires_at,
            "candidate_task_ids": [],
            "source": "autonomous_goal_task_created",
            "owner_user_id": str(persisted.get("created_by") or ""),
            "expected_report_at": str(persisted.get("due_at") or ""),
            "original_owner_text": summary,
            "goal_id": str(action.get("goal_id") or ""),
            "goal_action_id": str(action.get("goal_action_id") or ""),
        }
        return contexts

    def remember_pending(value: Any) -> dict[str, Any]:
        contexts = value if isinstance(value, dict) else {}
        contexts[target_id] = {
            "user_id": target_id,
            "task_id": task_id,
            "task_title": str(persisted.get("title") or summary),
            "task_level": str(persisted.get("level") or "A"),
            "task_type": str(persisted.get("type") or "manual_assignment"),
            "student_id": str(persisted.get("student_id") or student_name),
            "student_name": str(persisted.get("student_name") or student_name),
            "source": "autonomous_goal_task_notification",
            "trigger_words": ["继续", "开始", "处理", "完成", "进展"],
            "created_at": started_at,
            "expires_at": expires_at,
            "owner_user_id": str(persisted.get("created_by") or ""),
            "expected_report_at": str(persisted.get("due_at") or ""),
            "original_owner_text": summary,
            "goal_id": str(action.get("goal_id") or ""),
            "goal_action_id": str(action.get("goal_action_id") or ""),
        }
        return contexts

    def remember_focus(value: Any) -> dict[str, Any]:
        focuses = value if isinstance(value, dict) else {}
        key = f"wecom_callback:{target_id}"
        focus = focuses.get(key) if isinstance(focuses.get(key), dict) else {}
        focus.update({
            "task_id": task_id,
            "student_name": str(persisted.get("student_name") or student_name),
            "task_type": str(persisted.get("type") or "manual_assignment"),
            "focus_source": "task_created",
            "focus_expires_at": expires_at,
            "goal_id": str(action.get("goal_id") or ""),
            "goal_action_id": str(action.get("goal_action_id") or ""),
            "updated_at": started_at,
        })
        focuses[key] = focus
        return focuses

    store.update_json("active_task_context.json", {}, remember_active)
    store.update_json("pending_next_task_context.json", {}, remember_pending)
    store.update_json("model_focus.json", {}, remember_focus)
    active_context = store.read_json("active_task_context.json", {})
    pending_context = store.read_json("pending_next_task_context.json", {})
    focus_context = store.read_json("model_focus.json", {})
    context_verified = bool(
        isinstance(active_context, dict)
        and str((active_context.get(target_id) or {}).get("task_id") or "") == task_id
        and isinstance(pending_context, dict)
        and str((pending_context.get(target_id) or {}).get("task_id") or "") == task_id
        and isinstance(focus_context, dict)
        and str((focus_context.get(f"wecom_callback:{target_id}") or {}).get("task_id") or "") == task_id
    )
    if not context_verified:
        return {
            "ok": False,
            "error": "task_context_writeback_failed",
            "message": "目标子任务已创建，但老师任务上下文反查失败，未安排外发。",
            "task": persisted,
            "writeback_verified": False,
        }
    notification_id = f"{task_id}:teacher:task_created"
    content = (
        f"你收到一项新任务：{persisted.get('title') or summary}\n"
        f"截止：{persisted.get('due_at') or '请在合理时间内处理'}\n"
        f"闭环证据：{persisted.get('evidence_requirement') or '请回复实际结果和下一步安排'}\n"
        "请直接回复真实进展；信息不完整时小优只会追问缺少的一项。"
    )
    queued = {"value": False}

    def enqueue(rows: Any) -> Any:
        rows = rows if isinstance(rows, list) else []
        if any(isinstance(item, dict) and str(item.get("id") or "") == notification_id for item in rows):
            return JSON_NO_CHANGE
        rows.append({
            "id": notification_id, "status": "pending", "delivery_mode": "direct_wecom",
            "notification_type": "task_created", "task_id": task_id, "role": "teacher",
            "action": "task_created", "touser": target_id, "target_user_id": target_id,
            "content": content[:1000], "goal_id": str(action.get("goal_id") or ""),
            "goal_action_id": str(action.get("goal_action_id") or ""), "created_at": _now_iso(timestamp), "attempt_count": 0,
        })
        queued["value"] = True
        return rows[-2000:]

    store.update_json(OUTBOX_FILE, [], enqueue)
    action_update = submit_goal_action(
        store, identity=identity, goal_id=str(action.get("goal_id") or ""),
        action_type="create_low_risk_task", summary=summary,
        operation_id=f"{operation_id}:waiting", goal_action_id=str(action.get("goal_action_id") or ""),
        status="waiting_reply", source_text=f"task_id={task_id}; notification_id={notification_id}",
    )
    return {
        "ok": bool(action_update.get("writeback_verified")), "task": persisted,
        "outbox_id": notification_id, "delivery_state": "queued", "goal_action": action_update.get("goal_action") or {},
        "writeback_verified": bool(action_update.get("writeback_verified") and context_verified), "state_changed": bool(queued["value"]),
        "task_context_verified": context_verified,
        "rendered_text": "目标内低风险子任务已创建并安排通知；只有发送回执后才能说老师已经收到。",
    }


def seed_goal_actions_for_confirmed_goal(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    goal: dict[str, Any],
    review: dict[str, Any],
    operation_id: str,
) -> list[dict[str, Any]]:
    goal_id = str(goal.get("goal_id") or "")
    results: list[dict[str, Any]] = []
    responsibility = review.get("responsibility_coverage") if isinstance(review.get("responsibility_coverage"), dict) else {}
    unresolved = int(responsibility.get("unresolved_count") or 0)
    if unresolved:
        target_id, target_name = _first_active_user_for_role(store, "manager")
        target_role = "manager" if target_id else "boss"
        if not target_id:
            target_id, target_name = _first_active_user_for_role(store, "boss")
        results.append(submit_goal_action(
            store, identity=identity, goal_id=goal_id, action_type="ask_staff_fact",
            summary="确认当前学生服务关系和主责老师缺口，避免把目标派错人。",
            operation_id=f"{operation_id}:seed:responsibility", target_role=target_role,
            target_user_id=target_id, target_name=target_name,
            evidence_requirement="店长或老板明确确认午托、晚托、全托和主责老师关系。",
            escalation_path=["manager", "boss_if_blocked_or_high_risk"], source_text=str(goal.get("confirmation_text") or ""),
        ))
        return results
    followups = [item for item in goal.get("teacher_followups") or [] if isinstance(item, dict)]
    for index, item in enumerate(followups[:3]):
        user_id = str(item.get("teacher_user_id") or "")
        students = [str(value) for value in item.get("student_names") or [] if str(value)]
        if not user_id or not students:
            continue
        results.append(submit_goal_action(
            store, identity=identity, goal_id=goal_id, action_type="create_low_risk_task",
            summary=f"请责任老师完成第一批家校沟通并反馈真实结果：{'、'.join(students[:5])}。",
            operation_id=f"{operation_id}:seed:teacher:{index}", target_role="teacher",
            target_user_id=user_id, target_name=str(item.get("teacher_name") or ""), student_names=students[:5],
            evidence_requirement="老师回复家长态度、孩子现状和下一步安排；只有完整事实才算闭环。",
            escalation_path=["retry_same_teacher_twice", "manager", "boss_if_blocked_or_high_risk"], source_text=str(goal.get("confirmation_text") or ""),
        ))
    if not results:
        results.append(submit_goal_action(
            store,
            identity=identity,
            goal_id=goal_id,
            action_type="query_internal_data",
            summary=f"先核对目标当前事实基线并找出第一项可验证缺口：{str(goal.get('goal_text') or '未命名目标')}。",
            operation_id=f"{operation_id}:seed:baseline",
            planned_at=_now_iso(),
            evidence_requirement="给出当前已知事实、缺口、事实归属人和第一项可执行行动，不能只保存静态方案。",
            escalation_path=["manager_for_operating_fact", "boss_if_blocked_or_high_risk"],
            source_text=str(goal.get("confirmation_text") or ""),
        ))
    return results


def stop_goal_execution(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    goal_id: str,
    operation_id: str,
    reason: str,
) -> dict[str, Any]:
    """Stop unsent goal work without deleting audit history."""

    stopped_actions = 0
    stopped_touches = 0
    stopped_outbox = 0
    for action in list(_goal_action_rows(store).values()):
        if str(action.get("goal_id") or "") != str(goal_id) or str(action.get("status") or "") not in OPEN_GOAL_ACTION_STATUSES:
            continue
        result = submit_goal_action(
            store, identity=identity, goal_id=goal_id,
            action_type=str(action.get("action_type") or "ask_staff_fact"),
            summary=str(action.get("summary") or "目标已停止"),
            operation_id=f"{operation_id}:action:{action.get('goal_action_id')}",
            goal_action_id=str(action.get("goal_action_id") or ""), status="cancelled",
            source_text=reason,
        )
        stopped_actions += int(bool(result.get("writeback_verified")))
    from .digital_employee_state import update_relationship_touch_candidate_status

    for touch in list(_relationship_rows(store).values()):
        if str(touch.get("goal_id") or "") != str(goal_id) or str(touch.get("status") or "") in TERMINAL_RELATIONSHIP_STATUSES:
            continue
        result = update_relationship_touch_candidate_status(
            store, identity=identity, candidate_id=str(touch.get("candidate_id") or ""),
            status="superseded", operation_id=f"{operation_id}:touch:{touch.get('candidate_id')}",
            failure_reason=reason, source_text=reason,
        )
        stopped_touches += int(bool(result.get("writeback_verified")))

    def suppress(rows: Any) -> Any:
        nonlocal stopped_outbox
        rows = rows if isinstance(rows, list) else []
        changed = False
        for row in rows:
            if not isinstance(row, dict) or str(row.get("goal_id") or "") != str(goal_id):
                continue
            if str(row.get("status") or "") not in {"pending", "retry_pending", "sending"}:
                continue
            row["status"] = "suppressed"
            row["suppressed_at"] = _now_iso()
            row["suppressed_reason"] = reason
            stopped_outbox += 1
            changed = True
        return rows if changed else JSON_NO_CHANGE

    store.update_json(OUTBOX_FILE, [], suppress)
    return {
        "ok": True,
        "stopped_goal_action_count": stopped_actions,
        "stopped_relationship_touch_count": stopped_touches,
        "suppressed_outbox_count": stopped_outbox,
        "writeback_verified": True,
    }


def _first_active_user_for_role(store: TuoguanStore, role: str) -> tuple[str, str]:
    whitelist = store.read_json("wecom_whitelist.json", {})
    roles = whitelist.get("user_roles") if isinstance(whitelist, dict) and isinstance(whitelist.get("user_roles"), dict) else {}
    staff = store.read_json("staff.json", {})
    for user_id, actual_role in roles.items():
        if str(actual_role) != role or not _staff_is_active(store, str(user_id), role):
            continue
        profile = staff.get(user_id) if isinstance(staff, dict) else {}
        return str(user_id), str(profile.get("name") or user_id) if isinstance(profile, dict) else str(user_id)
    return "", ""


def proactive_health_snapshot(store: TuoguanStore, *, now: datetime | None = None) -> dict[str, Any]:
    timestamp = _now(now)
    authorizations = [item for item in _authorization_rows(store).values() if _authorization_is_effective(item, timestamp)]
    touches = list(_relationship_rows(store).values())
    actions = list(_goal_action_rows(store).values())
    status_counts: dict[str, int] = {}
    for item in touches:
        status = str(item.get("status") or "candidate")
        status_counts[status] = status_counts.get(status, 0) + 1
    goal_status_counts: dict[str, int] = {}
    for item in actions:
        status = str(item.get("status") or "planned")
        goal_status_counts[status] = goal_status_counts.get(status, 0) + 1
    return {
        "effective_authorization_count": len(authorizations),
        "relationship_status_counts": status_counts,
        "goal_action_status_counts": goal_status_counts,
        "open_goal_action_count": sum(count for status, count in goal_status_counts.items() if status in OPEN_GOAL_ACTION_STATUSES),
        "stuck_candidate_count": sum(1 for item in touches if str(item.get("status") or "candidate") == "candidate" and (_parse_time(item.get("created_at")) or timestamp) < timestamp - timedelta(hours=6)),
        "unverified_promise_risk": sum(1 for item in touches if str(item.get("status") or "") in {"candidate", "authorized"}),
    }
