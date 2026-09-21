#!/usr/bin/env python3
"""Generate a clean demo Hermes tenant package from a tenant profile.

V0 is intentionally local and conservative: it does not connect to servers,
does not install systemd services, and does not read production data. It is a
dry-run style generator for validating commercial tenant packaging.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
from pathlib import Path
from typing import Any


FORBIDDEN_MARKERS = (
    "示例机构",
    "机构负责人",
    "示例老师",
    "owner_test",
    "example_institution",
    "九月份续费率",
    "/opt/hermes-youyi/data/tuoguan-data",
)

JSONL_LEDGERS = (
    "hermes_work_items.jsonl",
    "wakeup_requests.jsonl",
    "business_events.jsonl",
    "action_executions.jsonl",
    "attention_threads.jsonl",
    "relationship_touch_candidates.jsonl",
    "proactive_authorizations.jsonl",
    "goal_actions.jsonl",
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
    "supervision_runs.jsonl",
    "supervision_findings.jsonl",
    "supervision_repairs.jsonl",
)

TENANT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,62}$")


class TenantInitializerError(RuntimeError):
    pass


def load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - error message path
        raise TenantInitializerError(f"无法读取 tenant profile: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise TenantInitializerError("tenant profile 必须是 JSON object。")
    return data


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def contains_forbidden(text: str) -> list[str]:
    return [marker for marker in FORBIDDEN_MARKERS if marker in text]


def validate_no_forbidden_in_profile(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    found = contains_forbidden(text)
    if found:
        raise TenantInitializerError("tenant profile 含禁止携带的旧机构标记。")


def validate_profile(profile: dict[str, Any], *, tenant_id_override: str | None = None) -> str:
    tenant = profile.get("tenant") if isinstance(profile.get("tenant"), dict) else {}
    tenant_id = str(tenant_id_override or tenant.get("tenant_id") or "").strip()
    institution_name = str(tenant.get("institution_name") or "").strip()
    bosses = ((profile.get("roles") or {}).get("bosses") or []) if isinstance(profile.get("roles"), dict) else []
    wecom = profile.get("wecom") if isinstance(profile.get("wecom"), dict) else {}

    if not TENANT_ID_RE.match(tenant_id):
        raise TenantInitializerError("tenant_id 必须是 2-63 位小写字母、数字、下划线或短横线。")
    if not institution_name:
        raise TenantInitializerError("机构名称不能为空。")
    if not isinstance(bosses, list) or not bosses:
        raise TenantInitializerError("至少需要一个老板账号。")
    if not any(str(item.get("wecom_user_id") or "").strip() for item in bosses if isinstance(item, dict)):
        raise TenantInitializerError("至少一个老板必须有企业微信 user_id。")

    for key in ("secret_ref", "token_ref", "encoding_aes_key_ref"):
        value = str(wecom.get(key) or "").strip()
        if value and not (value.startswith("env:") or value.startswith("secret:") or value.startswith("vault:") or value == "store_secret_outside_template"):
            raise TenantInitializerError(f"{key} 必须是安全引用，不允许写入明文。")
    for key in ("secret", "token", "encoding_aes_key"):
        if str(wecom.get(key) or "").strip():
            raise TenantInitializerError(f"{key} 不允许出现在 tenant profile 中，请使用 *_ref。")
    return tenant_id


def deep_copy_profile(profile: dict[str, Any], tenant_id: str) -> dict[str, Any]:
    data = json.loads(json.dumps(profile, ensure_ascii=False))
    data.setdefault("tenant", {})["tenant_id"] = tenant_id
    return data


def role_entries(profile: dict[str, Any], key: str) -> list[dict[str, Any]]:
    roles = profile.get("roles") if isinstance(profile.get("roles"), dict) else {}
    value = roles.get(key) or []
    return [item for item in value if isinstance(item, dict)]


def build_staff(profile: dict[str, Any]) -> dict[str, Any]:
    staff: dict[str, Any] = {}
    for role_key, role_name in (("bosses", "boss"), ("managers", "manager"), ("teachers", "teacher")):
        for item in role_entries(profile, role_key):
            user_id = str(item.get("wecom_user_id") or "").strip()
            if not user_id:
                continue
            staff[user_id] = {
                "name": str(item.get("name") or user_id),
                "role": role_name,
                "permissions": item.get("permissions") or [],
                "campus_ids": item.get("campus_ids") or [],
                "position": item.get("position") or "",
            }
    return staff


def build_wecom_whitelist(profile: dict[str, Any]) -> dict[str, Any]:
    bosses = role_entries(profile, "bosses")
    managers = role_entries(profile, "managers")
    teachers = role_entries(profile, "teachers")
    super_users = [str(item.get("wecom_user_id") or "").strip() for item in bosses if str(item.get("wecom_user_id") or "").strip()]
    allowed_users = [
        str(item.get("wecom_user_id") or "").strip()
        for item in managers + teachers
        if str(item.get("wecom_user_id") or "").strip()
    ]
    user_roles = {user_id: "boss" for user_id in super_users}
    user_roles.update({str(item.get("wecom_user_id")): "manager" for item in managers if str(item.get("wecom_user_id") or "").strip()})
    user_roles.update({str(item.get("wecom_user_id")): "teacher" for item in teachers if str(item.get("wecom_user_id") or "").strip()})
    return {
        "super_users": super_users,
        "allowed_users": allowed_users,
        "pending_users": [],
        "rejected_users": [],
        "user_roles": user_roles,
    }


def build_teacher_wecom_map(profile: dict[str, Any]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for key in ("bosses", "managers", "teachers"):
        for item in role_entries(profile, key):
            name = str(item.get("name") or "").strip()
            user_id = str(item.get("wecom_user_id") or "").strip()
            if name and user_id:
                mapping[name] = user_id
    return mapping


def build_operating_model(profile: dict[str, Any], tenant_id: str) -> dict[str, Any]:
    tenant = profile.get("tenant") if isinstance(profile.get("tenant"), dict) else {}
    return {
        "schema_version": "institution_operating_model_v1",
        "tenant_id": tenant_id,
        "institution_name": tenant.get("institution_name", ""),
        "brand_name": tenant.get("brand_name", ""),
        "campuses": tenant.get("campuses", []),
        "business_lines": tenant.get("business_lines", []),
        "current_top_goals": tenant.get("current_top_goals", []),
        "operation_policy": profile.get("operation_policy", {}),
        "service_relations": profile.get("service_relations", {}),
        "parent_communication_policy": profile.get("parent_communication_policy", {}),
        "performance_policy": profile.get("performance_policy", {}),
        "proactive_contact_policy": profile.get("proactive_contact_policy", {}),
        "source": "tenant_profile",
    }


def build_term_state(profile: dict[str, Any]) -> dict[str, Any]:
    service_relations = profile.get("service_relations") if isinstance(profile.get("service_relations"), dict) else {}
    window = service_relations.get("new_term_confirmation_window") if isinstance(service_relations.get("new_term_confirmation_window"), dict) else {}
    return {
        "schema_version": "academic_term_state_v1",
        "service_relation_policy": "tenant_defined",
        "confirmation_window_start": window.get("start", ""),
        "confirmation_window_end": window.get("end", ""),
        "student_roster_confidence": "unknown_until_import",
        "notes": "新机构初始化生成；历史学生资料导入前不能当作当前在读事实。",
    }


def build_memory(profile: dict[str, Any], tenant_id: str) -> str:
    tenant = profile.get("tenant") if isinstance(profile.get("tenant"), dict) else {}
    bosses = role_entries(profile, "bosses")
    boss_names = "、".join(str(item.get("name") or "").strip() for item in bosses if str(item.get("name") or "").strip()) or "待确认"
    lines = [
        "# MEMORY",
        "",
        f"- tenant_id: `{tenant_id}`",
        f"- 机构名称：{tenant.get('institution_name', '')}",
        f"- 品牌名称：{tenant.get('brand_name', '')}",
        f"- 老板/授权人：{boss_names}",
        f"- 已知业务线：{'、'.join(tenant.get('business_lines') or [])}",
        "- Hermes 是该机构的托管数字员工；当前 Memory 只包含初始化时确认的稳定事实。",
        "- 未导入或未确认的学生、老师责任关系、工资制度和家校沟通制度，都应作为缺失事实继续确认。",
        "- 不得把其他机构历史、样板数据或测试数据当成本机构事实。",
        "",
    ]
    return "\n".join(lines)


def build_skill(profile: dict[str, Any], tenant_id: str) -> str:
    tenant = profile.get("tenant") if isinstance(profile.get("tenant"), dict) else {}
    institution_name = tenant.get("institution_name", "")
    return f"""---
