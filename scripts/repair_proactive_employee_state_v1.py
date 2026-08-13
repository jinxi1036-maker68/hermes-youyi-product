"""Append-only repair for the first proactive-employee production rollout."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))

from plugins.tuoguan_core.digital_employee_state import (  # noqa: E402
    HERMES_WORK_ITEMS_FILE,
    RELATIONSHIP_TOUCH_CANDIDATES_FILE,
    query_hermes_work_items,
    query_relationship_touch_candidates,
    update_hermes_work_item,
    update_relationship_touch_candidate_status,
)
from plugins.tuoguan_core.models import UserIdentity  # noqa: E402
from plugins.tuoguan_core.proactive_work import (  # noqa: E402
    PROACTIVE_AUTHORIZATIONS_FILE,
    query_proactive_authorizations,
    submit_proactive_authorization,
)
from plugins.tuoguan_core.store import TuoguanStore  # noqa: E402
from plugins.tuoguan_core.write_guard import authorized_system_write  # noqa: E402


WORKSTYLE_FILE = "person_workstyle_events.jsonl"
SELF_EVOLUTION_FILE = "self_evolution_events.jsonl"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _system_identity() -> UserIdentity:
    return UserIdentity("system", "proactive_state_repair", "proactive_state_repair", "小优状态修复", "boss", "approved")


def build_plan(store: TuoguanStore) -> dict[str, Any]:
    identity = _system_identity()
    touches = query_relationship_touch_candidates(store, identity=identity, include_closed=True, limit=100)
    stale_touches = [
        row for row in touches.get("candidates") or []
        if str(row.get("target_user_id") or "") == "CeShi"
        and str(row.get("status") or "") == "candidate"
        and str(row.get("created_at") or "")[:10] <= "2026-08-13"
    ]
    workstyle_rows = _read_jsonl(store.path_for(WORKSTYLE_FILE))
    mismatches = [
        row for row in workstyle_rows
        if str(row.get("record_type") or "") == "person_workstyle_preference"
        and str(row.get("dimension_key") or "") == "tone"
        and any(term in "".join(str(row.get(key) or "") for key in ("source_text", "preference_text", "normalized_rule")) for term in ("自主决定", "自己决定", "找谁", "什么时候找"))
    ]
    work = query_hermes_work_items(store, identity=identity, include_closed=False, limit=100)
    stale_work = []
    for row in work.get("items") or []:
        waiting = row.get("current_waiting") if isinstance(row.get("current_waiting"), dict) else {}
        text = json.dumps(waiting, ensure_ascii=False)
        if any(term in text for term in ("李老师全名", "看板获取方式")):
            stale_work.append(row)
    current_auth = query_proactive_authorizations(store, identity=identity, include_inactive=False)
    active_pairs = {
        (str(row.get("subject_role") or ""), tuple(sorted(str(value) for value in row.get("subject_user_ids") or [])))
        for row in current_auth.get("authorizations") or []
    }
    authorization_additions = []
    if ("boss", ("JinWenJie",)) not in active_pairs:
        authorization_additions.append({"role": "boss", "users": ["JinWenJie"], "daily_limit": 2})
    if ("teacher", ("CeShi",)) not in active_pairs:
        authorization_additions.append({"role": "teacher", "users": ["CeShi"], "daily_limit": 2})
    return {
        "stale_relationship_touch_ids": [str(row.get("candidate_id") or "") for row in stale_touches],
        "semantic_mismatch_preference_ids": [str(row.get("preference_id") or row.get("event_id") or "") for row in mismatches],
        "stale_work_focus_keys": [str(row.get("focus_key") or "") for row in stale_work],
        "authorization_additions": authorization_additions,
    }


def apply_plan(store: TuoguanStore, plan: dict[str, Any]) -> dict[str, Any]:
    identity = _system_identity()
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    results: list[dict[str, Any]] = []
    allowed = {
        RELATIONSHIP_TOUCH_CANDIDATES_FILE,
        PROACTIVE_AUTHORIZATIONS_FILE,
        HERMES_WORK_ITEMS_FILE,
        WORKSTYLE_FILE,
        SELF_EVOLUTION_FILE,
    }
    with authorized_system_write(store.data_dir, job_name="repair_proactive_employee_state_v1", allowed_files=allowed):
        for candidate_id in plan.get("stale_relationship_touch_ids") or []:
            results.append(update_relationship_touch_candidate_status(
                store,
                identity=identity,
                candidate_id=candidate_id,
                status="superseded",
                operation_id=f"repair-proactive:touch:{candidate_id}",
                failure_reason="老板后续已重新定义主动工作闭环；旧候选未发送且已过时，不补发。",
                source_text="主动工作 V1 历史状态收口",
            ))
        existing_mismatch = {
            str(row.get("preference_id") or "") for row in _read_jsonl(store.path_for(WORKSTYLE_FILE))
            if str(row.get("record_type") or "") == "person_workstyle_semantic_mismatch"
        }
        for preference_id in plan.get("semantic_mismatch_preference_ids") or []:
            if not preference_id or preference_id in existing_mismatch:
                continue
            row = {
                "record_type": "person_workstyle_semantic_mismatch",
                "preference_id": preference_id,
                "status": "superseded",
                "reason": "老板授权小优自主决定找谁和何时找人属于行动授权，不是语气偏好。",
                "created_at": stamp,
            }
            store.append_jsonl_verified(WORKSTYLE_FILE, row)
            results.append({"ok": True, "writeback_verified": True, "kind": "workstyle_semantic_mismatch", "preference_id": preference_id})
        for focus_key in plan.get("stale_work_focus_keys") or []:
            results.append(update_hermes_work_item(
                store,
                identity=identity,
                focus_key=focus_key,
                status="active",
                focus_summary="旧等待条件已经失效；后续从当前人员目录、目标行动和真实回执恢复。",
                current_waiting={},
                blocked_by=[],
                update_text="清除等待李老师全名或看板方式的失效状态，不删除历史。",
                source_text="主动工作 V1 历史状态收口",
                operation_id=f"repair-proactive:work:{focus_key}",
            ))
        for index, addition in enumerate(plan.get("authorization_additions") or []):
            results.append(submit_proactive_authorization(
                store,
                identity=identity,
                operation_id=f"repair-proactive:authorization:{index}",
                subject_role=str(addition.get("role") or ""),
                subject_user_ids=list(addition.get("users") or []),
                action_types=["owner_decision", "ask_work_fact", "ask_operating_fact", "ask_task_result", "ask_student_service_fact", "follow_up", "assign_low_risk_goal_task"],
                daily_limit=int(addition.get("daily_limit") or 1),
                rollout_stage="pilot_jin_and_li",
                source_text="2026-08-13 老板明确授权小优主动找金总和李老师，并自主判断询问对象、时间和具体工作事实。",
            ))
    return {
        "ok": all(item.get("ok") for item in results),
        "result_count": len(results),
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    store = TuoguanStore(Path(args.data_dir))
    plan = build_plan(store)
    output: dict[str, Any] = {"ok": True, "mode": "dry_run", "plan": plan}
    if args.apply:
        output = {"mode": "apply", "plan": plan, **apply_plan(store, plan)}
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if output.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
