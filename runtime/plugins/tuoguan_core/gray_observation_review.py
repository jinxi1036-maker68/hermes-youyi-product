"""Read-only review candidates derived from gray observations.

The output is a human review queue only. It must not update handbooks, create
learning candidates, patch tools, change permissions, or constrain the model.
"""

from __future__ import annotations

from typing import Any

from .digital_employee_state import query_gray_observations
from .models import UserIdentity
from .store import TuoguanStore


def generate_gray_observation_candidates(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    outcome: str = "issue",
    limit: int = 50,
) -> dict[str, Any]:
    if identity.role not in {"boss", "manager"}:
        return {"ok": False, "error": "permission_denied", "message": "灰度观察优化候选第一阶段仅允许老板或店长查看。"}
    observations = query_gray_observations(store, identity=identity, outcome=outcome, limit=limit)
    if not observations.get("ok"):
        return observations
    candidates = [_candidate_from_observation(item) for item in observations.get("observations", [])]
    counts: dict[str, int] = {}
    for item in candidates:
        key = str(item.get("candidate_type") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return {
        "ok": True,
        "candidate_count": len(candidates),
        "candidate_type_counts": counts,
        "candidates": candidates,
        "source_observation_count": int(observations.get("observation_count") or 0),
        "boundary": {
            "review_only": True,
            "limits_model": False,
            "updates_handbook": False,
            "creates_learning_candidate": False,
            "patches_tools": False,
            "changes_permissions": False,
            "creates_tasks": False,
            "sends_notifications": False,
            "changes_salary": False,
        },
        "rendered_text": f"基于灰度观察生成 {len(candidates)} 条人工优化候选。候选只供老板/实现者审核，不自动改手册、不创建学习候选、不修工具、不限制模型。",
        "render_verified": True,
    }


def _candidate_from_observation(observation: dict[str, Any]) -> dict[str, Any]:
    text = str(observation.get("observation_text") or "")
    scenario_id = str(observation.get("scenario_id") or "")
    candidate_type = _candidate_type(text, scenario_id)
    return {
        "candidate_id": f"gray_review_candidate:{observation.get('observation_id') or scenario_id}",
        "candidate_type": candidate_type,
        "scenario_id": scenario_id,
        "source_observation_id": str(observation.get("observation_id") or ""),
        "source_outcome": str(observation.get("outcome") or ""),
        "source_text": text,
        "suggested_review_question": _review_question(candidate_type),
        "suggested_next_step": _next_step(candidate_type),
        "auto_effects": {
            "updates_handbook": False,
            "creates_learning_candidate": False,
            "patches_tools": False,
            "changes_permissions": False,
            "limits_model": False,
        },
    }


def _candidate_type(text: str, scenario_id: str) -> str:
    compact = text.lower()
    if any(term in text for term in ("自主工作", "等待", "唤醒", "醒来", "恢复", "结果未知", "推进到哪一步", "还在等什么")):
        return "autonomous_work_candidate"
    if any(term in text for term in ("不自然", "话术", "回复", "表达", "语气")):
        return "response_style_candidate"
    if any(term in text for term in ("没记录", "写入", "失败", "工具", "查不到", "不准", "数据")):
        return "tool_or_data_candidate"
    if any(term in text for term in ("权限", "拦截", "不能", "被挡", "校验")):
        return "permission_boundary_candidate"
    if any(term in text for term in ("手册", "不知道", "应该", "流程", "怎么做")):
        return "handbook_candidate"
    if scenario_id in {"boss_goal", "manager_gap_query", "parent_communication_coverage"}:
        return "handbook_candidate"
    if "error" in compact or "failed" in compact:
        return "tool_or_data_candidate"
    return "manual_review_candidate"


def _review_question(candidate_type: str) -> str:
    return {
        "response_style_candidate": "这是否需要优化 Hermes 的表达风格、确认话术或回复结构？",
        "tool_or_data_candidate": "这是否是工具缺陷、数据缺口、字段映射问题或测试样本问题？",
        "permission_boundary_candidate": "这是否是权限边界应保留，还是授权/上下文配置需要修复？",
        "handbook_candidate": "这是否需要进入手册作为业务经验或员工操作参考？",
        "autonomous_work_candidate": "这是否需要优化 Hermes 自主工作状态、等待恢复、唤醒材料或结果未知处理？",
    }.get(candidate_type, "这条观察是否需要形成手册、工具、话术或权限优化候选？")


def _next_step(candidate_type: str) -> str:
    return {
        "response_style_candidate": "先人工复盘原对话，确认是否整理成话术优化候选。",
        "tool_or_data_candidate": "先复现并定位数据/工具问题，确认后再开工具修复。",
        "permission_boundary_candidate": "先判断是正确拦截还是误拦截，误拦截再修授权边界。",
        "handbook_candidate": "先由老板确认是否符合数字员工理念，再进入手册候选。",
        "autonomous_work_candidate": "先复盘真实对话和状态账本，确认是手册经验、工具状态缺口还是权限边界问题。",
    }.get(candidate_type, "先人工判断候选类型，不自动进入任何正式规则。")
