"""Deterministic supervision and allowlisted self-repair for Xiaoyou.

The supervisor is deliberately outside the real-time WeCom path.  It observes
receipts and projections, never infers business facts from a chat transcript,
and can only repair derived technical state.  Business decisions remain with
the main Xiaoyou model and human approvals.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable
import uuid

import httpx

from .store import JSON_NO_CHANGE, TuoguanStore
from .tenant_context import current_tenant_id
from .write_guard import authorized_system_write


SUPERVISION_RUNS_FILE = "supervision_runs.jsonl"
SUPERVISION_FINDINGS_FILE = "supervision_findings.jsonl"
SUPERVISION_REPAIRS_FILE = "supervision_repairs.jsonl"

FINDING_STATES = {
    "detected", "reproduced", "classified", "repair_candidate", "applied",
    "verified", "false_positive", "deferred", "escalated", "failed",
}
TERMINAL_FINDING_STATES = {"verified", "false_positive", "deferred", "escalated", "failed"}
AUTO_REPAIR_ACTIONS = {
    "rebuild_dashboard_cache",
    "rebuild_context_projection",
    "supersede_duplicate_unsent_candidate",
    "record_self_correction",
    "reapply_verified_workstyle_context",
}
ADVISOR_ROLES = (
    "truthfulness_auditor",
    "context_time_auditor",
    "task_execution_auditor",
)
NIGHTLY_ADVISOR_ROLES = (
    "truthfulness_auditor",
    "context_time_auditor",
    "handbook_method_auditor",
)
P0_CATEGORIES = {
    "identity_participant_mismatch",
    "unauthorized_outbound",
    "business_data_overwrite",
    "illegal_task_transition",
    "continuous_service_failure",
}


def _now() -> datetime:
    return datetime.now().astimezone()


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat(timespec="seconds")


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _fingerprint(*parts: Any) -> str:
    text = "|".join(str(part or "").strip() for part in parts)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def _read_jsonl(store: TuoguanStore, filename: str) -> list[dict[str, Any]]:
    path = store.path_for(filename)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and str(value.get("tenant_id") or "") in {"", current_tenant_id()}:
            rows.append(value)
    return rows


def _append(store: TuoguanStore, filename: str, row: dict[str, Any]) -> bool:
    return store.append_jsonl_verified(filename, row)


def _parse_time(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    # Older append-only ledgers predate timezone-aware timestamps.  Production
    # writes are in the server's local business timezone, so normalize legacy
    # values at the read boundary before any detector compares them.
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=_now().tzinfo)
    return parsed


def _within(value: Any, *, now: datetime, hours: int) -> bool:
    parsed = _parse_time(value)
    if parsed is None:
        return False
    reference = now
    if parsed.tzinfo is None and reference.tzinfo is not None:
        parsed = parsed.replace(tzinfo=reference.tzinfo)
    elif parsed.tzinfo is not None and reference.tzinfo is None:
        reference = reference.replace(tzinfo=parsed.tzinfo)
    return parsed >= reference - timedelta(hours=hours)


def _fold_findings(store: TuoguanStore) -> dict[str, dict[str, Any]]:
    findings: dict[str, dict[str, Any]] = {}
    for row in _read_jsonl(store, SUPERVISION_FINDINGS_FILE):
        record_type = str(row.get("record_type") or "")
        finding_id = str(row.get("finding_id") or "")
        if not finding_id:
            continue
        if record_type == "supervision_finding" and finding_id not in findings:
            findings[finding_id] = deepcopy(row)
            findings[finding_id].setdefault("recurrence_count", 1)
            continue
        if record_type != "supervision_finding_update" or finding_id not in findings:
            continue
        patch = row.get("patch") if isinstance(row.get("patch"), dict) else {}
        findings[finding_id].update(deepcopy(patch))
        findings[finding_id]["updated_at"] = str(row.get("created_at") or findings[finding_id].get("updated_at") or "")
        findings[finding_id]["last_event_id"] = str(row.get("event_id") or "")
    return findings


def _update_finding(
    store: TuoguanStore,
    finding: dict[str, Any],
    *,
    state: str,
    patch: dict[str, Any],
    operation_id: str,
) -> dict[str, Any]:
    if state not in FINDING_STATES:
        raise ValueError("invalid_supervision_finding_state")
    row = {
        "record_type": "supervision_finding_update",
        "event_id": _id("supervision_event"),
        "finding_id": str(finding.get("finding_id") or ""),
        "tenant_id": current_tenant_id(),
        "state": state,
        "patch": {"state": state, **deepcopy(patch)},
        "operation_id": str(operation_id or ""),
        "created_at": _iso(),
    }
    _append(store, SUPERVISION_FINDINGS_FILE, row)
    merged = deepcopy(finding)
    merged.update(row["patch"])
    merged["updated_at"] = row["created_at"]
    return merged


def _evidence_ref(kind: str, object_id: str, *, count: int = 1, detail: str = "") -> dict[str, Any]:
    """Only object references and counts; never chat bodies or personal data."""

    result = {"source_kind": kind, "object_id": str(object_id or ""), "count": max(0, int(count or 0))}
    if detail:
        result["detail"] = str(detail)[:240]
    return result


def _open_or_recur_finding(
    store: TuoguanStore,
    *,
    category: str,
    severity: str,
    evidence: list[dict[str, Any]],
    scope: str,
    repair_action: str = "",
    summary: str = "",
    now: datetime,
    operation_id: str,
) -> tuple[dict[str, Any], bool]:
    fingerprint = _fingerprint(current_tenant_id(), category, scope)
    existing = next(
        (
            item for item in _fold_findings(store).values()
            if str(item.get("fingerprint") or "") == fingerprint
        ),
        None,
    )
    if existing:
        recurrence = int(existing.get("recurrence_count") or 1) + 1
        prior_state = str(existing.get("state") or "detected")
        regression = prior_state in {"verified", "false_positive"}
        next_state = "reproduced" if recurrence < 3 and not regression else "escalated"
        next_severity = "p0" if category in P0_CATEGORIES or recurrence >= 3 or regression else severity
        updated = _update_finding(
            store,
            existing,
            state=next_state,
            operation_id=operation_id,
            patch={
                "severity": next_severity,
                "recurrence_count": recurrence,
                "last_seen_at": _iso(now),
                "evidence": deepcopy(evidence),
                "regression": regression,
                "repair_action": repair_action or str(existing.get("repair_action") or ""),
                "summary": summary or str(existing.get("summary") or ""),
            },
        )
        return updated, False
    finding = {
        "record_type": "supervision_finding",
        "finding_id": _id("supervision_finding"),
        "tenant_id": current_tenant_id(),
        "fingerprint": fingerprint,
        "category": category,
        "severity": "p0" if category in P0_CATEGORIES else severity,
        "state": "repair_candidate" if repair_action in AUTO_REPAIR_ACTIONS else "classified",
        "summary": str(summary or category)[:400],
        "scope": scope[:300],
        "evidence": deepcopy(evidence),
        "repair_action": repair_action if repair_action in AUTO_REPAIR_ACTIONS else "",
        "recurrence_count": 1,
        "first_seen_at": _iso(now),
        "last_seen_at": _iso(now),
        "created_at": _iso(now),
        "updated_at": _iso(now),
        "model_review": {"status": "not_requested", "advisory_only": True},
    }
    _append(store, SUPERVISION_FINDINGS_FILE, finding)
    return finding, True


def _stale_context_findings(store: TuoguanStore, *, now: datetime) -> list[dict[str, Any]]:
    """Context files are projections only. A stale pointer is safe to repair."""

    from .digital_employee_state import _fold_hermes_work_items
    from .tasks import task_is_closed

    work_items = _fold_hermes_work_items(store)
    findings: list[dict[str, Any]] = []
    for filename in ("active_task_context.json", "pending_next_task_context.json", "model_focus.json"):
        value = store.read_json(filename, {})
        rows = value.values() if isinstance(value, dict) else value if isinstance(value, list) else []
        stale = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            task_id = str(row.get("task_id") or row.get("id") or "")
            work_item_id = str(row.get("work_item_id") or "")
            if task_id:
                task = next((item for item in store.load_tasks() if str(item.get("id") or "") == task_id), None)
                if task is None or task_is_closed(task):
                    stale += 1
            if work_item_id:
                item = work_items.get(work_item_id)
                if item is None or str(item.get("status") or "") in {"closed", "superseded"}:
                    stale += 1
        if stale:
            findings.append({
                "category": "stale_context_projection",
                "severity": "p1",
                "scope": filename,
                "repair_action": "rebuild_context_projection",
                "summary": f"{filename} 含 {stale} 条已关闭或不存在对象的投影引用。",
                "evidence": [_evidence_ref("context_projection", filename, count=stale)],
            })
    return findings


def _duplicate_unsent_candidate_findings(store: TuoguanStore) -> list[dict[str, Any]]:
    outbox = store.read_json("notification_outbox.json", [])
    if not isinstance(outbox, list):
        return []
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in outbox:
        if not isinstance(row, dict) or str(row.get("status") or "") not in {"pending", "retry_pending", "queued"}:
            continue
        key = _fingerprint(
            row.get("target_user_id") or row.get("touser"),
            row.get("task_id"), row.get("candidate_id"), row.get("focus_key"),
            row.get("notification_type") or row.get("action"),
        )
        groups.setdefault(key, []).append(row)
    findings: list[dict[str, Any]] = []
    for key, rows in groups.items():
        if len(rows) < 2:
            continue
        ids = sorted(str(row.get("id") or "") for row in rows if str(row.get("id") or ""))
        findings.append({
            "category": "duplicate_unsent_candidate",
            "severity": "p1",
            "scope": key,
            "repair_action": "supersede_duplicate_unsent_candidate",
            "summary": f"发现 {len(ids)} 条相同且尚未发送的候选。",
            "evidence": [_evidence_ref("notification_outbox", key, count=len(ids), detail=",".join(ids[:8]))],
        })
    return findings


def _dashboard_divergence_findings(store: TuoguanStore, *, now: datetime) -> list[dict[str, Any]]:
    """Detect only stale cache timestamps here; never interpret business data twice."""

    cache = store.read_json("dashboard_cache.json", {})
    generated = _parse_time(cache.get("generated_at")) if isinstance(cache, dict) else None
    if not generated or now - generated > timedelta(hours=2):
        return [{
            "category": "dashboard_projection_stale",
            "severity": "p1",
            "scope": "dashboard_cache",
            "repair_action": "rebuild_dashboard_cache",
            "summary": "H5 看板缓存不存在或超过两小时未重建。",
            "evidence": [_evidence_ref("dashboard_cache", "generated_at", detail=str(cache.get("generated_at") if isinstance(cache, dict) else ""))],
        }]
    return []


def _runtime_findings(store: TuoguanStore, *, now: datetime) -> list[dict[str, Any]]:
    traces = [row for row in _read_jsonl(store, "turn_traces.jsonl") if _within(row.get("completed_at") or row.get("started_at"), now=now, hours=1)]
    timeouts = [
        row for row in traces
        if str(row.get("failure_type") or "") == "provider_timeout_or_interruption"
        or str(row.get("final_outcome") or "") == "failed" and "timeout" in json.dumps(row.get("provider_events") or [], ensure_ascii=False).lower()
    ]
    if len(timeouts) >= 2:
        return [{
            "category": "model_consecutive_timeout",
            "severity": "p0" if len(timeouts) >= 4 else "p1",
            "scope": "turn_traces:last_hour",
            "summary": f"最近一小时有 {len(timeouts)} 次模型超时或中断。",
            "evidence": [_evidence_ref("turn_trace", "provider_timeout_or_interruption", count=len(timeouts))],
        }]
    return []


def _commitment_findings(store: TuoguanStore, *, now: datetime) -> list[dict[str, Any]]:
    rows = [row for row in _read_jsonl(store, "reply_ledger.jsonl") if _within(row.get("completed_at") or row.get("created_at"), now=now, hours=24)]
    unverified = [
        row for row in rows
        if isinstance(row.get("workstyle_adaptation"), dict)
        and (row.get("workstyle_adaptation") or {}).get("unverified_commitment") is True
    ]
    if not unverified:
        return []
    return [{
        "category": "commitment_without_receipt",
        "severity": "p1",
        "scope": "reply_ledger:last_24h",
        "repair_action": "record_self_correction",
        "summary": f"最近24小时有 {len(unverified)} 次承诺没有对应写后反查。",
        "evidence": [_evidence_ref("reply_ledger", "unverified_commitment", count=len(unverified))],
    }]


def _workstyle_findings(store: TuoguanStore, *, now: datetime) -> list[dict[str, Any]]:
    from .workstyle_profiles import _active_preferences, _read_events

    def failure_dimensions(application: dict[str, Any]) -> set[str]:
        compliance = application.get("compliance") if isinstance(application.get("compliance"), dict) else {}
        failures = compliance.get("failures") if isinstance(compliance.get("failures"), list) else []
        dimensions: set[str] = set()
        for failure in failures:
            text = str(failure or "")
            if text.startswith("interaction_pacing_"):
                dimensions.add("interaction_pacing")
            elif text.startswith("reply_too_long_for_saved_length_"):
                dimensions.add("length")
            elif text.startswith("avoidance_term_present:"):
                dimensions.add("avoidance")
        return dimensions

    def still_applies(application: dict[str, Any]) -> bool:
        """Do not page on an old failed application after its rule was retired.

        The event ledger is intentionally append-only. A historical failed
        output remains valuable audit evidence, but it is no longer a current
        adaptation failure once the exact preference dimension is superseded.
        """

        target_user_id = str(application.get("target_user_id") or "")
        scope = str(application.get("scope") or "direct_reply")
        failed_dimensions = failure_dimensions(application)
        if not target_user_id or not failed_dimensions:
            return True
        active_dimensions = {
            str(item.get("dimension_key") or "")
            for item in _active_preferences(
                _read_events(store),
                target_user_id=target_user_id,
                scope=scope,
            )
        }
        return bool(failed_dimensions & active_dimensions)

    rows = [row for row in _read_jsonl(store, "reply_ledger.jsonl") if _within(row.get("completed_at") or row.get("created_at"), now=now, hours=24)]
    failed = [
        row for row in rows
        if isinstance(((row.get("workstyle_adaptation") or {}).get("application_result") or {}).get("application"), dict)
        and ((((row.get("workstyle_adaptation") or {}).get("application_result") or {}).get("application") or {}).get("compliance") or {}).get("ok") is False
        and still_applies(((row.get("workstyle_adaptation") or {}).get("application_result") or {}).get("application") or {})
    ]
    if not failed:
        return []
    return [{
        "category": "workstyle_saved_not_applied",
        "severity": "p1",
        "scope": "reply_ledger:last_24h",
        "repair_action": "reapply_verified_workstyle_context",
        "summary": f"最近24小时有 {len(failed)} 次已保存工作方式未在真实输出中生效。",
        "evidence": [_evidence_ref("reply_ledger", "workstyle_compliance", count=len(failed))],
    }]


def _verify_cleared_findings(
    store: TuoguanStore,
    *,
    detected: list[dict[str, Any]],
    categories: set[str],
    now: datetime,
    operation_id: str,
) -> list[dict[str, Any]]:
    """Record a technical finding as verified when its detector is now clean."""

    active_fingerprints = {
        _fingerprint(current_tenant_id(), item.get("category"), item.get("scope"))
        for item in detected
        if str(item.get("category") or "") in categories
    }
    verified: list[dict[str, Any]] = []
    for finding in _fold_findings(store).values():
        category = str(finding.get("category") or "")
        state = str(finding.get("state") or "")
        fingerprint = str(finding.get("fingerprint") or "")
        if category not in categories or state == "verified" or fingerprint in active_fingerprints:
            continue
        verified.append(_update_finding(
            store,
            finding,
            state="verified",
            operation_id=operation_id,
            patch={
                "verified_at": _iso(now),
                "verification": "deterministic_detector_clear",
                "resolution_note": "当前有效工作方式已不再复现该失败；历史审计记录保留。",
            },
        ))
    return verified


def _task_health(store: TuoguanStore, *, now: datetime) -> dict[str, Any]:
    """Return task-chain facts without interpreting a teacher's work result.

    The task ledger is authoritative. This helper deliberately reports only
    technical lifecycle evidence, so the supervisor can repair projections but
    never decide whether a real task should be completed or cancelled.
    """

    from .tasks import task_is_closed, task_is_open

    tasks = [row for row in store.load_tasks() if isinstance(row, dict)]
    outbox = store.read_json("notification_outbox.json", [])
    outbox = outbox if isinstance(outbox, list) else []
    notifications_by_task: dict[str, list[dict[str, Any]]] = {}
    for row in outbox:
        if not isinstance(row, dict):
            continue
        task_id = str(row.get("task_id") or "")
        if task_id:
            notifications_by_task.setdefault(task_id, []).append(row)

    recent_open_without_delivery: list[str] = []
    delivery_stuck: list[str] = []
    closed_with_open_coach: list[str] = []
    for task in tasks:
        task_id = str(task.get("id") or "")
        if not task_id:
            continue
        if task_is_closed(task) and str(task.get("coach_stage") or "") != "closed":
            closed_with_open_coach.append(task_id)
            continue
        if not task_is_open(task):
            continue
        created_at = task.get("created_at") or task.get("updated_at")
        # Historic tasks may predate delivery receipts. Only inspect recent
        # tasks, where a missing queue entry is a current creation-chain fault.
        if not _within(created_at, now=now, hours=48):
            continue
        notifications = notifications_by_task.get(task_id, [])
        statuses = {str(item.get("status") or "") for item in notifications}
        if not notifications and str(task.get("delivery_status") or "") not in {"sent", "suppressed"}:
            recent_open_without_delivery.append(task_id)
        if statuses & {"pending", "queued", "sending", "retry_pending", "result_unknown", "failed"}:
            delivery_stuck.append(task_id)

    return {
        "as_of": _iso(now),
        "total_task_count": len(tasks),
        "open_task_count": sum(1 for task in tasks if task_is_open(task)),
        "closed_task_count": sum(1 for task in tasks if task_is_closed(task)),
        "recent_open_without_delivery": recent_open_without_delivery,
        "delivery_stuck": delivery_stuck,
        "closed_with_open_coach": closed_with_open_coach,
    }


def _task_chain_findings(store: TuoguanStore, *, now: datetime) -> list[dict[str, Any]]:
    """Find task lifecycle faults without modifying a task's business state."""

    health = _task_health(store, now=now)
    findings: list[dict[str, Any]] = []
    no_delivery = health["recent_open_without_delivery"]
    if no_delivery:
        findings.append({
            "category": "task_created_without_notification_receipt",
            "severity": "p1",
            "scope": "tasks:recent_creation_delivery",
            "summary": f"有 {len(no_delivery)} 个近48小时创建的开放任务没有通知入队或送达证据。",
            "evidence": [_evidence_ref("tasks", "recent_open_without_delivery", count=len(no_delivery), detail=",".join(no_delivery[:8]))],
        })
    delivery_stuck = health["delivery_stuck"]
    if delivery_stuck:
        findings.append({
            "category": "task_notification_nonterminal",
            "severity": "p1",
            "scope": "notification_outbox:task_delivery",
            "summary": f"有 {len(delivery_stuck)} 个开放任务的通知仍未到达终态。",
            "evidence": [_evidence_ref("notification_outbox", "task_delivery_nonterminal", count=len(delivery_stuck), detail=",".join(delivery_stuck[:8]))],
        })
    open_coach = health["closed_with_open_coach"]
    if open_coach:
        findings.append({
            "category": "closed_task_coach_projection_open",
            "severity": "p1",
            "scope": "tasks:coach_stage",
            "summary": f"有 {len(open_coach)} 个已关闭任务仍保持开放陪伴投影。",
            "evidence": [_evidence_ref("tasks", "closed_task_coach_stage", count=len(open_coach), detail=",".join(open_coach[:8]))],
        })
    return findings


