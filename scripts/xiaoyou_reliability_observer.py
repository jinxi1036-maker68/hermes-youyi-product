#!/usr/bin/env python3
"""Record privacy-safe evidence for Xiaoyou's 14-day release freeze."""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE = Path("/opt/hermes-youyi-current")
DEFAULT_DATA_DIR = Path("/opt/hermes-youyi/data/tuoguan-data")
OBSERVATION_DAYS = 14
MIN_HOURLY_SAMPLES = 300
MAX_ALLOWED_GAP_HOURS = 3.0


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.astimezone()


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _release_manifest_candidates(base: Path) -> list[Path]:
    candidates = [base / "release_manifest.json"]
    plugin = base / "runtime/plugins/tuoguan_core"
    try:
        resolved = plugin.resolve(strict=True)
    except OSError:
        return candidates
    candidates.extend(parent / "release_manifest.json" for parent in resolved.parents)
    return candidates


def detect_release(base: Path) -> dict[str, str]:
    for path in _release_manifest_candidates(base):
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        return {
            "release_id": str(payload.get("release_id") or ""),
            "source_commit": str(payload.get("source_commit") or ""),
            "hermes_version": str(payload.get("hermes_version") or ""),
            "model": str(payload.get("model") or ""),
            "manifest_path": str(path),
        }
    return {
        "release_id": "unknown",
        "source_commit": "unknown",
        "hermes_version": "0.20.0",
        "model": "agnes-2.5-flash",
        "manifest_path": "",
    }


def collect_health(data_dir: Path, *, now: datetime) -> dict[str, Any]:
    runtime = str(ROOT / "runtime")
    if runtime not in sys.path:
        sys.path.insert(0, runtime)
    from plugins.tuoguan_core.digital_employee_state import query_xiaoyou_health
    from plugins.tuoguan_core.models import UserIdentity
    from plugins.tuoguan_core.store import TuoguanStore

    identity = UserIdentity(
        platform="system",
        platform_user_id="reliability-observer",
        canonical_user_id="reliability-observer",
        person_name="reliability-observer",
        role="boss",
        approval_state="approved",
    )
    return query_xiaoyou_health(
        TuoguanStore(data_dir), identity=identity,
        now_at=now.isoformat(timespec="seconds"), limit=5,
    )


