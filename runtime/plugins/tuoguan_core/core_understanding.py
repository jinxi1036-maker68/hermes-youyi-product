"""Read-only LLM understanding for Core Capability Sprint V1."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

import httpx
import yaml


ALLOWED = {
    "",
    "teacher_task_guidance",
    "safety_workflow_coach",
    "management_boss_advisor",
    "teacher_my_tasks_query",
    "summer_points",
    "student_daily_record",
    "boss_operations_query",
    "operations_daily_report",
}
KEYS = {"capability_candidate", "confidence", "reason", "business_object_hint"}
EVIDENCE_KEYS = {"observation_completed", "condition_change", "activity_state", "future_only", "confidence", "reason"}


class CoreUnderstandingAdapter:
    """The adapter can only classify; it has no repository or tool access."""

    def __init__(self, *, base_url: str, api_key: str, model: str, timeout: float = 20.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.last_trace: dict[str, Any] = {}

    @classmethod
    def from_config(cls, path: Path) -> "CoreUnderstandingAdapter":
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        cfg = data.get("model") or {}
        model = str(cfg.get("model") or cfg.get("name") or "").strip()
        if not all((cfg.get("base_url"), cfg.get("api_key"), model)):
            raise ValueError("llm_provider_config_incomplete")
        return cls(base_url=str(cfg["base_url"]), api_key=str(cfg["api_key"]), model=model, timeout=float(cfg.get("request_timeout_seconds") or 20))

    def understand(self, payload: dict[str, Any]) -> dict[str, Any]:
        safe = {k: v for k, v in payload.items() if k not in {"api_key", "token", "secret"}}
        self.last_trace = {"first_output_error": "", "correction_attempted": False, "correction_result": "not_needed"}
        messages = [{"role": "system", "content": PROMPT}, {"role": "user", "content": json.dumps(safe, ensure_ascii=False)}]
        first = self._request(messages)
        try:
            return validate(json.loads(_json_text(first)))
        except Exception as exc:
            self.last_trace.update(first_output_error=_safe_error(exc), correction_attempted=True)
            corrected = self._request(messages + [
                {"role": "assistant", "content": first},
                {"role": "user", "content": "上一个JSON结构不合法，错误：" + _safe_error(exc) + "。只修JSON结构，不改变判断。" + SKELETON},
            ])
            try:
                result = validate(json.loads(_json_text(corrected)))
                self.last_trace["correction_result"] = "schema_valid"
                return result
            except Exception as second:
                self.last_trace["correction_result"] = "rejected:" + _safe_error(second)
                raise ValueError("core_schema_invalid_after_correction") from second

    def _request(self, messages: list[dict[str, str]]) -> str:
        response = httpx.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json={"model": self.model, "temperature": 0, "max_tokens": 300, "response_format": {"type": "json_object"}, "messages": messages},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return str(response.json()["choices"][0]["message"]["content"])

    def match_pending_evidence(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Propose semantic evidence only; never advances workflow state."""
        safe = {k: v for k, v in payload.items() if k not in {"api_key", "token", "secret"}}
        messages = [{"role": "system", "content": EVIDENCE_PROMPT}, {"role": "user", "content": json.dumps(safe, ensure_ascii=False)}]
        first = self._request(messages)
        try:
            return validate_evidence(json.loads(_json_text(first)))
        except Exception as exc:
            corrected = self._request(messages + [
                {"role": "assistant", "content": first},
                {"role": "user", "content": "只修正JSON结构，错误：" + _safe_error(exc) + "。" + EVIDENCE_SKELETON},
            ])
            return validate_evidence(json.loads(_json_text(corrected)))


