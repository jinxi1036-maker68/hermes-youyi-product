"""Natural, fact-locked success replies for Student Record Coach only."""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import json
from pathlib import Path
import re
import time
from typing import Any

import httpx
import yaml


OUTPUT_KEYS = {"acknowledgement", "guidance", "reply_text"}
SUCCESS_WORDS = ("记下", "记好", "记录好", "记录完成", "已经记录", "已记录")
GUIDANCE_PREFIXES = ("后面可以", "可以继续", "可以留意", "可以再", "不妨", "接下来可以")
TECHNICAL_WORDS = ("quality_score", "command_ready", "capability", "schema", "student_id", "record_scene", "writeback")
UNSUPPORTED_GENERALIZATIONS = ("最近一直", "一直都", "经常", "多次", "每天", "肯定会", "说明他已经", "说明她已经", "家长知道", "家长已经")
SCENE_GUIDANCE = {
    "positive_behavior": ["主动行为是否稳定出现", "在其他具体场景中是否也会主动承担"],
    "life": ["生活习惯的变化是否能保持", "后续几次同类场景中的表现"],
    "rest": ["午休行为是否稳定", "提醒后的变化以及是否影响他人"],
    "discipline": ["具体行为是否重复出现", "提醒后的变化"],
    "learning_problem": ["同类题是否重复出错", "区分偶发疏忽与知识点未掌握"],
    "learning_progress": ["类似题型或场景中能否稳定保持", "进步是否持续"],
    "teacher_intervention": ["老师处理后的即时变化", "后续是否仍需跟进"],
    "intervention_result": ["变化能否保持", "是否仍需继续跟踪"],
}


@dataclass(frozen=True)
class ComposedReply:
    reply_text: str
    reply_mode: str
    validated: bool
    fallback_reason: str
    acknowledgement: str = ""
    guidance: str = ""
    schema_trace: dict[str, Any] | None = None


class RecordCoachResponsePlanner:
    def __init__(self, *, base_url: str, api_key: str, model: str, timeout: float = 30.0) -> None:
        self.base_url, self.api_key, self.model, self.timeout = base_url.rstrip("/"), api_key, model, timeout
        self.last_trace: dict[str, Any] = {}

    @classmethod
    def from_config(cls, path: Path) -> "RecordCoachResponsePlanner":
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        cfg = data.get("model") or {}
        model = str(cfg.get("model") or cfg.get("name") or "").strip()
        if not all((cfg.get("base_url"), cfg.get("api_key"), model)):
            raise ValueError("llm_provider_config_incomplete")
        return cls(base_url=str(cfg["base_url"]), api_key=str(cfg["api_key"]), model=model, timeout=float(cfg.get("request_timeout_seconds") or 30))

    def compose(self, payload: dict[str, Any]) -> dict[str, str]:
        self.last_trace = {"first_output_error": "", "correction_attempted": False, "correction_result": "not_needed"}
        content = self._request([{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}])
        try:
            return _validate_schema(content)
        except Exception as exc:
            self.last_trace.update(first_output_error=_safe_error(exc), correction_attempted=True)
            skeleton = json.dumps({"acknowledgement": "", "guidance": "", "reply_text": ""}, ensure_ascii=False)
            corrected = self._request([
                {"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                {"role": "assistant", "content": content},
                {"role": "user", "content": f"上一个JSON不符合结构，错误：{_safe_error(exc)}。只修JSON结构，不改变含义。必须严格使用：{skeleton}"},
            ])
            try:
                result = _validate_schema(corrected)
                self.last_trace["correction_result"] = "schema_valid"
                return result
            except Exception as second:
                self.last_trace["correction_result"] = "rejected:" + _safe_error(second)
                raise ValueError("reply_schema_invalid_after_correction") from second

    def _request(self, messages: list[dict[str, str]]) -> str:
        response = None
        for attempt in range(2):
            try:
                response = httpx.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                    json={"model": self.model, "temperature": 0.45, "max_tokens": 500, "response_format": {"type": "json_object"}, "messages": messages},
                    timeout=self.timeout,
                )
            except (httpx.ReadTimeout, httpx.ConnectTimeout):
                if attempt == 1: raise
                time.sleep(1); continue
            if response.status_code != 429 or attempt == 1: break
            time.sleep(1)
        assert response is not None
        response.raise_for_status()
        return str(response.json()["choices"][0]["message"]["content"])

    def correct_fact_safety(self, payload: dict[str, Any], previous: dict[str, str], reason: str) -> dict[str, str]:
        self.last_trace["fact_correction_attempted"] = True
        student = str(payload.get("verified_business_result", {}).get("student_name") or "")
        specific_fix = {
            "ack_missing_verified_success_or_student": f"acknowledgement必须原样包含学生姓名“{student}”，并明确包含“记下来了”“记好了”或“已记录”之一。",
            "ack_not_grounded_in_confirmed_fact": "acknowledgement必须保留confirmed_facts中的至少一个原文关键动作或对象短语。",
            "guidance_not_marked_as_suggestion": "guidance必须以允许的建议前缀开头，不能把建议写成已发生事实。",
        }.get(reason, "只修复该校验问题。")
        corrected = self._request([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            {"role": "assistant", "content": json.dumps(previous, ensure_ascii=False)},
            {"role": "user", "content": f"回复未通过事实安全校验：{reason}。{specific_fix}仍只输出规定JSON；不得增加事实、数字、次数或已发生行为。"},
        ])
        result = _validate_schema(corrected)
        self.last_trace["fact_correction_result"] = "schema_valid"
        return result


