"""Controlled business learning loop for Youyi Hermes.

This module is intentionally conservative: it can record incidents, propose
candidate lessons, generate replay cases, and let the boss approve/reject
lessons. It must not modify runtime allowlists, code, permissions, student
data, tasks, H5, salary, parent messaging, or printing.
"""

from __future__ import annotations

import json
import hashlib
import functools
import re
import shutil
import tempfile
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .store import TuoguanStore
from .tenant_context import current_tenant_id


INCIDENTS_FILE = "learning_incidents.jsonl"
CANDIDATES_FILE = "learning_candidates.jsonl"
APPROVED_LESSONS_FILE = "approved_business_lessons.json"
CORRECTIONS_FILE = "correction_events.jsonl"
REPLAY_CASES_FILE = "replay_cases.jsonl"
DAILY_REPORT_MD = "learning_daily_report.md"
DAILY_REPORT_JSON = "learning_daily_report.json"
STATE_FILE = "learning_loop_state.json"

REPLAY_PROTECTED_FILES = (
    "students.json",
    "records.json",
    "tasks.json",
    "task_closure_events.json",
    "trial_leads.json",
    "point_events.json",
    "summer_points.json",
    "notification_outbox.json",
    "dashboard_cache.json",
)
_LEARNING_LOCK = threading.RLock()


def _serialized(function):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        with _LEARNING_LOCK:
            return function(*args, **kwargs)
    return wrapped

LEARNING_QUERY_PATTERNS = (
    "最近系统学到了什么",
    "系统学到了什么",
    "有哪些问题反复出现",
    "有哪些待确认经验",
    "待确认经验",
    "查看第",
    "确认第",
    "拒绝第",
    "停用经验",
    "恢复经验",
    "查看已生效经验",
    "运行最近问题回放测试",
    "最近问题回放",
)

