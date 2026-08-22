from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _healthy_sample(now: datetime) -> dict:
    return {
        "sample_at": now.isoformat(timespec="seconds"),
        "hour_key": now.strftime("%Y-%m-%dT%H"),
        "p0_signals": [],
    }


def test_observer_extracts_p0_without_persisting_business_content():
    from scripts.xiaoyou_reliability_observer import build_observation_sample

    now = datetime(2026, 8, 22, 10, tzinfo=timezone.utc)
    sample = build_observation_sample(
        {
            "status": "attention_needed",
            "daily_reports": {"sent_last_24h": False, "sent_count_last_24h": 0},
            "proactive_work": {
                "outbox": {"failed_or_unknown_count": 1, "repeated_task_reminder_candidate_count": 1},
                "execution_loop": {"stuck_candidate_count": 2},
            },
            "runtime_learning": {
                "unverified_commitment_count": 1,
                "inbound_receipts": {"failed_count": 1},
                "turn_trace": {
                    "context_guard_failure_count": 1,
                    "writeback_failure_count": 1,
                    "wrong_tool_count": 2,
                    "failed_turn_count": 1,
                    "performance": {"simple_reply_p95_ms": 13000},
                },
            },
            "market_learning": {"latest_status": "source_failed"},
            "public_learning": {
                "latest_status": "completed_no_relevant_sources",
                "accepted_evidence_count_last_24h": 0,
                "rejected_irrelevant_count_last_24h": 4,
            },
        },
        now=now,
    )

    assert set(sample["p0_signals"]) == {
        "daily_delivery_missing", "outbox_failed_or_unknown", "duplicate_task_reminder",
        "context_guard_failure", "writeback_failure", "unverified_commitment",
        "inbound_processing_failure",
    }
    assert sample["contains_business_content"] is False
    assert "wrong_tool_recurred" in sample["warnings"]
    assert "public_learning_no_relevant_sources" in sample["warnings"]
    assert sample["public_learning_rejected_source_count"] == 4


def test_observer_requires_same_release_and_continuous_14_day_evidence():
    from scripts.xiaoyou_reliability_observer import evaluate_observation

    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    release = {"source_commit": "abc123"}
    samples = [_healthy_sample(start + timedelta(hours=index)) for index in range(337)]
    state = {
        "started_at": start.isoformat(),
        "planned_end_at": (start + timedelta(days=14)).isoformat(),
        "source_commit": "abc123",
        "samples": samples,
        "p0_events": [],
    }

    passed = evaluate_observation(state, now=start + timedelta(days=14, hours=1), current_release=release)
    assert passed["status"] == "passed"
    assert passed["continuous_evidence"] is True

    changed = evaluate_observation(
        state, now=start + timedelta(days=14, hours=1),
        current_release={"source_commit": "different"},
    )
    assert changed["status"] == "reset_required"


def test_observer_deduplicates_same_hour_and_keeps_p0_history():
    from scripts.xiaoyou_reliability_observer import update_observation

    now = datetime(2026, 8, 22, 10, tzinfo=timezone.utc)
    release = {"release_id": "r1", "source_commit": "abc", "hermes_version": "0.20.0", "model": "agnes-2.5-flash", "manifest_path": ""}
    first = {**_healthy_sample(now), "p0_signals": ["unverified_commitment"]}
    state = update_observation({}, sample=first, now=now, current_release=release, start=True)
    replacement = _healthy_sample(now + timedelta(minutes=20))
    state = update_observation(state, sample=replacement, now=now + timedelta(minutes=20), current_release=release, start=False)

    assert len(state["samples"]) == 1
    assert len(state["p0_events"]) == 1
    assert state["evaluation"]["status"] == "failed"
