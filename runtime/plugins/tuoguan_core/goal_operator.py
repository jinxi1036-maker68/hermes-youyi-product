
"""Goal-oriented autonomous operator helpers for Youyi.

This module exposes business capabilities for the Hermes Agent. It does not
classify inbound text before the model. The model decides whether these tools are
useful; the tools only read facts, enforce permissions, persist confirmed goals,
and return verifiable business results.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import re
import uuid
from typing import Any

from .models import UserIdentity
from .store import TuoguanStore
from .tenant_context import current_tenant_id
from .tasks import CLOSED_TASK_STATUSES
from .responsibility_resolver import (
    REGULAR_PROGRAM_ID,
    regular_manager_names,
    resolve_student_responsibility,
    summarize_responsibility_coverage,
)

GOAL_FILE = "goal_operator_goals.json"
EVENT_FILE = "goal_operator_events.jsonl"
GOAL_TYPE_PARENT_COMMUNICATION = "parent_communication_coverage"
PROGRAM_SUMMER = "summer_2026"
PROGRAM_REGULAR = REGULAR_PROGRAM_ID


_CLOSED_TASK_STATUSES = CLOSED_TASK_STATUSES


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def stable_goal_id(*parts: str) -> str:
    raw = ":".join(str(part or "") for part in parts)
    return f"goal_{uuid.uuid5(uuid.NAMESPACE_URL, raw).hex[:16]}"


def append_jsonl(store: TuoguanStore, name: str, row: dict[str, Any]) -> None:
    from .write_guard import assert_business_write_allowed
    assert_business_write_allowed(store.data_dir, name)
    path = store.path_for(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        import json
        fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def _records(store: TuoguanStore) -> list[dict[str, Any]]:
    data = store.read_json("records.json", [])
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _tasks(store: TuoguanStore) -> list[dict[str, Any]]:
    return [item for item in store.load_tasks() if isinstance(item, dict)]


def _students(store: TuoguanStore) -> dict[str, dict[str, Any]]:
    data = store.read_json("students.json", {})
    if not isinstance(data, dict):
        return {}
    return {str(name): deepcopy(profile) for name, profile in data.items() if isinstance(profile, dict)}


def _is_active_summer_student(name: str, profile: dict[str, Any], enrollments_by_name: dict[str, list[dict[str, Any]]]) -> bool:
    if str(profile.get("program_id") or "") == PROGRAM_SUMMER:
        return str(profile.get("status") or profile.get("summer_status") or "active") not in {"inactive", "cancelled", "left"}
    for row in profile.get("program_enrollments") or []:
        if isinstance(row, dict) and str(row.get("program_id") or "") == PROGRAM_SUMMER:
            return str(row.get("status") or "active") in {"", "active", "enrolled"}
    for row in enrollments_by_name.get(name, []):
        if str(row.get("program_id") or "") == PROGRAM_SUMMER:
            return str(row.get("status") or "active") in {"", "active", "enrolled"}
    return False


def active_students(store: TuoguanStore, *, program_id: str = "") -> dict[str, dict[str, Any]]:
    students = _students(store)
    enrollments_raw = store.read_json("summer_enrollments.json", [])
    rows = enrollments_raw.values() if isinstance(enrollments_raw, dict) else enrollments_raw
    enrollments_by_name: dict[str, list[dict[str, Any]]] = {}
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict):
                enrollments_by_name.setdefault(str(row.get("student_name") or ""), []).append(row)
                name = str(row.get("student_name") or "")
                if name and name not in students:
                    students[name] = {
                        "student_name": name,
                        "grade": row.get("grade"),
                        "teacher": row.get("teacher") or row.get("teacher_user_id") or "",
                        "program_id": row.get("program_id") or PROGRAM_SUMMER,
                        "status": row.get("status") or "active",
                    }
    if program_id == PROGRAM_SUMMER:
        return {
            name: profile for name, profile in students.items()
            if _is_active_summer_student(name, profile, enrollments_by_name)
        }
    if program_id in {"", PROGRAM_REGULAR, "regular", "tuoguan"}:
        result = {}
        for name, profile in students.items():
            status = str(profile.get("status") or profile.get("student_status") or "active")
            if status in {"inactive", "cancelled", "left", "trial", "test"}:
                continue
            program_ids = {str(profile.get("program_id") or ""), str(profile.get("program") or "")}
            for row in profile.get("program_enrollments") or []:
                if isinstance(row, dict):
                    program_ids.add(str(row.get("program_id") or row.get("program") or ""))
            campus = str(profile.get("campus_id") or "")
            is_summer_only = PROGRAM_SUMMER in program_ids and PROGRAM_REGULAR not in program_ids and campus != "main"
            if not is_summer_only and (campus == "main" or PROGRAM_REGULAR in program_ids or str(profile.get("teacher") or "")):
                result[name] = profile
        return result
    return {
        name: profile for name, profile in students.items()
        if str(profile.get("status") or profile.get("student_status") or "active") not in {"inactive", "cancelled", "left"}
    }

def teacher_label(store: TuoguanStore, user_id: str) -> str:
    maps = store.read_json("teacher_wecom_map.json", {})
    if isinstance(maps, dict):
        for name, mapped in maps.items():
            if str(mapped) == str(user_id):
                return str(name)
    staff = store.read_json("staff.json", {})
    if isinstance(staff, dict):
        item = staff.get(str(user_id))
        if isinstance(item, dict):
            return str(item.get("name") or item.get("display_name") or user_id)
    return str(user_id or "未分配老师")


def _teacher_for_student(profile: dict[str, Any]) -> str:
    return str(profile.get("teacher_user_id") or profile.get("teacher") or profile.get("advisor_user_id") or profile.get("operator_user_id") or "").strip()


def _manager_question_packet(store: TuoguanStore, unresolved_count: int) -> dict[str, Any]:
    manager_names = [name for name in regular_manager_names(store) if name]
    ask_names = manager_names or ["老板"]
    if unresolved_count:
        question = (
            "请先确认正式托管孩子的服务类型和主责老师：只午托、只晚托、全托分别由谁负责家长沟通。"
            "尤其是缺少午托/晚托/全托或主责老师字段的孩子，不能凭空分配。"
        )
        next_action = "collect_responsibility_facts"
    else:
        question = "责任关系已基本明确，可以从第一批重点学生开始收集老师家长沟通反馈。"
        next_action = "start_goal_followup"
    return {
        "next_best_action": next_action,
        "ask_role": "regular_tuoguan_manager" if manager_names else "boss",
        "ask_names": ask_names,
        "question": question,
        "why": "责任关系决定该找哪位老师推进家长沟通；Hermes 不能替机构猜。",
        "auto_sent": False,
        "auto_parent_message": False,
        "fact_types_to_collect": [
            "student_service_type",
            "student_responsible_teacher",
            "teacher_operating_role",
        ],
    }


def _record_student_name(row: dict[str, Any]) -> str:
    return str(row.get("student_name") or row.get("student") or row.get("name") or "").strip()


def _record_text(row: dict[str, Any]) -> str:
    return str(row.get("content") or row.get("source_text") or row.get("summary") or "").strip()


def _record_created(row: dict[str, Any]) -> str:
    return str(row.get("created_at") or row.get("timestamp") or row.get("time") or row.get("date") or "")


def _looks_like_parent_communication(row: dict[str, Any]) -> bool:
    text = _record_text(row)
    if any(token in text for token in ("家长", "妈妈", "爸爸", "沟通", "反馈", "说明", "告知", "回访", "电话", "微信")):
        return True
    types = row.get("record_types") or row.get("tags") or []
    if isinstance(types, str):
        types = [types]
    return any(str(item) in {"parent_communication", "parent_feedback", "parent_follow_up"} for item in types)


def _recent_record_count(records: list[dict[str, Any]], student_name: str, *, days: int = 14) -> int:
    cutoff = datetime.now().astimezone() - timedelta(days=days)
    count = 0
    for row in records:
        if _record_student_name(row) != student_name:
            continue
        stamp = _record_created(row)
        try:
            when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except Exception:
            continue
        if when.tzinfo is None:
            when = when.astimezone()
        if when >= cutoff:
            count += 1
    return count


def _teacher_workload(store: TuoguanStore, students: dict[str, dict[str, Any]], records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for name, profile in students.items():
        teacher = _teacher_for_student(profile) or "unassigned"
        bucket = buckets.setdefault(teacher, {"teacher_user_id": teacher, "teacher_name": teacher_label(store, teacher), "student_count": 0, "students": []})
        bucket["student_count"] += 1
        bucket["students"].append(name)
    for bucket in buckets.values():
        bucket["students"] = sorted(bucket["students"])
        bucket["recent_record_students"] = sum(1 for student in bucket["students"] if _recent_record_count(records, student) > 0)
    return sorted(buckets.values(), key=lambda item: (-int(item.get("student_count") or 0), str(item.get("teacher_name") or "")))


def review_parent_communication_goal(store: TuoguanStore, *, goal_text: str, program_id: str = PROGRAM_REGULAR) -> dict[str, Any]:
    students = active_students(store, program_id=program_id)
    records = _records(store)
    tasks = _tasks(store)
    communicated: set[str] = set()
    for row in records:
        name = _record_student_name(row)
        if name in students and _looks_like_parent_communication(row):
            communicated.add(name)
    missing = sorted(set(students) - communicated)
    no_recent_records = sorted(name for name in students if _recent_record_count(records, name) == 0)
    workloads = _teacher_workload(store, students, records)
    responsibility_coverage = summarize_responsibility_coverage(store, students, purpose="parent_communication")
    safety_open = [
        item for item in tasks
        if str(item.get("level") or "") == "S" and str(item.get("status") or "") not in _CLOSED_TASK_STATUSES
    ]
    heavy = [item for item in workloads if int(item.get("student_count") or 0) >= 12]
    objections: list[str] = []
    if no_recent_records:
        objections.append(f"有 {len(no_recent_records)} 名学生近14天没有有效记录，直接沟通容易空泛。")
    if heavy:
        objections.append(f"有 {len(heavy)} 位老师负责学生数量较多，建议分批推进。")
    if safety_open:
        objections.append(f"当前有 {len(safety_open)} 个未闭环 S 级安全事项，应优先跟进。")
    if responsibility_coverage["unresolved_count"]:
        objections.append(f"有 {responsibility_coverage['unresolved_count']} 名学生缺少午托/晚托/全托或主责老师字段，不能凭空安排，需要先问店长或老板确认责任。")
    if not objections:
        objections.append("目标方向可执行，但仍建议按缺记录、重点风险、普通覆盖三批推进。")
    unresolved_names = [str(item.get("student_name") or "") for item in responsibility_coverage.get("unresolved_students", [])]
    first_batch = sorted(set(no_recent_records) | {str(item.get("student_name") or "") for item in safety_open if str(item.get("student_name") or "") in students})
    first_batch = [name for name in first_batch if name and name not in set(unresolved_names)]
    if not first_batch:
        first_batch = [name for name in missing if name not in set(unresolved_names)][: min(8, len(missing))]
    plan = [
        {"phase": "责任先确认", "focus": "午托/晚托/全托或主责老师缺失的孩子，先问店长或老板补责任，不直接派给老师", "student_names": unresolved_names},
        {"phase": "第一批", "focus": "在责任明确的孩子里，优先推进近期无记录、存在安全/家长焦虑或信息不足的学生", "student_names": first_batch},
        {"phase": "第二批", "focus": "剩余未沟通学生按已确认主责老师推进", "student_names": [name for name in missing if name not in set(first_batch) and name not in set(unresolved_names)]},
    ]
    question_packet = _manager_question_packet(store, int(responsibility_coverage.get("unresolved_count") or 0))
    decision_support = {
        "review_result_type": "fact_based_goal_review",
        "goal_is_reasonable": True,
        "should_challenge_owner": bool(no_recent_records or safety_open or responsibility_coverage["unresolved_count"] or heavy),
        "challenge_reasons": objections,
        "must_confirm_before_execution": True,
        "execution_readiness": "needs_responsibility_confirmation" if responsibility_coverage["unresolved_count"] else "ready_for_prioritized_followup",
        "missing_fact_strategy": {
            "has_missing_responsibility": bool(responsibility_coverage["unresolved_count"]),
            "ask_role_first": question_packet["ask_role"],
            "ask_names": question_packet["ask_names"],
            "question": question_packet["question"],
            "why": question_packet["why"],
            "fact_types_to_collect": question_packet["fact_types_to_collect"],
            "auto_sent": False,
        },
        "next_best_action": question_packet,
        "business_value": [
            "家长沟通覆盖会沉淀服务证据，提升续费和家长信任。",
            "按责任老师推进可以减少遗漏，也能让店长看到执行缺口。",
            "先补近期记录和重点风险，再沟通，能避免沟通内容空泛。",
        ],
        "model_reply_guidance": "这是工具事实包。Hermes 应根据这些事实自然组织回复，可以赞同、反驳、追问或建议分批；不要把 rendered_text 当成唯一模板。若 next_best_action.auto_sent=false，不得声称已经通知任何人。",
    }
    rendered_lines = [
        "【结论】",
        "目标方向对，但不建议直接平均推进。先补责任关系，再分批沟通。",
        "",
        "【关键数据】",
        f"正式托管学生：{len(students)}名",
        f"已有沟通痕迹：{len(communicated)}名",
        f"还需推进：{len(missing)}名",
        f"涉及老师：{len(workloads)}位",
        f"责任待确认：{responsibility_coverage['unresolved_count']}名",
        "",
        "【卡点】",
    ]
    rendered_lines.extend(f"- {item}" for item in objections[:3])
    rendered_lines.extend(["", "【我建议】"])
    for item in plan:
        names = "、".join(item["student_names"][:6]) if item["student_names"] else "暂无"
        suffix = "等" if len(item["student_names"]) > 6 else ""
        confirm_note = "（需店长/老板确认）" if str(item.get("phase") or "") == "责任先确认" and item.get("student_names") else ""
        rendered_lines.append(f"{item['phase']}：{item['focus']}{confirm_note}。对象：{names}{suffix}")
    rendered_lines.extend(["", "【需要你拍板】", "你认可这个方向后，我再保存目标并按事实推进；我不会自动给家长发消息。"] )
    return {
        "ok": True,
        "goal_type": GOAL_TYPE_PARENT_COMMUNICATION,
        "goal_text": str(goal_text or "").strip(),
        "program_id": program_id,
        "student_count": len(students),
        "communicated_count": len(communicated),
        "missing_count": len(missing),
        "teacher_count": len(workloads),
        "missing_students": missing,
        "no_recent_record_students": no_recent_records,
        "teacher_workloads": workloads,
        "responsibility_coverage": responsibility_coverage,
        "open_safety_task_count": len(safety_open),
        "objections": objections,
        "recommended_plan": plan,
        "decision_support": decision_support,
        "risk_boundaries": {
            "auto_parent_message": False,
            "payroll_change": False,
            "permission_change": False,
            "delete_data": False,
        },
        "rendered_text": "\n".join(rendered_lines),
        "render_verified": True,
    }


def load_goals(store: TuoguanStore) -> dict[str, Any]:
    data = store.read_json(GOAL_FILE, {"goals": []})
    if not isinstance(data, dict):
        return {"goals": []}
    goals = data.get("goals")
    if not isinstance(goals, list):
        data["goals"] = []
    return data


def save_goals(store: TuoguanStore, data: dict[str, Any]) -> None:
    store.write_json(GOAL_FILE, data)


def _goal_execution_plan(review: dict[str, Any], *, goal_id: str, goal_text: str) -> dict[str, Any]:
    recommended_plan = deepcopy(review.get("recommended_plan") or [])
    responsibility = review.get("responsibility_coverage") if isinstance(review.get("responsibility_coverage"), dict) else {}
    unresolved_count = int((responsibility or {}).get("unresolved_count") or 0)
    if unresolved_count:
        phase_key = "responsibility_confirmation"
        phase_name = "阶段1：责任关系确认"
        phase_goal = "先确认午托、晚托、全托和主责老师，避免把目标错派给不该负责的人。"
        next_actions = [
            {
                "action_type": "ask_manager_or_boss_for_responsibility_facts",
                "summary": "向店长或老板确认服务关系缺口；忙碌时段可先整理问题包，不自动打扰全体老师。",
                "requires_model_decision": True,
                "auto_execute": False,
            },
            {
                "action_type": "prepare_first_batch_after_facts",
                "summary": "责任事实补齐后，再挑近期记录少或风险更高的孩子作为第一批。",
                "requires_model_decision": True,
                "auto_execute": False,
            },
        ]
    else:
        phase_key = "first_batch_teacher_feedback"
        phase_name = "阶段1：第一批老师反馈"
        phase_goal = "优先推进责任明确、近期记录不足或存在风险的学生，先拿到真实服务证据。"
        next_actions = [
            {
                "action_type": "collect_teacher_feedback_for_first_batch",
                "summary": "围绕第一批学生向相关老师收集真实近况和家长沟通素材；不自动群发任务。",
                "requires_model_decision": True,
                "auto_execute": False,
            },
            {
                "action_type": "record_goal_progress_evidence",
                "summary": "老师或店长明确反馈后，记录目标进度证据。",
                "requires_model_decision": True,
                "auto_execute": False,
            },
        ]
    return {
        "goal_id": goal_id,
        "goal_text": str(goal_text or ""),
        "plan_type": "goal_execution_plan_v1",
        "owner_confirmed": True,
        "model_led": True,
        "system_does_not_force_next_step": True,
        "phases": recommended_plan,
        "current_phase": {
            "phase_key": phase_key,
            "phase_name": phase_name,
            "phase_goal": phase_goal,
            "basis": "来自目标审查事实包和老板确认，不是系统固定流程。",
            "auto_execute": False,
        },
        "next_actions": next_actions,
        "success_evidence_required": [
            "真实责任关系已确认",
            "老师/店长/老板明确反馈",
            "目标进度有学生、时间、来源和内容证据",
            "完成前重新核验覆盖率和剩余缺口",
        ],
        "cadence": {
            "default_attention": "每次醒来先看当前阶段、等待事项和最新事实，不只做周期汇总。",
            "owner_summary": "白天可在正式授权、灰度、频率和责任证据边界内主动找老板、店长或老师推进；家长外发始终关闭。",
        },
        "boundaries": {
            "auto_parent_message": False,
            "auto_teacher_bulk_task": False,
            "salary_change": False,
            "permission_change": False,
            "delete_data": False,
            "forces_model_workflow": False,
        },
    }


def _sync_goal_autonomous_work_item(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    goal: dict[str, Any],
    review: dict[str, Any],
    operation_id: str,
) -> dict[str, Any]:
    try:
        from .digital_employee_state import submit_hermes_work_item
    except Exception as exc:
        return {
            "ok": False,
            "error": "autonomous_work_item_sync_unavailable",
            "message": str(exc),
            "non_blocking": True,
        }

    goal_id = str(goal.get("goal_id") or "")
    goal_text = str(goal.get("goal_text") or "")
    plan = _goal_execution_plan(review, goal_id=goal_id, goal_text=goal_text)
    progress = goal.get("progress") if isinstance(goal.get("progress"), dict) else {}
    workloads = review.get("teacher_workloads") if isinstance(review.get("teacher_workloads"), list) else []
    related_staff = [
        str(item.get("teacher_user_id") or "")
        for item in workloads
        if str(item.get("teacher_user_id") or "").strip() and str(item.get("teacher_user_id") or "") != "unassigned"
    ]
    current_phase = plan["current_phase"]
    next_actions = plan["next_actions"]
    try:
        result = submit_hermes_work_item(
            store,
            identity=identity,
            focus_key=f"goal:{goal_id}",
            title=f"目标推进：{goal_text or '未命名目标'}",
            focus_summary=(
                f"老板已确认目标，需要按阶段推进而不是只做周期汇总。"
                f"当前进度：已沟通 {progress.get('communicated_count', 0)}，"
                f"剩余 {progress.get('missing_count', 0)}。"
            ),
            related_objects=[{"type": "goal", "goal_id": goal_id, "goal_type": goal.get("goal_type")}],
            related_staff_user_ids=related_staff,
            execution_plan=plan.get("phases") or [],
            current_phase=current_phase,
            next_actions=next_actions,
            progress_evidence=[],
            confirmed_facts=[
                {"fact": "老板已确认目标", "goal_id": goal_id, "goal_text": goal_text},
                {"fact": "目标审查已生成分阶段建议", "plan_type": plan.get("plan_type")},
                {"fact": "当前阶段不是周期汇总，而是推进计划恢复材料", "phase": current_phase.get("phase_name")},
            ],
            pending_judgements=[],
            completed_actions=[],
            current_waiting={},
            next_attention_at="",
            status="active",
            source_text=str(goal.get("confirmation_text") or ""),
            operation_id=f"{operation_id}:goal_work_item",
        )
    except Exception as exc:
        return {
            "ok": False,
            "error": "autonomous_work_item_sync_blocked",
            "message": str(exc),
            "non_blocking": True,
        }
    if not result.get("ok"):
        result = {**result, "non_blocking": True}
    return result


def confirm_goal(store: TuoguanStore, *, identity: UserIdentity, goal_text: str, confirmation_text: str, operation_id: str, program_id: str = PROGRAM_REGULAR) -> dict[str, Any]:
    review = review_parent_communication_goal(store, goal_text=goal_text, program_id=program_id)
    goal_id = stable_goal_id(identity.canonical_user_id, program_id, goal_text or confirmation_text or operation_id)
    data = load_goals(store)
    existing = next((item for item in data["goals"] if isinstance(item, dict) and str(item.get("goal_id") or "") == goal_id), None)
    stamp = now_iso()
    goal = {
        "goal_id": goal_id,
        "goal_type": GOAL_TYPE_PARENT_COMMUNICATION,
        "tenant_id": current_tenant_id(),
        "program_id": program_id,
        "status": "confirmed",
        "goal_text": str(goal_text or "").strip(),
        "confirmation_text": str(confirmation_text or "").strip(),
        "owner_user_id": identity.canonical_user_id,
        "created_at": existing.get("created_at") if isinstance(existing, dict) else stamp,
        "updated_at": stamp,
        "review_snapshot": review,
        "progress": {
            "student_count": review["student_count"],
            "communicated_count": review["communicated_count"],
            "missing_count": review["missing_count"],
            "teacher_count": review["teacher_count"],
        },
        "teacher_followups": [
            {
                "teacher_user_id": item.get("teacher_user_id"),
                "teacher_name": item.get("teacher_name"),
                "student_names": [name for name in item.get("students", []) if name in set(review["missing_students"])],
                "unresolved_students": item.get("unresolved_students", []),
                "status": "needs_responsibility_confirmation" if item.get("teacher_user_id") == "" else "pending",
            }
            for item in (review.get("responsibility_coverage") or {}).get("by_responsible_teacher", [])
            if any(name in set(review["missing_students"]) for name in item.get("students", []))
        ],
        "risk_boundaries": deepcopy(review.get("risk_boundaries") or {}),
        "decision_support": deepcopy(review.get("decision_support") or {}),
        "next_step_policy": {
            "auto_parent_message": False,
            "auto_teacher_message": "only_after_model_selection_and_proactive_authorization",
            "autonomous_low_risk_goal_subtasks": True,
            "requires_model_decision_for_each_outreach": True,
            "requires_human_confirmation_for_high_risk": True,
            "note": "确认目标会建立持久行动账本；每次真实触达仍需模型选择并重新通过权限、频率、幂等和写后反查。",
        },
        "boss_final_approval_required_for_high_risk": True,
    }
    if existing is None:
        data["goals"].append(goal)
    else:
        existing.clear(); existing.update(goal)
    save_goals(store, data)
    append_jsonl(store, EVENT_FILE, {"event": "goal_confirmed", "goal_id": goal_id, "operation_id": operation_id, "actor_user_id": identity.canonical_user_id, "created_at": stamp})
    autonomous_work_item = _sync_goal_autonomous_work_item(store, identity=identity, goal=goal, review=review, operation_id=operation_id)
    try:
        from .proactive_work import seed_goal_actions_for_confirmed_goal

        goal_actions = seed_goal_actions_for_confirmed_goal(
            store,
            identity=identity,
            goal=goal,
            review=review,
            operation_id=operation_id,
        )
    except Exception as exc:
        goal_actions = [{"ok": False, "error": "goal_action_seed_failed", "message": str(exc)[:300]}]
    verified = any(isinstance(item, dict) and str(item.get("goal_id") or "") == goal_id and str(item.get("status") or "") == "confirmed" for item in load_goals(store).get("goals", []))
    unresolved_count = int((review.get('responsibility_coverage') or {}).get('unresolved_count') or 0)
    next_packet = _manager_question_packet(store, unresolved_count)
    rendered_lines = [
        "【已确认】",
        "我已保存这个家长沟通覆盖目标。",
        "",
        "【目标范围】",
        f"正式托管学生：{review['student_count']}名",
        f"还需推进：{review['missing_count']}名",
        f"涉及老师：{review['teacher_count']}位",
        f"责任待确认：{unresolved_count}名",
        "",
        "【下一步】",
    ]
    if unresolved_count:
        rendered_lines.append(f"先向{'、'.join(next_packet['ask_names'])}确认午托/晚托/全托和主责老师关系。")
    else:
        rendered_lines.append("从第一批责任明确、近期记录不足或风险更高的学生开始推进老师反馈。")
    rendered_lines.extend(["", "【边界】", "我不会直接联系家长；授权内可主动找责任明确的老师推进并以真实回复更新目标。"] )
    rendered = "\n".join(rendered_lines)
    return {
        "ok": True,
        "goal": goal,
        "goal_id": goal_id,
        "writeback_verified": verified,
        "autonomous_work_item": autonomous_work_item,
        "goal_actions": goal_actions,
        "rendered_text": rendered,
        "render_verified": True,
        "next_best_action": next_packet,
        "internal_outreach": {"auto_sent": False, "target_names": next_packet["ask_names"], "question": next_packet["question"]},
    }


def find_active_goal(store: TuoguanStore, *, goal_id: str = "") -> dict[str, Any] | None:
    goals = [item for item in load_goals(store).get("goals", []) if isinstance(item, dict)]
    if goal_id:
        return next(
            (
                item for item in goals
                if str(item.get("goal_id") or "") == goal_id
                and str(item.get("status") or "") in {"confirmed", "in_progress"}
            ),
            None,
        )
    active = [item for item in goals if str(item.get("status") or "") in {"confirmed", "in_progress"}]
    if not active:
        return None
    return sorted(active, key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)[0]


def withdraw_goal(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    goal_id: str = "",
    goal_text: str = "",
    withdraw_reason: str = "",
    operation_id: str,
) -> dict[str, Any]:
    data = load_goals(store)
    goals = [item for item in data.get("goals", []) if isinstance(item, dict)]
    normalized_goal_id = str(goal_id or "").strip()
    normalized_text = str(goal_text or "").strip()
    goal: dict[str, Any] | None = None
    if normalized_goal_id:
        goal = next((item for item in goals if str(item.get("goal_id") or "") == normalized_goal_id), None)
    if goal is None and normalized_text:
        matches = [
            item for item in goals
            if normalized_text in str(item.get("goal_text") or "")
            or str(item.get("goal_text") or "") in normalized_text
        ]
        if matches:
            goal = sorted(matches, key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)[0]
    if goal is None:
        return {"ok": False, "error": "goal_not_found", "message": "没有找到要撤出的目标。", "data": {}}

    previous_status = str(goal.get("status") or "")
    if previous_status in {"withdrawn", "cancelled", "superseded"}:
        return {
            "ok": True,
            "goal_id": goal.get("goal_id"),
            "goal": goal,
            "writeback_verified": True,
            "idempotent_replay": True,
            "rendered_text": "这个目标已经不在当前推进列表里；历史记录仍保留用于审计。",
            "render_verified": True,
        }

    stamp = now_iso()
    reason = str(withdraw_reason or "").strip() or "老板明确要求撤出当前目标。"
    try:
        from .proactive_work import stop_goal_execution

        execution_stop = stop_goal_execution(
            store,
            identity=identity,
            goal_id=str(goal.get("goal_id") or ""),
            operation_id=f"{operation_id}:stop_execution",
            reason=reason,
        )
    except Exception as exc:
        execution_stop = {"ok": False, "error": "goal_execution_stop_failed", "message": str(exc)[:300]}
    goal["status"] = "withdrawn"
    goal["previous_status"] = previous_status
    goal["withdrawn_at"] = stamp
    goal["withdrawn_by_user_id"] = identity.canonical_user_id
    goal["withdraw_reason"] = reason
    goal["current_material_status"] = "not_current_decision_material"
    goal["decision_material_status"] = "not_current_decision_material"
    goal["updated_at"] = stamp
    if "测试" in reason or "test" in reason.lower() or "测试" in str(goal.get("goal_text") or ""):
        goal["test_goal_removed"] = True
    save_goals(store, data)
    append_jsonl(
        store,
        EVENT_FILE,
        {
            "event": "goal_withdrawn",
            "goal_id": goal.get("goal_id"),
            "operation_id": operation_id,
            "actor_user_id": identity.canonical_user_id,
            "previous_status": previous_status,
            "withdraw_reason": reason,
            "created_at": stamp,
        },
    )

    work_item_update: dict[str, Any] = {"ok": False, "error": "work_item_not_found", "non_blocking": True}
    try:
        from .digital_employee_state import update_hermes_work_item

        work_item_update = update_hermes_work_item(
            store,
            identity=identity,
            focus_key=f"goal:{goal.get('goal_id')}",
            status="superseded",
            focus_summary="老板已明确撤出该目标；它不再作为 Hermes 当前推进或提醒材料。",
            current_phase={"phase_key": "withdrawn_by_owner", "phase_name": "老板已撤出目标", "auto_execute": False},
            next_actions=[],
            progress_evidence=[],
            confirmed_facts=[
                {"fact": "老板明确撤出该目标", "goal_id": goal.get("goal_id"), "reason": reason},
            ],
            pending_judgements=[],
            completed_actions=[{"action": "goal_withdrawn_from_current_materials", "at": stamp}],
            current_waiting={},
            stop_reason=reason,
            update_text="老板明确撤出目标；同步关闭对应自主工作项。",
            source_text=reason,
            operation_id=f"{operation_id}:goal_work_item_withdraw",
        )
    except Exception as exc:
        work_item_update = {"ok": False, "error": "work_item_update_unavailable", "message": str(exc), "non_blocking": True}

    persisted = next(
        (
            item for item in load_goals(store).get("goals", [])
            if isinstance(item, dict) and str(item.get("goal_id") or "") == str(goal.get("goal_id") or "")
        ),
        None,
    )
    verified = isinstance(persisted, dict) and str(persisted.get("status") or "") == "withdrawn"
    rendered = "已把这个目标撤出当前推进。它不会再作为 Hermes 当前目标继续推进；历史和审计记录会保留，不会物理删除。"
    return {
        "ok": True,
        "goal_id": goal.get("goal_id"),
        "goal": goal,
        "previous_status": previous_status,
        "writeback_verified": verified,
        "autonomous_work_item": work_item_update,
        "goal_execution_stop": execution_stop,
        "rendered_text": rendered,
        "render_verified": True,
        "auto_parent_message": False,
        "auto_teacher_message": False,
        "business_boundary": "withdraw_current_goal_only_keep_audit_history",
    }


def update_goal_progress(store: TuoguanStore, *, identity: UserIdentity, goal_id: str, student_name: str, update_text: str, operation_id: str) -> dict[str, Any]:
    data = load_goals(store)
    goals = [item for item in data.get("goals", []) if isinstance(item, dict)]
    if goal_id:
        goal = next((item for item in goals if str(item.get("goal_id") or "") == str(goal_id)), None)
    else:
        active = [item for item in goals if str(item.get("status") or "") in {"confirmed", "in_progress"}]
        goal = sorted(active, key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)[0] if active else None
    if goal is None:
        return {"ok": False, "error": "goal_not_found", "message": "没有找到正在推进的家长沟通目标。", "data": {}}
    if str(goal.get("status") or "") not in {"confirmed", "in_progress"}:
        return {
            "ok": False,
            "error": "goal_not_active",
            "message": "这个目标已经不在当前推进列表里，不能继续写入进展。",
            "data": {"goal_id": goal.get("goal_id"), "status": goal.get("status")},
        }
    name = str(student_name or "").strip()
    if not name:
        return {"ok": False, "error": "student_required", "message": "请说明是哪位学生的家长沟通反馈。", "data": {}}
    review = goal.get("review_snapshot") if isinstance(goal.get("review_snapshot"), dict) else {}
    valid_names = set(review.get("missing_students") or []) | set(active_students(store, program_id=str(goal.get("program_id") or PROGRAM_REGULAR)).keys())
    if name not in valid_names:
        return {"ok": False, "error": "student_not_in_goal", "message": f"{name} 不在当前目标学生范围里，我先不写入目标进度。", "data": {}}
    responsibility = resolve_student_responsibility(store, name, purpose="parent_communication")
    if identity.role == "teacher":
        responsible_user = str(responsibility.get("responsible_user_id") or "")
        known_user = str(responsibility.get("known_teacher_user_id") or "")
        if responsible_user and responsible_user != identity.canonical_user_id and known_user != identity.canonical_user_id:
            return {
                "ok": False,
                "error": "student_out_of_teacher_scope",
                "message": f"{name} 的家长沟通责任当前不在你的老师范围内，我先不写入目标进度。",
                "data": {"responsibility": responsibility},
            }
        if responsibility.get("resolution_status") != "resolved" and known_user != identity.canonical_user_id:
            return {
                "ok": False,
                "error": "responsibility_not_confirmed",
                "message": f"{name} 的主责关系还没确认，需要先问店长或老板。",
                "data": {"responsibility": responsibility},
            }
    stamp = now_iso()
    updates = goal.setdefault("teacher_updates", [])
    if not isinstance(updates, list):
        goal["teacher_updates"] = updates = []
    update_id = stable_goal_id("goal_update", operation_id, identity.canonical_user_id, name)
    existing = next((item for item in updates if isinstance(item, dict) and str(item.get("update_id") or "") == update_id), None)
    row = {
        "update_id": update_id,
        "student_name": name,
        "teacher_user_id": identity.canonical_user_id,
        "teacher_role": identity.role,
        "update_text": str(update_text or "").strip(),
        "created_at": existing.get("created_at") if isinstance(existing, dict) else stamp,
        "updated_at": stamp,
        "source": "teacher_statement",
        "effectiveness": "needs_review" if len(str(update_text or "").strip()) < 8 else "usable_statement",
        "responsibility_snapshot": responsibility,
    }
    if existing is None:
        updates.append(row)
    else:
        existing.clear(); existing.update(row)
    goal["status"] = "in_progress"
    goal["updated_at"] = stamp
    communicated = {item.get("student_name") for item in updates if isinstance(item, dict) and item.get("student_name")}
    student_count = int((goal.get("progress") or {}).get("student_count") or len(valid_names))
    progress = goal.setdefault("progress", {})
    progress["teacher_update_count"] = len(updates)
    progress["communicated_count_by_goal_updates"] = len(communicated)
    progress["remaining_by_goal_updates"] = max(0, student_count - len(communicated))
    save_goals(store, data)
    append_jsonl(store, EVENT_FILE, {"event": "goal_progress_updated", "goal_id": goal.get("goal_id"), "update_id": update_id, "operation_id": operation_id, "actor_user_id": identity.canonical_user_id, "student_name": name, "created_at": stamp})
    persisted = find_active_goal(store, goal_id=str(goal.get("goal_id") or ""))
    verified = isinstance(persisted, dict) and any(isinstance(item, dict) and str(item.get("update_id") or "") == update_id for item in persisted.get("teacher_updates", []))
    rendered = f"已把{name}的家长沟通反馈记入目标进度。当前目标内已有 {len(communicated)} 名学生有老师反馈。"
    return {"ok": True, "goal_id": goal.get("goal_id"), "update": row, "progress": progress, "writeback_verified": verified, "rendered_text": rendered, "render_verified": True}


def query_goal_progress(store: TuoguanStore, *, goal_id: str = "") -> dict[str, Any]:
    goal = find_active_goal(store, goal_id=goal_id)
    if goal is None:
        return {"ok": False, "error": "goal_not_found", "message": "当前没有正在推进的目标。", "data": {}}
    review = goal.get("review_snapshot") if isinstance(goal.get("review_snapshot"), dict) else {}
    updates = [item for item in goal.get("teacher_updates", []) if isinstance(item, dict)]
    updated_students = sorted({str(item.get("student_name") or "") for item in updates if item.get("student_name")})
    missing = [name for name in review.get("missing_students", []) if name not in set(updated_students)]
    lines = [
        "【目标进度】",
        f"目标学生：{review.get('student_count', 0)}名",
        f"老师已反馈：{len(updated_students)}名",
        f"仍需跟进：{len(missing)}名",
    ]
    if missing:
        names = "、".join(missing[:10])
        lines.extend(["", "【还差】", f"{names}{'等' if len(missing) > 10 else ''}"])
    return {"ok": True, "goal": goal, "updated_students": updated_students, "missing_students": missing, "rendered_text": "\n".join(lines), "render_verified": True}



def _goal_workspace_stage(*, review: dict[str, Any] | None = None, goal: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the deterministic operating stage for a long-running goal.

    This is not an intent router. The Hermes Agent decides whether to use the
    workspace. Once used, the workspace only exposes the current factual stage,
    safe next actions and forbidden actions so the Agent does not have to juggle
    several low-level goal tools in one turn.
    """
    review = review or {}
    goal = goal or {}
    if goal:
        status = str(goal.get("status") or "")
        snapshot = goal.get("review_snapshot") if isinstance(goal.get("review_snapshot"), dict) else review
        responsibility = snapshot.get("responsibility_coverage") if isinstance(snapshot, dict) else {}
        unresolved = int((responsibility or {}).get("unresolved_count") or 0)
        if status in {"confirmed", "in_progress"} and unresolved:
            stage = "needs_responsibility_facts"
            next_action = "ask_manager_or_boss_for_responsibility_facts"
        elif status in {"confirmed", "in_progress"}:
            stage = "ready_to_collect_teacher_feedback"
            next_action = "collect_teacher_parent_communication_feedback"
        else:
            stage = status or "unknown"
            next_action = "review_goal_status"
    else:
        responsibility = review.get("responsibility_coverage") if isinstance(review, dict) else {}
        int((responsibility or {}).get("unresolved_count") or 0)
        stage = "waiting_boss_confirmation"
        next_action = "wait_for_boss_confirmation_before_execution"
    return {
        "stage": stage,
        "allowed_next_actions": [
            "model_explain_fact_based_plan",
            "model_ask_one_needed_question",
            "query_goal_progress",
            next_action,
        ],
        "forbidden_next_actions": [
            "auto_send_parent_message",
            "auto_notify_all_teachers_without_model_decision",
            "auto_create_bulk_tasks_before_goal_saved",
            "change_payroll",
            "change_permissions",
            "delete_business_data",
        ],
        "next_best_action": next_action,
        "requires_boss_confirmation_before_execution": not bool(goal),
    }