def build_supervision_snapshot(store: TuoguanStore, *, now: datetime | None = None) -> dict[str, Any]:
    timestamp = now or _now()
    task_health = _task_health(store, now=timestamp)
    return {
        "schema_version": 1,
        "tenant_id": current_tenant_id(),
        "generated_at": _iso(timestamp),
        "privacy": {"contains_chat_body": False, "contains_keys": False, "contains_parent_privacy": False},
        "source_counts": {
            "turn_traces_last_24h": sum(1 for row in _read_jsonl(store, "turn_traces.jsonl") if _within(row.get("completed_at") or row.get("started_at"), now=timestamp, hours=24)),
            "reply_ledger_last_24h": sum(1 for row in _read_jsonl(store, "reply_ledger.jsonl") if _within(row.get("completed_at") or row.get("created_at"), now=timestamp, hours=24)),
            "outbox_rows": len(store.read_json("notification_outbox.json", []) if isinstance(store.read_json("notification_outbox.json", []), list) else []),
            "work_items": len(_read_jsonl(store, "hermes_work_items.jsonl")),
            "tasks": task_health["total_task_count"],
        },
        "boundary": {
            "sends_messages": False,
            "writes_business_facts": False,
            "changes_permissions": False,
            "changes_code_or_config": False,
            "advisory_agents_read_only": True,
        },
    }


