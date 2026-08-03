#!/usr/bin/env python3
"""Read-only acceptance checks for a generated Hermes tenant package."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any


FORBIDDEN_MARKERS = (
    "优益",
    "金总",
    "李老师",
    "JinWenJie",
    "youyi_tuoguan",
    "九月份续费率",
    "/opt/hermes-youyi/data/tuoguan-data",
)

REQUIRED_DIRS = (
    "config",
    "data",
    "skills",
    "skills/institution-facts",
    "skills/institution-facts/references",
    "memory",
    "reports",
    "reports/startup",
    "backups",
    "logs",
    "runtime",
)

REQUIRED_JSON_FILES = (
    "config/tenant_profile.json",
    "data/wecom_whitelist.json",
    "data/teacher_wecom_map.json",
    "data/staff.json",
    "data/institution_operating_model.json",
    "data/academic_term_state.json",
)

REQUIRED_TEXT_FILES = (
    "config/runtime.env",
    "memory/MEMORY.md",
    "skills/institution-facts/SKILL.md",
    "skills/institution-facts/references/institution-profile.md",
)

JSONL_LEDGERS = (
    "hermes_work_items.jsonl",
    "wakeup_requests.jsonl",
    "business_events.jsonl",
    "action_executions.jsonl",
    "attention_threads.jsonl",
    "relationship_touch_candidates.jsonl",
    "daily_report_runs.jsonl",
    "hermes_employee_scorecard.jsonl",
    "value_progress_ledger.jsonl",
    "institution_fact_gap_events.jsonl",
    "industry_learning_candidates.jsonl",
    "external_research_runs.jsonl",
    "market_research_candidates.jsonl",
    "competitor_profiles.jsonl",
    "weekly_market_report_runs.jsonl",
    "agent_delegations.jsonl",
    "agent_delegation_results.jsonl",
)


class AcceptanceResult:
    def __init__(self) -> None:
        self.p0: list[str] = []
        self.warnings: list[str] = []
        self.info: list[str] = []
        self.evidence: list[str] = []

    @property
    def ok(self) -> bool:
        return not self.p0

    def fail(self, message: str) -> None:
        self.p0.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def note(self, message: str) -> None:
        self.info.append(message)

    def add_evidence(self, message: str) -> None:
        self.evidence.append(message)


def read_json(path: Path, result: AcceptanceResult) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        result.fail(f"JSON 文件无法解析：{path} ({exc})")
        return None


def check_dirs(tenant_root: Path, result: AcceptanceResult) -> None:
    for rel in REQUIRED_DIRS:
        path = tenant_root / rel
        if not path.is_dir():
            result.fail(f"缺少目录：{rel}")
        else:
            result.add_evidence(f"目录存在：{rel}")


def check_files(tenant_root: Path, result: AcceptanceResult) -> dict[str, Any]:
    loaded: dict[str, Any] = {}
    for rel in REQUIRED_JSON_FILES:
        path = tenant_root / rel
        if not path.is_file():
            result.fail(f"缺少核心 JSON 文件：{rel}")
            continue
        loaded[rel] = read_json(path, result)
        result.add_evidence(f"JSON 可读：{rel}")
    for rel in REQUIRED_TEXT_FILES:
        path = tenant_root / rel
        if not path.is_file():
            result.fail(f"缺少核心文本文件：{rel}")
        else:
            result.add_evidence(f"文本存在：{rel}")
    return loaded


def check_ledgers(tenant_root: Path, result: AcceptanceResult) -> None:
    for name in JSONL_LEDGERS:
        rel = f"data/{name}"
        path = tenant_root / rel
        if not path.is_file():
            result.fail(f"缺少空白运行账本：{rel}")
            continue
        text = path.read_text(encoding="utf-8")
        if text:
            result.fail(f"运行账本不是空白初始化：{rel}")
        else:
            result.add_evidence(f"空白账本：{rel}")


def check_identity(loaded: dict[str, Any], result: AcceptanceResult) -> tuple[str, str]:
    profile = loaded.get("config/tenant_profile.json") if isinstance(loaded.get("config/tenant_profile.json"), dict) else {}
    tenant = profile.get("tenant") if isinstance(profile.get("tenant"), dict) else {}
    tenant_id = str(tenant.get("tenant_id") or "").strip()
    institution_name = str(tenant.get("institution_name") or "").strip()
    if not tenant_id:
        result.fail("tenant_profile 缺少 tenant_id。")
    if not institution_name:
        result.fail("tenant_profile 缺少机构名称。")

    operating = loaded.get("data/institution_operating_model.json") if isinstance(loaded.get("data/institution_operating_model.json"), dict) else {}
    if tenant_id and str(operating.get("tenant_id") or "") != tenant_id:
        result.fail("institution_operating_model.json 的 tenant_id 与 tenant_profile 不一致。")
    if institution_name and str(operating.get("institution_name") or "") != institution_name:
        result.fail("institution_operating_model.json 的 institution_name 与 tenant_profile 不一致。")

    whitelist = loaded.get("data/wecom_whitelist.json") if isinstance(loaded.get("data/wecom_whitelist.json"), dict) else {}
    staff = loaded.get("data/staff.json") if isinstance(loaded.get("data/staff.json"), dict) else {}
    super_users = [str(item) for item in whitelist.get("super_users") or []]
    if not super_users:
        result.fail("wecom_whitelist.json 缺少老板 super_users。")
    user_roles = whitelist.get("user_roles") if isinstance(whitelist.get("user_roles"), dict) else {}
    for user_id in super_users:
        if user_roles.get(user_id) != "boss":
            result.fail(f"老板账号角色映射不是 boss：{user_id}")
        if user_id not in staff:
            result.fail(f"老板账号未出现在 staff.json：{user_id}")
    return tenant_id, institution_name


def check_policy(loaded: dict[str, Any], result: AcceptanceResult) -> None:
    profile = loaded.get("config/tenant_profile.json") if isinstance(loaded.get("config/tenant_profile.json"), dict) else {}
    parent = profile.get("parent_communication_policy") if isinstance(profile.get("parent_communication_policy"), dict) else {}
    if parent.get("auto_send_parent_messages") is not False:
        result.fail("家长自动发送必须关闭：parent_communication_policy.auto_send_parent_messages=false。")

    proactive = profile.get("proactive_contact_policy") if isinstance(profile.get("proactive_contact_policy"), dict) else {}
    for role in ("manager", "teacher"):
        role_policy = proactive.get(role) if isinstance(proactive.get(role), dict) else {}
        if role_policy.get("mode") != "candidate":
            result.fail(f"{role} 主动外发 V0 必须是 candidate 模式。")
    parent_policy = proactive.get("parent") if isinstance(proactive.get("parent"), dict) else {}
    if parent_policy.get("mode") not in {"disabled", None}:
        result.fail("parent 主动外发必须 disabled。")

    wecom = profile.get("wecom") if isinstance(profile.get("wecom"), dict) else {}
    for forbidden_key in ("secret", "token", "encoding_aes_key"):
        if str(wecom.get(forbidden_key) or "").strip():
            result.fail(f"企业微信明文密钥字段禁止出现：wecom.{forbidden_key}")
    for ref_key in ("secret_ref", "token_ref", "encoding_aes_key_ref"):
        value = str(wecom.get(ref_key) or "").strip()
        if value and not (value.startswith("env:") or value.startswith("secret:") or value.startswith("vault:") or value == "store_secret_outside_template"):
            result.fail(f"企业微信密钥必须是安全引用：wecom.{ref_key}")


def check_onboarding_materials(tenant_root: Path, institution_name: str, result: AcceptanceResult) -> None:
    for rel in ("memory/MEMORY.md", "skills/institution-facts/SKILL.md", "skills/institution-facts/references/institution-profile.md"):
        path = tenant_root / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        if institution_name and institution_name not in text:
            result.fail(f"入职材料未包含机构名称：{rel}")
        result.add_evidence(f"入职材料检查：{rel}")


def check_forbidden_markers(tenant_root: Path, result: AcceptanceResult) -> None:
    for path in tenant_root.rglob("*"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        found = [marker for marker in FORBIDDEN_MARKERS if marker in text]
        if found:
            result.fail(f"发现禁止携带旧机构标记：{path.relative_to(tenant_root)} -> {', '.join(found)}")


def check_warnings(loaded: dict[str, Any], result: AcceptanceResult) -> None:
    profile = loaded.get("config/tenant_profile.json") if isinstance(loaded.get("config/tenant_profile.json"), dict) else {}
    imports = profile.get("import_files") if isinstance(profile.get("import_files"), dict) else {}
    tenant = profile.get("tenant") if isinstance(profile.get("tenant"), dict) else {}
    roles = profile.get("roles") if isinstance(profile.get("roles"), dict) else {}
    if not imports.get("students"):
        result.warn("学生导入文件为空；首次启动后 Hermes 应把学生名单作为待确认资料。")
    if not tenant.get("current_top_goals"):
        result.warn("老板当前目标为空；首次启动后 Hermes 应主动询问老板优先目标。")
    if not roles.get("teachers"):
        result.warn("老师列表为空；老师看板和关系经营只能等后续补齐。")


def render_report(tenant_root: Path, tenant_id: str, institution_name: str, result: AcceptanceResult) -> str:
    status = "PASS" if result.ok else "FAIL"
    now = dt.datetime.now().astimezone().isoformat()

    def section(items: list[str], empty: str) -> str:
        if not items:
            return f"- {empty}"
        return "\n".join(f"- {item}" for item in items)

    return f"""# Tenant Acceptance Report

