"""Central authorization gate for production business-file writes."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import inspect
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Iterator
import uuid

from .tenant_context import current_tenant_id


PROTECTED_BUSINESS_FILES = {
    "tasks.json",
    "records.json",
    "students.json",
    "summer_enrollments.json",
    "trial_leads.json",
    "task_closure_events.json",
    "notification_outbox.json",
    "notification_deliveries.json",
    "dashboard_cache.json",
    "point_events.json",
    "summer_points.json",
    "core_workflow_contexts.json",
    "safety_test_events.json",
    "goal_operator_goals.json",
    "goal_operator_events.jsonl",
    "institution_operating_model.json",
    "youyi_operating_model.json",
    "operational_facts.json",
    "operational_fact_candidates.jsonl",
    "person_workstyle_events.jsonl",
    "institution_onboarding_state.json",
    "service_relations.json",
    "service_relation_candidates.jsonl",
    "information_requests.jsonl",
    "profile_candidates.jsonl",
    "profile_candidate_corrections.jsonl",
    "goal_evidence.jsonl",
    "performance_evidence_candidates.jsonl",
    "performance_evidence_responses.jsonl",
    "value_ledger.jsonl",
    "gray_observations.jsonl",
    "gray_rollout_decisions.jsonl",
    "gray_optimization_decisions.jsonl",
    "hermes_work_items.jsonl",
    "wakeup_requests.jsonl",
    "business_events.jsonl",
    "action_executions.jsonl",
    "daily_report_runs.jsonl",
    "institution_understanding_state.json",
    "hermes_employee_scorecard.jsonl",
    "industry_learning_candidates.jsonl",
    "institution_fact_gap_events.jsonl",
    "value_progress_ledger.jsonl",
    "agent_delegations.jsonl",
    "agent_delegation_results.jsonl",
    "attention_threads.jsonl",
    "external_research_runs.jsonl",
    "market_research_candidates.jsonl",
    "competitor_profiles.jsonl",
    "social_market_research_config.json",
    "social_market_research_runs.jsonl",
    "social_market_research_candidates.jsonl",
    "weekly_market_report_runs.jsonl",
    "relationship_touch_candidates.jsonl",
    "relationship_touch_policy.json",
    "proactive_authorizations.jsonl",
    "goal_actions.jsonl",
    "staff_voice_signals.jsonl",
    "self_evolution_events.jsonl",
    "turn_traces.jsonl",
    "tool_operations.json",
}


@dataclass(frozen=True)
class WriteAuthorization:
    source: str
    operation_id: str
    ledger_id: str
    audit_id: str
    allowed_files: tuple[str, ...] = ()


_ACTIVE_WRITE: ContextVar[WriteAuthorization | None] = ContextVar(
    "tuoguan_active_business_write", default=None
)
_AUDIT_LOCK = threading.RLock()


def _append_audit_row(data_dir: Path, row: dict[str, Any]) -> None:
    """Append security audit evidence using the store-compatible file lock."""

    path = data_dir / "business_action_audit.jsonl"
    lock_path = data_dir / ".business_action_audit.jsonl.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
    with _AUDIT_LOCK:
        deadline = time.monotonic() + 10
        fd: int | None = None
        while fd is None:
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode("ascii", errors="ignore"))
            except (FileExistsError, PermissionError):
                if time.monotonic() > deadline:
                    try:
                        if lock_path.exists() and time.time() - lock_path.stat().st_mtime > 30:
                            lock_path.unlink()
                            continue
                    except OSError:
                        pass
                    raise PermissionError("Timed out recording business write audit")
                time.sleep(0.05)
        try:
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(serialized + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            if fd is not None:
                os.close(fd)
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass


def _guard_config(data_dir: Path) -> dict[str, Any]:
    path = data_dir / "write_guard_config.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def guard_enabled(data_dir: Path) -> bool:
    return _guard_config(data_dir).get("enabled") is True


def _append_critical_audit(data_dir: Path, payload: dict[str, Any]) -> str:
    audit_id = str(payload.get("audit_event_id") or f"audit_critical_{uuid.uuid4().hex}")
    row = {
        "audit_event_id": audit_id,
        "tenant_id": current_tenant_id(),
        "channel": "wecom_callback",
        "event": "unauthorized_write_blocked",
        "action": "business_write_guard",
        "result": "critical",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        **payload,
    }
    _append_audit_row(data_dir, row)
    return audit_id


def record_unauthorized_tool_attempt(
    data_dir: Path,
    *,
    operation: str,
    operation_id: str,
    user_id: str,
    reason: str,
) -> str:
    return _append_critical_audit(data_dir, {
        "operation": operation,
        "operation_id": operation_id,
        "actor_user_id": user_id,
        "reason": reason,
    })


@contextmanager
def authorized_system_write(
    data_dir: Path,
    *,
    job_name: str,
    allowed_files: set[str] | tuple[str, ...],
) -> Iterator[WriteAuthorization]:
    """Authorize an audited scheduler/cache write outside a user request."""
    prepared = prepare_system_write(data_dir, job_name=job_name, allowed_files=allowed_files)
    with authorized_business_write(
        source="system_job",
        operation_id=prepared["operation_id"],
        ledger_id=prepared["ledger_id"],
        audit_id=prepared["audit_id"],
        allowed_files=allowed_files,
    ) as auth:
        yield auth


def prepare_system_write(
    data_dir: Path,
    *,
    job_name: str,
    allowed_files: set[str] | tuple[str, ...],
) -> dict[str, str]:
    stamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    audit_id = f"audit_system_write_{uuid.uuid4().hex}"
    ledger_id = f"system_job:{job_name}:{stamp}"
    row = {
        "audit_event_id": audit_id,
        "ledger_id": ledger_id,
        "tenant_id": current_tenant_id(),
        "channel": "system_job",
        "event": "system_write_authorized",
        "action": job_name,
        "result": "authorized",
        "allowed_files": sorted(allowed_files),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    _append_audit_row(data_dir, row)
    return {
        "operation_id": f"system:{job_name}:{stamp}",
        "ledger_id": ledger_id,
        "audit_id": audit_id,
    }


@contextmanager
def authorized_business_write(
    *,
    source: str,
    operation_id: str,
    ledger_id: str,
    audit_id: str,
    allowed_files: set[str] | tuple[str, ...] | None = None,
) -> Iterator[WriteAuthorization]:
    auth = WriteAuthorization(
        source=str(source),
        operation_id=str(operation_id),
        ledger_id=str(ledger_id),
        audit_id=str(audit_id),
        allowed_files=tuple(sorted(allowed_files or ())),
    )
    token = _ACTIVE_WRITE.set(auth)
    try:
        yield auth
    finally:
        _ACTIVE_WRITE.reset(token)


def assert_business_write_allowed(data_dir: Path, filename: str) -> None:
    if filename not in PROTECTED_BUSINESS_FILES or not guard_enabled(data_dir):
        return
    auth = _ACTIVE_WRITE.get()
    allowed = bool(
        auth
        and auth.operation_id
        and auth.ledger_id
        and auth.audit_id
        and (not auth.allowed_files or filename in auth.allowed_files)
    )
    if allowed:
        return
    caller = ""
    for frame in inspect.stack()[2:10]:
        if not frame.filename.endswith(("store.py", "write_guard.py")):
            caller = f"{Path(frame.filename).name}:{frame.function}:{frame.lineno}"
            break
    audit_id = _append_critical_audit(data_dir, {
        "file": filename,
        "reason": "missing_active_runtime_write_authorization",
        "caller": caller,
        "active_context": asdict(auth) if auth else None,
    })
    raise PermissionError(
        f"Unauthorized business write blocked: {filename} (audit={audit_id})"
    )


def protected_hashes(data_dir: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for filename in sorted(PROTECTED_BUSINESS_FILES):
        path = data_dir / filename
        if not path.exists():
            result[filename] = "missing"
            continue
        result[filename] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def changed_protected_hashes(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return sorted(name for name in set(before) | set(after) if before.get(name) != after.get(name))
