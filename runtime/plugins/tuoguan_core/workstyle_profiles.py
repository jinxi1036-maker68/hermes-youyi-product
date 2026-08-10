"""Per-person service style memory for Xiaoyou.

This module stores how Xiaoyou should serve a specific person. It is a
preference ledger, not an institution policy store and not a router.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
import uuid
from typing import Any

from .models import UserIdentity
from .store import TuoguanStore
from .tenant_context import current_tenant_id


WORKSTYLE_EVENTS_FILE = "person_workstyle_events.jsonl"

LOW_RISK_PREFERENCE_TYPES = {
    "report_length",
    "tone",
    "reminder_time",
    "followup_style",
    "detail_level",
    "format",
    "avoidance",
    "positive_preference",
    "other_low_risk",
}

ALLOWED_SCOPES = {
    "daily_report",
    "direct_reply",
    "task_followup",
    "proactive_question",
    "teacher_support",
    "manager_support",
    "all_communication",
}

HIGH_RISK_TERMS = (
    "工资",
    "绩效",
    "权限",
    "角色权限",
    "自动联系家长",
    "自动发家长",
    "主动联系家长",
    "发给家长",
    "删除",
    "清空",
    "回滚",
    "退费",
    "收费",
    "价格",
    "优惠",
    "安全事件",
    "闭环安全",
    "改制度",
    "机构制度",
    "长期制度",
    "老板授权",
)

DEFAULT_DAILY_REPORT_STYLE = {
    "style_id": "default_owner_daily_report_concise_v1",
    "report_length": "concise",
    "detail_level": "key_points_only",
    "format": "3_to_5_one_line_items",
    "layout": "one_line_items",
    "max_items": 5,
    "max_chars": 700,
    "closing_line": "细节我已留档，需要我展开哪一项你直接说。",
}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _event_id() -> str:
    return f"workstyle_pref_{uuid.uuid4().hex[:12]}"


def _limit_text(value: Any, limit: int = 240) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _read_events(store: TuoguanStore) -> list[dict[str, Any]]:
    path = store.path_for(WORKSTYLE_EVENTS_FILE)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _append_event(store: TuoguanStore, row: dict[str, Any]) -> None:
    from .write_guard import assert_business_write_allowed

    assert_business_write_allowed(store.data_dir, WORKSTYLE_EVENTS_FILE)
    path = store.path_for(WORKSTYLE_EVENTS_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def _normalize_preference_type(value: str) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in LOW_RISK_PREFERENCE_TYPES else "other_low_risk"


def _normalize_scope(value: str) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in ALLOWED_SCOPES else "all_communication"


def _target_defaults(identity: UserIdentity, target_user_id: str, target_name: str, target_role: str) -> tuple[str, str, str]:
    user_id = str(target_user_id or identity.canonical_user_id or identity.platform_user_id or "").strip()
    name = str(target_name or identity.person_name or user_id).strip()
    role = str(target_role or identity.role or "staff").strip().lower()
    return user_id, name, role


def _can_access(identity: UserIdentity, target_user_id: str, target_role: str) -> bool:
    if identity.role == "boss":
        return True
    if str(target_user_id or "") == str(identity.canonical_user_id or ""):
        return True
    if identity.role == "manager" and str(target_role or "") in {"teacher", "staff", "manager"}:
        return True
    return False


def _risk_check(*values: Any) -> tuple[bool, str]:
    compact = "".join(str(value or "") for value in values)
    compact = "".join(compact.split()).lower()
    for term in HIGH_RISK_TERMS:
        if term.lower() in compact:
            return True, term
    return False, ""


def _semantic_fingerprint(
    *,
    target_user_id: str,
    preference_type: str,
    scope: str,
    normalized_rule: str,
    preference_text: str,
) -> str:
    base = "|".join(
        (
            str(current_tenant_id()),
            str(target_user_id),
            str(preference_type),
            str(scope),
            " ".join(str(normalized_rule or preference_text).split()).lower(),
        )
    )
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:24]


def _active_preferences(events: list[dict[str, Any]], *, target_user_id: str, scope: str = "") -> list[dict[str, Any]]:
    active_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in events:
        if str(item.get("record_type") or "") != "person_workstyle_preference":
            continue
        if str(item.get("tenant_id") or "") not in {"", current_tenant_id()}:
            continue
        if str(item.get("target_user_id") or "") != str(target_user_id or ""):
            continue
        if str(item.get("status") or "active") != "active":
            continue
        item_scope = str(item.get("scope") or "all_communication")
        if scope and item_scope not in {scope, "all_communication"}:
            continue
        key = (
            str(item.get("target_user_id") or ""),
            str(item.get("preference_type") or ""),
            item_scope,
        )
        active_by_key[key] = deepcopy(item)
    return sorted(active_by_key.values(), key=lambda row: str(row.get("created_at") or ""))


def query_person_workstyle_profile(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    target_user_id: str = "",
    target_role: str = "",
    scope: str = "",
    limit: int = 30,
) -> dict[str, Any]:
    user_id, name, role = _target_defaults(identity, target_user_id, "", target_role)
    normalized_scope = _normalize_scope(scope) if scope else ""
    if not user_id:
        return {"ok": False, "error": "target_user_id_required", "message": "缺少人员 id。"}
    if not _can_access(identity, user_id, role):
        return {"ok": False, "error": "permission_denied", "message": "当前账号不能查询该人员的服务方式档案。"}
    preferences = _active_preferences(_read_events(store), target_user_id=user_id, scope=normalized_scope)
    if limit > 0:
        preferences = preferences[-int(limit):]
    summary_lines = _summary_lines(preferences)
    return {
        "ok": True,
        "tenant_id": current_tenant_id(),
        "target_user_id": user_id,
        "target_name": name,
        "target_role": role,
        "scope": normalized_scope,
        "preferences": preferences,
        "preference_count": len(preferences),
        "default_style": deepcopy(DEFAULT_DAILY_REPORT_STYLE) if role == "boss" else {},
        "summary_lines": summary_lines,
        "rendered_text": _render_profile(name, role, preferences),
        "render_verified": True,
        "auto_effects": _safe_auto_effects(),
    }


def submit_person_workstyle_preference(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    preference_type: str,
    scope: str,
    preference_text: str,
    operation_id: str,
    normalized_rule: str = "",
    target_user_id: str = "",
    target_name: str = "",
    target_role: str = "",
    source_text: str = "",
) -> dict[str, Any]:
    target_user_id, target_name, target_role = _target_defaults(identity, target_user_id, target_name, target_role)
    preference_type = _normalize_preference_type(preference_type)
    scope = _normalize_scope(scope)
    preference_text = _limit_text(preference_text, 360)
    normalized_rule = _limit_text(normalized_rule or preference_text, 360)
    source_text = _limit_text(source_text or preference_text, 500)
    if not target_user_id:
        return {"ok": False, "error": "target_user_id_required", "message": "缺少人员 id。"}
    if not preference_text:
        return {"ok": False, "error": "preference_text_required", "message": "缺少要保存的工作方式偏好原文。"}
    if not _can_access(identity, target_user_id, target_role):
        return {"ok": False, "error": "permission_denied", "message": "当前账号不能替该人员保存服务方式偏好。"}
    high_risk, term = _risk_check(preference_type, scope, preference_text, normalized_rule, source_text)
    if high_risk:
        return {
            "ok": False,
            "error": "workstyle_preference_high_risk",
            "risk_level": "high",
            "blocked_term": term,
            "message": "这句话涉及权限、制度、工资、家长外发、数据删除或安全边界，不能自动保存为个人工作方式偏好。",
            "auto_effects": _safe_auto_effects(),
        }
    fingerprint = _semantic_fingerprint(
        target_user_id=target_user_id,
        preference_type=preference_type,
        scope=scope,
        normalized_rule=normalized_rule,
        preference_text=preference_text,
    )
    events = _read_events(store)
    for item in reversed(events):
        if str(item.get("record_type") or "") != "person_workstyle_preference":
            continue
        if str(item.get("semantic_fingerprint") or "") == fingerprint and str(item.get("status") or "") == "active":
            profile = query_person_workstyle_profile(
                store,
                identity=identity,
                target_user_id=target_user_id,
                target_role=target_role,
                scope=scope,
            )
            return {
                "ok": True,
                "idempotent_replay": True,
                "preference": deepcopy(item),
                "profile": profile,
                "writeback_verified": True,
                "rendered_text": _render_saved(item),
                "auto_effects": _safe_auto_effects(),
            }
    supersedes = [
        str(item.get("preference_id") or "")
        for item in _active_preferences(events, target_user_id=target_user_id)
        if str(item.get("preference_type") or "") == preference_type
        and str(item.get("scope") or "") == scope
    ]
    row = {
        "record_type": "person_workstyle_preference",
        "preference_id": _event_id(),
        "tenant_id": current_tenant_id(),
        "target_user_id": target_user_id,
        "target_name": target_name,
        "target_role": target_role,
        "preference_type": preference_type,
        "scope": scope,
        "preference_text": preference_text,
        "normalized_rule": normalized_rule,
        "risk_level": "low",
        "status": "active",
        "semantic_fingerprint": fingerprint,
        "supersedes": [item for item in supersedes if item],
        "source_text": source_text,
        "source": {
            "actor_user_id": identity.canonical_user_id,
            "actor_name": identity.person_name,
            "actor_role": identity.role,
            "operation_id": str(operation_id or ""),
        },
        "created_at": now_iso(),
        "auto_effects": _safe_auto_effects(),
    }
    _append_event(store, row)
    verify_events = _read_events(store)
    verified = any(str(item.get("preference_id") or "") == row["preference_id"] for item in verify_events)
    profile = query_person_workstyle_profile(
        store,
        identity=identity,
        target_user_id=target_user_id,
        target_role=target_role,
        scope=scope,
    )
    active_verified = any(
        str(item.get("preference_id") or "") == row["preference_id"]
        for item in profile.get("preferences") or []
    )
    return {
        "ok": bool(verified and active_verified),
        "preference": row,
        "profile": profile,
        "writeback_verified": bool(verified and active_verified),
        "rendered_text": _render_saved(row) if verified and active_verified else "我理解了，但这条服务方式偏好还没有保存成功。",
        "auto_effects": _safe_auto_effects(),
    }


def workstyle_context_for_user(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    scope: str = "direct_reply",
    raw_text: str = "",
) -> str:
    profile = query_person_workstyle_profile(
        store,
        identity=identity,
        target_user_id=identity.canonical_user_id,
        target_role=identity.role,
        scope=scope,
        limit=8,
    )
    if not profile.get("ok"):
        return ""
    lines = [
        "【小优服务方式档案】",
        f"当前对象：{profile.get('target_name') or identity.person_name or identity.canonical_user_id}（{_role_label(profile.get('target_role') or identity.role)}）。",
        "优先级：安全/法律/权限 > 老板正式目标和任务 > 机构制度 > 个人工作方式偏好 > 默认风格。",
    ]
    if profile.get("summary_lines"):
        lines.append("已保存偏好：" + "；".join(profile["summary_lines"][:5]))
    elif identity.role == "boss":
        lines.append("默认老板早晚报：3-5条，一条一行，只说重点；异常才展开。")
    else:
        lines.append("目前没有保存过该人员的服务方式偏好，按默认专业、简洁、事实优先方式服务。")
    if _looks_like_workstyle_feedback(raw_text):
        lines.extend([
            "本轮可能包含服务方式反馈。若模型判断是低风险偏好，请调用 tuoguan_submit_person_workstyle_preference 保存。",
            "未看到工具 ok=true 且 writeback_verified=true 前，不得说“已保存、记住了、以后按这个来”。",
            "若涉及权限、工资、制度、家长外发、数据删除或安全闭环，只能说明边界或形成待确认候选，不能保存为低风险偏好。",
        ])
    return "\n".join(lines)


def daily_report_style_for_owner(store: TuoguanStore, owner_id: str) -> dict[str, Any]:
    style = deepcopy(DEFAULT_DAILY_REPORT_STYLE)
    if not owner_id:
        return style
    identity = UserIdentity(
        platform="system",
        platform_user_id=owner_id,
        canonical_user_id=owner_id,
        person_name="老板",
        role="boss",
        approval_state="approved",
    )
    profile = query_person_workstyle_profile(
        store,
        identity=identity,
        target_user_id=owner_id,
        target_role="boss",
        scope="daily_report",
        limit=20,
    )
    applied_preferences = _daily_report_preferences_for_style(store, owner_id)
    for pref in applied_preferences:
        _apply_daily_report_preference(style, pref)
    style["active_preferences"] = profile.get("preferences") or []
    style["applied_preferences"] = applied_preferences
    return style


def _daily_report_preferences_for_style(store: TuoguanStore, owner_id: str) -> list[dict[str, Any]]:
    """Return recent daily-report preferences as additive style dimensions.

    Workstyle events deliberately supersede same-type preferences for a compact
    profile view. Daily report rendering needs a slightly richer interpretation:
    "leave blank lines" is an additive formatting refinement, not a replacement
    for "only three key lines".
    """

    events = [
        deepcopy(item)
        for item in _read_events(store)
        if str(item.get("record_type") or "") == "person_workstyle_preference"
        and str(item.get("target_user_id") or "") == str(owner_id)
        and str(item.get("scope") or "") == "daily_report"
        and str(item.get("status") or "active") == "active"
    ]
    return events[-20:]


def _apply_daily_report_preference(style: dict[str, Any], pref: dict[str, Any]) -> None:
    ptype = str(pref.get("preference_type") or "")
    if ptype not in {"report_length", "detail_level", "format"}:
        return
    text = str(pref.get("normalized_rule") or pref.get("preference_text") or "")
    compact = "".join(text.split()).lower()
    if any(
        term in compact
        for term in (
            "更短", "简单", "简短", "少说", "只说重点", "极简", "三条", "3条", "三项", "3项",
            "3-5", "3到5", "三到五", "每条一行", "快速浏览", "扫一眼", "一句话带过",
        )
    ):
        style["report_length"] = "ultra_concise"
        style["max_items"] = 3
        style["max_chars"] = 520
    elif any(term in compact for term in ("详细", "展开", "多说", "细节")):
        style["report_length"] = "expanded"
        style["max_items"] = 5
        style["max_chars"] = 1000
    if any(term in compact for term in ("段落分明", "留空行", "空一行", "不要拥挤", "不拥挤", "别挤", "堆叠", "不堆")):
        style["layout"] = "spaced_sections"
    if any(term in compact for term in ("正常/异常", "需确认", "今日重点", "完成x件", "明日计划", "异常有/无")):
        style["format"] = "status_confirm_focus"


def _looks_like_workstyle_feedback(text: str) -> bool:
    compact = "".join(str(text or "").split())
    return any(
        term in compact
        for term in (
            "以后",
            "从现在开始",
            "别再",
            "不要再",
            "改成",
            "简单点",
            "简短",
            "少说点",
            "只说重点",
            "五点后",
            "几点后",
            "提醒我",
            "汇报格式",
            "语气",
            "我喜欢",
            "我不喜欢",
        )
    )


def _summary_lines(preferences: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for item in preferences:
        label = _preference_label(str(item.get("preference_type") or ""))
        scope = _scope_label(str(item.get("scope") or "all_communication"))
        rule = _limit_text(item.get("normalized_rule") or item.get("preference_text"), 80)
        if rule:
            lines.append(f"{scope}/{label}：{rule}")
    return lines[-8:]


def _render_profile(name: str, role: str, preferences: list[dict[str, Any]]) -> str:
    lines = [f"{name or '当前人员'}的服务方式档案（{_role_label(role)}）："]
    if not preferences:
        lines.append("- 暂无个人偏好，按默认专业、简洁、事实优先方式服务。")
        if role == "boss":
            lines.append("- 老板日报默认极简重点版：3-5条，一条一行。")
        return "\n".join(lines)
    for line in _summary_lines(preferences):
        lines.append(f"- {line}")
    return "\n".join(lines)


def _render_saved(row: dict[str, Any]) -> str:
    return f"已保存{_scope_label(str(row.get('scope') or 'all_communication'))}服务偏好：{_limit_text(row.get('normalized_rule') or row.get('preference_text'), 80)}"


def _safe_auto_effects() -> dict[str, bool]:
    return {
        "sends_parent_messages": False,
        "sends_teacher_messages": False,
        "creates_teacher_tasks": False,
        "changes_salary": False,
        "changes_permissions": False,
        "changes_responsibility_binding": False,
        "deletes_data": False,
        "changes_router": False,
        "forces_next_action": False,
        "changes_institution_policy": False,
    }


def _preference_label(value: str) -> str:
    return {
        "report_length": "汇报长短",
        "tone": "语气",
        "reminder_time": "提醒时间",
        "followup_style": "跟进方式",
        "detail_level": "细节程度",
        "format": "格式",
        "avoidance": "不要这样说",
        "positive_preference": "希望这样说",
        "other_low_risk": "其他低风险偏好",
    }.get(value, value)


def _scope_label(value: str) -> str:
    return {
        "daily_report": "日报",
        "direct_reply": "日常对话",
        "task_followup": "任务跟进",
        "proactive_question": "主动提问",
        "teacher_support": "老师支持",
        "manager_support": "店长支持",
        "all_communication": "所有沟通",
    }.get(value, value)


def _role_label(value: str) -> str:
    return {"boss": "老板", "manager": "店长", "teacher": "老师", "staff": "员工"}.get(str(value or ""), str(value or "员工"))
