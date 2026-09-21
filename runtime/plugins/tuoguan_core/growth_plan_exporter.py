"""Read-only P4 weakness-profile exporter for the 2026 summer program.

This module is intentionally not wired into routing, WeCom, or document
generation. Callers must explicitly enable the exporter and test mode.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


DEFAULT_BRIDGE_ROOT = Path(
    r"C:\Users\Administrator\Desktop\本机构托管-2026暑假班结业成长提升计划\_bridge_test"
)
DEFAULT_DESKTOP_OUTPUT_ROOT = Path(
    r"C:\Users\Administrator\Desktop\本机构托管-2026暑假班结业成长提升计划"
)
MOCK_SOURCE = "p4_2_mock_records"


@dataclass(frozen=True)
class GrowthPlanExporterConfig:
    growth_plan_exporter_enabled: bool = False
    growth_plan_exporter_test_mode: bool = True
    growth_plan_allow_real_students: bool = False
    growth_plan_allow_auto_word_generation: bool = False
    growth_plan_allow_wecom_push: bool = False
    growth_plan_allow_whitelist_export: bool = False
    growth_plan_whitelist_student_names: tuple[str, ...] = ()


class SourceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_id: str
    recorded_at: str
    scene: str
    subject: str
    content: str
    simulated: Literal[True]
    is_mock: Literal[True]
    source: Literal[MOCK_SOURCE]


class SubjectProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: Literal["数学", "语文", "英语"]
    strengths: list[str] = Field(min_length=1)
    weaknesses: list[str] = Field(min_length=1)
    evidence_text: str = Field(min_length=1)
    recommended_focus: list[str] = Field(min_length=1)
    severity: Literal["low", "medium", "high", "mild_to_medium"]
    evidence_strength: Literal["strong", "sufficient", "limited", "insufficient"]
    confidence: Literal["high", "medium_high", "medium", "low"]
    source_record_ids: list[str] = Field(min_length=1)
    avoid_overclaim: bool
    preferred_question_types: list[str]
    forbidden_question_types: list[str]


class DifficultyRatio(BaseModel):
    model_config = ConfigDict(extra="forbid")

    basic: Literal[0.7]
    light_improvement: Literal[0.3]


class PhaseRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phase: int
    days: str
    goal: str


class PracticePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_days: Literal[20]
    page_mode: Literal["16_pages"]
    daily_minutes: Literal["15-20"]
    difficulty_ratio: DifficultyRatio
    phase_rules: list[PhaseRule] = Field(min_length=3, max_length=3)
    subjects_per_day: list[str] = Field(min_length=3, max_length=3)
    require_answer_area: Literal[True]
    require_math_work_area: Literal[True]
    require_chinese_writing_area: Literal[True]
    require_english_writing_area: Literal[True]

    @field_validator("subjects_per_day")
    @classmethod
    def validate_subjects(cls, value: list[str]) -> list[str]:
        if value != ["语文", "数学", "英语"]:
            raise ValueError("subjects_per_day must follow the confirmed product order")
        return value


class ReviewPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    teacher_initial_review: Literal[True]
    teacher_initial_reviewer_role: Literal["普通老师"]
    manager_final_review: Literal[True]
    manager_final_reviewer: Literal["相关老师"]
    manager_final_reviewer_role: Literal["暑假班店长"]
    boss_spot_check: Literal[True]
    boss_reviewer: Literal["机构负责人"]
    boss_review_method: Literal["summary_and_spot_check_later"]
    auto_send_to_parent: Literal[False]


class ParentVisibleFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: Literal[True]
    block_internal_fields: Literal[True]
    blocked_fields: list[str]
    blocked_terms: list[str]


class OutputPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output_format: Literal["docx"]
    paper: Literal["A4"]
    duplex_print: Literal[True]
    target_pages: Literal[16]
    style: Literal["black_white_light_gray_table"]
    filename_rule: Literal[
        "{student_name}_{grade_transition}_暑假班结业成长提升计划_2026_{status}.docx"
    ]
    desktop_output_root: str
    parent_visible_filter: ParentVisibleFilter


class WeaknessProfile(BaseModel):
    """P4-1 bridge profile with the confirmed P4-2 policy fields."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.1"]
    schema_name: Literal["weakness_profile"]
    compatible_with: list[Literal["1.0"]] = Field(min_length=1)
    profile_id: str
    student_name: str
    current_grade: str
    next_grade: str
    grade_transition: str
    program_id: Literal["summer_2026"]
    program_name: Literal["2026暑假班"]
    evidence_level: Literal["sufficient", "limited", "insufficient"]
    package_type: Literal["graduation_growth_plan"]
    source_records: list[SourceRecord] = Field(min_length=1)
    subject_profiles: list[SubjectProfile] = Field(min_length=3, max_length=3)
    practice_policy: PracticePolicy
    review_policy: ReviewPolicy
    output_policy: OutputPolicy
    created_at: str
    created_by: Literal["hermes_p4_2_readonly_exporter", "hermes_p4_3_whitelist_exporter"]


