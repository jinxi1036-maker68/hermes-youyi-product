"""One-shot repair for Hermes employee-loop material consistency.

The repair marks obsolete goal/value material as historical evidence and appends
one current work-item calibration. It never deletes business history.
"""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from .digital_employee_state import (
    VALUE_PROGRESS_LEDGER_FILE,
    submit_hermes_work_item,
    update_hermes_work_item,
)
from .models import UserIdentity
from .store import TuoguanStore, resolve_tuoguan_data_dir
from .write_guard import authorized_system_write


GOAL_ID = "goal_9f25347346525253"
FOCUS_KEY = f"goal:{GOAL_ID}"
FILES_TO_BACKUP = (
    "goal_operator_goals.json",
    "hermes_work_items.jsonl",
    VALUE_PROGRESS_LEDGER_FILE,
    "attention_threads.jsonl",
    "notification_outbox.json",
)


def repair_employee_closure_materials(
    *,
    data_dir: str | Path | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    store = TuoguanStore(resolve_tuoguan_data_dir(data_dir))
    stamp = datetime.now().astimezone().strftime("%Y%m%d%H%M%S")
    report: dict[str, Any] = {
        "ok": True,
        "mode": "apply" if apply else "dry-run",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data_dir": str(store.data_dir),
        "backups": [],
        "goal_rows_marked": 0,
        "value_rows_marked": 0,
        "work_item_calibrated": False,
        "business_history_deleted": False,
    }
    goal_doc = store.read_json("goal_operator_goals.json", {"goals": []})
    new_goal_doc, goal_marked = _mark_goal_doc(goal_doc)
    value_rows, value_marked = _mark_value_rows(store.path_for(VALUE_PROGRESS_LEDGER_FILE))
    report["goal_rows_marked"] = goal_marked
    report["value_rows_marked"] = value_marked
    if not apply:
        report["would_backup"] = [name for name in FILES_TO_BACKUP if store.path_for(name).exists()]
        return report
    backup_dir = store.data_dir / "backup" / "employee_closure_v1" / stamp
    for name in FILES_TO_BACKUP:
        source = store.path_for(name)
        if source.exists():
            backup_dir.mkdir(parents=True, exist_ok=True)
            target = backup_dir / name
            shutil.copy2(source, target)
            report["backups"].append(str(target))
    with authorized_system_write(
        store.data_dir,
        job_name="employee_closure_material_repair_v1",
        allowed_files={"goal_operator_goals.json", "hermes_work_items.jsonl", VALUE_PROGRESS_LEDGER_FILE},
    ):
        if goal_marked:
            store.write_json("goal_operator_goals.json", new_goal_doc)
        report["work_item_calibrated"] = _calibrate_work_item(store, stamp)
        if value_marked:
            _write_jsonl_atomic(store, VALUE_PROGRESS_LEDGER_FILE, value_rows)
    return report


def _mark_goal_doc(payload: Any) -> tuple[Any, int]:
    doc = json.loads(json.dumps(payload, ensure_ascii=False))
    rows = doc.get("goals") if isinstance(doc, dict) else doc if isinstance(doc, list) else []
    if not isinstance(rows, list):
        return doc, 0
    marked = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        text = json.dumps(row, ensure_ascii=False)
        if str(row.get("goal_id") or row.get("id") or "") != GOAL_ID and "九月" not in text and "续费" not in text:
            continue
        row["current_decision_material_status"] = "current_work_item_authoritative"
        row["current_phase_override"] = "historical_analysis_and_preparation"
        row["service_relation_policy"] = "defer_until_new_term"
        row["new_term_confirmation_window"] = {"start": "2026-08-25", "end": "2026-09-10"}
        row["historical_roster_policy"] = {
            "old_roster_is_current_fact": False,
            "historical_student_count": _nested_get(row, ("review_snapshot", "student_count")),
            "use_as": "historical_snapshot_only",
        }
        review = row.get("review_snapshot")
        if isinstance(review, dict):
            review["decision_material_status"] = "historical_snapshot"
            review["not_current_blocker"] = True
        support = row.get("decision_support")
        if isinstance(support, dict):
            support["decision_material_status"] = "not_current_decision_material"
            support["superseded_by"] = "current_work_item historical_analysis_and_preparation"
        marked += 1
    return doc, marked


def _mark_value_rows(path: Path) -> tuple[list[dict[str, Any]], int]:
    rows = _read_jsonl(path)
    marked = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        text = json.dumps(row, ensure_ascii=False)
        stale = (
            any(term in text for term in ("122", "服务类型", "主责老师", "旧名单", "旧学生"))
            and any(term in text for term in ("等待", "确认", "缺少"))
        ) or ("未送达" in text and any(term in text for term in ("attention_thread", "提醒", "boss_attention")))
        if not stale:
            continue
        if str(row.get("decision_material_status") or "") == "not_current_decision_material":
            continue
        row["decision_material_status"] = "not_current_decision_material"
        row["superseded_reason"] = "新学期服务关系已暂缓；旧名单只作为历史快照，不再作为当前默认推进卡点。"
        row["superseded_by"] = FOCUS_KEY
        row["superseded_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        marked += 1
    return rows, marked


def _calibrate_work_item(store: TuoguanStore, stamp: str) -> bool:
    identity = UserIdentity(
        platform="system",
        platform_user_id="autonomous_employee_loop",
        canonical_user_id="autonomous_employee_loop",
        person_name="Hermes",
        role="boss",
        approval_state="approved",
    )
    common = {
        "identity": identity,
        "operation_id": f"system:employee_closure_material_repair_v1:{stamp}",
        "focus_key": FOCUS_KEY,
        "status": "active",
        "focus_summary": "九月份续费率更稳目标继续有效；当前不再把旧122人服务关系确认当作当前卡点，先推进历史数据分析、风险分组、沟通素材准备和记录标准。",
        "current_phase": {
            "phase_key": "historical_analysis_and_preparation",
            "name": "历史数据分析与沟通准备",
            "not_responsibility_confirmation": True,
            "basis": "当前以修复后的工作项为准：旧名单只作历史快照，新学期服务关系等待确认窗口；当前目标可继续推进历史数据分析、风险分组、沟通素材和记录标准。",
            "phase_goal": "先完成不依赖新学期名单的续费准备材料，2026-08-25后再进入新学期服务关系确认。",
            "auto_execute": False,
        },
        "next_actions": [
            "整理历史沟通覆盖和记录覆盖缺口，形成续费风险分组候选。",
            "准备家校沟通素材标准和老师最省事补记录建议。",
            "到2026-08-25至2026-09-10再确认新学期名单、服务类型和主责老师。",
        ],
        "confirmed_facts": [
            "旧122人名单只作为历史目标快照。",
            "新学期服务关系确认窗口为2026-08-25至2026-09-10。",
        ],
        "pending_judgements": [
            "哪些学生属于价格敏感、转校风险或观望型，需要结合历史沟通与记录证据继续分析。",
        ],
        "current_waiting": {
            "reason": "新学期名单、服务类型和主责老师等待确认窗口。",
            "wait_for": "新学期确认窗口与历史证据继续积累",
            "wait_type": "deferred_new_term_confirmation",
            "expected_response_by": "2026-08-25",
            "since": datetime.now().astimezone().isoformat(timespec="seconds"),
            "not_a_goal_blocker": True,
        },
        "blocked_by": [],
        "next_attention_at": "2026-08-02T08:00:00+08:00",
        "update_text": "修复旧材料污染：当前目标阶段校准为历史数据分析与沟通准备；旧名单不再作为当前默认卡点。",
        "source_text": "employee_closure_material_repair_v1",
        "source_message_id": f"employee_closure_material_repair_v1:{stamp}",
    }
    result = update_hermes_work_item(store, **common)
    if not result.get("ok") and result.get("error") == "work_item_not_found":
        result = submit_hermes_work_item(store, title="九月份续费率更稳", **common)
    return bool(result.get("ok"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _write_jsonl_atomic(store: TuoguanStore, filename: str, rows: list[dict[str, Any]]) -> None:
    from .write_guard import assert_business_write_allowed

    assert_business_write_allowed(store.data_dir, filename)
    path = store.path_for(filename)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )
    tmp.replace(path)


def _nested_get(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    value: Any = row
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Repair Hermes employee closure material consistency.")
    parser.add_argument("--data-dir", default="")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    result = repair_employee_closure_materials(data_dir=args.data_dir or None, apply=bool(args.apply))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