def scan_supervision(
    store: TuoguanStore,
    *,
    now: datetime | None = None,
    run_kind: str = "periodic",
    apply_repairs: bool = False,
    operation_id: str = "",
    advisor_call: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run deterministic checks.  Model advisors are intentionally not invoked here."""

    timestamp = now or _now()
    op_id = operation_id or f"supervision:{run_kind}:{timestamp.strftime('%Y%m%d%H%M%S')}"
    snapshot = build_supervision_snapshot(store, now=timestamp)
    detected: list[dict[str, Any]] = []
    detected.extend(_commitment_findings(store, now=timestamp))
    detected.extend(_workstyle_findings(store, now=timestamp))
    detected.extend(_task_chain_findings(store, now=timestamp))
    detected.extend(_stale_context_findings(store, now=timestamp))
    detected.extend(_duplicate_unsent_candidate_findings(store))
    detected.extend(_dashboard_divergence_findings(store, now=timestamp))
    detected.extend(_runtime_findings(store, now=timestamp))
    findings: list[dict[str, Any]] = []
    new_count = 0
    for item in detected:
        finding, created = _open_or_recur_finding(store, now=timestamp, operation_id=op_id, **item)
        findings.append(finding)
        new_count += int(created)
    verified_findings = _verify_cleared_findings(
        store,
        detected=detected,
        categories={"workstyle_saved_not_applied"},
        now=timestamp,
        operation_id=op_id,
    )
    repairs: list[dict[str, Any]] = []
    if apply_repairs:
        for finding in findings:
            if str(finding.get("repair_action") or "") in AUTO_REPAIR_ACTIONS:
                repairs.append(apply_low_risk_repair(store, finding=finding, operation_id=op_id, now=timestamp))
    council = run_supervision_council(
        snapshot,
        findings,
        advisor_call=advisor_call,
        nightly=run_kind == "nightly",
    )
    run = {
        "record_type": "supervision_run",
        "run_id": _id("supervision_run"),
        "tenant_id": current_tenant_id(),
        "run_kind": run_kind,
        "generated_at": _iso(timestamp),
        "status": "completed",
        "snapshot": snapshot,
        "detected_count": len(findings),
        "new_finding_count": new_count,
        "verified_finding_count": len(verified_findings),
        "repair_count": len(repairs),
        "model_advisors": council,
        "boundary": snapshot["boundary"],
    }
    _append(store, SUPERVISION_RUNS_FILE, run)
    return {
        "ok": True,
        "run": run,
        "findings": [_public_finding(item) for item in findings],
        "verified_findings": [_public_finding(item) for item in verified_findings],
        "repairs": repairs,
        "writeback_verified": True,
        "rendered_text": f"监督巡检完成：发现 {len(findings)} 项，已执行 {len(repairs)} 项低风险技术修复。",
    }


def _repair_context_projection(store: TuoguanStore) -> dict[str, Any]:
    from .digital_employee_state import _fold_hermes_work_items
    from .tasks import task_is_closed

    work_items = _fold_hermes_work_items(store)
    tasks = {str(item.get("id") or ""): item for item in store.load_tasks() if isinstance(item, dict)}
    counts: dict[str, int] = {}
    for filename in ("active_task_context.json", "pending_next_task_context.json", "model_focus.json"):
        def prune(value: Any) -> Any:
            if isinstance(value, dict):
                result: dict[str, Any] = {}
                removed = 0
                for key, row in value.items():
                    if not isinstance(row, dict):
                        result[key] = deepcopy(row)
                        continue
                    task_id = str(row.get("task_id") or row.get("id") or "")
                    work_id = str(row.get("work_item_id") or "")
                    stale = bool(task_id and (task_id not in tasks or task_is_closed(tasks.get(task_id))))
                    stale = stale or bool(work_id and (work_id not in work_items or str(work_items[work_id].get("status") or "") in {"closed", "superseded"}))
                    if stale:
                        removed += 1
                    else:
                        result[key] = deepcopy(row)
                counts[filename] = removed
                return result if removed else JSON_NO_CHANGE
            return JSON_NO_CHANGE
        store.update_json(filename, {}, prune)
    return {"removed_projection_entries": counts, "verified": True}


def _repair_duplicate_unsent_candidates(store: TuoguanStore) -> dict[str, Any]:
    superseded: list[str] = []
    def mutate(rows: Any) -> Any:
        if not isinstance(rows, list):
            return JSON_NO_CHANGE
        groups: dict[str, list[tuple[int, dict[str, Any]]]] = {}
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or str(row.get("status") or "") not in {"pending", "retry_pending", "queued"}:
                continue
            key = _fingerprint(row.get("target_user_id") or row.get("touser"), row.get("task_id"), row.get("candidate_id"), row.get("focus_key"), row.get("notification_type") or row.get("action"))
            groups.setdefault(key, []).append((index, row))
        changed = False
        for entries in groups.values():
            if len(entries) < 2:
                continue
            entries.sort(key=lambda item: str(item[1].get("created_at") or ""))
            for index, row in entries[1:]:
                copy = deepcopy(row)
                copy["status"] = "superseded"
                copy["superseded_at"] = _iso()
                copy["superseded_reason"] = "supervision_duplicate_unsent_candidate"
                rows[index] = copy
                superseded.append(str(copy.get("id") or ""))
                changed = True
        return rows if changed else JSON_NO_CHANGE
    store.update_json("notification_outbox.json", [], mutate)
    return {"superseded_outbox_ids": [item for item in superseded if item], "verified": True}


def _repair_dashboard_cache(store: TuoguanStore) -> dict[str, Any]:
    from .dashboard_builder import refresh_dashboard_cache

    result = refresh_dashboard_cache(store)
    cache = store.read_json("dashboard_cache.json", {})
    return {"refresh": result, "verified": bool(isinstance(cache, dict) and cache.get("generated_at"))}


def _repair_self_correction(store: TuoguanStore, finding: dict[str, Any], *, operation_id: str) -> dict[str, Any]:
    from .models import UserIdentity
    from .self_evolution import SELF_EVOLUTION_EVENTS_FILE, submit_self_evolution_event

    identity = UserIdentity("system", "supervision", "supervision", "小优监督", "system", "approved")
    result = submit_self_evolution_event(
        store,
        identity=identity,
        operation_id=f"{operation_id}:{finding.get('finding_id')}:self_correction",
        candidate_type="self_correction",
        summary=f"监督发现：{str(finding.get('summary') or '')[:280]}",
        evidence=[{"source": "supervision_finding", "finding_id": str(finding.get("finding_id") or ""), "category": str(finding.get("category") or "")}],
        source_text="监督系统的脱敏运行证据。",
        source_message_id="",
        cadence_mode="supervision",
    )
    return {"self_evolution": result, "verified": bool(result.get("writeback_verified"))}


def apply_low_risk_repair(
    store: TuoguanStore,
    *,
    finding: dict[str, Any],
    operation_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    action = str(finding.get("repair_action") or "")
    if action not in AUTO_REPAIR_ACTIONS:
        return {"ok": False, "error": "repair_not_allowlisted", "finding_id": finding.get("finding_id")}
    timestamp = now or _now()
    files = {
        "rebuild_dashboard_cache": {"dashboard_cache.json"},
        "rebuild_context_projection": {"active_task_context.json", "pending_next_task_context.json", "model_focus.json"},
        "supersede_duplicate_unsent_candidate": {"notification_outbox.json"},
        "record_self_correction": {"self_evolution_events.jsonl"},
        # Reapplying only means rebuilding the next-turn projection; personal
        # preferences themselves are never edited by the supervisor.
        "reapply_verified_workstyle_context": {"active_task_context.json", "pending_next_task_context.json", "model_focus.json"},
    }[action]
    files = set(files) | {SUPERVISION_FINDINGS_FILE, SUPERVISION_REPAIRS_FILE}
    repair_id = _id("supervision_repair")
    before = {name: _fingerprint(store.path_for(name).read_bytes() if store.path_for(name).exists() else "") for name in files}
    try:
        with authorized_system_write(store.data_dir, job_name=f"supervision_{action}", allowed_files=files):
            if action == "rebuild_dashboard_cache":
                outcome = _repair_dashboard_cache(store)
            elif action in {"rebuild_context_projection", "reapply_verified_workstyle_context"}:
                outcome = _repair_context_projection(store)
            elif action == "supersede_duplicate_unsent_candidate":
                outcome = _repair_duplicate_unsent_candidates(store)
            else:
                outcome = _repair_self_correction(store, finding, operation_id=operation_id)
            verified = bool(outcome.get("verified"))
            receipt = {
                "record_type": "supervision_repair",
                "repair_id": repair_id,
                "tenant_id": current_tenant_id(),
                "finding_id": str(finding.get("finding_id") or ""),
                "action": action,
                "status": "verified" if verified else "failed",
                "before_hashes": before,
                "allowed_files": sorted(files),
                "outcome": deepcopy(outcome),
                "operation_id": operation_id,
                "created_at": _iso(timestamp),
            }
            _append(store, SUPERVISION_REPAIRS_FILE, receipt)
            state = "verified" if verified else "failed"
            _update_finding(store, finding, state=state, operation_id=operation_id, patch={"last_repair_id": repair_id, "last_repair_at": _iso(timestamp)})
    except Exception as exc:
        receipt = {
            "record_type": "supervision_repair",
            "repair_id": repair_id,
            "tenant_id": current_tenant_id(),
            "finding_id": str(finding.get("finding_id") or ""),
            "action": action,
            "status": "failed",
            "error": type(exc).__name__,
            "operation_id": operation_id,
            "created_at": _iso(timestamp),
        }
        with authorized_system_write(store.data_dir, job_name="supervision_failed_repair_receipt", allowed_files={SUPERVISION_REPAIRS_FILE, SUPERVISION_FINDINGS_FILE}):
            _append(store, SUPERVISION_REPAIRS_FILE, receipt)
            _update_finding(store, finding, state="failed", operation_id=operation_id, patch={"last_repair_id": repair_id})
        return {"ok": False, "repair": receipt}
    return {"ok": bool(receipt.get("status") == "verified"), "repair": receipt, "writeback_verified": bool(receipt.get("status") == "verified")}


def query_supervision_status(store: TuoguanStore, *, limit: int = 20) -> dict[str, Any]:
    findings = list(_fold_findings(store).values())
    findings.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
    repairs = _read_jsonl(store, SUPERVISION_REPAIRS_FILE)
    repairs.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    rows = [_public_finding(item) for item in findings[: max(1, min(int(limit or 20), 50))]]
    active = [item for item in findings if str(item.get("state") or "") not in TERMINAL_FINDING_STATES]
    p0 = [item for item in findings if str(item.get("severity") or "") == "p0" and str(item.get("state") or "") not in {"verified", "false_positive"}]
    return {
        "ok": True,
        "report_type": "xiaoyou_supervision_v1",
        "tenant_id": current_tenant_id(),
        "read_only": True,
        "active_finding_count": len(active),
        "p0_open_count": len(p0),
        "finding_count": len(findings),
        "recent_repairs": [
            {key: deepcopy(row.get(key)) for key in ("repair_id", "finding_id", "action", "status", "created_at")}
            for row in repairs[:5]
        ],
        "findings": rows,
        "task_health": _task_health(store, now=_now()),
        "boundary": {
            "supervisor_changes_business_facts": False,
            "supervisor_sends_messages": False,
            "supervisor_changes_permissions": False,
            "advisors_are_read_only": True,
        },
        "rendered_text": f"监督状态：开放问题 {len(active)} 项，其中 P0 {len(p0)} 项；最近技术修复 {len(repairs[:5])} 项。",
    }


def run_supervision_council(
    snapshot: dict[str, Any],
    findings: list[dict[str, Any]],
    *,
    advisor_call: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    nightly: bool = False,
) -> dict[str, Any]:
    """Run at most three isolated, read-only advisor calls.

    The callable is injected by a dedicated restricted Hermes profile.  This
    module never imports a tool registry or grants a child agent any channel,
    file, terminal, memory, session-search, cron, or delegation capability.
    It also deliberately returns advice instead of applying it.
    """

    compact_snapshot = {
        "tenant_id": str(snapshot.get("tenant_id") or ""),
        "generated_at": str(snapshot.get("generated_at") or ""),
        "privacy": deepcopy(snapshot.get("privacy") or {}),
        "source_counts": deepcopy(snapshot.get("source_counts") or {}),
        "boundary": deepcopy(snapshot.get("boundary") or {}),
        "findings": [_public_finding(item) for item in findings[:3]],
    }
    if not findings:
        return {"status": "not_needed", "advisors": [], "read_only": True}
    if advisor_call is None:
        return {"status": "deferred_model_unavailable", "advisors": [], "read_only": True}
    advisors: list[dict[str, Any]] = []
    roles = NIGHTLY_ADVISOR_ROLES if nightly else ADVISOR_ROLES
    for role in roles:
        try:
            raw = advisor_call(role, deepcopy(compact_snapshot))
        except Exception as exc:
            advisors.append({"role": role, "status": "failed", "error": type(exc).__name__})
            continue
        if not isinstance(raw, dict):
            advisors.append({"role": role, "status": "failed", "error": "invalid_advice"})
            continue
        allowed = {
            "problem", "evidence", "reproduction", "root_cause", "risk_level",
            "recommended_repair", "forbidden_actions", "verification_method",
        }
        advice = {key: deepcopy(value) for key, value in raw.items() if key in allowed}
        if not str(advice.get("problem") or "").strip() or not str(advice.get("verification_method") or "").strip():
            advisors.append({"role": role, "status": "failed", "error": "incomplete_advice"})
            continue
        forbidden = advice.get("forbidden_actions") if isinstance(advice.get("forbidden_actions"), list) else []
        advice["forbidden_actions"] = [str(item)[:120] for item in forbidden[:8]]
        advice["recommended_repair"] = str(advice.get("recommended_repair") or "")[:500]
        advice["status"] = "completed"
        advice["role"] = role
        advisors.append(advice)
    return {
        "status": "completed" if any(item.get("status") == "completed" for item in advisors) else "deferred_model_unavailable",
        "advisors": advisors,
        "read_only": True,
        "max_depth": 1,
        "max_agents": 3,
    }


def restricted_advisor_call(role: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    """Ask one isolated, no-tool advisor for a recommendation only.

    This path is opt-in for the production shadow phase.  It intentionally
    calls only the primary Agnes configuration, honors the existing circuit
    state, and never gives the advisor channel, tool, file, or memory access.
    """

    if str(os.getenv("HERMES_SUPERVISION_COUNCIL_ENABLED") or "").strip().lower() not in {"1", "true", "yes", "on"}:
        raise RuntimeError("supervision_council_disabled")
    try:
        from .provider_resilience import provider_health_snapshot

        health = provider_health_snapshot()
        agnes = health.get("agnes") if isinstance(health, dict) else {}
        if str((agnes or {}).get("state") or "closed") != "closed":
            raise RuntimeError("provider_circuit_not_closed")
        from .autonomous_employee_loop import _load_model_configs

        configs = _load_model_configs()
        config = next((item for item in configs if "agnes" in str(item.get("model") or "").lower()), None)
        if not isinstance(config, dict):
            raise RuntimeError("agnes_supervision_config_missing")
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError("supervision_model_config_unavailable") from exc

    prompt = (
        "你是小优的只读监督顾问，不是对外数字员工。只能审查传入的脱敏运行快照，"
        "不得推断聊天原文、人员私密事实或业务结论；不得调用工具、发送消息、写入任何数据、"
        "创建任务、修改制度、权限、工资或配置。你的意见不是事实。"
        "只返回 JSON object，字段固定为 problem,evidence,reproduction,root_cause,risk_level,"
        "recommended_repair,forbidden_actions,verification_method。"
        f"你的审查方向是 {role}。"
    )
    response = httpx.post(
        f"{str(config.get('base_url') or '').rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {str(config.get('api_key') or '')}", "Content-Type": "application/json"},
        json={
            "model": str(config.get("model") or "agnes-2.5-flash"),
            "temperature": 0,
            "max_tokens": 500,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(snapshot, ensure_ascii=False)},
            ],
        },
        timeout=8,
    )
    response.raise_for_status()
    payload = response.json()
    choices = payload.get("choices") if isinstance(payload, dict) else []
    message = choices[0].get("message") if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    content = str((message or {}).get("content") or "")
    value = json.loads(content)
    if not isinstance(value, dict):
        raise RuntimeError("supervision_advisor_invalid_json")
    return value


def _public_finding(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: deepcopy(item.get(key))
        for key in (
            "finding_id", "category", "severity", "state", "summary", "scope",
            "recurrence_count", "first_seen_at", "last_seen_at", "updated_at",
            "repair_action", "last_repair_id", "regression",
        )
    } | {"evidence_count": len(item.get("evidence") or [])}


def should_run_supervision(now: datetime | None = None) -> bool:
    timestamp = now or _now()
    return timestamp.minute in {5, 35}