def build_observation_sample(health: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    daily = health.get("daily_reports") if isinstance(health.get("daily_reports"), dict) else {}
    proactive = health.get("proactive_work") if isinstance(health.get("proactive_work"), dict) else {}
    outbox = proactive.get("outbox") if isinstance(proactive.get("outbox"), dict) else {}
    execution = proactive.get("execution_loop") if isinstance(proactive.get("execution_loop"), dict) else {}
    runtime = health.get("runtime_learning") if isinstance(health.get("runtime_learning"), dict) else {}
    trace = runtime.get("turn_trace") if isinstance(runtime.get("turn_trace"), dict) else {}
    performance = trace.get("performance") if isinstance(trace.get("performance"), dict) else {}
    market = health.get("market_learning") if isinstance(health.get("market_learning"), dict) else {}
    public_learning = health.get("public_learning") if isinstance(health.get("public_learning"), dict) else {}
    p0_signals: list[str] = []
    checks = {
        "daily_delivery_missing": not bool(daily.get("sent_last_24h")),
        "outbox_failed_or_unknown": int(outbox.get("failed_or_unknown_count") or 0) > 0,
        "duplicate_task_reminder": int(outbox.get("repeated_task_reminder_candidate_count") or 0) > 0,
        "context_guard_failure": int(trace.get("context_guard_failure_count") or 0) > 0,
        "writeback_failure": int(trace.get("writeback_failure_count") or 0) > 0,
        "unverified_commitment": int(runtime.get("unverified_commitment_count") or 0) > 0,
        "inbound_processing_failure": int((runtime.get("inbound_receipts") or {}).get("failed_count") or 0) > 0,
    }
    p0_signals.extend(name for name, active in checks.items() if active)
    warnings: list[str] = []
    if int(trace.get("wrong_tool_count") or 0):
        warnings.append("wrong_tool_recurred")
    if int(trace.get("failed_turn_count") or 0):
        warnings.append("model_turn_failed")
    if int(execution.get("stuck_candidate_count") or 0):
        warnings.append("proactive_candidate_stuck")
    if str(market.get("latest_status") or "") in {"backend_unavailable", "source_failed"}:
        warnings.append("market_backend_or_source_failed")
    if str(public_learning.get("latest_status") or "") == "completed_no_relevant_sources":
        warnings.append("public_learning_no_relevant_sources")
    return {
        "sample_at": now.isoformat(timespec="seconds"),
        "hour_key": now.strftime("%Y-%m-%dT%H"),
        "health_status": str(health.get("status") or "unknown"),
        "p0_signals": p0_signals,
        "warnings": warnings,
        "daily_sent_last_24h": bool(daily.get("sent_last_24h")),
        "daily_sent_count_last_24h": int(daily.get("sent_count_last_24h") or 0),
        "outbox_pending_count": int(outbox.get("pending_count") or 0),
        "outbox_failed_or_unknown_count": int(outbox.get("failed_or_unknown_count") or 0),
        "proactive_stuck_count": int(execution.get("stuck_candidate_count") or 0),
        "turn_count_last_24h": int(trace.get("turn_count_last_24h") or 0),
        "failed_turn_count": int(trace.get("failed_turn_count") or 0),
        "wrong_tool_count": int(trace.get("wrong_tool_count") or 0),
        "performance_ms": {
            "simple_p95": float(performance.get("simple_reply_p95_ms") or 0.0),
            "direct_read_p95": float(performance.get("direct_read_p95_ms") or 0.0),
            "complex_p95": float(performance.get("complex_reply_p95_ms") or 0.0),
        },
        "market_latest_status": str(market.get("latest_status") or ""),
        "public_learning_latest_status": str(public_learning.get("latest_status") or ""),
        "public_learning_accepted_source_count": int(public_learning.get("accepted_evidence_count_last_24h") or 0),
        "public_learning_rejected_source_count": int(public_learning.get("rejected_irrelevant_count_last_24h") or 0),
        "contains_business_content": False,
    }


def evaluate_observation(state: dict[str, Any], *, now: datetime, current_release: dict[str, str]) -> dict[str, Any]:
    started = _parse_time(str(state["started_at"]))
    planned_end = _parse_time(str(state["planned_end_at"]))
    samples = [row for row in (state.get("samples") or []) if isinstance(row, dict)]
    release_changed = str(current_release.get("source_commit") or "unknown") != str(state.get("source_commit") or "unknown")
    p0_events = [row for row in (state.get("p0_events") or []) if isinstance(row, dict)]
    sample_times = sorted(_parse_time(str(row["sample_at"])) for row in samples if row.get("sample_at"))
    gaps = [
        (later - earlier).total_seconds() / 3600
        for earlier, later in zip(sample_times, sample_times[1:])
    ]
    max_gap = max(gaps, default=0.0)
    covered_dates = {item.date().isoformat() for item in sample_times}
    continuous = len(samples) >= MIN_HOURLY_SAMPLES and max_gap <= MAX_ALLOWED_GAP_HOURS and len(covered_dates) >= OBSERVATION_DAYS
    if release_changed:
        status = "reset_required"
    elif p0_events:
        status = "failed"
    elif now < planned_end:
        status = "observing"
    elif not continuous:
        status = "insufficient_evidence"
    else:
        status = "passed"
    return {
        "status": status,
        "started_at": started.isoformat(timespec="seconds"),
        "planned_end_at": planned_end.isoformat(timespec="seconds"),
        "elapsed_days": round(max(0.0, (now - started).total_seconds() / 86400), 3),
        "remaining_days": round(max(0.0, (planned_end - now).total_seconds() / 86400), 3),
        "sample_count": len(samples),
        "covered_date_count": len(covered_dates),
        "max_sample_gap_hours": round(max_gap, 3),
        "continuous_evidence": continuous,
        "p0_event_count": len(p0_events),
        "release_changed": release_changed,
        "expected_source_commit": str(state.get("source_commit") or "unknown"),
        "current_source_commit": str(current_release.get("source_commit") or "unknown"),
    }


def update_observation(
    state: dict[str, Any], *, sample: dict[str, Any], now: datetime,
    current_release: dict[str, str], start: bool,
) -> dict[str, Any]:
    if start or not state:
        state = {
            "schema_version": "xiaoyou_reliability_observation_v1",
            "started_at": now.isoformat(timespec="seconds"),
            "planned_end_at": (now + timedelta(days=OBSERVATION_DAYS)).isoformat(timespec="seconds"),
            **current_release,
            "samples": [],
            "p0_events": [],
        }
    samples = [row for row in (state.get("samples") or []) if isinstance(row, dict)]
    samples = [row for row in samples if str(row.get("hour_key") or "") != str(sample.get("hour_key") or "")]
    samples.append(deepcopy(sample))
    state["samples"] = samples[-400:]
    existing_events = {
        (str(row.get("hour_key") or ""), str(row.get("signal") or ""))
        for row in (state.get("p0_events") or []) if isinstance(row, dict)
    }
    events = [row for row in (state.get("p0_events") or []) if isinstance(row, dict)]
    for signal in sample.get("p0_signals") or []:
        key = (str(sample.get("hour_key") or ""), str(signal))
        if key not in existing_events:
            events.append({"hour_key": key[0], "signal": key[1], "detected_at": sample["sample_at"]})
    state["p0_events"] = events
    state["last_sample_at"] = sample["sample_at"]
    state["evaluation"] = evaluate_observation(state, now=now, current_release=current_release)
    return state


def main() -> int:
    parser = argparse.ArgumentParser(description="Record Xiaoyou's 14-day reliability evidence without changing business data.")
    parser.add_argument("--base", type=Path, default=DEFAULT_BASE)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--report-file", type=Path)
    parser.add_argument("--now", default="")
    parser.add_argument("--start", action="store_true")
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args()
    now = _parse_time(args.now) if args.now else datetime.now().astimezone()
    state = {}
    if args.state_file.is_file():
        state = json.loads(args.state_file.read_text(encoding="utf-8"))
    release = detect_release(args.base)
    health = collect_health(args.data_dir, now=now)
    sample = build_observation_sample(health, now=now)
    updated = update_observation(state, sample=sample, now=now, current_release=release, start=args.start)
    report = {
        "report_type": "xiaoyou_reliability_observer_v1",
        "read_only_business_data": True,
        "writes_business_data": False,
        "release": release,
        "latest_sample": sample,
        "evaluation": updated["evaluation"],
    }
    if not args.no_write:
        _atomic_write_json(args.state_file, updated)
        if args.report_file:
            _atomic_write_json(args.report_file, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if updated["evaluation"]["status"] in {"failed", "reset_required"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