USER_FEEDBACK_TERMS = ("还是不行", "不对", "又错了", "不正常", "怎么又", "没反应", "没有反馈")
WRITE_TOOLS = {
    "tuoguan_record_student",
    "tuoguan_record_summer_lesson",
    "tuoguan_create_task",
    "tuoguan_update_task",
    "tuoguan_report_safety_event",
    "tuoguan_register_student",
    "tuoguan_register_summer_student",
    "tuoguan_create_trial_lead",
    "tuoguan_change_summer_points",
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _append_jsonl(store: TuoguanStore, filename: str, payload: dict[str, Any]) -> None:
    path = store.data_dir / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    with store._process_write_lock(path):
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")


def _read_jsonl(path: Path, limit: int = 1000) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _read_candidates(store: TuoguanStore) -> list[dict[str, Any]]:
    return _read_jsonl(store.data_dir / CANDIDATES_FILE, limit=2000)


def _read_lessons(store: TuoguanStore) -> list[dict[str, Any]]:
    payload = store.read_json(APPROVED_LESSONS_FILE, {})
    if isinstance(payload, dict):
        payload = payload.get("lessons") or []
    return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []


def _write_lessons(store: TuoguanStore, lessons: list[dict[str, Any]]) -> None:
    store.write_json(APPROVED_LESSONS_FILE, {"schema_version": 1, "lessons": lessons})


def _write_jsonl(store: TuoguanStore, filename: str, rows: list[dict[str, Any]]) -> None:
    path = store.data_dir / filename
    with store._process_write_lock(path):
        path.write_text(
            "\n".join(json.dumps(item, ensure_ascii=False, separators=(",", ":")) for item in rows)
            + ("\n" if rows else ""),
            encoding="utf-8",
        )


def _business_hashes(data_dir: Path) -> dict[str, str]:
    return {
        name: hashlib.sha256((data_dir / name).read_bytes()).hexdigest()
        for name in REPLAY_PROTECTED_FILES
        if (data_dir / name).exists()
    }


def _set_lesson_active(store: TuoguanStore, lesson_id: str, active: bool, reviewer: str) -> bool:
    lessons = _read_lessons(store)
    changed = False
    for lesson in lessons:
        if str(lesson.get("lesson_id") or "") != lesson_id:
            continue
        lesson["status"] = "active" if active else "disabled"
        lesson["runtime_injection_allowed"] = bool(active and lesson.get("replay_status") == "passed")
        lesson["last_changed_by"] = reviewer
        lesson["last_changed_at"] = _now()
        changed = True
        break
    if changed:
        _write_lessons(store, lessons)
    return changed


def _format_active_lessons(store: TuoguanStore) -> str:
    lessons = [item for item in _read_lessons(store) if item.get("status") == "active"]
    if not lessons:
        return "当前没有已生效的业务经验。"
    lines = [f"当前有 {len(lessons)} 条已生效经验："]
    lines.extend(f"- {item.get('lesson_id')}｜{item.get('title') or '未命名'}" for item in lessons[:20])
    return "\n".join(lines)


def _candidate_by_display_index(store: TuoguanStore, index: int) -> dict[str, Any] | None:
    pending = [item for item in _read_candidates(store) if item.get("status") == "pending_review"]
    if index < 1 or index > len(pending):
        return None
    return pending[index - 1]


def _seen_id(store: TuoguanStore, collection: str, key: str) -> bool:
    state = store.read_json(STATE_FILE, {})
    if not isinstance(state, dict):
        return False
    seen = state.get(collection)
    return isinstance(seen, list) and key in set(str(item) for item in seen)


def _mark_seen(store: TuoguanStore, collection: str, key: str) -> None:
    state = store.read_json(STATE_FILE, {})
    if not isinstance(state, dict):
        state = {}
    seen = [str(item) for item in state.get(collection, []) if str(item)]
    if key not in seen:
        seen.append(key)
    state[collection] = seen[-3000:]
    state["updated_at"] = _now()
    store.write_json(STATE_FILE, state)


def _signals_from_ledger(item: dict[str, Any]) -> list[str]:
    signals: list[str] = []
    raw = str(item.get("raw_text") or "")
    if item.get("model_intent") == "unclassified_message" and (
        item.get("blocked_tool_calls") or item.get("tool_calls")
    ):
        signals.append("unclassified_business_message")
    if item.get("blocked_tool_calls"):
        signals.append("tool_blocked")
    if item.get("legacy_handler_intercepted"):
        signals.append("legacy_handler_intercepted")
    if item.get("writeback_verified") is False and any(
        str(call.get("tool") or "") in WRITE_TOOLS for call in item.get("tool_calls") or []
    ):
        signals.append("writeback_failed")
    if item.get("render_verified") is False:
        signals.append("render_failed")
    if item.get("reply_owner") == "system_technical" or (
        item.get("model_reply_applied") is False and item.get("model_reply_error")
    ):
        signals.append("model_reply_failed")
    if item.get("guard_result") in {"failed_safe", "critical_write_violation"}:
        signals.append(str(item.get("guard_result")))
    if any(term in raw for term in USER_FEEDBACK_TERMS):
        signals.append("user_negative_feedback")
    return sorted(set(signals))


def _candidate_for_incident(incident: dict[str, Any]) -> dict[str, Any]:
    signals = incident.get("signals") or []
    raw = str(incident.get("raw_text") or "")
    if "tool_blocked" in signals:
        title = "模型工具选择需要边界复盘"
        content = "遇到类似用户原话时，应复盘模型理解、上下文和对象是否清楚；工具只能在权限和事实校验通过后执行，不能把内部拦截机制暴露给用户。"
        ctype = "tool_constraint"
    elif "unclassified_business_message" in signals:
        title = "模型未稳定理解业务原话"
        content = "这类原话疑似业务请求，应沉淀为模型理解经验和回放样本：下次先理解用户真实意图、对象和范围；不通过新增关键词或固定路由抢在 Agent 前面决定业务路径。"
        ctype = "business_understanding"
    elif "writeback_failed" in signals:
        title = "写入反查失败"
        content = "写操作必须以工具返回 writeback_verified=true 为成功条件；反查失败时不得回复成功，应提示异常并保留审计。"
        ctype = "runtime_rule"
    elif "render_failed" in signals:
        title = "确定性渲染失败"
        content = "实时查询、列表、统计、日报、积分榜必须使用工具 rendered_text；render_verified=false 时不得输出业务结论。"
        ctype = "runtime_rule"
    elif "model_reply_failed" in signals:
        title = "模型最终回复结构失败"
        content = "模型最终回复未通过结构或事实校验时，只能安全技术回退并进入回放；不得交给旧 Handler 生成业务结论。"
        ctype = "runtime_rule"
    elif "user_negative_feedback" in signals:
        title = "用户反馈系统结果不正确"
        content = "用户指出结果不正确时，应关联最近 reply_ledger，复盘模型意图、工具调用、写入反查和最终回复。"
        ctype = "replay_case"
    else:
        title = "运行时异常需复盘"
        content = "该事件需要人工查看账本、审计和工具结果，决定是否补充模型理解经验、工具边界或回放测试。"
        ctype = "runtime_rule"
    return {
        "candidate_id": f"learn_{uuid.uuid4().hex[:12]}",
        "status": "pending_review",
        "candidate_type": ctype,
        "category": "runtime_learning",
        "title": title,
        "content": content,
        "source_incident_id": incident["incident_id"],
        "raw_text": raw,
        "proposed_triggers": [raw] if raw else [],
        "created_at": _now(),
        "risk_level": "low" if ctype in {"business_understanding", "replay_case"} else "medium",
        "replay_status": "pending",
    }


def _latest_previous_ledger(store: TuoguanStore, ledger_id: str) -> dict[str, Any]:
    rows = _read_jsonl(store.data_dir / "reply_ledger.jsonl", limit=500)
    previous: dict[str, Any] = {}
    for item in rows:
        if str(item.get("ledger_id") or "") == ledger_id:
            break
        previous = item
    return previous


def _replay_case_for_incident(incident: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": f"replay_{uuid.uuid4().hex[:12]}",
        "status": "pending_review",
        "source_incident_id": incident["incident_id"],
        "raw_text": incident.get("raw_text", ""),
        "role": incident.get("role", ""),
        "user_id": incident.get("user_id", ""),
        "expected_notes": "根据候选经验确认后补充期望理解、对象、权限、工具边界和自然回复标准。",
        "created_at": _now(),
    }


@_serialized
def record_learning_from_ledger(store: TuoguanStore, ledger_item: dict[str, Any]) -> dict[str, Any] | None:
    """Create incident/candidate/replay rows for high-value runtime failures."""

    ledger_id = str(ledger_item.get("ledger_id") or "")
    if not ledger_id or _seen_id(store, "incident_ledger_ids", ledger_id):
        return None
    signals = _signals_from_ledger(ledger_item)
    if not signals:
        return None
    incident = {
        "incident_id": f"incident_{uuid.uuid4().hex[:12]}",
        "ledger_id": ledger_id,
        "message_id": str(ledger_item.get("message_id") or ""),
        "user_id": str(ledger_item.get("user_id") or ""),
        "role": str(ledger_item.get("role") or ""),
        "raw_text": str(ledger_item.get("raw_text") or ""),
        "model_intent": str(ledger_item.get("model_intent") or ""),
        "selected_capability_card": str(ledger_item.get("selected_capability_card") or ""),
        "used_manual_cards": list(ledger_item.get("used_manual_cards") or []),
        "tool_calls": deepcopy(ledger_item.get("tool_calls") or []),
        "blocked_tool_calls": deepcopy(ledger_item.get("blocked_tool_calls") or []),
        "guard_result": str(ledger_item.get("guard_result") or ""),
        "writeback_verified": ledger_item.get("writeback_verified"),
        "render_verified": ledger_item.get("render_verified"),
        "legacy_handler_intercepted": bool(ledger_item.get("legacy_handler_intercepted")),
        "signals": signals,
        "status": "open",
        "created_at": _now(),
    }
    _append_jsonl(store, INCIDENTS_FILE, incident)
    if "user_negative_feedback" in signals:
        previous = _latest_previous_ledger(store, ledger_id)
        _append_jsonl(store, CORRECTIONS_FILE, {
            "correction_id": f"correction_{uuid.uuid4().hex[:12]}",
            "tenant_id": current_tenant_id(),
            "actor_user_id": str(ledger_item.get("user_id") or ""),
            "actor_role": str(ledger_item.get("role") or ""),
            "original_role": str(previous.get("role") or ledger_item.get("role") or ""),
            "source_ledger_id": str(previous.get("ledger_id") or ledger_id),
            "correction_message_ledger_id": ledger_id,
            "original_text": str(previous.get("raw_text") or ""),
            "wrong_plan": deepcopy(previous.get("business_plan") or {}),
            "wrong_result": deepcopy(previous.get("tool_results") or []),
            "user_correction": str(ledger_item.get("raw_text") or ""),
            "corrected_intent": "",
            "corrected_object": {},
            "lesson_type": "intent",
            "risk_level": "low",
            "status": "pending_replay",
            "created_at": _now(),
        })
    candidate = _candidate_for_incident(incident)
    replay_case = _replay_case_for_incident(incident)
    _append_jsonl(store, CANDIDATES_FILE, candidate)
    _append_jsonl(store, REPLAY_CASES_FILE, replay_case)
    _mark_seen(store, "incident_ledger_ids", ledger_id)
    return {"incident": incident, "candidate": candidate, "replay_case": replay_case}


def _recent_rows(store: TuoguanStore, filename: str, hours: int = 24, limit: int = 1000) -> list[dict[str, Any]]:
    cutoff = datetime.now() - timedelta(hours=hours)
    rows = []
    for item in _read_jsonl(store.data_dir / filename, limit=limit):
        ts = str(item.get("created_at") or item.get("completed_at") or "")
        try:
            when = datetime.fromisoformat(ts)
        except ValueError:
            when = datetime.now()
        if when >= cutoff:
            rows.append(item)
    return rows


def generate_daily_learning_report(store: TuoguanStore, hours: int = 24, *, force: bool = True) -> dict[str, Any]:
    existing = store.read_json(DAILY_REPORT_JSON, {})
    if (
        not force
        and isinstance(existing, dict)
        and str(existing.get("generated_at") or "")[:10] == _now()[:10]
    ):
        return existing
    incidents = _recent_rows(store, INCIDENTS_FILE, hours=hours)
    candidates = _read_candidates(store)
    pending = [item for item in candidates if item.get("status") == "pending_review"]
    approved = _read_lessons(store)
    signal_counts: dict[str, int] = {}
    for incident in incidents:
        for signal in incident.get("signals") or []:
            signal_counts[signal] = signal_counts.get(signal, 0) + 1
    report = {
        "generated_at": _now(),
        "window_hours": hours,
        "incident_count": len(incidents),
        "pending_candidate_count": len(pending),
        "approved_lesson_count": len(approved),
        "signal_counts": signal_counts,
        "latest_incidents": incidents[-10:],
        "pending_candidates": pending[:20],
    }
    store.write_json(DAILY_REPORT_JSON, report)
    lines = [
        "# Hermes 业务自学习日报",
        "",
        f"生成时间：{report['generated_at']}",
        f"最近 {hours} 小时 incident：{len(incidents)} 条",
        f"待确认经验：{len(pending)} 条",
        f"已确认经验：{len(approved)} 条",
        "",
        "## 问题类型",
    ]
    if signal_counts:
        lines.extend(f"- {key}: {value}" for key, value in sorted(signal_counts.items()))
    else:
        lines.append("- 暂无高价值异常。")
    lines.append("")
    lines.append("## 待确认经验")
    if pending:
        for idx, item in enumerate(pending[:10], 1):
            lines.append(f"{idx}. {item.get('title')}（{item.get('candidate_type')}）")
    else:
        lines.append("- 暂无。")
    (store.data_dir / DAILY_REPORT_MD).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def _format_candidates(store: TuoguanStore) -> str:
    pending = [item for item in _read_candidates(store) if item.get("status") == "pending_review"]
    if not pending:
        return "当前没有待确认经验。"
    lines = [f"当前有 {len(pending)} 条待确认经验："]
    for idx, item in enumerate(pending[:10], 1):
        lines.append(f"{idx}. {item.get('title')}｜类型：{item.get('candidate_type')}｜来源：{item.get('source_incident_id')}")
    lines.append("你可以回复“查看第1条经验”“确认第1条经验”或“拒绝第1条经验”。")
    return "\n".join(lines)


def _format_candidate_detail(store: TuoguanStore, index: int) -> str:
    item = _candidate_by_display_index(store, index)
    if not item:
        return "没有找到这条待确认经验。"
    return "\n".join([
        f"【第{index}条待确认经验】",
        f"标题：{item.get('title')}",
        f"类型：{item.get('candidate_type')}",
        f"原话：{item.get('raw_text') or '无'}",
        "建议：",
        str(item.get("content") or ""),
        "",
        "确认后只进入经验库和回放集，不会自动改代码或扩白名单。",
    ])


def _review_candidate(store: TuoguanStore, index: int, decision: str, reviewer: str) -> str:
    candidates = _read_candidates(store)
    pending = [item for item in candidates if item.get("status") == "pending_review"]
    if index < 1 or index > len(pending):
        return "没有找到这条待确认经验。"
    target_id = pending[index - 1]["candidate_id"]
    updated = None
    for item in candidates:
        if item.get("candidate_id") == target_id:
            item["status"] = "approved" if decision == "approve" else "rejected"
            item["reviewed_by"] = reviewer
            item["reviewed_at"] = _now()
            updated = item
            break
    _write_jsonl(store, CANDIDATES_FILE, candidates)
    if decision == "approve" and updated:
        lessons = _read_lessons(store)
        lesson = {
            "lesson_id": f"lesson_{uuid.uuid4().hex[:12]}",
            "source_candidate_id": target_id,
            "title": updated.get("title"),
            "content": updated.get("content"),
            "candidate_type": updated.get("candidate_type"),
            "proposed_triggers": updated.get("proposed_triggers") or [],
            "approved_by": reviewer,
            "approved_at": _now(),
            "status": "approved",
            "risk_level": str(updated.get("risk_level") or "medium"),
            "replay_status": str(updated.get("replay_status") or "pending"),
            "runtime_injection_allowed": False,
            "allowlist_expanded": False,
        }
        for key in ("roles", "match_all", "match_any", "replay_expectation"):
            if updated.get(key):
                lesson[key] = deepcopy(updated[key])
        lessons.append(lesson)
        _write_lessons(store, lessons)
        return "已确认这条经验。它已进入本机构经验库，但不会自动改代码、不会扩白名单。"
    return "已拒绝这条经验，运行时规则不受影响。"


def _plan_matches_expectation(plan: dict[str, Any], expected: dict[str, Any]) -> tuple[bool, list[str]]:
    mismatches: list[str] = []
    if expected.get("intent") and plan.get("intent") != expected["intent"]:
        mismatches.append("intent")
    if "clarification_needed" in expected and bool(plan.get("clarification_needed")) != bool(expected["clarification_needed"]):
        mismatches.append("clarification_needed")
    expected_tools = {str(value) for value in expected.get("tools") or []}
    actual_tools = {
        str(call.get("tool") or call.get("tool_name") or call.get("name") or "")
        for call in plan.get("proposed_tool_calls") or []
    }
    if "tools" in expected and actual_tools != expected_tools:
        mismatches.append("tools")
    expected_targets = {str(value) for value in expected.get("target_ids") or []}
    actual_targets = {str(value) for value in plan.get("business_object_candidates") or []}
    if "target_ids" in expected and not expected_targets.issubset(actual_targets):
        mismatches.append("target_ids")
    return not mismatches, mismatches


@_serialized
def run_learning_replay_check(store: TuoguanStore, *, adapter: Any = None) -> dict[str, Any]:
    """Replay approved lessons through the real planner without business writes."""

    production_before = _business_hashes(store.data_dir)
    tmp_root = Path(tempfile.mkdtemp(prefix="youyi_learning_replay_"))
    tmp_data = tmp_root / "tuoguan-data"
    shutil.copytree(store.data_dir, tmp_data, ignore=shutil.ignore_patterns("backup", "*.lock"))
    cases = _read_jsonl(store.data_dir / REPLAY_CASES_FILE, limit=500)
    pending = [item for item in cases if item.get("status") == "pending_review"]
    candidates = _read_candidates(store)
    lessons = _read_lessons(store)
    case_by_incident = {
        str(item.get("source_incident_id") or ""): item
        for item in cases
        if item.get("source_incident_id")
    }
    checked: list[dict[str, Any]] = []
    for candidate in [item for item in candidates if item.get("status") == "approved"]:
        text = " ".join(str(candidate.get(key) or "") for key in ("title", "content", "candidate_type"))
        forbidden = str(candidate.get("risk_level") or "medium") != "low" or any(
            term in text for term in ("扩白名单", "修改权限", "自动发家长", "删除数据", "工资", "直接写入")
        )
        source_case = case_by_incident.get(str(candidate.get("source_incident_id") or ""), {})
        expected = candidate.get("replay_expectation") or source_case.get("replay_expectation") or {}
        result: dict[str, Any] = {"candidate_id": candidate.get("candidate_id")}
        if forbidden:
            result["replay_status"] = candidate["replay_status"] = "blocked_high_risk"
        elif not isinstance(expected, dict) or not expected.get("intent"):
            result["replay_status"] = candidate["replay_status"] = "needs_expectation"
        else:
            from .ai_operator import CAPABILITIES, create_production_plan

            tmp_lessons = deepcopy(lessons)
            for lesson in tmp_lessons:
                if lesson.get("source_candidate_id") == candidate.get("candidate_id"):
                    lesson["status"] = "active"
                    lesson["runtime_injection_allowed"] = True
            (tmp_data / APPROVED_LESSONS_FILE).write_text(
                json.dumps({"schema_version": 1, "lessons": tmp_lessons}, ensure_ascii=False),
                encoding="utf-8",
            )
            config_path = tmp_root / "ai_operator_config.json"
            config_path.write_text(json.dumps({
                "enabled": True,
                "mode": "production",
                "tenant_id": str(source_case.get("tenant_id") or current_tenant_id()),
                "production_data_dir": str(tmp_data),
                "allow_business_write": False,
                "allow_runtime_reply_change": False,
                "model_config_path": "/opt/hermes-youyi/config/config.yaml",
                "ai_operator": {"business_plan": True, "model_reply": False, "canary_actor_ids": []},
            }, ensure_ascii=False), encoding="utf-8")
            planned = create_production_plan(
                data_dir=tmp_data,
                tenant_id=str(source_case.get("tenant_id") or current_tenant_id()),
                actor_user_id=str(source_case.get("user_id") or "replay_actor"),
                actor_role=str(source_case.get("role") or "teacher"),
                source_message_id=str(source_case.get("case_id") or candidate.get("candidate_id") or "replay"),
                raw_text=str(source_case.get("raw_text") or candidate.get("raw_text") or ""),
                allowed_capabilities=list(expected.get("allowed_capabilities") or CAPABILITIES),
                config_path=config_path,
                adapter=adapter,
            )
            plan = planned.get("business_plan") if isinstance(planned.get("business_plan"), dict) else {}
            matched, mismatches = _plan_matches_expectation(plan, expected)
            result.update({
                "replay_status": "passed" if matched and not planned.get("error") else "failed",
                "mismatches": mismatches,
                "planner_error": str(planned.get("error") or ""),
                "actual_intent": str(plan.get("intent") or ""),
                "retrieved_lesson_ids": list(plan.get("retrieved_lesson_ids") or []),
            })
            candidate["replay_status"] = result["replay_status"]
            if result["replay_status"] == "passed":
                source_case["status"] = "passed"
                source_case["replayed_at"] = _now()
        checked.append(result)
        if candidate["replay_status"] == "passed":
            for lesson in lessons:
                if lesson.get("source_candidate_id") == candidate.get("candidate_id"):
                    lesson["replay_status"] = "passed"
                    lesson["status"] = "active"
                    lesson["runtime_injection_allowed"] = True
                    break
    _write_jsonl(store, CANDIDATES_FILE, candidates)
    _write_jsonl(store, REPLAY_CASES_FILE, cases)
    _write_lessons(store, lessons)
    production_after = _business_hashes(store.data_dir)
    report = {
        "generated_at": _now(),
        "mode": "controlled_rule_replay",
        "case_count": len(cases),
        "pending_case_count": len(pending),
        "production_data_modified": production_before != production_after,
        "candidate_results": checked,
        "note": "回放使用生产数据副本调用模型计划层，不调用工具、Repository或生产CommandBus。",
        "sample_cases": pending[:20],
    }
    store.write_json("learning_replay_report.json", report)
    (store.data_dir / "learning_replay_report.md").write_text(
        "# Hermes 学习回放报告\n\n"
        f"生成时间：{report['generated_at']}\n\n"
        f"回放样本总数：{len(cases)}\n\n"
        f"待确认样本：{len(pending)}\n\n"
        "本轮使用生产数据副本盘点，未修改生产数据。\n",
        encoding="utf-8",
    )
    shutil.rmtree(tmp_root, ignore_errors=True)
    return report


@_serialized
def process_pending_corrections(
    store: TuoguanStore,
    *,
    adapter: Any = None,
    limit: int = 3,
    config_path: Path = Path("/opt/hermes-youyi/config/ai_operator_config.json"),
) -> dict[str, Any]:
    """Turn explicit corrections into replay-gated lessons without executing tools."""

    from .ai_operator import CAPABILITIES, create_production_plan

    corrections = _read_jsonl(store.data_dir / CORRECTIONS_FILE, limit=2000)
    pending = [row for row in corrections if row.get("status") == "pending_replay"][:limit]
    if not pending:
        return {"processed": 0, "activated": 0, "results": []}
    candidates = _read_candidates(store)
    cases = _read_jsonl(store.data_dir / REPLAY_CASES_FILE, limit=2000)
    lessons = _read_lessons(store)
    existing_corrections = {str(row.get("source_correction_id") or "") for row in candidates}
    results: list[dict[str, Any]] = []
    approved_created = False
    for correction in pending:
        correction_id = str(correction.get("correction_id") or "")
        original_text = str(correction.get("original_text") or "").strip()
        user_correction = str(correction.get("user_correction") or "").strip()
        actor_role = str(correction.get("actor_role") or correction.get("original_role") or "")
        actor_user_id = str(correction.get("actor_user_id") or "")
        if not correction_id or correction_id in existing_corrections:
            correction["status"] = "candidate_exists"
            continue
        if not original_text or not user_correction:
            correction["status"] = "needs_more_detail"
            results.append({"correction_id": correction_id, "status": correction["status"]})
            continue
        planned = create_production_plan(
            data_dir=store.data_dir,
            tenant_id=str(correction.get("tenant_id") or current_tenant_id()),
            actor_user_id=actor_user_id or "correction_actor",
            actor_role=actor_role or "teacher",
            source_message_id=correction_id,
            raw_text=f"上一条原话：{original_text}\n用户明确纠正：{user_correction}",
            allowed_capabilities=list(CAPABILITIES),
            config_path=config_path,
            adapter=adapter,
        )
        plan = planned.get("business_plan") if isinstance(planned.get("business_plan"), dict) else {}
        intent = str(plan.get("intent") or "")
        if planned.get("error") or not intent or plan.get("clarification_needed") or float(plan.get("confidence") or 0) < 0.75:
            correction["status"] = "needs_more_detail"
            correction["planner_reason"] = str(planned.get("error") or "uncertain_correction")
            results.append({"correction_id": correction_id, "status": correction["status"]})
            continue
        spec = CAPABILITIES.get(intent) or {}
        high_risk = bool(spec.get("write")) or intent == "safety_workflow_coach" or str(correction.get("lesson_type") or "") == "boundary"
        risk_level = "high" if high_risk else "low"
        auto_approved = actor_role == "boss" and not high_risk
        tools = [
            str(call.get("tool") or call.get("tool_name") or call.get("name") or "")
            for call in plan.get("proposed_tool_calls") or []
            if str(call.get("tool") or call.get("tool_name") or call.get("name") or "")
        ]
        targets = [str(value) for value in plan.get("business_object_candidates") or []]
        candidate_id = f"learn_{uuid.uuid4().hex[:12]}"
        incident_id = f"correction_incident_{correction_id}"
        content = (
            f"当用户表达“{original_text}”时，结合其明确纠正，应理解为 {intent}；"
            f"只保留用户明确给出的对象和事实，不得回退到原错误计划。"
        )
        expectation = {
            "intent": intent,
            "tools": tools,
            "target_ids": targets,
            "clarification_needed": False,
            "allowed_capabilities": list(CAPABILITIES),
        }
        candidate = {
            "candidate_id": candidate_id,
            "status": "approved" if auto_approved else "pending_review",
            "candidate_type": "intent_pattern",
            "category": "runtime_learning",
            "title": "用户纠正形成的业务理解经验",
            "content": content,
            "source_incident_id": incident_id,
            "source_correction_id": correction_id,
            "raw_text": original_text,
            "proposed_triggers": [original_text],
            "roles": [actor_role] if actor_role else [],
            "created_at": _now(),
            "risk_level": risk_level,
            "replay_status": "pending",
            "replay_expectation": expectation,
        }
        case = {
            "case_id": f"replay_{uuid.uuid4().hex[:12]}",
            "status": "pending_review",
            "source_incident_id": incident_id,
            "source_correction_id": correction_id,
            "tenant_id": str(correction.get("tenant_id") or current_tenant_id()),
            "user_id": actor_user_id or "correction_actor",
            "role": actor_role or "teacher",
            "raw_text": original_text,
            "replay_expectation": expectation,
            "created_at": _now(),
        }
        candidates.append(candidate)
        cases.append(case)
        if auto_approved:
            lessons.append({
                "lesson_id": f"lesson_{uuid.uuid4().hex[:12]}",
                "source_candidate_id": candidate_id,
                "title": candidate["title"],
                "content": content,
                "candidate_type": candidate["candidate_type"],
                "proposed_triggers": [original_text],
                "roles": candidate["roles"],
                "approved_by": actor_user_id,
                "approved_at": _now(),
                "status": "approved",
                "risk_level": risk_level,
                "replay_status": "pending",
                "runtime_injection_allowed": False,
                "allowlist_expanded": False,
                "replay_expectation": expectation,
            })
            approved_created = True
        correction["status"] = "approved_pending_replay" if auto_approved else "pending_review"
        correction["corrected_intent"] = intent
        correction["corrected_object"] = {"candidate_ids": targets}
        correction["risk_level"] = risk_level
        correction["candidate_id"] = candidate_id
        results.append({"correction_id": correction_id, "status": correction["status"], "risk_level": risk_level})
    _write_jsonl(store, CORRECTIONS_FILE, corrections)
    _write_jsonl(store, CANDIDATES_FILE, candidates)
    _write_jsonl(store, REPLAY_CASES_FILE, cases)
    _write_lessons(store, lessons)
    replay = run_learning_replay_check(store, adapter=adapter) if approved_created else None
    by_candidate = {
        str(row.get("candidate_id") or ""): row
        for row in (replay or {}).get("candidate_results") or []
    }
    for correction in corrections:
        candidate_result = by_candidate.get(str(correction.get("candidate_id") or ""))
        if not candidate_result:
            continue
        correction["status"] = "active" if candidate_result.get("replay_status") == "passed" else "replay_failed"
        correction["replay_result"] = candidate_result
    _write_jsonl(store, CORRECTIONS_FILE, corrections)
    return {
        "processed": len(results),
        "activated": sum(row.get("status") == "active" for row in corrections),
        "results": results,
        "replay": replay,
    }


def handle_learning_message(store: TuoguanStore, *, user_id: str, role: str, raw_text: str) -> str | None:
    compact = "".join(str(raw_text or "").split())
    if not any(pattern in compact for pattern in LEARNING_QUERY_PATTERNS):
        return None
    if role != "boss":
        return "这类系统学习经验只能由机构负责人查看或确认。"
    match = re.search(r"第(\d+)条", compact)
    index = int(match.group(1)) if match else 0
    lesson_match = re.search(r"(?:停用经验|恢复经验)(lesson_[A-Za-z0-9_]+)", compact)
    if "查看已生效经验" in compact:
        return _format_active_lessons(store)
    if lesson_match:
        active = "恢复经验" in compact
        changed = _set_lesson_active(store, lesson_match.group(1), active, user_id)
        if not changed:
            return "没有找到这条经验。"
        return "已恢复这条经验。" if active else "已停用这条经验，后续消息不会再召回它。"
    if "确认第" in compact:
        return _review_candidate(store, index, "approve", user_id)
    if "拒绝第" in compact:
        return _review_candidate(store, index, "reject", user_id)
    if "查看第" in compact:
        return _format_candidate_detail(store, index)
    if "运行最近问题回放" in compact or "最近问题回放" in compact:
        report = run_learning_replay_check(store)
        return f"已完成最近问题回放样本盘点：共 {report['case_count']} 条，待确认 {report['pending_case_count']} 条。未修改生产数据。"
    if "待确认经验" in compact:
        return _format_candidates(store)
    report = generate_daily_learning_report(store)
    signal_text = "、".join(f"{k}:{v}" for k, v in sorted(report["signal_counts"].items())) or "暂无高价值异常"
    return "\n".join([
        "【Hermes 业务自学习摘要】",
        f"最近 {report['window_hours']} 小时发现问题事件：{report['incident_count']} 条",
        f"待确认经验：{report['pending_candidate_count']} 条",
        f"已确认经验：{report['approved_lesson_count']} 条",
        f"主要类型：{signal_text}",
        "你可以说“有哪些待确认经验”查看，或“运行最近问题回放测试”。",
    ])