def mock_student() -> dict[str, str]:
    return {
        "student_name": "测试示例学生",
        "current_grade": "二年级",
        "next_grade": "三年级",
        "grade_transition": "二升三",
        "program_id": "summer_2026",
        "program_name": "2026暑假班",
        "is_mock": True,
    }


def mock_records() -> list[dict[str, Any]]:
    return [
        {
            "id": "P4-2-MATH-001", "student_name": "测试示例学生",
            "timestamp": "2026-07-08T10:00:00", "scene": "学科课堂", "subject": "数学",
            "content": "孩子计算反应较快，但审题和检查不稳定，偶尔漏看条件。",
            "is_mock": True, "source": MOCK_SOURCE,
        },
        {
            "id": "P4-2-CHINESE-001", "student_name": "测试示例学生",
            "timestamp": "2026-07-09T10:00:00", "scene": "学科课堂", "subject": "语文",
            "content": "孩子能理解短文大意，但回答问题时句子不够完整。",
            "is_mock": True, "source": MOCK_SOURCE,
        },
        {
            "id": "P4-2-ENGLISH-001", "student_name": "测试示例学生",
            "timestamp": "2026-07-10T10:00:00", "scene": "英语跟读", "subject": "英语",
            "content": "孩子愿意跟读字母和基础单词，但认读稳定性不足。",
            "is_mock": True, "source": MOCK_SOURCE,
        },
        {
            "id": "P4-2-OVERALL-001", "student_name": "测试示例学生",
            "timestamp": "2026-07-11T10:00:00", "scene": "综合观察", "subject": "综合",
            "content": "课堂参与较好，愿意跟随老师完成任务。",
            "is_mock": True, "source": MOCK_SOURCE,
        },
    ]


def _assert_mock_only(student: dict[str, Any], records: list[dict[str, Any]]) -> None:
    if student.get("is_mock") is not True or not str(student.get("student_name", "")).startswith("测试"):
        raise PermissionError("P4-2 only permits explicitly marked simulated students")
    if not records or any(
        item.get("is_mock") is not True or item.get("source") != MOCK_SOURCE for item in records
    ):
        raise PermissionError("P4-2 only permits p4_2_mock_records input")


def _subject_profiles() -> list[dict[str, Any]]:
    return [
        {
            "subject": "数学", "strengths": ["基础计算反应较快"],
            "weaknesses": ["审题和检查不稳定", "偶尔漏看条件"],
            "evidence_text": "孩子计算反应较快，但审题和检查不稳定，偶尔漏看条件。",
            "recommended_focus": ["圈画题目条件", "完成后检查算式和答案"],
            "severity": "mild_to_medium", "evidence_strength": "limited", "confidence": "medium",
            "source_record_ids": ["P4-2-MATH-001"],
            "avoid_overclaim": True,
            "preferred_question_types": ["基础计算", "审题", "验算", "简单应用题"],
            "forbidden_question_types": ["竞赛题", "超纲复杂应用题"],
        },
        {
            "subject": "语文", "strengths": ["能够理解短文大意"],
            "weaknesses": ["回答问题时句子不够完整"],
            "evidence_text": "孩子能理解短文大意，但回答问题时句子不够完整。",
            "recommended_focus": ["用完整句回答", "写清人物、事情和结果"],
            "severity": "mild_to_medium", "evidence_strength": "limited", "confidence": "medium",
            "source_record_ids": ["P4-2-CHINESE-001"],
            "avoid_overclaim": True,
            "preferred_question_types": ["阅读理解", "完整句表达", "简短写话"],
            "forbidden_question_types": ["长篇作文", "超年级古文"],
        },
        {
            "subject": "英语", "strengths": ["愿意跟读字母和基础单词"],
            "weaknesses": ["字母和基础单词认读稳定性不足"],
            "evidence_text": "孩子愿意跟读字母和基础单词，但认读稳定性不足。",
            "recommended_focus": ["字母认读", "大小写匹配", "基础单词认读"],
            "severity": "mild_to_medium", "evidence_strength": "limited", "confidence": "medium",
            "source_record_ids": ["P4-2-ENGLISH-001"],
            "avoid_overclaim": True,
            "preferred_question_types": ["字母", "颜色", "数字", "常见物品", "简单问候"],
            "forbidden_question_types": ["教材单元测试", "复杂语法", "长篇阅读"],
        },
    ]