def operate_goal_workspace(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    action: str,
    goal_text: str = "",
    confirmation_text: str = "",
    goal_id: str = "",
    student_name: str = "",
    update_text: str = "",
    withdraw_reason: str = "",
    operation_id: str = "",
    program_id: str = PROGRAM_REGULAR,
) -> dict[str, Any]:
    """Unified goal operating workspace for the Hermes Agent.

    The workspace keeps long-running goal work coherent without taking intent
    ownership away from the Agent. It never dispatches teacher tasks or parent
    messages by itself; it only returns verified facts, current stage, boundaries,
    and the safest next step.
    """
    normalized_action = str(action or "").strip() or "review"
    program_id = program_id or PROGRAM_REGULAR
    if normalized_action == "review":
        review = review_parent_communication_goal(store, goal_text=goal_text, program_id=program_id)
        stage = _goal_workspace_stage(review=review)
        data = {
            **review,
            "workspace_action": "review",
            "workspace_stage": stage["stage"],
            "allowed_next_actions": stage["allowed_next_actions"],
            "forbidden_next_actions": stage["forbidden_next_actions"],
            "next_best_action": stage["next_best_action"],
            "goal_saved": False,
            "auto_teacher_message": False,
            "auto_parent_message": False,
        }
        return {"ok": True, "data": data, "rendered_text": review.get("rendered_text", ""), "render_verified": True}

    if normalized_action == "confirm":
        result = confirm_goal(
            store,
            identity=identity,
            goal_text=goal_text,
            confirmation_text=confirmation_text,
            operation_id=operation_id,
            program_id=program_id,
        )
        if not result.get("ok"):
            return result
        stage = _goal_workspace_stage(goal=result.get("goal") if isinstance(result.get("goal"), dict) else {})
        result.update({
            "workspace_action": "confirm",
            "workspace_stage": stage["stage"],
            "allowed_next_actions": stage["allowed_next_actions"],
            "forbidden_next_actions": stage["forbidden_next_actions"],
            "next_best_action_type": stage["next_best_action"],
            "goal_saved": True,
            "auto_teacher_message": False,
            "auto_parent_message": False,
            "business_boundary": "goal_saved_only_no_auto_dispatch",
        })
        return result

    if normalized_action in {"query_progress", "next_step"}:
        result = query_goal_progress(store, goal_id=goal_id)
        if not result.get("ok"):
            return result
        goal = result.get("goal") if isinstance(result.get("goal"), dict) else {}
        stage = _goal_workspace_stage(goal=goal)
        result.update({
            "workspace_action": normalized_action,
            "workspace_stage": stage["stage"],
            "allowed_next_actions": stage["allowed_next_actions"],
            "forbidden_next_actions": stage["forbidden_next_actions"],
            "next_best_action_type": stage["next_best_action"],
            "auto_teacher_message": False,
            "auto_parent_message": False,
        })
        return result

    if normalized_action == "record_progress":
        result = update_goal_progress(
            store,
            identity=identity,
            goal_id=goal_id,
            student_name=student_name,
            update_text=update_text,
            operation_id=operation_id,
        )
        if not result.get("ok"):
            return result
        goal = find_active_goal(store, goal_id=str(result.get("goal_id") or goal_id or "")) or {}
        stage = _goal_workspace_stage(goal=goal)
        result.update({
            "workspace_action": "record_progress",
            "workspace_stage": stage["stage"],
            "allowed_next_actions": stage["allowed_next_actions"],
            "forbidden_next_actions": stage["forbidden_next_actions"],
            "next_best_action_type": stage["next_best_action"],
            "auto_teacher_message": False,
            "auto_parent_message": False,
        })
        return result

    if normalized_action in {"withdraw", "cancel", "supersede"}:
        result = withdraw_goal(
            store,
            identity=identity,
            goal_id=goal_id,
            goal_text=goal_text,
            withdraw_reason=withdraw_reason or confirmation_text or update_text,
            operation_id=operation_id,
        )
        if not result.get("ok"):
            return result
        stage = _goal_workspace_stage(goal=result.get("goal") if isinstance(result.get("goal"), dict) else {})
        result.update({
            "workspace_action": normalized_action,
            "workspace_stage": stage["stage"],
            "allowed_next_actions": stage["allowed_next_actions"],
            "forbidden_next_actions": stage["forbidden_next_actions"],
            "next_best_action_type": "review_remaining_active_goals",
            "goal_saved": False,
            "auto_teacher_message": False,
            "auto_parent_message": False,
            "business_boundary": "goal_withdrawn_from_current_materials_only_keep_audit",
        })
        return result

    return {
        "ok": False,
        "error": "unsupported_goal_workspace_action",
        "message": "目标工作区暂不支持这个动作。",
        "data": {"allowed_actions": ["review", "confirm", "query_progress", "next_step", "record_progress", "withdraw"]},
    }
