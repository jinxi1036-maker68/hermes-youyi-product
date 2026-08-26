"""Per-person service style memory for Xiaoyou.

This module stores how Xiaoyou should serve a specific person. It is a
preference ledger, not an institution policy store and not a router.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
import re
import uuid
from typing import Any

from .models import UserIdentity
from .store import TuoguanStore
from .tenant_context import current_tenant_id


WORKSTYLE_EVENTS_FILE = "person_workstyle_events.jsonl"
WORKSTYLE_APPLICATION_RECORD = "person_workstyle_application"

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

WORKSTYLE_DIMENSIONS = {
    "length",
    "layout",
    "structure",
    "tone",
    "timing",
    "detail",
    "avoidance",
    "followup_method",
    "interaction_pacing",
    "other",
}

ALLOWED_SCOPES = {
    "daily_report",
    "direct_reply",
    "task_followup",
    "proactive_question",
    "teacher_support",
    "manager_support",
    "institution_work",
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
    store.append_jsonl_verified(WORKSTYLE_EVENTS_FILE, row)


def _normalize_preference_type(value: str) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in LOW_RISK_PREFERENCE_TYPES else "other_low_risk"


def _normalize_scope(value: str) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in ALLOWED_SCOPES else "all_communication"


def infer_workstyle_scope(raw_text: str) -> str:
    """Choose a service-style scope from the current request, not old memory.

    This is only context selection for an already saved personal preference. It
    never picks a business action or changes an institution rule. In
    particular, a pacing preference for policy discussions must not make a
    normal conversation fail merely because it contains two questions.
    """

    compact = "".join(str(raw_text or "").split())
    if any(term in compact for term in ("制度", "流程", "机构工作", "执行方案", "落实方案")):
        return "institution_work"
    if any(term in compact for term in ("早报", "晚报", "日报", "汇报")):
        return "daily_report"
    if any(term in compact for term in ("任务", "跟进", "提醒", "催")):
        return "task_followup"
    if "主动" in compact and any(term in compact for term in ("问", "提问", "找")):
        return "proactive_question"
    return "direct_reply"


def _normalize_dimension_key(value: str) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in WORKSTYLE_DIMENSIONS else ""


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
    dimension_key: str,
    normalized_rule: str,
    preference_text: str,
) -> str:
    base = "|".join(
        (
            str(current_tenant_id()),
            str(target_user_id),
            str(preference_type),
            str(scope),
            str(dimension_key or ""),
            " ".join(str(normalized_rule or preference_text).split()).lower(),
        )
    )
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:24]


def _dimension_for_preference(preference_type: str, scope: str, *texts: Any) -> str:
    compact = "".join(str(text or "") for text in texts)
    compact = "".join(compact.split()).lower()
    if any(term in compact for term in ("一项一项", "一次一项", "一次不要说太多", "一个章节", "一个问题", "不要一下说完")):
        return "interaction_pacing"
    if str(preference_type or "") == "report_length":
        return "length"
    if any(term in compact for term in ("段落", "留空行", "空一行", "一条一行", "不要拥挤", "不拥挤", "别挤", "堆叠", "不堆")):
        return "layout"
    if any(term in compact for term in ("更短", "简单", "简短", "少说", "只说重点", "极简", "三条", "3条", "三项", "3项", "3-5", "三到五", "扫一眼", "一句话", "废话", "一大堆")):
        return "length"
    if any(term in compact for term in ("先说结论", "结论先行", "先给结论", "状态/异常", "正常/异常", "需确认", "今日重点", "明日计划", "完成x件", "格式", "模板")):
        return "structure"
    if any(term in compact for term in ("五点后", "几点后", "提醒时间", "晚点", "早上", "上午", "下午", "晚上", "明天", "定时")):
        return "timing"
    if any(term in compact for term in ("语气", "直接", "柔和", "温和", "客气", "别生硬", "像朋友", "鼓励", "严肃", "强硬")):
        return "tone"
    if any(term in compact for term in ("详细", "展开", "细节", "过程", "来源", "证据", "不要展开", "少讲过程")):
        return "detail"
    if any(term in compact for term in ("别再", "不要", "不准", "禁止", "不要说", "别说", "少说")):
        return "avoidance"
    if any(term in compact for term in ("跟进", "提醒", "催", "问我", "先问", "先给方案", "方案再问", "处理方式")):
        return "followup_method"
    return {
        "report_length": "length",
        "format": "structure",
        "tone": "tone",
        "reminder_time": "timing",
        "followup_style": "followup_method",
        "detail_level": "detail",
        "avoidance": "avoidance",
        "positive_preference": "tone",
    }.get(str(preference_type or ""), "other")


def _dimension_for_row(row: dict[str, Any]) -> str:
    explicit = _normalize_dimension_key(str(row.get("dimension_key") or ""))
    if explicit:
        return explicit
    return _dimension_for_preference(
        str(row.get("preference_type") or ""),
        str(row.get("scope") or ""),
        row.get("normalized_rule"),
        row.get("preference_text"),
        row.get("source_text"),
    )


def _active_preferences(events: list[dict[str, Any]], *, target_user_id: str, scope: str = "") -> list[dict[str, Any]]:
    active_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    semantic_mismatches = {
        str(item.get("preference_id") or "")
        for item in events
        if str(item.get("record_type") or "") == "person_workstyle_semantic_mismatch"
        and str(item.get("status") or "superseded") == "superseded"
    }
    for item in events:
        if str(item.get("record_type") or "") != "person_workstyle_preference":
            continue
        if str(item.get("tenant_id") or "") not in {"", current_tenant_id()}:
            continue
        if str(item.get("target_user_id") or "") != str(target_user_id or ""):
            continue
        if str(item.get("status") or "active") != "active":
            continue
        if str(item.get("preference_id") or "") in semantic_mismatches:
            continue
        item_scope = str(item.get("scope") or "all_communication")
        if scope and item_scope not in {scope, "all_communication"}:
            continue
        key = (
            str(item.get("target_user_id") or ""),
            item_scope,
            _dimension_for_row(item),
        )
        copied = deepcopy(item)
        copied["dimension_key"] = _dimension_for_row(copied)
        active_by_key[key] = copied
    return sorted(active_by_key.values(), key=lambda row: str(row.get("created_at") or ""))


def _normalize_confidence(value: float | str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = 1.0
    return max(0.0, min(parsed, 1.0))


def _scope_rank(row_scope: str, requested_scope: str) -> int:
    if str(row_scope or "") == str(requested_scope or "") and requested_scope:
        return 2
    if str(row_scope or "") == "all_communication":
        return 1
    return 0


def _effective_preferences(preferences: list[dict[str, Any]], *, scope: str = "") -> list[dict[str, Any]]:
    by_dimension: dict[str, dict[str, Any]] = {}
    ordered = sorted(
        [deepcopy(item) for item in preferences],
        key=lambda row: (_scope_rank(str(row.get("scope") or ""), scope), str(row.get("created_at") or "")),
    )
    for item in ordered:
        dimension = _dimension_for_row(item)
        item["dimension_key"] = dimension
        by_dimension[dimension] = item
    return sorted(by_dimension.values(), key=lambda row: str(row.get("created_at") or ""))


def _effective_preferences_by_scope(preferences: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    scopes = sorted({str(item.get("scope") or "all_communication") for item in preferences})
    for scope in scopes:
        result[scope] = _effective_preferences(
            [
                item for item in preferences
                if str(item.get("scope") or "all_communication") in {scope, "all_communication"}
            ],
            scope=scope,
        )
    return result


def _recent_application_status(events: list[dict[str, Any]], *, target_user_id: str, scope: str = "") -> dict[str, Any]:
    rows = [
        deepcopy(item)
        for item in events
        if str(item.get("record_type") or "") == WORKSTYLE_APPLICATION_RECORD
        and str(item.get("target_user_id") or "") == str(target_user_id or "")
        and str(item.get("tenant_id") or "") in {"", current_tenant_id()}
        and (not scope or str(item.get("scope") or "") in {scope, "all_communication"})
    ]
    rows.sort(key=lambda row: str(row.get("created_at") or ""))
    latest = rows[-1] if rows else {}
    failures = [
        item for item in rows[-20:]
        if item.get("compliance", {}).get("ok") is False
        or item.get("unverified_commitment")
        or item.get("missed_feedback_save")
    ]
    return {
        "application_count": len(rows),
        "latest_application": latest,
        "recent_failure_count": len(failures),
        "recent_failures": failures[-5:],
    }


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
    events = _read_events(store)
    preferences = _active_preferences(events, target_user_id=user_id, scope=normalized_scope)
    if limit > 0:
        preferences = preferences[-int(limit):]
    effective = _effective_preferences(preferences, scope=normalized_scope)
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
        "effective_preferences": effective,
        "effective_preferences_by_scope": _effective_preferences_by_scope(preferences),
        "applied_dimensions": sorted({str(item.get("dimension_key") or "") for item in effective if str(item.get("dimension_key") or "")}),
        "recent_application_status": _recent_application_status(events, target_user_id=user_id, scope=normalized_scope),
        "default_style": deepcopy(DEFAULT_DAILY_REPORT_STYLE) if role == "boss" else {},
        "summary_lines": summary_lines,
        "rendered_text": _render_profile(name, role, preferences),
        "render_verified": True,
        "auto_effects": _safe_auto_effects(),
    }


def resolve_workstyle_for(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    target_user_id: str = "",
    target_role: str = "",
    scope: str = "direct_reply",
    limit: int = 30,
) -> dict[str, Any]:
    profile = query_person_workstyle_profile(
        store,
        identity=identity,
        target_user_id=target_user_id,
        target_role=target_role,
        scope=scope,
        limit=limit,
    )
    if not profile.get("ok"):
        return profile
    effective = profile.get("effective_preferences") or []
    constraints = _output_constraints_from_preferences(effective)
    return {
        "ok": True,
        "tenant_id": profile.get("tenant_id"),
        "target_user_id": profile.get("target_user_id"),
        "target_name": profile.get("target_name"),
        "target_role": profile.get("target_role"),
        "scope": profile.get("scope") or _normalize_scope(scope),
        "effective_preferences": effective,
        "applied_dimensions": profile.get("applied_dimensions") or [],
        "output_constraints": constraints,
        "recent_application_status": profile.get("recent_application_status") or {},
        "boundary": _boundary(),
        "rendered_text": _render_resolved_workstyle(profile, constraints),
        "render_verified": True,
    }


def record_workstyle_application(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    scope: str,
    source_message_id: str = "",
    final_reply: str = "",
    operation_id: str = "",
    applied_preferences: list[dict[str, Any]] | None = None,
    compliance: dict[str, Any] | None = None,
    missed_feedback_save: bool = False,
    unverified_commitment: bool = False,
) -> dict[str, Any]:
    scope = _normalize_scope(scope)
    preferences = applied_preferences
    if preferences is None:
        resolved = resolve_workstyle_for(store, identity=identity, scope=scope, limit=20)
        preferences = resolved.get("effective_preferences") if resolved.get("ok") else []
    preferences = preferences or []
    row = {
        "record_type": WORKSTYLE_APPLICATION_RECORD,
        "application_id": f"workstyle_app_{uuid.uuid4().hex[:12]}",
        "tenant_id": current_tenant_id(),
        "target_user_id": identity.canonical_user_id,
        "target_name": identity.person_name,
        "target_role": identity.role,
        "scope": scope,
        "applied_preference_ids": [str(item.get("preference_id") or "") for item in preferences if str(item.get("preference_id") or "")],
        "applied_dimensions": sorted({str(item.get("dimension_key") or _dimension_for_row(item)) for item in preferences if isinstance(item, dict)}),
        "source_message_id": str(source_message_id or ""),
        "operation_id": str(operation_id or ""),
        "final_reply_excerpt": _limit_text(final_reply, 300),
        "compliance": compliance or _check_reply_compliance(final_reply, preferences),
        "missed_feedback_save": bool(missed_feedback_save),
        "unverified_commitment": bool(unverified_commitment),
        "created_at": now_iso(),
        "auto_effects": _safe_auto_effects(),
    }
    _append_event(store, row)
    verified = any(
        str(item.get("application_id") or "") == row["application_id"]
        for item in _read_events(store)[-80:]
    )
    return {
        "ok": bool(verified),
        "application": row,
        "writeback_verified": bool(verified),
        "rendered_text": "已记录本轮工作方式应用证据。" if verified else "已理解本轮工作方式应用，但写后反查未通过。",
        "auto_effects": _safe_auto_effects(),
    }


def observe_workstyle_after_reply(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    raw_text: str,
    final_reply: str,
    raw_model_final_reply: str = "",
    tool_calls: list[Any] | None = None,
    tool_results: list[Any] | None = None,
    ledger_id: str = "",
    message_id: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    feedback = _classify_feedback_for_autosave(raw_text)
    tool_saved = _tool_results_include_verified_workstyle(tool_calls or [], tool_results or [])
    unverified_commitment = _looks_like_saved_claim(raw_model_final_reply or final_reply) and not tool_saved
    auto_saved: dict[str, Any] = {}
    missed_feedback_save = bool(feedback.get("ok") and not tool_saved)
    if missed_feedback_save:
        from .write_guard import authorized_system_write

        with authorized_system_write(
            store.data_dir,
            job_name="workstyle_feedback_guard",
            allowed_files={WORKSTYLE_EVENTS_FILE},
        ):
            auto_saved = submit_person_workstyle_preference(
                store,
                identity=identity,
                preference_type=str(feedback.get("preference_type") or "other_low_risk"),
                scope=str(feedback.get("scope") or "all_communication"),
                preference_text=str(feedback.get("preference_text") or raw_text),
                normalized_rule=str(feedback.get("normalized_rule") or feedback.get("preference_text") or raw_text),
                dimension_key=str(feedback.get("dimension_key") or ""),
                confidence=float(feedback.get("confidence") or 0.9),
                source_text=raw_text,
                source_turn_id=session_id,
                operation_id=f"{message_id or ledger_id or uuid.uuid4().hex}:auto_workstyle_feedback",
            )
    resolved = resolve_workstyle_for(
        store,
        identity=identity,
        scope=str(feedback.get("scope") or "direct_reply"),
        limit=20,
    )
    application: dict[str, Any] = {}
    if resolved.get("ok") and (resolved.get("effective_preferences") or missed_feedback_save or unverified_commitment):
        from .write_guard import authorized_system_write

        with authorized_system_write(
            store.data_dir,
            job_name="workstyle_application_observer",
            allowed_files={WORKSTYLE_EVENTS_FILE},
        ):
            application = record_workstyle_application(
                store,
                identity=identity,
                scope=str(resolved.get("scope") or "direct_reply"),
                source_message_id=message_id,
                final_reply=final_reply,
                operation_id=f"{message_id or ledger_id or uuid.uuid4().hex}:workstyle_application",
                applied_preferences=resolved.get("effective_preferences") or [],
                missed_feedback_save=missed_feedback_save,
                unverified_commitment=unverified_commitment,
            )
    if missed_feedback_save or unverified_commitment:
        _record_workstyle_self_correction(
            store,
            identity=identity,
            raw_text=raw_text,
            final_reply=final_reply,
            ledger_id=ledger_id,
            message_id=message_id,
            missed_feedback_save=missed_feedback_save,
            unverified_commitment=unverified_commitment,
        )
    return {
        "ok": True,
        "feedback_detected": bool(feedback.get("ok")),
        "tool_saved": bool(tool_saved),
        "auto_saved": bool(auto_saved.get("ok")),
        "auto_save_result": auto_saved,
        "application_recorded": bool(application.get("ok")),
        "application_result": application,
        "missed_feedback_save": missed_feedback_save,
        "unverified_commitment": unverified_commitment,
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
    dimension_key: str = "",
    confidence: float | str = 1.0,
    source_turn_id: str = "",
) -> dict[str, Any]:
    target_user_id, target_name, target_role = _target_defaults(identity, target_user_id, target_name, target_role)
    preference_type = _normalize_preference_type(preference_type)
    scope = _normalize_scope(scope)
    preference_text = _limit_text(preference_text, 360)
    normalized_rule = _limit_text(normalized_rule or preference_text, 360)
    source_text = _limit_text(source_text or preference_text, 500)
    dimension_key = _normalize_dimension_key(dimension_key) or _dimension_for_preference(preference_type, scope, normalized_rule, preference_text, source_text)
    confidence_value = _normalize_confidence(confidence)
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
        dimension_key=dimension_key,
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
        if str(item.get("scope") or "") == scope
        and _dimension_for_row(item) == dimension_key
    ]
    row = {
        "record_type": "person_workstyle_preference",
        "preference_id": _event_id(),
        "tenant_id": current_tenant_id(),
        "target_user_id": target_user_id,
        "target_name": target_name,
        "target_role": target_role,
        "preference_type": preference_type,
        "dimension_key": dimension_key,
        "scope": scope,
        "preference_text": preference_text,
        "normalized_rule": normalized_rule,
        "confidence": confidence_value,
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
            "source_turn_id": str(source_turn_id or ""),
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
    profile = resolve_workstyle_for(
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
    elif profile.get("effective_preferences"):
        lines.append("当前有效偏好：" + "；".join(
            f"{_dimension_label(str(item.get('dimension_key') or _dimension_for_row(item)))}：{_limit_text(item.get('normalized_rule') or item.get('preference_text'), 70)}"
            for item in (profile.get("effective_preferences") or [])[:5]
        ))
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
    resolved = resolve_workstyle_for(
        store,
        identity=identity,
        target_user_id=owner_id,
        target_role="boss",
        scope="daily_report",
        limit=20,
    )
    applied_preferences = resolved.get("effective_preferences") or _daily_report_preferences_for_style(store, owner_id)
    for pref in applied_preferences:
        _apply_daily_report_preference(style, pref)
    style["active_preferences"] = profile.get("preferences") or []
    style["applied_preferences"] = applied_preferences
    style["applied_dimensions"] = resolved.get("applied_dimensions") or []
    return style


def query_workstyle_adaptation_health(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    target_user_id: str = "",
    scope: str = "",
    limit: int = 30,
) -> dict[str, Any]:
    if identity.role not in {"boss", "manager"} and identity.platform != "system":
        return {"ok": False, "error": "permission_denied", "message": "只有老板、店长或系统巡检可以查看工作方式自适应健康度。"}
    events = _read_events(store)
    target = str(target_user_id or "").strip()
    normalized_scope = _normalize_scope(scope) if scope else ""
    rows = [
        deepcopy(item)
        for item in events
        if str(item.get("tenant_id") or "") in {"", current_tenant_id()}
        and str(item.get("record_type") or "") in {"person_workstyle_preference", WORKSTYLE_APPLICATION_RECORD}
        and (not target or str(item.get("target_user_id") or "") == target)
        and (not normalized_scope or str(item.get("scope") or "") in {normalized_scope, "all_communication"})
    ]
    rows.sort(key=lambda item: str(item.get("created_at") or ""))
    cap = max(1, min(int(limit or 30), 200))
    rows = rows[-cap:]
    preferences = [item for item in rows if str(item.get("record_type") or "") == "person_workstyle_preference"]
    applications = [item for item in rows if str(item.get("record_type") or "") == WORKSTYLE_APPLICATION_RECORD]
    missed = [item for item in applications if item.get("missed_feedback_save")]
    unverified = [item for item in applications if item.get("unverified_commitment")]
    failed = [
        item for item in applications
        if isinstance(item.get("compliance"), dict) and item["compliance"].get("ok") is False
    ]
    return {
        "ok": True,
        "tenant_id": current_tenant_id(),
        "report_type": "xiaoyou_workstyle_adaptation_health_v1",
        "preference_count": len(preferences),
        "application_count": len(applications),
        "missed_feedback_save_count": len(missed),
        "unverified_commitment_count": len(unverified),
        "application_failure_count": len(failed),
        "recent_preferences": preferences[-8:],
        "recent_applications": applications[-8:],
        "health_signals": {
            "has_missed_feedback_save": bool(missed),
            "has_unverified_commitment": bool(unverified),
            "has_application_failure": bool(failed),
        },
        "rendered_text": _render_adaptation_health(preferences, applications, missed, unverified, failed),
        "render_verified": True,
        "boundary": _boundary(),
    }


def _output_constraints_from_preferences(preferences: list[dict[str, Any]]) -> dict[str, Any]:
    constraints: dict[str, Any] = {
        "max_items": None,
        "max_chars": None,
        "layout": "",
        "structure": "",
        "tone_notes": [],
        "timing_notes": [],
        "detail_notes": [],
        "avoidance_notes": [],
        "followup_notes": [],
        "interaction_pacing_notes": [],
    }
    for pref in preferences:
        dimension = str(pref.get("dimension_key") or _dimension_for_row(pref))
        text = str(pref.get("normalized_rule") or pref.get("preference_text") or "")
        compact = "".join(text.split()).lower()
        if dimension == "length":
            if any(term in compact for term in ("三条", "3条", "三项", "3项", "3-5", "三到五", "只说重点", "极简", "简短", "少说")):
                constraints["max_items"] = 3
                constraints["max_chars"] = 520
            elif any(term in compact for term in ("详细", "展开", "多说")):
                constraints["max_items"] = 5
                constraints["max_chars"] = 1000
        elif dimension == "layout":
            if any(term in compact for term in ("段落分明", "留空行", "空一行", "不拥挤", "不要拥挤")):
                constraints["layout"] = "spaced_sections"
            elif any(term in compact for term in ("每条一行", "一条一行")):
                constraints["layout"] = constraints.get("layout") or "one_line_items"
        elif dimension == "structure":
            constraints["structure"] = _limit_text(text, 160)
        elif dimension == "tone":
            constraints["tone_notes"].append(_limit_text(text, 120))
        elif dimension == "timing":
            constraints["timing_notes"].append(_limit_text(text, 120))
        elif dimension == "detail":
            constraints["detail_notes"].append(_limit_text(text, 120))
        elif dimension == "avoidance":
            constraints["avoidance_notes"].append(_limit_text(text, 120))
        elif dimension == "followup_method":
            constraints["followup_notes"].append(_limit_text(text, 120))
        elif dimension == "interaction_pacing":
            constraints["interaction_pacing_notes"].append(_limit_text(text, 120))
    return constraints


def _boundary() -> dict[str, bool]:
    return {
        "model_led": True,
        "changes_policy": False,
        "changes_salary": False,
        "changes_permissions": False,
        "sends_parent_messages": False,
        "changes_router": False,
        "forces_next_action": False,
    }


def _render_resolved_workstyle(profile: dict[str, Any], constraints: dict[str, Any]) -> str:
    lines = [
        f"{profile.get('target_name') or '当前人员'}当前有效服务方式：",
    ]
    effective = profile.get("effective_preferences") or []
    if not effective:
        lines.append("- 暂无个人偏好，按默认专业、简洁、事实优先方式服务。")
    else:
        for item in effective[-6:]:
            lines.append(f"- {_scope_label(str(item.get('scope') or 'all_communication'))}/{_dimension_label(str(item.get('dimension_key') or _dimension_for_row(item)))}：{_limit_text(item.get('normalized_rule') or item.get('preference_text'), 80)}")
    if constraints.get("max_items"):
        lines.append(f"- 输出上限：约 {constraints.get('max_items')} 条重点。")
    if constraints.get("interaction_pacing_notes"):
        lines.append("- 节奏要求：制度、流程或方案讨论一次只推进一个章节，并且最多问一个关键问题。")
    return "\n".join(lines)


def _check_reply_compliance(final_reply: str, preferences: list[dict[str, Any]]) -> dict[str, Any]:
    text = str(final_reply or "")
    constraints = _output_constraints_from_preferences(preferences)
    failures: list[str] = []
    max_chars = constraints.get("max_chars")
    if isinstance(max_chars, int) and max_chars > 0 and len(text) > max_chars + 80:
        failures.append("reply_too_long_for_saved_length_preference")
    for pref in preferences:
        if str(pref.get("dimension_key") or _dimension_for_row(pref)) != "avoidance":
            continue
        rule = str(pref.get("normalized_rule") or pref.get("preference_text") or "")
        for term in _avoidance_terms(rule):
            if term and term in text:
                failures.append(f"avoidance_term_present:{term}")
                break
    if constraints.get("interaction_pacing_notes"):
        questions = len(re.findall(r"[？?]", text))
        headings = len(re.findall(r"(?m)^\s*(?:第[一二三四五六七八九十\d]+[章节]|[一二三四五六七八九十\d]+[、.．])", text))
        if questions > 1:
            failures.append("interaction_pacing_multiple_questions")
        if headings > 1:
            failures.append("interaction_pacing_multiple_sections")
    return {"ok": not failures, "failures": failures, "checked_at": now_iso()}


def _avoidance_terms(rule: str) -> list[str]:
    text = str(rule or "")
    terms: list[str] = []
    for marker in ("不要说", "别说", "不要再说", "别再说"):
        if marker in text:
            terms.append(text.split(marker, 1)[1].strip(" ：:，,。；;")[:24])
    if "废话" in text:
        terms.append("废话")
    return [term for term in terms if term]


def _classify_feedback_for_autosave(raw_text: str) -> dict[str, Any]:
    text = _limit_text(raw_text, 500)
    if not _looks_like_workstyle_feedback(text):
        return {"ok": False}
    high_risk, term = _risk_check(text)
    if high_risk:
        return {"ok": False, "risk_level": "high", "blocked_term": term}
    compact = "".join(text.split())
    scope = infer_workstyle_scope(text)
    if scope == "direct_reply":
        scope = "all_communication"
    elif "老师" in compact:
        scope = "teacher_support"
    elif "店长" in compact:
        scope = "manager_support"
    preference_type = "other_low_risk"
    if any(term in compact for term in ("短", "少说", "只说重点", "三条", "3条", "一大堆", "废话")):
        preference_type = "report_length"
    elif any(term in compact for term in ("格式", "段落", "空行", "先说结论", "结论")):
        preference_type = "format"
    elif any(term in compact for term in ("语气", "直接", "柔和", "温和", "客气")):
        preference_type = "tone"
    elif any(term in compact for term in ("几点", "五点", "早上", "晚上", "下午")):
        preference_type = "reminder_time"
    elif any(term in compact for term in ("一项一项", "一次一项", "一次不要说太多", "一个章节", "一个问题", "不要一下说完")):
        preference_type = "other_low_risk"
    elif any(term in compact for term in ("跟进", "提醒", "方案")):
        preference_type = "followup_style"
    elif any(term in compact for term in ("细节", "详细", "展开", "过程")):
        preference_type = "detail_level"
    elif any(term in compact for term in ("别再", "不要", "不准", "禁止")):
        preference_type = "avoidance"
    dimension = _dimension_for_preference(preference_type, scope, text)
    if not _is_explicit_feedback(text):
        return {"ok": False}
    return {
        "ok": True,
        "preference_type": preference_type,
        "scope": scope,
        "dimension_key": dimension,
        "preference_text": text,
        "normalized_rule": _normalize_feedback_rule(text, dimension),
        "confidence": 0.92,
    }


def _is_explicit_feedback(text: str) -> bool:
    compact = "".join(str(text or "").split())
    return any(term in compact for term in ("以后", "从现在开始", "下次", "别再", "不要再", "改成", "你要", "必须", "应该", "注意")) and any(
        term in compact
        for term in ("汇报", "回复", "提醒", "跟进", "语气", "格式", "工作方式", "服务", "沟通", "先说结论", "只说重点", "少说", "简单", "一项一项", "一次一项", "一次不要说太多", "一个章节", "一个问题")
    )


def _normalize_feedback_rule(text: str, dimension: str) -> str:
    value = _limit_text(text, 220)
    if dimension == "length" and any(term in value for term in ("只说重点", "三条", "3条", "少说", "简单")):
        return value
    if dimension == "structure" and "先说结论" in value:
        return value
    return value


def _tool_results_include_verified_workstyle(tool_calls: list[Any], tool_results: list[Any]) -> bool:
    called = any(
        isinstance(call, dict) and str(call.get("tool") or "") == "tuoguan_submit_person_workstyle_preference"
        for call in tool_calls
    )
    if not called:
        return False
    for result in tool_results:
        if not isinstance(result, dict):
            continue
        data = result.get("data") if isinstance(result.get("data"), dict) else result
        if result.get("ok") is True and isinstance(data, dict) and data.get("writeback_verified") is True:
            return True
        if isinstance(data, dict) and data.get("ok") is True and data.get("writeback_verified") is True:
            return True
    return False


def _looks_like_saved_claim(text: str) -> bool:
    return any(
        term in str(text or "")
        for term in ("已保存", "已经保存", "记住了", "已经记住", "以后按这个来", "以后就按这个来", "我改了", "已经改")
    )


def _record_workstyle_self_correction(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    raw_text: str,
    final_reply: str,
    ledger_id: str,
    message_id: str,
    missed_feedback_save: bool,
    unverified_commitment: bool,
) -> None:
    from .self_evolution import SELF_EVOLUTION_EVENTS_FILE, submit_self_evolution_event
    from .write_guard import authorized_system_write

    parts: list[str] = []
    if missed_feedback_save:
        parts.append("用户明确提出低风险工作方式反馈时，模型没有主动调用偏好保存工具")
    if unverified_commitment:
        parts.append("回复中出现保存/记住承诺但缺少工具写后反查")
    if not parts:
        return
    system_identity = UserIdentity(
        platform="system",
        platform_user_id="xiaoyou_workstyle_guard",
        canonical_user_id="xiaoyou_workstyle_guard",
        person_name="小优工作方式守卫",
        role="system",
        approval_state="approved",
    )
    with authorized_system_write(
        store.data_dir,
        job_name="workstyle_self_correction",
        allowed_files={SELF_EVOLUTION_EVENTS_FILE},
    ):
        submit_self_evolution_event(
            store,
            identity=system_identity,
            operation_id=f"{message_id or ledger_id or uuid.uuid4().hex}:workstyle_self_correction",
            candidate_type="self_correction",
            summary="；".join(parts) + "；下次必须先保存并写后反查，再说已保存或以后按此执行。",
            evidence=[{
                "source": "reply_ledger",
                "ledger_id": ledger_id,
                "message_id": message_id,
                "actor_user_id": identity.canonical_user_id,
                "actor_role": identity.role,
                "raw_text": _limit_text(raw_text, 240),
                "final_reply": _limit_text(final_reply, 240),
            }],
            risk_level="low",
            status="ready_for_application",
            source_text=raw_text,
            source_message_id=message_id,
            cadence_mode="runtime_reply",
        )


def _render_adaptation_health(
    preferences: list[dict[str, Any]],
    applications: list[dict[str, Any]],
    missed: list[dict[str, Any]],
    unverified: list[dict[str, Any]],
    failed: list[dict[str, Any]],
) -> str:
    lines = [
        "小优工作方式自适应健康度：",
        f"- 已保存偏好 {len(preferences)} 条，应用记录 {len(applications)} 条。",
        f"- 漏保存 {len(missed)} 次，未验证承诺 {len(unverified)} 次，应用失败 {len(failed)} 次。",
    ]
    if preferences:
        latest = preferences[-1]
        lines.append(f"- 最近偏好：{_scope_label(str(latest.get('scope') or 'all_communication'))}/{_dimension_label(str(latest.get('dimension_key') or _dimension_for_row(latest)))}：{_limit_text(latest.get('normalized_rule') or latest.get('preference_text'), 80)}")
    return "\n".join(lines)


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
    if any(term in compact for term in ("先说结论", "结论先行", "正常/异常", "需确认", "今日重点", "完成x件", "明日计划", "异常有/无")):
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
            "汇报",
            "回复",
            "沟通",
            "跟进",
            "先说结论",
            "结论先行",
            "不要废话",
            "废话",
            "段落分明",
            "空行",
            "语气",
            "我喜欢",
            "我不喜欢",
        )
    )


def _summary_lines(preferences: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for item in preferences:
        label = _preference_label(str(item.get("preference_type") or ""))
        dimension = _dimension_label(str(item.get("dimension_key") or _dimension_for_row(item)))
        scope = _scope_label(str(item.get("scope") or "all_communication"))
        rule = _limit_text(item.get("normalized_rule") or item.get("preference_text"), 80)
        if rule:
            lines.append(f"{scope}/{label}/{dimension}：{rule}")
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
    return f"已保存{_scope_label(str(row.get('scope') or 'all_communication'))}/{_dimension_label(str(row.get('dimension_key') or _dimension_for_row(row)))}服务偏好：{_limit_text(row.get('normalized_rule') or row.get('preference_text'), 80)}"


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
        "institution_work": "机构工作讨论",
        "all_communication": "所有沟通",
    }.get(value, value)


def _dimension_label(value: str) -> str:
    return {
        "length": "长短",
        "layout": "排版",
        "structure": "结构",
        "tone": "语气",
        "timing": "时间",
        "detail": "细节",
        "avoidance": "禁忌",
        "followup_method": "跟进方式",
        "interaction_pacing": "互动节奏",
        "other": "其他",
    }.get(value, value or "其他")


def _role_label(value: str) -> str:
    return {"boss": "老板", "manager": "店长", "teacher": "老师", "staff": "员工"}.get(str(value or ""), str(value or "员工"))