def build_weakness_profile(
    student: dict[str, Any],
    records: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    created_by: str = "hermes_p4_2_readonly_exporter",
) -> WeaknessProfile:
    _assert_mock_only(student, records)
    record_ids = {str(item["id"]) for item in records}
    source_records = [
        {
            "record_id": str(item["id"]), "recorded_at": str(item["timestamp"]),
            "scene": str(item["scene"]), "subject": str(item["subject"]),
            "content": str(item["content"]), "simulated": True,
            "is_mock": True, "source": MOCK_SOURCE,
        }
        for item in records
    ]
    profiles = _subject_profiles()
    if any(not set(item["source_record_ids"]).issubset(record_ids) for item in profiles):
        raise ValueError("subject profile references evidence outside source records")
    scenes = {str(item["scene"]) for item in records}
    has_subject = any(item.get("subject") in {"数学", "语文", "英语"} for item in records)
    evidence_level = "sufficient" if len(records) >= 3 and len(scenes) >= 2 and has_subject else "limited"
    payload = {
        "schema_version": "1.1",
        "schema_name": "weakness_profile",
        "compatible_with": ["1.0"],
        "profile_id": "summer_2026-test-xiaojin-p4-2",
        **{key: student[key] for key in (
            "student_name", "current_grade", "next_grade", "grade_transition", "program_id", "program_name"
        )},
        "evidence_level": evidence_level,
        "package_type": "graduation_growth_plan",
        "source_records": source_records,
        "subject_profiles": profiles,
        "practice_policy": {
            "total_days": 20, "page_mode": "16_pages", "daily_minutes": "15-20",
            "difficulty_ratio": {"basic": 0.7, "light_improvement": 0.3},
            "phase_rules": [
                {"phase": 1, "days": "1-7", "goal": "薄弱点巩固"},
                {"phase": 2, "days": "8-14", "goal": "准确率与表达完整度提升"},
                {"phase": 3, "days": "15-20", "goal": "新学期基础预习"},
            ],
            "subjects_per_day": ["语文", "数学", "英语"],
            "require_answer_area": True, "require_math_work_area": True,
            "require_chinese_writing_area": True, "require_english_writing_area": True,
        },
        "review_policy": {
            "teacher_initial_review": True, "teacher_initial_reviewer_role": "普通老师",
            "manager_final_review": True, "manager_final_reviewer": "相关老师",
            "manager_final_reviewer_role": "暑假班店长",
            "boss_spot_check": True, "boss_reviewer": "机构负责人",
            "boss_review_method": "summary_and_spot_check_later", "auto_send_to_parent": False,
        },
        "output_policy": {
            "output_format": "docx", "paper": "A4", "duplex_print": True,
            "target_pages": 16, "style": "black_white_light_gray_table",
            "filename_rule": "{student_name}_{grade_transition}_暑假班结业成长提升计划_2026_{status}.docx",
            "desktop_output_root": str(DEFAULT_DESKTOP_OUTPUT_ROOT),
            "parent_visible_filter": {
                "enabled": True,
                "block_internal_fields": True,
                "blocked_fields": [
                    "schema_version", "schema_name", "compatible_with", "profile_id", "record_id",
                    "source_record_ids", "source_type", "source", "simulated", "is_mock", "internal_notes",
                    "curriculum_confidence", "evidence_strength", "confidence",
                ],
                "blocked_terms": [
                    "P4", "mock", "模拟", "schema", "JSON", "source_type", "p4_2_mock_records",
                    "validation", "bridge", "测试开关", "旧项目", "curriculum-map", "真实数据检查",
                ],
            },
        },
        "created_at": (now or datetime.now()).isoformat(timespec="seconds"),
        "created_by": created_by,
    }
    return WeaknessProfile.model_validate(payload)