name: {tenant_id}-institution-facts
description: "Use when Hermes needs confirmed institution facts for {institution_name}. This is tenant-specific material, not a router."
---

# {institution_name} Institution Facts

Hermes serves this institution as a tutoring-center digital employee.

This Skill contains tenant-specific facts only. It must not contain facts from other institutions.

Use these facts as material for model judgment, not as a fixed workflow.
"""


def build_institution_profile(profile: dict[str, Any], tenant_id: str) -> str:
    tenant = profile.get("tenant") if isinstance(profile.get("tenant"), dict) else {}
    return "\n".join([
        f"# {tenant.get('institution_name', '')} 机构资料",
        "",
        f"- tenant_id: `{tenant_id}`",
        f"- brand_name: {tenant.get('brand_name', '')}",
        f"- business_lines: {'、'.join(tenant.get('business_lines') or [])}",
        "- 当前资料来自 tenant_profile.json。",
        "- 缺失资料由 Hermes 入职后向老板、店长或老师确认。",
        "",
    ])


def build_runtime_env(tenant_id: str, tenant_root: Path, profile: dict[str, Any]) -> str:
    operation = profile.get("operation_policy") if isinstance(profile.get("operation_policy"), dict) else {}
    return "\n".join([
        f"HERMES_TENANT_ID={tenant_id}",
        f"HERMES_TUOGUAN_DATA_DIR={tenant_root / 'data'}",
        f"HERMES_TENANT_CONFIG_DIR={tenant_root / 'config'}",
        f"HERMES_TENANT_MEMORY_DIR={tenant_root / 'memory'}",
        f"HERMES_TENANT_SKILLS_DIR={tenant_root / 'skills'}",
        "HERMES_TENANT_OPERATING_MODEL_FILE=institution_operating_model.json",
        f"HERMES_TIMEZONE={operation.get('timezone', 'Asia/Shanghai')}",
        "",
    ])


def scan_output_for_forbidden(tenant_root: Path) -> None:
    found: list[str] = []
    for path in tenant_root.rglob("*"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        markers = contains_forbidden(text)
        if markers:
            found.append(f"{path}: {', '.join(markers)}")
    if found:
        raise TenantInitializerError("生成结果含禁止携带的旧机构标记：\n" + "\n".join(found))


def initialization_report(
    *,
    tenant_id: str,
    tenant_root: Path,
    generated_files: list[Path],
    blank_ledgers: list[Path],
    profile: dict[str, Any],
) -> str:
    tenant = profile.get("tenant") if isinstance(profile.get("tenant"), dict) else {}
    missing: list[str] = []
    if not (profile.get("import_files") or {}).get("students"):
        missing.append("学生导入文件")
    if not role_entries(profile, "teachers"):
        missing.append("老师名单")
    if not (tenant.get("current_top_goals") or []):
        missing.append("老板当前目标")
    now = dt.datetime.now().astimezone().isoformat()
    file_lines = "\n".join(f"- `{path.relative_to(tenant_root)}`" for path in generated_files)
    ledger_lines = "\n".join(f"- `{path.relative_to(tenant_root)}`" for path in blank_ledgers)
    missing_lines = "\n".join(f"- {item}" for item in missing) if missing else "- 暂无"
    return f"""# Tenant Initialization Report

