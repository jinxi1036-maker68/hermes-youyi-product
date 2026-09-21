#!/usr/bin/env python3
"""Run a non-Youyi tenant isolation and capability acceptance gate."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE = ROOT / "work/commercialization/demo_tenant_profile.json"
FORBIDDEN_MARKERS = (
    "example_institution", "owner_test", "teacher_test", "机构负责人", "示例老师", "示例机构",
    "/opt/hermes-youyi/data/tuoguan-data",
)


PROBE = r'''
import json
import os
from datetime import datetime
from pathlib import Path

from plugins.tuoguan_core.capability_facades import operation_manifest
from plugins.tuoguan_core.daily_reporter import build_daily_boss_report
from plugins.tuoguan_core.dashboard_builder import build_dashboard_snapshot
from plugins.tuoguan_core.digital_employee_state import query_xiaoyou_health
from plugins.tuoguan_core.identity import IdentityService
from plugins.tuoguan_core.proactive_work import query_proactive_authorizations, submit_proactive_authorization
from plugins.tuoguan_core.staff_directory import query_staff_directory
from plugins.tuoguan_core.store import TuoguanStore
from plugins.tuoguan_core.tenant_context import current_tenant_id
from plugins.tuoguan_core.write_guard import authorized_system_write
from plugins.tuoguan_core.youyi_batch_capabilities import create_assigned_task

data_dir = Path(os.environ["HERMES_TUOGUAN_DATA_DIR"])
store = TuoguanStore(data_dir)
identities = IdentityService(store)
boss = identities.resolve("wecom_callback", "demo_boss_001", user_name="周园长")
teacher = identities.resolve("wecom_callback", "demo_teacher_001", user_name="王老师")
errors = []
if current_tenant_id() != "demo_tuoguan":
    errors.append("tenant_context_mismatch")
if (boss.role, boss.approval_state) != ("boss", "approved"):
    errors.append("boss_identity_mismatch")
if (teacher.role, teacher.approval_state) != ("teacher", "approved"):
    errors.append("teacher_identity_mismatch")
directory = query_staff_directory(store, query="企业微信里都有谁", include_inactive=True)
if directory.get("result_count", 0) < 4:
    errors.append("staff_directory_incomplete")
with authorized_system_write(data_dir, job_name="non_youyi_gate", allowed_files={"tasks.json", "proactive_authorizations.jsonl"}):
    task = create_assigned_task(
        store,
        title="整理一条学生服务事实",
        assignee_user_id="demo_teacher_001",
        created_by="demo_boss_001",
        created_by_role="boss",
        created_by_name="周园长",
        source_text="请王老师整理一条学生服务事实，作为第二租户验收。",
        evidence_requirement="说明事实来源和下一步。",
    )
    authorization = submit_proactive_authorization(
        store,
        identity=boss,
        operation_id="non-youyi-gate-auth-1",
        subject_role="teacher",
        subject_user_ids=["demo_teacher_001"],
        action_types=["ask_task_fact"],
        daily_limit=1,
        source_text="第二租户隔离验收授权。",
    )
if not task.get("ok") or not task.get("writeback_verified"):
    errors.append("task_writeback_failed")
if not authorization.get("ok") or not authorization.get("writeback_verified"):
    errors.append("proactive_authorization_failed")
authorization_query = query_proactive_authorizations(store, identity=boss, include_inactive=True)
if authorization_query.get("authorization_count", 0) != 1:
    errors.append("proactive_authorization_query_failed")
report = build_daily_boss_report("morning", store=store, now=datetime.fromisoformat("2026-08-13T08:30:00+08:00"))
if not report.get("ok") or not str(report.get("content") or "").strip():
    errors.append("daily_report_failed")
health = query_xiaoyou_health(store, identity=boss, now_at="2026-08-13T09:00:00+08:00")
if not health.get("ok") or health.get("boundary", {}).get("sends_messages") is not False:
    errors.append("health_boundary_failed")
dashboard = build_dashboard_snapshot(store, now=datetime.fromisoformat("2026-08-13T09:00:00"))
if not isinstance(dashboard, dict) or not isinstance(dashboard.get("boss_dashboard"), dict):
    errors.append("dashboard_failed")
manifest = operation_manifest()
if int(manifest.get("domains") or 0) != 12:
    errors.append("capability_manifest_not_compact")
memory_path = data_dir.parent / "memory/MEMORY.md"
if not memory_path.is_file():
    errors.append("tenant_memory_missing")
print(json.dumps({
    "ok": not errors,
    "errors": errors,
    "tenant_id": current_tenant_id(),
    "boss": {"user_id": boss.canonical_user_id, "role": boss.role},
    "teacher": {"user_id": teacher.canonical_user_id, "role": teacher.role},
    "staff_count": directory.get("result_count"),
    "task_writeback_verified": bool(task.get("writeback_verified")),
    "proactive_authorization_count": authorization_query.get("authorization_count"),
    "daily_report_line_count": len(str(report.get("content") or "").splitlines()),
    "dashboard_roles": ["boss", "manager", "teacher"],
    "capability_domain_count": int(manifest.get("domains") or 0),
    "health_read_only": health.get("read_only"),
}, ensure_ascii=False))
raise SystemExit(0 if not errors else 1)
'''


def _run(command: list[str], *, env: dict[str, str] | None = None) -> dict[str, Any]:
    completed = subprocess.run(
        command, cwd=ROOT, env=env or os.environ.copy(), text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
    )
    return {"command": command, "returncode": completed.returncode, "output": completed.stdout[-8000:]}


def _pollution_scan(tenant_root: Path) -> list[dict[str, str]]:
    hits: list[dict[str, str]] = []
    for path in tenant_root.rglob("*"):
        if not path.is_file() or path.stat().st_size > 2_000_000:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        for marker in FORBIDDEN_MARKERS:
            if marker in content:
                hits.append({"path": path.relative_to(tenant_root).as_posix(), "marker": marker})
    return hits


def run_gate(*, profile: Path = DEFAULT_PROFILE, work_dir: Path | None = None) -> dict[str, Any]:
    owner = None if work_dir else tempfile.TemporaryDirectory(prefix="xiaoyou-non-youyi-")
    platform_root = work_dir or Path(owner.name)
    initialized = _run([
        sys.executable, str(ROOT / "scripts/tenant_initializer.py"),
        "--tenant-profile", str(profile), "--platform-root", str(platform_root), "--force",
    ])
    tenant_root = platform_root / "tenants/demo_tuoguan"
    accepted = _run([
        sys.executable, str(ROOT / "scripts/tenant_acceptance_check.py"),
        "--tenant-root", str(tenant_root),
    ]) if initialized["returncode"] == 0 else {"returncode": 2, "output": "initializer_failed"}
    env = os.environ.copy()
    env.update({
        "PYTHONPATH": str(ROOT / "runtime") + os.pathsep + env.get("PYTHONPATH", ""),
        "HERMES_TENANT_ID": "demo_tuoguan",
        "HERMES_TUOGUAN_DATA_DIR": str(tenant_root / "data"),
        "HERMES_TENANT_OPERATING_MODEL_FILE": "institution_operating_model.json",
    })
    probed = _run([sys.executable, "-c", PROBE], env=env) if accepted["returncode"] == 0 else {"returncode": 2, "output": "acceptance_failed"}
    pollution = _pollution_scan(tenant_root) if tenant_root.is_dir() else []
    probe_payload: dict[str, Any] = {}
    if probed.get("output"):
        try:
            probe_payload = json.loads(str(probed["output"]).splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            probe_payload = {"ok": False, "error": "probe_output_invalid", "output_tail": probed["output"][-2000:]}
    result = {
        "ok": initialized["returncode"] == 0 and accepted["returncode"] == 0 and probed["returncode"] == 0 and not pollution,
        "tenant_root": str(tenant_root),
        "initializer": {"returncode": initialized["returncode"], "output_tail": initialized["output"][-1000:]},
        "acceptance": {"returncode": accepted["returncode"], "output_tail": accepted["output"][-2000:]},
        "capability_probe": probe_payload,
        "pollution_hits": pollution,
        "production_modified": False,
    }
    if owner is not None:
        owner.cleanup()
        result["ephemeral_tenant_removed"] = True
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Xiaoyou non-Youyi tenant gate.")
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--work-dir", type=Path)
    args = parser.parse_args()
    try:
        result = run_gate(profile=args.profile, work_dir=args.work_dir)
    except (OSError, RuntimeError, ValueError) as exc:
        result = {"ok": False, "error": f"{type(exc).__name__}:{exc}", "production_modified": False}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