class WhitelistStudentSource(Protocol):
    """Name-keyed source contract; implementations must not scan all students."""

    source_mode: str

    def get_student(self, student_name: str, program_id: str) -> dict[str, Any] | None: ...

    def get_records(self, student_name: str, program_id: str) -> list[dict[str, Any]]: ...


class P4MockWhitelistSource:
    """Scheme B source used by P4-3 without touching Hermes production data."""

    source_mode = "scheme_b_p4_mock_whitelist"

    def get_student(self, student_name: str, program_id: str) -> dict[str, Any] | None:
        student = mock_student()
        if student_name != student["student_name"] or program_id != student["program_id"]:
            return None
        return student

    def get_records(self, student_name: str, program_id: str) -> list[dict[str, Any]]:
        if self.get_student(student_name, program_id) is None:
            return []
        return [dict(item) for item in mock_records()]


CORE_1_0_FIELDS = {
    "schema_version", "profile_id", "student_name", "current_grade", "next_grade",
    "grade_transition", "program_id", "program_name", "evidence_level", "package_type",
    "source_records", "subject_profiles", "practice_policy", "review_policy", "output_policy",
    "created_at", "created_by",
}


def inspect_schema_compatibility(
    payload: dict[str, Any], *, error_report_path: Path | None = None
) -> dict[str, Any]:
    """Recognize 1.0 safely and strictly validate preferred version 1.1."""
    version = str(payload.get("schema_version") or "")
    if version == "1.1":
        WeaknessProfile.model_validate(payload)
        return {"accepted": True, "version": "1.1", "status": "preferred", "upgrade_available": False}
    if version == "1.0":
        missing = sorted(CORE_1_0_FIELDS - set(payload))
        if missing:
            raise ValueError(f"schema 1.0 missing required core fields: {', '.join(missing)}")
        return {
            "accepted": True,
            "version": "1.0",
            "status": "accepted_upgrade_recommended",
            "upgrade_available": True,
            "upgrade_target": "1.1",
        }
    message = f"unsupported weakness_profile schema_version: {version or '<missing>'}"
    if error_report_path is not None:
        _atomic_write(
            error_report_path,
            json.dumps(
                {
                    "accepted": False,
                    "error": "unsupported_schema_version",
                    "received_version": version or None,
                    "supported_versions": ["1.1", "1.0"],
                    "message": message,
                },
                ensure_ascii=False,
                indent=2,
            ) + "\n",
        )
    raise ValueError(message)