SYSTEM_PROMPT = """你是经验丰富但说话简短的托管老师工作教练。输入只包含已经写后反查通过的学生记录事实。
你只负责生成1到3句中文自然回复，不能调用工具，不能修改业务结果，不能增加未发生事实。
acknowledgement必须自然确认已记录，提到输入中的学生姓名，并只复述输入中已经确认的核心事实，不要整句机械复制。
说话要像正在和老师配合工作的同事。通常先回应老师说的具体变化，再在句中自然确认“这条记下了”；不要用独立的“好”“记好了”作为固定开头，不要使用“已记录到某某档案：……”或“已记录某某某行为”这类公文式系统模板。
自然改写时至少保留一个原事实中的关键动作或对象短语，避免全部替换成抽象评价。
guidance可以为空；有明显跟进价值时，只给一条轻量建议，必须以“后面可以/可以继续/可以留意/可以再/不妨/接下来可以”开头。
建议不能写成已发生事实，不能增加家长已知、老师已处理、学生已改善、次数、日期、分数或新行为。
不要使用“根据系统分析”“经AI判断”和任何技术字段。总长度尽量35到100个汉字。
只输出JSON对象，且键严格为 acknowledgement、guidance、reply_text。reply_text必须等于 acknowledgement 与 guidance 顺序拼接。"""


def _validate_schema(content: str) -> dict[str, str]:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    data = json.loads(text)
    if set(data) != OUTPUT_KEYS or not all(isinstance(data[key], str) for key in OUTPUT_KEYS):
        raise ValueError("invalid_reply_schema")
    ack, guidance, reply = (data[key].strip() for key in ("acknowledgement", "guidance", "reply_text"))
    expected = ack + ((" " if ack and guidance else "") + guidance)
    if re.sub(r"\s+", "", reply) != re.sub(r"\s+", "", expected):
        raise ValueError("reply_not_composed_from_parts")
    return {"acknowledgement": ack, "guidance": guidance, "reply_text": reply}


def _fact_character_overlap(summary: str, acknowledgement: str) -> float:
    """Measure grounding while allowing ordinary Chinese paraphrases."""
    stop = set("今天已经完成表现老师学生自己比较不错情况进行记录档案")
    source = {char for char in summary if "\u4e00" <= char <= "\u9fff" and char not in stop}
    target = {char for char in acknowledgement if "\u4e00" <= char <= "\u9fff" and char not in stop}
    return len(source & target) / max(1, len(source))


