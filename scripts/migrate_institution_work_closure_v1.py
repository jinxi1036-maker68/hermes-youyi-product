#!/usr/bin/env python3
"""Append-only migration for the safety-policy institution-work closure.

This script deliberately knows only the two production task ids confirmed by
the owner.  It never searches for similar wording, deletes history, or sends
an old message.  Run without ``--apply`` first and inspect the plan.
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
    advance_institution_work,
    query_institution_work,
)
from plugins.tuoguan_core.employee_identity import system_identity  # noqa: E402
from plugins.tuoguan_core.store import TuoguanStore  # noqa: E402
from plugins.tuoguan_core.tenant_context import current_tenant_id  # noqa: E402
from plugins.tuoguan_core.workstyle_profiles import WORKSTYLE_EVENTS_FILE  # noqa: E402
from plugins.tuoguan_core.write_guard import authorized_system_write  # noqa: E402


SAFETY_FOCUS = "institution:safety_management_policy"
WRONG_INTERNAL_TASK_ID = "task_9dfba24852ba"
OLD_MONTHLY_TASK_ID = "task_d9d1dc7698db"
TARGET_TASK_IDS = {WRONG_INTERNAL_TASK_ID, OLD_MONTHLY_TASK_ID}
TASK_CONTEXT_FILES = ("active_task_context.json", "pending_next_task_context.json", "model_focus.json")

CONFIRMED_FACTS = [
    "家长在机构门口接孩子；当前无需签字，也没有延迟费用。",
    "发生伤情时需要通知家长；当前没有固定合作医院。",
    "机构提供午餐和晚餐，由机构统一采购和烹饪；当前已有食品留样。",
    "机构有灭火器和逃生路线，老师知道位置。",
]
PENDING_ITEMS = [
    "免责责任、医疗费垫付和学生个人摔伤责任，待专业核验。",
    "迟到30分钟、食品留样48小时和125克等具体标准，待官方原始来源或专业人员核验。",
]


def _stamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _tasks(store: TuoguanStore) -> dict[str, dict[str, Any]]:
    return {
        str(row.get("id") or ""): row
        for row in store.load_tasks()
        if isinstance(row, dict) and str(row.get("id") or "")
    }


def _open_safety_work(store: TuoguanStore) -> dict[str, Any] | None:
    queried = query_institution_work(
        store,
        identity=system_identity(),
        focus_key=SAFETY_FOCUS,
        include_closed=True,
        limit=10,
    )
    rows = queried.get("items") if isinstance(queried.get("items"), list) else []
    return next((row for row in rows if isinstance(row, dict)), None)


def build_plan(store: TuoguanStore) -> dict[str, Any]:
    tasks = _tasks(store)
    outbox = store.read_json("notification_outbox.json", [])
    outbox = outbox if isinstance(outbox, list) else []
    contexts: dict[str, list[str]] = {}
    for filename in TASK_CONTEXT_FILES:
        value = store.read_json(filename, {})
        if not isinstance(value, dict):
            contexts[filename] = []
            continue
        contexts[filename] = [
            str(user_id)
            for user_id, row in value.items()
            if isinstance(row, dict) and str(row.get("task_id") or "") in TARGET_TASK_IDS
        ]
    pending_notices = [
        str(row.get("id") or "")
        for row in outbox
        if isinstance(row, dict)
        and str(row.get("task_id") or "") in TARGET_TASK_IDS
        and str(row.get("status") or "") not in {"sent", "failed", "suppressed", "superseded"}
    ]
    return {
        "ok": True,
        "safety_focus": SAFETY_FOCUS,
        "existing_safety_work_item_id": str((_open_safety_work(store) or {}).get("work_item_id") or ""),
        "tasks": [
            {
                "task_id": task_id,
                "exists": task_id in tasks,
                "status_before": str(tasks.get(task_id, {}).get("status") or ""),
                "action": "supersede_preserve_history",
            }
            for task_id in sorted(TARGET_TASK_IDS)
        ],
        "pending_notification_ids": pending_notices,
        "context_references": contexts,
        "boundary": {
            "history_deleted": False,
            "messages_sent": False,
            "sent_delivery_receipts_changed": False,
            "new_staff_tasks_created": False,
            "policy_effective": False,
        },
    }


def _supersede_tasks(store: TuoguanStore, *, stamp: str) -> list[dict[str, Any]]:
    changed: list[dict[str, Any]] = []

    def mutate(value: Any) -> list[dict[str, Any]]:
        rows = value if isinstance(value, list) else []
        for row in rows:
            if not isinstance(row, dict) or str(row.get("id") or "") not in TARGET_TASK_IDS:
                continue
            if str(row.get("status") or "") == "superseded":
                continue
            task_id = str(row.get("id") or "")
            reason = (
                "机构制度起草属于小优内部成果工作，不能误分配给老板。"
                if task_id == WRONG_INTERNAL_TASK_ID
                else "旧一次性月度巡查任务已迁移为待授权周期安排候选，不再作为开放任务。"
            )
            row.update({
                "status": "superseded",
                "superseded_at": stamp,
                "superseded_reason": reason,
                "closure_evidence": {
                    "action": "institution_work_migration_v1",
                    "at": stamp,
                    "reason": reason,
                },
            })
            changed.append({"task_id": task_id, "reason": reason})
        return rows

    store.update_tasks(mutate)
    after = _tasks(store)
    for row in changed:
        row["writeback_verified"] = str(after.get(row["task_id"], {}).get("status") or "") == "superseded"
    return changed


def _suppress_pending_notices(store: TuoguanStore, *, stamp: str) -> list[str]:
    changed: list[str] = []

    def mutate(value: Any) -> Any:
        rows = value if isinstance(value, list) else []
        for row in rows:
            if not isinstance(row, dict) or str(row.get("task_id") or "") not in TARGET_TASK_IDS:
                continue
            if str(row.get("status") or "") in {"sent", "failed", "suppressed", "superseded"}:
                continue
            row.update({
                "status": "superseded",
                "superseded_at": stamp,
                "suppressed_reason": "关联旧机构制度任务已经迁移，不补发历史提醒。",
            })
            changed.append(str(row.get("id") or ""))
        return rows

    store.update_json("notification_outbox.json", [], mutate)
    return changed


def _clear_context_projections(store: TuoguanStore) -> dict[str, int]:
    result: dict[str, int] = {}
    for filename in TASK_CONTEXT_FILES:
        count = {"value": 0}

        def mutate(value: Any) -> Any:
            rows = value if isinstance(value, dict) else {}
            for key, row in list(rows.items()):
                if isinstance(row, dict) and str(row.get("task_id") or "") in TARGET_TASK_IDS:
                    del rows[key]
                    count["value"] += 1
            return rows

        store.update_json(filename, {}, mutate)
        result[filename] = count["value"]
    return result


def _migrate_workstyle(store: TuoguanStore, *, stamp: str) -> dict[str, Any]:
    source_rows: list[dict[str, Any]] = []
    path = store.path_for(WORKSTYLE_EVENTS_FILE)
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                source_rows.append(row)
    mismatches = [
        row for row in source_rows
        if str(row.get("record_type") or "") == "person_workstyle_preference"
        and str(row.get("status") or "active") == "active"
        and (
            str(row.get("preference_id") or "") == "workstyle_pref_e1554ace1a1f"
            or "自主决定找谁" in str(row.get("preference_text") or row.get("normalized_rule") or "")
        )
    ]
    mismatch_ids = {str(row.get("preference_id") or "") for row in mismatches if str(row.get("preference_id") or "")}
    existing_pacing = any(
        str(row.get("record_type") or "") == "person_workstyle_preference"
        and str(row.get("target_user_id") or "") == "owner_test"
        and str(row.get("dimension_key") or "") == "interaction_pacing"
        and str(row.get("status") or "active") == "active"
        for row in source_rows
    )
    for preference_id in sorted(mismatch_ids):
        store.append_jsonl_verified(WORKSTYLE_EVENTS_FILE, {
            "record_type": "person_workstyle_semantic_mismatch",
            "mismatch_id": f"workstyle_mismatch_{preference_id}_{stamp.replace(':', '').replace('+', '')}",
            "preference_id": preference_id,
            "status": "superseded",
            "reason": "业务闭环规则被错误保存为个人长度偏好，不再注入工作方式上下文。",
            "created_at": stamp,
            "source": "migrate_institution_work_closure_v1",
        })
    pacing_id = ""
    if not existing_pacing:
        pacing_id = f"workstyle_pref_institution_pacing_{stamp.replace(':', '').replace('+', '')}"
        store.append_jsonl_verified(WORKSTYLE_EVENTS_FILE, {
            "record_type": "person_workstyle_preference",
            "preference_id": pacing_id,
            "tenant_id": current_tenant_id(),
            "target_user_id": "owner_test",
            "target_name": "机构负责人",
            "target_role": "boss",
            "preference_type": "other_low_risk",
            "scope": "all_communication",
            "dimension_key": "interaction_pacing",
            "preference_text": "制度讨论一项一项推进，一次只讲一个章节、只问一个关键问题。",
            "normalized_rule": "机构制度讨论一次只推进一个章节和一个关键问题。",
            "source_text": "老板明确要求一项一项、一次不要说太多。",
            "status": "active",
            "created_at": stamp,
            "source": "migrate_institution_work_closure_v1",
        })
    return {"semantic_mismatch_ids": sorted(mismatch_ids), "interaction_pacing_preference_id": pacing_id, "already_present": existing_pacing}


def _create_or_reuse_safety_v01(store: TuoguanStore, *, stamp: str) -> dict[str, Any]:
    identity = system_identity()
    item = _open_safety_work(store)
    if item is None:
        discovered = advance_institution_work(
            store, identity=identity, action="discover", operation_id=f"migration:safety:discover:{stamp}",
            focus_key=SAFETY_FOCUS, title="示例机构托管安全管理制度", summary="将已确认安全做法整理为待重新审核的 V0.1 草案。",
            evidence=[{"source_kind": "internal_confirmed", "summary": text, "source_message_id": "historical_owner_safety_dialogue"} for text in CONFIRMED_FACTS],
            source_text="历史安全制度对话迁移：仅保存老板已明确说明的机构事实。",
            source_message_id="historical_owner_safety_dialogue",
        )
        if not discovered.get("ok"):
            return {"ok": False, "error": discovered.get("error")}
        item = discovered.get("work_item") or {}
    versions = item.get("artifacts") if isinstance(item.get("artifacts"), list) else []
    v01 = next((row for row in versions if isinstance(row, dict) and str(row.get("title") or "").endswith("V0.1")), None)
    if v01 is None:
        content = "\n".join([
            "示例机构托管安全管理制度 V0.1（待重新审核）",
            "已确认机构做法：",
            *[f"- {fact}" for fact in CONFIRMED_FACTS],
            "待专业核验，不纳入生效版本：",
            *[f"- {item}" for item in PENDING_ITEMS],
        ])
        drafted = advance_institution_work(
            store, identity=identity, action="save_draft", operation_id=f"migration:safety:draft:{stamp}",
            work_item_id=str(item.get("work_item_id") or ""), artifact_title="示例机构托管安全管理制度 V0.1", artifact_content=content,
            pending_items=PENDING_ITEMS, source_text="历史安全制度对话迁移：V0.1待重新审核。", source_message_id="historical_owner_safety_dialogue",
        )
        if not drafted.get("ok"):
            return {"ok": False, "error": drafted.get("error")}
        v01 = drafted.get("artifact") or {}
        item = drafted.get("work_item") or item
    if str(item.get("institution_stage") or "") != "awaiting_content_approval":
        submitted = advance_institution_work(
            store, identity=identity, action="submit_for_review", operation_id=f"migration:safety:review:{stamp}",
            work_item_id=str(item.get("work_item_id") or ""), artifact_version_id=str(v01.get("version_id") or ""),
            source_text="迁移后需要老板重新审核 V0.1 内容；历史确认不沿用。", source_message_id="migration_review_request",
        )
        if not submitted.get("ok"):
            return {"ok": False, "error": submitted.get("error")}
        item = submitted.get("work_item") or item
    return {"ok": True, "work_item_id": str(item.get("work_item_id") or ""), "artifact_version_id": str(v01.get("version_id") or item.get("current_artifact_version_id") or ""), "institution_stage": str(item.get("institution_stage") or "")}


def migrate(data_dir: Path, *, apply: bool) -> dict[str, Any]:
    store = TuoguanStore(data_dir)
    plan = build_plan(store)
    if not apply:
        return {"ok": True, "dry_run": True, "data_dir": str(data_dir), "plan": plan, "writes": []}
    stamp = _stamp()
    with authorized_system_write(
        store.data_dir,
        job_name="migrate_institution_work_closure_v1",
        allowed_files={HERMES_WORK_ITEMS_FILE, "tasks.json", "notification_outbox.json", *TASK_CONTEXT_FILES, WORKSTYLE_EVENTS_FILE, "dashboard_cache.json"},
    ):
        safety = _create_or_reuse_safety_v01(store, stamp=stamp)
        task_updates = _supersede_tasks(store, stamp=stamp)
        notices = _suppress_pending_notices(store, stamp=stamp)
        contexts = _clear_context_projections(store)
        workstyle = _migrate_workstyle(store, stamp=stamp)
        dashboard = refresh_dashboard_cache(store, create_operation_tasks=False)
    tasks = _tasks(store)
    verified_tasks = all(
        task_id not in tasks or str(tasks[task_id].get("status") or "") == "superseded"
        for task_id in TARGET_TASK_IDS
    )
    verified_safety = bool(safety.get("ok")) and str(safety.get("institution_stage") or "") == "awaiting_content_approval"
    return {
        "ok": bool(verified_tasks and verified_safety),
        "dry_run": False,
        "data_dir": str(data_dir),
        "plan": plan,
        "writes": {"safety": safety, "tasks": task_updates, "suppressed_notification_ids": notices, "cleared_contexts": contexts, "workstyle": workstyle, "dashboard": dashboard},
        "writeback_verified": bool(verified_tasks and verified_safety),
        "boundary": plan["boundary"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate the verified safety-policy conversation into an institution-work V0.1 review item.")
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = migrate(args.data_dir, apply=bool(args.apply))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