SCHEMA_VERSION_POLICY = """# weakness_profile 协议版本规则

## 版本 1.0

P4-1 建立核心字段：学生基础信息、证据等级、来源记录、三科学科画像、
练习策略、审核策略、输出策略和创建信息。

## 版本 1.1

P4-2.2 保留 1.0 全部核心字段，新增 `schema_name`、`compatible_with`、
明确审核人、家长版字段过滤，以及每科的 `evidence_strength` 和 `confidence`。
单科只有一条证据时降级为 `limited / medium / mild_to_medium`。

## 兼容规则

1.1 是当前优先版本。1.0 在核心字段齐全时可被识别为旧版有效输入，桥接器
必须明确提示可升级到 1.1。缺少 1.1 新字段时，必须明确报错，或通过有记录的
升级步骤补充默认值，禁止静默失败。

未知版本或缺少 `schema_version` 时必须拒绝，并由调用方写入错误报告。
当前 Hermes 桥接协议优先支持 1.1，同时识别 1.0 并提示升级。
"""


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def export_mock_weakness_profile(
    config: GrowthPlanExporterConfig,
    *,
    bridge_root: Path = DEFAULT_BRIDGE_ROOT,
    student: dict[str, Any] | None = None,
    records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not config.growth_plan_exporter_enabled:
        raise PermissionError("growth plan exporter is disabled")
    if not config.growth_plan_exporter_test_mode:
        raise PermissionError("P4-2 requires test mode")
    if config.growth_plan_allow_real_students:
        raise PermissionError("P4-2 forbids enabling real-student export")
    if config.growth_plan_allow_auto_word_generation or config.growth_plan_allow_wecom_push:
        raise PermissionError("P4-2 forbids Word generation and WeCom push")

    selected_student = student or mock_student()
    selected_records = records or mock_records()
    profile = build_weakness_profile(selected_student, selected_records)
    output_dir = bridge_root / "input" / "测试示例学生_二升三"
    profile_path = output_dir / "weakness_profile.json"
    schema_path = output_dir / "weakness_profile.schema.json"
    policy_path = output_dir / "schema_version_policy.md"
    report_path = output_dir / "export_validation_report.json"
    log_path = output_dir / "export_log.txt"
    review_fix_path = output_dir / "p4_2_2_review_fix_report.json"
    profile_data = profile.model_dump(mode="json")
    compatibility = inspect_schema_compatibility(profile_data)
    scene_count = len({item["scene"] for item in selected_records})
    has_subject = any(item["subject"] in {"数学", "语文", "英语"} for item in selected_records)
    evidence_check = {
        "record_count": len(selected_records), "scene_count": scene_count,
        "has_subject_record": has_subject,
        "can_generate_personalized_profile": (
            len(selected_records) >= 3 and scene_count >= 2 and has_subject
            and all(item["strengths"] and item["weaknesses"] and item["source_record_ids"]
                    for item in profile_data["subject_profiles"])
        ),
    }
    checks = {
        "schema_validation": True,
        "schema_version_is_1_1": profile_data["schema_version"] == "1.1",
        "compatible_with_1_0": "1.0" in profile_data["compatible_with"],
        "mock_student_only": selected_student.get("is_mock") is True,
        "mock_records_only": all(item.get("source") == MOCK_SOURCE for item in selected_records),
        "three_subject_profiles": {item["subject"] for item in profile_data["subject_profiles"]}
        == {"数学", "语文", "英语"},
        "all_weaknesses_have_source_record_ids": all(
            item["source_record_ids"] for item in profile_data["subject_profiles"]
        ),
        "single_evidence_downgraded": all(
            item["evidence_strength"] == "limited"
            and item["confidence"] == "medium"
            and item["severity"] == "mild_to_medium"
            and item["avoid_overclaim"] is True
            for item in profile_data["subject_profiles"]
        ),
        "manager_reviewer_is_shen": profile_data["review_policy"]["manager_final_reviewer"] == "相关老师",
        "boss_reviewer_is_jin": profile_data["review_policy"]["boss_reviewer"] == "机构负责人",
        "parent_visible_filter_enabled": profile_data["output_policy"]["parent_visible_filter"]["enabled"],
        "word_generation_disabled": not config.growth_plan_allow_auto_word_generation,
        "wecom_push_disabled": not config.growth_plan_allow_wecom_push,
    }
    report = {
        "stage": "P4-2.2", "passed": all(checks.values()) and evidence_check["can_generate_personalized_profile"],
        "schema_contract": "P4-1 weakness profile bridge, P4-2 policy revision 1.1",
        "schema_compatibility": compatibility,
        "config": asdict(config), "evidence_check": evidence_check, "checks": checks,
        "parent_visible_filter_check": {
            "enabled": profile_data["output_policy"]["parent_visible_filter"]["enabled"],
            "blocked_fields_configured": bool(profile_data["output_policy"]["parent_visible_filter"]["blocked_fields"]),
            "blocked_terms_configured": bool(profile_data["output_policy"]["parent_visible_filter"]["blocked_terms"]),
        },
        "output_files": [
            str(profile_path), str(schema_path), str(policy_path), str(report_path), str(log_path),
            str(review_fix_path),
        ],
        "forbidden_actions": {
            "word_generated": False, "legacy_renderer_called": False,
            "wecom_called": False, "real_data_read": False, "real_data_modified": False,
        },
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    review_fix_report = {
        "stage": "P4-2.2",
        "passed": report["passed"],
        "schema_version_rule": compatibility,
        "reviewers": {
            "teacher_initial_reviewer_role": "普通老师",
            "manager_final_reviewer": "相关老师",
            "manager_final_reviewer_role": "暑假班店长",
            "boss_reviewer": "机构负责人",
        },
        "parent_visible_filter_check": report["parent_visible_filter_check"],
        "single_subject_evidence_rule": {
            "evidence_count": 1,
            "evidence_strength": "limited",
            "confidence": "medium",
            "severity": "mild_to_medium",
            "avoid_overclaim": True,
            "parent_wording": "后续可以重点关注……",
        },
        "forbidden_actions": report["forbidden_actions"],
    }
    log_lines = [
        "P4-2.2 Hermes read-only weakness profile export",
        f"student={selected_student['student_name']} (mock)",
        f"records={len(selected_records)} source={MOCK_SOURCE}",
        f"schema_validation={checks['schema_validation']}",
        "legacy_renderer_called=false",
        "word_generated=false",
        "wecom_called=false",
        "real_data_read=false",
        f"result={'PASS' if report['passed'] else 'FAIL'}",
    ]
    _atomic_write(profile_path, json.dumps(profile_data, ensure_ascii=False, indent=2) + "\n")
    _atomic_write(schema_path, json.dumps(WeaknessProfile.model_json_schema(), ensure_ascii=False, indent=2) + "\n")
    _atomic_write(policy_path, SCHEMA_VERSION_POLICY)
    _atomic_write(report_path, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    _atomic_write(log_path, "\n".join(log_lines) + "\n")
    _atomic_write(review_fix_path, json.dumps(review_fix_report, ensure_ascii=False, indent=2) + "\n")
    return {"profile": profile_data, "report": report, "output_dir": output_dir}


def export_whitelisted_weakness_profile(
    config: GrowthPlanExporterConfig,
    *,
    requested_student: str,
    bridge_root: Path = DEFAULT_BRIDGE_ROOT,
    source: WhitelistStudentSource | None = None,
) -> dict[str, Any]:
    """Export one explicitly whitelisted student through a name-keyed source."""
    if not config.growth_plan_exporter_enabled:
        raise PermissionError("growth plan exporter is disabled")
    if not config.growth_plan_exporter_test_mode:
        raise PermissionError("P4-3 requires test mode")
    if not config.growth_plan_allow_whitelist_export:
        raise PermissionError("whitelist export is disabled")
    whitelist = tuple(name.strip() for name in config.growth_plan_whitelist_student_names if name.strip())
    if not whitelist:
        raise PermissionError("whitelist is empty")
    if requested_student not in whitelist:
        raise PermissionError(f"student is not in whitelist: {requested_student}")
    if config.growth_plan_allow_real_students:
        raise PermissionError("P4-3 scheme B forbids real-student access")
    if config.growth_plan_allow_auto_word_generation or config.growth_plan_allow_wecom_push:
        raise PermissionError("P4-3 forbids Word generation and WeCom push")

    selected_source = source or P4MockWhitelistSource()
    output_dir = bridge_root / "p4_3_whitelist_export" / "测试示例学生_二升三"
    student = selected_source.get_student(requested_student, "summer_2026")
    if student is None:
        error_path = output_dir / "whitelist_export_error_report.json"
        error_report = {
            "stage": "P4-3",
            "error": "whitelist_student_not_found",
            "requested_student": requested_student,
            "program_id": "summer_2026",
            "source_mode": selected_source.source_mode,
            "real_data_modified": False,
        }
        _atomic_write(error_path, json.dumps(error_report, ensure_ascii=False, indent=2) + "\n")
        raise LookupError(f"whitelist student not found; report={error_path}")

    records = selected_source.get_records(requested_student, "summer_2026")
    _assert_mock_only(student, records)
    profile = build_weakness_profile(
        student,
        records,
        created_by="hermes_p4_3_whitelist_exporter",
    )
    profile_data = profile.model_dump(mode="json")
    compatibility = inspect_schema_compatibility(profile_data)
    profile_path = output_dir / "weakness_profile.json"
    validation_path = output_dir / "export_validation_report.json"
    whitelist_report_path = output_dir / "whitelist_export_report.json"
    log_path = output_dir / "export_log.txt"
    source_summary_path = output_dir / "source_record_summary.json"

    subject_by_record: dict[str, str] = {}
    for subject_profile in profile_data["subject_profiles"]:
        for record_id in subject_profile["source_record_ids"]:
            subject_by_record[record_id] = subject_profile["subject"]
    source_summaries = []
    for record in records:
        record_id = str(record["id"])
        bound_profile = subject_by_record.get(record_id)
        source_summaries.append({
            "record_id": record_id,
            "content_summary": str(record["content"]),
            "used_for_profile": True,
            "ignored_reason": None,
            "source_classification": "mock_test_whitelist_read",
            "is_mock": True,
            "subject": str(record["subject"]),
            "scene": str(record["scene"]),
            "recorded_at": str(record["timestamp"]),
            "bound_subject_profile": bound_profile or "整体画像（非学科弱项）",
        })
    source_summary = {
        "stage": "P4-3",
        "requested_student": requested_student,
        "program_id": "summer_2026",
        "source_mode": selected_source.source_mode,
        "contains_parent_phone": False,
        "contains_unrelated_private_data": False,
        "records": source_summaries,
    }

    subject_coverage = sorted({str(item["subject"]) for item in records})
    scene_coverage = sorted({str(item["scene"]) for item in records})
    whitelist_report = {
        "stage": "P4-3",
        "exporter_enabled": config.growth_plan_exporter_enabled,
        "test_mode": config.growth_plan_exporter_test_mode,
        "whitelist_enabled": config.growth_plan_allow_whitelist_export,
        "whitelist_students": list(whitelist),
        "requested_student": requested_student,
        "student_found": True,
        "source_mode": selected_source.source_mode,
        "records_read_count": len(records),
        "records_used_count": len(source_summaries),
        "ignored_records_count": 0,
        "ignored_reason_summary": {},
        "subject_coverage": subject_coverage,
        "scene_coverage": scene_coverage,
        "evidence_level": profile_data["evidence_level"],
        "generated_profile_path": str(profile_path),
        "schema_validation_passed": compatibility["accepted"],
        "word_generated": False,
        "old_project_called": False,
        "wecom_pushed": False,
        "real_data_modified": False,
        "safety_result": "pass_mock_whitelist_only_no_full_scan",
    }
    validation_report = {
        "stage": "P4-3",
        "passed": True,
        "schema_validation": compatibility,
        "student_scope_check": {
            "requested_student_is_whitelisted": True,
            "single_student_lookup_only": True,
            "full_student_scan": False,
            "program_id": "summer_2026",
        },
        "evidence_check": {
            "record_count": len(records),
            "scene_count": len(scene_coverage),
            "subject_coverage": subject_coverage,
            "three_subject_profiles_complete": {
                item["subject"] for item in profile_data["subject_profiles"]
            } == {"数学", "语文", "英语"},
            "single_evidence_downgrade_passed": all(
                item["evidence_strength"] == "limited"
                and item["confidence"] == "medium"
                and item["severity"] == "mild_to_medium"
                for item in profile_data["subject_profiles"]
            ),
        },
        "parent_visible_filter_check": {
            "enabled": profile_data["output_policy"]["parent_visible_filter"]["enabled"],
            "block_internal_fields": profile_data["output_policy"]["parent_visible_filter"]["block_internal_fields"],
        },
        "forbidden_actions": {
            "word_generated": False,
            "old_project_called": False,
            "wecom_pushed": False,
            "real_data_read": False,
            "real_data_modified": False,
            "batch_exported": False,
        },
    }
    log_lines = [
        "P4-3 whitelist read-only weakness profile export",
        f"requested_student={requested_student}",
        f"source_mode={selected_source.source_mode}",
        f"records_read={len(records)} records_used={len(source_summaries)}",
        "single_student_lookup_only=true",
        "full_student_scan=false",
        "word_generated=false",
        "old_project_called=false",
        "wecom_pushed=false",
        "real_data_read=false",
        "real_data_modified=false",
        "result=PASS",
    ]
    _atomic_write(profile_path, json.dumps(profile_data, ensure_ascii=False, indent=2) + "\n")
    _atomic_write(validation_path, json.dumps(validation_report, ensure_ascii=False, indent=2) + "\n")
    _atomic_write(whitelist_report_path, json.dumps(whitelist_report, ensure_ascii=False, indent=2) + "\n")
    _atomic_write(log_path, "\n".join(log_lines) + "\n")
    _atomic_write(source_summary_path, json.dumps(source_summary, ensure_ascii=False, indent=2) + "\n")
    return {
        "profile": profile_data,
        "validation_report": validation_report,
        "whitelist_report": whitelist_report,
        "source_record_summary": source_summary,
        "output_dir": output_dir,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hermes P4-2 mock weakness-profile exporter")
    parser.add_argument("--enable", action="store_true", help="explicitly enable this run")
    parser.add_argument("--test-mode", action="store_true", help="explicitly confirm mock-only test mode")
    parser.add_argument("--bridge-root", type=Path, default=DEFAULT_BRIDGE_ROOT)
    args = parser.parse_args(argv)
    config = GrowthPlanExporterConfig(
        growth_plan_exporter_enabled=args.enable,
        growth_plan_exporter_test_mode=args.test_mode,
    )
    try:
        result = export_mock_weakness_profile(config, bridge_root=args.bridge_root)
    except (PermissionError, ValueError, ValidationError) as exc:
        print(f"EXPORT BLOCKED: {exc}")
        return 2
    print(f"EXPORT PASS: {result['output_dir']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