def validate_fact_safety(output: dict[str, str], verified: dict[str, Any]) -> tuple[bool, str]:
    ack, guidance, reply = output["acknowledgement"], output["guidance"], output["reply_text"]
    student, summary = str(verified.get("student_name") or ""), str(verified.get("normalized_summary") or "")
    if not verified.get("record_created") or not verified.get("writeback_verified"):
        return False, "business_result_not_verified"
    if not student or student not in ack or not any(word in ack for word in SUCCESS_WORDS):
        return False, "ack_missing_verified_success_or_student"
    if not 8 <= len(reply) <= 150:
        return False, "reply_length_out_of_bounds"
    if any(word.lower() in reply.lower() for word in TECHNICAL_WORDS):
        return False, "technical_field_leak"
    if any(word in reply and word not in summary for word in UNSUPPORTED_GENERALIZATIONS):
        return False, "unsupported_generalization"
    if "家长" in reply and "家长" not in summary:
        return False, "unsupported_parent_claim"
    # The resolved student label is also a verified fact. This matters for
    # redacted fixtures and legitimate names that contain a numeric suffix.
    source_numbers = set(re.findall(r"\d+", summary + student))
    reply_numbers = set(re.findall(r"\d+", reply))
    if not reply_numbers.issubset(source_numbers):
        return False, "unsupported_number"
    if guidance and not guidance.startswith(GUIDANCE_PREFIXES):
        return False, "guidance_not_marked_as_suggestion"
    if _fact_character_overlap(summary, ack) < 0.20:
        return False, "ack_not_grounded_in_confirmed_fact"
    if not guidance and SequenceMatcher(None, summary.rstrip("。"), ack.replace(student, "").strip("，。记下来了已经记录好")).ratio() > 0.96:
        return False, "mechanical_copy"
    return True, "passed"


_PLANNER: RecordCoachResponsePlanner | None = None


def compose_verified_record_reply(*, verified: dict[str, Any], deterministic_fallback: str, planner: Any | None = None) -> ComposedReply:
    global _PLANNER
    try:
        if planner is None:
            if _PLANNER is None:
                _PLANNER = RecordCoachResponsePlanner.from_config(Path("/opt/hermes-youyi/config/config.yaml"))
            planner = _PLANNER
        scene = str(verified.get("record_scene") or "")
        payload = {
            "verified_business_result": {
                "record_created": bool(verified.get("record_created")),
                "student_name": str(verified.get("student_name") or ""),
                "record_scene": scene,
                "confirmed_facts": [str(verified.get("normalized_summary") or "")],
                "normalized_summary": str(verified.get("normalized_summary") or ""),
                "writeback_verified": bool(verified.get("writeback_verified")),
            },
            "allowed_guidance_scope": SCENE_GUIDANCE.get(scene, ["继续观察同类场景中的具体表现"]),
            "forbidden_claim_types": ["unverified_history", "parent_state", "teacher_action_not_stated", "new_numbers", "new_behavior"],
        }
        output = planner.compose(payload)
        safe, reason = validate_fact_safety(output, verified)
        if not safe and hasattr(planner, "correct_fact_safety"):
            try:
                output = planner.correct_fact_safety(payload, output, reason)
                safe, reason = validate_fact_safety(output, verified)
            except Exception as correction_error:
                reason = "fact_correction_failed:" + _safe_error(correction_error)
        if not safe:
            return ComposedReply(deterministic_fallback, "deterministic_fallback", False, "fact_safety:" + reason, schema_trace=getattr(planner, "last_trace", {}))
        return ComposedReply(output["reply_text"], "ai_coach_natural", True, "", output["acknowledgement"], output["guidance"], getattr(planner, "last_trace", {}))
    except Exception as exc:
        return ComposedReply(deterministic_fallback, "deterministic_fallback", False, type(exc).__name__, schema_trace=getattr(planner, "last_trace", {}) if planner else {})


def _safe_error(exc: Exception) -> str:
    return re.sub(r"[^A-Za-z0-9_:\-.,\[\] ']+", "?", f"{type(exc).__name__}:{exc}")[:300]