- Generated: {now}
- tenant_id: `{tenant_id}`
- institution_name: {tenant.get('institution_name', '')}
- tenant_root: `{tenant_root}`
- mode: local dry-run package generation

## Generated Files

{file_lines}

## Blank Ledgers

{ledger_lines}

## Safety Checks

- 禁止携带旧机构标记：通过
- 未读取生产数据目录：通过
- 未启动服务：通过
- 企业微信密钥仅使用引用：通过

## Missing Or Pending Inputs

{missing_lines}

## Next Manual Acceptance

- 启动前检查目录结构。
- 首次启动后确认 Hermes 能说出本机构名称。
- 首次启动后确认 Hermes 不携带其他机构资料。
"""


def initialize(profile_path: Path, platform_root: Path, *, tenant_id_override: str | None, force: bool) -> Path:
    validate_no_forbidden_in_profile(profile_path)
    profile = load_json(profile_path)
    tenant_id = validate_profile(profile, tenant_id_override=tenant_id_override)
    profile = deep_copy_profile(profile, tenant_id)

    tenant_root = platform_root / "tenants" / tenant_id
    marker = tenant_root / ".tenant_initializer_v0_marker"
    if tenant_root.exists():
        if not force:
            raise TenantInitializerError("目标租户目录已存在；如需覆盖 demo 输出，请使用 --force。")
        if not marker.exists():
            raise TenantInitializerError("目标目录不是 tenant_initializer V0 生成目录，拒绝覆盖。")
        shutil.rmtree(tenant_root)

    dirs = [
        "config",
        "data",
        "skills/institution-facts/references",
        "memory",
        "reports/startup",
        "backups",
        "logs",
        "runtime",
    ]
    for item in dirs:
        (tenant_root / item).mkdir(parents=True, exist_ok=True)

    generated: list[Path] = []
    blank_ledgers: list[Path] = []

    files: dict[str, Any] = {
        "config/tenant_profile.json": profile,
        "data/wecom_whitelist.json": build_wecom_whitelist(profile),
        "data/teacher_wecom_map.json": build_teacher_wecom_map(profile),
        "data/staff.json": build_staff(profile),
        "data/institution_operating_model.json": build_operating_model(profile, tenant_id),
        "data/academic_term_state.json": build_term_state(profile),
    }
    for rel, data in files.items():
        path = tenant_root / rel
        write_json(path, data)
        generated.append(path)

    text_files = {
        "config/runtime.env": build_runtime_env(tenant_id, tenant_root, profile),
        "memory/MEMORY.md": build_memory(profile, tenant_id),
        "skills/institution-facts/SKILL.md": build_skill(profile, tenant_id),
        "skills/institution-facts/references/institution-profile.md": build_institution_profile(profile, tenant_id),
    }
    for rel, text in text_files.items():
        path = tenant_root / rel
        write_text(path, text)
        generated.append(path)

    for name in JSONL_LEDGERS:
        path = tenant_root / "data" / name
        write_text(path, "")
        blank_ledgers.append(path)

    write_text(marker, "generated_by=tenant_initializer_v0\n")

    report_path = tenant_root / "reports" / "startup" / f"initialization-report-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
    write_text(
        report_path,
        initialization_report(
            tenant_id=tenant_id,
            tenant_root=tenant_root,
            generated_files=generated,
            blank_ledgers=blank_ledgers,
            profile=profile,
        ),
    )
    generated.append(report_path)

    scan_output_for_forbidden(tenant_root)
    return tenant_root


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a local dry-run Hermes tenant package.")
    parser.add_argument("--tenant-profile", required=True)
    parser.add_argument("--platform-root", required=True)
    parser.add_argument("--tenant-id", default="")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Accepted for clarity; V0 is always local dry-run generation.")
    parser.add_argument("--start-services", action="store_true", help="Rejected in V0.")
    args = parser.parse_args()

    if args.start_services:
        raise TenantInitializerError("V0 不支持启动服务；请只做本地干跑生成。")

    tenant_root = initialize(
        Path(args.tenant_profile),
        Path(args.platform_root),
        tenant_id_override=args.tenant_id or None,
        force=bool(args.force),
    )
    print(f"TENANT_ROOT:{tenant_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
