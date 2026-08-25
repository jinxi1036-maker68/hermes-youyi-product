#!/usr/bin/env python3
"""Append-only reconciliation for institution-work claims made in dialogue.

This migration never treats a conversational acknowledgement as an approval.
It creates bounded V0.1 drafts where evidence exists, keeps high-risk payroll
boundaries in drafting, and schedules only an internal review wakeup for the
owner's 2026-08-31 reminder.  It never sends a message, creates a staff task,
or turns a draft into an effective institution rule.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = REPO_ROOT / "runtime"
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from plugins.tuoguan_core.dashboard_builder import refresh_dashboard_cache  # noqa: E402
from plugins.tuoguan_core.digital_employee_state import (  # noqa: E402
    HERMES_WORK_ITEMS_FILE,
    WAKEUP_REQUESTS_FILE,
    advance_institution_work,
    query_institution_work,
    submit_wakeup_request,
)
from plugins.tuoguan_core.employee_identity import system_identity  # noqa: E402
from plugins.tuoguan_core.store import TuoguanStore  # noqa: E402
from plugins.tuoguan_core.write_guard import authorized_system_write  # noqa: E402


SAFETY_FOCUS = "institution:safety_management_policy"
STUDENT_RECORD_FOCUS = "institution:student_record_policy"
TASK_AUTHORIZATION_FOCUS = "institution:task_authorization_boundary"
PAYROLL_FOCUS = "institution:performance_pay_boundary"
LEGACY_UMBRELLA_FOCUS = "institution:operating_rules_missing"
REVIEW_WAKEUP_OPERATION = "migration:institution_dialogue_truth_v1:review_20260831"
REVIEW_WAKEUP_AT = "2026-08-31T09:30:00+08:00"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _items_by_focus(store: TuoguanStore, focus_key: str) -> list[dict[str, Any]]:
    result = query_institution_work(
        store,
        identity=system_identity(),
        focus_key=focus_key,
        include_closed=True,
        limit=20,
    )
    return [row for row in (result.get("items") or []) if isinstance(row, dict)]


def _open_or_latest_item(store: TuoguanStore, focus_key: str) -> dict[str, Any] | None:
    rows = _items_by_focus(store, focus_key)
    return rows[0] if rows else None


def _operation(prefix: str) -> str:
    return f"migration:institution_dialogue_truth_v1:{prefix}"


def _ensure_work_item(
    store: TuoguanStore,
    *,
    focus_key: str,
    title: str,
    summary: str,
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    existing = _open_or_latest_item(store, focus_key)
    if existing:
        return {"ok": True, "work_item": existing, "created": False}
    result = advance_institution_work(
        store,
        identity=system_identity(),
        action="discover",
        operation_id=_operation(f"{focus_key}:discover"),
        focus_key=focus_key,
        title=title,
        summary=summary,
        evidence=evidence,
        source_message_id="historical_owner_institution_dialogue_20260825",
        source_text="历史机构制度讨论仅迁移为待审核工作事项，不代表老板已确认或授权落实。",
    )
    return {"ok": bool(result.get("ok")), "work_item": result.get("work_item") or {}, "created": True, "error": result.get("error")}


def _ensure_draft(
    store: TuoguanStore,
    *,
    focus_key: str,
    title: str,
    summary: str,
    evidence: list[dict[str, Any]],
    artifact_title: str,
    artifact_content: str,
    submit_for_review: bool,
    pending_items: list[str] | None = None,
) -> dict[str, Any]:
    ensured = _ensure_work_item(
        store,
        focus_key=focus_key,
        title=title,
        summary=summary,
        evidence=evidence,
    )
    if not ensured.get("ok"):
        return ensured
    item = ensured.get("work_item") or {}
    stage = str(item.get("institution_stage") or "")
    artifacts = item.get("artifacts") if isinstance(item.get("artifacts"), list) else []
    matching = next(
        (
            artifact for artifact in artifacts
            if isinstance(artifact, dict) and str(artifact.get("title") or "") == artifact_title
        ),
        None,
    )
    if matching:
        return {
            "ok": True,
            "work_item": item,
            "artifact_version_id": str(matching.get("version_id") or ""),
            "created": False,
            "institution_stage": stage,
        }
    result = advance_institution_work(
        store,
        identity=system_identity(),
        action="save_draft",
        operation_id=_operation(f"{focus_key}:draft"),
        work_item_id=str(item.get("work_item_id") or ""),
        artifact_title=artifact_title,
        artifact_content=artifact_content,
        pending_items=pending_items or [],
        submit_for_review=submit_for_review,
        source_message_id="historical_owner_institution_dialogue_20260825",
        source_text="历史对话只形成 V0.1 草案；内容确认和落实授权仍须由老板分别明确表达。",
    )
    return {
        "ok": bool(result.get("ok")),
        "work_item": result.get("work_item") or item,
        "artifact_version_id": str((result.get("artifact") or {}).get("version_id") or ""),
        "created": True,
        "institution_stage": str((result.get("work_item") or {}).get("institution_stage") or ""),
        "error": result.get("error"),
    }


def _ensure_safety_review(store: TuoguanStore) -> dict[str, Any]:
    existing = _open_or_latest_item(store, SAFETY_FOCUS)
    if existing:
        return {
            "ok": True,
            "work_item_id": str(existing.get("work_item_id") or ""),
            "institution_stage": str(existing.get("institution_stage") or ""),
            "preserved_existing_v01": True,
        }
    return _ensure_draft(
        store,
        focus_key=SAFETY_FOCUS,
        title="优益托管安全管理制度",
        summary="将已知安全做法整理为待重新审核的 V0.1 草案。",
        evidence=[{
            "source_kind": "pending_hypothesis",
            "summary": "历史安全制度对话需要按当前版本重新审核；未把历史聊天视为有效制度。",
        }],
        artifact_title="优益托管安全管理制度 V0.1",
        artifact_content=(
            "优益托管安全管理制度 V0.1（待重新审核）\n"
            "本版本只用于重新核对现有做法。具体责任、医疗、食品标准和应急要求均待老板与专业来源核验，未生效。"
        ),
        submit_for_review=True,
        pending_items=["责任划分、医疗安排、食品标准和应急细节待重新核验。"],
    )


def _close_legacy_umbrella(store: TuoguanStore) -> dict[str, Any]:
    item = _open_or_latest_item(store, LEGACY_UMBRELLA_FOCUS)
    if not item:
        return {"ok": True, "changed": False, "reason": "legacy_umbrella_absent"}
    stage = str(item.get("institution_stage") or "")
    if stage in {"closed", "deferred", "rejected", "failed", "superseded"}:
        return {"ok": True, "changed": False, "work_item_id": str(item.get("work_item_id") or "")}
    result = advance_institution_work(
        store,
        identity=system_identity(),
        action="close",
        operation_id=_operation("legacy_umbrella:close"),
        work_item_id=str(item.get("work_item_id") or ""),
        summary=(
            "原“四项制度缺失”总事项已拆分为安全制度、学生记录制度、任务授权边界和绩效/工资边界四项；"
            "历史 21:00 缺口结论已纠正，不再作为已完成或待执行事实。"
        ),
        source_message_id="migration_institution_dialogue_truth_v1",
        source_text="历史总事项拆分并追加状态纠正；不删除原始证据。",
    )
    return {
        "ok": bool(result.get("ok")),
        "changed": bool(result.get("writeback_verified")),
        "work_item_id": str((result.get("work_item") or {}).get("work_item_id") or ""),
        "error": result.get("error"),
    }


def _has_review_wakeup(store: TuoguanStore) -> bool:
    path = store.path_for(WAKEUP_REQUESTS_FILE)
    if not path.exists():
        return False
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        source = row.get("source") if isinstance(row, dict) else {}
        if isinstance(source, dict) and str(source.get("operation_id") or "") == REVIEW_WAKEUP_OPERATION:
            return True
    return False


def _ensure_review_wakeup(store: TuoguanStore, *, related_work_item_id: str) -> dict[str, Any]:
    if _has_review_wakeup(store):
        return {"ok": True, "created": False, "writeback_verified": True}
    result = submit_wakeup_request(
        store,
        identity=system_identity(),
        wakeup_source="institution_dialogue_truth_review",
        reason="老板说明 8 月 31 日全员上班后重新审核学生记录制度；仅重新查看事实和草案，不自动实施、不联系员工、不创建任务。",
        operation_id=REVIEW_WAKEUP_OPERATION,
        related_work_item_id=related_work_item_id,
        related_objects=[{"type": "institution_work", "focus_key": STUDENT_RECORD_FOCUS}],
        scheduled_for=REVIEW_WAKEUP_AT,
        status="pending",
        source_text="老板说明“8月31日全员上班后再统一落实”；迁移只创建内部审核唤醒，不视为落实授权。",
        source_message_id="historical_owner_20260825_0831_review",
    )
    return {"ok": bool(result.get("ok")), "created": bool(result.get("writeback_verified")), "writeback_verified": bool(result.get("writeback_verified")), "error": result.get("error")}


def build_plan(store: TuoguanStore) -> dict[str, Any]:
    focuses = {
        name: _open_or_latest_item(store, focus)
        for name, focus in {
            "safety": SAFETY_FOCUS,
            "student_record": STUDENT_RECORD_FOCUS,
            "task_authorization": TASK_AUTHORIZATION_FOCUS,
            "performance_pay": PAYROLL_FOCUS,
            "legacy_umbrella": LEGACY_UMBRELLA_FOCUS,
        }.items()
    }
    return {
        "ok": True,
        "focuses": {
            name: {
                "work_item_id": str((item or {}).get("work_item_id") or ""),
                "stage": str((item or {}).get("institution_stage") or ""),
            }
            for name, item in focuses.items()
        },
        "review_wakeup_exists": _has_review_wakeup(store),
        "boundary": {
            "messages_sent": False,
            "staff_tasks_created": False,
            "policy_effective": False,
            "owner_approval_inferred": False,
            "history_deleted": False,
        },
    }


def migrate(data_dir: Path, *, apply: bool) -> dict[str, Any]:
    store = TuoguanStore(data_dir)
    plan = build_plan(store)
    if not apply:
        return {"ok": True, "dry_run": True, "data_dir": str(data_dir), "plan": plan, "writes": []}

    with authorized_system_write(
        store.data_dir,
        job_name="migrate_institution_dialogue_truth_v1",
        allowed_files={HERMES_WORK_ITEMS_FILE, WAKEUP_REQUESTS_FILE, "dashboard_cache.json"},
    ):
        safety = _ensure_safety_review(store)
        student_record = _ensure_draft(
            store,
            focus_key=STUDENT_RECORD_FOCUS,
            title="学生记录制度",
            summary="把学生记录的范围、最小字段和复核方式整理为待审核草案。",
            evidence=[{
                "source_kind": "model_judgment",
                "summary": "历史讨论提出学生记录制度需求；具体频率、抽查比例和绩效关联尚未由老板确认。",
            }],
            artifact_title="优益托管学生记录制度 V0.1",
            artifact_content=(
                "优益托管学生记录制度 V0.1（待审核）\n"
                "目标：让责任老师能基于真实记录持续了解学生学习、作业和沟通情况。\n"
                "待老板确认：记录范围、最小字段、填写频率、抽查方式及是否与任何考核关联。\n"
                "本草案未生效，不自动要求老师填写，也不改变绩效或工资。"
            ),
            submit_for_review=True,
            pending_items=["记录频率、抽查比例、是否纳入任何考核均待老板单独确认。"],
        )
        task_authorization = _ensure_draft(
            store,
            focus_key=TASK_AUTHORIZATION_FOCUS,
            title="任务授权边界",
            summary="把小优创建、取消和跟进任务的授权边界整理为待审核草案。",
            evidence=[{
                "source_kind": "model_judgment",
                "summary": "历史讨论反复要求任务必须可取消、可回执、可核验；具体授权范围尚待老板确认。",
            }],
            artifact_title="优益托管任务授权边界 V0.1",
            artifact_content=(
                "优益托管任务授权边界 V0.1（待审核）\n"
                "小优只在明确目标、责任人和成功证据存在时创建低风险任务；取消、完成和提醒必须回到同一权威任务记录。\n"
                "待老板确认：可自主创建的任务类型、人员范围、频率和升级方式。\n"
                "本草案未生效，不自动扩大主动联系或任务权限。"
            ),
            submit_for_review=True,
            pending_items=["任务自主创建范围、人员灰度和提醒频率待老板单独确认。"],
        )
        performance_pay = _ensure_draft(
            store,
            focus_key=PAYROLL_FOCUS,
            title="绩效与工资边界",
            summary="将绩效或工资相关讨论隔离为高风险草案，禁止自动应用。",
            evidence=[{
                "source_kind": "pending_hypothesis",
                "summary": "历史讨论提及可能关联记录与绩效；没有形成可执行、合法且经老板确认的制度。",
            }],
            artifact_title="优益托管绩效与工资边界 V0.1",
            artifact_content=(
                "优益托管绩效与工资边界 V0.1（高风险草案）\n"
                "任何工资、绩效、处罚或奖励规则必须经过老板明确确认和必要专业核验。\n"
                "小优不得根据聊天、学生记录或老师成长记录自动计算、调整或暗示工资与绩效。"
            ),
            submit_for_review=False,
            pending_items=["全部具体指标、法律合规性、适用人员和生效时间待老板与专业人员审核。"],
        )
        legacy = _close_legacy_umbrella(store)
        review_wakeup = _ensure_review_wakeup(
            store,
            related_work_item_id=str((student_record.get("work_item") or {}).get("work_item_id") or ""),
        )
        dashboard = refresh_dashboard_cache(store, create_operation_tasks=False)

    required = (safety, student_record, task_authorization, performance_pay, legacy, review_wakeup)
    return {
        "ok": all(bool(row.get("ok")) for row in required),
        "dry_run": False,
        "data_dir": str(data_dir),
        "writes": {
            "safety": safety,
            "student_record": student_record,
            "task_authorization": task_authorization,
            "performance_pay": performance_pay,
            "legacy_umbrella": legacy,
            "review_wakeup": review_wakeup,
            "dashboard_refreshed": bool(isinstance(dashboard, dict)),
        },
        "writeback_verified": all(
            bool(row.get("ok")) for row in required
        ) and bool(isinstance(dashboard, dict)),
        "boundary": {
            "messages_sent": False,
            "staff_tasks_created": False,
            "policy_effective": False,
            "owner_approval_inferred": False,
            "history_deleted": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--apply", action="store_true", help="Write append-only reconciliation events after dry-run review.")
    args = parser.parse_args()
    print(json.dumps(migrate(args.data_dir, apply=bool(args.apply)), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