PROMPT = """你是示例机构托管只读业务理解器，不能调用工具、不能写数据、不能回复用户。
只判断当前消息最可能属于输入 allowed_candidates 中的一个能力，或空字符串。
teacher_task_guidance：老师开始/继续任务、反馈任务进度、询问任务怎么做。
safety_workflow_coach：受伤或安全事件，以及该事件的连续处理。
management_boss_advisor：老板或店长询问今天优先关注什么、该先处理什么。
teacher_my_tasks_query：用户查询自己当前或今日的任务；老板说“我的任务”也只表示本人。
summer_points：暑假班积分加减、兑换、拍卖、单人积分或排行榜查询。
student_daily_record：记录或查询某个学生的学习、纪律、生活、午休等日常表现。
boss_operations_query：老板查询真实经营数字、老师记录、待办、安全、学生或积分概况。
operations_daily_report：老板要求生成、查看或总结今日经营日报。
身份、租户和上下文只相信输入。不要因为消息出现学生姓名就改判学生记录。
必须从输入的 allowed_candidates 中选择；不属于这些能力就返回空字符串。
business_object_hint 使用简短规范值：开始或继续下一项工作填 next_task；当前普通任务填 current_task；新安全事件填 new_safety_event；管理建议填 management_overview；本人任务查询填 my_tasks，全员任务查询填 all_tasks；积分加分/扣分/兑换/拍卖/单人查询/排行榜分别填 points_add、points_deduct、points_exchange、points_auction、points_query、points_ranking；学生记录写入/查询填 record_student、query_student_records；经营概况/老师记录/待办任务/安全任务/暑假班人数/积分概况分别填 operations_overview、operations_teacher_records、operations_open_tasks、operations_safety_tasks、operations_summer_students、operations_points；日报填 daily_report；无法确定填空字符串。
只返回一个JSON对象，不得增加、删除或重命名字段，不得输出Markdown。精确骨架：
{"capability_candidate":"","confidence":0.0,"reason":"","business_object_hint":""}"""
SKELETON = json.dumps({"capability_candidate": "", "confidence": 0.0, "reason": "", "business_object_hint": ""}, ensure_ascii=False, separators=(",", ":"))
EVIDENCE_PROMPT = """你是只读Workflow证据理解器，不能调用工具、不能推进状态、不能补充用户未说的事实。
结合当前阶段、审核要求、待补证据、既有事实和老师当前消息，判断老师是否已经完成一次观察并报告真实结果。
condition_change只能是not_worsened、improved、worsened、stable、unknown。
activity_state只能是normal、limited、unable、unknown。
未来计划不等于已完成观察。只返回指定JSON，不得输出Markdown。"""
EVIDENCE_SKELETON = json.dumps({"observation_completed": False, "condition_change": "unknown", "activity_state": "unknown", "future_only": False, "confidence": 0.0, "reason": ""}, ensure_ascii=False, separators=(",", ":"))


def validate(data: dict[str, Any]) -> dict[str, Any]:
    if set(data) != KEYS: raise ValueError("invalid_keys")
    candidate = data["capability_candidate"]
    if candidate not in ALLOWED: raise ValueError("invalid_capability")
    confidence = float(data["confidence"])
    if not 0 <= confidence <= 1: raise ValueError("invalid_confidence")
    return {"capability_candidate": candidate, "confidence": confidence, "reason": str(data["reason"]), "business_object_hint": str(data["business_object_hint"])}


def validate_evidence(data: dict[str, Any]) -> dict[str, Any]:
    if set(data) != EVIDENCE_KEYS: raise ValueError("invalid_evidence_keys")
    confidence = float(data["confidence"])
    if not 0 <= confidence <= 1: raise ValueError("invalid_confidence")
    return {
        "observation_completed": bool(data["observation_completed"]),
        "condition_change": str(data["condition_change"]),
        "activity_state": str(data["activity_state"]),
        "future_only": bool(data["future_only"]),
        "confidence": confidence,
        "reason": str(data["reason"]),
    }


def _json_text(value: str) -> str:
    value = value.strip()
    return re.sub(r"^```(?:json)?\s*|\s*```$", "", value, flags=re.I) if value.startswith("```") else value


def _safe_error(exc: Exception) -> str:
    return re.sub(r"[^A-Za-z0-9_:\-.,\[\] ']+", "?", f"{type(exc).__name__}:{exc}")[:240]