## Summary

- status: `{status}`
- generated: {now}
- tenant_id: `{tenant_id}`
- institution_name: {institution_name}
- tenant_root: `{tenant_root}`

## P0 Checks

{section(result.p0, "无 P0 问题")}

## Warnings

{section(result.warnings, "无 warning")}

## Generated Evidence

{section(result.evidence, "无 evidence")}

## Info

{section(result.info, "无 info")}

## Next Manual Checks

- 真实启动 Hermes 后，用老板账号确认 Hermes 能说出当前机构名称。
- 真实启动后验证企业微信收发、H5 权限、早晚报、自主唤醒和写入守卫。
- 真实启动后继续确认 Hermes 不把其他机构资料当成本机构事实。
"""


def run_acceptance(tenant_root: Path, report_dir: Path | None = None) -> tuple[AcceptanceResult, Path]:
    result = AcceptanceResult()
    if not tenant_root.is_dir():
        result.fail(f"租户目录不存在：{tenant_root}")
        tenant_id = ""
        institution_name = ""
    else:
        check_dirs(tenant_root, result)
        loaded = check_files(tenant_root, result)
        check_ledgers(tenant_root, result)
        tenant_id, institution_name = check_identity(loaded, result)
        check_policy(loaded, result)
        check_onboarding_materials(tenant_root, institution_name, result)
        check_forbidden_markers(tenant_root, result)
        check_warnings(loaded, result)
        file_count = sum(1 for path in tenant_root.rglob("*") if path.is_file())
        result.note(f"检查文件数：{file_count}")

    target_dir = report_dir or (tenant_root / "reports" / "startup")
    target_dir.mkdir(parents=True, exist_ok=True)
    report_path = target_dir / f"tenant-acceptance-report-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
    report_path.write_text(render_report(tenant_root, tenant_id, institution_name, result), encoding="utf-8")
    return result, report_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Run read-only V0 acceptance checks for a generated Hermes tenant.")
    parser.add_argument("--tenant-root", required=True)
    parser.add_argument("--report-dir", default="")
    args = parser.parse_args()

    result, report_path = run_acceptance(
        Path(args.tenant_root),
        Path(args.report_dir) if args.report_dir else None,
    )
    print(f"STATUS:{'PASS' if result.ok else 'FAIL'}")
    print(f"REPORT:{report_path}")
    if result.p0:
        print("P0:")
        for item in result.p0:
            print(f"- {item}")
    if result.warnings:
        print("WARNINGS:")
        for item in result.warnings:
            print(f"- {item}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
